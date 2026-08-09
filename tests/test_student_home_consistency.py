"""Cross-card invariants for the student home screen.

The timetable, progress summary, registered-hours card, and recommendations are
different presentations of one configured-term state.  These tests intentionally
exercise them together so a future local fix cannot make the screen contradict
itself again.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services import student_otp
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

# The recommender derives study level from the first two digits.  A 46xxxxxx id
# at 1448/1 makes term-1 requirements overdue and therefore valid candidates.
SID = 4660202
PROGRAM = "HOMECHECK"
CURRENT = "HC101"
NEXT = "HC102"
OTHER_COHORT = "HC103"


def _make_section(*, code: str, section: str, day: str, start: str, end: str) -> None:
    term_section = TermSection.objects.create(
        course_code=code[:2],
        course_number=code[2:],
        course_key=code,
        course_name=f"{code} course",
        section=section,
    )
    TermSectionMeeting.objects.create(
        term_section=term_section,
        day=day,
        start_time=start,
        end_time=end,
        room="B-101",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=term_section,
    )


@pytest.fixture
def home_student(monkeypatch):
    ensure_role_groups()
    Student.objects.create(
        student_id=SID,
        name="Home Consistency",
        program=PROGRAM,
        section="M",
        gpa=3.5,
        total_earned_credits=30,
    )
    for code, credits in ((CURRENT, 3), (NEXT, 4), (OTHER_COHORT, 5)):
        Course.objects.create(
            course_code=code,
            description=f"{code} course",
            credit_hours=credits,
        )
        ProgrammeRequirement.objects.create(
            program=PROGRAM,
            course_code=code,
            course_name=f"{code} course",
            type="Mandatory",
            programme_term=1,
            credit_hours=credits,
        )

    # Keep the deliberately bad opposite-cohort mapping out of the recommendation
    # list.  The row still has five credits, so it detects an unfiltered hours sum.
    Prerequisite.objects.create(
        program=PROGRAM,
        course_code=OTHER_COHORT,
        prerequisite_course_code="HC999",
    )
    _make_section(code=CURRENT, section="M1", day="SUN", start="09:00", end="10:15")
    _make_section(
        code=OTHER_COHORT,
        section="F9",
        day="MON",
        start="11:00",
        end="12:15",
    )

    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {"academic_year": 1448, "term": 1},
    )
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    return client


def _response(client: Client, language: str = "en"):
    response = client.get(
        reverse("student_home"),
        headers={"accept-language": language},
        SERVER_NAME="testserver",
    )
    assert response.status_code == 200
    return response


def _timetable_codes(response) -> set[str]:
    return {
        meeting["course_code"]
        for day in response.context["timetable"]
        for meeting in day["meetings"]
    }


def test_configured_term_is_one_state_for_timetable_progress_hours_and_recommendations(
    home_student,
):
    response = _response(home_student)

    assert _timetable_codes(response) == {CURRENT}
    assert response.context["home_cards"]["registered_hours"]["value"] == 3
    assert response.context["home_cards"]["registered_hours"]["course_count"] == 1
    assert response.context["home_cards"]["plan_state"]["studying"] == 1
    assert [row["code"] for row in response.context["recommendations"]] == [NEXT]


def test_home_renders_shared_timetable_data_and_a_semantic_exact_table(home_student):
    body = _response(home_student).content.decode()

    assert 'id="studentHomeTimetable"' in body
    assert 'id="studentHomeTimetableData"' in body
    assert re.search(r'<table[^>]+class="[^"]*student-timetable-table', body)
    assert "<caption" in body
    assert "js/page-student-home.js" in body
    # The exact row and the progressive-enhancement JSON use the same filtered
    # source. CSS/JS choose a presentation; they do not query a second timetable.
    assert body.count(CURRENT) >= 2
    assert OTHER_COHORT not in body


def test_arabic_timetable_host_and_exact_table_have_explicit_rtl(home_student):
    body = _response(home_student, language="ar").content.decode()

    assert re.search(r'id="studentHomeTimetable"[^>]*\bdir="rtl"', body)
    assert re.search(r'<table class="student-timetable-table" dir="rtl">', body)


def test_expected_plan_is_labelled_and_never_counted_as_registered(
    home_student,
    monkeypatch,
):
    StudentTermSection.objects.update(source="registration_plan_1448_t1")
    monkeypatch.setattr(
        "core.student_auth_views.recommend_next_courses",
        lambda *_args, **_kwargs: [CURRENT],
    )

    response = _response(home_student)
    body = response.content.decode()

    assert response.context["timetable_is_expected"] is True
    assert response.context["home_cards"]["registered_hours"]["known"] is False
    assert response.context["recommendations_already_current"] == []
    assert CURRENT in response.context["recommendations_already_expected"]
    assert "My expected timetable" in body
    assert "not an actual registration" in body
    assert all(meeting["source"] == "planned" for meeting in response.context["timetable_meetings"])


def test_home_states_the_read_only_boundary_and_links_recommendations(home_student):
    body = _response(home_student).content.decode()
    copy = re.sub(r"\s+", " ", body).lower()

    assert "read-only" in copy
    assert re.search(r"(?:does not|doesn't|neither) save?s?", copy)
    assert re.search(r"(?:does not|doesn't|nor) register?s?", copy)
    assert re.search(r"(?:main university portal|university(?:'s)? main portal)", copy)

    detail_url = reverse("student_course_detail_page", args=[NEXT])
    assert re.search(
        rf'<a[^>]+href="{re.escape(detail_url)}"[^>]*>.*?{NEXT}.*?</a>',
        body,
        flags=re.DOTALL,
    )
