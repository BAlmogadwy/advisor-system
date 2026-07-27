"""Temporal semantics for the scheduler — pure Python, no Django.

The declared slot grid is the **only** authority on legal meeting times (owner
decision D2: there is no prayer rule and no implicit blackout). A meeting is
legal iff it occupies a declared slot. Time-of-day policy is therefore changed by
editing the grid, which is versioned and fingerprinted, never by code.

All intervals are **half-open** ``[start, end)``: a meeting ending at 10:15 and
one starting at 10:15 do *not* overlap. This is stated once, here, and every
temporal predicate in the subsystem derives from it — the old engine's habit of
re-implementing overlap per stage (and drifting between exact-start equality and
interval overlap) is exactly what this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class Day(str, Enum):
    SUN = "SUN"
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"

    @property
    def index(self) -> int:
        return _DAY_ORDER[self]


_DAY_ORDER = {Day.SUN: 0, Day.MON: 1, Day.TUE: 2, Day.WED: 3, Day.THU: 4}


class MeetingKind(str, Enum):
    """What a meeting needs, which decides its room family and its grid."""

    LECTURE = "lecture"
    LAB = "lab"


class DeliveryMode(str, Enum):
    """How a meeting is delivered.

    ``ONLINE`` occupies time (it can clash for a student or an instructor) but
    consumes **no physical room** and creates no campus travel. Keeping this
    explicit avoids the old engine's habit of inferring online-ness from a
    course-code lookup at every call site.
    """

    IN_PERSON = "in_person"
    ONLINE = "online"


def parse_hhmm(value: str) -> int:
    """``"09:00"`` -> minutes since midnight. Raises on anything malformed."""
    text = str(value).strip()
    hh, _, mm = text.partition(":")
    if not _ or not hh.isdigit() or not mm.isdigit():
        raise ValueError(f"not a HH:MM time: {value!r}")
    hours, minutes = int(hh), int(mm)
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"time out of range: {value!r}")
    return hours * 60 + minutes


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True, order=True)
class TimeWindow:
    """A half-open interval within one day."""

    start: int  # minutes since midnight
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"non-positive window {self!r}")

    @property
    def duration(self) -> int:
        return self.end - self.start

    def overlaps(self, other: TimeWindow) -> bool:
        """Half-open overlap: touching endpoints do NOT overlap."""
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        return f"{format_hhmm(self.start)}-{format_hhmm(self.end)}"


@dataclass(frozen=True, slots=True, order=True)
class Slot:
    """One declared, legal placement cell.

    Identity is ``(day, window, kind, delivery)`` — never the start alone.
    Delivery is part of it because online teaching has its own declared family of
    times, deliberately placed late in the day so it does not compete with the
    on-campus grid.
    """

    day: Day
    window: TimeWindow
    kind: MeetingKind
    delivery: DeliveryMode = DeliveryMode.IN_PERSON

    def overlaps(self, other: Slot) -> bool:
        return self.day is other.day and self.window.overlaps(other.window)

    def __str__(self) -> str:
        return f"{self.day.value} {self.window} ({self.kind.value}/{self.delivery.value})"


@dataclass(frozen=True)
class Grid:
    """The declared set of legal slots — the sole time-of-day authority (D2).

    Deliberately *not* derived from credit hours or durations at runtime. If a
    duration has no declared slot, the answer is "there is nowhere legal to put
    it", which is an input finding, not something to paper over.
    """

    slots: tuple[Slot, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("a grid needs at least one slot")
        if len(set(self.slots)) != len(self.slots):
            raise ValueError("duplicate slots in grid")

    def of_kind(self, kind: MeetingKind) -> tuple[Slot, ...]:
        """Slots *declared* under a kind. This is a naming/provenance view — it is
        NOT what decides where a meeting may go. Use `windows_for(duration)`."""
        return tuple(s for s in self.slots if s.kind is kind)

    def durations(self, kind: MeetingKind) -> frozenset[int]:
        return frozenset(s.window.duration for s in self.of_kind(kind))

    def windows_for(
        self, duration: int, delivery: DeliveryMode = DeliveryMode.IN_PERSON
    ) -> tuple[TimeWindow, ...]:
        """Every legal *time* for a meeting of this length, on any day.

        **Timing family is decided by duration; room family is decided by kind.**
        They are independent, which is the institution's actual rule: a 100-minute
        *lecture* runs at the 100-minute (lab-timing) windows but occupies a
        **lecture** room. The old engine conflated the two and had to guess, which
        is why the same board could be judged valid under one reading of the rule
        and invalid under another.

        The consequence for room exclusivity is load-bearing: a 100-minute meeting
        at 09:00-10:40 overlaps a 75-minute one at 10:30-11:45 by ten minutes. Room
        conflicts must therefore be tested by **interval overlap**, never by equal
        start times.
        """
        return tuple(
            sorted(
                {
                    s.window
                    for s in self.slots
                    if s.window.duration == duration and s.delivery is delivery
                }
            )
        )

    def day_windows_for(
        self, duration: int, delivery: DeliveryMode = DeliveryMode.IN_PERSON
    ) -> tuple[Slot, ...]:
        """As `windows_for`, but as concrete (day, window) placements."""
        seen: set[tuple[Day, TimeWindow]] = set()
        out: list[Slot] = []
        for slot in sorted(self.slots):
            if slot.window.duration != duration or slot.delivery is not delivery:
                continue
            key = (slot.day, slot.window)
            if key in seen:
                continue
            seen.add(key)
            out.append(slot)
        return tuple(out)

    def placements_per_week(self, duration: int) -> int:
        """How many distinct (day, window) cells exist for this meeting length."""
        return len(self.day_windows_for(duration))

    def max_nonoverlapping_per_day(
        self,
        durations: frozenset[int],
        delivery: DeliveryMode = DeliveryMode.IN_PERSON,
    ) -> int:
        """Most meetings **one room** can host in one day, given these lengths.

        Counting declared cells overstates capacity, because several are mutually
        exclusive alternatives rather than additional capacity: 10:30-11:45 and
        10:50-12:05 are one lecture opportunity offered two ways, not two. A room
        can only ever hold a set of pairwise non-overlapping meetings.

        Exact for intervals via earliest-finish-time selection — this is a genuine
        upper bound on a room's daily throughput, so comparing it against required
        meetings is a counting *proof* of shortage, not an estimate.
        """
        windows = sorted(
            {
                s.window
                for s in self.slots
                if s.window.duration in durations and s.delivery is delivery
            },
            key=lambda w: (w.end, w.start),
        )
        count = 0
        cursor = -1
        for window in windows:
            if window.start >= cursor:
                count += 1
                cursor = window.end
        return count

    def room_periods_per_week(self, durations: frozenset[int], room_count: int) -> int:
        """Upper bound on meetings this many rooms can host per week."""
        return room_count * len(self.days()) * self.max_nonoverlapping_per_day(durations)

    def days(self) -> tuple[Day, ...]:
        return tuple(sorted({s.day for s in self.slots}, key=lambda d: d.index))

    @property
    def periods_per_week(self) -> dict[MeetingKind, int]:
        """How many placement cells exist per kind — the room-supply multiplier."""
        return {kind: len(self.of_kind(kind)) for kind in MeetingKind if self.of_kind(kind)}

    def fingerprint(self) -> str:
        payload = [
            [s.day.value, s.window.start, s.window.end, s.kind.value] for s in sorted(self.slots)
        ]
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @classmethod
    def from_spec(
        cls,
        *,
        lecture_starts: dict[str, int],
        lab_starts: dict[str, int],
        online_starts: dict[str, int] | None = None,
        days: tuple[Day, ...] = tuple(Day),
    ) -> Grid:
        """Build a grid from ``{start_hhmm: duration_minutes}`` maps.

        ``online_starts`` declares a **separate late-day family** for online
        teaching. It is deliberately its own family rather than a reuse of the
        lecture grid: online sessions are placed after the on-campus day so they
        do not compete with it, and they consume no room.
        """
        slots: list[Slot] = []
        families = [
            (MeetingKind.LECTURE, DeliveryMode.IN_PERSON, lecture_starts),
            (MeetingKind.LAB, DeliveryMode.IN_PERSON, lab_starts),
            (MeetingKind.LECTURE, DeliveryMode.ONLINE, online_starts or {}),
        ]
        for kind, delivery, starts in families:
            for hhmm, duration in starts.items():
                begin = parse_hhmm(hhmm)
                for day in days:
                    slots.append(
                        Slot(
                            day=day,
                            window=TimeWindow(begin, begin + duration),
                            kind=kind,
                            delivery=delivery,
                        )
                    )
        return cls(slots=tuple(sorted(slots)))
