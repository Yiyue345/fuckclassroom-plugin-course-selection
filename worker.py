from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fuckclassroom.core.config import AppConfig
from fuckclassroom.course_selection import (
    CourseSelectionAssistant,
    SelectionBatch,
    WebVpnSessionStatus,
)
from fuckclassroom.course_selection.automation import AutoSelectionService
from fuckclassroom.course_selection.proxy import Hy2ProxyError


class _RemoteHy2Proxy:
    def __init__(self, context) -> None:
        self.context = context

    def ensure_started(self) -> str:
        result = self.context.rpc.call("internal.selection.hy2.ensure")
        if not result:
            raise Hy2ProxyError("Hy2 主进程代理未返回地址")
        return str(result)


class _WorkerSelectionAssistant(CourseSelectionAssistant):
    def __init__(self, context) -> None:
        super().__init__(
            AppConfig(data_dir=context.data_dir),
            hy2_proxy=_RemoteHy2Proxy(context),
        )
        self._worker_context = context

    def verify_saved_session(
        self,
        timeout_ms: int = 15_000,
        *,
        auto_relogin: bool = True,
    ) -> WebVpnSessionStatus:
        payload = self._worker_context.rpc.call(
            "internal.selection.session.ensure",
            {
                "timeout_ms": timeout_ms,
                "auto_relogin": auto_relogin,
            },
        )
        if not isinstance(payload, dict):
            return WebVpnSessionStatus(False, "主进程没有返回教务会话状态")
        return WebVpnSessionStatus(**payload)

    def _detect_access_mode(self, force: bool = False) -> str:
        payload = self._worker_context.rpc.call(
            "internal.selection.access.info",
            {"force": force},
        )
        if not isinstance(payload, dict):
            return "webvpn"
        mode = str(payload.get("mode") or "webvpn")
        return mode if mode in {"campus", "public", "hy2", "webvpn"} else "webvpn"


_assistant: _WorkerSelectionAssistant | None = None
_auto: AutoSelectionService | None = None


def _state(context):
    global _assistant, _auto
    if _assistant is None:
        _assistant = _WorkerSelectionAssistant(context)
    if _auto is None:
        settings = AppConfig(data_dir=context.data_dir)._settings()
        _auto = AutoSelectionService(
            _assistant,
            Path(context.data_dir) / "webvpn" / "auto_selection_jobs.json",
            retry_seconds=settings.auto_selection_retry_seconds,
            session_retry_seconds=settings.auto_selection_session_retry_seconds,
            capacity_retry_seconds=settings.auto_selection_capacity_retry_seconds,
        )
    return _assistant, _auto


def _with_policy(assistant, params):
    return assistant.defer_hy2_start() if bool(params.get("_defer_hy2")) else _NullContext()


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def handle_call(method, params, context, progress):
    assistant, auto = _state(context)

    if method == "selection.runtime.invalidate":
        assistant.invalidate_access_mode()
        return {"ok": True}
    if method == "selection.runtime.clear_session":
        assistant.invalidate_access_mode()
        assistant._invalidate_selection_token()
        return {"ok": True}

    with _with_policy(assistant, params):
        if method == "selection.info.refresh":
            return assistant.refresh_selection_info_for_web(
                progress=progress,
                timeout_ms=int(params.get("timeout_ms") or 300_000),
                result_url=str(params.get("result_url") or "/selection/info"),
                student_id=_optional_int(params.get("student_id")),
                turn_id=_optional_int(params.get("turn_id")),
            )
        if method == "selection.counts":
            return assistant.get_live_selection_counts(
                timeout_ms=int(params.get("timeout_ms") or 60_000)
            )
        if method == "selection.lesson.select":
            return assistant.select_cached_lesson_for_web(
                int(params.get("lesson_id") or 0),
                progress=progress,
                timeout_ms=int(params.get("timeout_ms") or 900_000),
                result_url=str(params.get("result_url") or "/selection/info"),
            )
        if method == "selection.lesson.drop":
            return assistant.drop_cached_lesson_for_web(
                int(params.get("lesson_id") or 0),
                progress=progress,
                timeout_ms=int(params.get("timeout_ms") or 900_000),
                result_url=str(params.get("result_url") or "/selection/info/selected"),
            )
        if method == "selection.catalog.query":
            filters = params.get("filters")
            return assistant.query_whole_school_courses(
                filters if isinstance(filters, dict) else {},
                page=int(params.get("page") or 1),
                page_size=int(params.get("page_size") or 50),
                timeout_ms=int(params.get("timeout_ms") or 60_000),
            )
        if method == "selection.catalog.teachers":
            return assistant.query_course_catalog_teachers(
                str(params.get("term") or ""),
                timeout_ms=int(params.get("timeout_ms") or 30_000),
            )
        if method == "selection.catalog.cached":
            return assistant.get_cached_catalog_course(int(params.get("lesson_id") or 0))
        if method == "selection.catalog.remember":
            course = params.get("course")
            if not isinstance(course, dict):
                raise ValueError("缺少课程缓存")
            assistant._remember_catalog_courses([course])
            return {"ok": True}
        if method == "selection.catalog.action":
            return assistant.run_catalog_lesson_action_for_web(
                int(params.get("lesson_id") or 0),
                str(params.get("action") or ""),
                progress=progress,
                timeout_ms=int(params.get("timeout_ms") or 900_000),
                result_url=str(params.get("result_url") or "/selection/catalog"),
            )
        if method == "selection.catalog.batch":
            return assistant.get_available_catalog_batch(
                timeout_ms=int(params.get("timeout_ms") or 60_000)
            )
        if method == "selection.batch.select_once":
            raw_batch = params.get("batch")
            batch = SelectionBatch(**raw_batch) if isinstance(raw_batch, dict) else None
            if batch is None:
                raise ValueError("缺少选课批次")
            course = params.get("course")
            return assistant.select_lesson_for_batch_once(
                int(params.get("lesson_id") or 0),
                batch,
                course if isinstance(course, dict) else {},
                timeout_ms=int(params.get("timeout_ms") or 60_000),
            )
        if method == "selection.batch.capacity":
            raw_batch = params.get("batch")
            batch = SelectionBatch(**raw_batch) if isinstance(raw_batch, dict) else None
            if batch is None:
                raise ValueError("缺少选课批次")
            course = params.get("course")
            return assistant.get_lesson_capacity_for_batch(
                int(params.get("lesson_id") or 0),
                batch,
                course if isinstance(course, dict) else {},
                timeout_ms=int(params.get("timeout_ms") or 60_000),
            )

    if method == "selection.auto.configure":
        auto.configure_intervals(
            retry_seconds=float(params.get("retry_seconds") or 5),
            session_retry_seconds=float(params.get("session_retry_seconds") or 30),
            capacity_retry_seconds=float(params.get("capacity_retry_seconds") or 30),
        )
        return {"ok": True}
    if method == "selection.auto.start":
        auto.start()
        return {"ok": True}
    if method == "selection.auto.stop":
        auto.stop()
        return {"ok": True}
    if method == "selection.auto.add":
        course = params.get("course")
        batch = params.get("batch")
        job, created = auto.add_job(
            int(params.get("lesson_id") or 0),
            course if isinstance(course, dict) else {},
            batch if isinstance(batch, dict) else None,
        )
        return {"job": asdict(job), "created": created}
    if method == "selection.auto.list":
        return auto.list_jobs()
    if method == "selection.auto.active_lesson_ids":
        return sorted(auto.active_lesson_ids())
    if method in {"selection.auto.cancel", "selection.auto.resume"}:
        job_id = str(params.get("job_id") or "")
        try:
            job = auto.cancel(job_id) if method.endswith("cancel") else auto.resume(job_id)
        except KeyError:
            return {"error": "not_found"}
        return {"job": asdict(job)}
    if method == "selection.auto.delete":
        job_id = str(params.get("job_id") or "")
        try:
            auto.delete(job_id)
        except KeyError:
            return {"error": "not_found"}
        except ValueError as exc:
            return {"error": "active", "message": str(exc)}
        return {"ok": True}
    if method == "selection.auto.run_due_once":
        return auto.run_due_once()

    raise ValueError(f"未知 Course Selection Worker 方法：{method}")


def shutdown(context):
    global _auto
    if _auto is not None:
        _auto.stop()


def _optional_int(value):
    if value is None or value == "":
        return None
    return int(value)
