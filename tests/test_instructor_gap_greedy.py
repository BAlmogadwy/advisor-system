"""WS-5: greedy construction-time instructor-gap scoring.

Unit tests for the helpers that let the greedy scorer prefer options which add
no idle gap to a section's instructors' day.
"""

from __future__ import annotations

from core.services.timetable_autoplace import (
    _day_gap_min,
    _hhmm_to_min,
    _option_instructor_gap_delta,
)


def _opt(day: str, start: str, end: str) -> list[dict]:
    return [{"day": day, "start": start, "end": end}]


def test_hhmm_to_min() -> None:
    assert _hhmm_to_min("09:00") == 540
    assert _hhmm_to_min("13:30") == 810
    assert _hhmm_to_min("bad") == -1


def test_day_gap_min() -> None:
    assert _day_gap_min([(540, 615), (780, 855)]) == 165
    assert _day_gap_min([(540, 615)]) == 0
    assert _day_gap_min([]) == 0


def test_delta_adds_idle_for_distant_placement() -> None:
    # Instructor 7 already teaches SUN 09:00-10:15; placing SUN 13:00-14:15
    # opens a 165-minute gap.
    instr_placed = {7: [("SUN", 540, 615)]}
    assert _option_instructor_gap_delta(_opt("SUN", "13:00", "14:15"), {7}, instr_placed) == 165


def test_delta_zero_back_to_back() -> None:
    instr_placed = {7: [("SUN", 540, 615)]}
    assert _option_instructor_gap_delta(_opt("SUN", "10:15", "11:30"), {7}, instr_placed) == 0


def test_delta_zero_other_day() -> None:
    instr_placed = {7: [("SUN", 540, 615)]}
    assert _option_instructor_gap_delta(_opt("MON", "13:00", "14:15"), {7}, instr_placed) == 0


def test_delta_zero_first_meeting() -> None:
    assert _option_instructor_gap_delta(_opt("SUN", "09:00", "10:15"), {7}, {}) == 0


def test_delta_empty_instructors() -> None:
    instr_placed = {7: [("SUN", 540, 615)]}
    assert _option_instructor_gap_delta(_opt("SUN", "13:00", "14:15"), set(), instr_placed) == 0


def test_delta_multi_instructor_sums() -> None:
    # Section taught by 7 and 12; only 7 has a prior meeting, so only 7 incurs a gap.
    instr_placed = {7: [("SUN", 540, 615)]}
    assert _option_instructor_gap_delta(_opt("SUN", "13:00", "14:15"), {7, 12}, instr_placed) == 165
