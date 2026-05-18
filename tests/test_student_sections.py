from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
)

pytestmark = pytest.mark.django_db


def test_partial_section_mapping_keeps_unmapped_studying_courses_in_baseline() -> None:
    student = Student.objects.create(
        student_id=990846,
        registration_no="990846",
        name="Partial Mapping Student",
        program="DS",
        status="active",
    )
    mapped_course = Course.objects.create(
        course_code="MATH203",
        description="Calculus I",
        credit_hours=3,
    )
    unmapped_course = Course.objects.create(
        course_code="CS112",
        description="Programming II",
        credit_hours=4,
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="MATH203",
        course_name="Calculus I",
        programme_term=4,
        credit_hours=3,
        type="Mandatory",
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="CS112",
        course_name="Programming II",
        programme_term=4,
        credit_hours=4,
        type="Mandatory",
    )
    StudentCourse.objects.create(student=student, course=mapped_course, status="studying")
    StudentCourse.objects.create(student=student, course=unmapped_course, status="studying")

    term_section = TermSection.objects.create(
        course_code="MATH203",
        course_number="MATH203",
        course_key="MATH203",
        course_name="Calculus I",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=term_section,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1447",
        term="2",
        term_section=term_section,
        source="auto_from_studying",
    )

    baseline = append_unmapped_studying_courses(
        student.student_id,
        get_student_term_baseline(student.student_id, "1447", "2"),
    )

    by_code = {row["course_key"]: row for row in baseline}
    assert set(by_code) == {"MATH203", "CS112"}
    assert by_code["MATH203"]["credits"] == 3
    assert by_code["MATH203"]["day"] == "SUN"
    assert by_code["CS112"]["credits"] == 4
    assert by_code["CS112"]["source"] == "fallback_studying"
    assert by_code["CS112"]["day"] == ""


def test_scenario_owned_sections_do_not_become_registered_baseline() -> None:
    student = Student.objects.create(
        student_id=990847,
        registration_no="990847",
        name="Scenario Section Student",
        program="DS",
        status="active",
    )
    course = Course.objects.create(
        course_code="GS104",
        description="Islamic Values",
        credit_hours=2,
    )
    StudentCourse.objects.create(student=student, course=course, status="studying")
    scenario = TimetableScenario.objects.create(
        academic_year="1447",
        term="2",
        name="Generated scenario",
    )
    generated_section = TermSection.objects.create(
        scenario=scenario,
        source_tag="tw_auto",
        course_code="GS104",
        course_number="GS104",
        course_key="GS104",
        course_name="Islamic Values",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=generated_section,
        day="SUN",
        start_time="16:30",
        end_time="18:10",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1447",
        term="2",
        term_section=generated_section,
        source="auto_from_studying",
    )

    baseline = append_unmapped_studying_courses(
        student.student_id,
        get_student_term_baseline(student.student_id, "1447", "2"),
    )

    assert baseline == [
        {
            "course_code": "GS104",
            "course_key": "GS104",
            "course_name": "Islamic Values",
            "course_number": "",
            "section": "",
            "registered_count": None,
            "credits": 2,
            "day": "",
            "start_time": "",
            "end_time": "",
            "room": "",
            "instructor": "",
            "term_section_id": None,
            "source": "fallback_studying",
        }
    ]
