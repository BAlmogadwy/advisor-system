from __future__ import annotations

from core.services.timetable_load_balanced import _compute_balance_score
from core.services.timetable_local_search import _compute_cost
from core.services.timetable_same_course import (
    SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY,
    make_meeting_window,
    same_course_section_spread_penalty,
)


def test_shared_policy_rewards_back_to_back_same_course_sections() -> None:
    adjacent = {
        "AI331": [
            [make_meeting_window("AI331", "SUN", "09:00", "10:15", "S1")],
            [make_meeting_window("AI331", "SUN", "10:30", "11:45", "S2")],
        ]
    }
    separated = {
        "AI331": [
            [make_meeting_window("AI331", "SUN", "09:00", "10:15", "S1")],
            [make_meeting_window("AI331", "TUE", "09:00", "10:15", "S2")],
        ]
    }

    assert same_course_section_spread_penalty(adjacent) == 0
    assert (
        same_course_section_spread_penalty(separated) >= SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY
    )


def test_legacy_local_search_cost_uses_shared_same_course_policy() -> None:
    sections = [
        {"code": "AI331", "label": "S1"},
        {"code": "AI331", "label": "S2"},
    ]
    adjacent_schedule = {
        0: [{"day": "SUN", "start": "09:00", "end": "10:15", "mask": 1}],
        1: [{"day": "SUN", "start": "10:30", "end": "11:45", "mask": 2}],
    }
    separated_schedule = {
        0: [{"day": "SUN", "start": "09:00", "end": "10:15", "mask": 1}],
        1: [{"day": "TUE", "start": "09:00", "end": "10:15", "mask": 4}],
    }

    assert _compute_cost(separated_schedule, sections, set()) > _compute_cost(
        adjacent_schedule,
        sections,
        set(),
    )


def test_load_balancer_cost_uses_shared_same_course_policy() -> None:
    sections = [
        {"code": "DS331", "label": "S1"},
        {"code": "DS331", "label": "S2"},
    ]
    adjacent_schedule = {
        0: [{"day": "MON", "start": "09:00", "end": "10:15", "mask": 1}],
        1: [{"day": "MON", "start": "10:30", "end": "11:45", "mask": 2}],
    }
    separated_schedule = {
        0: [{"day": "MON", "start": "09:00", "end": "10:15", "mask": 1}],
        1: [{"day": "WED", "start": "09:00", "end": "10:15", "mask": 4}],
    }

    assert _compute_balance_score(separated_schedule, sections, set()) > _compute_balance_score(
        adjacent_schedule,
        sections,
        set(),
    )
