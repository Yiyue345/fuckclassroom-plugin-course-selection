from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fuckclassroom.core.atomic import atomic_write_text
from .assistant import CourseSelectionAssistant
from .models import SelectionBatch


ACTIVE_STATUSES = {
    "scheduled",
    "waiting_batch",
    "checking_batch",
    "retrying",
    "waiting_session",
    "waiting_capacity",
    "checking_capacity",
    "attempting",
}
TERMINAL_STATUSES = {"selected", "full", "failed", "expired", "cancelled"}
STATUS_LABELS = {
    "scheduled": "等待开始",
    "waiting_batch": "等待选课批次",
    "checking_batch": "正在检查批次",
    "attempting": "正在选课",
    "retrying": "等待重试",
    "waiting_capacity": "等待空位",
    "checking_capacity": "正在监测人数",
    "waiting_session": "等待登录",
    "selected": "已选上",
    "full": "课程已满",
    "failed": "已停止",
    "expired": "批次已结束",
    "cancelled": "已取消",
}


@dataclass
class AutoSelectionJob:
    id: str
    lesson_id: int
    student_id: int
    turn_id: int
    batch_label: str
    batch_url: str
    start_at: str
    end_at: str
    course: dict[str, Any]
    status: str = "scheduled"
    attempts: int = 0
    message: str = "等待选课开始"
    capacity_checks: int = 0
    last_selected_count: int | None = None
    max_count: int | None = None
    monitor_capacity: bool = False
    next_attempt_at: str | None = None
    last_attempt_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_dict(self, *, display: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if display:
            payload["status_label"] = STATUS_LABELS.get(self.status, self.status)
            payload["is_active"] = self.is_active
        return payload


class AutoSelectionService:
    def __init__(
        self,
        assistant: CourseSelectionAssistant,
        path: Path,
        *,
        retry_seconds: float = 5.0,
        session_retry_seconds: float = 30.0,
        capacity_retry_seconds: float = 30.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.assistant = assistant
        self.path = path
        self.retry_seconds = max(5.0, retry_seconds)
        self.session_retry_seconds = max(15.0, session_retry_seconds)
        self.capacity_retry_seconds = max(15.0, capacity_retry_seconds)
        self._now_provider = now_provider or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jobs = self._load_jobs()

    def configure_intervals(
        self,
        *,
        retry_seconds: float,
        session_retry_seconds: float,
        capacity_retry_seconds: float,
    ) -> None:
        with self._lock:
            self.retry_seconds = max(5.0, retry_seconds)
            self.session_retry_seconds = max(15.0, session_retry_seconds)
            self.capacity_retry_seconds = max(15.0, capacity_retry_seconds)
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="auto-course-selection",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def add_job(
        self,
        lesson_id: int,
        course: dict[str, Any],
        batch: dict[str, Any] | None,
    ) -> tuple[AutoSelectionJob, bool]:
        now = self._now()
        if course.get("is_selected"):
            raise ValueError("这门课已经在已选课程中")

        waiting_for_batch = batch is None
        if waiting_for_batch:
            student_id = 0
            turn_id = 0
            start_at = ""
            end_at = ""
            start = None
            end = None
            batch_label = "等待正式选课批次"
            batch_url = ""
        else:
            student_id = _required_int(batch.get("student_id"), "选课批次缺少学生编号")
            turn_id = _required_int(batch.get("turn_id"), "选课批次缺少批次编号")
            start_at = str(batch.get("start_at") or "").strip()
            end_at = str(batch.get("end_at") or "").strip()
            start = _parse_time(start_at)
            end = _parse_time(end_at)
            if start is None or end is None:
                raise ValueError("批次开始或结束时间未公布，无法创建定时选课")
            if end <= now:
                raise ValueError("当前选课批次已经结束")
            batch_label = str(batch.get("label") or "当前选课批次")
            batch_url = str(batch.get("url") or "")

        with self._lock:
            duplicate = next(
                (
                    job
                    for job in self._jobs.values()
                    if job.lesson_id == lesson_id
                    and job.is_active
                    and (
                        waiting_for_batch
                        or job.status in {"waiting_batch", "checking_batch"}
                        or (job.student_id == student_id and job.turn_id == turn_id)
                    )
                ),
                None,
            )
            if duplicate:
                return duplicate, False

            now_text = now.isoformat(timespec="seconds")
            selected_count = _optional_int(course.get("selected_count"))
            max_count = _optional_int(course.get("max_count"))
            monitor_capacity = bool(
                max_count and selected_count is not None and selected_count >= max_count
            )
            waiting_to_start = start is not None and start > now
            if waiting_for_batch:
                status = "waiting_batch"
                message = "等待正式选课批次，程序会自动检查"
                next_attempt = now
            else:
                status = "scheduled" if waiting_to_start or not monitor_capacity else "waiting_capacity"
                message = (
                    "等待选课开始"
                    if waiting_to_start
                    else (
                        f"课程已满（{selected_count}/{max_count}），等待空位"
                        if monitor_capacity
                        else "已进入选课时间，等待提交"
                    )
                )
                next_attempt = start if waiting_to_start else now
            job = AutoSelectionJob(
                id=uuid4().hex,
                lesson_id=lesson_id,
                student_id=student_id,
                turn_id=turn_id,
                batch_label=batch_label,
                batch_url=batch_url,
                start_at=start_at,
                end_at=end_at,
                course=dict(course),
                status=status,
                message=message,
                last_selected_count=selected_count,
                max_count=max_count,
                monitor_capacity=monitor_capacity,
                next_attempt_at=next_attempt.isoformat(timespec="seconds"),
                created_at=now_text,
                updated_at=now_text,
            )
            self._jobs[job.id] = job
            self._save_jobs()
        self._wake.set()
        return job, True

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [job.to_dict(display=True) for job in jobs]

    def active_lesson_ids(self) -> set[int]:
        with self._lock:
            return {job.lesson_id for job in self._jobs.values() if job.is_active}

    def cancel(self, job_id: str) -> AutoSelectionJob:
        with self._lock:
            job = self._get_job(job_id)
            if job.status in TERMINAL_STATUSES and job.status != "failed":
                return job
            self._set_state(job, "cancelled", "用户已取消定时选课", next_attempt_at=None)
            self._save_jobs()
            return job

    def resume(self, job_id: str) -> AutoSelectionJob:
        with self._lock:
            job = self._get_job(job_id)
            if job.status not in {"cancelled", "failed"}:
                return job
            now = self._now()
            end = _parse_time(job.end_at)
            if not job.student_id or not job.turn_id:
                self._set_state(
                    job,
                    "waiting_batch",
                    "等待正式选课批次，程序会自动检查",
                    next_attempt_at=now,
                )
            elif end is None or end <= now:
                self._set_state(job, "expired", "选课批次已经结束", next_attempt_at=None)
            else:
                start = _parse_time(job.start_at) or now
                next_attempt = start if start > now else now
                monitor_now = job.monitor_capacity and start <= now
                self._set_state(
                    job,
                    "waiting_capacity" if monitor_now else "scheduled",
                    (
                        "等待选课开始"
                        if start > now
                        else (
                            "继续等待课程空位"
                            if monitor_now
                            else "等待重新提交"
                        )
                    ),
                    next_attempt_at=next_attempt,
                )
            self._save_jobs()
        self._wake.set()
        return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self._get_job(job_id)
            if job.is_active:
                raise ValueError("请先取消正在等待的定时选课")
            del self._jobs[job_id]
            self._save_jobs()

    def run_due_once(self) -> bool:
        now = self._now()
        with self._lock:
            changed = self._expire_jobs(now)
            due = next(
                (
                    job
                    for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
                    if job.status in {
                        "scheduled",
                        "waiting_batch",
                        "retrying",
                        "waiting_session",
                        "waiting_capacity",
                    }
                    and (_parse_time(job.next_attempt_at) or now) <= now
                ),
                None,
            )
            if due is None:
                if changed:
                    self._save_jobs()
                return False
            if due.status == "waiting_batch":
                due.status = "checking_batch"
                due.message = "正在检查正式选课批次"
                due.updated_at = now.isoformat(timespec="seconds")
                due.next_attempt_at = None
                self._save_jobs()
                pending_job_id = due.id
            else:
                pending_job_id = ""
            monitor_capacity = due.monitor_capacity
            if not pending_job_id:
                due.status = "checking_capacity" if monitor_capacity else "attempting"
                if monitor_capacity:
                    due.capacity_checks += 1
                    due.message = f"正在进行第 {due.capacity_checks} 次空位监测"
                    due.updated_at = now.isoformat(timespec="seconds")
                else:
                    due.attempts += 1
                    due.last_attempt_at = now.isoformat(timespec="seconds")
                    due.updated_at = due.last_attempt_at
                    due.message = f"正在发起第 {due.attempts} 次选课请求"
                due.next_attempt_at = None
                self._save_jobs()
            job_id = due.id
            course = dict(due.course)
            lesson_id = due.lesson_id
            batch = SelectionBatch(
                student_id=due.student_id,
                turn_id=due.turn_id,
                label=due.batch_label,
                url=due.batch_url,
                is_open=True,
                start_at=due.start_at,
                end_at=due.end_at,
            )

        if pending_job_id:
            try:
                batch_data = self.assistant.get_available_catalog_batch(timeout_ms=60_000)
                start = _parse_time(str(batch_data.get("start_at") or ""))
                end = _parse_time(str(batch_data.get("end_at") or ""))
                if start is None or end is None or end <= self._now():
                    raise ValueError("正式选课批次尚未开放")
            except Exception as exc:
                with self._lock:
                    job = self._jobs.get(pending_job_id)
                    if job is None or job.status != "checking_batch":
                        return True
                    self._set_state(
                        job,
                        "waiting_batch",
                        f"{str(exc).strip() or '暂未发现正式选课批次'}；稍后自动检查",
                        next_attempt_at=self._now() + timedelta(seconds=self.session_retry_seconds),
                    )
                    self._save_jobs()
                return True

            with self._lock:
                job = self._jobs.get(pending_job_id)
                if job is None or job.status != "checking_batch":
                    return True
                job.student_id = _required_int(batch_data.get("student_id"), "选课批次缺少学生编号")
                job.turn_id = _required_int(batch_data.get("turn_id"), "选课批次缺少批次编号")
                job.batch_label = str(batch_data.get("label") or "当前选课批次")
                job.batch_url = str(batch_data.get("url") or "")
                job.start_at = str(batch_data.get("start_at") or "")
                job.end_at = str(batch_data.get("end_at") or "")
                next_attempt = start if start > self._now() else self._now()
                self._set_state(
                    job,
                    "scheduled",
                    "已匹配正式选课批次，等待提交",
                    next_attempt_at=next_attempt,
                )
                self._save_jobs()
            return True

        if monitor_capacity:
            try:
                capacity = self.assistant.get_lesson_capacity_for_batch(
                    lesson_id,
                    batch,
                    course,
                    timeout_ms=60_000,
                )
            except Exception as exc:
                outcome, message, delay = _classify_error(
                    str(exc),
                    self.retry_seconds,
                    self.session_retry_seconds,
                    self.capacity_retry_seconds,
                )
                outcome, message, delay = self._recover_session_if_needed(
                    outcome,
                    message,
                    delay,
                    monitor_capacity=True,
                )
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.status != "checking_capacity":
                        return True
                    next_attempt = (
                        self._now() + timedelta(seconds=delay) if delay is not None else None
                    )
                    self._set_state(job, outcome, message, next_attempt_at=next_attempt)
                    self._save_jobs()
                return True

            selected_count = _optional_int(capacity.get("selected_count"))
            max_count = (
                _optional_int(capacity.get("max_count"))
                or _optional_int(course.get("max_count"))
            )
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status != "checking_capacity":
                    return True
                job.last_selected_count = selected_count
                job.max_count = max_count
                if (
                    max_count and selected_count is not None and selected_count >= max_count
                ):
                    next_attempt = self._now() + timedelta(seconds=self.capacity_retry_seconds)
                    self._set_state(
                        job,
                        "waiting_capacity",
                        f"课程仍满（{selected_count}/{max_count}），继续等待空位",
                        next_attempt_at=next_attempt,
                    )
                    self._save_jobs()
                    return True
                job.monitor_capacity = False
                monitor_capacity = False
                job.status = "attempting"
                job.attempts += 1
                job.last_attempt_at = self._now().isoformat(timespec="seconds")
                job.updated_at = job.last_attempt_at
                job.message = f"检测到空位，正在发起第 {job.attempts} 次选课请求"
                self._save_jobs()

        try:
            self.assistant.select_lesson_for_batch_once(
                lesson_id,
                batch,
                course,
                timeout_ms=60_000,
            )
        except Exception as exc:
            outcome, message, delay = _classify_error(
                str(exc),
                self.retry_seconds,
                self.session_retry_seconds,
                self.capacity_retry_seconds,
            )
            outcome, message, delay = self._recover_session_if_needed(
                outcome,
                message,
                delay,
                monitor_capacity=monitor_capacity,
            )
        else:
            outcome, message, delay = "selected", "选课成功，任务已停止", None

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "attempting":
                return True
            next_attempt = self._now() + timedelta(seconds=delay) if delay is not None else None
            if outcome == "waiting_capacity":
                job.monitor_capacity = True
            self._set_state(job, outcome, message, next_attempt_at=next_attempt)
            self._save_jobs()
        return True

    def _recover_session_if_needed(
        self,
        outcome: str,
        message: str,
        delay: float | None,
        *,
        monitor_capacity: bool,
    ) -> tuple[str, str, float | None]:
        if outcome != "waiting_session":
            return outcome, message, delay
        try:
            status = self.assistant.verify_saved_session(
                timeout_ms=15_000,
                auto_relogin=True,
            )
        except Exception as exc:
            return (
                "waiting_session",
                f"{message}；自动登录失败：{exc}",
                self.session_retry_seconds,
            )
        if status.is_valid is True:
            next_status = "waiting_capacity" if monitor_capacity else "retrying"
            next_action = "继续监测课程空位" if monitor_capacity else "继续选课"
            return (
                next_status,
                f"教务会话已自动恢复；{next_action}",
                self.retry_seconds,
            )
        reason = status.message or "自动登录未能恢复教务会话"
        return (
            "waiting_session",
            f"{message}；{reason}",
            self.session_retry_seconds,
        )

    def _worker(self) -> None:
        while not self._stop.is_set():
            processed = self.run_due_once()
            self._wake.wait(1.0 if processed else self._next_wait_seconds())
            self._wake.clear()

    def _next_wait_seconds(self) -> float:
        now = self._now()
        with self._lock:
            next_times = [
                parsed
                for job in self._jobs.values()
                if job.status in {"scheduled", "waiting_batch", "retrying", "waiting_session", "waiting_capacity"}
                if (parsed := _parse_time(job.next_attempt_at)) is not None
            ]
        if not next_times:
            return 3.0
        return max(0.05, min(3.0, (min(next_times) - now).total_seconds()))

    def _expire_jobs(self, now: datetime) -> bool:
        changed = False
        for job in self._jobs.values():
            end = _parse_time(job.end_at)
            if job.is_active and end is not None and end <= now:
                self._set_state(job, "expired", "选课批次已经结束，任务已停止", next_attempt_at=None)
                changed = True
        return changed

    def _set_state(
        self,
        job: AutoSelectionJob,
        status: str,
        message: str,
        *,
        next_attempt_at: datetime | None,
    ) -> None:
        job.status = status
        job.message = message
        job.next_attempt_at = next_attempt_at.isoformat(timespec="seconds") if next_attempt_at else None
        job.updated_at = self._now().isoformat(timespec="seconds")

    def _get_job(self, job_id: str) -> AutoSelectionJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _load_jobs(self) -> dict[str, AutoSelectionJob]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        rows = payload.get("jobs", []) if isinstance(payload, dict) else []
        result: dict[str, AutoSelectionJob] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                job = AutoSelectionJob(**{key: row[key] for key in AutoSelectionJob.__dataclass_fields__ if key in row})
            except (KeyError, TypeError, ValueError):
                continue
            job.max_count = job.max_count or _optional_int(job.course.get("max_count"))
            if job.last_selected_count is None:
                job.last_selected_count = _optional_int(job.course.get("selected_count"))
            if job.status == "checking_batch":
                job.status = "waiting_batch"
                job.message = "程序重新启动，继续等待正式选课批次"
                job.next_attempt_at = self._now().isoformat(timespec="seconds")
            elif job.status in {"full", "checking_capacity"}:
                job.monitor_capacity = True
                job.status = "waiting_capacity"
                job.message = "程序重新启动，继续等待课程空位"
                job.next_attempt_at = self._now().isoformat(timespec="seconds")
            elif job.status == "attempting":
                job.status = (
                    "waiting_capacity"
                    if job.monitor_capacity
                    else "retrying"
                )
                job.message = "程序重新启动，等待继续处理选课任务"
                job.next_attempt_at = self._now().isoformat(timespec="seconds")
            result[job.id] = job
        return result

    def _save_jobs(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(
                {"jobs": [job.to_dict() for job in self._jobs.values()]},
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _now(self) -> datetime:
        value = self._now_provider()
        return value if value.tzinfo is not None else value.astimezone()


def _required_int(value: Any, message: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _classify_error(
    message: str,
    retry_seconds: float,
    session_retry_seconds: float,
    capacity_retry_seconds: float,
) -> tuple[str, str, float | None]:
    normalized = message.strip() or "选课请求失败"
    if any(marker in normalized for marker in ("已经选中", "已经在已选", "已选课程")):
        return "selected", "课程已在已选列表中，任务已停止", None
    if any(marker in normalized for marker in ("已满", "满额", "容量不足", "无课余量", "人数已达上限")):
        return "waiting_capacity", f"{normalized}；将持续监测空位", capacity_retry_seconds
    if any(marker in normalized for marker in ("重新登录", "会话", "token", "授权", "Cookie", "cookie")):
        return "waiting_session", f"{normalized}；登录恢复后将自动重试", session_retry_seconds
    if any(marker in normalized for marker in ("冲突", "不满足", "不允许", "额外确认", "先修", "不在选课范围")):
        return "failed", normalized, None
    return "retrying", f"{normalized}；稍后自动重试", retry_seconds
