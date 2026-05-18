"""
Unit tests for the timetable overlap matrix module.
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    ScenarioStudentMap,
    Student,
    TimetableScenario,
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

    # Students 1-3: OV_A + OV_B
    for s in students[:3]:
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=s.student_id,
            primary_term=1,
            is_cross_term=False,
            recommended_courses=["OV_A", "OV_B"],
        )

    # Students 4-5: OV_A only
    for s in students[3:5]:
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=s.student_id,
            primary_term=1,
            is_cross_term=False,
            recommended_courses=["OV_A"],
        )

    # Students 6-7: OV_C only
    for s in students[5:7]:
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=s.student_id,
            primary_term=1,
            is_cross_term=False,
            recommended_courses=["OV_C"],
        )

    # Student 8: OV_B + OV_C
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=students[7].student_id,
        primary_term=1,
        is_cross_term=False,
        recommended_courses=["OV_B", "OV_C"],
    )

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

    def test_scoped_to_course_codes(self, overlap_scenario):
        cs_map = build_course_students_map(overlap_scenario.id, {"OV_A"})
        assert "OV_A" in cs_map
        assert "OV_B" not in cs_map
        assert "OV_C" not in cs_map


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
