from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fuckclassroom.course_selection import (
    CourseSelectionApiError,
    CourseSelectionAssistant,
    SELECTION_INFO_VIEWS,
    SelectionTarget,
)
from fuckclassroom.course_selection.helpers import _group_plan_courses


def _resolve_catalog_queue_batch(
    batches: list[dict[str, object]],
    batch_key: str,
) -> dict[str, object] | None:
    if batch_key == "pending":
        return None
    try:
        student_id_text, turn_id_text = batch_key.split(":", 1)
        student_id = int(student_id_text)
        turn_id = int(turn_id_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("请选择有效的定时选课批次") from exc

    batch = next(
        (
            item
            for item in batches
            if str(item.get("student_id") or "") == str(student_id)
            and str(item.get("turn_id") or "") == str(turn_id)
        ),
        None,
    )
    if batch is None:
        raise ValueError("所选定时选课批次已不可用，请刷新后重试")
    return dict(batch)


def _login_and_refresh_selection_info(
    course_selection: CourseSelectionAssistant,
    progress: Callable[[int, str], None],
) -> str:
    def login_progress(value: int, message: str) -> None:
        progress(round(value * 0.7), message)

    def refresh_progress(value: int, message: str) -> None:
        progress(70 + round(value * 0.3), message)

    result_url = course_selection.login_for_web(
        progress=login_progress,
        force_interactive=True,
    )
    return course_selection.refresh_selection_info_for_web(
        progress=refresh_progress,
        result_url=result_url,
    )


def _run_for_result(operation, result_url: str) -> str:
    operation()
    return result_url


def register_routes(
    router: APIRouter,
    *,
    templates,
    course_selection,
    auto_selection,
    settings_store,
    task_manager,
    hy2_proxy,
    task_started_response,
) -> None:
    @router.post("/settings/hy2/build")
    async def build_hy2_proxy(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        updated = settings_store.update_from_form(form)
        auto_selection.configure_intervals(
            retry_seconds=updated.auto_selection_retry_seconds,
            session_retry_seconds=updated.auto_selection_session_retry_seconds,
            capacity_retry_seconds=updated.auto_selection_capacity_retry_seconds,
        )
        course_selection.invalidate_access_mode()
        hy2_proxy.stop()
        task = task_manager.start("构建 Hy2 代理", hy2_proxy.build_runner)
        return task_started_response(request, task, "/settings?hy2_built=1")

    @router.get("/selection")
    def selection() -> RedirectResponse:
        return RedirectResponse("/accounts#selection-session", status_code=307)

    @router.get("/selection/catalog", response_class=HTMLResponse)
    def selection_catalog(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection/catalog.html",
            {
                "session": course_selection.get_session_status(),
                "catalog_targets": settings_store.load().catalog_lesson_name_targets,
            },
        )

    @router.get("/api/selection/catalog")
    def selection_catalog_api(request: Request) -> JSONResponse:
        allowed = {
            "semester_id",
            "course_code",
            "course_name",
            "lesson_code",
            "lesson_name",
            "room",
            "teacher_id",
            "department_id",
            "campus_id",
            "course_type_id",
            "exam_mode_id",
            "grade",
            "week_start",
            "week_end",
            "weekday",
            "max_count_min",
            "max_count_max",
            "selected_count_min",
            "selected_count_max",
            "credits_min",
            "credits_max",
            "compulsory",
        }
        filters = {
            key: request.query_params.get(key, "")
            for key in allowed
            if request.query_params.get(key, "") != ""
        }
        filters["periods"] = request.query_params.getlist("periods")
        try:
            page = int(request.query_params.get("page", "1"))
            page_size = int(request.query_params.get("page_size", "50"))
            targets = settings_store.load().catalog_lesson_name_targets
            targets_only = request.query_params.get("targets_only") == "1" and bool(targets)
            if targets_only and targets:
                merged: dict[int, dict[str, object]] = {}
                first_result = None
                for target in targets:
                    target_filters = dict(filters)
                    target_filters["lesson_name"] = target
                    result = course_selection.query_whole_school_courses(
                        target_filters,
                        page=1,
                        page_size=100,
                    )
                    if first_result is None:
                        first_result = result
                    for course in result["courses"]:
                        lesson_id = course.get("lesson_id")
                        if isinstance(lesson_id, int):
                            merged[lesson_id] = course
                result = first_result or course_selection.query_whole_school_courses(
                    filters,
                    page=1,
                    page_size=20,
                )
                result["courses"] = list(merged.values())
                total = len(result["courses"])
                result["pagination"] = {
                    "page": 1,
                    "page_size": total or 1,
                    "total": total,
                    "pages": 1,
                }
            else:
                result = course_selection.query_whole_school_courses(
                    filters,
                    page=page,
                    page_size=page_size,
                )
            queued_ids = auto_selection.active_lesson_ids()
            for course in result["courses"]:
                lesson_name = str(course.get("lesson_name") or "").casefold()
                course["matched_targets"] = [
                    target for target in targets if target.casefold() in lesson_name
                ]
                course["is_queued"] = course.get("lesson_id") in queued_ids
            selection_info = course_selection.get_selection_info("all")
            result["selection_batches"] = selection_info.batches
            result["targets"] = list(targets)
            result["targets_only"] = targets_only
            return JSONResponse(result)
        except (CourseSelectionApiError, RuntimeError, TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @router.get("/api/selection/catalog/teachers")
    def selection_catalog_teachers_api(q: str = "") -> JSONResponse:
        try:
            return JSONResponse(
                {"teachers": course_selection.query_course_catalog_teachers(q)}
            )
        except (CourseSelectionApiError, RuntimeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @router.post("/selection/catalog/action")
    async def selection_catalog_action(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        action = (parsed.get("action") or [""])[-1]
        try:
            lesson_id = int((parsed.get("lesson_id") or [""])[-1])
            course_selection.get_cached_catalog_course(lesson_id)
            if action not in {"select", "drop"}:
                raise ValueError("课程操作无效")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        label = "退课" if action == "drop" else "选课"
        task = task_manager.start(
            f"全校开课{label}",
            lambda progress: course_selection.run_catalog_lesson_action_for_web(
                lesson_id,
                action,
                progress=progress,
            ),
        )
        return task_started_response(request, task, "/selection/catalog")

    @router.post("/selection/catalog/queue")
    async def selection_catalog_queue(request: Request) -> JSONResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        try:
            lesson_id = int((parsed.get("lesson_id") or [""])[-1])
            course = course_selection.get_cached_catalog_course(lesson_id)
            batch_key = (parsed.get("batch_key") or [""])[-1]
            batches = course_selection.get_selection_info("all").batches
            batch = _resolve_catalog_queue_batch(batches, batch_key)
            job, created = auto_selection.add_job(lesson_id, course, batch)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "job": job.to_dict(display=True),
                "message": "已加入定时队列" if created else "这门课已经在定时队列中",
            }
        )

    @router.get("/selection/info", response_class=HTMLResponse)
    def selection_info(request: Request) -> HTMLResponse:
        return _selection_info_response(request, "major-plan")

    @router.get("/selection/info/{view}", response_class=HTMLResponse)
    def selection_info_view(request: Request, view: str) -> HTMLResponse:
        if view not in SELECTION_INFO_VIEWS:
            raise HTTPException(status_code=404, detail="选课信息页面不存在")
        return _selection_info_response(request, view)

    def _selection_info_response(request: Request, view: str) -> HTMLResponse:
        info = course_selection.get_selection_info(view)
        course_sections = (
            _group_plan_courses(info.courses)
            if info.active_view == "major-plan"
            else [{"label": "", "courses": info.courses}]
        )
        return templates.TemplateResponse(
            request,
            "selection/info.html",
            {
                "session": course_selection.get_session_status(),
                "info": info,
                "course_sections": course_sections,
                "views": SELECTION_INFO_VIEWS,
                "last_action": course_selection.pop_selection_action_result(),
                "select_error": request.query_params.get("select_error"),
                "auto_lesson_ids": auto_selection.active_lesson_ids(),
            },
        )

    @router.get("/selection/auto", response_class=HTMLResponse)
    def selection_auto_jobs(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection/auto.html",
            {
                "session": course_selection.get_session_status(),
                "jobs": auto_selection.list_jobs(),
                "select_error": request.query_params.get("select_error"),
            },
        )

    @router.get("/api/selection/auto/jobs")
    def selection_auto_jobs_api() -> JSONResponse:
        return JSONResponse({"jobs": auto_selection.list_jobs()})

    @router.post("/selection/auto/add")
    async def add_selection_auto_job(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
            info = course_selection.get_selection_info("all")
            batch = info.current_batch
            course = next(
                (
                    item
                    for courses in info.sections.values()
                    for item in courses
                    if isinstance(item, dict) and item.get("lesson_id") == lesson_id
                ),
                None,
            )
            if batch is None:
                raise ValueError("尚未缓存选课批次，请先刷新课程")
            if course is None:
                raise ValueError("缓存中找不到这门课，请先刷新课程")
            job, created = auto_selection.add_job(lesson_id, course, batch)
        except (TypeError, ValueError) as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc)}, status_code=400)
            return RedirectResponse(f"/selection/info?select_error={quote(str(exc))}", status_code=303)

        message = "已加入定时选课" if created else "这门课已经在定时队列中"
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"job": job.to_dict(display=True), "message": message})
        return RedirectResponse("/selection/auto", status_code=303)

    @router.post("/selection/auto/{job_id}/cancel")
    def cancel_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        return RedirectResponse("/selection/auto", status_code=303)

    @router.post("/selection/auto/{job_id}/resume")
    def resume_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.resume(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        return RedirectResponse("/selection/auto", status_code=303)

    @router.post("/selection/auto/{job_id}/delete")
    def delete_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.delete(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        except ValueError as exc:
            return RedirectResponse(f"/selection/auto?select_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/selection/auto", status_code=303)


    @router.post("/selection/info/refresh")
    async def refresh_selection_info(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["major-plan"])[-1]
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        batch_key = (parsed.get("batch_key") or [""])[-1].strip()
        student_id = None
        turn_id = None
        if batch_key:
            try:
                raw_student_id, raw_turn_id = batch_key.split(":", 1)
                student_id = int(raw_student_id)
                turn_id = int(raw_turn_id)
            except (TypeError, ValueError):
                detail = "选课批次无效，请重新选择"
                if "application/json" in request.headers.get("accept", ""):
                    return JSONResponse({"detail": detail}, status_code=400)
                return RedirectResponse(f"{result_url}?select_error={quote(detail)}", status_code=303)
        task = task_manager.start(
            "切换选课批次" if batch_key else "刷新选课信息",
            lambda progress: course_selection.refresh_selection_info_for_web(
                progress=progress,
                result_url=result_url,
                student_id=student_id,
                turn_id=turn_id,
            ),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "task_id": task.id,
                    "status_url": f"/api/tasks/{task.id}",
                    "result_url": result_url,
                }
            )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @router.get("/api/selection/counts")
    def live_selection_counts() -> JSONResponse:
        try:
            return JSONResponse(course_selection.get_live_selection_counts())
        except CourseSelectionApiError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @router.get("/api/selection/action-result")
    def selection_action_result() -> JSONResponse:
        return JSONResponse(course_selection.pop_selection_action_result() or {})

    @router.post("/selection/info/select")
    async def select_cached_lesson(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["major-plan"])[-1]
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
        except ValueError:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "课程编号无效"}, status_code=400)
            return RedirectResponse(f"/selection/info?select_error={quote('课程编号无效')}", status_code=303)
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        task = task_manager.start(
            "选择缓存课程",
            lambda progress: course_selection.select_cached_lesson_for_web(lesson_id, progress=progress, result_url=result_url),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "task_id": task.id,
                "status_url": f"/api/tasks/{task.id}",
                "result_url": result_url,
            })
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @router.post("/selection/info/drop")
    async def drop_cached_lesson(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["selected"])[-1]
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
        except ValueError:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "课程编号无效"}, status_code=400)
            return RedirectResponse(f"/selection/info/selected?select_error={quote('课程编号无效')}", status_code=303)
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        task = task_manager.start(
            "退选缓存课程",
            lambda progress: course_selection.drop_cached_lesson_for_web(lesson_id, progress=progress, result_url=result_url),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "task_id": task.id,
                "status_url": f"/api/tasks/{task.id}",
                "result_url": result_url,
            })
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @router.post("/selection/login")
    def start_webvpn_login(request: Request) -> Response:
        task = task_manager.start(
            "本科教务登录",
            lambda progress: _run_for_result(
                lambda: _login_and_refresh_selection_info(course_selection, progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @router.post("/selection/open")
    def open_selection_home(request: Request) -> Response:
        task = task_manager.start(
            "打开本科教务",
            lambda progress: _run_for_result(
                lambda: course_selection.open_home_for_web(progress=progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @router.post("/selection/open-course-select")
    def open_course_select(request: Request) -> Response:
        task = task_manager.start(
            "打开选课入口",
            lambda progress: _run_for_result(
                lambda: course_selection.open_course_select_for_web(progress=progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @router.post("/selection/logout")
    def selection_logout() -> RedirectResponse:
        course_selection.clear_session()
        return RedirectResponse("/accounts#selection-session", status_code=303)

    @router.post("/selection/capture")
    async def capture_selection_interfaces(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        raw_duration = (parsed.get("duration_seconds") or ["180"])[-1]
        try:
            duration_seconds = max(30, min(900, int(raw_duration)))
        except ValueError:
            duration_seconds = 180
        task = task_manager.start(
            "抓取选课接口",
            lambda progress: _run_for_result(
                lambda: course_selection.capture_interfaces_for_web(
                    progress=progress,
                    duration_seconds=duration_seconds,
                ),
                "/selection/captures",
            ),
        )
        return task_started_response(request, task, "/selection/captures")

    @router.get("/selection/captures", response_class=HTMLResponse)
    def selection_captures(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection/captures.html",
            {
                "session": course_selection.get_session_status(),
                "captures": course_selection.list_captures(),
            },
        )

    @router.get("/selection/captures/{name}", response_class=HTMLResponse)
    def selection_capture_detail(request: Request, name: str) -> HTMLResponse:
        try:
            content = course_selection.read_capture_markdown(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="抓取记录不存在") from exc
        return templates.TemplateResponse(
            request,
            "selection/capture_detail.html",
            {
                "session": course_selection.get_session_status(),
                "name": name,
                "content": content,
            },
        )

    @router.post("/selection/highlight")
    async def highlight_selection_course(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        target = SelectionTarget(
            course_name=(parsed.get("name") or [""])[-1].strip(),
            course_code=(parsed.get("code") or [""])[-1].strip(),
            teacher=(parsed.get("teacher") or [""])[-1].strip(),
        )
        task = task_manager.start(
            "定位选课课程",
            lambda progress: course_selection.highlight_for_web(target, progress=progress),
        )
        return task_started_response(request, task, "/selection")

    @router.post("/selection/select-once")
    async def select_course_once(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        if (parsed.get("confirm_select") or [""])[-1] != "YES":
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "需要先确认只点击一次唯一匹配课程"}, status_code=400)
            return RedirectResponse(f"/selection?select_error={quote('需要先确认只点击一次唯一匹配课程')}", status_code=303)
        target = SelectionTarget(
            course_name=(parsed.get("name") or [""])[-1].strip(),
            course_code=(parsed.get("code") or [""])[-1].strip(),
            teacher=(parsed.get("teacher") or [""])[-1].strip(),
        )
        task = task_manager.start(
            "执行一次选课",
            lambda progress: course_selection.select_once_for_web(target, progress=progress),
        )
        return task_started_response(request, task, "/selection")




def build_router(context) -> APIRouter:
    from fuckclassroom.web.responses import task_started_response

    services = context.services
    router = APIRouter()
    register_routes(
        router,
        templates=services.get("templates"),
        course_selection=services.get("course_selection"),
        auto_selection=services.get("auto_selection"),
        settings_store=services.get("settings_store"),
        task_manager=services.get("task_manager"),
        hy2_proxy=services.get("hy2_proxy"),
        task_started_response=task_started_response,
    )
    return router


__all__ = ["build_router", "register_routes"]
