"""current_term_registrations evidence in the verified student context.

Live bug (student 4404824, 2026-06-11): the chat reported 3 registered
courses because the plan-status ``studying`` set (StudentCourse) cannot
represent retakes — a course passed in an earlier term and re-registered
this term keeps status='passed' there. The Timetable Builder showed the
correct 5 sections because it reads StudentTermSection. The student
context now carries a section-level registration block from the same
source the Timetable Builder uses (get_student_term_baseline).
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.virtual_advisor import build_verified_student_context

pytestmark = pytest.mark.django_db

SID = 4404824


def _course(code: str, name: str, credits: int, plan_term: int = 4) -> Course:
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code=code,
        course_name=name,
        type="Core",
        programme_term=plan_term,
        credit_hours=credits,
    )
    return Course.objects.create(course_code=code, description=name, credit_hours=credits)


def _register(student_id: int, code: str, section: str, year: str = "1447", term: str = "2"):
    ts = TermSection.objects.create(
        course_code=code.rstrip("0123456789"),
        course_number=code[len(code.rstrip("0123456789")) :],
        course_key=code,
        course_name=code,
        section=section,
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year=year,
        term=term,
        term_section=ts,
        source="scraper_timetable",
    )
    return ts


def _make_retake_student() -> Student:
    student = Student.objects.create(
        student_id=SID,
        name="Retake Student",
        program="AI",
        section="M",
        gpa=2.7,
        total_earned_credits=88,
        current_registered_credits=16,
    )
    for code, name, credits, status in [
        ("CS289", "Software Engineering", 4, "passed"),
        ("GS103", "Islamic Studies", 2, "passed"),
        ("CS372", "Database Systems", 4, "studying"),
        ("ENGL214", "Technical Writing", 3, "studying"),
        ("MATH243", "Linear Algebra 1", 3, "studying"),
    ]:
        course = _course(code, name, credits)
        StudentCourse.objects.create(student=student, course=course, status=status)
    for code, section in [
        ("CS289", "M16"),
        ("CS372", "M2"),
        ("ENGL214", "M26"),
        ("GS103", "M5"),
        ("MATH243", "M17"),
    ]:
        _register(SID, code, section)
    return student


def test_retaken_courses_appear_in_current_registrations():
    _make_retake_student()
    context = build_verified_student_context(student_id=SID)

    block = context["course_evidence"]["current_term_registrations"]
    codes = {row["course_code"] for row in block["registrations"]}
    assert codes == {"CS289", "CS372", "ENGL214", "GS103", "MATH243"}
    assert block["registered_course_count"] == 5
    assert block["registered_credit_hours"] == 16
    assert block["academic_year"] == "1447"
    assert block["term"] == "2"
    assert block["source"] == "timetable_sections"

    by_code = {row["course_code"]: row for row in block["registrations"]}
    assert by_code["CS289"]["retake"] is True
    assert by_code["GS103"]["retake"] is True
    assert by_code["CS372"]["retake"] is False
    assert by_code["CS289"]["section"] == "M16"

    # The plan-status list stays as-is (still useful for plan progress).
    assert set(context["course_evidence"]["studying"]) == {"CS372", "ENGL214", "MATH243"}


def test_latest_term_wins_over_older_registrations():
    Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    course = _course("CS101", "Intro", 3)
    StudentCourse.objects.create(
        student=Student.objects.get(student_id=SID), course=course, status="passed"
    )
    _register(SID, "CS101", "M1", year="1446", term="2")
    _register(SID, "CS201", "M3", year="1447", term="1")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert block["academic_year"] == "1447"
    assert block["term"] == "1"
    assert {row["course_code"] for row in block["registrations"]} == {"CS201"}


def test_plan_status_fallback_when_no_timetable_rows():
    student = Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    course = _course("CS372", "Database Systems", 4)
    StudentCourse.objects.create(student=student, course=course, status="studying")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert block["source"] == "plan_status_fallback"
    assert block["academic_year"] is None and block["term"] is None
    assert {row["course_code"] for row in block["registrations"]} == {"CS372"}
    assert block["registrations"][0]["section"] == ""


def test_unmapped_studying_course_unioned_into_registrations():
    student = Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    mapped = _course("CS372", "Database Systems", 4)
    unmapped = _course("ENGL214", "Technical Writing", 3)
    StudentCourse.objects.create(student=student, course=mapped, status="studying")
    StudentCourse.objects.create(student=student, course=unmapped, status="studying")
    _register(SID, "CS372", "M2")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    codes = {row["course_code"] for row in block["registrations"]}
    assert codes == {"CS372", "ENGL214"}
    assert block["registered_credit_hours"] == 7


def test_multi_section_course_counts_credits_once():
    Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    _course("CS372", "Database Systems", 4)
    _register(SID, "CS372", "M2")
    _register(SID, "CS372", "M2L")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert len(block["registrations"]) == 2
    assert block["registered_course_count"] == 1
    assert block["registered_credit_hours"] == 4


def test_recommendation_policy_exposes_real_credit_limit():
    """The model invented a '21-credit standard' and subtracted current-term
    credits from it (live failure, 2026-06-11). The context must carry the
    actual recommender cap so load answers are grounded."""
    _make_retake_student()
    # Eligible for the planning term: odd plan term to match next-term parity
    # for student 44xxxxx asked about 1448/1 (real term 8 → next term 9, odd).
    _course("AI305", "Neural Networks", 3, plan_term=5)

    context = build_verified_student_context(student_id=SID, academic_year=1448, term=1)

    policy = context["recommendation_policy"]
    assert policy["max_term_credit_hours"] == 18
    assert policy["recommended_credit_hours"] <= 18
    recs = context["recommendations"]
    assert {rec["course_code"] for rec in recs} == {"AI305"}
    assert recs[0]["credit_hours"] == 3
    assert policy["recommended_credit_hours"] == 3
    assert policy["credit_hours_unknown_for"] == []
    assert context["term_context"]["role"] == "planning_term_for_recommendations"


def test_recommend_capability_reports_credit_policy():
    from core.services.rbac import ROLE_SUPER_ADMIN
    from core.services.virtual_advisor_capabilities import get_default_registry

    _make_retake_student()
    _course("AI305", "Neural Networks", 3, plan_term=5)

    result = get_default_registry().execute(
        "recommend_courses",
        {"student_id": SID},
        scope={"role": ROLE_SUPER_ADMIN},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert result["ok"] is True
    assert result["credit_policy"]["max_term_credit_hours"] == 18
    assert result["credit_policy"]["recommended_credit_hours"] == 3
    assert result["credit_policy"]["credit_hours_unknown_for"] == []
    assert result["recommendations"][0]["credit_hours"] == 3
