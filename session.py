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
from fuckclassroom.core.config import AppConfig
from .proxy import Hy2ProxyError, Hy2ProxyManager
from .helpers import *
from .models import *

PUBLIC_LDAP_LOGIN_TIMEOUT_MS = 45_000


def _interactive_login_timeout_ms(
    access: SelectionAccess, submitted: bool, timeout_ms: int
) -> int:
    if access.mode == "public" and submitted:
        return min(timeout_ms, PUBLIC_LDAP_LOGIN_TIMEOUT_MS)
    return timeout_ms


class SelectionSessionMixin:
    def get_session_status(self) -> WebVpnSessionStatus:
        access = self._selection_access()
        if not self.storage_state_path.exists():
            return WebVpnSessionStatus(False, f"当前使用{access.label}，尚未保存教务会话")
        storage_state = self._read_json(self.storage_state_path, default={})
        cookies = storage_state.get("cookies", []) if isinstance(storage_state, dict) else []
        if not cookies:
            return WebVpnSessionStatus(False, "教务会话文件中没有 cookie，请重新登录")
        meta = self._read_json(self.session_meta_path, default={})
        saved_mode = self._saved_access_mode()
        if saved_mode != access.mode:
            saved_label = _selection_access(saved_mode).label
            return WebVpnSessionStatus(
                False,
                f"当前检测为{access.label}，已保存的是{saved_label}会话，请重新登录",
                saved_at=meta.get("saved_at"),
                final_url=meta.get("final_url"),
            )
        check = self._read_json(self.session_check_path, default={})
        is_valid = check.get("is_valid") if isinstance(check, dict) else None
        checked_at = check.get("checked_at") if isinstance(check, dict) else None
        message = f"已保存{access.label}教务会话"
        if is_valid is True:
            message = f"{access.label}教务会话有效"
        elif is_valid is False:
            reason = str(check.get("message") or "会话已失效，请重新登录")
            message = f"{access.label}教务会话无效：{reason}"
        return WebVpnSessionStatus(
            True,
            message,
            saved_at=meta.get("saved_at"),
            final_url=meta.get("final_url"),
            is_valid=is_valid if isinstance(is_valid, bool) else None,
            checked_at=str(checked_at) if checked_at else None,
        )

    def get_credential_status(self) -> dict[str, object]:
        return self.credential_store.public_status()

    def clear_session(self) -> None:
        with self._login_lock:
            with self._selection_token_lock:
                self._selection_token = ""
                self._selection_token_expires_at = 0.0
                self._selection_token_mode = ""
            for path in (
                self.storage_state_path,
                self.session_meta_path,
                self.session_check_path,
            ):
                path.unlink(missing_ok=True)

    def verify_saved_session(
        self,
        timeout_ms: int = 15_000,
        *,
        auto_relogin: bool = True,
    ) -> WebVpnSessionStatus:
        with self._login_lock:
            deadline = time.monotonic() + max(1.0, timeout_ms / 1000)
            status = self._verify_saved_session_once(min(timeout_ms, 5_000))
            if status.is_valid is True or not auto_relogin:
                return status
            try:
                credentials = self.credential_store.load()
            except CredentialStoreError as exc:
                self._write_session_check(_session_check(False, str(exc)))
                return self.get_session_status()
            if credentials is None:
                return status
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms < 1_000:
                return status
            try:
                self._login_with_saved_credentials(credentials, timeout_ms=remaining_ms)
            except CourseSelectionApiError as exc:
                self._write_session_check(_session_check(False, f"自动登录失败：{exc}"))
                return self.get_session_status()
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms < 1_000:
                return self.get_session_status()
            return self._verify_saved_session_once(min(remaining_ms, 5_000))

    def _verify_saved_session_once(self, timeout_ms: int) -> WebVpnSessionStatus:
        status = self.get_session_status()
        if not status.is_saved:
            return status
        access = self._selection_access()
        try:
            opener, jar = self._selection_http_client()
            content, _, final_url = self._request_selection_bytes(
                opener,
                access.home_url,
                timeout_seconds=max(1.0, timeout_ms / 1000),
                accept="text/html,application/xhtml+xml,application/json",
            )
            preview = content[:5000].decode("utf-8", errors="ignore").lower()
            combined = f"{final_url} {preview}"
            if "/student/home" not in combined and JW_INTERNAL_HOST not in combined:
                raise CourseSelectionApiError("没有进入本科教务页面，请重新登录")
            self._persist_selection_cookies(jar)
            self._write_session_check(_session_check(True, f"{access.label}教务会话有效", final_url))
        except CourseSelectionApiError as exc:
            self._write_session_check(_session_check(False, str(exc), access.home_url))
        return self.get_session_status()

    def login(self, timeout_ms: int = 120_000) -> None:
        self._ensure_session_dir()
        with self._playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(WEBVPN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            print("请在弹出的浏览器中完成 WebVPN 登录。")
            input("登录完成后按 Enter，脚本会打开本科教务系统并保存独立会话...")
            page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            self._write_session_check(_session_check(True, "WebVPN/教务会话有效", page.url))
            print(f"WebVPN/教务会话已保存：{self.storage_state_path}")
            input("按 Enter 关闭浏览器...")
            browser.close()

    def open_home(self, timeout_ms: int = 120_000) -> None:
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print(f"已打开本科教务系统：{page.url}")
        input("操作完成后按 Enter 保存当前会话并关闭浏览器...")
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url)
        context.close()

    def login_for_web(
        self,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 600_000,
        *,
        force_interactive: bool = False,
    ) -> str:
        self._ensure_session_dir()
        _report(progress, 1, "正在等待其他登录流程结束")
        with self._login_lock:
            try:
                credentials = self.credential_store.load()
            except CredentialStoreError as exc:
                raise CourseSelectionApiError(str(exc)) from exc
            if force_interactive:
                return self._login_interactively(
                    progress,
                    timeout_ms,
                    credentials,
                    reuse_saved_session=False,
                )
            access = self._selection_access(force=True)
            if credentials:
                _report(progress, 5, "正在使用保存的账号自动登录本科教务")
                try:
                    self._login_with_saved_credentials(credentials, timeout_ms=min(timeout_ms, 60_000), progress=progress)
                    _report(progress, 100, f"{access.label}教务会话已自动恢复")
                    return "/selection"
                except CourseSelectionApiError as exc:
                    _report(
                        progress,
                        12,
                        f"自动登录需要人工确认：{exc}，正在打开登录窗口",
                    )
            return self._login_interactively(
                progress,
                timeout_ms,
                credentials,
                reuse_saved_session=False,
            )

    def _login_interactively(
        self,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 600_000,
        credentials: SelectionCredentials | None = None,
        *,
        reuse_saved_session: bool = True,
    ) -> str:
        self._ensure_session_dir()
        access = self._selection_access(force=True)
        self._invalidate_selection_token()
        _report(progress, 5, f"正在通过{access.label}打开本科教务入口")
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=False,
                    **self._playwright_proxy_options(access),
                )
                context = self._new_login_context(browser, reuse_saved_session=reuse_saved_session)
                page = context.new_page()
                self._goto_login_page(page, access.login_url, timeout_ms)
                filled = False
                submitted = False
                if credentials:
                    if access.mode == "public":
                        filled = self._fill_public_credentials(page, credentials)
                    else:
                        submitted = self._submit_saved_credentials(page, credentials)
                        filled = submitted
                if filled:
                    message = (
                        "已填入保存的账号密码，请确认后登录"
                        if access.mode == "public"
                        else "已填入保存的账号密码，正在等待统一认证"
                    )
                    _report(progress, 15, message)
                _report(progress, 20, f"请在弹出的浏览器中完成{access.label}统一身份认证，认证成功后程序会自动保存并关闭窗口")
                wait_timeout_ms = _interactive_login_timeout_ms(access, submitted, timeout_ms)
                deadline = datetime.now(timezone.utc).timestamp() + wait_timeout_ms / 1000
                next_home_attempt_at = 0.0
                while datetime.now(timezone.utc).timestamp() < deadline:
                    if page.is_closed():
                        self._save_closed_login_context(context, access)
                        browser.close()
                        _report(progress, 100, "浏览器已关闭，会话已尽量保存")
                        return "/selection"
                    open_pages = [item for item in context.pages if not item.is_closed()]
                    if open_pages:
                        page = open_pages[-1]
                    if credentials and is_sms_verification_page(page):
                        run_sms_verification(
                            page,
                            credentials.username,
                            self.verification_broker,
                            "本科教务",
                            progress,
                        )
                        self._goto_login_page(page, access.login_url, min(timeout_ms, 30_000))
                        self._submit_saved_credentials(page, credentials)
                        continue
                    if self._looks_like_jw_home(page):
                        _report(progress, 60, f"{access.label}教务登录成功，正在保存会话")
                        page.wait_for_timeout(250)
                        context.storage_state(path=str(self.storage_state_path))
                        self._write_session_meta(page.url)
                        self._write_session_check(_session_check(True, f"{access.label}教务会话有效", page.url))
                        self._write_access_mode(access.mode)
                        browser.close()
                        _report(progress, 100, f"{access.label}教务会话已保存")
                        return "/selection"
                    now = datetime.now(timezone.utc).timestamp()
                    if access.mode == "webvpn" and self._looks_like_saved_webvpn_session(context, page) and now >= next_home_attempt_at:
                        _report(progress, 45, "WebVPN 登录成功，正在自动进入本科教务首页")
                        next_home_attempt_at = now + 3
                        try:
                            page.goto(
                                access.home_url,
                                wait_until="domcontentloaded",
                                timeout=min(timeout_ms, 30_000),
                            )
                        except Exception:  # The navigation may continue after Playwright times out.
                            pass
                        continue
                    try:
                        page.wait_for_timeout(500)
                    except Exception as exc:
                        if "closed" not in str(exc).lower():
                            raise
                        self._save_closed_login_context(context, access)
                        _report(progress, 100, "浏览器已关闭，会话已尽量保存")
                        return "/selection"
                if access.mode == "public" and submitted:
                    browser.close()
                    message = "公网教务 LDAP 登录接口长时间没有响应，请稍后重试"
                    self._write_session_check(
                        _session_check(False, message, access.login_url)
                    )
                    raise CourseSelectionApiError(message)
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta(page.url if not page.is_closed() else "")
                browser.close()
                _report(progress, 100, "登录等待超时，已保存当前浏览器会话")
                self._write_access_mode(access.mode)
                return "/selection"
        except PermissionError as exc:
            raise PermissionError("Windows 拒绝访问：可能是浏览器进程启动、Python 缓存文件或教务会话文件被占用/无权限") from exc

    def _login_with_saved_credentials(
        self,
        credentials: SelectionCredentials,
        *,
        timeout_ms: int,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._ensure_session_dir()
        access = self._selection_access(force=True)
        self._invalidate_selection_token()
        deadline = time.monotonic() + max(1.0, min(timeout_ms / 1000, 60.0))
        with self._playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                **self._playwright_proxy_options(access),
            )
            context = self._new_login_context(browser)
            page = context.new_page()
            try:
                self._goto_login_page(page, access.login_url, timeout_ms)
                if self._looks_like_jw_home(page):
                    self._save_authenticated_context(context, page, access)
                    return
                if not self._submit_saved_credentials(page, credentials):
                    raise CourseSelectionApiError("统一认证页没有出现账号登录表单")

                next_home_attempt_at = 0.0
                while time.monotonic() < deadline:
                    open_pages = [item for item in context.pages if not item.is_closed()]
                    if not open_pages:
                        raise CourseSelectionApiError("自动登录页面已关闭")
                    page = open_pages[-1]
                    if self._looks_like_jw_home(page):
                        self._save_authenticated_context(context, page, access)
                        return
                    if is_sms_verification_page(page):
                        run_sms_verification(
                            page,
                            credentials.username,
                            self.verification_broker,
                            "本科教务",
                            progress,
                        )
                        remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1000))
                        self._goto_login_page(page, access.login_url, min(remaining_ms, 30_000))
                        if not self._looks_like_jw_home(page):
                            self._submit_saved_credentials(page, credentials)
                        continue
                    reason = self._login_interaction_reason(page)
                    if reason:
                        context.storage_state(path=str(self.storage_state_path))
                        self._write_access_mode(access.mode)
                        raise CourseSelectionApiError(reason)
                    now = time.monotonic()
                    if (
                        access.mode == "webvpn"
                        and self._looks_like_saved_webvpn_session(context, page)
                        and now >= next_home_attempt_at
                    ):
                        next_home_attempt_at = now + 3
                        remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1000))
                        self._goto_login_page(page, access.home_url, min(remaining_ms, 30_000))
                        continue
                    page.wait_for_timeout(400)
                context.storage_state(path=str(self.storage_state_path))
                self._write_access_mode(access.mode)
                raise CourseSelectionApiError("自动登录等待超时")
            finally:
                browser.close()

    def _new_login_context(self, browser: Any, *, reuse_saved_session: bool = True) -> Any:
        if reuse_saved_session and self.storage_state_path.exists():
            try:
                return browser.new_context(storage_state=str(self.storage_state_path))
            except Exception:
                pass
        return browser.new_context()

    @staticmethod
    def _fill_public_credentials(page: Any, credentials: SelectionCredentials) -> bool:
        try:
            username = page.locator('input[placeholder="用户名"]')
            password = page.locator('input[placeholder="密码"]')
            username.wait_for(state="visible", timeout=5_000)
            password.wait_for(state="visible", timeout=5_000)
            username.fill(credentials.username)
            password.fill(credentials.password)
            return True
        except Exception:
            return False

    @staticmethod
    def _submit_saved_credentials(page: Any, credentials: SelectionCredentials) -> bool:
        try:
            if "/student/ldap/login" in str(page.url):
                submit = page.locator('button:has-text("登录")').first
                submit.wait_for(state="visible", timeout=5_000)
                if not SelectionSessionMixin._fill_public_credentials(page, credentials):
                    return False
                submit.evaluate("element => setTimeout(() => element.click(), 0)")
                return True
        except Exception:
            return False
        return submit_saved_credentials(page, credentials)

    def _save_closed_login_context(
        self,
        context: Any,
        access: SelectionAccess,
    ) -> None:
        try:
            context.storage_state(path=str(self.storage_state_path))
        except Exception:
            pass
        self._write_session_meta("")
        self._write_access_mode(access.mode)

    @staticmethod
    def _login_interaction_reason(page: Any) -> str:
        try:
            captcha = page.locator('input[placeholder="验证码"]')
            if captcha.count() and captcha.is_visible():
                return "公网教务登录要求输入图形验证码"
            error_tip = page.locator(".text-danger")
            if error_tip.count() and error_tip.is_visible():
                message = error_tip.inner_text().strip()
                if message:
                    return f"公网教务返回：{message}"
        except Exception:
            pass
        return login_interaction_reason(page)

    def _goto_login_page(self, page: Any, url: str, timeout_ms: int) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:
                last_error = exc
                retryable = "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc) or "502" in str(exc)
                if not retryable or attempt == 2:
                    break
                page.wait_for_timeout(500 * (attempt + 1))
        detail = str(last_error).split("Call log:", 1)[0].strip() if last_error else "未知错误"
        raise CourseSelectionApiError(f"教务登录入口连接失败：{detail}") from last_error

    def _save_authenticated_context(
        self,
        context: Any,
        page: Any,
        access: SelectionAccess,
    ) -> None:
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url)
        self._write_session_check(_session_check(True, f"{access.label}教务会话有效", page.url))
        self._write_access_mode(access.mode)

    def open_home_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在验证本科教务系统")
        error_url = self._verify_session_for_web(progress, timeout_ms)
        if error_url:
            return error_url
        _report(progress, 100, "本科教务会话可用")
        return "/selection"

    def open_course_select_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在验证选课入口")
        error_url = self._verify_session_for_web(progress, timeout_ms)
        if error_url:
            return error_url
        return self._open_saved_session_page(self._selection_access().course_select_url, "选课入口会话可用", progress, timeout_ms)

    def _verify_session_for_web(
        self,
        progress: ProgressCallback | None,
        timeout_ms: int,
    ) -> str | None:
        _report(progress, 15, "正在检查教务会话，失效时将自动重新登录")
        status = self.verify_saved_session(
            timeout_ms=min(timeout_ms, 15_000),
            auto_relogin=True,
        )
        if status.is_valid is True:
            _report(progress, 70, status.message)
            return None
        message = status.message or "会话已失效且自动登录失败，请手动登录"
        _report(progress, 100, message)
        return f"/selection?{urlencode({'select_error': message})}"

    def _open_saved_session_page(
        self,
        url: str,
        ready_message: str,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 1_800_000,
    ) -> str:
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 18, "正在验证 WebVPN/教务会话")
        check = self._verify_opened_session(context, page)
        self._write_session_check(check)
        if not check["is_valid"]:
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url if not page.is_closed() else "")
            context.close()
            message = str(check.get("message") or "会话已失效，请重新登录")
            _report(progress, 100, message)
            return f"/selection?{urlencode({'select_error': message})}"
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()
        _report(progress, 100, ready_message)
        return "/selection"

    def capture_interfaces_for_web(
        self,
        progress: ProgressCallback | None = None,
        duration_seconds: int = 180,
        timeout_ms: int = 1_800_000,
    ) -> str:
        _report(progress, 5, "正在打开本科教务系统并准备抓取接口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        captured: list[dict[str, Any]] = []

        def on_response(response: Any) -> None:
            try:
                request = response.request
                if request.resource_type not in CAPTURE_RESOURCE_TYPES:
                    return
                url = request.url
                if WEBVPN_HOST not in (urlparse(url).hostname or "") and JW_INTERNAL_HOST not in url:
                    return
                record = self._capture_response(response)
                captured.append(record)
            except Exception as exc:
                captured.append({"capture_error": str(exc)})

        page.on("response", on_response)
        page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 15, "浏览器已打开，请进入选课页面并执行查询操作")
        deadline = datetime.now(timezone.utc).timestamp() + max(10, duration_seconds)
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                break
            left = int(deadline - datetime.now(timezone.utc).timestamp())
            percent = 15 + int((duration_seconds - max(0, left)) / max(1, duration_seconds) * 75)
            _report(progress, percent, f"正在抓取接口，剩余 {max(0, left)} 秒，已记录 {len(captured)} 条")
            page.wait_for_timeout(5_000)

        if not page.is_closed():
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
        context.close()
        capture_base = self._write_capture(captured)
        _report(progress, 100, f"接口抓取完成，已保存 {len(captured)} 条请求")
        return f"/selection/captures/{capture_base}"

    def highlight_for_web(
        self,
        target: SelectionTarget,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
    ) -> str:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        _report(progress, 5, "正在打开选课入口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page = self._prepare_selection_search_page(context, page, target, progress, timeout_ms)
        candidates = self.find_candidates(page, target)
        if candidates:
            _report(progress, 70, f"已高亮 {len(candidates)} 个候选课程，请在浏览器中确认")
        else:
            _report(progress, 70, "当前页面未找到候选课程，可手动调整页面后重新发起定位")
        deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta("")
                context.close()
                _report(progress, 100, "浏览器已关闭，会话已保存")
                return "/selection"
            page.wait_for_timeout(5_000)
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()
        _report(progress, 100, "定位任务已结束，会话已保存")
        return "/selection"

    def select_once_for_web(
        self,
        target: SelectionTarget,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
    ) -> str:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        _report(progress, 5, "正在打开选课入口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page = self._prepare_selection_search_page(context, page, target, progress, timeout_ms)
        candidates = self.find_candidates(page, target)
        if len(candidates) != 1:
            if candidates:
                _report(progress, 90, f"找到 {len(candidates)} 个候选，已高亮但不会自动点击")
            else:
                _report(progress, 90, "没有找到候选课程，未执行选课")
            self._wait_until_closed_or_timeout(context, page, timeout_ms)
            _report(progress, 100, "选课任务结束")
            return "/selection"

        clicked = self.click_candidate_action(page, candidates[0].index)
        if clicked:
            _report(progress, 80, f"已点击一次：{clicked}。如有弹窗或验证码，请手动完成")
        else:
            _report(progress, 80, "找到唯一候选，但未找到可点击的选课按钮")
        self._wait_until_closed_or_timeout(context, page, timeout_ms)
        _report(progress, 100, "选课任务结束")
        return "/selection"

    def _prepare_selection_search_page(
        self,
        context: Any,
        page: Any,
        target: SelectionTarget,
        progress: ProgressCallback | None,
        timeout_ms: int,
    ) -> Any:
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 20, "正在进入选课批次")
        page.wait_for_timeout(2_000)
        page = self._click_start_course_select(context, page)
        _report(progress, 35, "正在切换到全部课程并填写查询条件")
        page.wait_for_timeout(2_000)
        self._query_course_in_page(page, target)
        _report(progress, 55, "查询已提交，正在等待课程列表")
        page.wait_for_timeout(3_000)
        return page

    def _prepare_selected_lessons_page(
        self,
        context: Any,
        page: Any,
        progress: ProgressCallback | None,
        timeout_ms: int,
    ) -> Any:
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 20, "正在进入选课批次")
        page.wait_for_timeout(2_000)
        page = self._click_start_course_select(context, page)
        _report(progress, 35, "正在切换到已选课程")
        page.wait_for_timeout(2_000)
        _click_text_in_page(page, "已选课程")
        page.wait_for_timeout(3_000)
        return page

    @staticmethod
    def _click_start_course_select(context: Any, page: Any) -> Any:
        clicked = page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll("button, a, [role=button], .el-button"));
              const start = buttons.find((item) => {
                const text = (item.innerText || item.textContent || "").replace(/\\s+/g, "");
                return text.includes("开始选课") || text.includes("进入预览");
              });
              if (!start) return false;
              start.click();
              return true;
            }
            """
        )
        if not clicked:
            return page
        page.wait_for_timeout(3_000)
        pages = context.pages
        return pages[-1] if pages else page

    @staticmethod
    def _query_course_in_page(page: Any, target: SelectionTarget) -> None:
        query_text = target.course_code or target.course_name
        _click_text_in_page(page, "全部课程")
        page.wait_for_timeout(800)
        page.evaluate(
            """
            ({ queryText }) => {
              const setNativeValue = (input, value) => {
                if (!input) return false;
                const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
                descriptor.set.call(input, value);
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              };
              const inputs = Array.from(document.querySelectorAll("input"));
              const courseInput = inputs.find((input) => {
                const rect = input.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && (input.placeholder || "").includes("课程名称");
              });
              if (queryText) setNativeValue(courseInput, queryText);
            }
            """,
            {"queryText": query_text},
        )
        page.wait_for_timeout(300)
        _click_text_in_page(page, "查询")
