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

    # First pass: mark rooms already assigned (from greedy or previous run)
    for p in placements:
        if p.room:
            tracker.usage[(p.day, p.start_time)].add(p.room)

    # Get actual students per section and credit hours from budget
    from core.models import ScenarioSectionBudget

    all_budgets = ScenarioSectionBudget.objects.filter(scenario=board.scenario)
    budget_map = {}
    credit_map = {}
    for b in all_budgets:
        raw = (
            -(-b.total_demand // b.planned_sections)
            if b.planned_sections > 0
            else b.max_per_section
        )
        budget_map[b.course_code] = int(raw * 1.1)
        credit_map[b.course_code] = b.credit_hours

    # Sort unassigned placements by capacity DESC (largest first = best-fit-decreasing)
    unassigned_placements = [p for p in placements if not p.room]
    unassigned_placements.sort(key=lambda p: -(budget_map.get(p.term_section.course_code, 40)))

    assigned = 0
    unassigned = 0

    for p in unassigned_placements:
        cap = budget_map.get(p.term_section.course_code, 40)
        cr = credit_map.get(p.term_section.course_code, 3)
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        # Only 4-credit courses have lab meetings. 2-credit 100-min
        # meetings are long lectures, not labs.
        room_type = "lab" if (duration > 80 and cr == 4) else "lecture"

        # For lab meetings, don't filter by capacity — lab rooms have a
        # fixed physical size (computers/benches).
        room_cap = 0 if room_type == "lab" else cap
        # Prefer per-section gender (exam-style sections like 'M1'/'F1');
        # fall back to the board-level gender (timetable-style 'S1'/'S2').
        gender = _section_gender(p.term_section.section) or board_gender
        room_code = tracker.assign_best_fit(p.day, p.start_time, room_cap, room_type, gender)
        if room_code:
            p.room = room_code
            p.save(update_fields=["room", "updated_at"])
            assigned += 1
        else:
            p.room = "UNASSIGNED"
            p.save(update_fields=["room", "updated_at"])
            unassigned += 1

    return {"assigned": assigned, "unassigned": unassigned}
