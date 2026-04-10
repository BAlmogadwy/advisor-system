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

from collections import defaultdict

from core.models import DeliveryBoard, Room, SectionPlacement


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
                }
            )
    return result


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

    def _pool(self, room_type: str) -> list[dict]:
        return self.lab_rooms if room_type == "lab" else self.lecture_rooms

    def is_feasible(
        self, day: str, start: str, min_capacity: int, room_type: str = "lecture"
    ) -> bool:
        """Can we fit a section of *min_capacity* in this (day, start) slot?"""
        used = self.usage.get((day, start), set())
        pool = self._pool(room_type)
        return any(r["room_code"] not in used and r["capacity"] >= min_capacity for r in pool)

    def assign_best_fit(
        self, day: str, start: str, min_capacity: int, room_type: str = "lecture"
    ) -> str | None:
        """Assign the smallest sufficient room of matching type.

        Returns the ``room_code`` on success, or ``None`` if no room fits.
        """
        used = self.usage.get((day, start), set())
        pool = self._pool(room_type)
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


def assign_rooms_to_board(board_id: int) -> dict:
    """Post-placement room assignment for solver/annealing paths.

    Assigns rooms to all ``SectionPlacement`` rows on the board that
    currently have an empty ``room`` field.  Uses greedy best-fit per
    (day, start_time) slot.

    Returns ``{assigned: int, unassigned: int}``.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"assigned": 0, "unassigned": 0}

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    if not programmes:
        return {"assigned": 0, "unassigned": 0}

    rooms = get_programme_rooms(programmes)
    if not rooms:
        return {"assigned": 0, "unassigned": 0}

    tracker = RoomTracker(rooms)

    # Load all placements, sorted by capacity DESC (largest sections first)
    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("day", "start_time")
    )

    # First pass: mark rooms already assigned (from greedy or previous run)
    for p in placements:
        if p.room:
            tracker.usage[(p.day, p.start_time)].add(p.room)

    # Get section capacities from budget
    from core.models import ScenarioSectionBudget

    budget_map = {
        b.course_code: b.max_per_section
        for b in ScenarioSectionBudget.objects.filter(
            scenario=board.scenario, programme_term=board.nominal_term
        )
    }

    # Sort unassigned placements by capacity DESC (largest first = best-fit-decreasing)
    unassigned_placements = [p for p in placements if not p.room]
    unassigned_placements.sort(key=lambda p: -(budget_map.get(p.term_section.course_code, 40)))

    assigned = 0
    unassigned = 0

    for p in unassigned_placements:
        cap = budget_map.get(p.term_section.course_code, 40)
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        room_type = "lab" if duration > 80 else "lecture"

        room_code = tracker.assign_best_fit(p.day, p.start_time, cap, room_type)
        if room_code:
            p.room = room_code
            p.save(update_fields=["room", "updated_at"])
            assigned += 1
        else:
            p.room = "UNASSIGNED"
            p.save(update_fields=["room", "updated_at"])
            unassigned += 1

    return {"assigned": assigned, "unassigned": unassigned}
