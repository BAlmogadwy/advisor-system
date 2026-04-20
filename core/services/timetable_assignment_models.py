from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class RiskTier(IntEnum):
    C = 1
    B = 2
    A = 3


@dataclass(frozen=True)
class StudentProfile:
    student_id: str
    department: str
    recommended_courses: list[str]
    risk_tier: RiskTier
    intra_tier_score: float


@dataclass(frozen=True)
class SectionMeeting:
    day: int
    start_min: int
    end_min: int
    slot_size: int = 5
    mask: int = field(init=False)

    def __post_init__(self) -> None:
        if self.slot_size <= 0:
            raise ValueError("slot_size must be positive")
        if self.end_min <= self.start_min:
            raise ValueError("end_min must be greater than start_min")
        if self.start_min < 0:
            raise ValueError("start_min must be non-negative")
        if self.day < 0 or self.day > 6:
            raise ValueError("day must be between 0 and 6")
        start_idx = self.start_min // self.slot_size
        end_idx = self.end_min // self.slot_size
        object.__setattr__(self, "mask", ((1 << (end_idx - start_idx)) - 1) << start_idx)


@dataclass
class SectionState:
    section_id: str
    course_code: str
    meetings: list[SectionMeeting]
    max_capacity: int
    reserve_capacity: int
    current_enrollment: int = 0
    enrolled_student_ids: set[str] = field(default_factory=set)
    pattern_id: str = ""
    pattern_family: str = ""
    assigned_room_id: str | None = None
    room_type_required: str = "lecture"
    demand_capacity: int | None = None

    def __post_init__(self) -> None:
        if self.max_capacity < 0:
            raise ValueError("max_capacity cannot be negative")
        if self.reserve_capacity < 0 or self.reserve_capacity > self.max_capacity:
            raise ValueError("reserve_capacity must be between 0 and max_capacity")
        if self.current_enrollment < 0 or self.current_enrollment > self.max_capacity:
            raise ValueError("current_enrollment must be between 0 and max_capacity")

    def regular_limit(self) -> int:
        return self.max_capacity - self.reserve_capacity

    def regular_used(self) -> int:
        return min(self.current_enrollment, self.regular_limit())

    def reserve_used(self) -> int:
        return max(0, self.current_enrollment - self.regular_limit())

    def can_enroll(self, allow_reserve: bool) -> bool:
        if self.current_enrollment >= self.max_capacity:
            return False
        if self.current_enrollment < self.regular_limit():
            return True
        return allow_reserve

    def room_demand(self) -> int:
        return self.demand_capacity if self.demand_capacity is not None else self.max_capacity


@dataclass(frozen=True)
class UnresolvedReason:
    course_code: str
    reason: str


@dataclass
class StudentAssignmentState:
    student_id: str
    assigned_sections: dict[str, str] = field(default_factory=dict)
    section_ids: set[str] = field(default_factory=set)
    occupied_mask_by_day: dict[int, int] = field(default_factory=lambda: {i: 0 for i in range(7)})
    total_gap_minutes: int = 0
    unresolved_courses: dict[str, UnresolvedReason] = field(default_factory=dict)

    def has_clash(self, meetings: list[SectionMeeting]) -> bool:
        for meeting in meetings:
            if self.occupied_mask_by_day.get(meeting.day, 0) & meeting.mask:
                return True
        return False

    def check_clash(self, meetings: list[SectionMeeting]) -> int:
        clashes = 0
        for meeting in meetings:
            if self.occupied_mask_by_day.get(meeting.day, 0) & meeting.mask:
                clashes += 1
        return clashes


@dataclass(frozen=True)
class CanonicalPattern:
    pattern_id: str
    signature: str
    meetings: list[SectionMeeting]
    pattern_family: str
    duration_permutation: str
    is_lab_mixed: bool
    meeting_count: int
    days_used: frozenset[int]
    slot_fingerprint: str


@dataclass(frozen=True)
class TimetableMove:
    move_type: str
    section_id_a: str
    from_pattern_id_a: str
    to_pattern_id_a: str
    section_id_b: str | None = None
    from_pattern_id_b: str | None = None
    to_pattern_id_b: str | None = None


@dataclass
class SectionGridSnapshot:
    section_id: str
    old_pattern_id: str
    old_meetings: list[SectionMeeting]
    old_room_id: str | None


@dataclass
class MoveSnapshot:
    snapshots: list[SectionGridSnapshot]


@dataclass(frozen=True)
class RoomProfile:
    room_id: str
    capacity: int
    room_type: str
    gender: str


@dataclass
class RoomOccupancy:
    room_id: str
    occupied_mask_by_day: dict[int, int] = field(default_factory=lambda: {i: 0 for i in range(7)})
    assigned_section_ids: set[str] = field(default_factory=set)

    def can_accommodate(self, meetings: list[SectionMeeting]) -> bool:
        for meeting in meetings:
            if self.occupied_mask_by_day.get(meeting.day, 0) & meeting.mask:
                return False
        return True

    def rebuild_from_truth(self, sections_by_id: dict[str, SectionState]) -> None:
        self.occupied_mask_by_day = {i: 0 for i in range(7)}
        for sec_id in self.assigned_section_ids:
            sec = sections_by_id[sec_id]
            for meeting in sec.meetings:
                self.occupied_mask_by_day[meeting.day] |= meeting.mask


@dataclass
class TimetableEvaluationResult:
    candidate_id: str
    lexicographic_score: tuple[int, int, int, int, int, int]
    assignment_states: dict[str, StudentAssignmentState]
    unresolved_student_ids: list[str]
    hotspot_courses: list[str]
    capacity_pressure_courses: list[str]
    reserve_heavy_sections: list[tuple[str, float]]
