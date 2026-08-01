"""Post-build instructor-schedule compaction pass.

Reduces excess instructor working days and then physical-campus span/idle by
relocating sessions in TIME. It never changes who teaches what (one instructor
per course is fixed by policy), runs after daily-cap repair, and treats H7/H8 as
hard gates. Online meetings count as teaching work but not as campus span/idle.

Validated on scenario 627 (rolled-back replay): total instructor idle -40%, all
instructors improved / none worsened, feasibility + reserve unchanged, and total
student gap actually fell — because compacting an instructor's day tends to
compact the students who take those classes too.

Design (agreed with the user + an external review):
- **Fair workday-first hill-climb.** The individual day lower bound is the
  maximum of ``ceil(session_count / daily_cap)`` and H2's distinct-day bound.
  Maximum excess is minimised before total excess, so one poor instructor is not
  hidden by improvements to instructors who are already compact.
- **Instructor objective** (lexicographic, lower=better): maximum/total excess
  days, then worst/total campus span and idle, then added student gap.
- **Layered student guards** (hard, vs the pre-pass baseline): feasibility
  (unresolved tier-A / total unresolved / unassigned / clashes) and reserve never
  worsen; total student gap-minutes ≤ baseline·(1+budget); tier-A AND graduating
  (tier-B) added gap ≤ 0; per-student added gap ≤ a ceiling. A **trade alert**
  rejects any move whose student-gap cost isn't repaid by enough instructor
  saving (ratio guard) — the safety net for scenarios where the win/win
  alignment does not hold.
- **Modular neighbourhood**: relocation today; ``swap`` (and later chain/LNS) can
  be added to ``_NEIGHBOURHOODS`` without touching the evaluator or guards.
- **Room-safe persistence**: room repair and hard-safety verification run in the
  same transaction. If a moved physical meeting cannot be roomed, everything is
  rolled back.
- **Oscillation guards**: max rounds, accepted-move cap, no-revisit set, and the
  strict-improvement rule.

Flag-gated (``is_instructor_compaction_enabled``); no-op when off.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Collection, Mapping
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.services.timetable_assignment_models import RiskTier, SectionMeeting
from core.services.timetable_pr4_instructor import (
    get_instructor_compaction_config,
    get_instructor_daily_cap,
    is_instructor_compaction_enabled,
)

logger = logging.getLogger(__name__)

WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]


def _compute_instructor_compaction_metrics(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Collection[object]],
    daily_cap: int,
) -> dict:
    """Return workday and physical-campus metrics for compaction.

    Every meeting contributes to ``session_count`` and ``working_days``:
    remote teaching is still work and remains subject to H7/H8. Only campus
    days, span and idle omit ``SectionState.is_online`` sections, because a
    remote meeting cannot strand an instructor on campus.

    The per-instructor lower bound combines the cap bound
    ``ceil(session_count / daily_cap)`` with H2's distinct-day bound (the
    largest meeting count of a section taught by that instructor). It is still
    only a lower bound: blocked slots, co-teaching, student conflicts and rooms
    can make it unattainable.
    """
    if daily_cap <= 0:
        raise ValueError("daily_cap must be positive")

    teaching_days: dict[object, set[int]] = defaultdict(set)
    session_counts: dict[object, int] = defaultdict(int)
    max_section_meetings: dict[object, int] = defaultdict(int)
    campus_byday: dict[tuple[object, int], list[tuple[int, int]]] = defaultdict(list)

    for sid, section in sections_by_id.items():
        instructors = section_instructor_ids.get(sid, ())
        if not instructors:
            continue
        for iid in instructors:
            max_section_meetings[iid] = max(max_section_meetings[iid], len(section.meetings))
            for meeting in section.meetings:
                session_counts[iid] += 1
                teaching_days[iid].add(meeting.day)
                if not section.is_online:
                    campus_byday[(iid, meeting.day)].append((meeting.start_min, meeting.end_min))

    instructor_ids = set(session_counts)
    weekly_idle: dict[object, int] = defaultdict(int)
    weekly_span: dict[object, int] = defaultdict(int)
    campus_days: dict[object, int] = defaultdict(int)
    holes: dict[tuple[object, int], tuple[int, int]] = {}
    largest_hole = 0
    largest_daily_span = 0
    over90 = 0
    total_idle = 0
    total_span = 0

    for key, raw_sessions in campus_byday.items():
        # Hard feasibility means intervals do not overlap. ``set`` avoids
        # double-counting an accidental duplicate row in diagnostic metrics.
        sessions = sorted(set(raw_sessions))
        gaps = [
            gap
            for gap in (
                sessions[index + 1][0] - sessions[index][1] for index in range(len(sessions) - 1)
            )
            if gap > 0
        ]
        idle = sum(gaps)
        hole = max(gaps) if gaps else 0
        span = max(end for _start, end in sessions) - min(start for start, _end in sessions)
        iid, _day = key
        campus_days[iid] += 1
        weekly_idle[iid] += idle
        weekly_span[iid] += span
        holes[key] = (hole, idle)
        largest_hole = max(largest_hole, hole)
        largest_daily_span = max(largest_daily_span, span)
        over90 += int(hole > 90)
        total_idle += idle
        total_span += span

    per_instructor: dict[object, dict[str, int]] = {}
    for iid in instructor_ids:
        sessions = session_counts[iid]
        # H2 requires every meeting of one section to use a distinct day, so
        # that section's weekly meeting count is an independent lower bound.
        lower_bound = max(
            (sessions + daily_cap - 1) // daily_cap,
            max_section_meetings[iid],
        )
        workdays = len(teaching_days[iid])
        per_instructor[iid] = {
            "session_count": sessions,
            "working_days": workdays,
            "lower_bound_days": lower_bound,
            "excess_days": max(0, workdays - lower_bound),
            "campus_days": campus_days[iid],
            "physical_span": weekly_span[iid],
            "physical_idle": weekly_idle[iid],
        }

    excesses = [row["excess_days"] for row in per_instructor.values()]
    return {
        "max_excess_days": max(excesses, default=0),
        "total_excess_days": sum(excesses),
        "total_working_days": sum(row["working_days"] for row in per_instructor.values()),
        "total_lower_bound_days": sum(row["lower_bound_days"] for row in per_instructor.values()),
        "total_campus_days": sum(row["campus_days"] for row in per_instructor.values()),
        "worst_weekly_span": max(weekly_span.values(), default=0),
        "largest_daily_span": largest_daily_span,
        "total_span": total_span,
        # Historical names remain available to keep report consumers compatible.
        "largest": largest_hole,
        "over90": over90,
        "worst_weekly": max(weekly_idle.values(), default=0),
        "total": total_idle,
        "holes": holes,
        "weekly": dict(weekly_idle),
        "weekly_span": dict(weekly_span),
        "per_instructor": per_instructor,
    }


def _instructor_compaction_objective(metrics: Mapping[str, Any], added_gap: int) -> tuple:
    """Fair lexicographic objective; lower is better.

    Max-before-total avoids improving already compact instructors while leaving
    one instructor with a visibly poor week. Student gap is only the final
    tie-breaker here; the independent baseline guards still reject forbidden
    student regressions and unfavourable instructor/student trades.
    """
    return (
        metrics["max_excess_days"],
        metrics["total_excess_days"],
        metrics["worst_weekly_span"],
        metrics["largest_daily_span"],
        metrics["total_span"],
        metrics["largest"],
        metrics["over90"],
        metrics["worst_weekly"],
        metrics["total"],
        added_gap,
    )


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _interval_busy(slotset, day2: int, s2: int, e2: int) -> bool:
    """True if ``[s2, e2)`` on ``day2`` overlaps any interval in ``slotset``.

    Occupancy is stored as ``(day, start_min, end_min)`` so the guard tests true
    INTERVAL overlap, not start-time equality. The lecture grid (e.g. 10:30-11:45)
    and lab grid (09:00-10:40) interleave, so two sessions can overlap in time at
    different start minutes — a start-only guard misses that and lets a relocation
    create a ``same_board_overlaps`` the safety gate rolls back wholesale.
    """
    return any(d == day2 and s2 < oe and e2 > os for (d, os, oe) in slotset)


_PERSISTENCE_HARD_KEYS = (
    "h15_critical_overlaps",
    "h7_instructor_clashes",
    "h8_daily_overload_sessions",
    "h9_room_clashes",
    "physical_unassigned_rooms",
)


def _sweep_interval_overlaps(
    rows: Collection[tuple[object, int, int, object, Any]],
    on_overlap=None,
) -> dict[str, int]:
    """Visit every half-open interval overlap once using a deterministic sweep.

    Each row is ``(group_key, start, end, stable_id, payload)``. Intervals in
    different groups never interact. The active set is expired with a heap, so
    non-overlapping inputs do not perform all-pairs checks; work is
    ``O(n log n + k)`` apart from deterministic active-set iteration, where
    ``k`` is the number of actual overlapping pairs.

    ``on_overlap(left_payload, right_payload)`` is called in sweep order. The
    returned operation counters are intentionally exposed for non-timing-based
    scalability regression tests.
    """
    import heapq

    grouped: dict[object, list[tuple[int, int, object, Any]]] = defaultdict(list)
    for group_key, start, end, stable_id, payload in rows:
        grouped[group_key].append((int(start), int(end), stable_id, payload))

    row_count = 0
    candidate_pair_checks = 0
    overlap_pairs = 0
    heap_expirations = 0
    max_active = 0
    for group_key in sorted(grouped, key=lambda value: (type(value).__name__, repr(value))):
        group_rows = sorted(
            grouped[group_key],
            key=lambda row: (
                row[0],
                row[1],
                type(row[2]).__name__,
                repr(row[2]),
            ),
        )
        active: dict[object, tuple[int, int, object, Any]] = {}
        expiry_heap: list[tuple[int, int, object]] = []
        serial = 0
        for current in group_rows:
            start, end, stable_id, payload = current
            row_count += 1
            while expiry_heap and expiry_heap[0][0] <= start:
                _expired_end, _serial, expired_id = heapq.heappop(expiry_heap)
                active.pop(expired_id, None)
                heap_expirations += 1
            for previous in active.values():
                candidate_pair_checks += 1
                previous_start, previous_end, _previous_id, previous_payload = previous
                # Retain the exact historical half-open predicate, including
                # its behaviour for any malformed zero-length diagnostic row.
                if previous_start < end and previous_end > start:
                    overlap_pairs += 1
                    if on_overlap is not None:
                        on_overlap(previous_payload, payload)
            active[stable_id] = current
            heapq.heappush(expiry_heap, (end, serial, stable_id))
            serial += 1
            max_active = max(max_active, len(active))
    return {
        "rows": row_count,
        "groups": len(grouped),
        "candidate_pair_checks": candidate_pair_checks,
        "overlap_pairs": overlap_pairs,
        "heap_expirations": heap_expirations,
        "max_active": max_active,
    }


def _scenario_persistence_room_metrics(scenario_id: int) -> dict[str, int]:
    """Count scenario-wide physical room clashes and unassigned meetings."""
    from core.models import SectionPlacement
    from core.services.timetable_online import OnlineCourseLookup

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("board", "term_section")
        .order_by("id")
    )
    online_lookup = OnlineCourseLookup()
    room_intervals: list[tuple[object, int, int, object, Any]] = []
    physical_unassigned = 0
    for placement in placements:
        if online_lookup.is_online_course_for_board(
            placement.board, placement.term_section.course_code
        ):
            continue
        room_key = str(placement.room or "").strip().upper()
        if not room_key or room_key == "UNASSIGNED":
            physical_unassigned += 1
            continue
        room_intervals.append(
            (
                (room_key, str(placement.day).upper()),
                _to_min(str(placement.start_time)[:5]),
                _to_min(str(placement.end_time)[:5]),
                placement.id,
                placement,
            )
        )
    sweep = _sweep_interval_overlaps(room_intervals)
    return {
        "h9_room_clashes": sweep["overlap_pairs"],
        "physical_unassigned_rooms": physical_unassigned,
    }


def _scenario_persistence_hard_metrics(scenario_id: int, daily_cap: int) -> dict[str, int]:
    """Return independent persisted hard-constraint counters.

    These counters deliberately remain separate. In particular, H15 and H7
    must not be folded into a single ``critical`` total: one can fall while the
    other rises, which would make an aggregate non-increase check unsafe.
    Room clashes are counted scenario-wide (including different boards), while
    online placements are excluded only from the physical-room counters.
    """
    from core.models import SectionPlacement
    from core.services.student_helpers import normalize_code
    from core.services.timetable_constraints import (
        count_instructor_clashes,
        count_instructor_daily_overloads,
    )
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        build_overlap_matrix,
        shared_student_count,
    )

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("board", "term_section")
        .order_by("id")
    )
    course_identities = {
        str(p.term_section.course_key or p.term_section.course_code or "") for p in placements
    }
    overlap_matrix = build_overlap_matrix(scenario_id, course_identities)

    h15 = 0

    def count_critical_overlap(left_code: str, right_code: str) -> None:
        nonlocal h15
        same_course = normalize_code(left_code) == normalize_code(right_code)
        if (
            same_course
            or shared_student_count(overlap_matrix, left_code, right_code) >= HARD_OVERLAP_THRESHOLD
        ):
            h15 += 1

    h15_intervals = []
    for placement in placements:
        course_code = str(
            placement.term_section.course_key or placement.term_section.course_code or ""
        )
        h15_intervals.append(
            (
                str(placement.day).upper(),
                _to_min(str(placement.start_time)[:5]),
                _to_min(str(placement.end_time)[:5]),
                placement.id,
                course_code,
            )
        )
    _sweep_interval_overlaps(h15_intervals, count_critical_overlap)

    room_metrics = _scenario_persistence_room_metrics(scenario_id)
    states = build_section_states_for_scenario(scenario_id)
    sections_by_id = {section.section_id: section for section in states}
    instructor_map = build_section_instructor_map_for_scenario(scenario_id)
    return {
        "h15_critical_overlaps": h15,
        "h7_instructor_clashes": count_instructor_clashes(sections_by_id, instructor_map),
        "h8_daily_overload_sessions": count_instructor_daily_overloads(
            sections_by_id, instructor_map, daily_cap
        ),
        **room_metrics,
    }


def _validate_moved_room_compatibility(
    scenario_id: int,
    placement_ids: Collection[int],
) -> dict[str, Any]:
    """Validate H11-H14 for every moved physical placement.

    The rooming service historically ignored capacity for lab rooms. This gate
    intentionally does not: the same buffered budget demand is required for
    lectures and labs. A room code is accepted only when one concrete room row
    simultaneously satisfies type, buffered capacity, gender, and department.
    """
    from core.models import Room, ScenarioSectionBudget, SectionPlacement
    from core.services.timetable_rooming import (
        _budget_value_for_placement,
        _build_rooming_budget_maps,
        _section_gender,
        get_board_gender,
        get_capacity_buffer,
        room_type_for_placement,
    )

    wanted_ids = sorted({int(placement_id) for placement_id in placement_ids})
    placements = list(
        SectionPlacement.objects.filter(
            id__in=wanted_ids,
            board__scenario_id=scenario_id,
        )
        .select_related("board", "term_section")
        .order_by("id")
    )
    budgets = list(ScenarioSectionBudget.objects.filter(scenario_id=scenario_id))
    budget_maps = _build_rooming_budget_maps(budgets, get_capacity_buffer())
    rooms_by_code: dict[str, list[object]] = defaultdict(list)
    for room in Room.objects.all().order_by("id"):
        rooms_by_code[str(room.room_code or "").strip().upper()].append(room)

    board_gender: dict[int, str] = {}
    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for placement in placements:
        room_code = str(placement.room or "").strip()
        required_type = room_type_for_placement(placement, budget_maps=budget_maps)
        buffered_demand = _budget_value_for_placement(placement, budget_maps, "buffered", 40)
        if placement.board_id not in board_gender:
            board_gender[placement.board_id] = get_board_gender(placement.board_id)
        required_gender = (
            _section_gender(placement.term_section.section) or board_gender[placement.board_id]
        )
        required_programmes = {
            value.strip().upper()
            for value in str(placement.board.program or "").split(",")
            if value.strip()
        }
        candidates = rooms_by_code.get(room_code.upper(), []) if room_code else []
        candidate_checks: list[dict[str, Any]] = []
        valid_room_ids: list[int] = []
        for room in candidates:
            room_programmes = {
                value.strip().upper()
                for value in str(room.department or "").split(",")
                if value.strip()
            }
            flags = {
                "H11": str(room.room_type or "lecture").strip().lower() == required_type,
                "H12": int(room.capacity or 0) >= buffered_demand,
                "H13": not required_gender
                or str(room.section or "").strip().upper() == required_gender,
                "H14": bool(required_programmes & room_programmes),
            }
            candidate_checks.append({"room_id": room.id, **flags})
            if all(flags.values()):
                valid_room_ids.append(room.id)

        row = {
            "placement_id": placement.id,
            "term_section_id": placement.term_section_id,
            "room": room_code,
            "required_type": required_type,
            "buffered_demand": buffered_demand,
            "required_gender": required_gender,
            "required_programmes": sorted(required_programmes),
            "candidate_checks": candidate_checks,
            "matched_room_ids": valid_room_ids,
            "valid": bool(valid_room_ids),
        }
        checks.append(row)
        if not valid_room_ids:
            if not candidates:
                failed = ["H11", "H12", "H13", "H14"]
            else:
                failed = [
                    key
                    for key in ("H11", "H12", "H13", "H14")
                    if not any(candidate[key] for candidate in candidate_checks)
                ]
                if not failed:
                    failed = ["H11-H14_COMBINATION"]
            violations.append({**row, "failed_constraints": failed})

    missing_ids = sorted(set(wanted_ids) - {placement.id for placement in placements})
    for placement_id in missing_ids:
        violations.append(
            {
                "placement_id": placement_id,
                "failed_constraints": ["PLACEMENT_NOT_FOUND"],
                "valid": False,
            }
        )
    return {
        "valid": not violations,
        "checked_count": len(checks),
        "expected_count": len(wanted_ids),
        "checks": checks,
        "violations": violations,
    }


def _exact_repair_affected_rooms(
    scenario_id: int,
    moved_physical_ids: Collection[int],
    preferred_room_by_id: Mapping[int, str],
    *,
    time_limit_seconds: float = 10.0,
) -> dict[str, Any]:
    """Exactly re-room the smallest affected scope after greedy failure.

    The first model varies only moved physical placements. If that is
    infeasible, the second model expands to the overlap-connected component of
    assigned, unlocked physical placements, which is the complete scope in
    which room swaps can release capacity for a moved meeting. Everything else
    remains fixed occupancy.

    H9 and H11-H14 are model constraints. The exact objective is
    lexicographic: number of room-code changes from the pre-compaction board,
    then total capacity waste. Sorted inputs, one CP-SAT worker and a fixed seed
    make the chosen optimum reproducible. A merely feasible/time-limited result
    is rejected; persistence proceeds only with a proven optimum.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return {
            "status": "solver_unavailable",
            "proven_optimal": False,
            "reason": "OR-Tools CP-SAT is unavailable",
            "attempts": [],
        }

    from core.models import Room, ScenarioSectionBudget, SectionPlacement
    from core.services.timetable_online import OnlineCourseLookup
    from core.services.timetable_rooming import (
        _budget_value_for_placement,
        _build_rooming_budget_maps,
        _section_gender,
        get_board_gender,
        get_capacity_buffer,
        room_type_for_placement,
    )

    all_placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("board", "term_section")
        .order_by("id")
    )
    online_lookup = OnlineCourseLookup()
    physical = [
        placement
        for placement in all_placements
        if not online_lookup.is_online_course_for_board(
            placement.board, placement.term_section.course_code
        )
    ]
    physical_by_id = {placement.id: placement for placement in physical}
    moved_ids = sorted({int(value) for value in moved_physical_ids})
    missing_moved = sorted(set(moved_ids) - set(physical_by_id))
    if missing_moved:
        return {
            "status": "invalid_scope",
            "proven_optimal": False,
            "reason": f"moved physical placements not found: {missing_moved}",
            "attempts": [],
        }
    if not moved_ids:
        return {
            "status": "not_needed",
            "proven_optimal": True,
            "reason": "no moved physical placements",
            "attempts": [],
            "assignment": {},
            "objective": {"room_changes": 0, "capacity_waste": 0},
        }
    locked_moved = sorted(
        placement_id for placement_id in moved_ids if physical_by_id[placement_id].is_locked
    )
    if locked_moved:
        return {
            "status": "invalid_scope",
            "proven_optimal": False,
            "reason": f"moved placements unexpectedly locked: {locked_moved}",
            "attempts": [],
        }

    def overlaps(left, right) -> bool:
        return (
            str(left.day).upper() == str(right.day).upper()
            and _to_min(str(left.start_time)[:5]) < _to_min(str(right.end_time)[:5])
            and _to_min(str(left.end_time)[:5]) > _to_min(str(right.start_time)[:5])
        )

    component = set(moved_ids)
    while True:
        additions = {
            placement.id
            for placement in physical
            if placement.id not in component
            and any(
                overlaps(placement, physical_by_id[component_id])
                for component_id in sorted(component)
            )
        }
        if not additions:
            break
        component.update(additions)

    assigned_unlocked_component = {
        placement_id
        for placement_id in component
        if not physical_by_id[placement_id].is_locked
        and str(physical_by_id[placement_id].room or "").strip().upper() not in {"", "UNASSIGNED"}
    }
    scope_candidates = [
        ("moved_only", tuple(moved_ids)),
        (
            "overlap_component",
            tuple(sorted(set(moved_ids) | assigned_unlocked_component)),
        ),
    ]
    # Avoid solving the same scope twice when no assigned neighbour exists.
    unique_scopes: list[tuple[str, tuple[int, ...]]] = []
    seen_scopes: set[tuple[int, ...]] = set()
    for scope_name, scope_ids in scope_candidates:
        if scope_ids not in seen_scopes:
            unique_scopes.append((scope_name, scope_ids))
            seen_scopes.add(scope_ids)

    budgets = list(ScenarioSectionBudget.objects.filter(scenario_id=scenario_id))
    budget_maps = _build_rooming_budget_maps(budgets, get_capacity_buffer())
    rooms = list(Room.objects.all().order_by("room_code", "capacity", "id"))
    board_gender: dict[int, str] = {}

    def compatible_domain(placement) -> list[dict[str, Any]]:
        required_type = room_type_for_placement(placement, budget_maps=budget_maps)
        demand = _budget_value_for_placement(placement, budget_maps, "buffered", 40)
        if placement.board_id not in board_gender:
            board_gender[placement.board_id] = get_board_gender(placement.board_id)
        gender = _section_gender(placement.term_section.section) or board_gender[placement.board_id]
        programmes = {
            value.strip().upper()
            for value in str(placement.board.program or "").split(",")
            if value.strip()
        }
        by_code: dict[str, dict[str, Any]] = {}
        for room in rooms:
            room_programmes = {
                value.strip().upper()
                for value in str(room.department or "").split(",")
                if value.strip()
            }
            if str(room.room_type or "lecture").strip().lower() != required_type:
                continue
            if int(room.capacity or 0) < demand:
                continue
            if gender and str(room.section or "").strip().upper() != gender:
                continue
            if not (programmes & room_programmes):
                continue
            key = str(room.room_code or "").strip().upper()
            if not key:
                continue
            candidate = {
                "key": key,
                "room": str(room.room_code).strip(),
                "room_id": room.id,
                "capacity": int(room.capacity or 0),
                "demand": demand,
                "waste": int(room.capacity or 0) - demand,
            }
            incumbent = by_code.get(key)
            if incumbent is None or (candidate["waste"], candidate["room_id"]) < (
                incumbent["waste"],
                incumbent["room_id"],
            ):
                by_code[key] = candidate
        return [by_code[key] for key in sorted(by_code)]

    attempts: list[dict[str, Any]] = []
    solve_started = time.monotonic()
    for scope_name, scope_ids in unique_scopes:
        variable_set = set(scope_ids)
        variables = [physical_by_id[placement_id] for placement_id in scope_ids]
        fixed = [placement for placement in physical if placement.id not in variable_set]
        domains: dict[int, list[dict[str, Any]]] = {}
        empty_domains: list[int] = []
        for placement in variables:
            domain = [
                candidate
                for candidate in compatible_domain(placement)
                if not any(
                    str(fixed_placement.room or "").strip().upper() == candidate["key"]
                    and overlaps(placement, fixed_placement)
                    for fixed_placement in fixed
                    if str(fixed_placement.room or "").strip().upper() not in {"", "UNASSIGNED"}
                )
            ]
            domains[placement.id] = domain
            if not domain:
                empty_domains.append(placement.id)
        if empty_domains:
            attempts.append(
                {
                    "scope": scope_name,
                    "placement_ids": list(scope_ids),
                    "status": "infeasible_empty_domain",
                    "empty_domain_placement_ids": empty_domains,
                }
            )
            continue

        model = cp_model.CpModel()
        choice: dict[tuple[int, str], Any] = {}
        for placement in variables:
            for candidate in domains[placement.id]:
                choice[(placement.id, candidate["key"])] = model.NewBoolVar(
                    f"room_p{placement.id}_{len(choice)}"
                )
            model.Add(
                sum(choice[(placement.id, candidate["key"])] for candidate in domains[placement.id])
                == 1
            )

        for index, left in enumerate(variables):
            for right in variables[index + 1 :]:
                if not overlaps(left, right):
                    continue
                shared_codes = {candidate["key"] for candidate in domains[left.id]} & {
                    candidate["key"] for candidate in domains[right.id]
                }
                for room_key in sorted(shared_codes):
                    model.Add(choice[(left.id, room_key)] + choice[(right.id, room_key)] <= 1)

        change_terms = []
        waste_terms = []
        max_total_waste = 0
        for placement in variables:
            preferred = str(preferred_room_by_id.get(placement.id, "") or "").strip().upper()
            max_total_waste += max(candidate["waste"] for candidate in domains[placement.id])
            for candidate in domains[placement.id]:
                variable = choice[(placement.id, candidate["key"])]
                if candidate["key"] != preferred:
                    change_terms.append(variable)
                if candidate["waste"]:
                    waste_terms.append(candidate["waste"] * variable)
        changes_expr = sum(change_terms) if change_terms else 0
        waste_expr = sum(waste_terms) if waste_terms else 0
        change_weight = max_total_waste + 1
        model.Minimize(change_weight * changes_expr + waste_expr)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.1, float(time_limit_seconds))
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        status = solver.Solve(model)
        status_name = solver.StatusName(status).lower()
        attempt = {
            "scope": scope_name,
            "placement_ids": list(scope_ids),
            "status": status_name,
            "variable_count": len(variables),
            "domain_sizes": {
                str(placement.id): len(domains[placement.id]) for placement in variables
            },
        }
        attempts.append(attempt)
        if status == cp_model.INFEASIBLE:
            continue
        if status != cp_model.OPTIMAL:
            return {
                "status": "not_proven_optimal",
                "proven_optimal": False,
                "reason": f"CP-SAT returned {status_name}",
                "attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - solve_started, 3),
            }

        assignment: dict[int, str] = {}
        total_changes = 0
        total_waste = 0
        for placement in variables:
            selected = next(
                candidate
                for candidate in domains[placement.id]
                if solver.Value(choice[(placement.id, candidate["key"])])
            )
            assignment[placement.id] = selected["room"]
            total_waste += selected["waste"]
            preferred = str(preferred_room_by_id.get(placement.id, "") or "").strip().upper()
            total_changes += int(selected["key"] != preferred)

        write_time = timezone.now()
        for placement_id in sorted(assignment):
            updated = SectionPlacement.objects.filter(
                id=placement_id, board__scenario_id=scenario_id
            ).update(room=assignment[placement_id], updated_at=write_time)
            if updated != 1:
                return {
                    "status": "write_failed",
                    "proven_optimal": False,
                    "reason": f"failed to update placement {placement_id}",
                    "attempts": attempts,
                }
        return {
            "status": "optimal",
            "proven_optimal": True,
            "scope": scope_name,
            "scope_placement_ids": list(scope_ids),
            "assignment": {str(key): assignment[key] for key in sorted(assignment)},
            "objective": {
                "room_changes": total_changes,
                "capacity_waste": total_waste,
            },
            "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - solve_started, 3),
        }

    return {
        "status": "infeasible",
        "proven_optimal": False,
        "reason": "no H9/H11-H14-feasible assignment in the affected component",
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - solve_started, 3),
    }


def compact_instructor_schedules(scenario_id: int) -> dict:
    """Compact instructor days for a scenario. Persists accepted relocations.
    Returns a full before/after audit report. No-op when the flag is off."""
    report: dict = {"enabled": False}
    if not is_instructor_compaction_enabled():
        return report
    report["enabled"] = True

    from core.models import SectionPlacement, TermSectionMeeting, TimetableScenario
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_locked_section_ids_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_rooming import assign_rooms_to_board
    from core.services.timetable_student_assignment import (
        TI_SOFT,
        is_tiered_score,
        reserve_used_of,
    )
    from core.services.timetable_validation import blocked_slot_keys

    cfg = get_instructor_compaction_config()
    cap = get_instructor_daily_cap()
    if cap <= 0:
        report["note"] = "invalid instructor daily cap; compaction skipped"
        return report

    scenario = TimetableScenario.objects.get(id=scenario_id)
    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("term_section", "board")
        .order_by("term_section_id", "day", "start_time", "id")
    )

    # One SectionState is keyed by term section, not board. Replicas of one
    # section across several boards would therefore be double-counted by the
    # search and a dict mapping could silently retain only the last placement.
    # Until the optimiser has an explicit shared-meeting representation, reject
    # this shape deterministically before evaluating or writing anything.
    placements_by_section: dict[int, list[object]] = defaultdict(list)
    for placement in placements:
        placements_by_section[placement.term_section_id].append(placement)
    shared_sections: list[dict[str, Any]] = []
    duplicate_logical_keys = 0
    for term_section_id, section_placements in sorted(placements_by_section.items()):
        board_ids = sorted({placement.board_id for placement in section_placements})
        if len(board_ids) <= 1:
            continue
        logical_keys: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for placement in section_placements:
            logical_keys[
                (
                    str(placement.day).upper(),
                    str(placement.start_time)[:5],
                    str(placement.end_time)[:5],
                )
            ].append(placement.id)
        replicas = [
            {
                "day": key[0],
                "start": key[1],
                "end": key[2],
                "placement_ids": sorted(ids),
            }
            for key, ids in sorted(logical_keys.items())
            if len(ids) > 1
        ]
        duplicate_logical_keys += len(replicas)
        term_section = section_placements[0].term_section
        shared_sections.append(
            {
                "term_section_id": term_section_id,
                "section_id": (
                    f"{term_section.course_key or term_section.course_code}_{term_section.section}"
                ),
                "board_ids": board_ids,
                "placement_ids": sorted(placement.id for placement in section_placements),
                "replicated_logical_meetings": replicas,
            }
        )
    if shared_sections:
        reason = (
            "shared multi-board term sections are not supported by instructor "
            "compaction; no placements were changed"
        )
        report.update(
            {
                "note": reason,
                "coverage": {
                    "placements_total": len(placements),
                    "unique_term_sections": len(placements_by_section),
                    "shared_multi_board_sections": len(shared_sections),
                    "duplicate_logical_meeting_keys": duplicate_logical_keys,
                },
                "unsupported_shared_sections": shared_sections,
                "search": {
                    "moves_evaluated": 0,
                    "moves_accepted": 0,
                    "rounds_used": 0,
                    "timed_out": False,
                    "elapsed_seconds": 0.0,
                },
                "persistence": {
                    "committed": False,
                    "rolled_back": False,
                    "skipped": True,
                    "reason": reason,
                    "rooming": {},
                },
                "relocations": [],
            }
        )
        return report

    states = build_section_states_for_scenario(scenario_id)
    if not states:
        report["note"] = "no placements"
        return report
    sbi = {s.section_id: s for s in states}
    # Empty profiles are fine: the student gates simply become vacuous and the
    # pass optimises instructor idle freely (still respecting the daily cap).
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id) if profiles else {}
    cap_map = build_section_instructor_map_for_scenario(scenario_id)
    course_tiers = (
        build_course_tier_map_for_scenario(scenario_id) if is_tiered_objective_enabled() else None
    )
    linked_sections = {sid for sid in sbi if cap_map.get(sid)}
    linked_instructors = {iid for sid in linked_sections for iid in cap_map.get(sid, frozenset())}
    total_meetings = sum(len(section.meetings) for section in sbi.values())
    linked_meetings = sum(len(sbi[sid].meetings) for sid in linked_sections)
    report["coverage"] = {
        "instructors": len(linked_instructors),
        "sections_with_instructor": len(linked_sections),
        "sections_total": len(sbi),
        "meetings_with_instructor": linked_meetings,
        "meetings_total": total_meetings,
        "meeting_coverage_percent": round(
            100.0 * linked_meetings / total_meetings if total_meetings else 0.0, 1
        ),
    }
    report["limitations"] = [
        (
            f"Instructor metrics cover {linked_meetings}/{total_meetings} scheduled meetings; "
            "unlinked meetings cannot be checked or compacted."
        ),
        "No per-instructor availability or preferred-day data exists, so it cannot be optimised.",
        (
            "The day minimum is a lower bound from ceil(session_count/daily_cap) and H2; "
            "blocked slots, co-teaching, student conflicts and rooms can make it unattainable."
        ),
    ]
    if not cap_map:
        report["note"] = "no instructor assignments (links off?)"
        return report
    locked = build_locked_section_ids_for_scenario(scenario_id)
    tier = {sid: p.risk_tier for sid, p in profiles.items()}
    code_of = {sid: s.course_code for sid, s in sbi.items()}

    lec = [
        (_to_min(s["start"]), _to_min(s["end"])) for s in (scenario.slot_config or DEFAULT_SLOTS)
    ]
    lab = [
        (_to_min(s["start"]), _to_min(s["end"]))
        for s in (scenario.lab_slot_config or DEFAULT_LAB_SLOTS)
    ]
    blocked = {(d.upper(), _to_min(st)) for (d, st) in blocked_slot_keys(scenario.blocked_slots)}

    # ── DB ↔ in-memory mapping for persistence ──
    def _sid(p) -> str:
        ts = p.term_section
        return f"{ts.course_key or ts.course_code}_{ts.section}"

    placement_of: dict[tuple[str, int, int, int], list[object]] = defaultdict(list)
    for p in placements:
        sid = _sid(p)
        di = WEEKDAYS.index(p.day.upper()) if p.day.upper() in WEEKDAYS else -1
        placement_of[(sid, di, _to_min(p.start_time), _to_min(p.end_time))].append(p)

    # ── Metrics ──
    def evaluate():
        return evaluate_generated_timetable_candidate(
            "compaction",
            states,
            profiles,
            rigidity,
            section_instructor_ids=cap_map,
            course_tiers=course_tiers,
        )

    def student_metrics(res):
        per = {sid: st.total_gap_minutes for sid, st in res.assignment_states.items()}
        total = sum(per.values())
        by_tier: dict = defaultdict(int)
        for s, g in per.items():
            by_tier[tier.get(s)] += g
        return per, total, by_tier

    def instr_metrics():
        return _compute_instructor_compaction_metrics(sbi, cap_map, cap)

    # ── Occupancy (kept in sync during search) ──
    instr_slots: dict = defaultdict(set)
    course_slots: dict = defaultdict(set)
    section_days: dict = defaultdict(set)
    for sid, s in sbi.items():
        for m in s.meetings:
            section_days[sid].add(m.day)
            course_slots[code_of[sid]].add((m.day, m.start_min, m.end_min))
            for iid in cap_map.get(sid, frozenset()):
                instr_slots[iid].add((m.day, m.start_min, m.end_min))

    def _relocation_moves(sid, midx, iids):
        """Neighbourhood generator: feasible (day2, s2, e2) targets for one
        session, honouring all-diff-days, blocked slots, instructor clash,
        same-course separation and the daily cap. (Swap/chain go here later.)"""
        meeting = sbi[sid].meetings[midx]
        dur = meeting.end_min - meeting.start_min
        slots = lab if dur > 75 else lec
        others = section_days[sid] - {meeting.day}
        course = code_of[sid]
        for day2 in range(len(WEEKDAYS)):
            same_day = day2 == meeting.day
            if not same_day and day2 in others:
                continue
            if not same_day and any(
                sum(1 for (d, _s, _e) in instr_slots[iid] if d == day2) >= cap for iid in iids
            ):
                continue
            for s2, e2 in slots:
                # H1 is exact, including on custom grids: relocation may change
                # day/start but never the meeting duration.
                if e2 - s2 != dur:
                    continue
                if same_day and s2 == meeting.start_min:
                    continue
                if (WEEKDAYS[day2], s2) in blocked:
                    continue
                if any(_interval_busy(instr_slots[iid], day2, s2, e2) for iid in iids):
                    continue
                if _interval_busy(course_slots[course], day2, s2, e2):
                    continue
                yield day2, s2, e2

    _NEIGHBOURHOODS = [_relocation_moves]  # swap/chain/LNS slot in here

    def _apply(sid, midx, day2, s2, e2, iids):
        old = sbi[sid].meetings[midx]
        sbi[sid].meetings[midx] = SectionMeeting(day2, s2, e2)
        section_days[sid].discard(old.day)
        section_days[sid].add(day2)
        course_slots[code_of[sid]].discard((old.day, old.start_min, old.end_min))
        course_slots[code_of[sid]].add((day2, s2, e2))
        for iid in iids:
            instr_slots[iid].discard((old.day, old.start_min, old.end_min))
            instr_slots[iid].add((day2, s2, e2))
        return old

    def _revert(sid, midx, old, day2, s2, e2, iids):
        course_slots[code_of[sid]].discard((day2, s2, e2))
        course_slots[code_of[sid]].add((old.day, old.start_min, old.end_min))
        section_days[sid].discard(day2)
        section_days[sid].add(old.day)
        for iid in iids:
            instr_slots[iid].discard((day2, s2, e2))
            instr_slots[iid].add((old.day, old.start_min, old.end_min))
        sbi[sid].meetings[midx] = old

    # ── Baseline ──
    base_eval = evaluate()
    base_score = tuple(base_eval.lexicographic_score)
    base_per, base_total_gap, base_by_tier = student_metrics(base_eval)
    base_instr = instr_metrics()
    orig_pos = {
        sid: [(m.day, m.start_min, m.end_min) for m in s.meetings] for sid, s in sbi.items()
    }

    gap_ceiling = base_total_gap * (1.0 + cfg["gap_budget"])

    def gates_ok(res, idle_saved, gap_added):
        sc = tuple(res.lexicographic_score)
        # Positions 0-3 are the hard feasibility block in BOTH layouts (legacy:
        # tier_a/unres/unassigned/clash; tiered: high-risk/clash/T1/T2-over), so
        # this slice guards feasibility either way. Reserve is idx 5 legacy /
        # idx 6 tiered — reserve_used_of resolves it. In the tiered layout idx 5
        # is soft_unresolved (T3 + T2-within-tolerance): guard it too so
        # relocating instructor sessions can never strand a soft-tier student
        # that the legacy [5] slot used to protect via reserve.
        if sc[0:4] > base_score[0:4] or reserve_used_of(sc) > reserve_used_of(base_score):
            return False
        if is_tiered_score(sc) and sc[TI_SOFT] > base_score[TI_SOFT]:
            return False
        per, total, by_tier = student_metrics(res)
        if total > gap_ceiling:
            return False
        if by_tier.get(RiskTier.A, 0) > base_by_tier.get(RiskTier.A, 0):
            return False
        if by_tier.get(RiskTier.B, 0) > base_by_tier.get(RiskTier.B, 0):
            return False
        for s, g in per.items():
            if g - base_per.get(s, 0) > cfg["per_student_cap"]:
                return False
        # Trade alert: if this move costs student spread, require enough payoff.
        if gap_added > 0 and idle_saved < cfg["trade_ratio"] * gap_added:
            return False
        return True

    movable = [
        (sid, mi)
        for sid in sbi
        for mi in range(len(sbi[sid].meetings))
        if cap_map.get(sid) and sid not in locked
    ]

    cur_instr = base_instr
    cur_eval = base_eval
    visited: set = set()
    moves_evaluated = moves_accepted = 0
    max_moves = max(20, len(movable) * 4)

    def _signature():
        return frozenset((sid, m.day, m.start_min) for sid, s in sbi.items() for m in s.meetings)

    visited.add(_signature())

    # Wall-clock budget: workday-first ranking front-loads the largest excess.
    # If time runs out, the safe accepted prefix is retained.
    t0 = time.monotonic()
    budget = cfg["time_budget"]

    def _over_budget() -> bool:
        return budget > 0 and (time.monotonic() - t0) > budget

    timed_out = False
    rounds_used = 0
    for _round in range(cfg["max_rounds"]):
        if moves_accepted >= max_moves:
            break
        if _over_budget():
            timed_out = True
            break
        rounds_used += 1
        per_instructor = cur_instr["per_instructor"]
        target_iids = [
            iid
            for iid, row in sorted(
                per_instructor.items(),
                key=lambda item: (
                    -item[1]["excess_days"],
                    -item[1]["physical_span"],
                    -item[1]["physical_idle"],
                    str(item[0]),
                ),
            )
            if row["excess_days"] > 0 or row["physical_idle"] > 0
        ]
        if not target_iids:
            break
        target_set = set(target_iids)
        target_rank = {iid: rank for rank, iid in enumerate(target_iids)}
        round_movable = sorted(
            movable,
            key=lambda item: min(
                (target_rank[iid] for iid in cap_map.get(item[0], ()) if iid in target_set),
                default=len(target_rank),
            ),
        )
        _, cur_gap, _ = student_metrics(cur_eval)
        cur_tuple = _instructor_compaction_objective(cur_instr, cur_gap - base_total_gap)
        best = None
        for sid, midx in round_movable:
            iids = cap_map.get(sid, frozenset())
            if not (set(iids) & target_set):
                continue
            for gen in _NEIGHBOURHOODS:
                for day2, s2, e2 in gen(sid, midx, iids):
                    old = _apply(sid, midx, day2, s2, e2, iids)
                    sig = _signature()
                    if sig not in visited:
                        res = evaluate()
                        moves_evaluated += 1
                        im = instr_metrics()
                        _, tg, _ = student_metrics(res)
                        idle_saved = cur_instr["total"] - im["total"]
                        gap_added = tg - cur_gap
                        if gates_ok(res, idle_saved, gap_added):
                            it = _instructor_compaction_objective(im, tg - base_total_gap)
                            if it < cur_tuple and (best is None or it < best[0]):
                                best = (it, sid, midx, day2, s2, e2, res, im, sig)
                    _revert(sid, midx, old, day2, s2, e2, iids)
                    if _over_budget():
                        timed_out = True
                        break
                if timed_out:
                    break
            if timed_out:
                break
        if best is None:
            break
        _, sid, midx, day2, s2, e2, res, im, sig = best
        _apply(sid, midx, day2, s2, e2, cap_map.get(sid, frozenset()))
        visited.add(sig)
        cur_instr, cur_eval = im, res
        moves_accepted += 1

    search_elapsed_seconds = time.monotonic() - t0
    if budget > 0 and search_elapsed_seconds > budget:
        timed_out = True

    # ── Persist accepted relocations ──
    touched_boards: set[int] = set()
    relocations: list[dict[str, Any]] = []
    planned_changes: list[dict[str, Any]] = []
    unmapped_changes: list[str] = []
    for sid, section in sbi.items():
        for index, meeting in enumerate(section.meetings):
            old_day, old_start, old_end = orig_pos[sid][index]
            if (meeting.day, meeting.start_min, meeting.end_min) == (
                old_day,
                old_start,
                old_end,
            ):
                continue
            candidates = placement_of.get((sid, old_day, old_start, old_end), [])
            if len(candidates) != 1:
                unmapped_changes.append(
                    f"{sid}:{WEEKDAYS[old_day]}:{_hhmm(old_start)}-"
                    f"{_hhmm(old_end)} (matches={len(candidates)})"
                )
                continue
            placement = candidates[0]
            planned_changes.append(
                {
                    "sid": sid,
                    "meeting": meeting,
                    "old_day": old_day,
                    "old_start": old_start,
                    "old_end": old_end,
                    "placement": placement,
                }
            )
            touched_boards.add(placement.board_id)

    expected_update_mapping = [
        {
            "placement_id": change["placement"].id,
            "term_section_id": change["placement"].term_section_id,
            "from": {
                "day": WEEKDAYS[change["old_day"]],
                "start": _hhmm(change["old_start"]),
                "end": _hhmm(change["old_end"]),
            },
            "to": {
                "day": WEEKDAYS[change["meeting"].day],
                "start": _hhmm(change["meeting"].start_min),
                "end": _hhmm(change["meeting"].end_min),
            },
        }
        for change in planned_changes
    ]
    persistence = {
        "committed": not planned_changes,
        "rolled_back": False,
        "reason": "",
        "rooming": {},
        "rooming_attempted": [],
        "expected_update_mapping": expected_update_mapping,
    }

    class _UnsafePersistence(RuntimeError):
        pass

    def _restore_in_memory_candidate() -> None:
        nonlocal cur_instr, cur_eval, moves_accepted, relocations
        for section_id, positions in orig_pos.items():
            sbi[section_id].meetings = [
                SectionMeeting(day, start, end) for day, start, end in positions
            ]
        cur_instr = base_instr
        cur_eval = base_eval
        moves_accepted = 0
        relocations = []

    if unmapped_changes:
        persistence.update(
            {
                "committed": False,
                "rolled_back": True,
                "reason": "could not map candidate moves to placements: "
                + ", ".join(unmapped_changes),
                "candidate_moves_rejected": moves_accepted,
            }
        )
        _restore_in_memory_candidate()
        planned_changes = []
    elif planned_changes:
        moved_physical_ids = {
            change["placement"].id for change in planned_changes if not sbi[change["sid"]].is_online
        }
        moved_term_section_ids = {change["placement"].term_section_id for change in planned_changes}
        preferred_room_by_id = {placement.id: str(placement.room or "") for placement in placements}
        candidate_moves_accepted = moves_accepted
        candidate_relocations = [
            {
                "placement_id": change["placement"].id,
                "section": (f"{code_of[change['sid']]} {change['sid'].split('_')[-1]}"),
                "from": (f"{WEEKDAYS[change['old_day']]} {_hhmm(change['old_start'])}"),
                "to": (f"{WEEKDAYS[change['meeting'].day]} {_hhmm(change['meeting'].start_min)}"),
            }
            for change in planned_changes
        ]
        try:
            hard_before = _scenario_persistence_hard_metrics(scenario_id, cap)
            persistence["hard_constraints"] = {"before": hard_before}
            with transaction.atomic():
                moved_ids = [change["placement"].id for change in planned_changes]
                locked_rows = {
                    placement.id: placement
                    for placement in SectionPlacement.objects.select_for_update()
                    .filter(id__in=moved_ids, board__scenario_id=scenario_id)
                    .order_by("id")
                }
                if set(locked_rows) != set(moved_ids):
                    raise _UnsafePersistence(
                        "one or more planned placements disappeared before persistence"
                    )

                # Verify the mapping is still current, then vacate every unique
                # key before writing any final key. The unique staging day makes
                # swaps/cycles safe and remains invisible outside the transaction.
                for change in planned_changes:
                    placement = locked_rows[change["placement"].id]
                    persisted = (
                        str(placement.day).upper(),
                        _to_min(str(placement.start_time)[:5]),
                        _to_min(str(placement.end_time)[:5]),
                    )
                    expected = (
                        WEEKDAYS[change["old_day"]],
                        change["old_start"],
                        change["old_end"],
                    )
                    if persisted != expected:
                        raise _UnsafePersistence(
                            "placement changed concurrently before compaction "
                            f"(placement id {placement.id})"
                        )
                    updated = SectionPlacement.objects.filter(id=placement.id).update(
                        day=f"__COMPACTION_STAGE_{placement.id}__",
                        start_time="00:00",
                        end_time="00:01",
                        room="",
                    )
                    if updated != 1:
                        raise _UnsafePersistence(f"failed to stage placement id {placement.id}")

                for change in planned_changes:
                    placement_id = change["placement"].id
                    meeting = change["meeting"]
                    updated = SectionPlacement.objects.filter(id=placement_id).update(
                        day=WEEKDAYS[meeting.day],
                        start_time=_hhmm(meeting.start_min),
                        end_time=_hhmm(meeting.end_min),
                        room="",
                        updated_at=timezone.now(),
                    )
                    if updated != 1:
                        raise _UnsafePersistence(f"failed to write placement id {placement_id}")

                for board_id in sorted(touched_boards):
                    persistence["rooming_attempted"].append(board_id)
                    persistence["rooming"][board_id] = assign_rooms_to_board(
                        board_id, respect_locked=True
                    )

                greedy_room_validation = _validate_moved_room_compatibility(
                    scenario_id, moved_physical_ids
                )
                greedy_room_metrics = _scenario_persistence_room_metrics(scenario_id)
                persistence["greedy_room_validation"] = greedy_room_validation
                persistence["greedy_room_metrics"] = greedy_room_metrics
                fallback_reasons: list[str] = []
                if not greedy_room_validation["valid"]:
                    fallback_reasons.append("moved H11-H14/unassigned validation failed")
                if greedy_room_metrics["h9_room_clashes"] > hard_before["h9_room_clashes"]:
                    fallback_reasons.append("H9 room clashes increased")
                if (
                    greedy_room_metrics["physical_unassigned_rooms"]
                    > hard_before["physical_unassigned_rooms"]
                ):
                    fallback_reasons.append("physical unassigned rooms increased")

                if fallback_reasons:
                    exact_room_fallback = _exact_repair_affected_rooms(
                        scenario_id,
                        moved_physical_ids,
                        preferred_room_by_id,
                    )
                    exact_room_fallback["trigger_reasons"] = fallback_reasons
                    persistence["exact_room_fallback"] = exact_room_fallback
                    if not exact_room_fallback.get("proven_optimal"):
                        unroomed_moved = sorted(
                            violation["placement_id"]
                            for violation in greedy_room_validation["violations"]
                            if not str(violation.get("room", "") or "").strip()
                            or str(violation.get("room", "")).strip().upper() == "UNASSIGNED"
                        )
                        if unroomed_moved:
                            raise _UnsafePersistence(
                                "room repair could not assign every moved physical "
                                f"meeting (placement ids: {unroomed_moved}); exact "
                                f"fallback status={exact_room_fallback['status']}"
                            )
                        raise _UnsafePersistence(
                            "exact affected-period room fallback did not prove a "
                            f"safe optimum (status={exact_room_fallback['status']})"
                        )
                else:
                    persistence["exact_room_fallback"] = {
                        "status": "not_needed",
                        "proven_optimal": True,
                        "trigger_reasons": [],
                    }

                room_validation = _validate_moved_room_compatibility(
                    scenario_id, moved_physical_ids
                )
                room_metrics_after_repair = _scenario_persistence_room_metrics(scenario_id)
                persistence["room_validation"] = room_validation
                persistence["room_metrics_after_repair"] = room_metrics_after_repair
                if not room_validation["valid"]:
                    unroomed_moved = sorted(
                        violation["placement_id"]
                        for violation in room_validation["violations"]
                        if not str(violation.get("room", "") or "").strip()
                        or str(violation.get("room", "")).strip().upper() == "UNASSIGNED"
                    )
                    if unroomed_moved:
                        raise _UnsafePersistence(
                            "room repair could not assign every moved physical meeting "
                            f"(placement ids: {unroomed_moved})"
                        )
                    invalid_ids = sorted(
                        violation["placement_id"] for violation in room_validation["violations"]
                    )
                    raise _UnsafePersistence(
                        "H11-H14 room compatibility failed for moved physical "
                        f"placements {invalid_ids}"
                    )
                room_regressions = [
                    f"{key} increased {hard_before[key]}->{room_metrics_after_repair[key]}"
                    for key in ("h9_room_clashes", "physical_unassigned_rooms")
                    if room_metrics_after_repair[key] > hard_before[key]
                ]
                if room_regressions:
                    raise _UnsafePersistence("; ".join(room_regressions))

                from core.services.timetable_board_persistence import (
                    sync_meetings_from_placements,
                )

                sync_result = sync_meetings_from_placements(scenario_id)
                persistence["meeting_sync"] = {
                    "sections_synced": sync_result.sections_synced,
                    "meetings_written": sync_result.meetings_written,
                    "meetings_deleted": sync_result.meetings_deleted,
                }

                mapping_checks: list[dict[str, Any]] = []
                mapping_mismatches: list[int] = []
                for term_section_id in sorted(moved_term_section_ids):
                    expected_rows = sorted(
                        {
                            (
                                str(day),
                                str(start)[:5],
                                str(end)[:5],
                                str(room or ""),
                            )
                            for day, start, end, room in SectionPlacement.objects.filter(
                                board__scenario_id=scenario_id,
                                term_section_id=term_section_id,
                            ).values_list("day", "start_time", "end_time", "room")
                        }
                    )
                    actual_rows = sorted(
                        {
                            (
                                str(day),
                                str(start)[:5],
                                str(end)[:5],
                                str(room or ""),
                            )
                            for day, start, end, room in TermSectionMeeting.objects.filter(
                                term_section_id=term_section_id
                            ).values_list("day", "start_time", "end_time", "room")
                        }
                    )
                    matches = actual_rows == expected_rows
                    mapping_checks.append(
                        {
                            "term_section_id": term_section_id,
                            "expected": expected_rows,
                            "actual": actual_rows,
                            "matches": matches,
                        }
                    )
                    if not matches:
                        mapping_mismatches.append(term_section_id)
                persistence["meeting_mapping_checks"] = mapping_checks
                if mapping_mismatches:
                    raise _UnsafePersistence(
                        "TermSectionMeeting sync did not produce the expected "
                        f"placement mapping for term sections {mapping_mismatches}"
                    )

                hard_after = _scenario_persistence_hard_metrics(scenario_id, cap)
                regressions = [
                    f"{key} increased {hard_before[key]}->{hard_after[key]}"
                    for key in _PERSISTENCE_HARD_KEYS
                    if hard_after[key] > hard_before[key]
                ]
                persistence["hard_constraints"].update(
                    {"after": hard_after, "regressions": regressions}
                )
                if regressions:
                    raise _UnsafePersistence("; ".join(regressions))
            persistence["committed"] = True
            relocations = candidate_relocations
        except Exception as exc:
            # The DB transaction has already restored placements, meetings and
            # rooms. Restore the in-memory candidate as well so every reported
            # after metric describes the timetable that actually remains.
            if isinstance(exc, _UnsafePersistence):
                reason = str(exc)
                logger.warning(
                    "Instructor compaction persistence rejected for scenario %d: %s",
                    scenario_id,
                    reason,
                )
            else:
                reason = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Instructor compaction persistence failed for scenario %d",
                    scenario_id,
                )
            rooming_was_attempted = bool(persistence["rooming_attempted"])
            persistence.update(
                {
                    "committed": False,
                    "rolled_back": True,
                    "reason": reason,
                    "exception_type": type(exc).__name__,
                    "candidate_moves_rejected": candidate_moves_accepted,
                    "rooming": {},
                    "rooming_rolled_back": rooming_was_attempted,
                    "rooming_results_discarded": rooming_was_attempted,
                }
            )
            if "meeting_sync" in persistence:
                persistence["meeting_sync_rolled_back"] = True
                persistence["meeting_sync"] = {}
            if "room_validation" in persistence:
                persistence["room_validation"]["rolled_back"] = True
            if "exact_room_fallback" in persistence:
                persistence["exact_room_fallback"]["rolled_back"] = True
            if "hard_constraints" in persistence:
                persistence["hard_constraints"]["rolled_back"] = True
            _restore_in_memory_candidate()

    # ── Audit report ──
    fin_per, fin_total_gap, fin_by_tier = student_metrics(cur_eval)
    max_add = max((fin_per.get(s, 0) - base_per.get(s, 0) for s in base_per), default=0)
    worsened = [s for s in base_per if fin_per.get(s, 0) > base_per.get(s, 0)]
    improved_students = [s for s in base_per if fin_per.get(s, 0) < base_per.get(s, 0)]
    base_ip = base_instr["per_instructor"]
    final_ip = cur_instr["per_instructor"]
    all_iids = sorted(set(base_ip) | set(final_ip), key=str)
    per_instructor_report = [
        {
            "instructor_id": iid,
            "session_count": final_ip[iid]["session_count"],
            "lower_bound_days": final_ip[iid]["lower_bound_days"],
            "working_days_before": base_ip[iid]["working_days"],
            "working_days_after": final_ip[iid]["working_days"],
            "excess_days_before": base_ip[iid]["excess_days"],
            "excess_days_after": final_ip[iid]["excess_days"],
            "campus_days_before": base_ip[iid]["campus_days"],
            "campus_days_after": final_ip[iid]["campus_days"],
            "physical_span_before": base_ip[iid]["physical_span"],
            "physical_span_after": final_ip[iid]["physical_span"],
            "physical_idle_before": base_ip[iid]["physical_idle"],
            "physical_idle_after": final_ip[iid]["physical_idle"],
        }
        for iid in all_iids
    ]
    report.update(
        {
            "neighbourhood_version": "relocation-v2-workdays",
            "protected": {
                "feasibility_before": list(base_score[0:4]),
                "feasibility_after": list(tuple(cur_eval.lexicographic_score)[0:4]),
                "reserve_before": reserve_used_of(base_score),
                "reserve_after": reserve_used_of(tuple(cur_eval.lexicographic_score)),
            },
            "student_impact": {
                "total_gap_before": base_total_gap,
                "total_gap_after": fin_total_gap,
                "total_gap_delta": fin_total_gap - base_total_gap,
                "budget_ceiling": int(gap_ceiling),
                "max_added_gap_any_student": max_add,
                "students_worsened": len(worsened),
                "students_improved": len(improved_students),
                "tierA_gap_delta": fin_by_tier.get(RiskTier.A, 0) - base_by_tier.get(RiskTier.A, 0),
                "graduating_gap_delta": fin_by_tier.get(RiskTier.B, 0)
                - base_by_tier.get(RiskTier.B, 0),
            },
            "instructor_impact": {
                "working_days_before": base_instr["total_working_days"],
                "working_days_after": cur_instr["total_working_days"],
                "working_days_saved": base_instr["total_working_days"]
                - cur_instr["total_working_days"],
                "lower_bound_days": base_instr["total_lower_bound_days"],
                "max_excess_days_before": base_instr["max_excess_days"],
                "max_excess_days_after": cur_instr["max_excess_days"],
                "total_excess_days_before": base_instr["total_excess_days"],
                "total_excess_days_after": cur_instr["total_excess_days"],
                "campus_days_before": base_instr["total_campus_days"],
                "campus_days_after": cur_instr["total_campus_days"],
                "worst_weekly_span_before": base_instr["worst_weekly_span"],
                "worst_weekly_span_after": cur_instr["worst_weekly_span"],
                "total_physical_span_before": base_instr["total_span"],
                "total_physical_span_after": cur_instr["total_span"],
                "total_idle_before": base_instr["total"],
                "total_idle_after": cur_instr["total"],
                "total_idle_saved": base_instr["total"] - cur_instr["total"],
                "largest_hole_before": base_instr["largest"],
                "largest_hole_after": cur_instr["largest"],
                "over90_before": base_instr["over90"],
                "over90_after": cur_instr["over90"],
                "worst_weekly_before": base_instr["worst_weekly"],
                "worst_weekly_after": cur_instr["worst_weekly"],
                "instructors_improved": sum(
                    1
                    for iid in all_iids
                    if final_ip[iid]["physical_idle"] < base_ip[iid]["physical_idle"]
                ),
                "instructors_worsened": sum(
                    1
                    for iid in all_iids
                    if final_ip[iid]["physical_idle"] > base_ip[iid]["physical_idle"]
                ),
                "working_days_improved": sum(
                    1
                    for iid in all_iids
                    if final_ip[iid]["working_days"] < base_ip[iid]["working_days"]
                ),
                "working_days_worsened": sum(
                    1
                    for iid in all_iids
                    if final_ip[iid]["working_days"] > base_ip[iid]["working_days"]
                ),
                "per_instructor": per_instructor_report,
            },
            "search": {
                "moves_evaluated": moves_evaluated,
                "moves_accepted": moves_accepted,
                "rounds_used": rounds_used,
                "residual_largest_hole": cur_instr["largest"],
                "timed_out": timed_out,
                "elapsed_seconds": round(search_elapsed_seconds, 3),
            },
            "persistence": persistence,
            "relocations": relocations,
        }
    )
    logger.info(
        "Instructor compaction (scenario %d): %d moves, working days %d->%d, "
        "idle %d->%d min, student gap %d->%d",
        scenario_id,
        moves_accepted,
        base_instr["total_working_days"],
        cur_instr["total_working_days"],
        base_instr["total"],
        cur_instr["total"],
        base_total_gap,
        fin_total_gap,
    )
    return report
