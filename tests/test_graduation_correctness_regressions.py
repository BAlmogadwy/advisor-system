from __future__ import annotations

import pytest

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.advisor_presentations import graduation_presentation_from_tool_results
from core.services.student_graduation import (
    MAX_SIMULATED_TERMS,
    REGISTERED_TIMETABLE,
    build_graduation_report,
    build_graduation_what_if,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("invalid_term", [0, 3, "summer", None])
def test_graduation_report_rejects_non_main_academic_terms(invalid_term):
    with pytest.raises(ValueError, match=r"academic term must be 1 or 2"):
        build_graduation_report(4_401_991, 1448, invalid_term)


def test_graduation_what_if_inherits_main_term_validation():
    with pytest.raises(ValueError, match=r"academic term must be 1 or 2"):
        build_graduation_what_if(4_401_991, 1448, 3)


def test_presentation_keeps_every_term_the_calculator_can_simulate():
    term_plan = [
        {
            "sequence": sequence,
            "academic_year": 1448 + (sequence // 2),
            "term": 2 if sequence % 2 else 1,
            "courses": [
                {
                    "code": f"GT{sequence:02d}",
                    "name": f"Graduation term {sequence}",
                    "credits": 3,
                }
            ],
        }
        for sequence in range(1, MAX_SIMULATED_TERMS + 1)
    ]
    result = {
        "tool": "graduation_progress",
        "ok": True,
        "program": "GT",
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "planning_baseline_kind": REGISTERED_TIMETABLE,
        "planning_baseline_courses_assumed_passed": [],
        "term_plan": term_plan,
        "scenario_graph": {"items": [], "nameOf": {}, "statusOf": {}},
    }

    presentation = graduation_presentation_from_tool_results([result])

    final_code = f"GT{MAX_SIMULATED_TERMS:02d}"
    assert len(term_plan) == MAX_SIMULATED_TERMS == 24
    assert len(presentation["graph"]["extraNodes"]) == MAX_SIMULATED_TERMS
    assert presentation["graph"]["termOf"][final_code] == MAX_SIMULATED_TERMS + 1
    assert presentation["band_labels"][str(MAX_SIMULATED_TERMS + 1)].startswith("Projected ")


def test_registered_passed_retake_does_not_count_twice_toward_hour_gate():
    student_id = 4_401_992
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Passed retake graduation regression",
        program="GRT",
        section="M",
        status="active",
        total_earned_credits=97,
        current_registered_credits=3,
    )
    retake = Course.objects.create(
        course_code="GRT101",
        description="Already passed retake",
        credit_hours=3,
    )
    Course.objects.create(
        course_code="GRT499",
        description="Hundred-hour capstone",
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="GRT",
        course_code="GRT101",
        course_name="Already passed retake",
        type="Mandatory",
        programme_term=1,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="GRT",
        course_code="GRT499",
        course_name="Hundred-hour capstone",
        type="Mandatory",
        programme_term=10,
        credit_hours=3,
    )
    Prerequisite.objects.create(
        program="GRT",
        course_code="GRT499",
        prerequisite_course_code="100(HOURS)",
    )
    StudentCourse.objects.create(student=student, course=retake, status="passed")
    section = TermSection.objects.create(
        course_code="GRT101",
        course_number="GRT101",
        course_key="GRT101",
        course_name="Already passed retake",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        term_section=section,
        source="scraper_timetable",
    )

    report = build_graduation_report(
        student_id,
        1448,
        1,
        planning_baseline_kind=REGISTERED_TIMETABLE,
    )

    # The retake still occupies three credits of timetable load.
    assert report["planning_baseline_credits"] == 3
    # It cannot add those same credits to the already-earned registrar total.
    assert report["simulation_completed"] is False
    assert report["hour_gates"] == [
        {
            "code": "GRT499",
            "name": "Hundred-hour capstone",
            "required": 100,
            "effective": 97,
            "remaining": 3,
        }
    ]
    capstone = next(row for row in report["unresolved_requirements"] if row["code"] == "GRT499")
    assert capstone["credit_hour_gate"] == {
        "required": 100,
        "effective_in_scenario": 97,
        "remaining": 3,
    }
