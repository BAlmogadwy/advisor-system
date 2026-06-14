"""Unit tests for the instructor idle-gap metric (WS-1).

``_compute_instructor_idle_minutes`` mirrors the student day-gap metric but
keyed by instructor. It is the building block for the soft objective gated by
``TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED``.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_student_assignment import _compute_instructor_idle_minutes


def _section(
    section_id: str, course_code: str, meetings: list[tuple[int, int, int]]
) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=30,
        reserve_capacity=0,
    )


def _by_id(*sections: SectionState) -> dict[str, SectionState]:
    return {s.section_id: s for s in sections}


def test_single_instructor_same_day_gap() -> None:
    # SUN 09:00-10:15 then SUN 13:00-14:15 -> idle = 780 - 615 = 165 min
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    b = _section("MA101_S1", "MA101", [(0, 780, 855)])
    smap = {"CS101_S1": frozenset({7}), "MA101_S1": frozenset({7})}
    assert _compute_instructor_idle_minutes(_by_id(a, b), smap) == 165


def test_back_to_back_is_zero() -> None:
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    b = _section("MA101_S1", "MA101", [(0, 615, 690)])  # starts exactly when a ends
    smap = {"CS101_S1": frozenset({7}), "MA101_S1": frozenset({7})}
    assert _compute_instructor_idle_minutes(_by_id(a, b), smap) == 0


def test_different_days_no_gap() -> None:
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    b = _section("MA101_S1", "MA101", [(1, 780, 855)])  # different day
    smap = {"CS101_S1": frozenset({7}), "MA101_S1": frozenset({7})}
    assert _compute_instructor_idle_minutes(_by_id(a, b), smap) == 0


def test_empty_map_returns_zero() -> None:
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    assert _compute_instructor_idle_minutes(_by_id(a), {}) == 0


def test_unassigned_section_does_not_contribute() -> None:
    # Only CS101 has an instructor; MA101 is unassigned (absent from the map).
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    b = _section("MA101_S1", "MA101", [(0, 780, 855)])
    smap = {"CS101_S1": frozenset({7})}
    # Instructor 7 has a single meeting -> no gap; the unassigned section is invisible.
    assert _compute_instructor_idle_minutes(_by_id(a, b), smap) == 0


def test_multi_instructor_section_counts_for_each() -> None:
    # Shared lecture taught by {7, 12}; lab taught only by 7.
    shared = _section("CS101_S1", "CS101", [(0, 540, 615)])  # 09:00-10:15
    lab = _section("CS101_LAB", "CS101", [(0, 780, 855)])  # 13:00-14:15
    smap = {"CS101_S1": frozenset({7, 12}), "CS101_LAB": frozenset({7})}
    # instructor 7 sees both meetings -> gap 165; instructor 12 sees one -> 0.
    assert _compute_instructor_idle_minutes(_by_id(shared, lab), smap) == 165


def test_three_meetings_sums_consecutive_gaps() -> None:
    # 09:00-10:15, 11:00-12:15, 14:00-15:15 -> gaps 45 + 105 = 150
    a = _section("CS101_S1", "CS101", [(0, 540, 615)])
    b = _section("MA101_S1", "MA101", [(0, 660, 735)])
    c = _section("PH101_S1", "PH101", [(0, 840, 915)])
    smap = {
        "CS101_S1": frozenset({7}),
        "MA101_S1": frozenset({7}),
        "PH101_S1": frozenset({7}),
    }
    assert _compute_instructor_idle_minutes(_by_id(a, b, c), smap) == 150


# ── WS-2: evaluator conditional position-6 + flag-OFF parity contract ──────────


def _eval_profiles() -> dict[str, StudentProfile]:
    return {
        "S1": StudentProfile(
            student_id="S1",
            department="CS",
            recommended_courses=["CS101", "MA101"],
            risk_tier=RiskTier.A,
            intra_tier_score=9.0,
        ),
    }


def _eval(sections, smap):
    return evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=sections,
        student_profiles=_eval_profiles(),
        course_rigidity={"CS101": 1.0, "MA101": 1.0},
        section_instructor_ids=smap,
    ).lexicographic_score


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False)
def test_flag_off_keeps_six_tuple_even_with_map() -> None:
    sections = [_section("CS101_S1", "CS101", [(0, 540, 615)])]
    score = _eval(sections, {"CS101_S1": frozenset({7})})
    assert len(score) == 6  # byte-parity: shape unchanged when flag OFF


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=True)
def test_flag_on_without_map_keeps_six_tuple() -> None:
    sections = [_section("CS101_S1", "CS101", [(0, 540, 615)])]
    score = evaluate_generated_timetable_candidate(
        candidate_id="c",
        generated_sections=sections,
        student_profiles=_eval_profiles(),
        course_rigidity={"CS101": 1.0},
        section_instructor_ids=None,  # no map supplied
    ).lexicographic_score
    assert len(score) == 6


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=True)
def test_flag_on_with_map_appends_position_6() -> None:
    # Instructor 7 teaches both CS101 (09:00-10:15) and MA101 (13:00-14:15) on SUN
    # -> position 6 carries the 165-minute idle gap.
    sections = [
        _section("CS101_S1", "CS101", [(0, 540, 615)]),
        _section("MA101_S1", "MA101", [(0, 780, 855)]),
    ]
    smap = {"CS101_S1": frozenset({7}), "MA101_S1": frozenset({7})}
    score = _eval(sections, smap)
    assert len(score) == 7
    assert score[6] == 165
    # positions 0-5 are unchanged from the flag-OFF run (students-first)
    with override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False):
        off = _eval(sections, smap)
    assert tuple(score[:6]) == tuple(off)
