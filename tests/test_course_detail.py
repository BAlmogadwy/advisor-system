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
    CourseDetailUnavailable,
    build_course_detail,
)
from core.services.elective_readiness import NOT_PUBLISHED, READY
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
    assert d["mapping_status"] == NOT_PUBLISHED
    assert d["message_ar"], "an empty list with no sentence explains nothing"


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
    assert d["mapping_status"] == READY
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


# ── the screen ───────────────────────────────────────────────────


def _page(client, code):
    return client.get(reverse("student_course_detail_page", args=[code]))


def test_no_raw_template_syntax_reaches_the_student(client_as_student):
    """Django's `{# … #}` is SINGLE-LINE only; a multi-line one renders as text.

    It has happened on the adviser screen and on the planner screen. It is a test
    now rather than a habit.
    """
    for code in ("CB201", "CE1", "ZZ999"):
        body = _page(client_as_student, code).content.decode()
        for marker in ("{%", "{#", "{{"):
            assert marker not in body, f"{code}: unrendered template syntax {marker}"


def test_a_course_page_shows_status_reasons_and_prerequisites(client_as_student):
    body = _page(client_as_student, "CB201").content.decode()
    assert "CA101" in body
    assert "لم تُستوفَ متطلباته السابقة بعد." in body
    assert "يتطلب اجتياز" in body


def test_an_unready_slot_shows_one_sentence_and_no_option_cards(client_as_student):
    """77 of 84 live slots are unmapped, so this IS the screen for most students.

    No empty option cards, no greyed-out list, no guessed alternatives.
    """
    response = _page(client_as_student, "CE1")
    body = response.content.decode()
    assert "لم تُنشر خيارات هذا المتطلب الاختياري بعد" in body
    assert 'class="cd-options"' not in body, "an option list rendered for an unready slot"
    assert 'class="cd-option"' not in body


def test_a_ready_slot_shows_its_options(client_as_student):
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
    body = _page(client_as_student, "CE1").content.decode()
    assert "CX401" in body
    assert "لم تُنشر خيارات" not in body


def test_the_gate_is_the_backend_answer_not_a_programme_name(client_as_student):
    """A template that knew which programmes were ready would be a second copy of
    the rule, in the layer least able to check itself."""
    from pathlib import Path

    template = Path("core/templates/core/student_course_detail.html").read_text(encoding="utf-8")
    for programme in ("AI", "AI2", "DS", "DS2", "CS", "IS"):
        assert f'"{programme}"' not in template and f"'{programme}'" not in template
    assert "mapping_status" in template


def test_the_planner_button_appears_only_when_prerequisites_are_met(client_as_student):
    """Asserted on the FORM, not its label: the button text is bilingual template
    chrome and renders in whichever language the interface is set to, while the
    service strings are Arabic either way."""
    action = reverse("student_course_to_planner", args=["CB201"])
    blocked = _page(client_as_student, "CB201").content.decode()
    assert action not in blocked, "a blocked course offered a planner action"

    _passed("CA101")
    open_now = _page(client_as_student, "CB201").content.decode()
    assert action in open_now


def test_the_planner_action_is_worded_as_planning_never_permission(client_as_student):
    """The RENDERED page, not the source.

    The first version of this read the template file and tripped over the word
    "eligible" inside a comment explaining why eligibility is not claimed — a
    comment that never reaches the student. What matters is the output.
    """
    _passed("CA101")
    body = _page(client_as_student, "CB201").content.decode()
    assert "does not register you" in body or "لا يسجّلك" in body, "the disclaimer is missing"
    for permission in ("يمكنك تسجيل", "eligible", "you may register", "you can register"):
        assert permission not in body, f"the screen implied permission: {permission}"

    # Both wordings exist, so neither interface is missing the disclaimer.
    from pathlib import Path

    template = Path("core/templates/core/student_course_detail.html").read_text(encoding="utf-8")
    assert "هذا لا يسجّلك في المقرر." in template
    assert "does not register you" in template


def test_the_planner_button_creates_a_draft_and_redirects(client_as_student):
    from core.models import PlannerDraft

    _passed("CA101")
    response = client_as_student.post(reverse("student_course_to_planner", args=["CB201"]))
    assert response.status_code == 302
    draft = PlannerDraft.objects.get(student_id=SID)
    assert draft.course_codes == ["CB201"]
    assert response.url == reverse("student_planner_page", args=[str(draft.id)])


def test_a_course_outside_the_plan_cannot_be_planned(client_as_student):
    """Same validation as the JSON door — one service, two doors."""
    from core.models import PlannerDraft

    response = client_as_student.post(reverse("student_course_to_planner", args=["ZZ999"]))
    assert response.status_code == 409
    assert not PlannerDraft.objects.filter(student_id=SID).exists()


def test_a_refusal_renders_as_a_page_not_a_line_of_json(client_as_student):
    Student.objects.filter(student_id=SID).update(program="")
    response = _page(client_as_student, "CB201")
    assert response.status_code == 409
    body = response.content.decode()
    assert "<html" in body.lower(), "an HTML route answered with something else"
    assert "لا يوجد برنامج دراسي" in body


def test_the_page_refuses_a_non_student(client, plan):
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_user(username="staffer3", password="x"))
    assert _page(client, "CB201").status_code == 403


def test_an_invalid_mapping_withholds_the_options_too(client_as_student):
    """`NOT_PUBLISHED` is not the only unready state.

    A mapping that exists but is wrong — another programme's elective under this
    slot — must withhold the list just as firmly. The student is told the same
    thing either way: naming the difference would expose an internal data fault as
    though it were their situation.
    """
    from core.services.elective_readiness import INVALID_MAPPING

    elective = ElectiveCourse.objects.create(
        course_code="CY401", course_name="Foreign", credit_hours=3, programme="SOMEWHERE"
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    d = _detail("CE1")
    assert d["mapping_status"] == INVALID_MAPPING
    assert d["options"] == [], "an invalid mapping leaked its options"
    assert d["message_ar"]

    body = _page(client_as_student, "CE1").content.decode()
    assert "CY401" not in body
    assert 'class="cd-option"' not in body


def test_a_mapping_for_another_term_does_not_open_the_gate(client_as_student):
    """`ElectiveTermMapping` is term-scoped, and the gate must be too.

    Without the filter a slot mapped for last year reads as ready and the screen
    ships options that are not on offer.
    """
    from core.services.elective_readiness import NOT_PUBLISHED

    elective = ElectiveCourse.objects.create(
        course_code="CZ401", course_name="Last year", credit_hours=3, programme=PROG
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year="1447",
        term="2",
    )
    d = _detail("CE1")
    assert d["mapping_status"] == NOT_PUBLISHED
    assert d["options"] == []
    assert "CZ401" not in _page(client_as_student, "CE1").content.decode()
