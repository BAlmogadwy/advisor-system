"""
core/services/timetable_rooming.py
Room assignment service for the timetable workspace.

Provides:
  - RoomTracker: in-memory room usage tracker for greedy placement
  - assign_rooms_to_board(): post-placement room filler for solver/SA paths
  - get_programme_rooms(): load rooms filtered by programme codes
  - check_room_feasibility(): pre-check section sizes vs room capacities
"""

from __future__ import annotations

import time
from collections import defaultdict

from django.conf import settings

from core.models import DeliveryBoard, Room, SectionPlacement
from core.services.timetable_decision_trace import DecisionTrace
from core.services.timetable_lab_predicate import (
    is_lab_heuristic_unified,
    meeting_requires_lab_room,
)
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    ROOM_BUFFER_REJECT,
    RoomFailureReason,
    check_capacity_feasibility,
    check_gender_feasibility,
    check_occupancy,
    check_type_feasibility,
    is_room_oracle_enabled,
    room_failure_breakdown,
)
from core.services.timetable_solver_codes import (
    ROOMING_REPAIR_REASSIGNED,
    is_stage_trace_enabled,
)
from core.services.timetable_stage_telemetry import (
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
    record_stage_iterations,
    record_stage_ms,
)


def get_capacity_buffer() -> float:
    """Return the active room-sizing multiplier (e.g. 1.1 = +10% for late adds).

    Reads ``settings.TIMETABLE_CAPACITY_BUFFER`` and falls back to 1.1 if the
    setting is missing or invalid. Kept as a helper so every site that sizes
    rooms stays in sync with config.
    """
    try:
        value = float(getattr(settings, "TIMETABLE_CAPACITY_BUFFER", 1.1))
    except (TypeError, ValueError):
        return 1.1
    if value <= 0:
        return 1.1
    return value


def get_programme_rooms(programmes: list[str]) -> list[dict]:
    """Load rooms available for a list of programmes.

    Matches any room whose comma-separated ``department`` field contains
    at least one of the given programme codes.

    Returns list of dicts sorted by capacity ASC (for best-fit allocation):
        ``[{room_code, capacity, room_type, wing, building}, ...]``
    """
    all_rooms = Room.objects.all().order_by("capacity")
    result = []
    progs_upper = {p.strip().upper() for p in programmes if p.strip()}
    for r in all_rooms:
        room_progs = {p.strip().upper() for p in r.department.split(",") if p.strip()}
        if room_progs & progs_upper:
            result.append(
                {
                    "room_code": r.room_code,
                    "capacity": r.capacity,
                    "room_type": r.room_type or "lecture",
                    "wing": r.wing,
                    "building": r.building,
                    "section": (r.section or "").upper(),
                }
            )
    return result


def _section_gender(label: str | None) -> str:
    """Extract M/F gender from a TermSection.section label (e.g. 'M1' → 'M').

    Returns '' if the label doesn't start with M or F.
    """
    if not label:
        return ""
    first = str(label).strip()[:1].upper()
    return first if first in ("M", "F") else ""


def get_board_gender(board_id: int) -> str:
    """Derive M/F gender for a board from the students linked to it.

    Returns 'M' or 'F' if all linked students share the same section;
    '' if the board is empty or mixed (falls back to no gender filter).
    """
    from core.models import BoardStudentLink, Student

    student_ids = BoardStudentLink.objects.filter(board_id=board_id).values_list(
        "student_id", flat=True
    )
    if not student_ids:
        return ""
    genders = (
        Student.objects.filter(student_id__in=list(student_ids))
        .values_list("section", flat=True)
        .distinct()
    )
    unique = {str(g or "").strip().upper() for g in genders}
    unique.discard("")
    return next(iter(unique)) if len(unique) == 1 else ""


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


class RoomTracker:
    """In-memory tracker for room usage during greedy placement.

    Maintains a ``(day, start_time) -> set[room_code]`` map of occupied
    rooms.  Used by ``auto_place_board()`` to check availability and
    assign rooms as sections are placed.
    """

    def __init__(self, rooms: list[dict]):
        self.rooms = rooms
        self.lecture_rooms = sorted(
            [r for r in rooms if r["room_type"] == "lecture"],
            key=lambda r: r["capacity"],
        )
        self.lab_rooms = sorted(
            [r for r in rooms if r["room_type"] == "lab"],
            key=lambda r: r["capacity"],
        )
        self.usage: dict[tuple[str, str], set[str]] = defaultdict(set)

    def _pool(self, room_type: str, gender: str = "") -> list[dict]:
        base = self.lab_rooms if room_type == "lab" else self.lecture_rooms
        if gender in ("M", "F"):
            return [r for r in base if r.get("section", "") == gender]
        return base

    def is_feasible(
        self,
        day: str,
        start: str,
        min_capacity: int,
        room_type: str = "lecture",
        gender: str = "",
    ) -> bool:
        """Can we fit a section of *min_capacity* in this (day, start) slot?"""
        used = self.usage.get((day, start), set())
        pool = self._pool(room_type, gender)
        return any(r["room_code"] not in used and r["capacity"] >= min_capacity for r in pool)

    def assign_best_fit(
        self,
        day: str,
        start: str,
        min_capacity: int,
        room_type: str = "lecture",
        gender: str = "",
    ) -> str | None:
        """Assign the smallest sufficient room of matching type and gender.

        Returns the ``room_code`` on success, or ``None`` if no room fits.
        """
        used = self.usage.get((day, start), set())
        pool = self._pool(room_type, gender)
        for r in pool:  # already sorted by capacity ASC
            if r["room_code"] not in used and r["capacity"] >= min_capacity:
                self.usage[(day, start)].add(r["room_code"])
                return r["room_code"]
        return None

    def release(self, day: str, start: str, room_code: str) -> None:
        """Free a room (for undo/retry)."""
        key = (day, start)
        if key in self.usage:
            self.usage[key].discard(room_code)


def check_room_feasibility(
    board_id: int,
    rooms: list[dict],
) -> list[dict]:
    """Pre-check: can every section find a room with sufficient capacity?

    Returns a list of violations (empty = all feasible):
        ``[{course_code, max_per_section, room_type_needed, max_room_capacity}, ...]``
    """
    from core.models import ScenarioSectionBudget
    from core.services.timetable_autoplace import get_meeting_pattern

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return []

    budgets = ScenarioSectionBudget.objects.filter(
        scenario=board.scenario, programme_term=board.nominal_term
    )

    lecture_max = max((r["capacity"] for r in rooms if r["room_type"] == "lecture"), default=0)
    lab_max = max((r["capacity"] for r in rooms if r["room_type"] == "lab"), default=0)

    violations = []
    for b in budgets:
        pattern = get_meeting_pattern(b.credit_hours or 3)
        has_lab = any(d > 75 for d in pattern)
        cap = b.max_per_section

        if cap > lecture_max:
            violations.append(
                {
                    "course_code": b.course_code,
                    "max_per_section": cap,
                    "room_type_needed": "lecture",
                    "max_room_capacity": lecture_max,
                }
            )
        if has_lab and cap > lab_max:
            violations.append(
                {
                    "course_code": b.course_code,
                    "max_per_section": cap,
                    "room_type_needed": "lab",
                    "max_room_capacity": lab_max,
                }
            )

    return violations


def assign_rooms_to_board(board_id: int, *, respect_locked: bool = False) -> dict:
    """Post-placement room assignment for solver/annealing paths.

    Assigns rooms to ``SectionPlacement`` rows on the board that currently
    have an empty ``room`` field. Uses greedy best-fit per (day, start_time)
    slot. When ``respect_locked`` is true, locked placements are treated as
    fixed and are not assigned/repaired.

    Returns ``{assigned: int, unassigned: int}``.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            # PR6 commit 6 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    if not programmes:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            "stage_telemetry": empty_stage_telemetry(),
        }

    rooms = get_programme_rooms(programmes)
    if not rooms:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            "stage_telemetry": empty_stage_telemetry(),
        }

    board_gender = get_board_gender(board_id)
    tracker = RoomTracker(rooms)

    # Pre-populate tracker with rooms used by OTHER boards in the same scenario
    other_placements = (
        SectionPlacement.objects.filter(board__scenario=board.scenario)
        .exclude(board=board)
        .exclude(room="")
        .exclude(room="UNASSIGNED")
        .values_list("day", "start_time", "room")
    )
    for day, start, room_code in other_placements:
        tracker.usage[(day, start)].add(room_code)

    # Load all placements for THIS board
    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("day", "start_time")
    )

    # First pass: mark rooms already assigned (from greedy or previous run).
    # Exclude the sentinel "UNASSIGNED" so placements left unroomed by a
    # prior pass become repair candidates below rather than being treated
    # as if the sentinel occupied a slot.
    for p in placements:
        if p.room and p.room != "UNASSIGNED":
            tracker.usage[(p.day, p.start_time)].add(p.room)

    # Get actual students per section and credit hours from budget
    from core.models import ScenarioSectionBudget

    all_budgets = ScenarioSectionBudget.objects.filter(scenario=board.scenario)
    budget_map = {}
    raw_budget_map = {}
    credit_map = {}
    buffer_multiplier = get_capacity_buffer()
    for b in all_budgets:
        raw = (
            -(-b.total_demand // b.planned_sections)
            if b.planned_sections > 0
            else b.max_per_section
        )
        raw_budget_map[b.course_code] = int(raw)
        budget_map[b.course_code] = int(raw * buffer_multiplier)
        credit_map[b.course_code] = b.credit_hours

    # Sort unassigned placements by capacity DESC (largest first = best-fit-decreasing).
    # PR5 commit 6: also re-process placements carrying the "UNASSIGNED"
    # sentinel so the rooming 2nd pass can repair them when a fitting room
    # exists. Capture the sentinel state per placement BEFORE mutation so
    # trace emission can gate strictly on the UNASSIGNED → assigned
    # transition (no emission for empty-string → assigned, which is the
    # normal first-pass path).
    locked_unassigned_count = sum(
        1
        for p in placements
        if respect_locked and p.is_locked and (not p.room or p.room == "UNASSIGNED")
    )
    unassigned_placements = [
        p
        for p in placements
        if (not p.room or p.room == "UNASSIGNED") and not (respect_locked and p.is_locked)
    ]
    unassigned_placements.sort(key=lambda p: -(budget_map.get(p.term_section.course_code, 40)))
    previous_room_by_id: dict[int, str] = {p.id: p.room for p in unassigned_placements}
    decision_trace: dict[str, dict] = {}
    emit_trace = is_stage_trace_enabled()

    # PR6 commit 6 — rooming_repair stage-boundary timing. Scoped to the
    # repair pass only (UNASSIGNED → room reassignments), per ChatGPT
    # guardrail: "keep timing scoped to the repair pass only, not the
    # whole room assignment function. The first-pass rooming belongs
    # outside this stage." We gate on whether any placement arrives
    # carrying the UNASSIGNED sentinel; if none do, this is a pure
    # first-pass call and both rooming_repair keys stay at zero.
    _stage_telemetry: dict[str, dict[str, int]] = empty_stage_telemetry()
    _repair_candidates = sum(1 for p in unassigned_placements if p.room == "UNASSIGNED")
    _telemetry_on = is_stage_telemetry_enabled() and _repair_candidates > 0
    _repair_t0 = time.perf_counter() if _telemetry_on else 0.0
    _repair_reassignments = 0

    assigned = 0
    unassigned = locked_unassigned_count
    # Labs currently ignore capacity in rooming (room_cap=0 below); buffer
    # diagnostics therefore apply to lecture room assignment only.
    #
    # Authoritative per-placement buffer-reject counter — populated only
    # when the oracle flag is on and Stage 2 confirms the rejection was
    # buffer-only. (The legacy flag-agnostic ``lecture_room_reject_due_to_buffer_count``
    # was retired in PR4 commit 7; dashboards migrated to this key.)
    buffer_only_rejects = 0
    room_failures: list[dict] = []

    for p in unassigned_placements:
        cap = budget_map.get(p.term_section.course_code, 40)
        raw_cap = raw_budget_map.get(p.term_section.course_code, 40)
        cr = credit_map.get(p.term_section.course_code, 3)
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        # Only 4-credit courses have lab meetings. 2-credit 100-min
        # meetings are long lectures, not labs.
        # Rule: only 4-credit courses have lab meetings. A long (>=80 min)
        # meeting of a non-4-credit course is a long lecture, not a lab.
        # Keep the unified duration predicate as the duration gate; gate
        # additionally on ``cr == 4`` so non-4-credit meetings never enter
        # the lab pool regardless of length.
        if is_lab_heuristic_unified():
            room_type = "lab" if (cr == 4 and meeting_requires_lab_room(duration)) else "lecture"
        else:
            room_type = "lab" if (duration > 80 and cr == 4) else "lecture"

        # For lab meetings, don't filter by capacity — lab rooms have a
        # fixed physical size (computers/benches).
        room_cap = 0 if room_type == "lab" else cap
        # Prefer per-section gender (exam-style sections like 'M1'/'F1');
        # fall back to the board-level gender (timetable-style 'S1'/'S2').
        gender = _section_gender(p.term_section.section) or board_gender
        room_code = tracker.assign_best_fit(p.day, p.start_time, room_cap, room_type, gender)
        if room_code:
            prev_room = previous_room_by_id.get(p.id, "")
            p.room = room_code
            p.save(update_fields=["room", "updated_at"])
            assigned += 1
            # PR6 commit 6 — count only true repair reassignments
            # (UNASSIGNED → room). Matches the semantics of the
            # ROOMING_REPAIR_REASSIGNED sentinel; empty-string →
            # assigned is the normal first-pass path and is NOT a
            # repair reassignment.
            if _telemetry_on and prev_room == "UNASSIGNED":
                _repair_reassignments += 1
            # PR5 commit 6 — emit ROOMING_REPAIR_REASSIGNED when the 2nd
            # pass rescued a placement previously marked UNASSIGNED.
            # Strictly gated: empty-string → assigned is the normal
            # first-pass path and does NOT emit.
            if emit_trace and prev_room == "UNASSIGNED":
                section_code = f"{p.term_section.course_code}|{p.term_section.section}"
                start_str = (
                    p.start_time.strftime("%H:%M")
                    if hasattr(p.start_time, "strftime")
                    else str(p.start_time)[:5]
                )
                end_str = (
                    p.end_time.strftime("%H:%M")
                    if hasattr(p.end_time, "strftime")
                    else str(p.end_time)[:5]
                )
                entry = DecisionTrace(
                    section_code=section_code,
                    course_code=p.term_section.course_code,
                    chosen_day=p.day,
                    chosen_start_time=start_str,
                    chosen_end_time=end_str,
                    chosen_room=room_code,
                    alternatives=(),
                    stage_origin="rooming_repair",
                    stage_context={
                        "code": ROOMING_REPAIR_REASSIGNED,
                        "previous_room": "UNASSIGNED",
                        "new_room": room_code,
                    },
                )
                decision_trace[section_code] = entry.to_dict()
        else:
            # Would a raw-cap room have fit? Used below to refine the oracle
            # rejection code into ROOM_BUFFER_REJECT when the section could
            # have been placed without the capacity buffer.
            is_buffer_only = room_type != "lab" and tracker.is_feasible(
                p.day, p.start_time, raw_cap, room_type, gender
            )
            p.room = "UNASSIGNED"
            p.save(update_fields=["room", "updated_at"])
            unassigned += 1
            # PR2 commit 4 — oracle refinement chain. When the flag is off
            # the helpers all return None and the default NO_ROOM_CAPACITY
            # path below runs — commit 3's payload is preserved bit-for-bit.
            # When the flag is on:
            #   * Stage 2: a buffer-only rejection wins over Stage 1 codes
            #     (the section *could* have been placed at raw capacity,
            #     the buffer is what rejected it), bumps the authoritative
            #     ``buffer_only_rejects`` counter.
            #   * Stage 1: type → gender → capacity, first matching wins.
            #   * Occupancy: if Stage 1 finds an eligible pool but every
            #     room is already busy at this slot, emit ROOM_OCCUPIED.
            section_dict = {
                "course_code": p.term_section.course_code,
                "section_code": p.term_section.section,
                "day": p.day,
                "start_time": p.start_time,
                "end_time": p.end_time,
                "demand": raw_cap,
                "room_type_required": room_type,
                "gender_required": gender,
            }
            refined: RoomFailureReason | None = None
            if is_room_oracle_enabled():
                if is_buffer_only:
                    refined = RoomFailureReason(
                        code=ROOM_BUFFER_REJECT,
                        day=p.day,
                        start_time=p.start_time,
                        end_time=p.end_time,
                        course_code=p.term_section.course_code,
                        section_code=p.term_section.section,
                    )
                    buffer_only_rejects += 1
                else:
                    refined = (
                        check_type_feasibility(section_dict, tracker.rooms)
                        or check_gender_feasibility(section_dict, tracker.rooms)
                        or check_capacity_feasibility(
                            section_dict, tracker.rooms, buffer_multiplier
                        )
                        or check_occupancy(
                            section_dict,
                            tracker.rooms,
                            tracker.usage.get((p.day, p.start_time), set()),
                        )
                    )
            if refined is None:
                refined = RoomFailureReason(
                    code=NO_ROOM_CAPACITY,
                    day=p.day,
                    start_time=p.start_time,
                    end_time=p.end_time,
                    course_code=p.term_section.course_code,
                    section_code=p.term_section.section,
                )
            room_failures.append(refined.to_dict())

    if _telemetry_on:
        record_stage_ms(
            _stage_telemetry,
            "rooming_repair",
            max(1, int((time.perf_counter() - _repair_t0) * 1000)),
        )
        record_stage_iterations(_stage_telemetry, "rooming_repair", _repair_reassignments)

    return {
        "assigned": assigned,
        "unassigned": unassigned,
        "capacity_buffer": buffer_multiplier,
        "buffer_only_rejects": buffer_only_rejects,
        "room_failures": room_failures,
        "room_failure_breakdown": room_failure_breakdown(room_failures),
        "unplaced_count": unassigned,
        "locked_skipped": locked_unassigned_count,
        "decision_trace": decision_trace,
        "stage_telemetry": _stage_telemetry,
    }


def simulate_buffer_impact(board_id: int, buffers: list[float]) -> dict:
    """Dry-run rooming across several buffer values.

    For each ``buffer`` in ``buffers`` (e.g. ``[1.0, 1.1]``), simulates
    ``assign_rooms_to_board`` on a fresh in-memory room tracker and counts
    how many placements would be assigned vs left unassigned at that
    buffer. Never touches the database.

    Returns::

        {
            "board_id": int,
            "programmes": [str, ...],
            "results": [
                {"buffer": float, "assigned": int, "unassigned": int,
                 "rejected_by_buffer_vs_1_0": int},
                ...
            ],
        }
    """
    from core.models import ScenarioSectionBudget

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"board_id": board_id, "programmes": [], "results": []}

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    rooms = get_programme_rooms(programmes) if programmes else []

    board_gender = get_board_gender(board_id)

    # Stable pre-population: rooms consumed by OTHER boards in this scenario.
    other_usage = list(
        SectionPlacement.objects.filter(board__scenario=board.scenario)
        .exclude(board=board)
        .exclude(room="")
        .exclude(room="UNASSIGNED")
        .values_list("day", "start_time", "room")
    )

    all_budgets = list(ScenarioSectionBudget.objects.filter(scenario=board.scenario))
    raw_map = {}
    credit_map = {}
    for b in all_budgets:
        raw_map[b.course_code] = int(
            -(-b.total_demand // b.planned_sections)
            if b.planned_sections > 0
            else b.max_per_section
        )
        credit_map[b.course_code] = b.credit_hours

    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("day", "start_time")
    )

    results: list[dict] = []
    for buf in buffers:
        tracker = RoomTracker(rooms)
        for day, start, room_code in other_usage:
            tracker.usage[(day, start)].add(room_code)
        # Seed with rooms already permanently assigned on THIS board.
        for p in placements:
            if p.room and p.room != "UNASSIGNED":
                tracker.usage[(p.day, p.start_time)].add(p.room)

        assigned = 0
        unassigned = 0
        rejected_by_buffer = 0

        targets = [p for p in placements if not p.room or p.room == "UNASSIGNED"]
        targets.sort(key=lambda p: -raw_map.get(p.term_section.course_code, 40))

        for p in targets:
            raw_cap = raw_map.get(p.term_section.course_code, 40)
            buffered_cap = int(raw_cap * buf)
            cr = credit_map.get(p.term_section.course_code, 3)
            duration = _to_min(p.end_time) - _to_min(p.start_time)
            # Same cr==4 gate as assign_rooms_to_board (see comment there).
            if is_lab_heuristic_unified():
                room_type = (
                    "lab" if (cr == 4 and meeting_requires_lab_room(duration)) else "lecture"
                )
            else:
                room_type = "lab" if (duration > 80 and cr == 4) else "lecture"
            room_cap = 0 if room_type == "lab" else buffered_cap
            gender = _section_gender(p.term_section.section) or board_gender
            room_code = tracker.assign_best_fit(p.day, p.start_time, room_cap, room_type, gender)
            if room_code:
                assigned += 1
            else:
                if room_type != "lab" and tracker.is_feasible(
                    p.day, p.start_time, raw_cap, room_type, gender
                ):
                    rejected_by_buffer += 1
                unassigned += 1

        results.append(
            {
                "buffer": buf,
                "assigned": assigned,
                "unassigned": unassigned,
                "rejected_by_buffer_vs_1_0": rejected_by_buffer,
            }
        )

    return {"board_id": board_id, "programmes": programmes, "results": results}
