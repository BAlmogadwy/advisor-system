"""`scheduler.domain` — the pure problem model. **Never imports Django.**

That single rule is what makes the solver reproducible, unit-testable without a
database, and impossible to couple accidentally to live tables. It is enforced by
a test (`test_scheduler_domain_is_pure`), not just by convention.
"""

from .calendar import (
    Day,
    DeliveryMode,
    Grid,
    MeetingKind,
    Slot,
    TimeWindow,
    format_hhmm,
    parse_hhmm,
)
from .entities import (
    CapacityPolicy,
    DemandIndex,
    Instructor,
    InstructorId,
    MeetingRequirement,
    Offering,
    OfferingId,
    Room,
    RoomId,
    Section,
    SectionId,
    StudentDemand,
    StudentId,
)
from .snapshot import Snapshot

__all__ = [
    "CapacityPolicy",
    "Day",
    "DeliveryMode",
    "DemandIndex",
    "Grid",
    "Instructor",
    "InstructorId",
    "MeetingKind",
    "MeetingRequirement",
    "Offering",
    "OfferingId",
    "Room",
    "RoomId",
    "Section",
    "SectionId",
    "Slot",
    "Snapshot",
    "StudentDemand",
    "StudentId",
    "TimeWindow",
    "format_hhmm",
    "parse_hhmm",
]
