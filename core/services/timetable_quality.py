from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
)

TIMETABLE_QUALITY_POLICY = "timetable-quality-v1"
WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]
DAY_LABEL_BY_INDEX = {index: day for index, day in enumerate(WEEKDAYS)}


def evaluate_timetable_quality(
    sections: list[SectionState],
    assignment_states: dict[str, StudentAssignmentState] | None = None,
    *,
    previous_room_by_section: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Return soft timetable-quality diagnostics for ranking equal hard outcomes."""

    sections_by_id = {section.section_id: section for section in sections}
    weak_slot = sum(
        meeting_weak_slot_penalty(meeting.day, meeting.start_min, meeting.end_min)
        for section in sections
        for meeting in section.meetings
    )
    day_counts = Counter(
        meeting.day
        for section in sections
        for meeting in section.meetings
        if 0 <= meeting.day < len(WEEKDAYS)
    )
    components = {
        "weak_slot": weak_slot,
        "day_balance": day_balance_penalty(day_counts),
        "spare_capacity": spare_capacity_penalty(sections),
        "section_balance": section_balance_penalty(sections),
        "room_change": room_change_penalty(sections, previous_room_by_section or {}),
        "student_day_overload": student_day_overload_penalty(
            assignment_states or {},
            sections_by_id,
        ),
    }
    total = sum(int(value or 0) for value in components.values())
    return {
        "policy": TIMETABLE_QUALITY_POLICY,
        "penalty": total,
        "components": components,
        "day_load": {
            DAY_LABEL_BY_INDEX[index]: int(day_counts.get(index, 0))
            for index in range(len(WEEKDAYS))
        },
        "reasons": quality_reason_rows(components),
    }


def meeting_weak_slot_penalty(day: int, start_min: int, end_min: int) -> int:
    """Penalty for slots that are valid but operationally less desirable."""

    penalty = 0
    if start_min < 8 * 60:
        penalty += 40
    if start_min >= 16 * 60:
        penalty += 45
    if end_min > 18 * 60 + 10:
        penalty += 60
    if day == 4 and start_min >= 14 * 60:
        penalty += 35
    if 12 * 60 + 30 <= start_min < 14 * 60:
        penalty += 10
    return penalty


def day_balance_penalty(day_counts: Counter[int] | dict[int, int]) -> int:
    counts = [int(day_counts.get(index, 0)) for index in range(len(WEEKDAYS))]
    total = sum(counts)
    if total <= 0:
        return 0
    average = total / len(WEEKDAYS)
    spread = max(counts) - min(counts)
    deviation = sum(abs(count - average) for count in counts)
    return int(round((deviation * 8) + (spread * 5)))


def spare_capacity_penalty(sections: list[SectionState]) -> int:
    penalty = 0
    for section in sections:
        regular_limit = max(0, section.regular_limit())
        regular_remaining = regular_limit - int(section.current_enrollment or 0)
        if int(section.current_enrollment or 0) >= int(section.max_capacity or 0):
            penalty += 120
        elif regular_remaining < 0:
            penalty += 70 + abs(regular_remaining) * 8
        elif regular_remaining == 0:
            penalty += 45
        elif regular_remaining <= max(1, int(regular_limit * 0.1)):
            penalty += 15
    return penalty


def section_balance_penalty(sections: list[SectionState]) -> int:
    penalty = 0
    by_course: dict[str, list[SectionState]] = defaultdict(list)
    for section in sections:
        by_course[section.course_code].append(section)
    for course_sections in by_course.values():
        if len(course_sections) < 2:
            continue
        utilizations = [
            int(round((section.current_enrollment / max(1, section.max_capacity)) * 100))
            for section in course_sections
        ]
        penalty += max(utilizations) - min(utilizations)
    return penalty


def room_change_penalty(
    sections: list[SectionState],
    previous_room_by_section: dict[str, str | None],
) -> int:
    if not previous_room_by_section:
        return 0
    penalty = 0
    for section in sections:
        previous = str(previous_room_by_section.get(section.section_id) or "").strip()
        current = str(section.assigned_room_id or "").strip()
        if previous and current and previous != current:
            penalty += 20
    return penalty


def student_day_overload_penalty(
    assignment_states: dict[str, StudentAssignmentState],
    sections_by_id: dict[str, SectionState],
    *,
    meeting_limit_per_day: int = 3,
) -> int:
    penalty = 0
    for state in assignment_states.values():
        day_counts: Counter[int] = Counter()
        for section_id in state.section_ids:
            section = sections_by_id.get(section_id)
            if not section:
                continue
            for meeting in section.meetings:
                day_counts[meeting.day] += 1
        for count in day_counts.values():
            if count > meeting_limit_per_day:
                penalty += (count - meeting_limit_per_day) * 30
    return penalty


def quality_reason_rows(components: dict[str, int]) -> list[dict[str, Any]]:
    labels = {
        "weak_slot": "Uses weaker teaching slots",
        "day_balance": "Uneven day distribution",
        "spare_capacity": "Consumes spare capacity",
        "section_balance": "Uneven same-course section load",
        "room_change": "Requires room changes",
        "student_day_overload": "Creates heavy student days",
    }
    rows = [
        {
            "component": key,
            "label": labels.get(key, key.replace("_", " ").title()),
            "penalty": int(value or 0),
        }
        for key, value in components.items()
        if int(value or 0) > 0
    ]
    rows.sort(key=lambda row: (-int(row["penalty"]), str(row["component"])))
    return rows


def section_meeting(day: int, start_min: int, end_min: int) -> SectionMeeting:
    return SectionMeeting(day=day, start_min=start_min, end_min=end_min)
