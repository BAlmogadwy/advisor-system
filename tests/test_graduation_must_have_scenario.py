from __future__ import annotations

import pytest

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.student_graduation import (
    OPTIMIZED_CURRENT_OFFERINGS_KIND,
    RECOMMENDED_CURRENT_TERM,
    REGISTERED_TIMETABLE,
    build_graduation_must_have_scenario,
    build_graduation_what_if,
)

pytestmark = pytest.mark.django_db

STUDENT_ID = 4_501_909
YEAR = 1448
TERM = 1
PROGRAM = "MWI"


def _add_requirement(
    code: str,
    name: str,
    programme_term: int,
    *,
    credits: int = 3,
    requirement_type: str = "Mandatory",
    prerequisites: tuple[str, ...] = (),
    global_name: str | None = None,
) -> None:
    Course.objects.create(
        course_code=code,
        description=global_name or name,
        credit_hours=credits,
    )
    ProgrammeRequirement.objects.create(
        program=PROGRAM,
        course_code=code,
        course_name=name,
        programme_term=programme_term,
        credit_hours=credits,
        type=requirement_type,
    )
    if prerequisites:
        Prerequisite.objects.create(
            program=PROGRAM,
            course_code=code,
            prerequisite_course_code=",".join(prerequisites),
        )


@pytest.fixture
def plan() -> Student:
    student = Student.objects.create(
        student_id=STUDENT_ID,
        registration_no=str(STUDENT_ID),
        name="Must-have scenario student",
        program=PROGRAM,
        section="M",
        status="active",
        total_earned_credits=3,
        current_registered_credits=0,
    )
    _add_requirement("MW100", "Completed foundation", 1)
    _add_requirement(
        "MW200",
        "Direct prerequisite",
        7,
        prerequisites=("MW100",),
    )
    _add_requirement(
        "MW300",
        "Programme-authoritative target",
        8,
        prerequisites=("MW200",),
        global_name="Wrong global catalogue title",
    )
    for index in range(1, 7):
        _add_requirement(f"MW1{index:02d}", f"Baseline priority {index}", 7)
    StudentCourse.objects.create(
        student=student,
        course=Course.objects.get(course_code="MW100"),
        status=StudentCourse.Status.PASSED,
    )
    return student


def _course_row(code: str) -> dict:
    row = ProgrammeRequirement.objects.get(program=PROGRAM, course_code=code)
    return {
        "code": code,
        "name": row.course_name,
        "credits": int(row.credit_hours or 0),
        "section": "",
        "source": "baseline_fixture",
    }


def _baseline(kind: str) -> dict:
    courses = [_course_row(f"MW1{index:02d}") for index in range(1, 7)]
    return {
        "program": PROGRAM,
        "planning_baseline_kind": kind,
        "planning_baseline_academic_year": YEAR,
        "planning_baseline_term": TERM,
        "planning_baseline_credits": 18,
        "registered_credits_at_planning_baseline": 18,
        "registered_credits_now": 18,
        "planning_baseline_courses_assumed_passed": courses,
        "current_courses_assumed_passed": courses,
        "simulation_completed": True,
        "estimated_additional_terms": 2,
        "lower_bound_additional_terms": 2,
        "term_plan": [],
        "unresolved_requirements": [],
    }


def test_explicit_what_if_keeps_strict_default_and_opt_in_auto_adds_one_direct_level(plan):
    strict = build_graduation_what_if(
        STUDENT_ID,
        YEAR,
        TERM,
        add_current_courses=["MW300"],
    )
    assert strict["what_if"]["valid"] is False
    assert {
        (row["kind"], row.get("course_code")) for row in strict["what_if"]["validation_errors"]
    } >= {("ADDED_COURSE_PREREQUISITES_UNMET", "MW300")}

    relaxed = build_graduation_what_if(
        STUDENT_ID,
        YEAR,
        TERM,
        add_current_courses=["MW300"],
        allow_same_term_direct_prerequisites=True,
    )
    what_if = relaxed["what_if"]
    assert what_if["valid"] is True
    assert what_if["same_term_direct_prerequisite_approval"] is True
    assert [row["code"] for row in what_if["auto_added_prerequisites"]] == ["MW200"]
    assert what_if["same_term_direct_prerequisite_edges"] == [
        {
            "course_code": "MW300",
            "prerequisite_code": "MW200",
            "exception": "same_term_direct_prerequisite",
        }
    ]
    target = next(row for row in what_if["added_current_courses"] if row["code"] == "MW300")
    assert target["name"] == "Programme-authoritative target"


def test_recommended_must_have_displaces_only_trailing_unprotected_courses(plan):
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=_baseline(RECOMMENDED_CURRENT_TERM),
        must_have_courses=["MW300"],
        allow_same_term_direct_prerequisites=True,
    )

    what_if = report["what_if"]
    assert what_if["valid"] is True
    assert what_if["same_term_direct_prerequisite_approval"] is True
    assert [row["code"] for row in what_if["displaced_baseline_courses"]] == [
        "MW105",
        "MW106",
    ]
    assert [row["code"] for row in report["planning_baseline_courses_assumed_passed"]] == [
        "MW101",
        "MW102",
        "MW103",
        "MW104",
        "MW300",
        "MW200",
    ]
    assert report["planning_baseline_credits"] == 18
    target = next(
        row for row in report["planning_baseline_courses_assumed_passed"] if row["code"] == "MW300"
    )
    assert target["scenario_role"] == "must_have"
    assert target["source"] == "admin_override"
    prerequisite = next(
        row for row in report["planning_baseline_courses_assumed_passed"] if row["code"] == "MW200"
    )
    assert prerequisite["scenario_role"] == "auto_prerequisite"
    assert prerequisite["source"] == "same_term_direct_prerequisite"
    assert report["planning_baseline_provenance"]["baseline_reoptimized"] is False


def test_registered_must_have_never_displaces_real_registration(plan):
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=_baseline(REGISTERED_TIMETABLE),
        must_have_courses=["MW300"],
        allow_same_term_direct_prerequisites=True,
    )

    what_if = report["what_if"]
    assert what_if["valid"] is False
    assert what_if["displaced_baseline_courses"] == []
    assert {row["kind"] for row in what_if["validation_errors"]} == {"SCENARIO_EXCEEDS_CREDIT_CAP"}
    assert [row["code"] for row in report["planning_baseline_courses_assumed_passed"]] == [
        f"MW1{index:02d}" for index in range(1, 7)
    ]


def test_requested_relaxation_without_an_actual_edge_is_not_called_approved(plan):
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=_baseline(RECOMMENDED_CURRENT_TERM),
        must_have_courses=["MW200"],
        allow_same_term_direct_prerequisites=True,
    )
    assert report["what_if"]["valid"] is True
    assert report["what_if"]["allow_same_term_direct_prerequisites"] is True
    assert report["what_if"]["same_term_direct_prerequisite_edges"] == []
    assert report["what_if"]["same_term_direct_prerequisite_approval"] is False


def test_same_term_exception_is_not_recursive(plan):
    _add_requirement("MW400", "Recursive target", 8, prerequisites=("MW300",))
    recursive = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report={
            **_baseline(RECOMMENDED_CURRENT_TERM),
            "planning_baseline_courses_assumed_passed": [],
        },
        must_have_courses=["MW400"],
        allow_same_term_direct_prerequisites=True,
    )
    assert recursive["what_if"]["valid"] is False
    strict_peer = next(
        row
        for row in recursive["what_if"]["validation_errors"]
        if row["kind"] == "SAME_TERM_PREREQUISITE_NOT_STRICTLY_ELIGIBLE"
    )
    assert strict_peer["course_code"] == "MW300"
    assert strict_peer["missing_prerequisites"] == ["MW200"]


@pytest.mark.parametrize("allow_same_term", [False, True])
def test_must_have_hour_gates_remain_earned_only(plan, allow_same_term):
    _add_requirement("MW500", "Hour-gated target", 8, prerequisites=("12(HOURS)",))
    hour_gated = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=_baseline(RECOMMENDED_CURRENT_TERM),
        must_have_courses=["MW500"],
        allow_same_term_direct_prerequisites=allow_same_term,
    )
    hour_error = next(
        row
        for row in hour_gated["what_if"]["validation_errors"]
        if row["kind"] == "ADDED_COURSE_CREDIT_GATE_UNMET"
    )
    assert hour_error == {
        "kind": "ADDED_COURSE_CREDIT_GATE_UNMET",
        "course_code": "MW500",
        "required": 12,
        "effective": 3,
        "remaining": 9,
    }


def test_graduation_project_pair_can_never_use_same_term_exception(plan):
    _add_requirement("MW491", "GRADUATION PROJECT I", 9, credits=2)
    _add_requirement(
        "MW492",
        "GRADUATION PROJECT II",
        10,
        prerequisites=("MW491",),
    )
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report={
            **_baseline(RECOMMENDED_CURRENT_TERM),
            "planning_baseline_courses_assumed_passed": [],
        },
        must_have_courses=["MW492"],
        allow_same_term_direct_prerequisites=True,
    )

    assert report["what_if"]["valid"] is False
    assert {row["kind"] for row in report["what_if"]["validation_errors"]} >= {
        "GRADUATION_PROJECT_SEQUENCE_REQUIRES_NEXT_TERM"
    }
    assert report["what_if"]["same_term_direct_prerequisite_edges"] == []


def test_optimized_baseline_is_supported_but_manual_result_is_not_called_reoptimized(plan):
    baseline = {
        **_baseline(OPTIMIZED_CURRENT_OFFERINGS_KIND),
        "planning_baseline_provenance": {"source": "recorded_current_section_snapshot"},
        "offering_optimization": {"optimization_complete": True, "selected_credits": 18},
    }
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=baseline,
        must_have_courses=["MW300"],
        allow_same_term_direct_prerequisites=True,
    )

    assert report["what_if"]["valid"] is True
    assert report["planning_baseline_kind"] == OPTIMIZED_CURRENT_OFFERINGS_KIND
    assert report["planning_baseline"]["kind"] == OPTIMIZED_CURRENT_OFFERINGS_KIND
    assert report["planning_baseline_provenance"] == {
        "source": "admin_must_have_override",
        "base_planning_baseline_kind": OPTIMIZED_CURRENT_OFFERINGS_KIND,
        "manual_constraint_applied": True,
        "baseline_reoptimized": False,
        "registered_courses_displaced": False,
        "displaced_baseline_course_codes": ["MW105", "MW106"],
        "base_provenance": {"source": "recorded_current_section_snapshot"},
    }
    assert report["offering_optimization"]["optimization_complete"] is False
    assert report["offering_optimization"]["reoptimized"] is False
    assert report["offering_optimization"]["base_optimization"] == {
        "optimization_complete": True,
        "selected_credits": 18,
    }


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        ("NOTINPLAN", "MUST_HAVE_COURSE_NOT_IN_PLAN"),
        ("MWEL1", "MUST_HAVE_ELECTIVE_PLACEHOLDER"),
        ("MW100", "ALREADY_PASSED"),
    ],
)
def test_must_have_rejects_unknown_placeholder_and_passed_targets(plan, code, expected_kind):
    if code == "MWEL1":
        _add_requirement(
            code,
            "PROGRAM ELECTIVE I",
            7,
            requirement_type="Program Elective",
        )
    report = build_graduation_must_have_scenario(
        STUDENT_ID,
        YEAR,
        TERM,
        baseline_report=_baseline(RECOMMENDED_CURRENT_TERM),
        must_have_courses=[code],
    )
    assert report["what_if"]["valid"] is False
    assert {row["kind"] for row in report["what_if"]["validation_errors"]} == {expected_kind}
