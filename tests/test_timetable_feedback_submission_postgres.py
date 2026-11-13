"""PostgreSQL only: two submissions from one student racing to open the term's case.

The race is forced and proven with the helper the schema's race tests use: both
connections open first, and the first submission holds its transaction until
PostgreSQL shows the second blocked by it. The rate-limit budgets are switched
off here so the two writers meet only at the case itself; spending them is
tested elsewhere. SQLite runs one writer at a time, so these skip there and are
required in the ``planner (PostgreSQL 16)`` job.
"""

from __future__ import annotations

import os

import pytest
from django.db import connection
from django.utils import timezone

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableFeedback,
    TimetableFeedbackPoint,
)
from core.services import timetable_feedback as feedback
from core.services.student_term_timetable import read_student_term_timetable
from tests.test_timetable_feedback_postgres import _race

pytestmark = pytest.mark.django_db(transaction=True)

SID = 4991101
PROG = "FBK"
YEAR = "1448"
TERM = "1"


@pytest.fixture(autouse=True)
def _postgresql_only():
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL feedback-submission race tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row locks and concurrent unique-index checks.")


@pytest.fixture(autouse=True)
def _accepting(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    settings.TIMETABLE_FEEDBACK_ACCEPTING = True
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = ()
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))
    monkeypatch.setattr(feedback, "_spend", lambda *_args: None)


@pytest.fixture
def anchor():
    """One student whose expected timetable holds one section."""
    Student.objects.create(student_id=SID, name="Racer", program=PROG, section="M")
    ProgrammeRequirement.objects.create(
        program=PROG, course_code="SA101", course_name="INTRO", type="Mandatory", credit_hours=3
    )
    section = TermSection.objects.create(
        source_tag="other",
        course_name="INTRO",
        course_code="SA",
        course_number="101",
        course_key="SA101",
        section="M1",
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="SUN", start_time="09:00", end_time="10:15", room="A1"
    )
    TermSectionProgram.objects.create(term_section=section, program=PROG)
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="registration_plan_1448_t1",
    )
    return f"SECTION|{section.pk}"


def _fingerprint():
    timetable = read_student_term_timetable(SID, YEAR, TERM)
    return feedback.forecast_fingerprint(
        academic_year=YEAR,
        term=TERM,
        forecast_class=timetable.forecast_class,
        sections=feedback.seen_sections(timetable, programme=PROG),
    )


def _fields_for(anchor, reason, note=""):
    return {
        "student_id": SID,
        "anchor": anchor,
        "reason": reason,
        "note": note,
        "seen_fingerprint": _fingerprint(),
    }


def _submitting(anchor, reason):
    fields = _fields_for(anchor, reason)
    return lambda: feedback.submit_point(**fields)


def test_two_first_points_racing_open_one_case_and_both_land_in_it(anchor):
    first, second = _race(_submitting(anchor, "TIME_CLASH"), _submitting(anchor, "TIME_UNSUITABLE"))

    assert first.created_case is True
    assert second.created_case is False and second.reference == first.reference
    case = TimetableFeedback.objects.get(student_id=SID)
    assert sorted(case.points.values_list("reason_code", flat=True)) == [
        "TIME_CLASH",
        "TIME_UNSUITABLE",
    ]
    assert case.events.filter(kind="SUBMITTED").count() == 1


def test_the_same_first_point_racing_with_itself_is_written_once(anchor):
    first, second = _race(_submitting(anchor, "TIME_CLASH"), _submitting(anchor, "TIME_CLASH"))

    assert first.created_case is True and first.duplicate is False
    assert second.duplicate is True
    assert (second.reference, second.point_id) == (first.reference, first.point_id)
    assert TimetableFeedbackPoint.objects.count() == 1
    assert TimetableFeedback.objects.count() == 1


def test_two_new_points_racing_into_an_open_case_both_land(anchor):
    """The case is already open. The second submission waits on the case's row
    lock, then claims the version the first left, so neither is refused."""
    feedback.submit_point(**_fields_for(anchor, "OTHER", note="opened the case"))

    first, second = _race(_submitting(anchor, "TIME_CLASH"), _submitting(anchor, "TIME_UNSUITABLE"))

    assert first.created_case is False and second.created_case is False
    case = TimetableFeedback.objects.get(student_id=SID)
    assert sorted(case.points.values_list("reason_code", flat=True)) == [
        "OTHER",
        "TIME_CLASH",
        "TIME_UNSUITABLE",
    ]
    assert case.version == 3


def test_a_withdrawal_racing_a_new_point_still_starts_the_cool_down(anchor):
    """The submission saw an open case before the lock, so it skipped the early
    cap check. The withdrawal commits while it waits on the case's row lock; the
    check under the lock is what refuses a fresh case inside the cool-down."""
    opened = feedback.submit_point(**_fields_for(anchor, "OTHER", note="opened the case"))
    case_pk = TimetableFeedback.objects.get(reference=opened.reference).pk
    fields = _fields_for(anchor, "TIME_CLASH")

    def withdraw():
        return TimetableFeedback.objects.filter(pk=case_pk).update(
            status="WITHDRAWN", closed_at=timezone.now()
        )

    def submit():
        try:
            return feedback.submit_point(**fields)
        except feedback.FeedbackRefused as refused:
            return ("refused", refused.code)

    first, second = _race(withdraw, submit)

    assert first == 1
    assert second == ("refused", "COOLING_DOWN")
    assert list(TimetableFeedback.objects.values_list("status", flat=True)) == ["WITHDRAWN"]
