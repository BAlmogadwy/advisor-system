"""The persisted-board report of teaching-time rule violations
(``TIMETABLE_INSTRUCTOR_AVAILABILITY``, slice B2, decision B2-7).

``scenario_instructor_availability_report`` reads the PERSISTED board and the
teacher map in force (the stored map, or the run's pinned one inside a run), and
lists every meeting of a charged, non-exempt section that breaks a rule of the
teacher it is charged to. This is the authoritative number shown to staff: the
evaluator's side band (``timetable_candidate_eval``) is a delta net over the
sections a stage can move, not a count of the board.

Rows are keyed by ``(course_key, label, day, start, end, instructor_id)``, never
by a placement id (persisting recreates the rows). The online exemption is the
one predicate every stage uses
(``timetable_instructor_availability.online_exempt_sections``). The report
never raises: a failure reads ``checked: False`` and is logged, because a
reader must not turn a failed read into a clean board.

``pair_counts`` and ``excused_pairs`` serve the runner's
``instructor_availability_violations`` gate (decision B2-6).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from core.services.timetable_instructor_availability import availability_for, online_exempt_sections

logger = logging.getLogger(__name__)

_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_ROW_LIMIT = 50


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _unchecked(reason: str) -> dict[str, Any]:
    return {"checked": False, "count": 0, "rows": [], "unchecked": [], "reason": reason}


def scenario_instructor_availability_report(scenario_id: int) -> dict[str, Any]:
    """``{"checked", "count", "rows", "unchecked"}`` of the persisted board.

    ``rows``: one per (distinct meeting, teacher whose rule it breaks), sorted,
    each ``{section_id, course_key, label, day, start, end, instructor_id}``.
    ``unchecked``: English sections with no one complete weekly pattern, which
    have no known meetings (as the clash backstop reports them). ``checked`` is
    False (with a ``reason``) when no rule binds a teacher of the scenario or the
    read failed; that is not a clean board. Never raises."""
    try:
        availability = availability_for(scenario_id)
        if not availability:
            return _unchecked("no_rules")

        from core.models import TermSection
        from core.services.timetable_constraints import list_instructor_availability_violations
        from core.services.timetable_optimizer_v2 import (
            INCOMPLETE_COLLECT,
            IncompletePolicyPattern,
            build_section_instructor_map_for_scenario,
            build_section_states_for_scenario,
        )

        charged = build_section_instructor_map_for_scenario(
            scenario_id, incomplete=INCOMPLETE_COLLECT
        )
        incomplete: list[IncompletePolicyPattern] = []
        states = {
            state.section_id: state
            for state in build_section_states_for_scenario(
                scenario_id, incomplete=INCOMPLETE_COLLECT, collected=incomplete
            )
        }
        identity = {
            f"{course_key or code}_{label}": (course_key or code, label)
            for course_key, code, label in TermSection.objects.filter(
                scenario_id=scenario_id
            ).values_list("course_key", "course_code", "section")
        }
        exempt = [f"{key}_{label}" for key, label in online_exempt_sections(scenario_id)]
        rows: dict[tuple, dict[str, Any]] = {}
        for found in list_instructor_availability_violations(
            states, charged, availability, exempt=exempt
        ):
            course_key, label = identity.get(found["section_id"], (found["section_id"], ""))
            day = _DAYS[found["day"]] if 0 <= found["day"] < len(_DAYS) else str(found["day"])
            key = (
                course_key,
                label,
                day,
                _hhmm(found["start_min"]),
                _hhmm(found["end_min"]),
                found["instructor_id"],
            )
            rows.setdefault(
                key,
                {
                    "section_id": found["section_id"],
                    "course_key": course_key,
                    "label": label,
                    "day": day,
                    "start": key[3],
                    "end": key[4],
                    "instructor_id": found["instructor_id"],
                },
            )
        ordered = [rows[key] for key in sorted(rows, key=lambda k: tuple(str(x) for x in k))]
        return {
            "checked": True,
            "count": len(ordered),
            "rows": ordered,
            "unchecked": [
                {
                    "course_code": exc.course_code,
                    "section": exc.section,
                    "board_ids": list(exc.board_ids),
                }
                for exc in incomplete
            ],
        }
    except Exception:  # a reader must never break the run that asks
        logger.exception("Instructor availability report failed for scenario %s", scenario_id)
        return _unchecked("report_error")


def pair_counts(report: Mapping[str, Any]) -> Counter:
    """Blocked meetings per ``(section_id, instructor_id)`` of a report."""
    return Counter((row["section_id"], row["instructor_id"]) for row in report.get("rows", ()))


def _section_id_of(row: Mapping[str, Any]) -> str | None:
    if row.get("section_id"):
        return str(row["section_id"])
    # A placer row names its section ``course_key|label``.
    section = row.get("section")
    if isinstance(section, str) and "|" in section:
        course_key, _bar, label = section.partition("|")
        return f"{course_key}_{label}"
    return None


def excused_pairs(*row_groups: Iterable[Mapping[str, Any]]) -> set[tuple[str, int]]:
    """``(section_id, instructor_id)`` of every row of the groups (the repair's
    ``locked_blocked`` and ``unavoidable``, a placer's ``ranked_last``): the
    violations a stage already reported and could not avoid."""
    out: set[tuple[str, int]] = set()
    for rows in row_groups:
        for row in rows or ():
            section_id = _section_id_of(row)
            if section_id is None:
                continue
            ids = row.get("instructor_ids") or (
                [row["instructor_id"]] if row.get("instructor_id") is not None else []
            )
            out.update((section_id, int(iid)) for iid in ids)
    return out


def attachable(report: Mapping[str, Any]) -> dict[str, Any]:
    """The report as it is attached to a run result: the full count, the first
    ``_ROW_LIMIT`` rows."""
    shown = dict(report)
    shown["rows"] = list(report.get("rows", ()))[:_ROW_LIMIT]
    return shown


__all__ = [
    "attachable",
    "excused_pairs",
    "pair_counts",
    "scenario_instructor_availability_report",
]
