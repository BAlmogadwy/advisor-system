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
    build_graduation_must_have_scenario,
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


def test_the_scenario_seed_counts_only_what_was_actually_passed():
    """The seed must be the PASSED plan credits, never the whole plan's total.

    Dropping the wrong half of that comprehension -- summing every row in
    `plan_rows` instead of the passed subset -- hands each student the entire
    plan at term zero and satisfies every `N(HOURS)` gate immediately, including
    the zero-slack co-op gate this whole change exists to reach honestly. The
    slip is invisible unless a fixture leaves a LARGE unpassed remainder, so this
    student has passed 3 credits of a 105-credit plan, and the gate is set beyond
    the plan's reach so the scenario's own credit total stays readable.
    """

    student_id = 4_401_994
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Large unpassed plan remainder",
        program="REM",
        section="M",
        status="active",
        total_earned_credits=3,
        current_registered_credits=0,
    )
    passed = Course.objects.create(course_code="REM101", description="Only pass", credit_hours=3)
    _plan_row("REM", "REM101", 1, 3, "Only pass")
    Course.objects.create(course_code="REM490", description="Capstone", credit_hours=3)
    _plan_row("REM", "REM490", 10, 3, "Capstone")
    # 33 filler courses of 3 credits: the plan totals 105, of which 3 are passed.
    for index in range(33):
        code = f"REM2{index:02d}"
        Course.objects.create(course_code=code, description=code, credit_hours=3)
        _plan_row("REM", code, 2, 3, code)
    Prerequisite.objects.create(
        program="REM", course_code="REM490", prerequisite_course_code="300(HOURS)"
    )
    StudentCourse.objects.create(student=student, course=passed, status="passed")

    report = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    capstone = next(row for row in report["unresolved_requirements"] if row["code"] == "REM490")
    # 3 passed credits, NOT the plan's 105. Summing the whole plan would report 105
    # here, clear the gate at term zero and leave nothing unresolved at all.
    # 3 credits passed + the 99 the scenario schedules = 102. Seeding from the
    # WHOLE plan instead of the passed subset reports 204 here, and on any plan
    # whose gate is actually reachable it clears every hour gate at term zero.
    assert capstone["credit_hour_gate"] == {
        "required": 300,
        "effective_in_scenario": 102,
        "remaining": 198,
    }


def test_credits_earned_outside_the_plan_survive_the_reconciliation():
    """The registrar side of `max` -- the transfer / programme-change case.

    Outside-plan credits exist ONLY in the registrar aggregate; the plan-derived
    sum cannot see them by construction. Taking the plan sum alone, rather than
    the larger of the two, would silently confiscate them.
    """

    student_id = 4_401_995
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Transfer credits outside the plan",
        program="OUT",
        section="M",
        status="active",
        # 3 credits from OUT101 plus 90 transferred credits for courses that are
        # not part of this plan at all.
        total_earned_credits=93,
        current_registered_credits=0,
    )
    in_plan = Course.objects.create(course_code="OUT101", description="In plan", credit_hours=3)
    outside = Course.objects.create(
        course_code="XFER200", description="Transferred, not in plan", credit_hours=3
    )
    _plan_row("OUT", "OUT101", 1, 3, "In plan")
    Course.objects.create(course_code="OUT490", description="Capstone", credit_hours=3)
    _plan_row("OUT", "OUT490", 10, 3, "Capstone")
    Prerequisite.objects.create(
        program="OUT", course_code="OUT490", prerequisite_course_code="90(HOURS)"
    )
    StudentCourse.objects.create(student=student, course=in_plan, status="passed")
    StudentCourse.objects.create(student=student, course=outside, status="passed")

    report = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    # The registrar's 93 wins over the plan-derived 3, so the gate is already met
    # and the capstone is reachable.
    assert report["unresolved_requirements"] == []
    assert report["simulation_completed"] is True
    assert [term["course_codes"] for term in report["term_plan"]] == [["OUT490"]]


def _zero_slack_student(student_id: int, program: str) -> None:
    """A registrar total that omits a passed plan course, against a zero-slack gate.

    Both plan courses are PASSED, so the strict hour gate -- which deliberately
    withholds merely-registered credits -- has no other reason to refuse. The
    registrar/plan seam is then the only variable left.
    """

    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Registrar seam at a zero-slack gate",
        program=program,
        section="M",
        status="active",
        # The registrar counts 201 but not 101: 3 where the plan says 7.
        total_earned_credits=3,
        current_registered_credits=0,
    )
    undercounted = Course.objects.create(
        course_code=f"{program}101", description="Passed but uncounted", credit_hours=4
    )
    counted = Course.objects.create(
        course_code=f"{program}201", description="Passed and counted", credit_hours=3
    )
    Course.objects.create(course_code=f"{program}490", description="Co-op", credit_hours=6)
    _plan_row(program, f"{program}101", 1, 4, "Passed but uncounted")
    _plan_row(program, f"{program}201", 1, 3, "Passed and counted")
    _plan_row(program, f"{program}490", 2, 6, "Co-op")
    Prerequisite.objects.create(
        program=program, course_code=f"{program}490", prerequisite_course_code="7(HOURS)"
    )
    StudentCourse.objects.create(student=student, course=undercounted, status="passed")
    StudentCourse.objects.create(student=student, course=counted, status="passed")


def test_the_must_have_validator_agrees_with_the_forecast_about_the_same_gate():
    """One screen must not say "you graduate" beside "you cannot register".

    The forecast reconciles the registrar aggregate with plan-denominated credits;
    the must-have validator read the registrar aggregate raw. On a zero-slack co-op
    gate that produced a live contradiction on the adviser portfolio screen: the
    forecast placed the co-op and reported graduation while the validator refused
    the very same course for want of the very same credits.

    The must-have scenario is a projection -- "if these were placed" -- not a
    registration decision, so it takes the projection's basis. It remains strict
    about merely-REGISTERED credits; that is a different rule and is unchanged.
    """

    student_id = 4_401_996
    _zero_slack_student(student_id, "MHV")

    baseline = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )
    assert baseline["earned_credits_registrar"] == 3
    assert baseline["passed_credits_in_plan"] == 7
    assert baseline["simulation_completed"] is True

    scenario = build_graduation_must_have_scenario(
        student_id,
        1448,
        1,
        baseline_report=baseline,
        must_have_courses=["MHV490"],
    )

    what_if = scenario["what_if"]
    gate_errors = [
        error
        for error in what_if.get("validation_errors") or []
        if error.get("kind") == "ADDED_COURSE_CREDIT_GATE_UNMET"
    ]
    # On the registrar aggregate alone this reported required 7 / effective 3.
    assert gate_errors == [], f"validator contradicts the forecast: {gate_errors}"
    assert what_if["valid"] is True


def test_the_report_says_which_credit_total_the_scenario_believed():
    """Two sources of truth, and on a zero-slack gate the choice IS the answer.

    Both inputs were already published; nothing said which one the forecast
    consumed. Switching authority silently is how the original defect stayed
    invisible.
    """

    student_id = 4_401_999
    _zero_slack_student(student_id, "BAS")

    report = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    assert report["scenario_credit_seed_registrar"] == 3
    assert report["scenario_credit_seed_plan"] == 7
    assert report["scenario_credit_seed"] == 7
    assert report["scenario_credit_seed_basis"] == "plan"


def test_an_ordinary_student_stays_on_the_registrar_basis():
    """The reconciliation must announce itself only when the sources disagree."""

    student_id = 4_402_000
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Sources agree",
        program="AGR",
        section="M",
        status="active",
        total_earned_credits=3,
        current_registered_credits=0,
    )
    passed = Course.objects.create(course_code="AGR101", description="Passed", credit_hours=3)
    _plan_row("AGR", "AGR101", 1, 3, "Passed")
    Course.objects.create(course_code="AGR201", description="Next", credit_hours=3)
    _plan_row("AGR", "AGR201", 2, 3, "Next")
    StudentCourse.objects.create(student=student, course=passed, status="passed")

    report = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    assert report["scenario_credit_seed_registrar"] == 3
    assert report["scenario_credit_seed_plan"] == 3
    assert report["scenario_credit_seed_basis"] == "registrar"


def test_a_seam_and_outside_plan_credit_together_are_knowingly_under_counted():
    """`max` is a heuristic, not an identity, and this is the case that shows it.

    With a registrar seam S and outside-plan credits O the student truly holds
    ``R_in + S + O``; taking the larger of the two totals returns ``R_in + max(O, S)``
    and so loses ``min(O, S)``. The registrar aggregate is one number, so R_in and
    O cannot be separated out of it — the exact value is not available at this
    layer, and guessing a decomposition would inflate credits. Under-counting is
    the right failure direction for a gate, and this test states the cost plainly
    rather than leaving it to be rediscovered.
    """

    student_id = 4_402_001
    student = Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Seam and transfer together",
        program="BTA",
        section="M",
        status="active",
        # R_in = 0 for the seam course, O = 10 transferred -> registrar shows 10.
        total_earned_credits=10,
        current_registered_credits=0,
    )
    seam = Course.objects.create(course_code="BTA101", description="Uncounted", credit_hours=8)
    outside = Course.objects.create(course_code="XFR900", description="Transfer", credit_hours=10)
    _plan_row("BTA", "BTA101", 1, 8, "Uncounted")
    Course.objects.create(course_code="BTA490", description="Capstone", credit_hours=3)
    _plan_row("BTA", "BTA490", 10, 3, "Capstone")
    Prerequisite.objects.create(
        program="BTA", course_code="BTA490", prerequisite_course_code="18(HOURS)"
    )
    StudentCourse.objects.create(student=student, course=seam, status="passed")
    StudentCourse.objects.create(student=student, course=outside, status="passed")

    report = build_graduation_report(
        student_id, 1448, 1, planning_baseline_kind=REGISTERED_TIMETABLE
    )

    # Truly held: 8 (plan) + 10 (transfer) = 18, which would clear the gate.
    # Returned: max(10, 8) = 10, losing min(O=10, S=8) = 8.
    assert report["scenario_credit_seed_registrar"] == 10
    assert report["scenario_credit_seed_plan"] == 8
    assert report["scenario_credit_seed"] == 10
    capstone = next(row for row in report["unresolved_requirements"] if row["code"] == "BTA490")
    assert capstone["credit_hour_gate"] == {
        "required": 18,
        "effective_in_scenario": 10,
        "remaining": 8,
    }
