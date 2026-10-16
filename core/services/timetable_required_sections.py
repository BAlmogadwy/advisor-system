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

from core.services.timetable_flags import is_required_section_guarantee_enabled
from core.services.timetable_workspace import compute_scenario_budget

logger = logging.getLogger(__name__)


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

    before_codes = [row["course_code"] for row in before]
    logger.warning(
        "Required-section guarantee: %d course(s) short a section in scenario %d, "
        "attempting reinstatement: %s",
        len(before),
        scenario_id,
        before_codes,
    )

    # Both the placement attempt AND the post-check share one try/except.
    # This runs before the runner's own snapshot exists (current mode calls
    # it as a pre-step, ahead of ``snapshot_scenario``), so an exception
    # escaping here — even from the read-only post-check, after the placer
    # already wrote something — would propagate uncaught with nothing left
    # to roll back to. Report a zero-progress attempt instead; the courses
    # that were missing going in are the safe assumption still missing.
    try:
        from core.services.timetable_autoplace import auto_place_scenario

        auto_place_scenario(scenario_id)
        after = find_missing_required_sections(scenario_id)
    except Exception:
        logger.exception(
            "Required-section guarantee: reinstatement attempt raised for scenario %d",
            scenario_id,
        )
        after = before

    after_codes = {row["course_code"] for row in after}
    newly_placed = [code for code in before_codes if code not in after_codes]

    if after:
        logger.error(
            "Required-section guarantee: %d course(s) STILL short a section in scenario "
            "%d after reinstatement: %s",
            len(after),
            scenario_id,
            sorted(after_codes),
        )

    return {
        "enabled": True,
        "attempted": len(before),
        "before_missing": before,
        "after_missing": after,
        "newly_placed": newly_placed,
    }


def maybe_reinstate_missing_required_sections(scenario_id: int) -> dict | None:
    """Flag-gated wrapper: runs the repair only when the guarantee is on.

    Returns ``None`` when the flag is off, so callers can tell "checked and
    nothing to do" apart from "did not check".
    """
    if not is_required_section_guarantee_enabled():
        return None
    return reinstate_missing_required_sections(scenario_id)
