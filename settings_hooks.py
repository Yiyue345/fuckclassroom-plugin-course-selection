from __future__ import annotations

from typing import Any

from fuckclassroom.core.plugins import PluginContext, ServiceContainer


def build_selection_network_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    hy2_proxy = services.get("hy2_proxy")
    return {
        "hy2_status": hy2_proxy.get_status().to_dict(),
    }


def apply_settings(
    context: PluginContext,
    settings: object,
    _form: dict[str, str],
) -> None:
    services = context.services
    course_selection = services.get("course_selection")
    auto_selection = services.get("auto_selection")
    hy2_proxy = services.get("hy2_proxy")

    course_selection.invalidate_access_mode()
    auto_selection.configure_intervals(
        retry_seconds=settings.auto_selection_retry_seconds,
        session_retry_seconds=settings.auto_selection_session_retry_seconds,
        capacity_retry_seconds=settings.auto_selection_capacity_retry_seconds,
    )
    if not settings.hy2_enabled:
        hy2_proxy.stop()


def clear_session(context: PluginContext) -> None:
    context.services.get("course_selection").clear_session()


__all__ = [
    "apply_settings",
    "build_selection_network_context",
    "clear_session",
]
