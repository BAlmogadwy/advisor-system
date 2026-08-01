"""Scenario-wide exact room assignment + shortfall decomposition — preview only.

Today rooms are filled greedily, per board (``assign_rooms_to_board``). The shared
room pool is never coordinated across boards, so the greedy can strand meetings it
had no global reason to strand, and the publish gate then *rejects* the result
without ever explaining how far off it was.

This module builds the scenario's physical meetings into an exact CP-SAT room
assignment (``_solve_relaxed``, a lexicographic ``(unroomed, capacity shortfall,
room changes, excess capacity)`` solve over whole meetings) and returns a *report*:

* whether every physical meeting can be roomed with zero capacity shortfall;
* if not, the shortfall split into **unavoidable** (no compatible room is big
  enough, ignoring time collisions — an inventory fact) versus **congestion**
  (a big-enough room exists but is busy — an algorithmically reducible fact);
* the **no-compatible-room** meetings (empty type∩gender∩department domain — a
  structural gap no room solver can close, e.g. a lab meeting on a board whose
  programme has no lab rooms);
* **oversubscribed intervals** (time windows where more meetings are active than
  there are rooms to serve them — a room-count contention, a *time* problem);
* how many rooms would change from the currently-persisted assignment.

The exact-room-assignment *idea* (whole-meeting CP-SAT, lexicographic capacity /
change / waste) is harvested from the ``New project77`` research engine, but the
solve here is a purpose-built **relaxation**: the engine's ``solve_exact_room_
assignment`` is all-or-nothing (one unroomable meeting → the whole model is
INFEASIBLE), which is the wrong contract for fixed-time rooming, so this module
lets a meeting go unroomed at top priority and keeps no dependency on that tree.

It is deliberately **read-only**: it never writes ``SectionPlacement.room`` and is
not part of any publish gate. Adoption is a separate, explicit decision (the
``apply`` path, gated by ``TIMETABLE_EXACT_ROOMING_ENABLED``, is built on top of
this once the numbers are trusted).

Design decisions forced by the code (see the recon in the session notes):

* **The demand unit is the physical meeting** ``(term_section, day, start, end)``,
  not the ``SectionPlacement`` row. A section shared across boards at the same
  slot is *one* physical class that must get *one* room written to *every* row —
  grouping this way also avoids doubling ``TermSectionMeeting`` rows on persist.
* **The compatible-room domain mirrors the publish-gate auditor**
  (``validate_physical_room_compatibility``), not the greedy: labs are
  capacity-checked with buffered demand, room type/gender/department are
  normalised. Producing a board the auditor would reject is not a "pass".
* **Capacity is not a hard domain filter here.** Rooms are admitted on
  type∩gender∩department; the solver then *minimises* the capacity shortfall
  (``allow_capacity_shortfall=True``). That keeps a too-small-room meeting as a
  measurable shortfall rather than collapsing the whole model to INFEASIBLE, and
  lets the decomposition separate "no room at all" from "no big-enough room".
* **Online meetings need no room** and are excluded (online-ness is per
  ``(course_code, board)``).
* Room identity is the ``room_code``; the solver models one capacity per code, so
  a scenario whose inventory carries the same code twice (the ``(room_code,
  section)`` uniqueness) is refused rather than silently mis-sized.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings

from core.models import DeliveryBoard, Room, ScenarioSectionBudget, SectionPlacement
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_rooming import (
    _budget_value_for_placement,
    _build_rooming_budget_maps,
    _section_gender,
    get_board_gender,
    get_capacity_buffer,
    room_type_for_placement,
)


class ExactRoomingError(Exception):
    """A scenario the exact room pass refuses to reason about (fail closed)."""


@dataclass(frozen=True)
class RelaxedRoomResult:
    """Result of the fixed-time relaxed exact room assignment.

    Unlike the vendored ``solve_exact_room_assignment`` (all-or-nothing → the
    whole model is INFEASIBLE the moment one meeting cannot be roomed), this
    relaxation lets a meeting go **unroomed** at top lexicographic priority, so it
    is always total: it returns the minimum set of meetings that cannot be roomed
    at their fixed times (room-count contention), then the minimum capacity
    shortfall, minimum room changes, minimum excess capacity — in that order.
    """

    status: str
    assignment: dict  # demand_id -> room_code (only roomed meetings)
    unroomed: tuple[str, ...]
    shortfall: int
    changes: int
    excess: int
    proven_optimal: bool
    wall_time_seconds: float


def is_exact_rooming_enabled() -> bool:
    """Reads ``TIMETABLE_EXACT_ROOMING_ENABLED``. Default ``False`` (preview-only)."""
    return bool(getattr(settings, "TIMETABLE_EXACT_ROOMING_ENABLED", False))


def _hhmm(value: object) -> str:
    return value.strftime("%H:%M") if hasattr(value, "strftime") else str(value)[:5]


def _to_min(value: object) -> int:
    """Parse ``HH:MM`` to minutes, or raise ``ExactRoomingError`` (fail closed).

    ``SectionPlacement.start_time`` / ``end_time`` are free-text fields, so a
    malformed or empty value must surface as the typed error the endpoint turns
    into a clean 422 — never a raw ``ValueError`` that escapes as a 500.
    """
    text = _hhmm(value)
    try:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as exc:
        raise ExactRoomingError(f"Un-parseable placement time {value!r}.") from exc


@dataclass(frozen=True)
class MeetingDemand:
    """One physical meeting that needs exactly one room.

    A meeting can be represented by several ``SectionPlacement`` rows when its
    section is shared across boards at the same day/time; ``placement_ids`` lists
    them all and the chosen room is written to every one.
    """

    demand_id: str
    term_section_id: int
    course_code: str
    day: str
    start_min: int
    end_min: int
    required_type: str
    required_capacity: int
    required_gender: str
    required_programmes: frozenset[str]
    placement_ids: tuple[int, ...]
    board_ids: tuple[int, ...]
    incumbent_rooms: tuple[str, ...]
    compatible_rooms: tuple[str, ...]
    # One programme set per constituent board. A room must satisfy H14 for EVERY
    # entry (intersection-of-satisfaction), so reporting only the flat union would
    # read as an OR when the rule is an AND across boards.
    programmes_per_board: tuple[frozenset[str], ...] = ()

    @property
    def incumbent_room(self) -> str:
        """The single incumbent room if every row agrees, else '' (a change target)."""
        distinct = {r for r in self.incumbent_rooms if r and r != "UNASSIGNED"}
        return next(iter(distinct)) if len(distinct) == 1 else ""


@dataclass
class ExactRoomingReport:
    scenario_id: int
    status: str
    feasible: bool
    physical_meetings: int
    roomed_meetings: int
    # Shortfall decomposition (buffered seat-units)
    total_shortfall: int
    unavoidable_shortfall: int
    congestion_shortfall: int
    # Structural gaps
    no_compatible_room: list[dict] = field(default_factory=list)
    unroomable_meetings: list[dict] = field(default_factory=list)
    capacity_short_meetings: list[dict] = field(default_factory=list)
    hall_witnesses: list[dict] = field(default_factory=list)
    # Change footprint vs the currently persisted rooms
    room_changes: int = 0
    excess_capacity: int = 0
    proven_optimal: bool = False
    assignment: dict = field(default_factory=dict)
    wall_time_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "feasible": self.feasible,
            "physical_meetings": self.physical_meetings,
            "roomed_meetings": self.roomed_meetings,
            "shortfall": {
                "total": self.total_shortfall,
                "unavoidable_inventory": self.unavoidable_shortfall,
                "congestion_reducible": self.congestion_shortfall,
            },
            "no_compatible_room": self.no_compatible_room,
            "unroomable_meetings": self.unroomable_meetings,
            "capacity_short_meetings": self.capacity_short_meetings,
            "hall_witnesses": self.hall_witnesses,
            "room_changes": self.room_changes,
            "excess_capacity": self.excess_capacity,
            "proven_optimal": self.proven_optimal,
            "wall_time_seconds": round(self.wall_time_seconds, 3),
            "notes": self.notes,
        }


def _load_room_inventory() -> tuple[dict[str, dict], dict[str, int]]:
    """Return (rows_by_code, capacity_by_code); fail closed on duplicate codes.

    The exact solver models one capacity per room code. The schema only enforces
    uniqueness on ``(room_code, section)``, so a code *could* carry two rows with
    different capacities — that would silently mis-size the model, so we refuse it.
    """
    rows_by_code: dict[str, dict] = {}
    duplicates: set[str] = set()
    for room in Room.objects.all().order_by("id"):
        code = str(room.room_code or "").strip().upper()
        if not code:
            continue
        if code in rows_by_code:
            duplicates.add(code)
            continue
        rows_by_code[code] = {
            "room_code": code,
            "capacity": int(room.capacity or 0),
            "room_type": str(room.room_type or "lecture").strip().lower(),
            "section": str(room.section or "").strip().upper(),
            "programmes": frozenset(
                p.strip().upper() for p in str(room.department or "").split(",") if p.strip()
            ),
        }
    if duplicates:
        raise ExactRoomingError(
            "Room inventory carries duplicate room_codes "
            f"{sorted(duplicates)}; the exact model needs one capacity per code. "
            "Deduplicate the inventory (or extend the model to (code, section)) first."
        )
    capacity_by_code = {code: row["capacity"] for code, row in rows_by_code.items()}
    return rows_by_code, capacity_by_code


def build_meeting_demands(
    scenario_id: int,
    rows_by_code: dict[str, dict] | None = None,
) -> list[MeetingDemand]:
    """Group the scenario's physical placements into per-meeting room demands.

    Mirrors ``validate_physical_room_compatibility`` for the requirement side
    (type/capacity/gender/programme) and the compatible-room domain, but keyed by
    the physical meeting so shared-across-board sections resolve to one room.

    ``rows_by_code`` is the room inventory from a *single* ``_load_room_inventory``
    read; pass the same mapping the caller uses to build ``capacity_by_code`` so a
    concurrent room change cannot make a compatible room missing from the capacity
    map (which would raise mid-solve).
    """
    if rows_by_code is None:
        rows_by_code, _ = _load_room_inventory()
    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("board", "term_section")
        .order_by("id")
    )
    if not placements:
        return []

    online_lookup = OnlineCourseLookup()
    budget_maps = _build_rooming_budget_maps(
        list(ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)),
        get_capacity_buffer(),
    )
    board_gender_cache: dict[int, str] = {}

    def board_gender(board_id: int) -> str:
        if board_id not in board_gender_cache:
            board_gender_cache[board_id] = get_board_gender(board_id)
        return board_gender_cache[board_id]

    def board_programmes(board) -> frozenset[str]:
        return frozenset(
            v.strip().upper() for v in str(board.program or "").split(",") if v.strip()
        )

    # Group physical (non-online) placements into meetings. Day/time are
    # normalised so a spelling difference across boards cannot hide an overlap.
    groups: dict[tuple, list[SectionPlacement]] = defaultdict(list)
    for p in placements:
        if online_lookup.is_online_course_for_board(p.board, p.term_section.course_code):
            continue
        start_hhmm = _hhmm(p.start_time)
        end_hhmm = _hhmm(p.end_time)
        if _to_min(end_hhmm) <= _to_min(start_hhmm):  # fail closed on degenerate intervals
            raise ExactRoomingError(
                f"Placement {p.id} ({p.term_section.course_code}) has a non-positive "
                f"interval {start_hhmm}-{end_hhmm}."
            )
        key = (p.term_section_id, str(p.day).strip().upper(), start_hhmm, end_hhmm)
        groups[key].append(p)

    demands: list[MeetingDemand] = []
    for (term_section_id, day, start_hhmm, end_hhmm), rows in sorted(
        groups.items(), key=lambda kv: str(kv[0])
    ):
        sample = rows[0]
        required_type = room_type_for_placement(
            sample, start_time=start_hhmm, end_time=end_hhmm, budget_maps=budget_maps
        )
        buffered_demand = _budget_value_for_placement(sample, budget_maps, "buffered", 40)

        # Gender: per-section first, board second. A shared meeting whose boards
        # disagree on gender is a data defect; fail closed on that meeting.
        genders = set()
        for p in rows:
            genders.add(_section_gender(p.term_section.section) or board_gender(p.board_id))
        if len({g for g in genders if g}) > 1:
            raise ExactRoomingError(
                f"Meeting {sample.term_section.course_code}|{sample.term_section.section} "
                f"on {day} {start_hhmm} spans boards with conflicting gender {sorted(genders)}."
            )
        required_gender = next(iter(g for g in genders if g), "")

        # Programme (H14): the auditor validates EACH placement against its own
        # board's programme, so the chosen room must serve EVERY constituent
        # board — an intersection over boards, not a union. Reported for context.
        per_board_programmes = {board_programmes(p.board) for p in rows}
        required_programmes = (
            frozenset().union(*per_board_programmes) if per_board_programmes else frozenset()
        )

        # Compatible-room domain = auditor's type∩gender∩department (NOT capacity —
        # capacity becomes a minimised shortfall, not a hard prune). A room must
        # satisfy H14 for every board the meeting belongs to.
        compatible = tuple(
            sorted(
                code
                for code, room in rows_by_code.items()
                if room["room_type"] == required_type
                and (not required_gender or room["section"] == required_gender)
                and all(bool(bp & room["programmes"]) for bp in per_board_programmes)
            )
        )

        demands.append(
            MeetingDemand(
                # Must be TOTAL over the grouping key: two meetings of one section
                # on one day sharing a start but differing in end are distinct
                # demands, and a colliding id would silently overwrite one of them
                # in the solver's variable dicts (leaving it unconstrained).
                demand_id=f"{term_section_id}#{day}#{start_hhmm}-{end_hhmm}",
                term_section_id=term_section_id,
                course_code=str(sample.term_section.course_code),
                day=day,
                start_min=_to_min(start_hhmm),
                end_min=_to_min(end_hhmm),
                required_type=required_type,
                required_capacity=int(buffered_demand),
                required_gender=required_gender,
                required_programmes=required_programmes,
                placement_ids=tuple(p.id for p in rows),
                board_ids=tuple(sorted({p.board_id for p in rows})),
                programmes_per_board=tuple(sorted(per_board_programmes, key=sorted)),
                incumbent_rooms=tuple(str(p.room or "").strip().upper() for p in rows),
                compatible_rooms=compatible,
            )
        )
    return demands


def _greedy_room_assignment(
    demands: list[MeetingDemand],
    capacity_by_code: dict[str, int],
) -> dict[str, str]:
    """Deterministic first-fit warm start: best-fit room free at each interval.

    Meetings are taken largest-demand-first (best-fit-decreasing); each takes the
    smallest compatible room that is big enough and free, else the largest
    compatible room still free, else it stays unroomed. Used both as a CP-SAT hint
    and as a total fallback so the pass never returns nothing.
    """
    busy: dict[str, list[tuple[int, int]]] = defaultdict(list)  # room -> [(start,end)]

    def free(room: str, day: str, start: int, end: int) -> bool:
        return all(not (start < e and s < end) for (s, e) in busy[f"{room}#{day}"])

    assignment: dict[str, str] = {}
    for d in sorted(demands, key=lambda x: (-x.required_capacity, x.demand_id)):
        candidates = sorted(d.compatible_rooms, key=lambda r: (capacity_by_code[r], r))
        big_enough = [r for r in candidates if capacity_by_code[r] >= d.required_capacity]
        big_set = set(big_enough)
        ordered = big_enough + [r for r in reversed(candidates) if r not in big_set]
        for room in ordered:
            if free(room, d.day, d.start_min, d.end_min):
                assignment[d.demand_id] = room
                busy[f"{room}#{d.day}"].append((d.start_min, d.end_min))
                break
    return assignment


def _solve_relaxed(
    demands: list[MeetingDemand],
    capacity_by_code: dict[str, int],
    *,
    time_limit_seconds: float,
) -> RelaxedRoomResult:
    """Fixed-time room assignment that may leave a meeting unroomed (total, exact).

    Lexicographic objective, solved as a fixed-value phase chain with a single
    worker for reproducibility:

        1. minimise the number of unroomed meetings (room-count contention);
        2. minimise total buffered-capacity shortfall over roomed meetings;
        3. minimise rooms changed from the current board;
        4. minimise total excess capacity (waste).

    A deterministic greedy warm start seeds every phase (as a CP-SAT hint) and is
    returned as a proven-``False`` fallback if the first phase cannot even find a
    feasible assignment inside the budget.
    """
    from ortools.sat.python import cp_model

    # demand_id keys every variable dict below; a duplicate would silently drop a
    # meeting from the objectives and from H9 exclusivity. Enforce, never assume.
    if len({d.demand_id for d in demands}) != len(demands):
        raise ExactRoomingError("Duplicate demand_id: meeting identity is not unique.")

    greedy = _greedy_room_assignment(demands, capacity_by_code)

    def greedy_result(status: str) -> RelaxedRoomResult:
        roomed = [d for d in demands if d.demand_id in greedy]
        shortfall = sum(
            max(0, d.required_capacity - capacity_by_code[greedy[d.demand_id]]) for d in roomed
        )
        changes = sum(
            1 for d in roomed if d.incumbent_room and greedy[d.demand_id] != d.incumbent_room
        )
        excess = sum(
            max(0, capacity_by_code[greedy[d.demand_id]] - d.required_capacity) for d in roomed
        )
        return RelaxedRoomResult(
            status=status,
            assignment=dict(greedy),
            unroomed=tuple(d.demand_id for d in demands if d.demand_id not in greedy),
            shortfall=int(shortfall),
            changes=int(changes),
            excess=int(excess),
            proven_optimal=False,
            wall_time_seconds=0.0,
        )

    model = cp_model.CpModel()
    assign: dict[tuple[str, str], object] = {}
    roomed: dict[str, object] = {}
    for d in demands:
        options = []
        for room in d.compatible_rooms:
            var = model.new_bool_var(f"x_{d.demand_id}_{room}")
            assign[(d.demand_id, room)] = var
            options.append(var)
        flag = model.new_bool_var(f"roomed_{d.demand_id}")
        roomed[d.demand_id] = flag
        model.add(sum(options) == flag)  # at most one room; flag records whether roomed
        # Seed the greedy warm start as a hint on every assignment literal.
        hinted = greedy.get(d.demand_id)
        for room in d.compatible_rooms:
            model.add_hint(assign[(d.demand_id, room)], 1 if room == hinted else 0)
        model.add_hint(flag, 1 if hinted else 0)

    # Room exclusivity (H9) across every pair of overlapping meetings.
    for i, left in enumerate(demands):
        left_rooms = set(left.compatible_rooms)
        for right in demands[i + 1 :]:
            if left.day != right.day:
                continue
            if not (left.start_min < right.end_min and right.start_min < left.end_min):
                continue
            for room in left_rooms.intersection(right.compatible_rooms):
                model.add(assign[(left.demand_id, room)] + assign[(right.demand_id, room)] <= 1)

    unroomed_expr = sum(1 - roomed[d.demand_id] for d in demands)
    shortfall_expr = sum(
        assign[(d.demand_id, room)] * max(0, d.required_capacity - capacity_by_code[room])
        for d in demands
        for room in d.compatible_rooms
    )
    changes_expr = sum(
        assign[(d.demand_id, room)] * int(bool(d.incumbent_room) and room != d.incumbent_room)
        for d in demands
        for room in d.compatible_rooms
    )
    excess_expr = sum(
        assign[(d.demand_id, room)] * max(0, capacity_by_code[room] - d.required_capacity)
        for d in demands
        for room in d.compatible_rooms
    )

    phases = (unroomed_expr, shortfall_expr, changes_expr, excess_expr)
    phase_limit = max(1.0, time_limit_seconds / len(phases))
    proofs: list[bool] = []
    total_wall = 0.0

    def extract(solver) -> dict[str, str]:
        return {
            d.demand_id: room
            for d in demands
            for room in d.compatible_rooms
            if solver.value(assign[(d.demand_id, room)])
        }

    last_assignment: dict[str, str] = {}
    for expr in phases:
        model.minimize(expr)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = phase_limit
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        status = solver.solve(model)
        total_wall += solver.wall_time
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # This phase found nothing in budget. Keep the best solution proven so
            # far (or the greedy warm start for phase 1) and stop refining.
            if not last_assignment:
                return greedy_result(solver.status_name(status))
            roomed = [d for d in demands if d.demand_id in last_assignment]
            return RelaxedRoomResult(
                status=solver.status_name(status),
                assignment=dict(last_assignment),
                unroomed=tuple(d.demand_id for d in demands if d.demand_id not in last_assignment),
                shortfall=sum(
                    max(0, d.required_capacity - capacity_by_code[last_assignment[d.demand_id]])
                    for d in roomed
                ),
                changes=sum(
                    1
                    for d in roomed
                    if d.incumbent_room and last_assignment[d.demand_id] != d.incumbent_room
                ),
                excess=sum(
                    max(0, capacity_by_code[last_assignment[d.demand_id]] - d.required_capacity)
                    for d in roomed
                ),
                proven_optimal=False,
                wall_time_seconds=total_wall,
            )
        value = int(round(solver.objective_value))
        proofs.append(status == cp_model.OPTIMAL)
        # Pin with <= (not ==): when the phase is proven OPTIMAL this is
        # equivalent, but if it only reached FEASIBLE, `value` is an upper bound —
        # `<=` lets a later phase still recover a strictly better solution instead
        # of freezing a possibly-suboptimal count as if it were the minimum.
        model.add(expr <= value)
        last_assignment = extract(solver)

    # Report metrics from the FINAL assignment, not the per-phase recorded values:
    # with `<=` pins a later phase can strictly improve an earlier objective, so the
    # returned numbers must match the returned assignment exactly.
    roomed = [d for d in demands if d.demand_id in last_assignment]
    return RelaxedRoomResult(
        status="OPTIMAL" if all(proofs) else "FEASIBLE",
        assignment=last_assignment,
        unroomed=tuple(d.demand_id for d in demands if d.demand_id not in last_assignment),
        shortfall=sum(
            max(0, d.required_capacity - capacity_by_code[last_assignment[d.demand_id]])
            for d in roomed
        ),
        changes=sum(
            1
            for d in roomed
            if d.incumbent_room and last_assignment[d.demand_id] != d.incumbent_room
        ),
        excess=sum(
            max(0, capacity_by_code[last_assignment[d.demand_id]] - d.required_capacity)
            for d in roomed
        ),
        proven_optimal=all(proofs),
        wall_time_seconds=total_wall,
    )


def _oversubscribed_intervals(demands: list[MeetingDemand]) -> list[dict]:
    """Time windows where more meetings are active than there are rooms to serve them.

    A self-contained necessary-infeasibility diagnostic: at each maximal time atom
    (a gap between consecutive meeting boundaries on a day), count the active
    meetings and the distinct rooms compatible with at least one of them. When the
    meetings outnumber the rooms, at least that many must go unroomed *at that
    instant* — it explains a room-count contention the exact solve then quantifies.
    """
    boundaries: dict[str, set[int]] = defaultdict(set)
    for d in demands:
        boundaries[d.day].update((d.start_min, d.end_min))
    out: list[dict] = []
    for day in sorted(boundaries):
        marks = sorted(boundaries[day])
        for start, end in zip(marks, marks[1:], strict=False):
            active = [
                d for d in demands if d.day == day and d.start_min < end and start < d.end_min
            ]
            if len(active) < 2:
                continue
            rooms = set().union(*(set(d.compatible_rooms) for d in active))
            deficiency = len(active) - len(rooms)
            if deficiency > 0:
                out.append(
                    {
                        "day": day,
                        "start": start,
                        "end": end,
                        "meetings": sorted(d.demand_id for d in active),
                        "available_rooms": len(rooms),
                        "deficiency": deficiency,
                    }
                )
    return out


def plan_exact_rooming(
    scenario_id: int,
    *,
    time_limit_seconds: float = 30.0,
) -> ExactRoomingReport:
    """Read-only exact-rooming report for a scenario (never writes placements)."""
    try:
        DeliveryBoard.objects.filter(scenario_id=scenario_id).exists()
    except Exception as exc:  # pragma: no cover - defensive
        raise ExactRoomingError(f"scenario {scenario_id} unreadable: {exc}") from exc

    # One inventory read feeds both the compatible-room domains and the capacity
    # map, so a concurrent room change can't leave a compatible room missing from
    # capacities mid-solve.
    rows_by_code, capacity_by_code = _load_room_inventory()
    demands = build_meeting_demands(scenario_id, rows_by_code)

    report = ExactRoomingReport(
        scenario_id=scenario_id,
        status="EMPTY",
        feasible=True,
        physical_meetings=len(demands),
        roomed_meetings=0,
        total_shortfall=0,
        unavoidable_shortfall=0,
        congestion_shortfall=0,
    )
    if not demands:
        report.notes.append("No physical meetings to room.")
        return report

    # Split off meetings with an empty type∩gender∩department domain: no room
    # solver can help these — it is an inventory/eligibility gap, not congestion.
    solvable: list[MeetingDemand] = []
    for d in demands:
        if d.compatible_rooms:
            solvable.append(d)
        else:
            report.no_compatible_room.append(
                {
                    "meeting": d.demand_id,
                    "course": d.course_code,
                    "day": d.day,
                    "required_type": d.required_type,
                    "required_gender": d.required_gender,
                    "required_programmes": sorted(d.required_programmes),
                    # The room must serve EVERY board's programmes, not any one.
                    "required_programmes_per_board": [sorted(bp) for bp in d.programmes_per_board],
                    "buffered_demand": d.required_capacity,
                }
            )

    if not solvable:
        report.status = "NO_COMPATIBLE_ROOMS"
        report.feasible = False
        report.notes.append(
            f"{len(report.no_compatible_room)} meetings have no room of the required "
            "type/gender/department at all — a structural inventory gap."
        )
        return report

    report.hall_witnesses = _oversubscribed_intervals(solvable)

    result = _solve_relaxed(solvable, capacity_by_code, time_limit_seconds=time_limit_seconds)
    report.status = result.status
    report.wall_time_seconds = result.wall_time_seconds
    report.proven_optimal = result.proven_optimal

    if not result.assignment and result.unroomed and len(result.unroomed) == len(solvable):
        report.feasible = False
        report.notes.append(f"Relaxed solver returned {result.status} without any assignment.")
        return report

    shortfall, changes, excess = result.shortfall, result.changes, result.excess
    report.assignment = dict(result.assignment)
    report.roomed_meetings = len(result.assignment)
    report.total_shortfall = int(shortfall)
    report.room_changes = int(changes)
    report.excess_capacity = int(excess)
    # Only a PROVEN-optimal solve may claim feasibility. A timed-out (FEASIBLE/
    # UNKNOWN) run can report unroomed meetings that are actually roomable, so
    # "feasible" and the structural verdicts below stay provisional until proven.
    report.feasible = (
        result.proven_optimal
        and shortfall == 0
        and not result.unroomed
        and not report.no_compatible_room
    )

    # Meetings the fixed times leave unroomable — room-count contention, not
    # capacity. These need a *time* change (a different pass), not a bigger room.
    by_id = {d.demand_id: d for d in solvable}
    for demand_id in result.unroomed:
        d = by_id[demand_id]
        report.unroomable_meetings.append(
            {
                "meeting": d.demand_id,
                "course": d.course_code,
                "day": d.day,
                "compatible_rooms": len(d.compatible_rooms),
                "buffered_demand": d.required_capacity,
            }
        )

    # Decomposition of the shortfall over the ROOMED meetings (the set `total`
    # is measured on): give each its largest COMPATIBLE room ignoring collisions →
    # any residual is inventory-unavoidable; the rest is congestion.
    unavoidable = 0
    for d in solvable:
        if d.demand_id not in result.assignment:
            continue  # unroomed meetings are a separate (count) bucket, not capacity
        best = max((capacity_by_code[c] for c in d.compatible_rooms), default=0)
        unavoidable += max(0, d.required_capacity - best)
    report.unavoidable_shortfall = int(min(unavoidable, shortfall))
    report.congestion_shortfall = max(0, int(shortfall) - report.unavoidable_shortfall)

    # Which meetings actually landed in a too-small room.
    for d in solvable:
        room = result.assignment.get(d.demand_id)
        if room is None:
            continue
        deficit = d.required_capacity - capacity_by_code.get(room, 0)
        if deficit > 0:
            report.capacity_short_meetings.append(
                {
                    "meeting": d.demand_id,
                    "course": d.course_code,
                    "day": d.day,
                    "assigned_room": room,
                    "room_capacity": capacity_by_code.get(room, 0),
                    "buffered_demand": d.required_capacity,
                    "deficit": deficit,
                }
            )

    if not report.proven_optimal:
        report.notes.append(
            f"PROVISIONAL — the solve reached {report.status} within the time budget, not a "
            "proven optimum; the counts below are an upper bound and may improve with more time."
        )
    if report.feasible:
        report.notes.append(
            f"All {report.roomed_meetings} physical meetings roomed with zero capacity "
            f"shortfall ({report.room_changes} rooms differ from the current board)."
        )
    else:
        verb = "cannot" if report.proven_optimal else "were not"
        if report.unroomable_meetings:
            report.notes.append(
                f"{len(report.unroomable_meetings)} meetings {verb} be roomed at their "
                "fixed times — more concurrent meetings than compatible rooms (room-count "
                "contention). These need a time change, not a bigger room."
            )
        if report.total_shortfall:
            qualifier = "Minimum capacity" if report.proven_optimal else "Capacity"
            report.notes.append(
                f"{qualifier} shortfall {report.total_shortfall} seat-units among roomed "
                f"meetings ({report.unavoidable_shortfall} unavoidable from inventory, "
                f"{report.congestion_shortfall} congestion-reducible)."
            )
        if report.no_compatible_room:
            report.notes.append(
                f"{len(report.no_compatible_room)} meetings have no room of the required "
                "type/gender/department at all (structural inventory gap)."
            )
    return report
