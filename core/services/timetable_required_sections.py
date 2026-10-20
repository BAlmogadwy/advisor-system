"""Guarantees a required course section is never silently dropped.

Found live on 2026-10-15 (scenario 23/24, course DS331): the V2
Generate/Optimise pipeline can leave a course short of a section it is
budgeted for — zero ``TermSectionMeeting`` rows, zero ``SectionPlacement``
rows anywhere in the scenario — with nothing downstream ever retrying it.
Once dropped, the course stays dropped across every later re-optimise,
because ``optimise_current_timetable`` reads the board "as is" and never
calls the placer, and nothing else in the pipeline revisits a section that
was never a placement candidate in the first place. The only place the drop
was visible at all was the section-budget audit
(:func:`core.services.timetable_workspace.compute_scenario_budget`), which
is not part of the normal Generate/Optimise result.

This module is deliberately thin: it reuses that same audit for detection
and reuses :func:`core.services.timetable_autoplace.auto_place_scenario` —
the exact function that performs the original greedy placement — for
repair, rather than reimplementing the hard-constraint checks (instructor
clash, daily cap, same-course overlap) a second time. ``auto_place_board``
only ever attempts courses where ``already < planned_sections`` (computed
fresh from the DB on every call), so calling it again here can never touch,
move, or re-evaluate a section that is already fully placed — it is a safe,
additive retry, not a rebuild.
"""

from __future__ import annotations

import logging

from core.services.course_identity import display_course_label
from core.services.timetable_flags import is_required_section_guarantee_enabled
from core.services.timetable_workspace import compute_scenario_budget

logger = logging.getLogger(__name__)


def _row_course_key(row: dict) -> str:
    return str(row.get("course_key") or row["course_code"])


def sections_short_by_course(rows: list[dict]) -> dict[str, int]:
    """``{course_key: sections still missing}`` for audit rows.

    Keyed by the planner identity, never the display code: CS112 PROGRAMMING I
    and CS112 PROGRAMMING II are two courses, and keying on ``course_code``
    let one of them hide the other's drop or repair. A count, not a set, so
    "short by one" turning into "short by two" is visible too.
    """
    return {_row_course_key(row): int(row.get("remaining_sections") or 0) for row in rows}


def course_labels_by_key(rows: list[dict]) -> dict[str, str]:
    """``{course_key: "CS112 - PROGRAMMING II"}`` display labels for audit rows."""
    return {
        _row_course_key(row): display_course_label(row["course_code"], row.get("course_name"))
        for row in rows
    }


def describe_short_courses(rows: list[dict]) -> list[str]:
    """Human-facing ``"CS112 - PROGRAMMING II (1 of 1)"`` labels for log lines."""
    return [
        f"{display_course_label(row['course_code'], row.get('course_name'))} "
        f"({row.get('remaining_sections')} of {row.get('planned_sections')})"
        for row in rows
    ]


def find_missing_required_sections(scenario_id: int) -> list[dict]:
    """Courses whose planned section count exceeds what is actually placed.

    A thin filter over :func:`compute_scenario_budget` — the same audit
    that found DS331's drop. A course appears here when
    ``remaining_sections > 0``: at least one of its required sections has
    no ``SectionPlacement`` row anywhere in the scenario.
    """
    budget = compute_scenario_budget(scenario_id)
    return [row for row in budget if int(row.get("remaining_sections", 0) or 0) > 0]


def summarise_required_sections(scenario_id: int) -> dict:
    """JSON-safe required-section status for a job's ``result_json``.

    Always safe to call regardless of the guarantee flag — this is pure
    reporting over :func:`find_missing_required_sections`, so the operator
    can see a drop even when active reinstatement is turned off.
    """
    missing = find_missing_required_sections(scenario_id)
    return {
        "status": "complete" if not missing else "incomplete",
        "missing": missing,
    }


def reinstate_missing_required_sections(scenario_id: int) -> dict:
    """Attempt to place any section currently missing from the scenario.

    No-ops (no DB writes, no import of the placer) when nothing is missing —
    the common case. When something is missing, calls
    :func:`auto_place_scenario` once more; it is idempotent against
    already-placed sections, so this can only add sections, never move or
    remove existing ones. Never raises: a failure here must not discard an
    otherwise-successful optimiser run, so any exception is logged and
    reported as a zero-progress attempt instead.
    """
    before = find_missing_required_sections(scenario_id)
    if not before:
        return {
            "enabled": True,
            "attempted": 0,
            "before_missing": [],
            "after_missing": [],
            "newly_placed": [],
        }

    logger.warning(
        "Required-section guarantee: %d course(s) short a section in scenario %d, "
        "attempting reinstatement: %s",
        len(before),
        scenario_id,
        describe_short_courses(before),
    )

    # Both the placement attempt AND the post-check share one try/except.
    # This runs before the runner's own snapshot exists (current mode calls
    # it as a pre-step, ahead of ``snapshot_scenario``), so an exception
    # escaping here — even from the read-only post-check, after the placer
    # already wrote something — would propagate uncaught with nothing left
    # to roll back to. Report a zero-progress attempt instead; the courses
    # that were missing going in are the safe assumption still missing.
    placed = None
    try:
        from core.services.timetable_autoplace import auto_place_scenario

        placed = auto_place_scenario(scenario_id)
        after = find_missing_required_sections(scenario_id)
    except Exception:
        logger.exception(
            "Required-section guarantee: reinstatement attempt raised for scenario %d",
            scenario_id,
        )
        after = before

    # A course "newly placed" any section it was missing — partial progress
    # (3 of 5 -> 4 of 5) changed the board too, and the full-mode re-score keys
    # off this list.
    short_before = sections_short_by_course(before)
    short_after = sections_short_by_course(after)
    newly_placed = [key for key, n in short_before.items() if short_after.get(key, 0) < n]

    if after:
        logger.error(
            "Required-section guarantee: %d course(s) STILL short a section in scenario "
            "%d after reinstatement: %s",
            len(after),
            scenario_id,
            describe_short_courses(after),
        )

    report = {
        "enabled": True,
        "attempted": len(before),
        "before_missing": before,
        "after_missing": after,
        "newly_placed": newly_placed,
    }
    # One teacher per section: a section the placer could only put at an option
    # its guard ranks last (``PlacerGuard``) is listed here; the coverage's
    # ``tbd_sections`` say what stops each teacher.
    boards = placed.get("boards") if isinstance(placed, dict) else None
    ranked_last = [
        row
        for board in (boards or {}).values()
        if isinstance(board, dict)
        for row in (board.get("teacher_guard") or {}).get("ranked_last", [])
    ]
    if ranked_last:
        report["teacher_guard_ranked_last"] = ranked_last
    return report


def maybe_reinstate_missing_required_sections(scenario_id: int) -> dict | None:
    """Flag-gated wrapper: runs the repair only when the guarantee is on.

    Returns ``None`` when the flag is off, so callers can tell "checked and
    nothing to do" apart from "did not check".
    """
    if not is_required_section_guarantee_enabled():
        return None
    return reinstate_missing_required_sections(scenario_id)
