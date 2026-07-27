"""A deliberately naive reference placer — **not** the solver.

S3 builds the real one. This exists for two honest reasons:

1. **S2 must be runnable end-to-end.** A validator with nothing to validate is a
   layer, and layers with no caller are exactly what killed the two previous
   rebuilds. This gives the checker a real board to grade.
2. **S3 needs a baseline.** "Better" is meaningless without something to be
   better than. This is the floor: first-fit, no lookahead, no objective.

It is first-fit and greedy on purpose. It respects the hard structure it can
respect cheaply (legal windows, distinct days, no double-booking of a room,
instructor and offering separation) and makes no attempt whatsoever at quality —
no student-gap reasoning, no load balancing, no room-utilisation objective. If
the real solver cannot beat this comfortably, the real solver is not working.
"""

from __future__ import annotations

from dataclasses import dataclass

from scheduler.domain import DeliveryMode, Snapshot, TimeWindow
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day


@dataclass
class _Occupancy:
    """Who is busy when. Plain interval bookkeeping — no turnover (D8)."""

    def __init__(self) -> None:
        self.by_room: dict[tuple[str, Day], list[TimeWindow]] = {}
        self.by_instructor: dict[tuple[int, Day], list[TimeWindow]] = {}
        self.by_offering: dict[tuple[str, Day], list[TimeWindow]] = {}
        self.by_section_day: set[tuple[str, Day]] = set()
        self.instructor_day_count: dict[tuple[int, Day], int] = {}

    @staticmethod
    def _free(slots: list[TimeWindow], window: TimeWindow) -> bool:
        return all(not window.overlaps(w) for w in slots)

    def room_free(self, room: str, day: Day, window: TimeWindow) -> bool:
        return self._free(self.by_room.get((room, day), []), window)

    def instructor_free(self, iid: int, day: Day, window: TimeWindow, cap: int) -> bool:
        if self.instructor_day_count.get((iid, day), 0) >= cap:
            return False
        return self._free(self.by_instructor.get((iid, day), []), window)

    def offering_free(self, oid: str, day: Day, window: TimeWindow) -> bool:
        return self._free(self.by_offering.get((oid, day), []), window)

    def take(self, p: Placement) -> None:
        if p.room_id is not None:
            self.by_room.setdefault((p.room_id, p.day), []).append(p.window)
        if p.instructor_id is not None:
            self.by_instructor.setdefault((p.instructor_id, p.day), []).append(p.window)
            key = (p.instructor_id, p.day)
            self.instructor_day_count[key] = self.instructor_day_count.get(key, 0) + 1
        self.by_offering.setdefault((p.offering_id, p.day), []).append(p.window)
        self.by_section_day.add((p.section_id, p.day))


def place_naively(snapshot: Snapshot, *, instructor_cap: int = 3) -> Board:
    """First-fit every required meeting. Leaves a meeting unroomed rather than
    dropping it — a room is an assignment that can be left unmade (D7)."""
    occupancy = _Occupancy()
    offerings = snapshot.offerings_by_id
    placements: list[Placement] = []

    # Largest sections first: they have the fewest room options, so placing them
    # while the grid is empty avoids trivially avoidable failures.
    sections = sorted(snapshot.sections, key=lambda s: (-s.capacity, s.id))

    for section in sections:
        offering = offerings[section.offering_id]
        meeting_index = 0
        for requirement in offering.requirements:
            for _ in range(requirement.count_per_week):
                meeting_index += 1
                placed = _place_one(
                    snapshot,
                    occupancy,
                    section,
                    offering,
                    requirement,
                    meeting_index,
                    instructor_cap,
                )
                if placed is not None:
                    placements.append(placed)
                    occupancy.take(placed)
    return Board(placements=tuple(placements))


def _place_one(snapshot, occupancy, section, offering, requirement, index, cap):
    needs_room = requirement.delivery is DeliveryMode.IN_PERSON
    candidate_rooms = (
        [
            r
            for r in snapshot.rooms
            if r.kind is requirement.kind
            and (offering.programs & r.programs)
            and r.capacity >= snapshot.policy.required_room_capacity(section.capacity)
        ]
        if needs_room
        else []
    )
    candidate_rooms.sort(key=lambda r: (r.capacity, r.id))  # best fit first

    for slot in snapshot.grid.day_windows_for(requirement.duration, requirement.delivery):
        day, window = slot.day, slot.window
        if (section.id, day) in occupancy.by_section_day:
            continue  # H2: one meeting per section per day
        if not occupancy.offering_free(offering.id, day, window):
            continue  # H10: sibling sections must not collide
        if section.instructor_id is not None and not occupancy.instructor_free(
            section.instructor_id, day, window, cap
        ):
            continue  # H7/H8
        if not needs_room:
            return _make(section, offering, index, requirement, day, window, None)
        for room in candidate_rooms:
            if occupancy.room_free(room.id, day, window):
                return _make(section, offering, index, requirement, day, window, room.id)
        # Every compatible room is busy at this window — try the next window
        # before giving up on a room entirely.
    # No window had a free room. Place it in time anyway, unroomed (D7).
    for slot in snapshot.grid.day_windows_for(requirement.duration, requirement.delivery):
        day, window = slot.day, slot.window
        if (section.id, day) in occupancy.by_section_day:
            continue
        if not occupancy.offering_free(offering.id, day, window):
            continue
        if section.instructor_id is not None and not occupancy.instructor_free(
            section.instructor_id, day, window, cap
        ):
            continue
        return _make(section, offering, index, requirement, day, window, None)
    return None  # genuinely nowhere legal — reported as a missing meeting by H1


def _make(section, offering, index, requirement, day, window, room_id):
    return Placement(
        section_id=section.id,
        offering_id=offering.id,
        meeting_index=index,
        kind=requirement.kind,
        delivery=requirement.delivery,
        day=day,
        window=window,
        room_id=room_id,
        instructor_id=section.instructor_id,
    )
