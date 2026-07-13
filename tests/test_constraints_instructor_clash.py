"""Tests for the interval-aware instructor-clash predicates (PR-2a).

These pin the correctness fix at the heart of the constraint engine: an
instructor double-booked across *overlapping* intervals (not just an identical
start minute) is a clash. The previous start-equality predicates missed the
interleaved lecture/lab grid case entirely.
"""

from __future__ import annotations

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_constraints import (
    count_instructor_clashes,
    has_instructor_clash,
    move_introduces_instructor_clash,
)


def _section(section_id: str, *meetings: tuple[int, int, int]) -> SectionState:
    """SectionState with the given (day, start_min, end_min) meetings."""
    return SectionState(
        section_id=section_id,
        course_code=section_id.split("_")[0],
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for (d, s, e) in meetings],
        max_capacity=30,
        reserve_capacity=0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id=f"p_{section_id}",
    )


# Day 0 = SUN. 10:30-11:45 = 630-705; 10:45-12:25 (lab) = 645-745;
# 10:50-12:05 = 650-725; 13:00-14:15 = 780-855; 14:30-15:45 = 870-945.


def test_interval_overlap_lecture_vs_lab_is_a_clash() -> None:
    # Same instructor (7) teaches a 10:30-11:45 lecture and a 10:45-12:25 lab on
    # the same day — different start minutes, overlapping intervals. The old
    # start-equality predicate MISSED this; the interval one must catch it.
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "AI101_LAB": _section("AI101_LAB", (0, 645, 745)),
    }
    instr = {"AI101_S1": frozenset({7}), "AI101_LAB": frozenset({7})}
    assert has_instructor_clash(sections, instr) is True
    assert count_instructor_clashes(sections, instr) == 1


def test_interval_overlap_two_lecture_slots_is_a_clash() -> None:
    # 10:30-11:45 vs 10:50-12:05 — the exact case the 10:50 slot introduced.
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "DS201_S1": _section("DS201_S1", (0, 650, 725)),
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({7})}
    assert has_instructor_clash(sections, instr) is True


def test_non_overlapping_same_day_is_not_a_clash() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 780, 855)),  # 13:00-14:15
        "DS201_S1": _section("DS201_S1", (0, 870, 945)),  # 14:30-15:45
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({7})}
    assert has_instructor_clash(sections, instr) is False
    assert count_instructor_clashes(sections, instr) == 0


def test_touching_end_to_start_is_not_a_clash() -> None:
    # 10:30-11:45 then 11:45-13:00 — half-open intervals, adjacent, no overlap.
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "DS201_S1": _section("DS201_S1", (0, 705, 780)),
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({7})}
    assert has_instructor_clash(sections, instr) is False


def test_different_instructors_sharing_a_slot_is_not_a_clash() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "DS201_S1": _section("DS201_S1", (0, 630, 705)),
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({9})}
    assert has_instructor_clash(sections, instr) is False


def test_different_days_same_time_is_not_a_clash() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),  # SUN
        "DS201_S1": _section("DS201_S1", (1, 630, 705)),  # MON
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({7})}
    assert has_instructor_clash(sections, instr) is False


def test_empty_instructor_map_is_never_a_clash() -> None:
    sections = {"AI101_S1": _section("AI101_S1", (0, 630, 705))}
    assert has_instructor_clash(sections, None) is False
    assert has_instructor_clash(sections, {}) is False
    assert count_instructor_clashes(sections, {}) == 0


def test_delta_ignores_preexisting_unrelated_clash() -> None:
    # A & B already clash (instructor 7). C belongs to instructor 9 and overlaps
    # nothing. A move that only touches C must NOT be rejected for the A/B clash.
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "AI101_LAB": _section("AI101_LAB", (0, 645, 745)),  # clashes with AI101_S1
        "CS300_S1": _section("CS300_S1", (0, 870, 945)),  # unrelated, instr 9
    }
    instr = {
        "AI101_S1": frozenset({7}),
        "AI101_LAB": frozenset({7}),
        "CS300_S1": frozenset({9}),
    }
    # Whole-board sees the pre-existing clash...
    assert has_instructor_clash(sections, instr) is True
    # ...but the delta form, asked about the unrelated move, does not block it.
    assert move_introduces_instructor_clash(sections, instr, {"CS300_S1"}) is False


def test_delta_detects_a_newly_created_clash() -> None:
    # Moving DS201 (instructor 7) onto a slot overlapping AI101 creates a clash.
    sections = {
        "AI101_S1": _section("AI101_S1", (0, 630, 705)),
        "DS201_S1": _section("DS201_S1", (0, 650, 725)),  # now overlaps AI101
    }
    instr = {"AI101_S1": frozenset({7}), "DS201_S1": frozenset({7})}
    assert move_introduces_instructor_clash(sections, instr, {"DS201_S1"}) is True
