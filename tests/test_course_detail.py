"""One course, explained — the three kinds, the four reasons, and what must not leak.

The recon proposed two screens here and they turned out to be one endpoint
branching on what the code names. These tests are written against that branch: a
real course, an elective placeholder, and a code in no plan of this student's — the
third state that `_exec_course_prerequisites`, `why_course_locked` and
`eligibility` currently answer three different ways.
"""

from __future__ import annotations

import json
import re

import pytest
from django.test import Client
from django.urls import reverse

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)
from core.services import student_otp
from core.services.course_detail import (
    KIND_COURSE,
    KIND_ELECTIVE_SLOT,
    KIND_NOT_IN_PLAN,
    OPTIONS_NOT_PUBLISHED,
    OPTIONS_PUBLISHED,
    CourseDetailUnavailable,
    build_course_detail,
)
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

SID = 4940001
PROG = "CDT"
YEAR, TERM = "1448", "1"


@pytest.fixture
def plan():
    """A → B, an hour-gated capstone, an elective slot, and a mandatory GS course."""
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={
            "name": "Detail",
            "program": PROG,
            "section": "M",
            "total_earned_credits": 100,
            "current_registered_credits": 0,
        },
    )
    for code, name, term, type_ in (
        ("CA101", "Alpha", 1, "Mandatory"),
        ("CB201", "Beta", 3, "Mandatory"),
        ("CCAP", "Capstone", 9, "Mandatory"),
        ("CE1", "Programme Elective I", 7, "Program Elective"),
        ("GS104", "Islamic Values", 1, "Mandatory"),
    ):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": name, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={
                "programme_term": term,
                "credit_hours": 3,
                "type": type_,
                "course_name": name,
            },
        )
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="CB201", prerequisite_course_code="CA101"
    )
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="CCAP", prerequisite_course_code="120(HOURS)"
    )
    yield


def _passed(code):
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code=code),
        defaults={"status": "passed", "programme_term": 1},
    )


def _detail(code, **kw):
    return build_course_detail(SID, code, academic_year=YEAR, term=TERM, **kw)


@pytest.fixture
def client_as_student(plan):
    c = Client()
    c.force_login(student_otp.provision_student_user(SID))
    return c


def _get(client, code):
    return client.get(reverse("student_course_detail", args=[code]))


# ── the three kinds ──────────────────────────────────────────────


def test_a_real_course_is_a_course(plan):
    d = _detail("CB201")
    assert d["kind"] == KIND_COURSE
    assert d["course_name"] == "Beta"
    assert d["prerequisites"][0]["course_code"] == "CA101"


def test_an_elective_placeholder_is_a_slot_not_a_course(plan):
    """Answering `prerequisites: []` for a slot reads as "this has none"."""
    d = _detail("CE1")
    assert d["kind"] == KIND_ELECTIVE_SLOT
    assert "prerequisites" not in d


def test_a_code_in_no_plan_of_theirs_is_its_own_kind(plan):
    """The third state. A student clicking a code from a friend's programme."""
    Course.objects.update_or_create(course_code="ZZ999", defaults={"description": "Elsewhere"})
    d = _detail("ZZ999")
    assert d["kind"] == KIND_NOT_IN_PLAN
    assert d["message_ar"]
    assert "your_status" not in d, "a status we cannot know must not be invented"


def test_a_mandatory_course_with_a_slot_shaped_code_is_a_course(plan):
    """Issue #55 at this surface: the declared type decides, not the code."""
    assert _detail("GS104")["kind"] == KIND_COURSE


# ── status and the reason union ──────────────────────────────────


def test_a_passed_course_says_so(plan):
    _passed("CA101")
    d = _detail("CA101")
    assert d["your_status"] == "passed"
    assert d["status_ar"]


def test_a_blocked_course_carries_its_reason_in_arabic(plan):
    d = _detail("CB201")
    assert d["your_status"] == "blocked"
    assert d["reasons"], "a blocked course with no reason explains nothing"
    reason = d["reasons"][0]
    assert reason["kind"] == "MISSING_COURSE"
    assert "CA101" in reason["text_ar"]


def test_an_hour_gate_reason_carries_the_numbers(plan):
    """`MISSING_HOURS` has no course; its entire value is the arithmetic.

    A flat reason shape loses `required`/`effective`/`remaining` and the student is
    told they need more credit hours without being told how many.
    """
    d = _detail("CCAP")
    hours = [r for r in d["reasons"] if r["kind"] == "MISSING_HOURS"]
    assert hours, d["reasons"]
    text = hours[0]["text_ar"]
    assert "120" in text and "100" in text and "20" in text


def test_every_reason_kind_is_translated_never_echoed(plan):
    """Including one that does not exist yet."""
    from core.services.course_detail import REASON_AR, _reason_ar

    for kind in list(REASON_AR) + ["SOME_FUTURE_KIND", ""]:
        text = _reason_ar({"kind": kind, "code": "CA101"}, {"CA101": "Alpha"})
        assert text and " " in text
        if kind:
            assert kind not in text and kind.lower() not in text


def test_a_prerequisite_carries_the_students_own_state(plan):
    """The affordance chat cannot give: three of five marked passed at a glance."""
    _passed("CA101")
    prereq = _detail("CB201")["prerequisites"][0]
    assert prereq["student_status"] == "passed"
    assert prereq["student_status_ar"] == "مجتاز"


def test_what_passing_this_would_open(plan):
    """Already computed by the report and thrown away by every caller."""
    assert [u["course_code"] for u in _detail("CA101")["unlocks"]] == ["CB201"]


# ── the elective empty state, which is the common case ───────────


def test_an_unmapped_slot_says_so_rather_than_returning_a_blank_list(plan):
    """77 of 84 live slots are unmapped, so this IS the screen for most students."""
    d = _detail("CE1")
    assert d["options"] == []
    assert d["options_state"] == OPTIONS_NOT_PUBLISHED
    assert d["options_message_ar"], "an empty list with no sentence explains nothing"


def test_a_mapped_slot_lists_its_options(plan):
    elective = ElectiveCourse.objects.create(
        course_code="CX401", course_name="Extra", credit_hours=3, programme=PROG
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    d = _detail("CE1")
    assert d["options_state"] == OPTIONS_PUBLISHED
    assert [o["course_code"] for o in d["options"]] == ["CX401"]


def test_the_options_list_is_not_capped_by_a_chat_display_limit(plan):
    """`_MAX_COURSE_MATCHES` is for chat readability. Inheriting it here would turn
    a display decision into "you may not take the eleventh option alphabetically"."""
    for i in range(14):
        elective = ElectiveCourse.objects.create(
            course_code=f"CX{400 + i}", course_name=f"Opt {i}", credit_hours=3, programme=PROG
        )
        ElectiveTermMapping.objects.create(
            programme=PROG,
            placeholder_code="CE1",
            elective_id=elective.id,
            academic_year=YEAR,
            term=TERM,
        )
    assert len(_detail("CE1")["options"]) == 14


# ── refusals, and what must never appear ─────────────────────────


def test_a_student_with_no_programme_is_refused_not_guessed_at(plan):
    """The planner's precedent: refuse rather than answer from another plan."""
    Student.objects.filter(student_id=SID).update(program="")
    with pytest.raises(CourseDetailUnavailable):
        _detail("CB201")


def test_the_answer_says_which_term_it_answered_for(plan):
    """Three code paths pick a term three ways and agree only on today's data."""
    d = _detail("CB201")
    assert d["academic_year"] == YEAR and d["term"] == TERM
    assert d["program"] == PROG


def test_nothing_claims_the_student_may_register(plan):
    """`eligible_now` needs the canonical engine (#56). Absence of a blocker is not
    permission, and this surface must never imply otherwise."""
    for code in ("CA101", "CB201", "CCAP", "CE1"):
        body = json.dumps(_detail(code), ensure_ascii=False)
        for forbidden in ("eligible_now", "you_can_take_it_now", "can_register"):
            assert forbidden not in body, f"{code} implied permission via {forbidden}"
        assert "يمكنك تسجيل" not in body


def test_no_english_sentence_reaches_the_student(plan):
    """Course codes and names are data; a SENTENCE of English is a defect."""
    latin = re.compile(r"[A-Za-z]{4,}\s+[A-Za-z]{4,}")
    d = _detail("CCAP")
    for key in ("status_ar",):
        assert not latin.search(d[key]), d[key]
    for reason in d["reasons"]:
        assert not latin.search(reason["text_ar"]), reason


# ── the endpoint ─────────────────────────────────────────────────


def test_the_endpoint_answers_for_the_session_student_only(client_as_student):
    """The URL names a COURSE. There is no student id to tamper with."""
    response = _get(client_as_student, "CB201")
    assert response.status_code == 200
    assert response.json()["course_code"] == "CB201"
    # A payload student id must not change whose plan is read.
    other = client_as_student.get(
        reverse("student_course_detail", args=["CB201"]) + "?student_id=9999999"
    )
    assert other.json()["program"] == PROG


def test_an_anonymous_request_never_reaches_the_view(client, plan):
    assert _get(client, "CB201").status_code in (301, 302)


def test_a_signed_in_non_student_is_refused(client, plan):
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_user(username="staffer2", password="x"))
    assert _get(client, "CB201").status_code == 403


def test_a_student_with_no_programme_gets_a_sentence_not_a_crash(client_as_student):
    Student.objects.filter(student_id=SID).update(program="")
    response = _get(client_as_student, "CB201")
    assert response.status_code == 409
    assert response.json()["error"]


def test_the_endpoint_spends_the_read_budget_not_the_expensive_ones(client_as_student):
    from core.models import RateLimitBucket
    from core.services.rate_limit import GENERATION, HISTORY, PLANNING

    _get(client_as_student, "CB201")
    spent = dict(RateLimitBucket.objects.values_list("key", "count"))
    assert spent.get(f"{HISTORY}:{SID}") == 1
    for expensive in (GENERATION, PLANNING):
        assert spent.get(f"{expensive}:{SID}", 0) == 0
