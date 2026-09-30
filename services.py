from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fuckclassroom.core.atomic import atomic_write_text
from fuckclassroom.core.plugins import PluginContext
from .assistant import CourseSelectionAssistant
from .automation import AutoSelectionJob
from .models import CourseSelectionApiError, SelectionBatch
from .proxy import Hy2ProxyManager
from fuckclassroom.plugins.process_runtime import ProcessPluginError, ProcessPluginHost
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION


class CourseSelectionProcessFacade:
    """Keep auth/session state in-process while moving API execution to a worker."""

    def __init__(
        self,
        local: CourseSelectionAssistant,
        host: ProcessPluginHost,
    ) -> None:
        self.local = local
        self.host = host
        self._policy = threading.local()
        self._catalog_lock = threading.Lock()
        self._catalog_cache_path = local.session_dir / "catalog_worker_cache.json"

    def __getattr__(self, name: str):
        return getattr(self.local, name)

    @contextmanager
    def defer_hy2_start(self):
        previous = bool(getattr(self._policy, "defer_hy2", False))
        self._policy.defer_hy2 = True
        try:
            with self.local.defer_hy2_start():
                yield
        finally:
            self._policy.defer_hy2 = previous

    def _call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        progress=None,
        timeout: float = 180.0,
    ):
        payload = dict(params or {})
        payload["_defer_hy2"] = bool(getattr(self._policy, "defer_hy2", False))
        try:
            return self.host.call_sync(
                method,
                payload,
                progress=progress,
                timeout=timeout,
            )
        except ProcessPluginError as exc:
            raise CourseSelectionApiError(str(exc)) from exc

    def invalidate_access_mode(self) -> None:
        self.local.invalidate_access_mode()
        try:
            self._call("selection.runtime.invalidate", {}, timeout=20)
        except CourseSelectionApiError:
            # Settings should remain saveable when the worker is already stopping.
            pass

    def clear_session(self) -> None:
        self.local.clear_session()
        try:
            self._call("selection.runtime.clear_session", {}, timeout=20)
        except CourseSelectionApiError:
            pass

    def refresh_selection_info_for_web(
        self,
        progress=None,
        timeout_ms: int = 300_000,
        result_url: str = "/selection/info",
        student_id: int | None = None,
        turn_id: int | None = None,
    ) -> str:
        return str(
            self._call(
                "selection.info.refresh",
                {
                    "timeout_ms": timeout_ms,
                    "result_url": result_url,
                    "student_id": student_id,
                    "turn_id": turn_id,
                },
                progress=progress,
                timeout=max(360.0, timeout_ms / 1000 + 60),
            )
            or result_url
        )

    def get_live_selection_counts(self, timeout_ms: int = 60_000) -> dict[str, Any]:
        result = self._call(
            "selection.counts",
            {"timeout_ms": timeout_ms},
            timeout=max(90.0, timeout_ms / 1000 + 30),
        )
        if not isinstance(result, dict):
            raise CourseSelectionApiError("选课 Worker 返回格式错误")
        return result

    def select_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress=None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info",
    ) -> str:
        return str(
            self._call(
                "selection.lesson.select",
                {
                    "lesson_id": lesson_id,
                    "timeout_ms": timeout_ms,
                    "result_url": result_url,
                },
                progress=progress,
                timeout=max(960.0, timeout_ms / 1000 + 60),
            )
            or result_url
        )

    def drop_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress=None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info/selected",
    ) -> str:
        return str(
            self._call(
                "selection.lesson.drop",
                {
                    "lesson_id": lesson_id,
                    "timeout_ms": timeout_ms,
                    "result_url": result_url,
                },
                progress=progress,
                timeout=max(960.0, timeout_ms / 1000 + 60),
            )
            or result_url
        )

    def query_whole_school_courses(
        self,
        filters,
        *,
        page: int = 1,
        page_size: int = 50,
        timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        result = self._call(
            "selection.catalog.query",
            {
                "filters": dict(filters),
                "page": page,
                "page_size": page_size,
                "timeout_ms": timeout_ms,
            },
            timeout=max(90.0, timeout_ms / 1000 + 30),
        )
        if not isinstance(result, dict):
            raise CourseSelectionApiError("选课 Worker 返回格式错误")
        courses = result.get("courses")
        if isinstance(courses, list):
            self._remember_catalog_courses(
                [item for item in courses if isinstance(item, dict)]
            )
        return result

    def query_course_catalog_teachers(
        self,
        term: str,
        *,
        timeout_ms: int = 30_000,
    ) -> list[dict[str, Any]]:
        result = self._call(
            "selection.catalog.teachers",
            {"term": term, "timeout_ms": timeout_ms},
            timeout=max(60.0, timeout_ms / 1000 + 30),
        )
        if not isinstance(result, list):
            raise CourseSelectionApiError("选课 Worker 返回格式错误")
        return [item for item in result if isinstance(item, dict)]

    def get_cached_catalog_course(self, lesson_id: int) -> dict[str, Any]:
        course_id = int(lesson_id)
        with self._catalog_lock:
            cache = self._load_catalog_cache()
            cached = cache.get(str(course_id))
            if isinstance(cached, dict):
                return dict(cached)
        try:
            result = self._call(
                "selection.catalog.cached",
                {"lesson_id": course_id},
                timeout=30,
            )
        except CourseSelectionApiError as exc:
            raise ValueError("缓存中找不到这门课，请先刷新课程") from exc
        if not isinstance(result, dict):
            raise ValueError("缓存中找不到这门课，请先刷新课程")
        self._remember_catalog_courses([result])
        return result

    def _load_catalog_cache(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._catalog_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = payload.get("courses") if isinstance(payload, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in rows.items()
            if isinstance(value, dict)
        }

    def _remember_catalog_courses(self, courses: list[dict[str, Any]]) -> None:
        with self._catalog_lock:
            cache = self._load_catalog_cache()
            for course in courses:
                try:
                    lesson_id = int(course.get("lesson_id"))
                except (TypeError, ValueError):
                    continue
                cache[str(lesson_id)] = dict(course)
            if len(cache) > 2_000:
                cache = dict(list(cache.items())[-1_500:])
            atomic_write_text(
                self._catalog_cache_path,
                json.dumps({"courses": cache}, ensure_ascii=False, indent=2),
            )

    def run_catalog_lesson_action_for_web(
        self,
        lesson_id: int,
        action: str,
        *,
        progress=None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/catalog",
    ) -> str:
        course = self.get_cached_catalog_course(lesson_id)
        self._call(
            "selection.catalog.remember",
            {"course": course},
            timeout=20,
        )
        return str(
            self._call(
                "selection.catalog.action",
                {
                    "lesson_id": lesson_id,
                    "action": action,
                    "timeout_ms": timeout_ms,
                    "result_url": result_url,
                },
                progress=progress,
                timeout=max(960.0, timeout_ms / 1000 + 60),
            )
            or result_url
        )

    def get_available_catalog_batch(self, timeout_ms: int = 60_000) -> dict[str, Any]:
        result = self._call(
            "selection.catalog.batch",
            {"timeout_ms": timeout_ms},
            timeout=max(90.0, timeout_ms / 1000 + 30),
        )
        if not isinstance(result, dict):
            raise CourseSelectionApiError("选课 Worker 返回格式错误")
        return result

    def select_lesson_for_batch_once(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict[str, Any],
        timeout_ms: int = 60_000,
    ) -> str:
        return str(
            self._call(
                "selection.batch.select_once",
                {
                    "lesson_id": lesson_id,
                    "batch": asdict(batch),
                    "course": course,
                    "timeout_ms": timeout_ms,
                },
                timeout=max(90.0, timeout_ms / 1000 + 30),
            )
            or ""
        )

    def get_lesson_capacity_for_batch(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict[str, Any],
        timeout_ms: int = 60_000,
    ) -> dict[str, int | None]:
        result = self._call(
            "selection.batch.capacity",
            {
                "lesson_id": lesson_id,
                "batch": asdict(batch),
                "course": course,
                "timeout_ms": timeout_ms,
            },
            timeout=max(90.0, timeout_ms / 1000 + 30),
        )
        if not isinstance(result, dict):
            raise CourseSelectionApiError("选课 Worker 返回格式错误")
        return {
            "selected_count": _optional_int(result.get("selected_count")),
            "max_count": _optional_int(result.get("max_count")),
        }


_AUTO_SELECTION_HANDOFF_LOCK = threading.Lock()
_AUTO_SELECTION_OWNER: "AutoSelectionProcessProxy | None" = None
_AUTO_SELECTION_PENDING: "AutoSelectionProcessProxy | None" = None


class AutoSelectionProcessProxy:
    def __init__(self, host: ProcessPluginHost) -> None:
        self.host = host

    def _call(self, method: str, params: dict[str, object] | None = None, *, timeout: float = 60.0):
        try:
            return self.host.call_sync(method, params or {}, timeout=timeout)
        except ProcessPluginError as exc:
            raise RuntimeError(str(exc)) from exc

    def configure_intervals(
        self,
        *,
        retry_seconds: float,
        session_retry_seconds: float,
        capacity_retry_seconds: float,
    ) -> None:
        self._call(
            "selection.auto.configure",
            {
                "retry_seconds": retry_seconds,
                "session_retry_seconds": session_retry_seconds,
                "capacity_retry_seconds": capacity_retry_seconds,
            },
        )

    def start(self) -> None:
        global _AUTO_SELECTION_OWNER, _AUTO_SELECTION_PENDING
        should_start = False
        with _AUTO_SELECTION_HANDOFF_LOCK:
            if _AUTO_SELECTION_OWNER is self:
                return
            if _AUTO_SELECTION_OWNER is None:
                _AUTO_SELECTION_OWNER = self
                if _AUTO_SELECTION_PENDING is self:
                    _AUTO_SELECTION_PENDING = None
                should_start = True
            else:
                _AUTO_SELECTION_PENDING = self
        if not should_start:
            return
        try:
            self._call("selection.auto.start")
        except Exception:
            with _AUTO_SELECTION_HANDOFF_LOCK:
                if _AUTO_SELECTION_OWNER is self:
                    _AUTO_SELECTION_OWNER = None
            raise

    def stop(self) -> None:
        global _AUTO_SELECTION_OWNER, _AUTO_SELECTION_PENDING
        next_owner = None
        should_stop = False
        with _AUTO_SELECTION_HANDOFF_LOCK:
            if _AUTO_SELECTION_PENDING is self:
                _AUTO_SELECTION_PENDING = None
            if _AUTO_SELECTION_OWNER is self:
                should_stop = True
            elif not should_stop:
                return

        if should_stop:
            try:
                self._call("selection.auto.stop", timeout=15)
            except RuntimeError:
                pass

        with _AUTO_SELECTION_HANDOFF_LOCK:
            if _AUTO_SELECTION_OWNER is self:
                _AUTO_SELECTION_OWNER = None
            if _AUTO_SELECTION_PENDING is not None:
                next_owner = _AUTO_SELECTION_PENDING
                _AUTO_SELECTION_PENDING = None

        if next_owner is not None:
            next_owner.start()

    def add_job(
        self,
        lesson_id: int,
        course: dict[str, Any],
        batch: dict[str, Any] | None,
    ) -> tuple[AutoSelectionJob, bool]:
        try:
            result = self._call(
                "selection.auto.add",
                {"lesson_id": lesson_id, "course": course, "batch": batch},
            )
        except RuntimeError as exc:
            raise ValueError(_clean_worker_error(str(exc))) from exc
        if not isinstance(result, dict) or not isinstance(result.get("job"), dict):
            raise ValueError("定时选课 Worker 返回格式错误")
        return AutoSelectionJob(**result["job"]), bool(result.get("created"))

    def list_jobs(self) -> list[dict[str, Any]]:
        result = self._call("selection.auto.list")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def active_lesson_ids(self) -> set[int]:
        result = self._call("selection.auto.active_lesson_ids")
        if not isinstance(result, list):
            return set()
        return {int(item) for item in result}

    def cancel(self, job_id: str) -> AutoSelectionJob:
        return self._job_mutation("selection.auto.cancel", job_id)

    def resume(self, job_id: str) -> AutoSelectionJob:
        return self._job_mutation("selection.auto.resume", job_id)

    def delete(self, job_id: str) -> None:
        result = self._call("selection.auto.delete", {"job_id": job_id})
        if isinstance(result, dict) and result.get("error") == "not_found":
            raise KeyError(job_id)
        if isinstance(result, dict) and result.get("error") == "active":
            raise ValueError(str(result.get("message") or "任务仍在运行"))

    def run_due_once(self) -> bool:
        return bool(self._call("selection.auto.run_due_once", timeout=180))

    def _job_mutation(self, method: str, job_id: str) -> AutoSelectionJob:
        result = self._call(method, {"job_id": job_id})
        if isinstance(result, dict) and result.get("error") == "not_found":
            raise KeyError(job_id)
        if not isinstance(result, dict) or not isinstance(result.get("job"), dict):
            raise RuntimeError("定时选课 Worker 返回格式错误")
        return AutoSelectionJob(**result["job"])


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _clean_worker_error(message: str) -> str:
    for prefix in ("ValueError: ", "CourseSelectionApiError: ", "RuntimeError: "):
        if message.startswith(prefix):
            return message[len(prefix):]
    return message


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    local = services.maybe("academic_session")
    hy2_proxy = services.maybe("hy2_proxy")
    if local is None:
        if hy2_proxy is None:
            hy2_proxy = Hy2ProxyManager(config)
            services.add("hy2_proxy", hy2_proxy)
        local = CourseSelectionAssistant(
            config,
            hy2_proxy=hy2_proxy,
            credential_store=services.get("credential_store"),
            verification_broker=services.get("verification_broker"),
            login_lock=services.get("login_lock"),
        )
        services.add("academic_session", local)
    elif hy2_proxy is None:
        hy2_proxy = getattr(local, "hy2_proxy", None) or Hy2ProxyManager(config)
        services.add("hy2_proxy", hy2_proxy)
    host = ProcessPluginHost(
        plugin_id="course-selection-worker",
        root=Path(__file__).resolve().parent,
        entry="worker.py",
        data_dir=Path(config.data_dir),
        rpc_registry=services.get("plugin_rpc"),
        rpc_api_version=PLUGIN_RPC_API_VERSION,
        rpc_permissions=(
            "internal.selection.session.ensure",
            "internal.selection.access.info",
            "internal.selection.hy2.ensure",
        ),
    )
    facade = CourseSelectionProcessFacade(local, host)
    auto_selection = AutoSelectionProcessProxy(host)

    services.add("course_selection_process_host", host)
    services.add("course_selection_local", local)
    services.add("course_selection", facade)
    services.add("auto_selection", auto_selection)


async def startup(context: PluginContext) -> None:
    await context.services.get("course_selection_process_host").start()


async def shutdown(context: PluginContext) -> None:
    await context.services.get("course_selection_process_host").stop()


__all__ = [
    "AutoSelectionProcessProxy",
    "CourseSelectionProcessFacade",
    "setup_services",
    "shutdown",
    "startup",
]
