"""
Tests for the pair-feasibility max-flow checker.
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
from core.services.timetable_demand import sync_scenario_student_course_requests
from core.services.timetable_overlap import build_overlap_matrix
from core.services.timetable_pair_feasibility import (
    _bfs_augment,
    _sections_overlap,
    check_pair_feasibility,
    find_infeasible_hotspots,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def feasibility_scenario():
    """Create a scenario with two courses that fully overlap (infeasible)
    and two courses that don't overlap (feasible).
    """
    for code in ["PF_A", "PF_B", "PF_C"]:
        Course.objects.get_or_create(
            course_code=code,
            defaults={"credit_hours": 3, "department": "PF"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="PF",
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3},
        )

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Feasibility Test",
        slot_config=DEFAULT_SLOTS,
        lab_slot_config=DEFAULT_LAB_SLOTS,
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="PF",
        display_order=1,
    )

    # 20 students all take PF_A + PF_B + PF_C
    for i in range(20):
        s, _ = Student.objects.get_or_create(
            student_id=7700001 + i,
            defaults={"program": "PF", "section": "M", "name": f"PF Student {i}"},
        )
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=s.student_id,
            primary_term=1,
            is_cross_term=False,
            recommended_courses=["PF_A", "PF_B", "PF_C"],
        )

    # The overlap / pair-feasibility services source shared-student counts
    # from the canonical ``ScenarioStudentCourseRequest`` table, so mirror
    # the same 20 students × {PF_A, PF_B, PF_C} demand there too.
    sync_scenario_student_course_requests(
        scenario=scenario,
        classified_students=[
            {
                "student_id": 7700001 + i,
                "recommended_courses": ["PF_A", "PF_B", "PF_C"],
                "primary_term": 1,
                "is_cross_term": False,
            }
            for i in range(20)
        ],
        student_course_keys={7700001 + i: ["PF_A", "PF_B", "PF_C"] for i in range(20)},
        source="pair_feasibility_test",
    )

    for code in ["PF_A", "PF_B", "PF_C"]:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=code,
            department="PF",
            credit_hours=3,
            planned_sections=1,
            max_per_section=25,
            total_demand=20,
            programme_term=1,
        )

    # Place PF_A and PF_B at SAME time (infeasible pair)
    ts_a, _ = TermSection.objects.get_or_create(
        course_key="PF_A",
        section="S1",
        defaults={
            "course_code": "PF_A",
            "course_number": "PF_A",
            "course_name": "PF_A",
            "available_capacity": 25,
            "source_tag": "tw_auto",
        },
    )
    ts_b, _ = TermSection.objects.get_or_create(
        course_key="PF_B",
        section="S1",
        defaults={
            "course_code": "PF_B",
            "course_number": "PF_B",
            "course_name": "PF_B",
            "available_capacity": 25,
            "source_tag": "tw_auto",
        },
    )
    ts_c, _ = TermSection.objects.get_or_create(
        course_key="PF_C",
        section="S1",
        defaults={
            "course_code": "PF_C",
            "course_number": "PF_C",
            "course_name": "PF_C",
            "available_capacity": 25,
            "source_tag": "tw_auto",
        },
    )

    # PF_A and PF_B: SAME slot (SUN 09:00) → infeasible
    SectionPlacement.objects.create(
        board=board, term_section=ts_a, day="SUN", start_time="09:00", end_time="10:15"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts_a, day="TUE", start_time="09:00", end_time="10:15"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts_b, day="SUN", start_time="09:00", end_time="10:15"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts_b, day="TUE", start_time="09:00", end_time="10:15"
    )

    # PF_C: DIFFERENT slot (MON 10:30) → feasible with both A and B
    SectionPlacement.objects.create(
        board=board, term_section=ts_c, day="MON", start_time="10:30", end_time="11:45"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts_c, day="WED", start_time="10:30", end_time="11:45"
    )

    return scenario, board


class TestSectionsOverlap:
    def test_overlapping(self):
        a = [{"day": "SUN", "start_time": "09:00", "end_time": "10:15"}]
        b = [{"day": "SUN", "start_time": "09:00", "end_time": "10:15"}]
        assert _sections_overlap(a, b) is True

    def test_not_overlapping(self):
        a = [{"day": "SUN", "start_time": "09:00", "end_time": "10:15"}]
        b = [{"day": "MON", "start_time": "09:00", "end_time": "10:15"}]
        assert _sections_overlap(a, b) is False


class TestMaxFlow:
    def test_simple_flow(self):
        graph = {
            "S": {"A": 10, "B": 10},
            "A": {"T": 10},
            "B": {"T": 10},
        }
        assert _bfs_augment(graph, "S", "T") == 20

    def test_bottleneck(self):
        graph = {
            "S": {"A": 10},
            "A": {"T": 5},
        }
        assert _bfs_augment(graph, "S", "T") == 5


class TestCheckPairFeasibility:
    def test_infeasible_pair_detected(self, feasibility_scenario):
        scenario, board = feasibility_scenario
        matrix = build_overlap_matrix(scenario.id, {"PF_A", "PF_B", "PF_C"})
        results = check_pair_feasibility(board.id, matrix, threshold=15)

        # PF_A vs PF_B should be infeasible (both at SUN 09:00)
        ab_result = [
            r
            for r in results
            if (r["course_a"] == "PF_A" and r["course_b"] == "PF_B")
            or (r["course_a"] == "PF_B" and r["course_b"] == "PF_A")
        ]
        assert len(ab_result) == 1
        assert ab_result[0]["feasible"] is False
        assert ab_result[0]["max_assignable"] == 0

    def test_feasible_pair_ok(self, feasibility_scenario):
        scenario, board = feasibility_scenario
        matrix = build_overlap_matrix(scenario.id, {"PF_A", "PF_B", "PF_C"})
        results = check_pair_feasibility(board.id, matrix, threshold=15)

        # PF_A vs PF_C should be feasible (different times)
        ac_result = [
            r
            for r in results
            if (r["course_a"] == "PF_A" and r["course_b"] == "PF_C")
            or (r["course_a"] == "PF_C" and r["course_b"] == "PF_A")
        ]
        assert len(ac_result) == 1
        assert ac_result[0]["feasible"] is True


class TestFindInfeasibleHotspots:
    def test_finds_infeasible_only(self, feasibility_scenario):
        scenario, board = feasibility_scenario
        matrix = build_overlap_matrix(scenario.id, {"PF_A", "PF_B", "PF_C"})
        infeasible = find_infeasible_hotspots(board.id, matrix)
        assert len(infeasible) == 1  # only PF_A vs PF_B
        assert infeasible[0]["feasible"] is False
