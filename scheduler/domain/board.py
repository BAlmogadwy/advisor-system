"""A proposed timetable — pure Python, no Django.

A `Board` is what a solver produces and a checker grades. It is deliberately a
plain value: no database rows, no lazy loading, no hidden state. Two boards with
the same placements are equal, and a board can be constructed in a test in three
lines.

Room assignment is **optional** on a placement (owner decision D7): a meeting may
be scheduled in time with no room, and that is a legitimate, publishable-blocking
but build-legal state — not a failure. `room_id=None` means "no room assigned",
never "no room needed"; the latter is a property of the *meeting requirement*
(`DeliveryMode.ONLINE`), which is a different thing entirely.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .calendar import Day, DeliveryMode, MeetingKind, TimeWindow
from .entities import InstructorId, OfferingId, RoomId, SectionId


@dataclass(frozen=True, slots=True)
class Placement:
    """One meeting of one section, placed at a time and (maybe) in a room."""

    section_id: SectionId
    offering_id: OfferingId
    meeting_index: int  # 1-based within the section's required multiset
    kind: MeetingKind
    delivery: DeliveryMode
    day: Day
    window: TimeWindow
    room_id: RoomId | None = None
    instructor_id: InstructorId | None = None
    #: False when this meeting is housed in the course's OWN rooms and consumes
    #: none of the shared estate (D19). See `MeetingRequirement.uses_shared_room`.
    uses_shared_room: bool = True

    @property
    def id(self) -> str:
        return f"{self.section_id}#M{self.meeting_index}"

    @property
    def needs_room(self) -> bool:
        """Whether this meeting should consume a room FROM THE SHARED ESTATE,
        independent of whether one has been assigned.

        False for online meetings, which need no room at all, and for courses
        with rooms of their own (D19) — those are in-person and occupy a student
        and an instructor, but their space is not ours to allocate or to run out
        of.
        """
        return self.delivery is DeliveryMode.IN_PERSON and self.uses_shared_room

    @property
    def is_roomed(self) -> bool:
        return self.room_id is not None

    def overlaps(self, other: Placement) -> bool:
        """Same-day, half-open interval overlap.

        No turnover allowance (D8): back-to-back and touching meetings in one room
        are legal, because they cost no capacity and shorten student days.
        """
        return self.day is other.day and self.window.overlaps(other.window)

    def __str__(self) -> str:
        room = self.room_id or "—"
        return f"{self.section_id} {self.day.value} {self.window} [{room}]"


@dataclass(frozen=True)
class Board:
    """A complete proposed timetable for one snapshot."""

    placements: tuple[Placement, ...]

    @property
    def by_section(self) -> dict[SectionId, tuple[Placement, ...]]:
        out: dict[SectionId, list[Placement]] = defaultdict(list)
        for p in self.placements:
            out[p.section_id].append(p)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def by_offering(self) -> dict[OfferingId, tuple[Placement, ...]]:
        out: dict[OfferingId, list[Placement]] = defaultdict(list)
        for p in self.placements:
            out[p.offering_id].append(p)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def physical(self) -> tuple[Placement, ...]:
        return tuple(p for p in self.placements if p.needs_room)

    @property
    def unroomed(self) -> tuple[Placement, ...]:
        """Physical meetings with no room — reported, never a build failure (D7)."""
        return tuple(p for p in self.placements if p.needs_room and not p.is_roomed)

    def summary(self) -> dict:
        return {
            "placements": len(self.placements),
            "sections_placed": len(self.by_section),
            "physical_meetings": len(self.physical),
            "roomed": len(self.physical) - len(self.unroomed),
            "unroomed": len(self.unroomed),
        }
