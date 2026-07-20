"""Persist-time backstop for the instructor-clash HARD rule.

**Why this exists.** Instructor double-booking is a hard constraint: no build may
ship one. The optimise pipeline enforces it per-move in every stage, but several
paths write placements *without* going through those filters —

1. ``timetable_solver.persist_solver_result`` — the board CP-SAT models rooms and
   students but has no instructor variable at all;
2. ``timetable_load_balanced.rebalance_and_persist_board`` — same;
3. manual placement / drag-drop in the workspace views, and
   ``timetable_workspace.create_planned_section_placements``, which compute a
   validation and then persist regardless of it.

Nothing downstream re-checked the written board, so a clash introduced by any of
those was persisted **silently**. This module is the single check those paths
call, so a double-booking is always detected, logged with the instructor and
slot, and returned to the caller.

**Scenario-wide, deliberately.** The workspace already has a per-board detector
(``detect_board_conflicts``), and ``compute_scenario_safety_summary`` sums its
counts into ``same_board_conflicts["instructors"]``. That grouping is per board,
so an instructor teaching on *two* boards in the same slot is invisible to it —
and instructors routinely span boards, since boards are program/gender scoped.
The two real clashes fixed in PR #44 were exactly that shape. So the per-board
number is a strict *subset* of the truth and cannot serve as the backstop.

**Sibling sections are excluded, and that is load-bearing.**
``build_section_instructor_map_for_scenario`` attributes instructors at COURSE
granularity: it unions every instructor assigned to a course and hands that same
set to *every section* of it. So a course with two instructors and two
overlapping sections reports two clashes when the real assignment (one
instructor each) has none. Because this count now vetoes an optimise run, a
phantom could discard an entire good run. Sibling overlap is already forbidden
by the separate same-course rule, so dropping those pairs here loses no
enforcement — it only removes the pairs the instructor map cannot speak to
truthfully. Cross-course clashes, the ones this backstop exists for, are
unaffected.

**Delta, not absolute.** Where a caller gates, the question is "did this write
make it worse". An absolute-zero gate would refuse every persist onto a board
carrying a legacy clash — precisely the paralysis the delta-form predicates in
``timetable_constraints`` were introduced to avoid (see that module's header).
Blocking a repair because the board was already broken is how you get a board
nobody can fix.

**Where enforcement actually happens.** Only one caller can safely act: the V2
runner holds a scenario snapshot, so ``instructor_clashes_scenario`` is wired
into ``optimiser_safety_regression`` and a newly-introduced clash rolls the run
back. The other paths cannot roll back a scenario without discarding unrelated
work, and a manual placement is a human acting deliberately who should be *told*,
not silently overruled — so they report: a WARNING naming the instructor and
slot, plus the verdict on the response for the UI to surface. That asymmetry is
intentional, but it does mean the value of this module on those paths is
observability, not prevention.
"""

from __future__ import annotations

import logging
from typing import Any

from core.services.timetable_constraints import list_instructor_clashes

logger = logging.getLogger(__name__)

_EMPTY: dict[str, Any] = {"count": 0, "clashes": [], "checked": False}


def _unchecked(reason: str) -> dict[str, Any]:
    return {"count": 0, "clashes": [], "checked": False, "reason": reason}


def scenario_instructor_clash_report(scenario_id: int) -> dict[str, Any]:
    """Read the PERSISTED board back and report instructor double-bookings.

    Reads ``SectionPlacement`` (the written truth, not the optimiser's in-memory
    candidate) so it verifies what actually landed in the database. Returns
    ``{"count": int, "clashes": [...], "checked": bool}``.

    ``checked`` is False when the check could not run — links disabled, no
    instructor assignments, or an internal error. That is NOT the same as a
    clean board and callers must never render it as one.

    **Never raises.** Every caller invokes this immediately before or after a
    write, so an exception here would break persists that worked fine before this
    module existed — turning a safety net into an outage. The guard lives at this
    level rather than only in :func:`verify_persisted_scenario` because the
    count-only helper shares it; an unguarded twin ahead of the guarded one at
    every call site would defeat the whole contract.
    """
    try:
        from core.services.timetable_pr4_instructor import is_instructor_links_enabled

        if not is_instructor_links_enabled():
            return _unchecked("links_disabled")

        from core.services.timetable_optimizer_v2 import (
            build_section_instructor_map_for_scenario,
            build_section_states_for_scenario,
        )

        section_instructor_ids = build_section_instructor_map_for_scenario(scenario_id)
        if not section_instructor_ids:
            return _unchecked("no_instructor_links")

        sections_by_id = {
            state.section_id: state for state in build_section_states_for_scenario(scenario_id)
        }
        clashes = [
            row
            for row in list_instructor_clashes(sections_by_id, section_instructor_ids)
            if not _is_sibling_pair(sections_by_id, row)
        ]
        return {"count": len(clashes), "clashes": clashes, "checked": True}
    except Exception:  # pragma: no cover - defensive; see docstring
        logger.exception("Instructor clash backstop failed for scenario %s", scenario_id)
        return _unchecked("backstop_error")


def _is_sibling_pair(sections_by_id: dict[str, Any], row: dict[str, Any]) -> bool:
    """True when both sections of a reported pair belong to the same course.

    See the module docstring: course-granular instructor attribution cannot say
    which sibling a given instructor actually teaches, so such a pair is not
    evidence of a double-booking. The same-course rule forbids the overlap on its
    own account.
    """
    try:
        first, second = row["sections"]
    except (KeyError, ValueError):
        return False
    a, b = sections_by_id.get(first), sections_by_id.get(second)
    if a is None or b is None:
        return False
    return a.course_code == b.course_code


def section_id_for(term_section: Any) -> str:
    """The ``section_id`` the clash rows are keyed by, for a ``TermSection`` row.

    Mirrors ``build_section_states_for_scenario`` /
    ``build_section_instructor_map_for_scenario``: ``f"{course_key or course_code}_{section}"``.
    Kept here so a caller wanting "did the thing I just placed clash?" does not
    re-derive the convention and drift from it.
    """
    key = getattr(term_section, "course_key", "") or getattr(term_section, "course_code", "")
    return f"{key}_{getattr(term_section, 'section', '')}"


def clashes_involving(report: dict[str, Any], section_id: str) -> list[dict[str, Any]]:
    """The reported clashes that involve one specific section.

    This — not the scenario-wide delta — is the actionable signal for a manual
    placement. The absolute count would fire on every drag while any unrelated
    legacy clash existed, and a before/after delta would cost a second
    whole-scenario scan on an interactive endpoint. Asking "is the section I just
    touched double-booked" answers the registrar's actual question from the
    single post-write scan.
    """
    if not report.get("checked"):
        return []
    return [row for row in report.get("clashes", []) if section_id in (row.get("sections") or [])]


def scenario_instructor_clash_count(scenario_id: int) -> int:
    """Clash count only. Shares the never-raises guard above."""
    return int(scenario_instructor_clash_report(scenario_id).get("count") or 0)


def verify_persisted_scenario(
    scenario_id: int,
    *,
    context: str,
    before: int | None = None,
) -> dict[str, Any]:
    """Backstop a persist: detect clashes on the written board and log them.

    ``context`` names the writing path (e.g. ``"board_cpsat_solver"``) so a
    violation in the log points at its source. ``before`` is optional and costs a
    second whole-scenario scan, so pass it only where a caller actually gates on
    the delta — most callers want the single post-write scan and the absolute
    count.

    Never raises (the guard is in :func:`scenario_instructor_clash_report`).
    ``test_report_runs_end_to_end_on_a_real_scenario`` pins the happy path, so a
    permanently-disabled backstop fails the suite rather than passing quietly.
    """
    report = scenario_instructor_clash_report(scenario_id)
    report["context"] = context
    if before is not None and report["checked"]:
        report["before"] = before
        report["introduced"] = max(0, report["count"] - before)
    else:
        report["introduced"] = 0

    if report["checked"] and report["count"]:
        logger.warning(
            "Instructor clash backstop: scenario %s has %d double-booking(s) after %s: %s",
            scenario_id,
            report["count"],
            context,
            report["clashes"][:5],
        )
    return report
