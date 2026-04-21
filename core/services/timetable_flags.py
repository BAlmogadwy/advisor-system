"""Planner-stack flag helpers — single source of truth.

PR6 introduced ``is_stage_telemetry_enabled``, PR7 ``is_async_planner_enabled``,
PR8 ``is_async_job_ui_enabled`` / ``is_async_job_ui_effective``. Each
originally lived in its own service module. This module consolidates
them so callers have one import path. The original modules keep
back-compat re-exports (PR9 does not rewrite consumer call sites).

Every helper is a thin ``bool(getattr(settings, ..., False))`` wrapper —
no caching, no env parsing, no logic. Settings defaults are defined
in ``config/settings.py`` and overridable via env vars.
"""

from __future__ import annotations

from django.conf import settings

STAGE_TELEMETRY_SETTING = "TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED"
ASYNC_PLANNER_SETTING = "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED"
ASYNC_JOB_UI_SETTING = "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED"


def is_stage_telemetry_enabled() -> bool:
    return bool(getattr(settings, STAGE_TELEMETRY_SETTING, False))


def is_async_planner_enabled() -> bool:
    return bool(getattr(settings, ASYNC_PLANNER_SETTING, False))


def is_async_job_ui_enabled() -> bool:
    return bool(getattr(settings, ASYNC_JOB_UI_SETTING, False))


def is_async_job_ui_effective() -> bool:
    """True only when both PR7 (backend) and PR8 (UI) flags are on.

    PR8 card hides when PR7 is off — no dead controls on the workspace
    page.
    """
    return is_async_planner_enabled() and is_async_job_ui_enabled()


__all__ = [
    "ASYNC_JOB_UI_SETTING",
    "ASYNC_PLANNER_SETTING",
    "STAGE_TELEMETRY_SETTING",
    "is_async_job_ui_effective",
    "is_async_job_ui_enabled",
    "is_async_planner_enabled",
    "is_stage_telemetry_enabled",
]
