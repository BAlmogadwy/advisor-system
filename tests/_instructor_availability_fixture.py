"""Builders shared by the instructor teaching-time rule engine tests (not collected)."""

from __future__ import annotations

from collections.abc import Iterable

from core.models import Instructor, InstructorTimeRule
from core.services.instructor_availability import mask_from_days, to_minutes
from tests.test_section_day_spread import PRODUCTION_FLAGS

#: Production's engine flags plus the day spread, with the rules ON.
AVAILABILITY_FLAGS = {
    **PRODUCTION_FLAGS,
    "TIMETABLE_SECTION_DAY_SPREAD": True,
    "TIMETABLE_SECTION_DAY_SPREAD_WEIGHT": 40,
    "TIMETABLE_INSTRUCTOR_AVAILABILITY": True,
}

ALL_DAYS = ("SUN", "MON", "TUE", "WED", "THU")


def rule(
    person: Instructor | int,
    kind: str,
    days: Iterable[str] = ALL_DAYS,
    start: str = "00:00",
    end: str = "24:00",
) -> None:
    """Store one rule (``bulk_create``, as a seed import would)."""
    InstructorTimeRule.objects.bulk_create(
        [
            InstructorTimeRule(
                instructor_id=getattr(person, "pk", person),
                kind=kind,
                day_mask=mask_from_days(days),
                start_minute=to_minutes(start),
                end_minute=to_minutes(end),
            )
        ]
    )


def rule_queries(captured: Iterable[dict]) -> list[str]:
    """The captured SQL statements that read the rules table."""
    return [q["sql"] for q in captured if "instructor_time_rules" in q["sql"]]
