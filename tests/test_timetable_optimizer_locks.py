from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat
from core.services.timetable_local_search_v2 import (
    generate_all_repattern_moves,
    generate_swap_moves,
)
from core.services.timetable_optimizer_v2 import build_locked_section_ids_for_scenario
from core.services.timetable_rooming import assign_rooms_to_board


def _section(
    section_id: str, course_code: str, pattern_id: str, day: int, start: int
) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=day, start_min=start, end_min=start + 75)],
        max_capacity=30,
        reserve_capacity=0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id=pattern_id,
    )


def _pattern(pattern_id: str, day: int, start: int) -> CanonicalPattern:
    meeting = SectionMeeting(day=day, start_min=start, end_min=start + 75)
    return CanonicalPattern(
        pattern_id=pattern_id,
        signature=pattern_id,
        meetings=[meeting],
        pattern_family="ONCAMPUS_LEC_75",
        duration_permutation="75",
        is_lab_mixed=False,
        meeting_count=1,
        days_used=frozenset({day}),
        slot_fingerprint=f"{day}:{start}",
    )


def test_local_search_move_generation_skips_locked_sections() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", "p1", 0, 540),
        "DS201_S1": _section("DS201_S1", "DS201", "p2", 1, 540),
    }
    catalog = {
        "ONCAMPUS_LEC_75": [
            _pattern("p1", 0, 540),
            _pattern("p2", 1, 540),
            _pattern("p3", 2, 540),
        ]
    }
    locked = {"AI101_S1"}

    repattern_moves = generate_all_repattern_moves(
        sections,
        catalog,
        locked_section_ids=locked,
    )
    swap_moves = generate_swap_moves(
        sections,
        catalog,
        hotspot_courses=["AI101", "DS201"],
        locked_section_ids=locked,
    )

    assert repattern_moves
    assert all(move.section_id_a not in locked for move in repattern_moves)
    assert all(
        move.section_id_a not in locked and move.section_id_b not in locked for move in swap_moves
    )


@pytest.mark.django_db
def test_locked_section_ids_freeze_whole_section_when_one_meeting_is_locked() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="lock map")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        is_locked=True,
    )

    assert build_locked_section_ids_for_scenario(scenario.id) == {"AI101_S1"}


@pytest.mark.django_db
def test_optimiser_rooming_respects_locked_unassigned_rooms() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="room lock")
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
    )
    Room.objects.create(
        room_code="AI-101",
        capacity=40,
        room_type="lecture",
        department="AI",
        section="M",
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI101",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
    )
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
        is_locked=True,
    )

    result = assign_rooms_to_board(board.id, respect_locked=True)

    placement.refresh_from_db()
    assert placement.room == "UNASSIGNED"
    assert result["assigned"] == 0
    assert result["locked_skipped"] == 1


@pytest.mark.django_db
def test_cpsat_polisher_keeps_locked_sections_out_of_solution() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="cpsat lock")
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=990001,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    for course in ["AI101", "DS201"]:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=990001,
            course_key=course,
            course_code=course,
            primary_term=1,
            status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
            source="test",
        )
    sections = [
        _section("AI101_S1", "AI101", "p1", 0, 540),
        _section("DS201_S1", "DS201", "p1", 0, 540),
    ]
    profiles = {
        "990001": StudentProfile(
            student_id="990001",
            department="AI",
            recommended_courses=["AI101", "DS201"],
            risk_tier=RiskTier.C,
            intra_tier_score=0,
        )
    }
    current_eval = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
    )

    result = polish_scenario_with_cpsat(
        scenario_id=scenario.id,
        current_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
        current_eval=current_eval,
        time_limit_seconds=5,
        locked_section_ids={"AI101_S1"},
    )

    assert result is not None
    improved_ids = {section.section_id for section in result["improved_sections"]}
    assert "AI101_S1" not in improved_ids
