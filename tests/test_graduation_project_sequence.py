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
from core.services.student_graduation import (
    RECOMMENDED_CURRENT_TERM,
    REGISTERED_TIMETABLE,
    _graduation_project_stage,
    _graduation_project_successors,
    build_graduation_report,
)

pytestmark = pytest.mark.django_db

YEAR = 1448
TERM = 1


def _student(student_id: int, program: str) -> Student:
    return Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name=f"{program} project sequence student",
        program=program,
        section="M",
        status="active",
    )


def _requirement(
    program: str,
    code: str,
    name: str,
    programme_term: int,
    credits: int,
) -> None:
    Course.objects.create(
        course_code=code,
        description=name,
        credit_hours=credits,
    )
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        type="Mandatory",
        programme_term=programme_term,
        credit_hours=credits,
    )


def _register(student_id: int, code: str, name: str) -> None:
    section = TermSection.objects.create(
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=name,
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def _project_plan_with_full_second_term(
    *,
    student_id: int,
    program: str,
    registered_project_one: bool,
) -> set[str]:
    _student(student_id, program)
    _requirement(program, "GP491", "GRADUATION PROJECT I", 9, 2)
    _requirement(program, "GP492", "GRADUATION PROJECT II", 10, 3)
    Prerequisite.objects.create(
        program=program,
        course_code="GP492",
        prerequisite_course_code="GP491",
    )
    fillers = {f"AA10{number}" for number in range(1, 7)}
    for code in sorted(fillers):
        _requirement(program, code, f"Second-term filler {code}", 10, 3)
    if registered_project_one:
        _register(student_id, "GP491", "GRADUATION PROJECT I")
    return fillers


@pytest.mark.parametrize(
    ("baseline_kind", "registered_project_one"),
    [
        (REGISTERED_TIMETABLE, True),
        (RECOMMENDED_CURRENT_TERM, False),
    ],
)
def test_project_two_displaces_lower_priority_work_in_the_immediately_following_term(
    baseline_kind: str,
    registered_project_one: bool,
) -> None:
    student_id = 4_401_777
    fillers = _project_plan_with_full_second_term(
        student_id=student_id,
        program="GP",
        registered_project_one=registered_project_one,
    )

    report = build_graduation_report(
        student_id,
        YEAR,
        TERM,
        planning_baseline_kind=baseline_kind,
    )

    baseline_codes = {row["code"] for row in report["planning_baseline_courses_assumed_passed"]}
    assert baseline_codes == {"GP491"}
    immediately_following = report["term_plan"][0]
    assert (immediately_following["academic_year"], immediately_following["term"]) == (
        1448,
        2,
    )
    assert "GP492" in immediately_following["course_codes"]
    assert immediately_following["credits"] == 18
    assert len(fillers & set(immediately_following["course_codes"])) == 5
    assert report["project_sequence_pairs"] == [{"project_1": "GP491", "project_2": "GP492"}]
    assert report["project_sequence_blockers"] == []


def test_optimized_starting_course_gets_project_two_next_even_when_normal_parity_would_wait():
    """The optimizer supplies an out-of-parity starting term through this override."""
    student_id = 4_501_778  # target programme term 7 at 1448/1
    program = "GPO"
    _student(student_id, program)
    _requirement(program, "GPO398", "GRADUATION PROJECT I", 8, 2)
    _requirement(program, "GPO499", "GRADUATION PROJECT II", 9, 3)
    Prerequisite.objects.create(
        program=program,
        course_code="GPO499",
        prerequisite_course_code="GPO398",
    )

    report = build_graduation_report(
        student_id,
        YEAR,
        TERM,
        planning_baseline_kind=RECOMMENDED_CURRENT_TERM,
        _current_courses_override=[
            {
                "code": "GPO398",
                "name": "GRADUATION PROJECT I",
                "credits": 2,
                "section": "",
            }
        ],
    )

    immediately_following = report["term_plan"][0]
    assert (immediately_following["academic_year"], immediately_following["term"]) == (
        1448,
        2,
    )
    assert immediately_following["course_codes"] == ["GPO499"]


def test_project_sequence_never_exceeds_the_credit_cap_or_publishes_a_later_project_two():
    student_id = 4_401_779
    program = "GPC"
    _student(student_id, program)
    _requirement(program, "GPC491", "GRADUATION PROJECT I", 9, 2)
    _requirement(program, "GPC492", "GRADUATION PROJECT II", 10, 3)
    Prerequisite.objects.create(
        program=program,
        course_code="GPC492",
        prerequisite_course_code="GPC491",
    )

    report = build_graduation_report(
        student_id,
        YEAR,
        TERM,
        planning_baseline_kind=RECOMMENDED_CURRENT_TERM,
        max_credits_per_term=2,
    )

    assert report["planning_baseline_courses_assumed_passed"][0]["code"] == "GPC491"
    assert report["term_plan"] == []
    assert report["simulation_completed"] is False
    assert report["project_sequence_blockers"] == [
        {
            "project_1": "GPC491",
            "project_2": "GPC492",
            "required_academic_year": 1448,
            "required_term": 2,
            "required_immediately_after": True,
            "reason": "CREDIT_CAP",
            "course_credits": 3,
            "maximum_credits": 2,
        }
    ]
    project_two = next(row for row in report["unresolved_requirements"] if row["code"] == "GPC492")
    assert project_two["project_sequence_gate"]["reason"] == "CREDIT_CAP"


def test_historical_project_one_pass_does_not_claim_it_was_the_previous_term():
    student_id = 4_501_780  # target programme term 7 at 1448/1
    program = "GPH"
    student = _student(student_id, program)
    _requirement(program, "GPH398", "GRADUATION PROJECT I", 8, 2)
    _requirement(program, "GPH499", "GRADUATION PROJECT II", 9, 3)
    Prerequisite.objects.create(
        program=program,
        course_code="GPH499",
        prerequisite_course_code="GPH398",
    )
    StudentCourse.objects.create(
        student=student,
        course=Course.objects.get(course_code="GPH398"),
        status="passed",
    )

    report = build_graduation_report(
        student_id,
        YEAR,
        TERM,
        planning_baseline_kind=REGISTERED_TIMETABLE,
    )

    assert report["planning_baseline_courses_assumed_passed"] == []
    assert report["term_plan"][0]["waiting_term"] is True
    assert report["term_plan"][1]["course_codes"] == ["GPH499"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("GRADUATION PROJECT I", 1),
        ("GRADUATION  PROJECT II", 2),
        ("GRADUATION I", 1),
        ("PROJECT II", 2),
        ("388\t GRADUATION PROJECT I", 1),
        ("INFORMATION SYSTEMS PROJECT MANAGEMENT", None),
        ("COOPERATIVE TRAINING", None),
    ],
)
def test_project_stage_uses_anchored_plan_titles(title: str, expected: int | None):
    assert _graduation_project_stage(title) == expected


def test_project_pair_requires_a_direct_edge_and_consecutive_curriculum_terms():
    plan_rows = {
        "PM301": {"name": "INFORMATION SYSTEMS PROJECT MANAGEMENT", "term": 7},
        "CYB388": {"name": "388\t GRADUATION PROJECT I", "term": 8},
        "CYB479": {"name": "GRADUATION PROJECT II", "term": 9},
    }
    assert _graduation_project_successors(
        plan_rows,
        {"CYB479": ["CYB388"]},
    ) == {"CYB388": "CYB479"}
    assert _graduation_project_successors(plan_rows, {"CYB479": []}) == {}

    plan_rows["CYB479"]["term"] = 10
    assert (
        _graduation_project_successors(
            plan_rows,
            {"CYB479": ["CYB388"]},
        )
        == {}
    )
