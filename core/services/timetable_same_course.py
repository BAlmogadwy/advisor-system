from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SAME_COURSE_BACK_TO_BACK_GRACE_MINUTES = 15
SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY = 5000
SAME_COURSE_DIFFERENT_DAY_PENALTY = 1000
SAME_COURSE_OVERLAP_PENALTY = 10000
DAY_ORDER = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


@dataclass(frozen=True)
class SameCourseMeetingWindow:
    course_code: str
    day: Any
    start_min: int
    end_min: int
    section_key: str = ""


def parse_time_to_minutes(value: int | str) -> int:
    if isinstance(value, int):
        return value
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def make_meeting_window(
    course_code: str,
    day: Any,
    start: int | str,
    end: int | str,
    section_key: str = "",
) -> SameCourseMeetingWindow:
    return SameCourseMeetingWindow(
        course_code=course_code,
        day=day,
        start_min=parse_time_to_minutes(start),
        end_min=parse_time_to_minutes(end),
        section_key=section_key,
    )


def meeting_gap_or_overlap(
    left: SameCourseMeetingWindow,
    right: SameCourseMeetingWindow,
) -> int | None:
    """Return same-day idle gap, -1 for overlap, or None for different days."""
    if left.day != right.day:
        return None
    if left.start_min < right.end_min and right.start_min < left.end_min:
        return -1
    return (
        right.start_min - left.end_min
        if left.end_min <= right.start_min
        else left.start_min - right.end_min
    )


def is_back_to_back_gap(gap: int | None) -> bool:
    return gap is not None and 0 <= gap <= SAME_COURSE_BACK_TO_BACK_GRACE_MINUTES


def has_back_to_back_pair(meetings: Sequence[SameCourseMeetingWindow]) -> bool:
    for idx, left in enumerate(meetings):
        for right in meetings[idx + 1 :]:
            if left.course_code != right.course_code:
                continue
            if is_back_to_back_gap(meeting_gap_or_overlap(left, right)):
                return True
    return False


def first_overlapping_same_course_window(
    candidate_meetings: Sequence[SameCourseMeetingWindow],
    existing_meetings: Sequence[SameCourseMeetingWindow],
) -> tuple[SameCourseMeetingWindow, SameCourseMeetingWindow] | None:
    for candidate in candidate_meetings:
        for existing in existing_meetings:
            if candidate.course_code != existing.course_code:
                continue
            if meeting_gap_or_overlap(candidate, existing) == -1:
                return candidate, existing
    return None


def has_same_course_overlap(meetings: Sequence[SameCourseMeetingWindow]) -> bool:
    for idx, left in enumerate(meetings):
        for right in meetings[idx + 1 :]:
            if left.course_code != right.course_code:
                continue
            if left.section_key and left.section_key == right.section_key:
                continue
            if meeting_gap_or_overlap(left, right) == -1:
                return True
    return False


def same_course_candidate_penalty(
    candidate_meetings: Sequence[SameCourseMeetingWindow],
    existing_meetings: Sequence[SameCourseMeetingWindow],
) -> int:
    """Penalty for placing a section away from existing same-course sections."""
    if not existing_meetings:
        return 0

    existing_pair = has_back_to_back_pair(existing_meetings)
    missing_adjacent_meetings = 0
    spread_penalty = 0

    for candidate in candidate_meetings:
        same_course_existing = [
            existing
            for existing in existing_meetings
            if existing.course_code == candidate.course_code
        ]
        if not same_course_existing:
            continue

        gaps = [meeting_gap_or_overlap(candidate, existing) for existing in same_course_existing]
        if any(gap == -1 for gap in gaps):
            spread_penalty += SAME_COURSE_OVERLAP_PENALTY
            continue

        same_day_gaps = [gap for gap in gaps if gap is not None]
        if not same_day_gaps:
            missing_adjacent_meetings += 1
            spread_penalty += SAME_COURSE_DIFFERENT_DAY_PENALTY
            continue

        nearest_gap = min(same_day_gaps)
        if is_back_to_back_gap(nearest_gap):
            continue

        missing_adjacent_meetings += 1
        spread_penalty += _same_day_gap_penalty(nearest_gap)

    if not existing_pair and missing_adjacent_meetings:
        spread_penalty += missing_adjacent_meetings * SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY
    return spread_penalty


def same_course_section_spread_penalty(
    section_meetings_by_course: Mapping[str, Sequence[Sequence[SameCourseMeetingWindow]]],
) -> int:
    """Penalty for courses whose sections do not form at least one adjacent pair.

    The rule is shared by timetable generation, post-processing, and the
    student-assignment evaluator:

    * one-section courses are neutral
    * two-section courses should be back-to-back
    * three-or-more-section courses should have at least one back-to-back pair
    """
    total = 0
    for sections in section_meetings_by_course.values():
        if len(sections) < 2:
            continue

        anchors: list[SameCourseMeetingWindow] = []
        for meetings in sections:
            if not meetings:
                continue
            anchors.append(min(meetings, key=_meeting_sort_key))

        if len(anchors) < 2:
            continue

        has_adjacent_pair = False
        for idx, left in enumerate(anchors):
            for right in anchors[idx + 1 :]:
                gap = meeting_gap_or_overlap(left, right)
                if gap is None:
                    total += SAME_COURSE_DIFFERENT_DAY_PENALTY
                elif gap == -1:
                    total += SAME_COURSE_OVERLAP_PENALTY
                elif is_back_to_back_gap(gap):
                    has_adjacent_pair = True
                else:
                    total += _same_day_gap_penalty(gap)

        if not has_adjacent_pair:
            total += SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY
    return total


def _same_day_gap_penalty(gap: int) -> int:
    if gap <= 30:
        return 30
    if gap <= 120:
        return 120
    return gap


def same_day_gap_penalty(gap: int) -> int:
    return _same_day_gap_penalty(gap)


def _meeting_sort_key(meeting: SameCourseMeetingWindow) -> tuple[int, int, int, str]:
    if isinstance(meeting.day, int):
        day_index = meeting.day
    else:
        day_index = DAY_ORDER.get(str(meeting.day).upper()[:3], 99)
    return (day_index, meeting.start_min, meeting.end_min, str(meeting.day))
