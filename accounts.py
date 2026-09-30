from __future__ import annotations

from typing import Any

from fuckclassroom.core.plugins import ServiceContainer


def build_account_context(
    services: ServiceContainer,
    request: Any,
) -> dict[str, object]:
    course_selection = services.get("course_selection")
    error = request.query_params.get("select_error")
    return {
        "session": course_selection.get_session_status(),
        "access": course_selection.get_access_info(),
        "credentials": course_selection.get_credential_status(),
        "notices": (
            ({"kind": "error", "icon": "circle-alert", "message": error},)
            if error
            else ()
        ),
    }


__all__ = ["build_account_context"]
