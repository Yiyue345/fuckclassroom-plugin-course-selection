from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fuckclassroom.core.plugins import PluginServiceError


WEBVPN_URL = "https://v.guet.edu.cn/"
JW_WEBVPN_BASE_URL = (
    "https://v.guet.edu.cn/https/77726476706e69737468656265737421"
    "f2fc4b8b33357b44300f9ca98c1b2631e4350e37"
)
JW_WEBVPN_HOME_URL = f"{JW_WEBVPN_BASE_URL}/student/home"
JW_WEBVPN_COURSE_SELECT_URL = f"{JW_WEBVPN_BASE_URL}/student/for-std/course-select"
COURSE_SELECTION_API_BASE_URL = f"{JW_WEBVPN_BASE_URL}/course-selection-api/api/v1/student/course-select"
JW_INTERNAL_HOST = "bkjwtest.guet.edu.cn"
WEBVPN_HOST = "v.guet.edu.cn"
CAMPUS_PROBE_URL = "https://iw.guet.edu.cn/"
JW_DIRECT_BASE_URL = "https://bkjwtest.guet.edu.cn"
JW_DIRECT_LOGIN_URL = f"{JW_DIRECT_BASE_URL}/student/sso/login"
JW_PUBLIC_LOGIN_URL = f"{JW_DIRECT_BASE_URL}/student/ldap/login"
JW_DIRECT_HOME_URL = f"{JW_DIRECT_BASE_URL}/student/home"
JW_DIRECT_COURSE_SELECT_URL = f"{JW_DIRECT_BASE_URL}/student/for-std/course-select"
DIRECT_COURSE_SELECTION_API_BASE_URL = (
    f"{JW_DIRECT_BASE_URL}/course-selection-api/api/v1/student/course-select"
)
SENSITIVE_KEY_PARTS = ("token", "ticket", "session", "cookie", "auth", "password", "secret", "jwt")
CAPTURE_RESOURCE_TYPES = {"xhr", "fetch"}


class CourseSelectionApiError(PluginServiceError):
    pass


@dataclass(frozen=True)
class SelectionTarget:
    course_name: str = ""
    course_code: str = ""
    teacher: str = ""

    @property
    def keywords(self) -> list[str]:
        return [item for item in (self.course_name, self.course_code, self.teacher) if item]


@dataclass(frozen=True)
class SelectionCandidate:
    index: int
    text: str


@dataclass(frozen=True)
class SelectionBatch:
    student_id: int
    turn_id: int
    label: str
    url: str
    is_open: bool = False
    start_at: str | None = None
    end_at: str | None = None

@dataclass(frozen=True)
class SelectionAccess:
    mode: str
    label: str
    login_url: str
    home_url: str
    course_select_url: str
    api_base_url: str



@dataclass(frozen=True)
class WebVpnSessionStatus:
    is_saved: bool
    message: str
    saved_at: str | None = None
    final_url: str | None = None
    is_valid: bool | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class CaptureFile:
    name: str
    json_path: Path
    markdown_path: Path | None
    updated_at: str
    request_count: int


@dataclass(frozen=True)
class SelectionInfoCache:
    updated_at: str | None
    title: str
    turn_id: int | None
    student_id: int | None
    courses: list[dict[str, Any]]
    sections: dict[str, list[dict[str, Any]]]
    active_view: str
    batches: list[dict[str, Any]]
    current_batch: dict[str, Any] | None


SELECTION_INFO_VIEWS: dict[str, str] = {
    "major-plan": "培养方案",
    "all": "全部课程",
    "retake": "重修选课",
    "selected": "已选课程",
}


ProgressCallback = Callable[[int, str], None]

