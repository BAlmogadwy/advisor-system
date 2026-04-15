from __future__ import annotations

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_autoplace import get_meeting_pattern_variants
from core.services.timetable_candidate_eval import (
    evaluate_generated_timetable_candidate,
    rank_timetable_candidates,
)


def _section(
    section_id: str,
    course_code: str,
    meetings: list[tuple[int, int, int]],
    max_capacity: int = 5,
    reserve_capacity: int = 0,
) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[
            SectionMeeting(day=day, start_min=start_min, end_min=end_min)
            for day, start_min, end_min in meetings
        ],
        max_capacity=max_capacity,
        reserve_capacity=reserve_capacity,
    )


def _profiles() -> dict[str, StudentProfile]:
    return {
        "S1": StudentProfile(
            student_id="S1",
            department="CS",
            recommended_courses=["CS101", "MA101"],
            risk_tier=RiskTier.A,
            intra_tier_score=9.0,
        ),
        "S2": StudentProfile(
            student_id="S2",
            department="CS",
            recommended_courses=["CS101"],
            risk_tier=RiskTier.C,
            intra_tier_score=1.0,
        ),
    }


def test_get_meeting_pattern_variants_exposes_4_credit_permutations() -> None:
    assert get_meeting_pattern_variants(4) == [
        [100, 75, 75],
        [75, 100, 75],
        [75, 75, 100],
    ]
    assert get_meeting_pattern_variants(3) == [[75, 75]]


def test_evaluate_generated_timetable_candidate_assigns_without_mutating_input() -> None:
    sections = [
        _section("CS101-A", "CS101", [(0, 540, 615)]),
        _section("MA101-A", "MA101", [(1, 540, 615)]),
    ]

    result = evaluate_generated_timetable_candidate(
        candidate_id="good",
        generated_sections=sections,
        student_profiles=_profiles(),
        course_rigidity={"CS101": 1.0, "MA101": 1.0},
    )

    assert result.lexicographic_score == (0, 0, 0, 0, 0, 0)
    assert result.unresolved_student_ids == []
    assert result.hotspot_courses == []
    assert sections[0].current_enrollment == 0
    assert sections[1].current_enrollment == 0


def test_rank_timetable_candidates_prefers_lower_lexicographic_score() -> None:
    good_sections = [
        _section("CS101-A", "CS101", [(0, 540, 615)]),
        _section("MA101-A", "MA101", [(1, 540, 615)]),
    ]
    bad_sections = [
        _section("CS101-A", "CS101", [(0, 540, 615)]),
        _section("MA101-A", "MA101", [(0, 540, 615)]),
    ]

    ranked = rank_timetable_candidates(
        candidate_list=[
            {"id": "bad", "sections": bad_sections},
            {"id": "good", "sections": good_sections},
        ],
        student_profiles=_profiles(),
        course_rigidity={"CS101": 1.0, "MA101": 1.0},
    )

    assert [result.candidate_id for result in ranked] == ["good", "bad"]
    assert ranked[0].lexicographic_score < ranked[1].lexicographic_score
