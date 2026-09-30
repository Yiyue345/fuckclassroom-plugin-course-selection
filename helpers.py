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

from .models import *

def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)


def _session_check(is_valid: bool, message: str, final_url: str | None = None, status: int | None = None) -> dict[str, Any]:
    return {
        "is_valid": is_valid,
        "message": message,
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "final_url": final_url,
        "status": status,
    }


def _extract_selection_bootstrap_token(content: str) -> str:
    normalized = content.replace("\\/", "/")
    match = re.search(
        r"https://bkjwtest\.guet\.edu\.cn/course-selection/\?token=([^'\"&<\s]+)",
        normalized,
    )
    return match.group(1) if match else ""


def _selection_token_expiry(token: str, now: float) -> float:
    raw_token = token.removeprefix("Bearer ").strip()
    parts = raw_token.split(".")
    if len(parts) != 3:
        return now + 120
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        expires_at = float(payload.get("exp"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return now + 120
    if expires_at > 10_000_000_000:
        expires_at /= 1000
    return expires_at if expires_at > now else now


def _selection_access(mode: str) -> SelectionAccess:
    if mode in {"campus", "public", "hy2"}:
        labels = {
            "campus": "校园网直连",
            "public": "公网直连",
            "hy2": "Hy2 代理",
        }
        return SelectionAccess(
            mode=mode,
            label=labels[mode],
            login_url=JW_PUBLIC_LOGIN_URL if mode == "public" else JW_DIRECT_LOGIN_URL,
            home_url=JW_DIRECT_HOME_URL,
            course_select_url=JW_DIRECT_COURSE_SELECT_URL,
            api_base_url=DIRECT_COURSE_SELECTION_API_BASE_URL,
        )
    return SelectionAccess(
        mode="webvpn",
        label="WebVPN",
        login_url=WEBVPN_URL,
        home_url=JW_WEBVPN_HOME_URL,
        course_select_url=JW_WEBVPN_COURSE_SELECT_URL,
        api_base_url=COURSE_SELECTION_API_BASE_URL,
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        fp: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _extract_selection_student_ids(payload: Any) -> list[int]:
    rows = _selection_api_rows(payload, "学生记录")
    student_ids: list[int] = []
    for item in rows:
        student_id = _safe_int(item.get("id")) if isinstance(item, dict) else _safe_int(item)
        if student_id is not None and student_id not in student_ids:
            student_ids.append(student_id)
    return student_ids


def _selection_batches_from_api(
    payload: Any,
    student_id: int,
    *,
    now: datetime | None = None,
) -> list[SelectionBatch]:
    batches: list[SelectionBatch] = []
    for item in _selection_api_rows(payload, "选课批次"):
        if not isinstance(item, dict):
            continue
        turn_id = _safe_int(item.get("id"))
        if turn_id is None:
            continue
        start_at, end_at = _selection_batch_time_range(item)
        batches.append(
            SelectionBatch(
                student_id=student_id,
                turn_id=turn_id,
                label=str(item.get("name") or f"选课批次 {turn_id}"),
                url=_selection_batch_url(student_id, turn_id),
                is_open=_selection_batch_is_open(
                    item.get("allowEnter"),
                    start_at,
                    end_at,
                    now=now,
                ),
                start_at=start_at,
                end_at=end_at,
            )
        )
    return batches


def _selection_batch_time_range(item: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("openDateTimeRange", "selectDateTimeRange", "dropDateTimeRange"):
        value = item.get(key)
        if not isinstance(value, dict):
            continue
        start_at = _selection_datetime_text(value.get("startDateTime"))
        end_at = _selection_datetime_text(value.get("endDateTime"))
        if start_at or end_at:
            return start_at, end_at

    for key in ("openDateTimeText", "selectDateTimeText", "dropDateTimeText"):
        value = str(item.get(key) or "").strip()
        if not value or "~" not in value:
            continue
        start_at, end_at = value.split("~", 1)
        return _selection_datetime_text(start_at), _selection_datetime_text(end_at)
    return None, None


def _selection_datetime_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M")


def _selection_batch_is_open(
    allow_enter: Any,
    start_at: Any,
    end_at: Any,
    *,
    now: datetime | None = None,
) -> bool:
    if not _selection_api_bool(allow_enter):
        return False

    current = now or datetime.now().astimezone()
    start = _selection_datetime_for_compare(start_at, current)
    end = _selection_datetime_for_compare(end_at, current)
    if start is not None and current < start:
        return False
    if end is not None and current >= end:
        return False
    return True


def _selection_datetime_for_compare(value: Any, reference: datetime) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None and reference.tzinfo is not None:
        return parsed.replace(tzinfo=reference.tzinfo)
    if parsed.tzinfo is not None and reference.tzinfo is None:
        return parsed.astimezone().replace(tzinfo=None)
    if parsed.tzinfo is not None and reference.tzinfo is not None:
        return parsed.astimezone(reference.tzinfo)
    return parsed


def _selection_api_rows(payload: Any, label: str) -> list[Any]:
    if not isinstance(payload, dict):
        raise CourseSelectionApiError(f"{label}接口返回格式不正确")
    if _selection_api_bool(payload.get("result")):
        message = str(payload.get("message") or payload.get("data") or f"{label}接口请求失败")
        raise CourseSelectionApiError(message)
    rows = payload.get("data")
    return rows if isinstance(rows, list) else []


def _selection_api_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _sort_selection_batches(batches: list[SelectionBatch]) -> list[SelectionBatch]:
    deduped: dict[tuple[int, int], SelectionBatch] = {}
    for batch in batches:
        deduped[(batch.student_id, batch.turn_id)] = batch
    return sorted(
        deduped.values(),
        key=lambda item: (item.is_open, item.turn_id),
        reverse=True,
    )


def _selection_batch_url(student_id: int, turn_id: int) -> str:
    return f"{JW_WEBVPN_BASE_URL}/course-selection/course-select/{student_id}/turn/{turn_id}/select"


def _extract_semester_id(payload: Any) -> int | None:
    if isinstance(payload, list):
        for item in payload:
            semester_id = _extract_semester_id(item)
            if semester_id is not None:
                return semester_id
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("semesterId", "semester_id", "semesterAssoc"):
        semester_id = _safe_int(payload.get(key))
        if semester_id is not None:
            return semester_id
    semester = payload.get("semester")
    if isinstance(semester, dict):
        semester_id = _safe_int(semester.get("id"))
        if semester_id is not None:
            return semester_id
    for value in payload.values():
        if isinstance(value, (dict, list)):
            semester_id = _extract_semester_id(value)
            if semester_id is not None:
                return semester_id
    return None


def _selection_route_ids(url: str) -> tuple[int, int] | None:
    match = re.search(r"course-select/(\d+)/turn/(\d+)/select", url)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _is_selection_token_error(exc: Exception) -> bool:
    return "未在选课页读取到课程选择 token" in str(exc)


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_lessons(payload: Any) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if _looks_like_lesson(value):
            lesson_id = str(value.get("id") or value.get("lessonId") or value.get("lessonAssoc") or "")
            if lesson_id and lesson_id not in seen:
                seen.add(lesson_id)
                lessons.append(value)
            return
        for key in ("lesson", "selectedLesson"):
            nested = value.get(key)
            if isinstance(nested, dict):
                walk(nested)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return lessons


def _extract_plan_courses(payload: Any) -> list[dict[str, Any]]:
    courses: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        plan_courses = value.get("planCourses")
        if isinstance(plan_courses, list):
            for item in plan_courses:
                if not isinstance(item, dict):
                    continue
                course_id = _safe_int(item.get("id"))
                course_code = str(item.get("code") or "").strip()
                key = (course_id, course_code)
                if key != (None, "") and key not in seen:
                    seen.add(key)
                    courses.append(item)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return courses


def _merge_lessons(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not primary or not fallback:
        return []

    plan_ids: set[int] = set()
    plan_codes: set[str] = set()
    for item in primary:
        course = item.get("course") if isinstance(item.get("course"), dict) else item
        course_id = _safe_int(course.get("id"))
        course_code = str(course.get("code") or "").strip()
        if course_id is not None:
            plan_ids.add(course_id)
        if course_code:
            plan_codes.add(course_code)

    matched: list[dict[str, Any]] = []
    for lesson in fallback:
        course = lesson.get("course")
        if not isinstance(course, dict):
            continue
        course_id = _safe_int(course.get("id"))
        course_code = str(course.get("code") or "").strip()
        if course_id in plan_ids or (course_code and course_code in plan_codes):
            matched.append(lesson)
    return matched


def _looks_like_lesson(value: dict[str, Any]) -> bool:
    course = value.get("course")
    teachers = value.get("teachers")
    return bool(value.get("id")) and isinstance(course, dict) and isinstance(teachers, list)


def _extract_selected_lesson_ids(payload: Any) -> set[int]:
    selected: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key in ("lessonId", "lessonAssoc", "lesson_id"):
            lesson_id = _safe_int(value.get(key))
            if lesson_id is not None:
                selected.add(lesson_id)
        if _looks_like_lesson(value):
            lesson_id = _safe_int(value.get("id"))
            if lesson_id is not None:
                selected.add(lesson_id)
        for key in ("lesson", "selectedLesson"):
            nested = value.get(key)
            if isinstance(nested, dict):
                walk(nested)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return selected


def _extract_count_map(payload: Any) -> dict[int, int]:
    result: dict[int, int] = {}
    data = payload.get("data") if isinstance(payload, dict) else payload

    def add_count(lesson_id: Any, count: Any) -> None:
        parsed_id = _safe_int(lesson_id)
        if isinstance(count, str):
            count = count.split("-", 1)[0].strip()
        parsed_count = _safe_int(count)
        if parsed_id is not None and parsed_count is not None:
            result[parsed_id] = parsed_count

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                add_count(key, value.get("count") or value.get("stdCount") or value.get("selectedCount") or value.get("num"))
                add_count(value.get("lessonId") or value.get("lessonAssoc"), value.get("count") or value.get("stdCount"))
            else:
                add_count(key, value)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                add_count(
                    item.get("lessonId") or item.get("lessonAssoc") or item.get("id"),
                    item.get("count") or item.get("stdCount") or item.get("selectedCount") or item.get("num"),
                )
    return result


def _local_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _selection_query_body(turn_id: int, student_id: int, semester_id: int | None) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "studentId": student_id,
        "semesterId": semester_id,
        "pageNo": 1,
        "pageSize": 1000,
        "courseNameOrCode": "",
        "lessonNameOrCode": "",
        "teacherNameOrCode": "",
        "week": "",
        "grade": "",
        "departmentId": "",
        "majorId": "",
        "adminclassId": "",
        "campusId": "",
        "openDepartmentId": "",
        "courseTypeId": "",
        "coursePropertyId": "",
        "canSelect": 1,
        "_canSelect": "可选",
        "creditGte": None,
        "creditLte": None,
        "hasCount": None,
        "ids": None,
        "substitutedCourseId": None,
        "courseSubstitutePoolId": None,
        "sortField": "course",
        "sortType": "ASC",
    }


def _selection_request_middle(lesson: dict[str, Any]) -> dict[str, Any]:
    lesson_id = _safe_int(lesson.get("id"))
    if lesson_id is None:
        raise CourseSelectionApiError("课程缺少教学班编号")
    schedule_group_assoc = _safe_int(lesson.get("scheduleGroupAssoc"))
    groups = lesson.get("scheduleGroups")
    groups = groups if isinstance(groups, list) else []
    if schedule_group_assoc is None and groups:
        defaults = [group for group in groups if isinstance(group, dict) and group.get("default")]
        candidate_groups = defaults or groups
        if len(candidate_groups) != 1:
            raise CourseSelectionApiError("该教学班有多个上课分组，请先在教务系统中选择分组")
        schedule_group_assoc = _safe_int(candidate_groups[0].get("id"))
    return {
        "lessonAssoc": lesson_id,
        "virtualCost": 0,
        "scheduleGroupAssoc": schedule_group_assoc,
    }


def _selection_request_id(payload: Any, label: str) -> int | str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, (int, str)) and str(data).strip():
        return data
    raise CourseSelectionApiError(_selection_response_message(payload) or f"{label}接口没有返回请求编号")


def _selection_response_data(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _selection_response_message(payload: Any) -> str:
    messages = _extract_action_messages(payload)
    return messages[0] if messages else ""


def _selection_predicate_message(payload: Any, lesson_id: int) -> str:
    data = _selection_response_data(payload)
    if data.get("success") is not True:
        return _selection_response_message(payload) or "平台条件校验未通过"
    result = data.get("result")
    if not isinstance(result, dict):
        return ""
    value = result.get(str(lesson_id), result.get(lesson_id))
    if not value:
        return ""
    if isinstance(value, str):
        aliases = {
            "ATTEND": "需要确认跟班听课",
            "CONFIRM_MIDTERM_RETAKE": "需要确认退课后的期中重修影响",
        }
        return aliases.get(value, value)
    return _selection_response_message(value) or str(value)


_PLAN_COURSE_CATEGORIES = ("必修", "实践", "专业限选", "专业任选", "通识")


def _plan_course_category(course: dict[str, Any]) -> str:
    course_property = str(course.get("course_property") or "").strip()
    course_type = str(course.get("course_type") or "").strip()
    for category in _PLAN_COURSE_CATEGORIES:
        if category in course_property:
            return category
    for category in _PLAN_COURSE_CATEGORIES:
        if category in course_type:
            return category
    return "其他"


def _group_plan_courses(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = {category: [] for category in (*_PLAN_COURSE_CATEGORIES, "其他")}
    for course in courses:
        grouped[_plan_course_category(course)].append(course)
    return [
        {"label": category, "courses": grouped[category]}
        for category in (*_PLAN_COURSE_CATEGORIES, "其他")
        if grouped[category]
    ]


def _normalize_lesson(lesson: dict[str, Any], count_map: dict[int, int], selected_ids: set[int]) -> dict[str, Any]:
    course = lesson.get("course") if isinstance(lesson.get("course"), dict) else {}
    campus = lesson.get("campus") if isinstance(lesson.get("campus"), dict) else {}
    course_type = lesson.get("courseType") if isinstance(lesson.get("courseType"), dict) else {}
    course_property = lesson.get("courseProperty") if isinstance(lesson.get("courseProperty"), dict) else {}
    open_department = lesson.get("openDepartment") if isinstance(lesson.get("openDepartment"), dict) else {}
    lesson_id = _safe_int(lesson.get("id"))
    teachers = [
        str(item.get("nameZh") or item.get("nameEn") or "").strip()
        for item in lesson.get("teachers", [])
        if isinstance(item, dict) and (item.get("nameZh") or item.get("nameEn"))
    ]
    time_text = _first_text(
        lesson.get("dateTimePlace"),
        [group.get("dateTimePlace") for group in lesson.get("scheduleGroups", []) if isinstance(group, dict)],
    )
    max_count = _safe_int(lesson.get("limitCount")) or _schedule_limit_count(lesson)
    selected_count = count_map.get(lesson_id) if lesson_id is not None else None
    return {
        "lesson_id": lesson_id,
        "course_name": str(course.get("nameZh") or lesson.get("courseName") or "").strip(),
        "course_code": str(course.get("code") or "").strip(),
        "lesson_name": str(lesson.get("nameZh") or "").strip(),
        "lesson_code": str(lesson.get("code") or "").strip(),
        "teachers": "、".join(teachers) if teachers else "未公布",
        "time": time_text or "未公布",
        "campus": str(campus.get("nameZh") or "").strip(),
        "credits": course.get("credits"),
        "course_type": str(course_type.get("nameZh") or "").strip(),
        "course_property": str(course_property.get("nameZh") or "").strip(),
        "department": str(open_department.get("nameZh") or "").strip(),
        "selected_count": selected_count,
        "max_count": max_count,
        "is_selected": lesson_id in selected_ids if lesson_id is not None else False,
    }


def _first_text(primary: Any, alternatives: list[Any]) -> str:
    for item in [primary, *alternatives]:
        if not isinstance(item, dict):
            continue
        text = item.get("textZh") or item.get("text") or item.get("textEn")
        if text:
            return str(text).replace("\n", "；")
    return ""


def _schedule_limit_count(lesson: dict[str, Any]) -> int | None:
    groups = lesson.get("scheduleGroups", [])
    if not isinstance(groups, list):
        return None
    for group in groups:
        if isinstance(group, dict):
            count = _safe_int(group.get("limitCount"))
            if count is not None:
                return count
    return None


def _first_teacher_name(teachers: str) -> str:
    cleaned = teachers.strip()
    if not cleaned or cleaned == "未公布":
        return ""
    return cleaned.split("、", 1)[0].strip()


def _summarize_action_responses(responses: list[dict[str, Any]]) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    messages: list[str] = []
    success_values: list[bool] = []
    for item in responses[:8]:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        messages.extend(_extract_action_messages(body))
        success = _extract_action_success(body)
        if success is not None:
            success_values.append(success)
        status_code = _safe_int(item.get("status"))
        if status_code is not None and status_code >= 400:
            success_values.append(False)
        detail = {
            "method": str(item.get("method") or ""),
            "url": str(item.get("url") or ""),
            "status": status_code,
        }
        body_messages = _extract_action_messages(body)
        if body_messages:
            detail["message"] = body_messages[0]
        details.append(detail)
    message = next((item for item in messages if item), "")
    status = ""
    if False in success_values:
        status = "失败"
    elif True in success_values:
        status = "成功"
    return {"status": status, "message": message, "responses": details}


def _extract_action_messages(value: Any) -> list[str]:
    messages: list[str] = []
    message_keys = {"message", "msg", "error", "errorMessage", "reason", "detail"}

    def collect_strings(item: Any, depth: int = 0) -> None:
        if depth > 4 or len(messages) >= 8:
            return
        if isinstance(item, str):
            cleaned = " ".join(item.split())
            if cleaned and 2 <= len(cleaned) <= 300:
                messages.append(cleaned)
            return
        if isinstance(item, list):
            for child in item:
                collect_strings(child, depth + 1)
            return
        if isinstance(item, dict):
            for child in item.values():
                collect_strings(child, depth + 1)

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 5 or len(messages) >= 8:
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        for key, child in item.items():
            if key in message_keys:
                collect_strings(child)
        for key, child in item.items():
            if key not in message_keys and isinstance(child, (dict, list)):
                walk(child, depth + 1)

    walk(value)
    deduped: list[str] = []
    for message in messages:
        if message not in deduped:
            deduped.append(message)
    return deduped


def _extract_action_success(value: Any) -> bool | None:
    if isinstance(value, list):
        for item in value:
            result = _extract_action_success(item)
            if result is not None:
                return result
        return None
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        lowered = str(key).lower()
        if lowered in {"success", "succeeded", "ok"} and isinstance(item, bool):
            return item
        if lowered in {"code", "status", "statuscode"}:
            if item in (0, "0", "success", "SUCCESS", "ok", "OK"):
                return True
            if isinstance(item, int) and item >= 400:
                return False
        if lowered in {"error", "errormessage"} and item:
            return False
    for item in value.values():
        result = _extract_action_success(item)
        if result is not None:
            return result
    return None


def _click_text_in_page(page: Any, text: str) -> bool:
    return bool(
        page.evaluate(
            """
            (text) => {
              const normalizedText = String(text).replace(/\\s+/g, "");
              const nodes = Array.from(document.querySelectorAll("button, a, [role=tab], [role=button], .el-button"));
              const node = nodes.find((item) => {
                const rect = item.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const content = (item.innerText || item.textContent || "").replace(/\\s+/g, "");
                return content.includes(normalizedText);
              });
              if (!node) return false;
              node.click();
              return true;
            }
            """,
            text,
        )
    )


def _sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query_items.append((key, "***" if _is_sensitive_key(key) else value))
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def _sanitize_mapping(mapping: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in mapping.items():
        result[str(key)] = "***" if _is_sensitive_key(str(key)) else str(value)
    return result


def _sanitize_body(body: str | None, max_length: int = 4000) -> Any:
    if not body:
        return None
    text = body[:max_length]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _sanitize_form_or_text(text)
    return _sanitize_value(data)


def _sanitize_form_or_text(text: str) -> Any:
    pairs = parse_qsl(text, keep_blank_values=True)
    if pairs:
        return {key: "***" if _is_sensitive_key(key) else value for key, value in pairs}
    return text


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "***" if _is_sensitive_key(str(key)) else _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _capture_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 选课接口抓取记录",
        "",
        f"- 抓取时间：{payload.get('captured_at', '')}",
        f"- 请求数量：{len(payload.get('requests', []))}",
        f"- 内部域名：{payload.get('jw_internal_host', '')}",
        "",
    ]
    for index, item in enumerate(payload.get("requests", []), start=1):
        if "capture_error" in item:
            lines.extend([f"## {index}. 抓取错误", "", str(item["capture_error"]), ""])
            continue
        lines.extend(
            [
                f"## {index}. {item.get('method')} {item.get('status')}",
                "",
                f"- 类型：{item.get('resource_type')}",
                f"- URL：`{item.get('url')}`",
                "",
            ]
        )
        if item.get("post_data") is not None:
            lines.extend(["请求体：", "", "```json", json.dumps(item["post_data"], ensure_ascii=False, indent=2), "```", ""])
        if item.get("response_body") is not None:
            lines.extend(
                ["响应体节选：", "", "```json", json.dumps(item["response_body"], ensure_ascii=False, indent=2), "```", ""]
            )
    return "\n".join(lines).strip() + "\n"


class _ManagedContext:
    def __init__(self, playwright_context_manager: Any, browser: Any, browser_context: Any, timeout_ms: int) -> None:
        self._playwright_context_manager = playwright_context_manager
        self._browser = browser
        self._browser_context = browser_context
        self._browser_context.set_default_timeout(timeout_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser_context, name)

    @property
    def browser(self) -> Any:
        return self._browser

    def storage_state(self, *args: Any, **kwargs: Any) -> Any:
        return self._browser_context.storage_state(*args, **kwargs)

    def new_page(self) -> Any:
        return self._browser_context.new_page()

    def close(self) -> None:
        try:
            try:
                self._browser_context.close()
            finally:
                self._browser.close()
        finally:
            self._playwright_context_manager.__exit__(None, None, None)

__all__ = ['_report', '_session_check', '_extract_selection_bootstrap_token', '_selection_token_expiry', '_selection_access', '_NoRedirectHandler', '_extract_selection_student_ids', '_selection_batches_from_api', '_selection_batch_time_range', '_selection_datetime_text', '_selection_batch_is_open', '_selection_api_rows', '_selection_api_bool', '_sort_selection_batches', '_selection_batch_url', '_extract_semester_id', '_selection_route_ids', '_is_selection_token_error', '_safe_int', '_extract_lessons', '_extract_plan_courses', '_merge_lessons', '_looks_like_lesson', '_extract_selected_lesson_ids', '_extract_count_map', '_local_now', '_selection_query_body', '_selection_request_middle', '_selection_request_id', '_selection_response_data', '_selection_response_message', '_selection_predicate_message', '_normalize_lesson', '_first_text', '_schedule_limit_count', '_first_teacher_name', '_summarize_action_responses', '_extract_action_messages', '_extract_action_success', '_click_text_in_page', '_sanitize_url', '_sanitize_mapping', '_sanitize_body', '_sanitize_form_or_text', '_sanitize_value', '_is_sensitive_key', '_capture_markdown', '_ManagedContext']
