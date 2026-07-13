"""Tests for the same-course overlap predicates in the constraint engine (PR-2c).

Same-course was already interval-aware (via timetable_same_course); these pin the
board-level home in the engine and the delta form used by local/chain search.
"""

from __future__ import annotations

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_constraints import (
    has_same_course_overlap,
    move_introduces_same_course_overlap,
)


def _section(section_id: str, course: str, *day_starts: tuple[int, int]) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=s + 75) for (d, s) in day_starts],
        max_capacity=30,
        reserve_capacity=0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id=f"p_{section_id}",
    )


def test_same_course_sections_overlapping_is_flagged() -> None:
    # AI101 S1 and S2 both SUN 09:00-10:15 → same-course overlap.
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "AI101_S2": _section("AI101_S2", "AI101", (0, 540)),
    }
    assert has_same_course_overlap(sections) is True


def test_same_course_interval_overlap_is_flagged() -> None:
    # 09:00-10:15 (540-615) vs 09:30-10:45 (570-645) — interval overlap, different start.
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "AI101_S2": _section("AI101_S2", "AI101", (0, 570)),
    }
    assert has_same_course_overlap(sections) is True


def test_different_courses_overlapping_is_not_same_course() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "DS201_S1": _section("DS201_S1", "DS201", (0, 540)),
    }
    assert has_same_course_overlap(sections) is False


def test_same_course_different_days_is_not_overlap() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "AI101_S2": _section("AI101_S2", "AI101", (1, 540)),
    }
    assert has_same_course_overlap(sections) is False


def test_delta_ignores_preexisting_unrelated_same_course_overlap() -> None:
    # AI101 S1/S2 already overlap (pre-existing). A move touching only DS201_S1
    # (a different course) must not be rejected for the AI101 overlap.
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "AI101_S2": _section("AI101_S2", "AI101", (0, 540)),
        "DS201_S1": _section("DS201_S1", "DS201", (0, 780)),
    }
    assert has_same_course_overlap(sections) is True
    assert move_introduces_same_course_overlap(sections, {"DS201_S1"}) is False


def test_delta_detects_a_moved_section_overlapping_a_sibling() -> None:
    # Moving AI101_S2 onto S1's slot creates a same-course overlap.
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", (0, 540)),
        "AI101_S2": _section("AI101_S2", "AI101", (0, 540)),
    }
    assert move_introduces_same_course_overlap(sections, {"AI101_S2"}) is True
