from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from fuckclassroom.auth.cas import (
    LoginVerificationBroker,
    is_sms_verification_page,
    login_interaction_reason,
    run_sms_verification,
    submit_saved_credentials,
)
from fuckclassroom.auth.credentials import CredentialStoreError, SelectionCredentialStore, SelectionCredentials
from fuckclassroom.core.atomic import atomic_write_text
from fuckclassroom.core.config import AppConfig
from .proxy import Hy2ProxyError, Hy2ProxyManager
from .helpers import *
from .models import *


_SELECTION_AUTH_FAILURE_MARKERS = (
    "401",
    "403",
    "重新登录",
    "登录页面",
    "登录页",
    "会话已失效",
    "会话无效",
    "token",
)


class SelectionApiMixin:
    def _run_with_session_recovery(
        self,
        operation: Callable[[], Any],
        *,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 60_000,
    ) -> Any:
        try:
            return operation()
        except (CourseSelectionApiError, RuntimeError) as exc:
            if not any(marker in str(exc) for marker in _SELECTION_AUTH_FAILURE_MARKERS):
                raise
            _report(progress, 18, "教务会话已失效，正在使用保存的账号自动登录")
            self._invalidate_selection_token()
            status = self.verify_saved_session(
                timeout_ms=min(timeout_ms, 15_000),
                auto_relogin=True,
            )
            if status.is_valid is not True:
                message = status.message or str(exc)
                raise type(exc)(message) from exc
            _report(progress, 24, "登录状态已恢复，正在重试刚才的操作")
            return operation()

    def list_captures(self) -> list[CaptureFile]:
        if not self.capture_dir.exists():
            return []
        result: list[CaptureFile] = []
        for json_path in sorted(self.capture_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                request_count = len(payload.get("requests", []))
            except (OSError, json.JSONDecodeError):
                request_count = 0
            markdown_path = json_path.with_suffix(".md")
            stat = json_path.stat()
            result.append(
                CaptureFile(
                    name=json_path.stem,
                    json_path=json_path,
                    markdown_path=markdown_path if markdown_path.exists() else None,
                    updated_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    request_count=request_count,
                )
            )
        return result

    def read_capture_markdown(self, name: str) -> str:
        path = self._capture_path(name, ".md")
        if not path.exists():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8")

    def get_selection_info(self, view: str = "major-plan") -> SelectionInfoCache:
        payload = self._read_json(self.selection_info_path, default={})
        if not isinstance(payload, dict):
            payload = {}
        sections = payload.get("sections", {})
        if not isinstance(sections, dict):
            sections = {}
        if "major-plan" not in sections and isinstance(payload.get("courses"), list):
            sections["major-plan"] = payload["courses"]
        raw_batches = payload.get("batches")
        batches: list[dict[str, Any]] = []
        if isinstance(raw_batches, list):
            for item in raw_batches:
                if not isinstance(item, dict):
                    continue
                batch = dict(item)
                batch["is_open"] = _selection_batch_is_open(
                    batch.get("is_open"),
                    batch.get("start_at"),
                    batch.get("end_at"),
                )
                batches.append(batch)
        current_turn_id = _safe_int(payload.get("turn_id"))
        current_student_id = _safe_int(payload.get("student_id"))
        current_batch = next(
            (
                item
                for item in batches
                if _safe_int(item.get("turn_id")) == current_turn_id
                and _safe_int(item.get("student_id")) == current_student_id
            ),
            None,
        )
        active_view = view if view in SELECTION_INFO_VIEWS else "major-plan"
        courses = sections.get(active_view, [])
        return SelectionInfoCache(
            updated_at=payload.get("updated_at"),
            title=str(payload.get("title") or ""),
            turn_id=current_turn_id,
            student_id=current_student_id,
            courses=courses if isinstance(courses, list) else [],
            sections={key: value for key, value in sections.items() if isinstance(value, list)},
            active_view=active_view,
            batches=batches,
            current_batch=current_batch,
        )

    def get_selection_action_result(self) -> dict[str, Any] | None:
        payload = self._read_json(self.selection_action_path, default=None)
        return payload if isinstance(payload, dict) else None

    def pop_selection_action_result(self) -> dict[str, Any] | None:
        with self._selection_action_lock:
            payload = self.get_selection_action_result()
            if payload is None:
                return None
            try:
                self.selection_action_path.unlink(missing_ok=True)
            except OSError:
                return payload
            return payload

    def _fetch_selection_info_via_api(
        self,
        progress: ProgressCallback | None,
        timeout_seconds: float,
        student_id: int | None = None,
        turn_id: int | None = None,
    ) -> tuple[dict[str, Any], SelectionBatch]:
        opener, jar = self._selection_http_client()
        _report(progress, 12, "正在通过接口获取选课批次")
        batches, token = self._fetch_selection_batches(opener, timeout_seconds)
        if not batches:
            raise CourseSelectionApiError("选课入口没有返回可用批次，请确认当前是否开放选课")
        if (student_id is None) != (turn_id is None):
            raise CourseSelectionApiError("选课批次参数不完整，请重新选择")
        if student_id is not None and turn_id is not None:
            requested_batch = next(
                (
                    batch
                    for batch in batches
                    if batch.student_id == student_id and batch.turn_id == turn_id
                ),
                None,
            )
            if requested_batch is None:
                raise CourseSelectionApiError("所选批次当前不可用，请重新刷新批次列表")
            candidate_batches = [requested_batch]
        else:
            candidate_batches = batches

        errors: list[str] = []
        for index, batch in enumerate(candidate_batches):
            percent = 25 + min(20, int(index * 20 / max(1, len(candidate_batches))))
            _report(progress, percent, f"正在读取批次：{batch.label}")
            try:
                payload = self._fetch_selection_batch_payload(opener, token, batch, timeout_seconds)
            except CourseSelectionApiError as exc:
                errors.append(str(exc))
                continue
            payload["title"] = batch.label
            payload["batches"] = [asdict(item) for item in batches]
            self._persist_selection_cookies(jar)
            return payload, batch

        detail = errors[-1] if errors else "没有批次返回课程数据"
        raise CourseSelectionApiError(f"选课批次读取失败：{detail}")

    def _selection_http_client(self) -> tuple[Any, CookieJar]:
        access = self._selection_access()
        if not self.storage_state_path.exists():
            raise CourseSelectionApiError("尚未保存教务会话，请先登录")
        state = self._read_json(self.storage_state_path, default={})
        saved_mode = self._saved_access_mode()
        if saved_mode != access.mode:
            saved_label = _selection_access(saved_mode).label
            raise CourseSelectionApiError(
                f"当前为{access.label}，保存的是{saved_label}会话，请重新登录"
            )
        cookies = state.get("cookies", []) if isinstance(state, dict) else []
        if not isinstance(cookies, list) or not cookies:
            raise CourseSelectionApiError("教务会话中没有 Cookie，请重新登录")

        jar = CookieJar()
        for item in cookies:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            domain = str(item.get("domain") or WEBVPN_HOST)
            expires = item.get("expires")
            expires_at = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=str(item["name"]),
                    value=str(item.get("value") or ""),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(domain),
                    domain_initial_dot=domain.startswith("."),
                    path=str(item.get("path") or "/"),
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=expires_at,
                    discard=expires_at is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        handlers: list[Any] = [HTTPCookieProcessor(jar)]
        if access.mode == "hy2":
            try:
                proxy_url = self.hy2_proxy.ensure_started()
            except Hy2ProxyError as exc:
                raise CourseSelectionApiError(str(exc)) from exc
            handlers.insert(0, ProxyHandler({"http": proxy_url, "https": proxy_url}))
        return build_opener(*handlers), jar

    def _fetch_selection_batches(
        self,
        opener: Any,
        timeout_seconds: float,
    ) -> tuple[list[SelectionBatch], str]:
        token = self._fetch_selection_token(opener, timeout_seconds)
        students_payload = self._request_selection_json(
            opener,
            token,
            "/students",
            timeout_seconds=timeout_seconds,
        )
        student_ids = _extract_selection_student_ids(students_payload)
        if not student_ids:
            raise CourseSelectionApiError("选课接口没有返回可用的学生记录")

        batches: list[SelectionBatch] = []
        for student_id in student_ids:
            turns_payload = self._request_selection_json(
                opener,
                token,
                f"/open-turns/{student_id}",
                timeout_seconds=timeout_seconds,
            )
            batches.extend(_selection_batches_from_api(turns_payload, student_id))
        return _sort_selection_batches(batches), token

    def _fetch_selection_token(self, opener: Any, timeout_seconds: float) -> str:
        access = self._selection_access()
        with self._selection_token_lock:
            now = time.time()
            if (
                self._selection_token
                and self._selection_token_mode == access.mode
                and self._selection_token_expires_at > now + 30
            ):
                return self._selection_token

            content, _, _ = self._request_selection_bytes(
                opener,
                access.course_select_url,
                timeout_seconds=timeout_seconds,
                accept="text/html,application/xhtml+xml,application/json",
            )
            text = content.decode("utf-8", errors="replace")
            token = _extract_selection_bootstrap_token(text)
            if not token:
                raise CourseSelectionApiError("教务入口没有下发选课授权，请重新登录")
            self._selection_token = token
            self._selection_token_mode = access.mode
            self._selection_token_expires_at = _selection_token_expiry(token, now)
            return token

    def _invalidate_selection_token(self, token: str | None = None) -> None:
        with self._selection_token_lock:
            if token is not None and token != self._selection_token:
                return
            self._selection_token = ""
            self._selection_token_expires_at = 0.0
            self._selection_token_mode = ""

    def _fetch_selection_batch_payload(
        self,
        opener: Any,
        token: str,
        batch: SelectionBatch,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        turn_id = batch.turn_id
        student_id = batch.student_id
        query_condition = self._request_selection_json(
            opener, token, f"/query-condition/{turn_id}", timeout_seconds=timeout_seconds
        )
        semester_id = _extract_semester_id(query_condition)
        selected_lessons = self._request_selection_json(
            opener, token, f"/selected-lessons/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        major_plan = self._request_selection_json(
            opener, token, f"/major-plan/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        repaired_courses = self._request_selection_json(
            opener, token, f"/repaired-courses/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        request_body = _selection_query_body(turn_id, student_id, semester_id)
        query_lessons = self._request_selection_json(
            opener,
            token,
            f"/query-lesson/{student_id}/{turn_id}",
            method="POST",
            body=request_body,
            timeout_seconds=timeout_seconds,
        )
        lesson_ids = sorted(
            {
                lesson_id
                for lesson in _extract_lessons(query_lessons)
                if (lesson_id := _safe_int(lesson.get("id"))) is not None
            }
        )
        std_count: Any = {"data": {}}
        if lesson_ids:
            query = urlencode({"lessonIds": ",".join(str(item) for item in lesson_ids)})
            std_count = self._request_selection_json(
                opener,
                token,
                f"/std-count?{query}",
                timeout_seconds=timeout_seconds,
            )
        return {
            "turnId": turn_id,
            "studentId": student_id,
            "selectedLessons": selected_lessons,
            "queryCondition": query_condition,
            "majorPlan": major_plan,
            "repairedCourses": repaired_courses,
            "queryLessons": query_lessons,
            "stdCount": std_count,
        }

    def _request_selection_json(
        self,
        opener: Any,
        token: str,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout_seconds: float,
    ) -> Any:
        access = self._selection_access()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": token,
            "Referer": access.course_select_url,
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            content, content_type, _ = self._request_selection_bytes(
                opener,
                f"{access.api_base_url}{path}",
                timeout_seconds=timeout_seconds,
                accept=headers["Accept"],
                method=method,
                data=data,
                extra_headers=headers,
            )
        except CourseSelectionApiError as exc:
            if any(marker in str(exc) for marker in ("401", "403", "重新登录")):
                self._invalidate_selection_token(token)
            raise
        if "json" not in content_type.lower() and content.lstrip().startswith(b"<"):
            self._invalidate_selection_token(token)
            raise CourseSelectionApiError("选课接口返回了登录页面，请重新登录")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CourseSelectionApiError("选课接口返回的数据不是有效 JSON") from exc
        if isinstance(payload, dict):
            status = _safe_int(payload.get("status") or payload.get("code"))
            if status in (401, 403):
                self._invalidate_selection_token(token)
                raise CourseSelectionApiError("选课 token 已失效，请重新登录")
        return payload

    @staticmethod
    def _request_selection_bytes(
        opener: Any,
        url: str,
        *,
        timeout_seconds: float,
        accept: str,
        method: str = "GET",
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        }
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                content = response.read()
                content_type = str(response.headers.get("content-type") or "")
                final_url = str(response.geturl())
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise CourseSelectionApiError(f"教务接口返回 {exc.code}，请重新登录 WebVPN") from exc
            raise CourseSelectionApiError(f"教务接口请求失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise CourseSelectionApiError(f"无法访问 WebVPN 教务接口：{exc.reason}") from exc

        preview = content[:5000].decode("utf-8", errors="ignore").lower()
        combined = f"{final_url} {preview}"
        if any(marker in combined for marker in ("authserver/login", "统一身份认证", "cas/login")):
            raise CourseSelectionApiError("WebVPN/教务会话已失效，请重新登录")
        if any(marker in combined for marker in ("wengine-auth-failed", "access forbidden", "访问出错 - 403")):
            raise CourseSelectionApiError("WebVPN 网关拒绝访问，请重新登录或稍后再试")
        return content, content_type, final_url

    def _persist_selection_cookies(self, jar: CookieJar) -> None:
        state = self._read_json(self.storage_state_path, default={})
        if not isinstance(state, dict):
            return
        rows = state.get("cookies", [])
        if not isinstance(rows, list):
            rows = []
        existing = {
            (str(item.get("name") or ""), str(item.get("domain") or ""), str(item.get("path") or "/")): item
            for item in rows
            if isinstance(item, dict)
        }
        for cookie in jar:
            key = (cookie.name, cookie.domain, cookie.path)
            item = existing.get(key)
            if item is None:
                item = {
                    "name": cookie.name,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
                rows.append(item)
                existing[key] = item
            item.update(
                {
                    "value": cookie.value,
                    "expires": cookie.expires if cookie.expires is not None else -1,
                    "secure": cookie.secure,
                }
            )
        state["cookies"] = rows
        try:
            atomic_write_text(
                self.storage_state_path,
                json.dumps(state, ensure_ascii=False, indent=2),
            )
        except OSError:
            pass

    def refresh_selection_info_for_web(
        self,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 300_000,
        result_url: str = "/selection/info",
        student_id: int | None = None,
        turn_id: int | None = None,
    ) -> str:
        _report(progress, 5, "正在读取 WebVPN 会话")
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        try:
            raw_info, batch = self._run_with_session_recovery(
                lambda: self._fetch_selection_info_via_api(
                    progress,
                    timeout_seconds,
                    student_id=student_id,
                    turn_id=turn_id,
                ),
                progress=progress,
                timeout_ms=timeout_ms,
            )
        except CourseSelectionApiError as exc:
            message = str(exc)
            access = self._selection_access()
            if any(marker in message for marker in _SELECTION_AUTH_FAILURE_MARKERS):
                self._write_session_check(_session_check(False, message, access.course_select_url))
            elif message == "选课接口没有返回可用的学生记录":
                self._write_session_check(
                    _session_check(
                        True,
                        f"{access.label}教务会话有效，当前没有可用选课学生记录",
                        access.course_select_url,
                    )
                )
            raise RuntimeError(message) from exc

        _report(progress, 70, "正在整理教师、时间和容量信息")
        cache_payload = self._normalize_selection_info(raw_info)
        cache_payload["batches"] = raw_info.get("batches", [])
        self._write_selection_info(cache_payload)
        self._write_session_meta(batch.url)
        self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
        total = sum(len(value) for value in cache_payload.get("sections", {}).values())
        _report(progress, 100, f"已通过接口缓存 {total} 条选课信息")
        return result_url

    def get_live_selection_counts(self, timeout_ms: int = 60_000) -> dict[str, Any]:
        return self._run_with_session_recovery(
            lambda: self._get_live_selection_counts_once(timeout_ms),
            timeout_ms=timeout_ms,
        )

    def _get_live_selection_counts_once(
        self,
        timeout_ms: int,
    ) -> dict[str, Any]:
        info = self.get_selection_info("all")
        course_by_id: dict[int, dict[str, Any]] = {}
        for courses in info.sections.values():
            for course in courses:
                if not isinstance(course, dict):
                    continue
                lesson_id = _safe_int(course.get("lesson_id"))
                if lesson_id is not None and lesson_id not in course_by_id:
                    course_by_id[lesson_id] = course
        if not course_by_id:
            return {"updated_at": _local_now(), "courses": {}}

        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        opener, jar = self._selection_http_client()
        batch = self._batch_from_cached_info(info)
        if batch is not None:
            token = self._fetch_selection_token(opener, timeout_seconds)
        else:
            batches, token = self._fetch_selection_batches(opener, timeout_seconds)
            batch = self._match_cached_batch(batches, info)
            if batch is None:
                raise CourseSelectionApiError("当前缓存对应的选课批次已不可用，请刷新课程")

        counts: dict[int, int] = {}
        lesson_ids = sorted(course_by_id)
        for offset in range(0, len(lesson_ids), 100):
            chunk = lesson_ids[offset : offset + 100]
            query = urlencode({"lessonIds": ",".join(str(item) for item in chunk)})
            payload = self._request_selection_json(
                opener,
                token,
                f"/std-count?{query}",
                timeout_seconds=timeout_seconds,
            )
            counts.update(_extract_count_map(payload))

        self._persist_selection_cookies(jar)
        self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
        return {
            "updated_at": _local_now(),
            "courses": {
                str(lesson_id): {
                    "selected_count": counts.get(lesson_id),
                    "max_count": _safe_int(course_by_id[lesson_id].get("max_count")),
                }
                for lesson_id in lesson_ids
            },
        }

    def select_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info",
    ) -> str:
        with self._selection_action_lock:
            return self._run_with_session_recovery(
                lambda: self._run_cached_lesson_action(
                    lesson_id=lesson_id,
                    action="select",
                    progress=progress,
                    timeout_ms=timeout_ms,
                    result_url=result_url,
                ),
                progress=progress,
                timeout_ms=timeout_ms,
            )

    def drop_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info/selected",
    ) -> str:
        with self._selection_action_lock:
            return self._run_with_session_recovery(
                lambda: self._run_cached_lesson_action(
                    lesson_id=lesson_id,
                    action="drop",
                    progress=progress,
                    timeout_ms=timeout_ms,
                    result_url=result_url,
                ),
                progress=progress,
                timeout_ms=timeout_ms,
            )

    def select_lesson_for_batch_once(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict[str, Any],
        timeout_ms: int = 60_000,
    ) -> str:
        with self._selection_action_lock:
            return self._run_cached_lesson_action(
                lesson_id=lesson_id,
                action="select",
                progress=None,
                timeout_ms=timeout_ms,
                result_url="/selection/auto",
                course_override=course,
                batch_override=batch,
                record_result=False,
            )

    def get_lesson_capacity_for_batch(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict[str, Any],
        timeout_ms: int = 60_000,
    ) -> dict[str, int | None]:
        with self._selection_action_lock:
            timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
            opener, jar = self._selection_http_client()
            token = self._fetch_selection_token(opener, timeout_seconds)
            payload = self._request_selection_json(
                opener,
                token,
                f"/std-count?{urlencode({'lessonIds': str(lesson_id)})}",
                timeout_seconds=timeout_seconds,
            )
            selected_count = _extract_count_map(payload).get(lesson_id)
            max_count = _safe_int(course.get("max_count"))
            self._persist_selection_cookies(jar)
            self._write_session_meta(batch.url)
            self._write_session_check(
                _session_check(True, "WebVPN/选课接口会话有效", batch.url)
            )
            return {
                "selected_count": selected_count,
                "max_count": max_count,
            }

    def _run_cached_lesson_action(
        self,
        lesson_id: int,
        action: str,
        progress: ProgressCallback | None,
        timeout_ms: int,
        result_url: str,
        course_override: dict[str, Any] | None = None,
        batch_override: SelectionBatch | None = None,
        record_result: bool = True,
    ) -> str:
        course = dict(course_override) if course_override is not None else self._find_cached_course(lesson_id)
        if course is None:
            raise ValueError("缓存中找不到这门课，请先刷新选课信息")
        if action == "select" and course.get("is_selected"):
            raise ValueError("这门课已经在已选课程中")
        if action not in {"select", "drop"}:
            raise ValueError("不支持的课程操作")

        action_label = "退课" if action == "drop" else "选课"
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        responses: list[dict[str, Any]] = []
        _report(progress, 5, "正在读取 WebVPN 会话")
        try:
            opener, jar = self._selection_http_client()
            _report(progress, 12, "正在获取选课授权")
            if batch_override is not None:
                batch = batch_override
                token = self._fetch_selection_token(opener, timeout_seconds)
            else:
                info = self.get_selection_info("all")
                batch = self._batch_from_cached_info(info)
                if batch is not None:
                    token = self._fetch_selection_token(opener, timeout_seconds)
                else:
                    batches, token = self._fetch_selection_batches(opener, timeout_seconds)
                    batch = self._match_cached_batch(batches, info)
                    if batch is None:
                        raise CourseSelectionApiError("当前缓存对应的选课批次已不可用，请刷新课程")

            _report(progress, 22, "正在读取目标课程状态")
            lesson = self._fetch_selection_action_lesson(
                opener, token, batch, lesson_id, action, timeout_seconds,
                max_count_hint=_safe_int(course.get("max_count")),
            )

            _report(progress, 35, f"正在校验{action_label}条件")
            predicate_path, predicate_body = self._selection_predicate_request(action, lesson, batch)
            predicate_start = self._record_selection_request(
                opener, token, predicate_path, responses, method="POST", body=predicate_body,
                timeout_seconds=timeout_seconds,
            )
            predicate_id = _selection_request_id(predicate_start, f"{action_label}条件校验")
            predicate_response = self._poll_selection_result(
                opener, token, f"/predicate-response/{batch.student_id}/{predicate_id}", responses,
                timeout_seconds, progress, 42, 58, f"正在等待{action_label}条件校验",
            )
            predicate_message = _selection_predicate_message(predicate_response, lesson_id)
            if predicate_message:
                raise CourseSelectionApiError(f"平台要求额外确认：{predicate_message}")

            _report(progress, 62, f"正在提交{action_label}请求")
            action_path, action_body = self._selection_submit_request(action, lesson, batch)
            action_start = self._record_selection_request(
                opener, token, action_path, responses, method="POST", body=action_body,
                timeout_seconds=timeout_seconds,
            )
            action_id = _selection_request_id(action_start, action_label)
            final_response = self._poll_selection_result(
                opener, token, f"/add-drop-response/{batch.student_id}/{action_id}", responses,
                timeout_seconds, progress, 68, 82, f"正在等待平台返回{action_label}结果",
            )
            final_data = _selection_response_data(final_response)
            if final_data.get("success") is not True:
                raise CourseSelectionApiError(_selection_response_message(final_response) or f"平台未确认{action_label}成功")

            message = _selection_response_message(final_response) or f"{action_label}成功"
            _report(progress, 88, f"{action_label}成功，正在更新本地状态")
            try:
                self._update_cached_lesson_selection(lesson_id, action == "select")
            except OSError as exc:
                message = f"{message}，但本地课程状态更新失败：{exc}"
            self._persist_selection_cookies(jar)
            self._write_session_meta(batch.url)
            self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
            if record_result:
                self._write_selection_action_result(
                    self._build_action_result(action, course, "接口提交", responses, status="成功", fallback_message=message)
                )
            _report(progress, 100, f"{action_label}完成")
            return result_url
        except CourseSelectionApiError as exc:
            message = str(exc)
            if record_result:
                self._write_selection_action_result(
                    self._build_action_result(action, course, None, responses, status="失败", fallback_message=message)
                )
            if any(marker in message for marker in ("重新登录", "会话", "token", "授权")):
                self._write_session_check(_session_check(False, message, JW_WEBVPN_COURSE_SELECT_URL))
            raise RuntimeError(message) from exc

    @staticmethod
    def _batch_from_cached_info(info: SelectionInfoCache) -> SelectionBatch | None:
        if info.student_id is None or info.turn_id is None:
            return None
        return SelectionBatch(
            student_id=info.student_id,
            turn_id=info.turn_id,
            label=info.title or "当前选课批次",
            url=_selection_batch_url(info.student_id, info.turn_id),
            is_open=True,
        )

    @staticmethod
    def _match_cached_batch(batches: list[SelectionBatch], info: SelectionInfoCache) -> SelectionBatch | None:
        return next(
            (batch for batch in batches if batch.turn_id == info.turn_id and batch.student_id == info.student_id),
            batches[0] if batches and info.turn_id is None else None,
        )

    def _fetch_selection_action_lesson(
        self,
        opener: Any,
        token: str,
        batch: SelectionBatch,
        lesson_id: int,
        action: str,
        timeout_seconds: float,
        max_count_hint: int | None = None,
    ) -> dict[str, Any]:
        if action == "drop":
            source = self._request_selection_json(
                opener,
                token,
                f"/selected-lessons/{batch.turn_id}/{batch.student_id}",
                timeout_seconds=timeout_seconds,
            )
        else:
            selected_source = self._request_selection_json(
                opener,
                token,
                f"/selected-lessons/{batch.turn_id}/{batch.student_id}",
                timeout_seconds=timeout_seconds,
            )
            if any(
                _safe_int(item.get("id")) == lesson_id
                for item in _extract_lessons(selected_source)
            ):
                raise CourseSelectionApiError("课程已经选中")
            query_condition = self._request_selection_json(
                opener,
                token,
                f"/query-condition/{batch.turn_id}",
                timeout_seconds=timeout_seconds,
            )
            source = self._request_selection_json(
                opener,
                token,
                f"/query-lesson/{batch.student_id}/{batch.turn_id}",
                method="POST",
                body=_selection_query_body(
                    batch.turn_id,
                    batch.student_id,
                    _extract_semester_id(query_condition),
                ),
                timeout_seconds=timeout_seconds,
            )
        lesson = next(
            (item for item in _extract_lessons(source) if _safe_int(item.get("id")) == lesson_id),
            None,
        )
        if lesson is None:
            message = "课程不在当前已选列表中" if action == "drop" else "课程当前不可选或已被选中"
            raise CourseSelectionApiError(message)
        if action == "select":
            count_payload = self._request_selection_json(
                opener,
                token,
                f"/std-count?{urlencode({'lessonIds': str(lesson_id)})}",
                timeout_seconds=timeout_seconds,
            )
            selected_count = _extract_count_map(count_payload).get(lesson_id)
            max_count = (
                _safe_int(lesson.get("limitCount"))
                or _schedule_limit_count(lesson)
                or max_count_hint
            )
            if max_count and selected_count is not None and selected_count >= max_count:
                raise CourseSelectionApiError(f"课程容量已满（{selected_count}/{max_count}）")
        return lesson

    def _update_cached_lesson_selection(self, lesson_id: int, is_selected: bool) -> None:
        payload = self._read_json(self.selection_info_path, default={})
        if not isinstance(payload, dict):
            return
        sections = payload.get("sections")
        if not isinstance(sections, dict):
            return

        selected_course: dict[str, Any] | None = None
        for courses in sections.values():
            if not isinstance(courses, list):
                continue
            for course in courses:
                if not isinstance(course, dict) or _safe_int(course.get("lesson_id")) != lesson_id:
                    continue
                course["is_selected"] = is_selected
                if selected_course is None:
                    selected_course = dict(course)

        selected_courses = sections.get("selected")
        selected_courses = selected_courses if isinstance(selected_courses, list) else []
        selected_courses = [
            course
            for course in selected_courses
            if not isinstance(course, dict) or _safe_int(course.get("lesson_id")) != lesson_id
        ]
        if is_selected and selected_course is not None:
            selected_course["is_selected"] = True
            selected_courses.append(selected_course)
            selected_courses.sort(
                key=lambda item: (
                    str(item.get("course_name") or ""),
                    str(item.get("lesson_code") or ""),
                    str(item.get("teachers") or ""),
                )
            )
        sections["selected"] = selected_courses
        if isinstance(sections.get("major-plan"), list):
            payload["courses"] = sections["major-plan"]
        payload["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._write_selection_info(payload)

    @staticmethod
    def _selection_predicate_request(
        action: str, lesson: dict[str, Any], batch: SelectionBatch,
    ) -> tuple[str, dict[str, Any]]:
        lesson_id = _safe_int(lesson.get("id"))
        if lesson_id is None:
            raise CourseSelectionApiError("课程缺少教学班编号")
        if action == "drop":
            return "/drop-predicate", {
                "studentAssoc": batch.student_id,
                "courseSelectTurnAssoc": batch.turn_id,
                "lessonAssocSet": [lesson_id],
            }
        return "/add-predicate", {
            "studentAssoc": batch.student_id,
            "courseSelectTurnAssoc": batch.turn_id,
            "requestMiddleDtos": [_selection_request_middle(lesson)],
            "coursePackAssoc": lesson.get("coursePackAssoc"),
        }

    @staticmethod
    def _selection_submit_request(
        action: str, lesson: dict[str, Any], batch: SelectionBatch,
    ) -> tuple[str, dict[str, Any]]:
        lesson_id = _safe_int(lesson.get("id"))
        if lesson_id is None:
            raise CourseSelectionApiError("课程缺少教学班编号")
        if action == "drop":
            return "/drop-request", {
                "studentAssoc": batch.student_id,
                "courseSelectTurnAssoc": batch.turn_id,
                "lessonAssocs": [lesson_id],
                "coursePackAssoc": lesson.get("coursePackAssoc"),
                "confirmMidtermRetake": False,
            }
        return "/add-request", {
            "studentAssoc": batch.student_id,
            "courseSelectTurnAssoc": batch.turn_id,
            "requestMiddleDtos": [_selection_request_middle(lesson)],
            "coursePackAssoc": lesson.get("coursePackAssoc"),
        }

    def _record_selection_request(
        self, opener: Any, token: str, path: str, responses: list[dict[str, Any]], *,
        timeout_seconds: float, method: str = "GET", body: dict[str, Any] | None = None,
    ) -> Any:
        payload = self._request_selection_json(
            opener, token, path, method=method, body=body, timeout_seconds=timeout_seconds,
        )
        responses.append({
            "method": method,
            "url": f"{COURSE_SELECTION_API_BASE_URL}{path.split('?', 1)[0]}",
            "status": 200,
            "body": payload,
        })
        return payload

    def _poll_selection_result(
        self, opener: Any, token: str, path: str, responses: list[dict[str, Any]],
        timeout_seconds: float, progress: ProgressCallback | None, progress_start: int,
        progress_end: int, message: str,
    ) -> Any:
        retry_delays = (0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0)
        for attempt in range(len(retry_delays) + 1):
            payload = self._record_selection_request(
                opener, token, path, responses, timeout_seconds=timeout_seconds,
            )
            if "success" in _selection_response_data(payload):
                return payload
            percent = progress_start + int(
                (attempt + 1) * (progress_end - progress_start) / (len(retry_delays) + 1)
            )
            _report(progress, percent, message)
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
        raise CourseSelectionApiError(f"{message}超时，请稍后刷新确认最终状态")

    def _find_cached_course(self, lesson_id: int) -> dict[str, Any] | None:
        info = self.get_selection_info()
        for courses in info.sections.values():
            for course in courses:
                if isinstance(course, dict) and _safe_int(course.get("lesson_id")) == lesson_id:
                    return course
        return None

    @staticmethod
    def _filter_candidates_by_cached_course(
        candidates: list[SelectionCandidate],
        course: dict[str, Any],
    ) -> list[SelectionCandidate]:
        required = [
            str(course.get("course_name") or "").strip(),
            str(course.get("lesson_code") or "").strip(),
        ]
        optional_teacher = _first_teacher_name(str(course.get("teachers") or ""))
        if optional_teacher:
            required.append(optional_teacher)
        required = [item for item in required if item and item != "未公布"]
        if not required:
            return candidates
        result = [candidate for candidate in candidates if all(item in candidate.text for item in required)]
        return result or candidates
