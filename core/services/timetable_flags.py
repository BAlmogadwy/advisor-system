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
TIERED_OBJECTIVE_SETTING = "TIMETABLE_TIERED_OBJECTIVE_ENABLED"
TIERED_T2_TOLERANCE_SETTING = "TIMETABLE_TIERED_T2_TOLERANCE"
TIERED_SOFT_GAP_BUDGET_SETTING = "TIMETABLE_TIERED_SOFT_GAP_BUDGET"


def is_stage_telemetry_enabled() -> bool:
    return bool(getattr(settings, STAGE_TELEMETRY_SETTING, False))


def is_tiered_objective_enabled() -> bool:
    """Gate the course-tier-aware lexicographic objective.

    When True the candidate evaluator returns the 9-element tiered tuple
    (high-risk / clash / T1 / T2-over-tolerance / real-gap / soft /
    reserve / spread / instructor-idle). When False the score keeps its
    legacy 6-tuple (or 7-tuple under the instructor-gap flag) shape *and*
    values, so optimiser output is byte-identical to pre-feature.
    """
    return bool(getattr(settings, TIERED_OBJECTIVE_SETTING, False))


def get_tiered_t2_tolerance() -> int:
    """Per-T2-course unresolved-seat tolerance (default 3).

    Up to this many unresolved seats in a single Tier-2 course are treated
    as *soft* (objective position E); the excess is *near-hard* (position
    C). Clamped at zero — a negative misconfig would let a strictly-worse
    candidate score better, so it is floored here at read time.
    """
    return max(0, int(getattr(settings, TIERED_T2_TOLERANCE_SETTING, 3)))


def get_tiered_soft_gap_budget() -> int:
    """Gap-minutes a single soft-tier unresolved seat is 'worth' (default 120).

    The tiered objective's student-cost position is
    ``real_gap_minutes + budget * soft_unresolved``, so the optimiser seats a
    soft (Tier-3 / Tier-2-within-tolerance) course exactly when doing so adds
    fewer than ``budget`` gap-minutes, and declines when it costs more — a
    bounded trade between schedule quality and gen-ed enrolment. 0 reproduces
    strict quality-first (gaps always beat gen-ed). Floored at 0.
    """
    return max(0, int(getattr(settings, TIERED_SOFT_GAP_BUDGET_SETTING, 120)))


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
    "TIERED_OBJECTIVE_SETTING",
    "TIERED_SOFT_GAP_BUDGET_SETTING",
    "TIERED_T2_TOLERANCE_SETTING",
    "get_tiered_soft_gap_budget",
    "get_tiered_t2_tolerance",
    "is_async_job_ui_effective",
    "is_async_job_ui_enabled",
    "is_async_planner_enabled",
    "is_stage_telemetry_enabled",
    "is_tiered_objective_enabled",
]
