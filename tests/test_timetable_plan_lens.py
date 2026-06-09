from __future__ import annotations

import pytest

from core.models import (
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_plan_lens import build_scenario_plan_lens

pytestmark = pytest.mark.django_db


def _create_student(student_id: int, program: str) -> None:
    Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name=f"Student {student_id}",
        program=program,
        section="M",
        status="active",
    )


def _add_request(
    scenario: TimetableScenario,
    student_id: int,
    course_key: str,
    *,
    primary_term: int,
) -> None:
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=student_id,
        course_key=course_key,
        course_code=course_key.split("::", 1)[0],
        primary_term=primary_term,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
    )


def test_plan_lens_allocates_plan_owned_and_shared_sections_without_mutating() -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Plan lens read only",
    )
    course_key = "CS111::PROGRAMMING_I"
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=course_key,
        course_code="CS111",
        course_name="PROGRAMMING I",
        department="CS",
        credit_hours=4,
        planned_sections=3,
        max_per_section=25,
        total_demand=65,
        programme_term=3,
    )
    for index, section in enumerate(["S1", "S2", "S3"], start=1):
        TermSection.objects.create(
            scenario=scenario,
            source_tag="tw_test",
            course_key=course_key,
            course_code="CS111",
            course_number="CS111",
            course_name="PROGRAMMING I",
            section=section,
            available_capacity=25,
            registered_count=0,
            created_at=str(index),
        )

    for offset in range(37):
        sid = 990000 + offset
        _create_student(sid, "AI")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=sid,
            primary_term=3,
            recommended_courses=["CS111"],
            recommended_course_keys=[course_key],
        )
        _add_request(scenario, sid, course_key, primary_term=3)
    for offset in range(28):
        sid = 991000 + offset
        _create_student(sid, "DS")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=sid,
            primary_term=3,
            recommended_courses=["CS111"],
            recommended_course_keys=[course_key],
        )
        _add_request(scenario, sid, course_key, primary_term=3)

    before = {
        "students": Student.objects.count(),
        "maps": ScenarioStudentMap.objects.count(),
        "requests": ScenarioStudentCourseRequest.objects.count(),
        "budgets": ScenarioSectionBudget.objects.count(),
        "sections": TermSection.objects.count(),
    }

    lens = build_scenario_plan_lens(scenario.id)

    after = {
        "students": Student.objects.count(),
        "maps": ScenarioStudentMap.objects.count(),
        "requests": ScenarioStudentCourseRequest.objects.count(),
        "budgets": ScenarioSectionBudget.objects.count(),
        "sections": TermSection.objects.count(),
    }
    assert after == before

    course = lens["courses"][course_key]
    assert course["plans"] == {"AI": 37, "DS": 28}
    assert course["total"] == 65
    assert course["planned_sections"] == 3
    assert course["max_per_section"] == 25
    assert course["allocation"] == {"AI": 1, "DS": 1, "shared": 1}
    assert course["shared"] is True
    assert course["shared_overflow"] is True
    assert course["shared_contributors"] == ["AI", "DS"]

    section_lens = list(lens["sections"].values())
    assert len(section_lens) == 3
    assert sum(1 for section in section_lens if section["owner"] == "AI") == 1
    assert sum(1 for section in section_lens if section["owner"] == "DS") == 1
    shared = [section for section in section_lens if section["owner"] == "SHARED"]
    assert len(shared) == 1
    assert shared[0]["filter_plans"] == ["AI", "DS"]
