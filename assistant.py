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

from fuckclassroom.auth.cas import LoginVerificationBroker
from fuckclassroom.auth.credentials import SelectionCredentialStore
from fuckclassroom.core.config import AppConfig
from .api import SelectionApiMixin
from .browser import SelectionBrowserMixin
from .catalog import SelectionCatalogMixin
from .helpers import _extract_count_map
from .proxy import Hy2ProxyManager
from .session import SelectionSessionMixin


_STD_COUNT_MAX_QUERY_LENGTH = 1400


class CourseSelectionAssistant(
    SelectionSessionMixin,
    SelectionCatalogMixin,
    SelectionApiMixin,
    SelectionBrowserMixin,
):
    def __init__(
        self,
        config: AppConfig | None = None,
        hy2_proxy: Hy2ProxyManager | None = None,
        credential_store: SelectionCredentialStore | None = None,
        verification_broker: LoginVerificationBroker | None = None,
        login_lock: threading.Lock | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.hy2_proxy = hy2_proxy or Hy2ProxyManager(self.config)
        self.session_dir = self.config.data_dir / "webvpn"
        self.credential_store = credential_store or SelectionCredentialStore(
            self.session_dir / "credentials.json"
        )
        self.storage_state_path = self.session_dir / "storage_state.json"
        self.session_meta_path = self.session_dir / "session_meta.json"
        self.verification_broker = verification_broker or LoginVerificationBroker()
        self.capture_dir = self.session_dir / "captures"
        self.selection_info_path = self.session_dir / "selection_info.json"
        self.selection_action_path = self.session_dir / "selection_action.json"
        self.access_mode_path = self.session_dir / "access_mode.json"
        self._access_mode = ""
        self._access_mode_checked_at = 0.0
        self._access_mode_lock = threading.Lock()
        self._access_policy = threading.local()
        self.session_check_path = self.session_dir / "session_check.json"
        self._selection_token = ""
        self._selection_token_expires_at = 0.0
        self._selection_token_mode = ""
        self._selection_token_lock = threading.Lock()
        self._login_lock = login_lock or threading.Lock()
        self._selection_action_lock = threading.Lock()

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
        """Split oversized std-count requests before the upstream server returns HTTP 414."""
        if method.upper() == "GET" and path.startswith("/std-count?"):
            parsed = urlparse(path)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            lesson_ids = [
                value.strip()
                for value in params.get("lessonIds", "").split(",")
                if value.strip()
            ]
            if lesson_ids and len(path) > _STD_COUNT_MAX_QUERY_LENGTH:
                merged: dict[int, int] = {}
                chunk: list[str] = []

                def request_chunk(values: list[str]) -> None:
                    if not values:
                        return
                    chunk_params = dict(params)
                    chunk_params["lessonIds"] = ",".join(values)
                    chunk_path = f"{parsed.path}?{urlencode(chunk_params)}"
                    payload = super(CourseSelectionAssistant, self)._request_selection_json(
                        opener,
                        token,
                        chunk_path,
                        method=method,
                        body=body,
                        timeout_seconds=timeout_seconds,
                    )
                    merged.update(_extract_count_map(payload))

                for lesson_id in lesson_ids:
                    candidate = [*chunk, lesson_id]
                    candidate_params = dict(params)
                    candidate_params["lessonIds"] = ",".join(candidate)
                    candidate_path = f"{parsed.path}?{urlencode(candidate_params)}"
                    if chunk and len(candidate_path) > _STD_COUNT_MAX_QUERY_LENGTH:
                        request_chunk(chunk)
                        chunk = [lesson_id]
                    else:
                        chunk = candidate
                request_chunk(chunk)
                return {"data": {str(lesson_id): count for lesson_id, count in merged.items()}}

        return super()._request_selection_json(
            opener,
            token,
            path,
            method=method,
            body=body,
            timeout_seconds=timeout_seconds,
        )
