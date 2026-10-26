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
ONLINE_GAP_EXCLUSION_SETTING = "TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED"
TIERED_T2_TOLERANCE_SETTING = "TIMETABLE_TIERED_T2_TOLERANCE"
TIERED_SOFT_GAP_BUDGET_SETTING = "TIMETABLE_TIERED_SOFT_GAP_BUDGET"
GAP_REGULAR_STUDENTS_ONLY_SETTING = "TIMETABLE_GAP_REGULAR_STUDENTS_ONLY"
REQUIRED_SECTION_GUARANTEE_SETTING = "TIMETABLE_REQUIRED_SECTION_GUARANTEE_ENABLED"
SECTION_DAY_SPREAD_SETTING = "TIMETABLE_SECTION_DAY_SPREAD"
SECTION_DAY_SPREAD_WEIGHT_SETTING = "TIMETABLE_SECTION_DAY_SPREAD_WEIGHT"
ONLINE_EVENING_WINDOWS_SETTING = "TIMETABLE_ONLINE_EVENING_WINDOWS"


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


def is_online_gap_exclusion_enabled() -> bool:
    """Gate excluding ONLINE sessions from the student/instructor gap chains.

    An online class is attended remotely, so it neither creates nor bridges an
    on-campus gap. Read ONCE where ``SectionState``s are built (not in the gap
    hot loop): with this off no section is ever flagged online, so the previous
    online-blind numbers are reproduced byte-for-byte.
    """
    return bool(getattr(settings, ONLINE_GAP_EXCLUSION_SETTING, False))


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


def is_gap_regular_students_only_enabled() -> bool:
    """Gate counting only REGULAR (non-visitor) students' gap-minutes (default OFF).

    A visitor is a cross-term student (``StudentProfile.is_visitor``, from the
    persisted ``ScenarioStudentCourseRequest.is_cross_term``). With the flag off
    every student's gap counts and all output is byte-identical to before.
    """
    return bool(getattr(settings, GAP_REGULAR_STUDENTS_ONLY_SETTING, False))


def is_required_section_guarantee_enabled() -> bool:
    """Gate the active retry that reinstates a required section the V2
    pipeline dropped (see ``core.services.timetable_required_sections``).

    Default True: placing every section the section-planning budget calls
    for is a hard rule, not a stakeholder trade-off. Detection/reporting
    (``find_missing_required_sections``) always runs regardless of this flag;
    only the retry call and the rollback-gate entry are gated.
    """
    return bool(getattr(settings, REQUIRED_SECTION_GUARANTEE_SETTING, True))


ONE_TEACHER_PER_SECTION_SETTING = "TIMETABLE_ONE_TEACHER_PER_SECTION"


def is_one_teacher_active() -> bool:
    """Charge each section to exactly one teacher (or to nobody) — default OFF.

    A ``CourseInstructor`` link says who MAY teach a course. With this off every
    linked teacher is charged with every section of it at once, as before. On,
    ``core.services.section_teacher_assignment`` picks one teacher per section
    and every instructor rule reads that map. It needs the links flag too: the
    eligible teachers come from the links, and with links off there are none.
    """
    return bool(getattr(settings, ONE_TEACHER_PER_SECTION_SETTING, False)) and bool(
        getattr(settings, "TIMETABLE_INSTRUCTOR_LINKS_ENABLED", False)
    )


SECTION_PROGRAMME_PREFERENCE_SETTING = "TIMETABLE_SECTION_PROGRAMME_PREFERENCE"


def is_section_programme_preference_enabled() -> bool:
    """Honour "mainly for programme X" section marks — default OFF.

    Read where ``SectionState``s are built (the only gate on seating, like
    ``is_online``), by the placement/lens payloads, the mark endpoint, the repair
    fingerprint and the scheduler bridge. Off, every state carries "" and the
    grouping pass is never entered, so all output is byte-identical. The stored
    marks themselves are data and are kept (and copied) either way.
    """
    return bool(getattr(settings, SECTION_PROGRAMME_PREFERENCE_SETTING, False))


def is_section_day_spread_enabled() -> bool:
    """Prefer a two- or three-meeting section's meetings on non-consecutive days — default OFF.

    Read ONCE per board by the greedy placer, once per solve by the two CP-SAT
    builders and once per run by the improvement stages that move meetings (local
    search v2, chains, annealing, the load balancer, the CP-SAT polisher, secondary
    recovery, the instructor cap / clash repairs and compaction), never by manual
    moves, validation or the evaluator. Off, no engine reads the weight and all
    output is byte-identical.
    """
    return bool(getattr(settings, SECTION_DAY_SPREAD_SETTING, False))


def get_section_day_spread_weight() -> int:
    """Soft-cost units one consecutive-day pair costs a 2- or 3-meeting section (default 40).

    Floored at 0, like ``get_tiered_t2_tolerance``: a negative setting would
    reward consecutive days.
    """
    return max(0, int(getattr(settings, SECTION_DAY_SPREAD_WEIGHT_SETTING, 40)))


def is_online_evening_windows_enabled() -> bool:
    """Online courses meet only in the online evening windows — default OFF.

    Read only by ``timetable_online_windows.active_online_windows``, which the
    automatic placers and movers call (never manual moves, validation, previews,
    the readiness check or the evaluator). Off, it returns no window and every
    engine is byte-identical.
    """
    return bool(getattr(settings, ONLINE_EVENING_WINDOWS_SETTING, False))


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
    "GAP_REGULAR_STUDENTS_ONLY_SETTING",
    "ONE_TEACHER_PER_SECTION_SETTING",
    "REQUIRED_SECTION_GUARANTEE_SETTING",
    "SECTION_DAY_SPREAD_SETTING",
    "SECTION_DAY_SPREAD_WEIGHT_SETTING",
    "SECTION_PROGRAMME_PREFERENCE_SETTING",
    "STAGE_TELEMETRY_SETTING",
    "TIERED_OBJECTIVE_SETTING",
    "TIERED_SOFT_GAP_BUDGET_SETTING",
    "TIERED_T2_TOLERANCE_SETTING",
    "get_section_day_spread_weight",
    "get_tiered_soft_gap_budget",
    "get_tiered_t2_tolerance",
    "is_async_job_ui_effective",
    "is_async_job_ui_enabled",
    "is_async_planner_enabled",
    "is_gap_regular_students_only_enabled",
    "is_one_teacher_active",
    "is_required_section_guarantee_enabled",
    "is_section_day_spread_enabled",
    "is_section_programme_preference_enabled",
    "is_stage_telemetry_enabled",
    "is_tiered_objective_enabled",
]
