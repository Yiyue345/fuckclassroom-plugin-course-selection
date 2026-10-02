from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urlencode

from .helpers import _local_now, _safe_int
from .models import CourseSelectionApiError


_CATALOG_ENTRY_PATH = "/student/for-std/lesson-search"
_CATALOG_ASSEMBLE_FIELDS = ",".join(
    (
        "course.code",
        "minorCourse.nameZh",
        "courseType",
        "openDepartment",
        "teacherAssignmentList",
        "examMode",
        "campus",
        "teachLang",
        "roomType",
        "timeTableLayout",
        "crossBizTypes",
        "courseProperty",
    )
)
_CATALOG_SELECTS = {
    "semester": "semesters",
    "openDepartmentAssoc": "departments",
    "campusAssoc": "campuses",
    "courseTypeAssoc": "course_types",
    "examModeAssoc": "exam_modes",
    "compulsorys": "compulsory_options",
    "grade": "grades",
    "weekIndexs": "weekdays",
    "courseIndexs": "periods",
}
_TEXT_FILTERS = {
    "course_code": "courseCodeLike",
    "course_name": "courseNameZhLike",
    "lesson_code": "codeLike",
    "lesson_name": "nameZhLike",
    "room": "roomNameLike",
}
_ID_FILTERS = {
    "teacher_id": "teacherAssoc",
    "department_id": "openDepartmentAssocs",
    "campus_id": "campusAssoc",
    "course_type_id": "courseTypeAssoc",
    "exam_mode_id": "examModeAssoc",
}
_INTEGER_FILTERS = {
    "grade": ("grades", 2000, 2100),
    "week_start": ("scheduleWeekGte", 1, 60),
    "week_end": ("scheduleWeekLte", 1, 60),
    "weekday": ("weekIndexs", 1, 7),
    "max_count_min": ("limitCountGte", 0, 100_000),
    "max_count_max": ("limitCountLte", 0, 100_000),
    "selected_count_min": ("stdCountGte", 0, 100_000),
    "selected_count_max": ("stdCountLte", 0, 100_000),
}
_FLOAT_FILTERS = {
    "credits_min": ("creditsGte", 0.0, 100.0),
    "credits_max": ("creditsLte", 0.0, 100.0),
}


class _CatalogOptionsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options = {target: [] for target in _CATALOG_SELECTS.values()}
        self.default_semester = ""
        self._select = ""
        self._option: dict[str, Any] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            self._select = _CATALOG_SELECTS.get(str(attributes.get("id") or ""), "")
            return
        if tag == "option" and self._select:
            self._option = {
                "value": str(attributes.get("value") or "").strip(),
                "selected": "selected" in attributes,
            }
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None:
            value = self._option["value"]
            label = " ".join("".join(self._text).split())
            if value and label:
                self.options[self._select].append({"value": value, "label": label})
                if self._select == "semesters" and self._option["selected"]:
                    self.default_semester = value
            self._option = None
            self._text = []
            return
        if tag == "select":
            self._select = ""


class SelectionCatalogMixin:
    def query_whole_school_courses(
        self,
        filters: Mapping[str, Any],
        *,
        page: int = 1,
        page_size: int = 50,
        timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        self._prefer_saved_catalog_session()
        normalized_filters = dict(filters)
        return self._run_with_session_recovery(
            lambda: self._query_whole_school_courses_once(
                normalized_filters,
                page=page,
                page_size=page_size,
                timeout_ms=timeout_ms,
            ),
            timeout_ms=timeout_ms,
        )

    def query_course_catalog_teachers(
        self,
        term: str,
        *,
        timeout_ms: int = 30_000,
    ) -> list[dict[str, Any]]:
        self._prefer_saved_catalog_session()
        query = _catalog_text(term, "教师姓名", maximum=40)
        if len(query) < 2:
            return []
        return self._run_with_session_recovery(
            lambda: self._query_course_catalog_teachers_once(query, timeout_ms),
            timeout_ms=timeout_ms,
        )

    def _query_whole_school_courses_once(
        self,
        filters: Mapping[str, Any],
        *,
        page: int,
        page_size: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        current_page = _catalog_integer(page, "页码", 1, 100_000)
        rows_per_page = int(page_size)
        if rows_per_page not in {20, 50, 100}:
            raise CourseSelectionApiError("每页数量只能是 20、50 或 100")
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        opener, jar = self._selection_http_client()
        bootstrap = self._course_catalog_bootstrap(opener, timeout_seconds)
        semester_id = str(filters.get("semester_id") or bootstrap["default_semester"]).strip()
        semesters = {item["value"] for item in bootstrap["options"]["semesters"]}
        if not semester_id.isdigit() or (semesters and semester_id not in semesters):
            raise CourseSelectionApiError("全校开课查询学期无效，请刷新后重试")

        query = _catalog_query_params(filters, current_page, rows_per_page)
        query_url = (
            f'{bootstrap["base_url"]}/student/for-std/lesson-search/semester/'
            f'{semester_id}/search/{bootstrap["index_id"]}?{urlencode(query, doseq=True)}'
        )
        payload = self._request_course_catalog_json(
            opener,
            query_url,
            bootstrap["referer"],
            timeout_seconds,
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CourseSelectionApiError("全校开课查询没有返回课程列表")
        raw_page_info = payload.get("_page_") if isinstance(payload, dict) else None
        page_info = raw_page_info if isinstance(raw_page_info, dict) else {}
        courses = [_normalize_catalog_course(item) for item in rows if isinstance(item, dict)]
        selected_ids = {
            _safe_int(course.get("lesson_id"))
            for course in self.get_selection_info("selected").sections.get("selected", [])
            if isinstance(course, dict)
        }
        active_ids = {item for item in selected_ids if item is not None}
        for course in courses:
            course["is_selected"] = course.get("lesson_id") in active_ids
        self._remember_catalog_courses(courses)
        self._persist_selection_cookies(jar)
        return {
            "courses": courses,
            "pagination": {
                "page": _safe_int(page_info.get("currentPage")) or current_page,
                "page_size": _safe_int(page_info.get("rowsPerPage")) or rows_per_page,
                "total": _safe_int(page_info.get("totalRows")) or 0,
                "pages": _safe_int(page_info.get("totalPages")) or 0,
            },
            "options": bootstrap["options"],
            "semester_id": semester_id,
            "updated_at": _local_now(),
        }

    def get_cached_catalog_course(self, lesson_id: int) -> dict[str, Any]:
        course_id = _catalog_integer(lesson_id, "教学班编号", 1, 100_000_000)
        cache = getattr(self, "_course_catalog_course_cache", {})
        course = cache.get(course_id) if isinstance(cache, dict) else None
        if not isinstance(course, dict):
            raise ValueError("查询缓存中找不到该教学班，请先重新查询")
        return dict(course)

    def run_catalog_lesson_action_for_web(
        self,
        lesson_id: int,
        action: str,
        *,
        progress: Any = None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/catalog",
    ) -> str:
        if action not in {"select", "drop"}:
            raise ValueError("不支持的课程操作")
        course = self.get_cached_catalog_course(lesson_id)
        with self._selection_action_lock:
            return self._run_with_session_recovery(
                lambda: self._run_cached_lesson_action(
                    lesson_id=lesson_id,
                    action=action,
                    progress=progress,
                    timeout_ms=timeout_ms,
                    result_url=result_url,
                    course_override=course,
                ),
                progress=progress,
                timeout_ms=timeout_ms,
            )

    def get_available_catalog_batch(self, timeout_ms: int = 60_000) -> dict[str, Any]:
        self._prefer_saved_catalog_session()
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))

        def fetch() -> dict[str, Any]:
            opener, jar = self._selection_http_client()
            batches, _ = self._fetch_selection_batches(opener, timeout_seconds)
            self._persist_selection_cookies(jar)
            if not batches:
                raise CourseSelectionApiError("当前还没有正式选课批次")
            batch = batches[0]
            return {
                "student_id": batch.student_id,
                "turn_id": batch.turn_id,
                "label": batch.label,
                "url": batch.url,
                "is_open": batch.is_open,
                "start_at": batch.start_at,
                "end_at": batch.end_at,
            }

        return self._run_with_session_recovery(fetch, timeout_ms=timeout_ms)

    def _prefer_saved_catalog_session(self) -> None:
        if not self.storage_state_path.exists() or self._saved_access_mode() != "hy2":
            return
        with self._access_mode_lock:
            self._access_mode = "hy2"
            self._access_mode_checked_at = time.time()

    def _remember_catalog_courses(self, courses: list[dict[str, Any]]) -> None:
        cache = getattr(self, "_course_catalog_course_cache", None)
        if not isinstance(cache, dict):
            cache = {}
        for course in courses:
            lesson_id = _safe_int(course.get("lesson_id"))
            if lesson_id is not None:
                cache[lesson_id] = dict(course)
        if len(cache) > 2_000:
            cache = dict(list(cache.items())[-1_500:])
        self._course_catalog_course_cache = cache

    def _query_course_catalog_teachers_once(
        self,
        term: str,
        timeout_ms: int,
    ) -> list[dict[str, Any]]:
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 30.0))
        opener, jar = self._selection_http_client()
        bootstrap = self._course_catalog_bootstrap(opener, timeout_seconds)
        query = urlencode({"term": term, "teaching": "true", "zaiZhi": "true"})
        payload = self._request_course_catalog_json(
            opener,
            f'{bootstrap["base_url"]}/student/ws/teacher/query-by-term?{query}',
            bootstrap["referer"],
            timeout_seconds,
        )
        self._persist_selection_cookies(jar)
        if not isinstance(payload, list):
            return []
        teachers: list[dict[str, Any]] = []
        for item in payload[:30]:
            if not isinstance(item, dict):
                continue
            teacher_id = _safe_int(item.get("id"))
            if teacher_id is None:
                continue
            person = item.get("person") if isinstance(item.get("person"), dict) else {}
            department = item.get("department") if isinstance(item.get("department"), dict) else {}
            teachers.append(
                {
                    "id": teacher_id,
                    "name": str(person.get("nameZh") or "").strip(),
                    "code": str(item.get("code") or "").strip(),
                    "department": str(department.get("nameZh") or "").strip(),
                }
            )
        return teachers

    def _course_catalog_bootstrap(self, opener: Any, timeout_seconds: float) -> dict[str, Any]:
        access = self._selection_access()
        cached = getattr(self, "_course_catalog_bootstrap_cache", None)
        if (
            isinstance(cached, dict)
            and cached.get("mode") == access.mode
            and float(cached.get("expires_at") or 0) > time.time()
        ):
            return cached

        home_suffix = "/student/home"
        if not access.home_url.endswith(home_suffix):
            raise CourseSelectionApiError("无法确定全校开课查询地址")
        entry_url = f'{access.home_url[:-len(home_suffix)]}{_CATALOG_ENTRY_PATH}'
        content, _, final_url = self._request_selection_bytes(
            opener,
            entry_url,
            timeout_seconds=timeout_seconds,
            accept="text/html,application/xhtml+xml",
        )
        match = re.search(r"/student/for-std/lesson-search/index/(\d+)", final_url)
        if not match:
            raise CourseSelectionApiError("教务系统没有返回全校开课查询入口")
        base_url = final_url.split("/student/for-std/lesson-search", 1)[0]
        parser = _CatalogOptionsParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        department_query = urlencode({"bizTypeId": "", "permCode": ""})
        departments = self._request_course_catalog_json(
            opener,
            (
                f"{base_url}/student/ws/select-department/departments/"
                f"getAllByIsOpenCourse?{department_query}"
            ),
            final_url,
            timeout_seconds,
        )
        if isinstance(departments, list):
            parser.options["departments"] = [
                {
                    "value": str(item.get("id") or ""),
                    "label": " · ".join(
                        value
                        for value in (
                            str(item.get("code") or "").strip(),
                            str(item.get("name") or item.get("nameZh") or "").strip(),
                        )
                        if value
                    ),
                }
                for item in departments
                if isinstance(item, dict) and item.get("id")
            ]
        default_semester = parser.default_semester
        if not default_semester and parser.options["semesters"]:
            default_semester = parser.options["semesters"][0]["value"]
        if not default_semester:
            raise CourseSelectionApiError("全校开课查询没有返回可用学期")
        result = {
            "mode": access.mode,
            "base_url": base_url,
            "index_id": match.group(1),
            "referer": final_url,
            "default_semester": default_semester,
            "options": parser.options,
            "expires_at": time.time() + 300,
        }
        self._course_catalog_bootstrap_cache = result
        return result

    def _request_course_catalog_json(
        self,
        opener: Any,
        url: str,
        referer: str,
        timeout_seconds: float,
    ) -> Any:
        content, content_type, _ = self._request_selection_bytes(
            opener,
            url,
            timeout_seconds=timeout_seconds,
            accept="application/json, text/plain, */*",
            extra_headers={"Referer": referer},
        )
        if "json" not in content_type.lower() and content.lstrip().startswith(b"<"):
            raise CourseSelectionApiError("全校开课查询返回了登录页面，请重新登录")
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CourseSelectionApiError("全校开课查询返回的数据不是有效 JSON") from exc


def _catalog_query_params(
    filters: Mapping[str, Any],
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    params = [
        ("bizTypeAssoc", "2"),
        ("queryPage__", f"{page},{page_size}"),
        ("assembleFields", _CATALOG_ASSEMBLE_FIELDS),
    ]
    for name, target in _TEXT_FILTERS.items():
        value = _catalog_text(filters.get(name), name)
        if value:
            params.append((target, value))
    for name, target in _ID_FILTERS.items():
        value = filters.get(name)
        if value not in (None, ""):
            params.append((target, str(_catalog_integer(value, name, 1, 10_000_000))))
    for name, (target, minimum, maximum) in _INTEGER_FILTERS.items():
        value = filters.get(name)
        if value not in (None, ""):
            params.append((target, str(_catalog_integer(value, name, minimum, maximum))))
    for name, (target, minimum, maximum) in _FLOAT_FILTERS.items():
        value = filters.get(name)
        if value not in (None, ""):
            number = _catalog_float(value, name, minimum, maximum)
            params.append((target, f"{number:g}"))
    compulsory = str(filters.get("compulsory") or "").strip()
    if compulsory:
        if compulsory not in {"COMPULSORY", "ELECTIVE"}:
            raise CourseSelectionApiError("课程修读类型筛选无效")
        params.append(("compulsory", compulsory))
    raw_periods = filters.get("periods")
    periods = raw_periods if isinstance(raw_periods, (list, tuple)) else [raw_periods]
    for value in periods:
        if value not in (None, ""):
            params.append(("courseIndexs", str(_catalog_integer(value, "节次", 1, 12))))
    return params


def _catalog_text(value: Any, label: str, *, maximum: int = 100) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise CourseSelectionApiError(f"{label}筛选条件过长")
    return text


def _catalog_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CourseSelectionApiError(f"{label}必须是整数") from exc
    if number < minimum or number > maximum:
        raise CourseSelectionApiError(f"{label}超出允许范围")
    return number


def _catalog_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CourseSelectionApiError(f"{label}必须是数字") from exc
    if number < minimum or number > maximum:
        raise CourseSelectionApiError(f"{label}超出允许范围")
    return number


def _localized_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("nameZh") or value.get("name") or "").strip()
    return str(value or "").strip()


def _nested_text(value: Any, *path: str) -> str:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    if isinstance(current, dict):
        return str(current.get("textZh") or current.get("text") or "").strip()
    return str(current or "").strip()


def _normalize_catalog_course(item: Mapping[str, Any]) -> dict[str, Any]:
    course = item.get("course") if isinstance(item.get("course"), dict) else {}
    teachers: list[str] = []
    assignments = item.get("teacherAssignmentList")
    if isinstance(assignments, list):
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            person = assignment.get("person") if isinstance(assignment.get("person"), dict) else {}
            name = str(person.get("nameZh") or "").strip()
            if name and name not in teachers:
                teachers.append(name)
    schedule = _nested_text(item.get("scheduleText"), "dateTimePlaceText")
    if not schedule:
        schedule = _nested_text(item.get("scheduleText"), "dateTimePlacePersonText")
    campus_match = re.search(r"([^\s;，,]{2,12}校区)", schedule)
    selected_count = _safe_int(item.get("stdCount"))
    max_count = _safe_int(item.get("limitCount"))
    return {
        "lesson_id": _safe_int(item.get("id")),
        "course_name": _localized_name(course),
        "course_code": str(course.get("code") or "").strip(),
        "lesson_name": str(item.get("nameZh") or "").strip(),
        "lesson_code": str(item.get("code") or "").strip(),
        "credits": course.get("credits"),
        "teachers": "、".join(teachers),
        "department": _localized_name(item.get("openDepartment")),
        "course_type": _localized_name(item.get("courseType")),
        "course_property": _localized_name(item.get("courseProperty")),
        "exam_mode": _localized_name(item.get("examMode")),
        "teach_language": _localized_name(item.get("teachLang")),
        "time": schedule,
        "campus": campus_match.group(1) if campus_match else "",
        "target_classes": str(item.get("nameZh") or "").strip(),
        "selected_count": selected_count,
        "max_count": max_count,
        "available_count": (
            max(0, max_count - selected_count)
            if max_count is not None and selected_count is not None
            else None
        ),
        "is_full": bool(
            max_count is not None
            and selected_count is not None
            and max_count > 0
            and selected_count >= max_count
        ),
        "total_periods": (
            item.get("requiredPeriodInfo", {}).get("total")
            if isinstance(item.get("requiredPeriodInfo"), dict)
            else None
        ),
        "remark": str(item.get("remark") or "").strip(),
    }
