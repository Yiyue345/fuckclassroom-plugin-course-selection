from __future__ import annotations

from pathlib import Path

from fuckclassroom.core.plugins import AccountPanel, NavigationGroup, NavigationItem, PluginContext, PluginSpec, SettingsPanel, UIAsset


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)

async def startup(context: PluginContext):
    from .services import startup as worker_startup
    await worker_startup(context)
    from .lifecycle import startup as hook
    return await hook(context)

async def shutdown(context: PluginContext):
    from .lifecycle import shutdown as lifecycle_shutdown
    lifecycle_shutdown(context)
    from .services import shutdown as worker_shutdown
    await worker_shutdown(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_account_context(services, request):
    from .accounts import build_account_context as build
    return build(services, request)


def build_selection_network_context(services, request):
    from .settings_hooks import build_selection_network_context as build
    return build(services, request)


def apply_settings(context: PluginContext, settings, form):
    from .settings_hooks import apply_settings as apply
    return apply(context, settings, form)


def clear_session(context: PluginContext):
    from .settings_hooks import clear_session as clear
    return clear(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="course_selection",
        ui_assets=(
            UIAsset("selection_display.js?v=20260930-2", pages=("selection_info", "selection_selected", "selection_catalog")),
            UIAsset("selection_ui.js?v=20260927-2", pages=("selection_info", "selection_selected", "selection_auto", "selection_catalog")),
            UIAsset("selection_catalog.js?v=20260930-1", pages=("selection_catalog",)),
        ),
        name="本科选课",
        order=20,
        requires=("core_ui",),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        shutdown=shutdown,
        settings_saved=apply_settings,
        clear_session=clear_session,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        stylesheets=("/plugins/course_selection/static/selection.css?v=20260930-1",),
        navigation_groups=(
            NavigationGroup(
                key="selection",
                label="本科选课",
                aria_label="本科选课导航",
                system="selection",
                order=20,
                status_template="selection_nav_status.html",
                items=(
                    NavigationItem(key="selection_info", label="选课中心", href="/selection/info", icon="check-square", active_keys=("selection_info",)),
                    NavigationItem(key="selection_catalog", label="全校开课", href="/selection/catalog", icon="book-open", active_keys=("selection_catalog",)),
                    NavigationItem(key="selection_selected", label="已选课程", href="/selection/info/selected", icon="list-checks", active_keys=("selection_selected",), badge_template="selection_badge_selected.html"),
                    NavigationItem(key="selection_auto", label="定时选课", href="/selection/auto", icon="clock-3", active_keys=("selection_auto",)),
                    NavigationItem(key="selection_captures", label="接口记录", href="/selection/captures", icon="activity", active_keys=("selection_captures",)),
                ),
            ),
        ),
        account_panels=(
            AccountPanel(
                key="course_selection",
                template="selection/account_panel.html",
                order=20,
                context_factory=build_account_context,
                shortcut_label="本科选课",
                shortcut_href="/selection/info",
                shortcut_icon="check-square",
                shortcut_primary=True,
            ),
        ),
        settings_panels=(
            SettingsPanel(key="auto-selection", label="定时选课", template="selection_settings_auto.html", order=30),
            SettingsPanel(
                key="selection-network",
                label="教务访问",
                template="selection_settings_network.html",
                order=35,
                checkbox_fields=("hy2_enabled",),
                context_factory=build_selection_network_context,
            ),
        ),
        sidebar_templates=("selection_sidebar_session.html",),
        topbar_status_templates=("selection_topbar_status.html",),
        system_labels=(("selection", "本科选课"),),
    )


__all__ = ["build_plugin"]
