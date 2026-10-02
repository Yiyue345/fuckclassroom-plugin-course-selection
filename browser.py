from __future__ import annotations

import base64
import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
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

class SelectionBrowserMixin:
    def assist_select(self, target: SelectionTarget, timeout_ms: int = 120_000) -> None:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print("脚本已打开选课入口，请确认进入了正确批次/标签。")
        input("页面准备好后按 Enter，脚本会在当前页面查找目标课程...")

        candidates = self.find_candidates(page, target)
        if not candidates:
            print("当前页面没有找到匹配课程。请手动调整筛选条件或换到正确选课页面后重试。")
            input("按 Enter 保存当前会话并关闭浏览器...")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return

        print("找到以下候选行，浏览器中已用蓝色边框标出：")
        for candidate in candidates:
            preview = " ".join(candidate.text.split())
            print(f"[{candidate.index}] {preview[:220]}")

        raw_index = input("输入要操作的候选编号；直接回车则取消：").strip()
        if not raw_index:
            print("已取消。")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return
        try:
            index = int(raw_index)
        except ValueError as exc:
            raise ValueError("候选编号必须是数字") from exc

        confirm = input("确认只点击一次该行里的选课/选择/提交按钮？输入 YES 继续：").strip()
        if confirm != "YES":
            print("未确认，已取消。")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return

        clicked = self.click_candidate_action(page, index)
        if clicked:
            print(f"已点击：{clicked}")
            print("如果页面弹出确认框、验证码或二次确认，请在浏览器里手动完成。")
        else:
            print("没有在候选行中找到可点击的选课按钮，请在浏览器里手动处理已标出的课程。")
        input("处理完成后按 Enter 保存当前会话并关闭浏览器...")
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url)
        context.close()

    def _wait_for_course_selection_ready(self, context: Any, page: Any, timeout_ms: int) -> Any:
        deadline = time.monotonic() + max(1_000, timeout_ms) / 1000
        last_url = page.url
        while time.monotonic() < deadline:
            open_pages = [item for item in context.pages if not item.is_closed()]
            if open_pages:
                page = open_pages[-1]
            last_url = page.url
            if _selection_route_ids(last_url) and self._read_selection_token(context, page):
                return page
            page = self._click_start_course_select(context, page)
            page.wait_for_timeout(500)
        if _selection_route_ids(last_url):
            raise RuntimeError("未在选课页读取到课程选择 token：平台尚未生成选课凭证，请重新登录并进入一次选课批次")
        raise RuntimeError("未在选课页读取到课程选择 token：没有进入具体选课批次，请重新登录后打开选课入口")

    @staticmethod
    def _read_selection_token(context: Any, page: Any) -> str:
        try:
            token = page.evaluate(
                """
                () => {
                  const cookie = document.cookie.split(";").map((part) => part.trim())
                    .find((part) => part.startsWith("cs-course-select-student-token="));
                  if (cookie) return decodeURIComponent(cookie.slice(cookie.indexOf("=") + 1));
                  for (const storage of [localStorage, sessionStorage]) {
                    for (let index = 0; index < storage.length; index += 1) {
                      const key = storage.key(index) || "";
                      if (key.toLowerCase().includes("cs-course-select-student-token")) {
                        return storage.getItem(key) || "";
                      }
                    }
                  }
                  return "";
                }
                """
            )
            if token:
                return str(token)
        except Exception:
            pass
        try:
            for cookie in context.cookies():
                if cookie.get("name") == "cs-course-select-student-token" and cookie.get("value"):
                    return str(cookie["value"])
        except Exception:
            pass
        return ""

    def _fetch_selection_info_payload(self, context: Any, page: Any) -> dict[str, Any]:
        route_ids = _selection_route_ids(page.url)
        token = self._read_selection_token(context, page)
        if not token:
            raise RuntimeError("未在选课页读取到课程选择 token，请重新打开选课入口后刷新")
        if route_ids is None:
            raise RuntimeError("当前页面不是具体选课批次页面")
        student_id, turn_id = route_ids
        return page.evaluate(
            """
            async ({token, studentId, turnId}) => {
              const apiBase = "https://bkjwtest.guet.edu.cn/course-selection-api/api/v1/student/course-select";
              const api = async (path, options = {}) => {
                const headers = {
                  "Accept": "application/json, text/plain, */*",
                  "Authorization": token,
                  ...(options.body ? {"Content-Type": "application/json"} : {}),
                };
                const response = await fetch(`${apiBase}${path}`, {
                  method: options.method || "GET",
                  headers,
                  body: options.body ? JSON.stringify(options.body) : undefined,
                  credentials: "include",
                });
                if (!response.ok) {
                  throw new Error(`${options.method || "GET"} ${path} 返回 ${response.status}`);
                }
                return response.json();
              };
              const readJsonStorage = (namePart) => {
                const key = Object.keys(localStorage).find((item) => item.includes(namePart));
                if (!key) return null;
                try {
                  return JSON.parse(localStorage.getItem(key));
                } catch {
                  return null;
                }
              };
              const options = readJsonStorage("cs-course-select-options") || {};
              const semesterId = options?.turn?.semester?.id || options?.turn?.semesterId || options?.semester?.id || null;
              const title = document.title || "选课";
              const [selectedLessons, queryCondition, majorPlan, repairedCourses] = await Promise.all([
                api(`/selected-lessons/${turnId}/${studentId}`),
                api(`/query-condition/${turnId}`),
                api(`/major-plan/${turnId}/${studentId}`),
                api(`/repaired-courses/${turnId}/${studentId}`),
              ]);
              const requestBody = {
                turnId,
                studentId,
                semesterId,
                pageNo: 1,
                pageSize: 1000,
                courseNameOrCode: "",
                lessonNameOrCode: "",
                teacherNameOrCode: "",
                week: "",
                grade: "",
                departmentId: "",
                majorId: "",
                adminclassId: "",
                campusId: "",
                openDepartmentId: "",
                courseTypeId: "",
                coursePropertyId: "",
                canSelect: 1,
                _canSelect: "可选",
                creditGte: null,
                creditLte: null,
                hasCount: null,
                ids: null,
                substitutedCourseId: null,
                courseSubstitutePoolId: null,
                sortField: "course",
                sortType: "ASC",
              };
              const queryLessons = await api(`/query-lesson/${studentId}/${turnId}`, {method: "POST", body: requestBody});
              const lessonIds = ((queryLessons.data && queryLessons.data.lessons) || []).map((lesson) => lesson.id).filter(Boolean);
              const stdCount = lessonIds.length
                ? await api(`/std-count?lessonIds=${lessonIds.join(",")}`)
                : {data: {}};
              return {
                title,
                turnId,
                studentId,
                selectedLessons,
                queryCondition,
                majorPlan,
                repairedCourses,
                queryLessons,
                stdCount,
              };
            }
            """,
            {"token": token, "studentId": student_id, "turnId": turn_id},
        )

    def _normalize_selection_info(self, raw_info: dict[str, Any]) -> dict[str, Any]:
        selected_ids = _extract_selected_lesson_ids(raw_info.get("selectedLessons"))
        count_map = _extract_count_map(raw_info.get("stdCount"))
        plan_courses = _extract_plan_courses(raw_info.get("majorPlan"))
        queried_lessons = _extract_lessons(raw_info.get("queryLessons"))
        repaired_lessons = _extract_lessons(raw_info.get("repairedCourses"))
        selected_lessons = _extract_lessons(raw_info.get("selectedLessons"))
        major_lessons = _merge_lessons(plan_courses, queried_lessons)
        sections = {
            "major-plan": self._normalize_lesson_list(major_lessons, count_map, selected_ids),
            "all": self._normalize_lesson_list(queried_lessons, count_map, selected_ids),
            "retake": self._normalize_lesson_list(repaired_lessons, count_map, selected_ids),
            "selected": self._normalize_lesson_list(selected_lessons, count_map, selected_ids),
        }
        return {
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "title": str(raw_info.get("title") or "选课信息"),
            "turn_id": _safe_int(raw_info.get("turnId")),
            "student_id": _safe_int(raw_info.get("studentId")),
            "source": "course-selection-api",
            "courses": sections["major-plan"],
            "sections": sections,
        }

    @staticmethod
    def _normalize_lesson_list(
        lessons: list[dict[str, Any]],
        count_map: dict[int, int],
        selected_ids: set[int],
    ) -> list[dict[str, Any]]:
        courses = [_normalize_lesson(lesson, count_map, selected_ids) for lesson in lessons]
        courses = [course for course in courses if course["lesson_id"] is not None]
        courses.sort(key=lambda item: (item["course_name"], item["lesson_code"], item["teachers"]))
        return courses

    def _write_selection_info(self, payload: dict[str, Any]) -> None:
        atomic_write_text(
            self.selection_info_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _write_selection_action_result(self, payload: dict[str, Any]) -> None:
        atomic_write_text(
            self.selection_action_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _build_action_result(
        self,
        action: str,
        course: dict[str, Any],
        clicked: str | None,
        responses: list[dict[str, Any]],
        status: str | None = None,
        fallback_message: str | None = None,
    ) -> dict[str, Any]:
        summary = _summarize_action_responses(responses)
        action_label = "退课" if action == "drop" else "选课"
        inferred_status = status or summary["status"] or ("已提交" if clicked else "未执行")
        message = summary["message"] or fallback_message
        if not message:
            message = f"已点击{action_label}按钮，平台未返回明确提示" if clicked else f"未找到可点击的{action_label}按钮"
        return {
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "action": action,
            "action_label": action_label,
            "lesson_id": _safe_int(course.get("lesson_id")),
            "course_name": str(course.get("course_name") or ""),
            "lesson_code": str(course.get("lesson_code") or ""),
            "clicked": clicked,
            "status": inferred_status,
            "message": message,
            "responses": summary["responses"],
        }

    @staticmethod
    def _install_action_response_capture(page: Any) -> None:
        page.evaluate(
            """
            () => {
              const sensitiveParts = ["token", "ticket", "session", "cookie", "auth", "password", "secret", "jwt"];
              const isSensitiveKey = (key) => sensitiveParts.some((part) => String(key).toLowerCase().includes(part));
              const sanitize = (value, depth = 0) => {
                if (depth > 5) return "[truncated]";
                if (Array.isArray(value)) return value.slice(0, 20).map((item) => sanitize(item, depth + 1));
                if (value && typeof value === "object") {
                  const result = {};
                  for (const [key, item] of Object.entries(value)) {
                    result[key] = isSensitiveKey(key) ? "***" : sanitize(item, depth + 1);
                  }
                  return result;
                }
                if (typeof value === "string") return value.length > 1000 ? `${value.slice(0, 1000)}...` : value;
                return value;
              };
              const cleanUrl = (url) => {
                try {
                  const parsed = new URL(url, location.href);
                  for (const key of Array.from(parsed.searchParams.keys())) {
                    if (isSensitiveKey(key)) parsed.searchParams.set(key, "***");
                  }
                  return `${parsed.origin}${parsed.pathname}${parsed.search}`;
                } catch {
                  return String(url || "");
                }
              };
              const shouldCapture = (url) => {
                const text = String(url || "");
                return text.includes("course-selection-api") || text.includes("course-select");
              };
              const parseBody = (text) => {
                if (!text) return null;
                try {
                  return sanitize(JSON.parse(text));
                } catch {
                  return text.length > 1000 ? `${text.slice(0, 1000)}...` : text;
                }
              };
              const pushRecord = (record) => {
                window.__fcActionResponses.push(record);
                if (window.__fcActionResponses.length > 12) window.__fcActionResponses.shift();
              };
              window.__fcActionResponses = [];
              if (!window.__fcActionFetchPatched) {
                const originalFetch = window.fetch;
                window.fetch = async (...args) => {
                  const response = await originalFetch.apply(window, args);
                  const request = args[0];
                  const url = typeof request === "string" ? request : (request && request.url) || "";
                  const method = (args[1] && args[1].method) || (request && request.method) || "GET";
                  if (shouldCapture(url)) {
                    response.clone().text().then((text) => {
                      pushRecord({
                        type: "fetch",
                        method,
                        url: cleanUrl(url),
                        status: response.status,
                        body: parseBody(text),
                      });
                    }).catch(() => {});
                  }
                  return response;
                };
                window.__fcActionFetchPatched = true;
              }
              if (!window.__fcActionXhrPatched) {
                const originalOpen = XMLHttpRequest.prototype.open;
                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                  this.__fcActionRequest = { method, url };
                  return originalOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                  this.addEventListener("loadend", () => {
                    const request = this.__fcActionRequest || {};
                    if (!shouldCapture(request.url)) return;
                    pushRecord({
                      type: "xhr",
                      method: request.method || "GET",
                      url: cleanUrl(request.url),
                      status: this.status,
                      body: parseBody(this.responseText),
                    });
                  });
                  return originalSend.apply(this, arguments);
                };
                window.__fcActionXhrPatched = true;
              }
            }
            """
        )

    @staticmethod
    def _read_action_responses(page: Any) -> list[dict[str, Any]]:
        responses = page.evaluate("() => window.__fcActionResponses || []")
        return responses if isinstance(responses, list) else []

    @staticmethod
    def find_candidates(page: Any, target: SelectionTarget) -> list[SelectionCandidate]:
        rows = page.evaluate(
            """
            (keywords) => {
              document.querySelectorAll("[data-fc-select-index]").forEach((node) => {
                node.style.outline = "";
                node.removeAttribute("data-fc-select-index");
              });
              const containers = Array.from(document.querySelectorAll(
                "tr, li, .el-table__row, .ant-table-row, [role=row], .course-item, .list-item"
              ));
              const normalizedKeywords = keywords.map((item) => String(item).trim()).filter(Boolean);
              const candidates = [];
              const seen = new Set();
              for (const node of containers) {
                const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
                if (!text || text.length > 1200) continue;
                const matched = normalizedKeywords.every((keyword) => text.includes(keyword));
                if (!matched || seen.has(text)) continue;
                seen.add(text);
                const index = candidates.length + 1;
                node.setAttribute("data-fc-select-index", String(index));
                node.style.outline = "3px solid #2563eb";
                node.style.outlineOffset = "2px";
                candidates.push({ index, text });
                if (candidates.length >= 20) break;
              }
              return candidates;
            }
            """,
            target.keywords,
        )
        return [SelectionCandidate(index=int(item["index"]), text=str(item["text"])) for item in rows]

    @staticmethod
    def click_candidate_action(page: Any, index: int, action_words: list[str] | None = None) -> str | None:
        return page.evaluate(
            """
            ({ index, actionWords }) => {
              const row = document.querySelector(`[data-fc-select-index="${index}"]`);
              if (!row) return null;
              const controls = Array.from(row.querySelectorAll(
                "button, a, [role=button], .el-button, .ant-btn"
              ));
              for (const control of controls) {
                const text = (control.innerText || control.textContent || "").trim();
                const disabled = control.disabled || control.getAttribute("aria-disabled") === "true";
                if (disabled) continue;
                if (actionWords.some((word) => text.includes(word))) {
                  control.click();
                  return text || control.tagName;
                }
              }
              return null;
            }
            """,
            {"index": index, "actionWords": action_words or ["选课", "选择", "加入", "提交", "报名"]},
        )

    def _new_context_with_session(self, timeout_ms: int) -> Any:
        if not self.storage_state_path.exists():
            raise FileNotFoundError("尚未保存 WebVPN 会话，请先运行 login 命令")
        playwright = self._playwright()
        manager = playwright.__enter__()
        access = self._selection_access()
        browser = manager.chromium.launch(
            headless=True,
            **self._playwright_proxy_options(access),
        )
        try:
            return _ManagedContext(
                playwright,
                browser,
                browser.new_context(storage_state=str(self.storage_state_path)),
                timeout_ms,
            )
        except Exception:
            browser.close()
            playwright.__exit__(None, None, None)
            raise

    def _wait_until_closed_or_timeout(self, context: Any, page: Any, timeout_ms: int) -> None:
        deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta("")
                context.close()
                return
            page.wait_for_timeout(5_000)
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()

    def _verify_opened_session(self, context: Any, page: Any) -> dict[str, Any]:
        try:
            page.wait_for_timeout(1_500)
            result = page.evaluate(
                """
                async () => {
                  const pageUrl = location.href;
                  const title = document.title || "";
                  try {
                    const response = await fetch(location.href, {
                      credentials: "include",
                      headers: { Accept: "text/html,application/xhtml+xml,application/json,*/*" },
                    });
                    const text = await response.text();
                    return {
                      request_ok: true,
                      status: response.status,
                      response_url: response.url,
                      page_url: pageUrl,
                      title,
                      body_preview: text.slice(0, 2000),
                    };
                  } catch (error) {
                    return {
                      request_ok: false,
                      status: null,
                      response_url: "",
                      page_url: pageUrl,
                      title,
                      error: error && error.message ? error.message : String(error),
                    };
                  }
                }
                """
            )
        except Exception as exc:
            return _session_check(False, f"会话验证请求失败：{exc}")

        final_url = str(result.get("response_url") or result.get("page_url") or page.url)
        status = _safe_int(result.get("status"))
        title = str(result.get("title") or "")
        body_preview = str(result.get("body_preview") or "")
        combined = f"{final_url} {title} {body_preview}".lower()
        if not result.get("request_ok"):
            return _session_check(False, f"会话验证请求失败：{result.get('error') or '未知错误'}", final_url, status)
        if any(marker in combined for marker in ("authserver", "统一身份认证", "/login", "cas/login")):
            return _session_check(False, "会话已跳转到登录页，请重新登录", final_url, status)
        if status is not None and status >= 400:
            return _session_check(False, f"教务访问返回 {status}，请重新登录或稍后再试", final_url, status)
        if any(marker in combined for marker in ("wengine-auth-failed", "访问出错 - 403", "access forbidden")):
            return _session_check(False, "WebVPN 网关拒绝访问，请重新登录或稍后再试", final_url, status)
        if self._looks_like_jw_page(context, page, final_url, title):
            return _session_check(True, "WebVPN/教务会话有效", final_url, status)
        return _session_check(False, "没有进入本科教务页面，请重新登录", final_url, status)

    @staticmethod
    def _looks_like_jw_page(context: Any, page: Any, final_url: str, title: str) -> bool:
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []
        lowered = f"{page.url} {final_url} {title}".lower()
        if any(marker in lowered for marker in ("login", "cas", "统一身份认证")):
            return False
        hosts = {urlparse(page.url).hostname or "", urlparse(final_url).hostname or ""}
        if not hosts.intersection({WEBVPN_HOST, JW_INTERNAL_HOST}):
            return False
        expected_host = WEBVPN_HOST if WEBVPN_HOST in hosts else JW_INTERNAL_HOST
        if not any((cookie.get("domain") or "").endswith(expected_host) for cookie in cookies):
            return False
        return any(marker in lowered for marker in ("/student/home", "/student/for-std/course-select", JW_INTERNAL_HOST))

    def _write_session_check(self, payload: dict[str, Any]) -> None:
        self._ensure_session_dir()
        self.session_check_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_session_meta(self, final_url: str) -> None:
        self._ensure_session_dir()
        payload = {
            "saved_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "webvpn_url": WEBVPN_URL,
            "jw_webvpn_home_url": JW_WEBVPN_HOME_URL,
            "jw_internal_host": JW_INTERNAL_HOST,
            "final_url": final_url,
            "final_host": urlparse(final_url).hostname,
        }
        self.session_meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_capture(self, requests: list[dict[str, Any]]) -> str:
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("selection_capture_%Y%m%d_%H%M%S")
        payload = {
            "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "jw_webvpn_home_url": JW_WEBVPN_HOME_URL,
            "jw_internal_host": JW_INTERNAL_HOST,
            "requests": requests,
        }
        json_path = self._capture_path(name, ".json")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._capture_path(name, ".md").write_text(_capture_markdown(payload), encoding="utf-8")
        return name

    def _capture_path(self, name: str, suffix: str) -> Path:
        safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        return self.capture_dir / f"{safe_name}{suffix}"

    def _ensure_session_dir(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _capture_response(response: Any) -> dict[str, Any]:
        request = response.request
        record: dict[str, Any] = {
            "method": request.method,
            "url": _sanitize_url(request.url),
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": _sanitize_mapping(request.headers),
            "response_headers": _sanitize_mapping(response.headers),
            "post_data": _sanitize_body(request.post_data),
        }
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                body = response.text()
            except Exception:
                body = ""
            record["response_body"] = _sanitize_body(body, max_length=6000)
        return record

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _looks_like_jw_home(page: Any) -> bool:
        try:
            url = page.url
            title = page.title()
        except Exception:
            return False
        if (urlparse(url).hostname or "") not in {WEBVPN_HOST, JW_INTERNAL_HOST}:
            return False
        if "/student/home" not in url and JW_INTERNAL_HOST not in url:
            return False
        lowered = f"{url} {title}".lower()
        return "login" not in lowered and "cas" not in lowered

    @staticmethod
    def _looks_like_saved_webvpn_session(context: Any, page: Any) -> bool:
        try:
            url = page.url
            title = page.title()
            cookies = context.cookies()
        except Exception:
            return False
        if WEBVPN_HOST not in (urlparse(url).hostname or ""):
            return False
        lowered = f"{url} {title}".lower()
        if any(marker in lowered for marker in ("login", "cas", "统一身份认证")):
            return False
        return any(
            cookie.get("name") == "wengine_vpn_ticket"
            and (cookie.get("domain") or "").endswith(WEBVPN_HOST)
            for cookie in cookies
        )

    @staticmethod
    def _playwright() -> Any:
        from playwright.sync_api import sync_playwright

        return sync_playwright()

    def get_access_info(self, force: bool = False) -> dict[str, str]:
        # Rendering a status page must not launch the optional unsigned Hy2 runner.
        with self.defer_hy2_start():
            access = self._selection_access(force=force)
        return {"mode": access.mode, "label": access.label}

    @contextmanager
    def defer_hy2_start(self):
        """Temporarily probe only direct/public/WebVPN access paths.

        This is used during application startup and passive status rendering so
        Windows Smart App Control is not prompted by an optional local executable.
        Explicit selection operations continue to use Hy2 when it is enabled.
        """
        previous = bool(getattr(self._access_policy, "defer_hy2", False))
        self._access_policy.defer_hy2 = True
        try:
            yield
        finally:
            self._access_policy.defer_hy2 = previous

    def invalidate_access_mode(self) -> None:
        with self._access_mode_lock:
            self._access_mode = ""
            self._access_mode_checked_at = 0.0
        self._invalidate_selection_token()

    def _selection_access(self, force: bool = False) -> SelectionAccess:
        return _selection_access(self._detect_access_mode(force=force))

    def _detect_access_mode(self, force: bool = False) -> str:
        with self._access_mode_lock:
            now = time.time()
            if not force and self._access_mode and self._access_mode_checked_at > now - 60:
                return self._access_mode
            mode = "webvpn"
            request = Request(
                CAMPUS_PROBE_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
            )
            try:
                with build_opener(_NoRedirectHandler()).open(request, timeout=1.5) as response:
                    if 200 <= int(response.status) < 300:
                        mode = "campus"
            except (HTTPError, URLError, TimeoutError, OSError):
                pass
            hy2_deferred = bool(getattr(self._access_policy, "defer_hy2", False))
            if mode == "webvpn" and self.config.hy2_enabled and not hy2_deferred:
                try:
                    proxy_url = self.hy2_proxy.ensure_started()
                    proxy_opener = build_opener(
                        ProxyHandler({"http": proxy_url, "https": proxy_url}),
                        _NoRedirectHandler(),
                    )
                    proxy_request = Request(
                        JW_DIRECT_HOME_URL,
                        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
                    )
                    try:
                        with proxy_opener.open(proxy_request, timeout=6) as response:
                            if 200 <= int(response.status) < 500:
                                mode = "hy2"
                    except HTTPError as exc:
                        if 300 <= exc.code < 500:
                            mode = "hy2"
                except (Hy2ProxyError, URLError, TimeoutError, OSError):
                    pass
            if mode == "webvpn":
                public_request = Request(
                    JW_PUBLIC_LOGIN_URL,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
                )
                try:
                    with build_opener(_NoRedirectHandler()).open(
                        public_request,
                        timeout=2,
                    ) as response:
                        if 200 <= int(response.status) < 400:
                            mode = "public"
                except HTTPError as exc:
                    if 300 <= exc.code < 400:
                        mode = "public"
                except (URLError, TimeoutError, OSError):
                    pass
            self._access_mode = mode
            self._access_mode_checked_at = now
            return mode

    def _saved_access_mode(self) -> str:
        payload = self._read_json(self.access_mode_path, default={})
        if not isinstance(payload, dict):
            return "webvpn"
        mode = str(payload.get("mode") or "webvpn")
        return mode if mode in {"campus", "public", "hy2", "webvpn"} else "webvpn"

    def _write_access_mode(self, mode: str) -> None:
        self._ensure_session_dir()
        self.access_mode_path.write_text(
            json.dumps({"mode": mode, "updated_at": _local_now()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _playwright_proxy_options(self, access: SelectionAccess) -> dict[str, Any]:
        if access.mode == "public":
            return {
                "args": ["--proxy-server=direct://", "--proxy-bypass-list=*"],
            }
        if access.mode != "hy2":
            return {}
        return {"proxy": {"server": self.hy2_proxy.ensure_started()}}
