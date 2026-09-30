from __future__ import annotations

import asyncio

from fuckclassroom.core.plugins import PluginContext


async def startup(context: PluginContext) -> None:
    services = context.services
    course_selection = services.get("course_selection")
    auto_selection = services.get("auto_selection")

    def verify_selection_session_without_hy2():
        with course_selection.defer_hy2_start():
            return course_selection.verify_saved_session(
                timeout_ms=60_000,
                auto_relogin=True,
            )

    def refresh_selection_info_without_hy2():
        with course_selection.defer_hy2_start():
            return course_selection.refresh_selection_info_for_web(timeout_ms=60_000)

    async def verify_saved_session() -> None:
        try:
            result = await asyncio.to_thread(verify_selection_session_without_hy2)
        except Exception as exc:  # noqa: BLE001 - startup work must not stop the app.
            context.logger.warning("启动时WebVPN/教务会话检测失败：%s", exc)
            return

        context.logger.info("启动时WebVPN/教务会话状态：%s", result.message)
        if result.is_valid is True:
            try:
                await asyncio.to_thread(refresh_selection_info_without_hy2)
            except Exception as exc:  # noqa: BLE001 - startup refresh must not stop the app.
                context.logger.warning("启动时选课信息刷新失败：%s", exc)

    auto_selection.start()
    context.create_task(verify_saved_session())


def shutdown(context: PluginContext) -> None:
    context.services.get("auto_selection").stop()
    context.services.get("hy2_proxy").stop()


__all__ = ["shutdown", "startup"]
