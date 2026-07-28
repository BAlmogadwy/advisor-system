"""The scheduling entities — pure Python, no Django.

Identity rules (non-negotiable N1/N2 in the blueprint):

* an **offering** is what students enrol in and what sections belong to. Its id is
  opaque and stable; the display ``course_code`` is *never* an identifier, because
  one code can map to two different offerings with different demand;
* a **section** belongs to exactly one offering and has exactly **one** schedule.
  It is not duplicated per board or per term — term membership is a set on the
  offering. This is what makes "the same class scheduled at two different times"
  unrepresentable rather than merely discouraged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .calendar import DeliveryMode, MeetingKind

OfferingId = str
SectionId = str
RoomId = str
InstructorId = int
StudentId = int


@dataclass(frozen=True, slots=True)
class MeetingRequirement:
    """One weekly meeting an offering requires — the compiled truth (N5).

    Credit hours are an *input* to compiling this, never a runtime substitute for
    it. The old engine re-derived meeting shape from credits and duration at
    several call sites, with three different lab-duration literals between them.
    """

    kind: MeetingKind
    delivery: DeliveryMode
    duration: int  # minutes
    count_per_week: int

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise ValueError("meeting duration must be positive")
        if self.count_per_week <= 0:
            raise ValueError("count_per_week must be positive")

    @property
    def needs_room(self) -> bool:
        return self.delivery is DeliveryMode.IN_PERSON


@dataclass(frozen=True, slots=True)
class Offering:
    """A course as actually offered to a cohort this term.

    ``is_scheduled`` is False for courses the timetable does not place at all —
    graduation projects and cooperative training. They carry credit and appear on
    a student's record, but they have no weekly meeting, no room and no slot, so
    scheduling them would invent contact time that does not exist.
    """

    id: OfferingId
    course_code: str  # display only — never an identity
    course_name: str
    credit_hours: int
    programs: frozenset[str]
    terms: frozenset[int]
    requirements: tuple[MeetingRequirement, ...]
    capacity: int  # seats per section
    capacity_is_declared: bool  # False => policy default was used (D3, reported)
    is_scheduled: bool = True  # False => graduation project / training
    #: Resolution priority, from the project's own classifier: "T1" specialised
    #: major, "T2" shared foundation, "T3" general education / free elective.
    #:
    #: Owner rule: T2 and T3 courses are taught in other sections right across
    #: the college, so a student who cannot fit one here will find a seat
    #: elsewhere. A collision involving one is therefore not worth distorting
    #: everybody's week to avoid.
    tier: str = "T1"

    @property
    def meetings_per_week(self) -> int:
        return sum(r.count_per_week for r in self.requirements)

    @property
    def physical_meetings_per_week(self) -> int:
        return sum(r.count_per_week for r in self.requirements if r.needs_room)

    @property
    def is_fully_online(self) -> bool:
        return all(not r.needs_room for r in self.requirements)


@dataclass(frozen=True, slots=True)
class Section:
    """One deliverable instance of an offering. Exactly one schedule. Ever.

    ``instructor_id`` is **optional by design, permanently**. The owner's rule:
    not every course will have an instructor linked, even in real operation. So a
    section without one is a normal, valid section — not a degraded or incomplete
    state. Instructor constraints (clash, daily cap) simply do not apply to it.

    The consequence for reporting is stated once and enforced everywhere: any
    instructor metric MUST be published together with its coverage, because a
    figure computed over 20% of meetings is not a statement about the timetable.
    """

    id: SectionId
    offering_id: OfferingId
    index: int  # 1-based, for display ("S1", "S2")
    capacity: int
    instructor_id: InstructorId | None = None

    @property
    def label(self) -> str:
        return f"S{self.index}"

    @property
    def has_instructor(self) -> bool:
        return self.instructor_id is not None


@dataclass(frozen=True, slots=True)
class Room:
    """A physical room already filtered to the snapshot's gender (D1)."""

    id: RoomId
    code: str
    capacity: int
    kind: MeetingKind
    programs: frozenset[str]

    # There was a `serves()` helper here. It was never called, and its docstring
    # promised "a room must serve EVERY programme that shares the meeting" while
    # its body — because of an `or` — reduced to a plain overlap test. Every live
    # call site (the placer, the validator, the shortfall report) tests overlap:
    # `offering.programs & room.programs`. Dead code that documents semantics the
    # system does not implement is worse than no code, so it is gone rather than
    # "fixed" — whether a shared course actually requires a room serving *all* of
    # its programmes is an owner's policy question, still open, and not one to
    # settle by quietly repairing an uncalled method. See blueprint §7.


@dataclass(frozen=True, slots=True)
class Instructor:
    id: InstructorId
    name: str
    eligible_offerings: frozenset[OfferingId]


@dataclass(frozen=True, slots=True)
class StudentDemand:
    """What one student needs this term, from the upstream recommender.

    ``scheduler`` consumes this; it does not compute it. Advising policy lives in
    the recommender and must have exactly one implementation (blueprint §0.2).
    """

    student_id: StudentId
    program: str
    offering_ids: frozenset[OfferingId]
    #: True when this student's courses span more than one curriculum term.
    #:
    #: A student taking a coherent term-N block has a week that CAN be compact;
    #: somebody picking up leftovers from terms 3, 5 and 7 has a scattered set by
    #: construction, and no timetable makes it tidy. Owner rule 2026-07-28:
    #: optimise waiting time for the regular students, and for the rest simply
    #: guarantee they can register without a clash.
    is_cross_term: bool = False


@dataclass(frozen=True)
class CapacityPolicy:
    """How section counts and sizes are decided (D3)."""

    default_capacity: int
    buffer: float = 1.0  # multiplier applied when sizing a room to a section

    def __post_init__(self) -> None:
        if self.default_capacity <= 0:
            raise ValueError("default_capacity must be positive")
        if self.buffer < 1.0:
            raise ValueError("buffer must be >= 1.0")

    def required_room_capacity(self, section_capacity: int) -> int:
        import math

        return math.ceil(section_capacity * self.buffer)


@dataclass
class DemandIndex:
    """Per-offering demand, derived once from the student demand set."""

    by_offering: dict[OfferingId, int] = field(default_factory=dict)
    students_by_offering: dict[OfferingId, frozenset[StudentId]] = field(default_factory=dict)
    #: The same index over REGULAR students only — those whose whole load sits in
    #: one curriculum term. Kept beside the full one rather than replacing it,
    #: because the two answer different questions: a CLASH must be counted for
    #: everybody, and waiting time is only worth optimising for the students
    #: whose week can actually be made tidy.
    regular_by_offering: dict[OfferingId, frozenset[StudentId]] = field(default_factory=dict)

    @classmethod
    def build(cls, demands: tuple[StudentDemand, ...]) -> DemandIndex:
        students: dict[OfferingId, set[StudentId]] = {}
        regular: dict[OfferingId, set[StudentId]] = {}
        for demand in demands:
            for offering_id in demand.offering_ids:
                students.setdefault(offering_id, set()).add(demand.student_id)
                if not demand.is_cross_term:
                    regular.setdefault(offering_id, set()).add(demand.student_id)
        return cls(
            by_offering={k: len(v) for k, v in students.items()},
            students_by_offering={k: frozenset(v) for k, v in students.items()},
            regular_by_offering={k: frozenset(v) for k, v in regular.items()},
        )

    def shared_students(self, a: OfferingId, b: OfferingId) -> int:
        return len(
            self.students_by_offering.get(a, frozenset())
            & self.students_by_offering.get(b, frozenset())
        )

    def shared_regular_students(self, a: OfferingId, b: OfferingId) -> int:
        """Shared students whose whole load sits in one curriculum term."""
        return len(
            self.regular_by_offering.get(a, frozenset())
            & self.regular_by_offering.get(b, frozenset())
        )
