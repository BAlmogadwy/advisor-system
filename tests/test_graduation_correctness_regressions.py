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
from core.services.advisor_graduation_optimization import OPTIMIZED_CURRENT_OFFERINGS
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


def test_optimized_baseline_stays_hypothetical_in_presentation_provenance():
    result = {
        "tool": "graduation_progress",
        "ok": True,
        "program": "OPT",
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "planning_baseline_kind": OPTIMIZED_CURRENT_OFFERINGS,
        "planning_baseline_courses_assumed_passed": [
            {"code": "OPT200", "name": "Optimized candidate", "credits": 3}
        ],
        "term_plan": [],
        "scenario_graph": {
            "items": [],
            "nameOf": {"OPT200": "Optimized candidate"},
            "statusOf": {"OPT200": "studying"},
        },
    }

    presentation = graduation_presentation_from_tool_results([result])

    assert presentation["planning_baseline_kind"] == OPTIMIZED_CURRENT_OFFERINGS
    assert presentation["band_labels"]["1"] == "Optimized current offerings 1448/1"
    assert presentation["graph"]["statusOf"]["OPT200"] == "open"


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


def _plan_row(program: str, code: str, term: int, credits: int, name: str) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        type="Mandatory",
        programme_term=term,
        credit_hours=credits,
    )


def test_plan_credits_survive_a_registrar_total_that_undercounts_a_passed_course():
    """A zero-slack hour gate must not be lost in the seam between two credit systems.

    The scenario schedules future courses at the PLAN's credit value but used to
    seed itself from the registrar's aggregate alone. A plan course already passed
    whose credits are missing from that aggregate was therefore counted by neither
    side: never re-scheduled, because the scenario knows it is passed, and never
    added, because the registrar never had it.

    That is not hypothetical. Fourteen DS2 students hold a passed CS111 (4 credits)
    that the registrar's `total_earned_credits` omits. All six second-cohort plans
    gate co-op at exactly `plan_total - co-op credits`, so those four credits were
    the difference between 143 and the 147 the gate demands, and all fourteen lost
    their forecast entirely.

    Here ZSG101's four credits are absent from the registrar total, and the gate
    needs every credit in the plan bar the co-op's own.
    """

    student_id = 4_401_993
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Undercounted registrar total",
        program="ZSG",
        section="M",
        status="active",
        # The registrar has no record of ZSG101's four credits.
        total_earned_credits=0,
        current_registered_credits=3,
    )
    undercounted = Course.objects.create(
        course_code="ZSG101", description="Passed but uncounted", credit_hours=4
    )
    Course.objects.create(course_code="ZSG201", description="Registered now", credit_hours=3)
    Course.objects.create(course_code="ZSG490", description="Co-op", credit_hours=6)
    _plan_row("ZSG", "ZSG101", 1, 4, "Passed but uncounted")
    _plan_row("ZSG", "ZSG201", 1, 3, "Registered now")
    _plan_row("ZSG", "ZSG490", 2, 6, "Co-op")
    # plan total 13, co-op 6 -> the gate is satisfiable only by passing everything else.
    Prerequisite.objects.create(
        program="ZSG", course_code="ZSG490", prerequisite_course_code="7(HOURS)"
    )
    StudentCourse.objects.create(student=student, course=undercounted, status="passed")
    section = TermSection.objects.create(
        course_code="ZSG201",
        course_number="ZSG201",
        course_key="ZSG201",
        course_name="Registered now",
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
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    # 4 passed + 3 registered = the 7 the gate asks for; the registrar aggregate
    # alone would have offered 3 and left the co-op permanently unreachable.
    assert report["unresolved_requirements"] == []
    assert report["simulation_completed"] is True
    assert report["estimated_additional_terms"] == 1
    assert [term["course_codes"] for term in report["term_plan"]] == [["ZSG490"]]
