"""Tests for the instructor daily-cap predicates in the constraint engine (PR-2b).

The daily cap is a per-(instructor, day) session COUNT — no interval logic. These
pin the whole-board predicates and the delta form used by local/chain search.
"""

from __future__ import annotations

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_constraints import (
    count_instructor_daily_overloads,
    exceeds_instructor_daily_cap,
    move_exceeds_instructor_daily_cap,
)


def _section(section_id: str, *day_starts: tuple[int, int]) -> SectionState:
    """SectionState with one 75-min meeting per (day, start_min)."""
    return SectionState(
        section_id=section_id,
        course_code=section_id.split("_")[0],
        meetings=[SectionMeeting(day=d, start_min=s, end_min=s + 75) for (d, s) in day_starts],
        max_capacity=30,
        reserve_capacity=0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id=f"p_{section_id}",
    )


def test_exceeds_when_instructor_has_more_than_cap_sessions_on_a_day() -> None:
    # Instructor 7 teaches 3 sections on SUN (day 0) → exceeds cap of 2.
    sections = {
        "A_S1": _section("A_S1", (0, 540)),
        "B_S1": _section("B_S1", (0, 630)),
        "C_S1": _section("C_S1", (0, 780)),
    }
    instr = {sid: frozenset({7}) for sid in sections}
    assert exceeds_instructor_daily_cap(sections, instr, 2) is True
    assert exceeds_instructor_daily_cap(sections, instr, 3) is False


def test_sessions_spread_across_days_do_not_exceed() -> None:
    sections = {
        "A_S1": _section("A_S1", (0, 540)),
        "B_S1": _section("B_S1", (1, 540)),
        "C_S1": _section("C_S1", (2, 540)),
    }
    instr = {sid: frozenset({7}) for sid in sections}
    assert exceeds_instructor_daily_cap(sections, instr, 1) is False


def test_count_overloads_sums_excess_per_day() -> None:
    # SUN: 3 sessions (excess 1 over cap 2). MON: 4 sessions (excess 2). Total 3.
    sections = {
        "A_S1": _section("A_S1", (0, 540)),
        "B_S1": _section("B_S1", (0, 630)),
        "C_S1": _section("C_S1", (0, 780)),
        "D_S1": _section("D_S1", (1, 540)),
        "E_S1": _section("E_S1", (1, 630)),
        "F_S1": _section("F_S1", (1, 780)),
        "G_S1": _section("G_S1", (1, 870)),
    }
    instr = {sid: frozenset({7}) for sid in sections}
    assert count_instructor_daily_overloads(sections, instr, 2) == 3


def test_section_with_two_instructors_counts_toward_each() -> None:
    sections = {"A_S1": _section("A_S1", (0, 540)), "B_S1": _section("B_S1", (0, 630))}
    instr = {"A_S1": frozenset({7, 9}), "B_S1": frozenset({7})}
    # Instructor 7 has 2 SUN sessions; instructor 9 has 1. Cap 1 → 7 exceeds.
    assert exceeds_instructor_daily_cap(sections, instr, 1) is True


def test_empty_map_never_exceeds() -> None:
    sections = {"A_S1": _section("A_S1", (0, 540))}
    assert exceeds_instructor_daily_cap(sections, None, 1) is False
    assert exceeds_instructor_daily_cap(sections, {}, 1) is False
    assert count_instructor_daily_overloads(sections, {}, 1) == 0


def test_delta_ignores_preexisting_overcap_on_an_untouched_cell() -> None:
    # Instructor 7 already over cap (3 sessions) on SUN — a pre-existing overload.
    # A move that only touches D (instructor 9, MON) must not be rejected for it.
    sections = {
        "A_S1": _section("A_S1", (0, 540)),
        "B_S1": _section("B_S1", (0, 630)),
        "C_S1": _section("C_S1", (0, 780)),
        "D_S1": _section("D_S1", (1, 540)),
    }
    instr = {
        "A_S1": frozenset({7}),
        "B_S1": frozenset({7}),
        "C_S1": frozenset({7}),
        "D_S1": frozenset({9}),
    }
    assert exceeds_instructor_daily_cap(sections, instr, 2) is True  # whole-board sees it
    assert move_exceeds_instructor_daily_cap(sections, instr, 2, {"D_S1"}) is False  # delta ignores


def test_delta_detects_a_move_landing_on_an_overcap_cell() -> None:
    # Instructor 7 has 2 sessions on SUN. Moving C (instr 7) onto SUN makes 3 > cap 2.
    sections = {
        "A_S1": _section("A_S1", (0, 540)),
        "B_S1": _section("B_S1", (0, 630)),
        "C_S1": _section("C_S1", (0, 780)),  # C now on SUN too
    }
    instr = {sid: frozenset({7}) for sid in sections}
    assert move_exceeds_instructor_daily_cap(sections, instr, 2, {"C_S1"}) is True
