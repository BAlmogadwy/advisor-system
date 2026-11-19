"""
Unit tests for the timetable overlap matrix module.
"""

from __future__ import annotations

import pytest
from pytest_django.fixtures import DjangoAssertNumQueries

from core.models import (
    Course,
    ProgrammeRequirement,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    Student,
    TimetableScenario,
)
from core.services.timetable_demand import (
    compute_scenario_course_demand,
    demand_read_scope,
    load_scenario_course_demands,
)
from core.services.timetable_overlap import (
    build_course_students_map,
    build_overlap_matrix,
    course_overlap_load,
    courses_share_students,
    overlap_key,
    shared_student_count,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def overlap_scenario():
    """Create a scenario with students having different course combinations.

    Students 1-3: take BOTH OV_A and OV_B (shared)
    Students 4-5: take ONLY OV_A (no overlap with OV_C)
    Students 6-7: take ONLY OV_C (no overlap with OV_A)
    Student 8: takes OV_B and OV_C (shared)
    """
    for code in ["OV_A", "OV_B", "OV_C"]:
        Course.objects.get_or_create(
            course_code=code,
            defaults={"credit_hours": 3, "department": "OV"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="OV",
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3},
        )

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Overlap Test",
    )

    students = []
    for i in range(8):
        s, _ = Student.objects.get_or_create(
            student_id=8800001 + i,
            defaults={"program": "OV", "section": "M", "name": f"OV Student {i}"},
        )
        students.append(s)

    def add_requests(student, courses: list[str]) -> None:
        for course in courses:
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario,
                student_id=student.student_id,
                course_key=course,
                course_code=course,
                primary_term=1,
                status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            )

    # Students 1-3: OV_A + OV_B
    for s in students[:3]:
        add_requests(s, ["OV_A", "OV_B"])

    # Students 4-5: OV_A only
    for s in students[3:5]:
        add_requests(s, ["OV_A"])

    # Students 6-7: OV_C only
    for s in students[5:7]:
        add_requests(s, ["OV_C"])

    # Student 8: OV_B + OV_C
    add_requests(students[7], ["OV_B", "OV_C"])

    return scenario


class TestOverlapKey:
    def test_sorted_order(self):
        assert overlap_key("CS211", "AI331") == ("AI331", "CS211")
        assert overlap_key("AI331", "CS211") == ("AI331", "CS211")

    def test_same_course(self):
        assert overlap_key("CS211", "CS211") == ("CS211", "CS211")

    def test_normalization(self):
        assert overlap_key("cs 211", "AI 331") == ("AI331", "CS211")


class TestBuildCourseStudentsMap:
    def test_basic(self, overlap_scenario):
        cs_map = build_course_students_map(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert len(cs_map["OV_A"]) == 5  # students 1-5
        assert len(cs_map["OV_B"]) == 4  # students 1-3 + 8
        assert len(cs_map["OV_C"]) == 3  # students 6-7 + 8

    def test_ignores_map_only_rows_without_canonical_requests(self, overlap_scenario):
        student_id = Student.objects.values_list("student_id", flat=True).first()
        assert student_id is not None
        ScenarioStudentMap.objects.create(
            scenario=overlap_scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["NR_A"],
            recommended_course_keys=["NR_A"],
        )

        cs_map = build_course_students_map(overlap_scenario.id, {"OV_A", "NR_A"})
        demand = compute_scenario_course_demand(overlap_scenario.id)
        rows = load_scenario_course_demands(overlap_scenario.id)

        assert len(cs_map["OV_A"]) == 5
        assert "NR_A" not in cs_map
        assert demand["OV_A"] == 5
        assert "NR_A" not in demand
        assert "NR_A" not in [row.course_key for row in rows]

    def test_scoped_to_course_codes(self, overlap_scenario):
        cs_map = build_course_students_map(overlap_scenario.id, {"OV_A"})
        assert "OV_A" in cs_map
        assert "OV_B" not in cs_map
        assert "OV_C" not in cs_map


def test_demand_scope_preserves_filters_and_independent_results(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    scenario = TimetableScenario.objects.create(name="Demand scope")
    other = TimetableScenario.objects.create(name="Other demand scope")
    for student_id, key, status in (
        (2, "CS101::B", ScenarioStudentCourseRequest.STATUS_SERVED),
        (1, "CS101::A", ScenarioStudentCourseRequest.STATUS_BLOCKED),
        (3, "CS101::A", ScenarioStudentCourseRequest.STATUS_IGNORED),
    ):
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=student_id,
            course_key=key,
            course_code="CS101",
            status=status,
            source_payload={"course": {"key": key}},
        )

    with demand_read_scope():
        with django_assert_num_queries(1):
            subset = load_scenario_course_demands(scenario.id, course_keys=[" cs 101::b "])
            assert [row.student_id for row in subset] == [2]
            course = subset.pop().source_payload["course"]
            assert isinstance(course, dict)
            course["key"] = "changed"
            rows = load_scenario_course_demands(scenario.id)
            assert [(row.student_id, row.course_key) for row in rows] == [
                (1, "CS101::A"),
                (2, "CS101::B"),
            ]
            assert rows[1].source_payload == {"course": {"key": "CS101::B"}}
            assert load_scenario_course_demands(scenario.id, course_keys=["CS101"]) == []

        inactive = [
            ScenarioStudentCourseRequest.STATUS_IGNORED,
            ScenarioStudentCourseRequest.STATUS_CANCELLED,
        ]
        with django_assert_num_queries(1):
            ignored = load_scenario_course_demands(scenario.id, statuses=inactive)
            assert [row.student_id for row in ignored] == [3]
            assert load_scenario_course_demands(scenario.id, statuses=reversed(inactive)) == ignored

        with django_assert_num_queries(1):
            assert load_scenario_course_demands(other.id) == []
            assert load_scenario_course_demands(other.id) == []
            assert load_scenario_course_demands(scenario.id) == rows

    ScenarioStudentCourseRequest.objects.filter(scenario=scenario, student_id=2).update(
        status=ScenarioStudentCourseRequest.STATUS_CANCELLED
    )
    with django_assert_num_queries(1):
        assert [row.student_id for row in load_scenario_course_demands(scenario.id)] == [1]


class TestBuildOverlapMatrix:
    def test_shared_pairs(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        # OV_A and OV_B: 3 shared students (students 1-3)
        assert matrix[overlap_key("OV_A", "OV_B")] == 3

        # OV_B and OV_C: 1 shared student (student 8)
        assert matrix[overlap_key("OV_B", "OV_C")] == 1

    def test_no_overlap(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        # OV_A and OV_C: 0 shared students
        assert matrix.get(overlap_key("OV_A", "OV_C"), 0) == 0

    def test_scoped_to_board_courses(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B"})
        # Only OV_A/OV_B pair should exist
        assert overlap_key("OV_A", "OV_B") in matrix
        assert overlap_key("OV_B", "OV_C") not in matrix


class TestCoursesShareStudents:
    def test_shared(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert courses_share_students(matrix, "OV_A", "OV_B") is True
        assert courses_share_students(matrix, "OV_B", "OV_C") is True

    def test_not_shared(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert courses_share_students(matrix, "OV_A", "OV_C") is False

    def test_same_course(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert courses_share_students(matrix, "OV_A", "OV_A") is True

    def test_same_course_normalized(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert courses_share_students(matrix, " ov_a ", "OV_A") is True


class TestSharedStudentCount:
    def test_counts(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert shared_student_count(matrix, "OV_A", "OV_B") == 3
        assert shared_student_count(matrix, "OV_B", "OV_C") == 1
        assert shared_student_count(matrix, "OV_A", "OV_C") == 0

    def test_same_course_sentinel(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert shared_student_count(matrix, "OV_A", "OV_A") == 999

    def test_same_course_sentinel_normalized(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        assert shared_student_count(matrix, " ov_a ", "OV_A") == 999


class TestCourseOverlapLoad:
    def test_load(self, overlap_scenario):
        matrix = build_overlap_matrix(overlap_scenario.id, {"OV_A", "OV_B", "OV_C"})
        # OV_A: overlaps with OV_B (3) + OV_C (0) = 3
        assert course_overlap_load(matrix, "OV_A") == 3

        # OV_B: overlaps with OV_A (3) + OV_C (1) = 4
        assert course_overlap_load(matrix, "OV_B") == 4

        # OV_C: overlaps with OV_B (1) + OV_A (0) = 1
        assert course_overlap_load(matrix, "OV_C") == 1
