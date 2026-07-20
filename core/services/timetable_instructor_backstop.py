"""Persist-time backstop for the instructor-clash HARD rule.

**Why this exists.** Instructor double-booking is a hard constraint: no build may
ship one. The optimise pipeline enforces it per-move in every stage, but three
paths write placements *without* going through those filters —

1. ``timetable_solver.persist_solver_result`` — the board CP-SAT models rooms and
   students but has no instructor constraint at all;
2. ``timetable_load_balanced.rebalance_and_persist_board`` — same;
3. manual placement / drag-drop in the workspace views, which computes a
   validation and then persists regardless of it.

Nothing downstream re-checked the written board, so a clash introduced by any of
those was persisted **silently**. This module is the single check every persist
path calls, so no path can write a double-booking without it being detected,
logged, and returned to the caller.

**Scenario-wide, deliberately.** The workspace already has a per-board detector
(``detect_board_conflicts``), and ``compute_scenario_safety_summary`` sums its
counts into ``same_board_conflicts["instructors"]``. That grouping is per board,
so an instructor teaching on *two* boards in the same slot is invisible to it —
and instructors routinely span boards, since boards are program/gender scoped.
The two real clashes fixed in PR #44 were exactly that shape. So the per-board
number is a strict *subset* of the truth and cannot serve as the backstop; this
module reads the whole scenario and uses the interval-aware predicates in
``timetable_constraints``.

**Delta, not absolute.** :func:`verify_persisted_scenario` reports the absolute
count but the *gate* is "did this write make it worse". An absolute-zero gate
would refuse every persist onto a board that already carries a legacy clash —
which is precisely the paralysis the delta-form predicates in
``timetable_constraints`` were introduced to avoid (see that module's header).
Blocking a repair because the board was already broken is how you get a board
nobody can fix.

**Detection here, enforcement at the caller.** The paths differ in what they can
safely do about a violation: the V2 runner holds a scenario snapshot and can roll
back, a board-scoped persist cannot roll back the scenario without discarding
unrelated work, and a manual placement is a human acting deliberately who should
be *told*, not silently overruled. So this module returns a verdict and each
caller applies the response its context allows.
"""

from __future__ import annotations

import logging
from typing import Any

from core.services.timetable_constraints import list_instructor_clashes

logger = logging.getLogger(__name__)


def scenario_instructor_clash_report(scenario_id: int) -> dict[str, Any]:
    """Read the PERSISTED board back and report instructor double-bookings.

    Reads ``SectionPlacement`` (the written truth, not the optimiser's in-memory
    candidate) so it verifies what actually landed in the database. Returns
    ``{"count": int, "clashes": [...], "checked": bool}``. ``checked`` is False
    when the instructor link feature is off or the scenario has no instructor
    assignments — there is nothing to verify, which is not the same as a clean
    board, and callers must not report it as one.
    """
    from core.services.timetable_pr4_instructor import is_instructor_links_enabled

    if not is_instructor_links_enabled():
        return {"count": 0, "clashes": [], "checked": False, "reason": "links_disabled"}

    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    section_instructor_ids = build_section_instructor_map_for_scenario(scenario_id)
    if not section_instructor_ids:
        return {"count": 0, "clashes": [], "checked": False, "reason": "no_instructor_links"}

    sections_by_id = {
        state.section_id: state for state in build_section_states_for_scenario(scenario_id)
    }
    clashes = list_instructor_clashes(sections_by_id, section_instructor_ids)
    return {"count": len(clashes), "clashes": clashes, "checked": True}


def verify_persisted_scenario(
    scenario_id: int,
    *,
    context: str,
    before: int | None = None,
) -> dict[str, Any]:
    """Backstop a persist: detect clashes on the written board and log them.

    ``context`` names the writing path (e.g. ``"board_cpsat"``) so a violation in
    the log points at its source. ``before`` is the clash count captured *before*
    the write; when supplied, ``introduced`` reports how many this write added —
    that delta, not the absolute count, is what a caller should gate on.

    Never raises: a backstop that can break the persist it guards is worse than
    the hole it closes. The catch is deliberately broad, which has a sharp edge —
    a bug *inside* the check (a bad import, say) would be swallowed and the
    backstop would silently verify nothing forever. ``checked`` exists so that
    state is visible rather than indistinguishable from a clean board, and
    ``test_report_runs_end_to_end_on_a_real_scenario`` pins the happy path so a
    permanently-disabled backstop fails the suite instead of passing quietly.
    """
    try:
        report = scenario_instructor_clash_report(scenario_id)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Instructor clash backstop failed for scenario %d (%s)", scenario_id, context
        )
        return {"count": 0, "clashes": [], "checked": False, "reason": "backstop_error"}

    report["context"] = context
    if before is not None and report["checked"]:
        report["before"] = before
        report["introduced"] = max(0, report["count"] - before)
    else:
        report["introduced"] = 0

    if report["checked"] and report["count"]:
        logger.warning(
            "Instructor clash backstop: scenario %d has %d double-booking(s) after %s "
            "(%d introduced by this write): %s",
            scenario_id,
            report["count"],
            context,
            report["introduced"],
            report["clashes"][:5],
        )
    return report


def scenario_instructor_clash_count(scenario_id: int) -> int:
    """Clash count only — for capturing the ``before`` value around a write."""
    return int(scenario_instructor_clash_report(scenario_id).get("count") or 0)
