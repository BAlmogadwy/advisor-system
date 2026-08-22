"""Reproduce-first: is every graduation estimate one term too long?

The 2026-08 correctness audit recorded exactly that (then at
student_graduation.py:104-112: "queries the recommender with the cursor term,
not the term planned").  The simulation loop has since been rewritten to
compute the PLANNED term before calling the recommender and documents the
cursor bug as fixed - but the ledger item stayed open, and a stale line
reference is not evidence either way.  These tests ARE the evidence: they
assert the correct totals for the two smallest scenarios with known answers.
If the off-by-one still exists anywhere on the path - the second call site,
the +1 for the baseline term, the rendering of len(term_plan) - one of these
goes red and points at it.
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
from core.services.student_graduation import (
    REGISTERED_TIMETABLE,
    build_graduation_report,
)

pytestmark = pytest.mark.django_db

# The join year is derived from the ID's first two digits (44 -> 1444), so a
# synthetic prefix outside the real cohort range breaks the programme-term
# arithmetic before the simulation even starts.
SID = 4_401_777
YEAR, TERM = 1448, 1


def _course(code: str, *, credits: int = 3) -> Course:
    return Course.objects.create(
        course_code=code,
        description=f"Course {code}",
        credit_hours=credits,
    )


def _requirement(code: str, *, programme_term: int) -> None:
    ProgrammeRequirement.objects.create(
        program="TG",
        course_code=code,
        course_name=f"Course {code}",
        type="Mandatory",
        programme_term=programme_term,
        credit_hours=3,
    )


def _student() -> Student:
    return Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Graduation count student",
        program="TG",
        section="M",
        status="active",
    )


def _register(code: str) -> None:
    section = TermSection.objects.create(
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=f"Course {code}",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def test_a_student_finishing_in_the_baseline_term_needs_zero_additional_terms():
    """Plan of two courses: one passed, one registered THIS term.

    When the baseline term's courses complete the plan, the truth is:
    zero additional terms, one term including the baseline.  The audited
    defect would report one and two.
    """
    student = _student()
    passed = _course("TG101")
    _course("TG102")
    _requirement("TG101", programme_term=1)
    _requirement("TG102", programme_term=2)
    StudentCourse.objects.create(student=student, course=passed, status="passed")
    _register("TG102")

    report = build_graduation_report(SID, YEAR, TERM, planning_baseline_kind=REGISTERED_TIMETABLE)

    assert report["simulation_completed"] is True
    assert report["estimated_additional_terms"] == 0
    assert report["estimated_terms_including_planning_baseline"] == 1


def test_one_remaining_unregistered_course_needs_exactly_one_future_term():
    """Plan of two courses: one passed, one not yet taken, nothing registered.

    The remaining course fits in a single future term, and with an empty
    baseline there is no baseline term to count: one additional term, one
    total.  The audited defect would report two.
    """
    student = _student()
    passed = _course("TG101")
    _course("TG102")
    _requirement("TG101", programme_term=1)
    _requirement("TG102", programme_term=2)
    StudentCourse.objects.create(student=student, course=passed, status="passed")

    report = build_graduation_report(SID, YEAR, TERM, planning_baseline_kind=REGISTERED_TIMETABLE)

    assert report["simulation_completed"] is True
    assert report["estimated_additional_terms"] == 1
    assert report["estimated_terms_including_planning_baseline"] == 1


def test_a_two_course_prerequisite_chain_needs_exactly_two_future_terms():
    """TG103 requires TG102; neither is registered.

    The chain forces two future terms - never three.  This pins the loop's
    cursor arithmetic across MORE than one simulated term, which the
    single-term cases cannot see.
    """
    from core.models import Prerequisite

    student = _student()
    passed = _course("TG101")
    _course("TG102")
    _course("TG103")
    _requirement("TG101", programme_term=1)
    _requirement("TG102", programme_term=2)
    _requirement("TG103", programme_term=3)
    Prerequisite.objects.create(program="TG", course_code="TG103", prerequisite_course_code="TG102")
    StudentCourse.objects.create(student=student, course=passed, status="passed")

    report = build_graduation_report(SID, YEAR, TERM, planning_baseline_kind=REGISTERED_TIMETABLE)

    assert report["simulation_completed"] is True
    assert report["estimated_additional_terms"] == 2
    assert report["estimated_terms_including_planning_baseline"] == 2
