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
    """31 of 38 slots are unmapped, so this IS the screen for most students."""
    d = _detail("CE1")
    assert d["options"] == []
    assert d["mapping_ready"] is False
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
    assert d["mapping_ready"] is True
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


def test_a_course_page_keeps_the_student_navigation(client_as_student):
    """A student subpage must not fall back to the staff sidebar when its view
    forgets the shared navigation context."""
    body = _page(client_as_student, "CB201").content.decode()
    assert reverse("student_home") in body
    assert reverse("student_advisor") in body
    assert "/virtual-advisor/" not in body


def test_an_unready_slot_shows_one_sentence_and_no_option_cards(client_as_student):
    """31 of 38 slots are unmapped, so this IS the screen for most students.

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
    assert "mapping_ready" in template


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
    assert d["mapping_ready"] is False
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
    assert d["mapping_ready"] is False
    assert d["options"] == []
    assert "CZ401" not in _page(client_as_student, "CE1").content.decode()


def test_every_non_ready_state_tells_the_student_exactly_the_same_thing(client_as_student):
    """The cause is the registrar's problem, not the student's situation.

    This was a dict, and `INVALID_MAPPING` said «غير مكتملة في النظام» — which
    tells a student that somebody misconfigured the system. Whether nobody
    published the mapping or somebody published it wrongly is not theirs to read,
    and the difference is preserved where it belongs: the operational report.
    """
    from core.services.elective_readiness import (
        INVALID_MAPPING,
        MAPPED_BUT_EMPTY,
        RESERVED_STATUSES,
        student_message,
    )

    messages = {student_message(s) for s in (NOT_PUBLISHED, INVALID_MAPPING, MAPPED_BUT_EMPTY)}
    assert len(messages) == 1, f"a non-ready state has its own wording: {messages}"
    assert student_message(READY) == "", "a ready slot renders options, not a sentence"
    assert MAPPED_BUT_EMPTY in RESERVED_STATUSES, "the reserved state must be marked as such"
    assert NOT_PUBLISHED not in RESERVED_STATUSES


def test_the_rendered_page_never_names_the_cause_or_an_internal_token(client_as_student):
    """Neither the state names nor the `kind` values may reach the page."""

    elective = ElectiveCourse.objects.create(
        course_code="CW401", course_name="Foreign", credit_hours=3, programme="SOMEWHERE"
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    assert _detail("CE1")["mapping_ready"] is False

    for code in ("CE1", "CB201", "ZZ999"):
        body = _page(client_as_student, code).content.decode()
        for token in (
            "INVALID_MAPPING",
            "NOT_PUBLISHED",
            "MAPPED_BUT_EMPTY",
            "NOT_IN_PLAN",
            "ELECTIVE_SLOT",
            "MISSING_COURSE",
            "MISSING_HOURS",
            "UNKNOWN_PREREQ",
            "ASK_ADVISOR",
            "cross-programme",
        ):
            assert token not in body, f"{code}: internal token {token} reached the page"


# ── review of PR #57: four defects, six regressions ──────────────


#: Every way a screen might tell a student they are ALLOWED to register. The first
#: version of this check held one Arabic phrase and missed «تستطيع تسجيله الآن»,
#: which was sitting in the prerequisite badge the whole time — a permission claim
#: wearing the clothes of a status label.
PERMISSION_WORDINGS = (
    "يمكنك تسجيل",
    "تستطيع تسجيل",
    "تستطيع تسجيله",
    "بإمكانك تسجيل",
    "مؤهل للتسجيل",
    "مسموح لك بتسجيل",
    "يحق لك تسجيل",
    "eligible",
    "you may register",
    "you can register",
    "may enrol",
    "can enroll",
)


def test_no_registration_permission_wording_in_any_payload(plan):
    """Neither an answer nor the vocabularies behind it may say a student MAY register."""
    from core.services.course_detail import PREREQ_STATUS_AR, REASON_AR, STATUS_AR

    _passed("CA101")
    bodies = [json.dumps(_detail(c), ensure_ascii=False) for c in ("CA101", "CB201", "CCAP", "CE1")]
    bodies += [json.dumps(v, ensure_ascii=False) for v in (STATUS_AR, PREREQ_STATUS_AR, REASON_AR)]
    for body in bodies:
        for wording in PERMISSION_WORDINGS:
            assert wording not in body, f"a permission claim reached the payload: {wording}"


def test_no_registration_permission_wording_on_any_rendered_page(client_as_student):
    """Both BEFORE and AFTER the prerequisite is passed.

    The first version passed CA101 up front, so no prerequisite was ever in the
    `open` state — which is the only state whose badge carried the permission
    wording. It caught the defect in the payload and missed it on the page.
    """
    codes = ("CA101", "CB201", "CCAP", "CE1", "ZZ999")

    def check(stage):
        for code in codes:
            body = _page(client_as_student, code).content.decode()
            for wording in PERMISSION_WORDINGS:
                assert wording not in body, f"{stage} {code}: permission claim: {wording}"

    # CA101 has no prerequisites, so it is `open` — and CB201 lists it.
    assert _detail("CB201")["prerequisites"][0]["student_status"] == "open"
    check("before passing:")

    _passed("CA101")
    check("after passing:")


def test_a_satisfied_prerequisite_describes_itself_not_what_you_may_do(plan):
    from core.services.course_detail import PREREQ_STATUS_AR

    assert PREREQ_STATUS_AR["open"] == "متطلباته السابقة مستوفاة"
    _passed("CA101")
    prereqs = {p["course_code"]: p for p in _detail("CB201")["prerequisites"]}
    assert prereqs["CA101"]["student_status_ar"] == "مجتاز"


def test_the_student_json_never_names_the_mapping_fault(client_as_student):
    """The HTML hid it; the JSON endpoint returned it, one view-source away.

    `student_message` says the same sentence for every non-ready state precisely so
    a student cannot tell missing data from a misconfigured mapping. Shipping the
    state name beside that sentence hands them the distinction anyway.
    """
    elective = ElectiveCourse.objects.create(
        course_code="CV401", course_name="Foreign", credit_hours=3, programme="SOMEWHERE"
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    response = _get(client_as_student, "CE1")
    body = response.content.decode()
    payload = response.json()

    assert payload["mapping_ready"] is False
    assert "mapping_status" not in payload, "the operational state name is in the payload"
    for state in ("INVALID_MAPPING", "NOT_PUBLISHED", "MAPPED_BUT_EMPTY", "READY"):
        assert state not in body, f"the JSON response names an operational state: {state}"
    assert "cross-programme" not in body and "SOMEWHERE" not in body


def test_the_operational_report_still_carries_the_detailed_state(plan):
    """The distinction is not deleted — it moves to where an operator reads it."""
    from core.services.elective_readiness import INVALID_MAPPING, readiness, slot_status

    elective = ElectiveCourse.objects.create(
        course_code="CU401", course_name="Foreign", credit_hours=3, programme="SOMEWHERE"
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    status, options, problems = slot_status(PROG, "CE1", YEAR, TERM)
    assert status == INVALID_MAPPING and options == [] and problems

    row = next(r for r in readiness(YEAR, TERM) if r["programme"] == PROG and r["slot"] == "CE1")
    assert row["status"] == INVALID_MAPPING
    assert any("cross-programme" in p for p in row["problems"])


def test_the_html_page_consumes_the_read_budget(client_as_student):
    """It calls the same 118-158-query report as the JSON route, and did so free."""
    from core.models import RateLimitBucket
    from core.services.rate_limit import HISTORY

    _page(client_as_student, "CB201")
    assert RateLimitBucket.objects.get(key=f"{HISTORY}:{SID}").count == 1
    _page(client_as_student, "CB201")
    assert RateLimitBucket.objects.get(key=f"{HISTORY}:{SID}").count == 2


def test_the_html_page_eventually_refuses_and_does_so_in_arabic_html(client_as_student):
    from core.services.rate_limit import HISTORY, LIMITS

    limit = LIMITS[HISTORY][0]
    response = None
    for _ in range(limit + 1):
        response = _page(client_as_student, "CB201")
        if response.status_code == 429:
            break
    else:
        raise AssertionError(f"{limit + 1} page loads were allowed against a limit of {limit}")

    body = response.content.decode()
    assert "<html" in body.lower(), "an HTML route answered a throttle with something else"
    assert "لقد فتحت صفحات كثيرة" in body
    assert response["Retry-After"]


def test_an_unlocked_course_carries_its_name(plan):
    """`_course_names` ran BEFORE `unlocks` was computed, so every downstream course
    rendered with an empty name — and the test asserted only the codes, so it passed."""
    unlocks = _detail("CA101")["unlocks"]
    assert [u["course_code"] for u in unlocks] == ["CB201"]
    assert unlocks[0]["course_name"] == "Beta", "the unlocked course has no name"


def test_an_unlocked_course_shows_its_name_on_the_page(client_as_student):
    body = _page(client_as_student, "CA101").content.decode()
    assert "CB201" in body and "Beta" in body


# ── re-review of PR #57: three more ──────────────────────────────


def _limiter_retry_after():
    """What the limiter itself would say, asked directly."""
    from core.services.rate_limit import HISTORY, LIMITS

    return LIMITS[HISTORY][1]


def test_the_page_throttle_reports_the_limiters_own_wait(client_as_student):
    """Not a number this layer invented.

    It hard-coded `Retry-After: 60` while the `HISTORY` window is 600 seconds — so
    a student was told to come back in a minute while the limiter went on refusing
    them for ten.
    """
    from core.advisor_http import over_budget
    from core.services.rate_limit import HISTORY, LIMITS

    limit = LIMITS[HISTORY][0]
    response = None
    for _ in range(limit + 1):
        response = _page(client_as_student, "CB201")
        if response.status_code == 429:
            break
    assert response.status_code == 429

    # The limiter, asked again right now, must agree with what the page said.
    limiter = over_budget(HISTORY, SID)
    assert limiter is not None, "the limiter is no longer refusing"
    assert response["Retry-After"] == limiter["Retry-After"]
    assert int(response["Retry-After"]) > 60, "the fabricated 60 is back"
    assert response["Retry-After"] in response.content.decode(), "the wait is not shown"


def test_the_planner_form_refuses_in_arabic_html_not_json(client_as_student):
    """A browser form POST. Returning the limiter's JsonResponse put a JSON
    document in the window — the same defect as the GET route, other door."""
    from core.advisor_http import over_budget
    from core.models import PlannerDraft
    from core.services.rate_limit import CONVERSATION, LIMITS

    _passed("CA101")
    url = reverse("student_course_to_planner", args=["CB201"])
    limit = LIMITS[CONVERSATION][0]

    response = None
    for _ in range(limit + 2):
        response = client_as_student.post(url)
        if response.status_code == 429:
            break
    else:
        raise AssertionError(f"{limit + 2} posts were allowed against a limit of {limit}")

    assert response["Content-Type"].startswith("text/html")
    body = response.content.decode()
    assert "<html" in body.lower()
    # The ACTION was refused, not a page view. See
    # `test_the_throttle_describes_what_was_actually_refused`.
    assert "لقد أرسلت طلبات كثيرة" in body
    assert "لقد فتحت صفحات كثيرة" not in body

    limiter = over_budget(CONVERSATION, SID)
    assert limiter is not None
    assert response["Retry-After"] == limiter["Retry-After"]

    before = PlannerDraft.objects.filter(student_id=SID).count()
    client_as_student.post(url)
    assert PlannerDraft.objects.filter(student_id=SID).count() == before, (
        "a throttled post still created a draft"
    )


# ── the report is built LAST, and only where it is read ──────────


def _report_calls(monkeypatch):
    """Count `build_unlock_report` calls through the name the service resolves."""
    import core.services.student_unlock as su

    calls: list[int] = []
    real = su.build_unlock_report

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(su, "build_unlock_report", counting)
    return calls


def test_a_code_outside_the_plan_does_not_build_the_report(plan, monkeypatch):
    """118-158 queries to say "that is not in your plan"."""
    calls = _report_calls(monkeypatch)
    assert _detail("ZZ999")["kind"] == KIND_NOT_IN_PLAN
    assert calls == [], "the report was built for a branch that never reads it"


def test_an_elective_slot_does_not_build_the_report(plan, monkeypatch):
    """And this is the COMMON path: 31 of 38 slots are unmapped, so the most
    frequent elective answer was also the most expensive way to say nothing."""
    calls = _report_calls(monkeypatch)
    assert _detail("CE1")["kind"] == KIND_ELECTIVE_SLOT
    assert calls == []


def test_a_ready_elective_slot_does_not_build_the_report_either(plan, monkeypatch):
    elective = ElectiveCourse.objects.create(
        course_code="CT401", course_name="Opt", credit_hours=3, programme=PROG
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    calls = _report_calls(monkeypatch)
    d = _detail("CE1")
    assert d["mapping_ready"] is True and d["options"]
    assert calls == []


def test_a_real_course_builds_the_report_exactly_once(plan, monkeypatch):
    calls = _report_calls(monkeypatch)
    assert _detail("CB201")["kind"] == KIND_COURSE
    assert calls == [1], f"the report ran {len(calls)} times"


def test_a_supplied_report_is_reused_rather_than_rebuilt(plan, monkeypatch):
    """The journey locked-list to "why?" to here must not pay twice."""
    from core.services.student_unlock import build_unlock_report

    prebuilt = build_unlock_report(SID, int(YEAR), int(TERM))
    calls = _report_calls(monkeypatch)
    d = _detail("CB201", report=prebuilt)
    assert d["kind"] == KIND_COURSE
    assert d["your_status"] == "blocked", "the supplied report was not actually used"
    assert calls == []


def test_the_cheap_branches_really_are_cheap(plan):
    """Measured, not asserted by mocking alone — the mock proves the call is gone,
    this proves the cost went with it."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    costs = {}
    for code in ("ZZ999", "CE1", "CB201"):
        with CaptureQueriesContext(connection) as ctx:
            _detail(code)
        costs[code] = len(ctx)

    assert costs["ZZ999"] < 10, costs
    assert costs["CE1"] < 15, costs
    assert costs["CB201"] > costs["CE1"] * 2, costs


def test_the_throttle_describes_what_was_actually_refused(client_as_student):
    """One sentence covered two different refusals, and fitted only one.

    A student who pressed "add to planner" was told they had opened too many
    PAGES, under the heading "cannot show the course" — an accurate refusal about
    something they had not done.
    """
    from core.course_detail_views import (
        THROTTLED_ACTION_AR,
        THROTTLED_ACTION_HEADING_AR,
        THROTTLED_PAGE_AR,
        THROTTLED_PAGE_HEADING_AR,
    )
    from core.services.rate_limit import CONVERSATION, HISTORY, LIMITS

    assert THROTTLED_PAGE_AR != THROTTLED_ACTION_AR
    assert THROTTLED_PAGE_HEADING_AR != THROTTLED_ACTION_HEADING_AR

    # Reading pages: refused as a page.
    page = None
    for _ in range(LIMITS[HISTORY][0] + 1):
        page = _page(client_as_student, "CB201")
        if page.status_code == 429:
            break
    assert page.status_code == 429
    page_body = page.content.decode()
    assert THROTTLED_PAGE_AR in page_body and THROTTLED_PAGE_HEADING_AR in page_body
    assert THROTTLED_ACTION_AR not in page_body

    # Pressing the button: refused as an action.
    _passed("CA101")
    url = reverse("student_course_to_planner", args=["CB201"])
    post = None
    for _ in range(LIMITS[CONVERSATION][0] + 2):
        post = client_as_student.post(url)
        if post.status_code == 429:
            break
    assert post.status_code == 429
    post_body = post.content.decode()
    assert THROTTLED_ACTION_AR in post_body and THROTTLED_ACTION_HEADING_AR in post_body
    assert THROTTLED_PAGE_AR not in post_body
    assert THROTTLED_PAGE_HEADING_AR not in post_body


# -- the declared type decides, AT THE SURFACE A STUDENT READS ----
#
# Four call-site mutants survived the entire 2024-test suite: the predicate was
# tested as a function and never through the branch that calls it, so the exact
# regression the narrowing fixed could be reintroduced here and every test stayed
# green. These are those tests.


@pytest.fixture
def declared_electives(plan):
    """`FE1`/`GSE1` -- declared electives that students TAKE.

    111 have passed FE1 and 139 GSE1 in the live database. They are 2-hour courses
    sitting with the university-requirement block (`GS101` is also 2h), unlike
    every Program Elective, which is 3h.
    """
    for code, name, type_ in (
        ("GSE1", "University Elective I", "University Elective"),
        ("FE1", "Free Elective I", "Free Elective"),
    ):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": name, "credit_hours": 2}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={
                "programme_term": 5,
                "credit_hours": 2,
                "type": type_,
                "course_name": name,
            },
        )
    yield


def test_a_passed_declared_elective_says_so_instead_of_withholding_options(declared_electives):
    """364 completed enrolments across 186 students were answered "not published yet".

    While `Free Elective` and `University Elective` counted as placeholders, this
    branch returned before any status lookup -- so a student who had PASSED `GSE1`
    was told the options for it had not been published.
    """
    from core.services.elective_readiness import NOT_READY_AR

    for code in ("GSE1", "FE1"):
        _passed(code)
        d = _detail(code)
        assert d["kind"] == KIND_COURSE, f"{code}: a course the student passed, answered as a slot"
        assert d["your_status"] == "passed", f"{code}: {d.get('your_status')!r}"
        assert d["status_ar"] == "اجتزتَ هذا المقرر."
        assert NOT_READY_AR not in (d.get("message_ar") or "")
        assert "options" not in d, f"{code}: offered options for a course, not a slot"


def test_an_untaken_declared_elective_is_a_course_with_a_real_status(declared_electives):
    """Not only the passed case -- otherwise the fix could be "look up a grade
    first" rather than "it was never a placeholder"."""
    d = _detail("FE1")
    assert d["kind"] == KIND_COURSE
    assert d["your_status"] in {"open_now", "blocked", "unknown"}
    assert d["status_ar"]
    assert "mapping_ready" not in d and "options" not in d
    assert d["credit_hours"] == 2, "the 2-hour declared elective lost its own credit value"


def test_a_program_elective_is_still_a_slot(declared_electives):
    """The positive control. Without it the two tests above pass with the predicate
    stubbed to `False`, which would be a different bug of the same size."""
    d = _detail("CE1")
    assert d["kind"] == KIND_ELECTIVE_SLOT
    assert "options" in d


# -- a placeholder the student has already settled -----------------


def test_a_passed_program_elective_reports_the_pass_not_the_gate(plan):
    """26 students have `DS1` passed, and `DS1` is declared `Program Elective`.

    Narrowing the placeholder set did not fix this: the ordering defect that
    produced it -- read the type, return, never look at the student -- survived for
    the set that stayed. A code can be a placeholder AND completed.
    """
    from core.services.elective_readiness import NOT_READY_AR

    _passed("CE1")
    d = _detail("CE1")
    assert d["your_status"] == "passed", "the slot branch still cannot say 'you did this'"
    assert d["status_ar"] == "اجتزتَ هذا المقرر."
    assert d["message_ar"] != NOT_READY_AR, "told to wait for options they no longer need"
    assert d["message_ar"] == ""
    assert d["options"] == [], "offered a choice to a student who has already made it"


def test_a_settled_slot_does_not_offer_options_even_when_they_are_published(plan):
    """The harmful ordering is the other way round too: READY must not override the
    student's own record and re-open a decision they have made."""
    elective = ElectiveCourse.objects.create(
        course_code="CT401", course_name="Opt", credit_hours=3, programme=PROG
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    assert _detail("CE1")["options"], "the slot is not actually READY; the test proves nothing"

    _passed("CE1")
    d = _detail("CE1")
    assert d["your_status"] == "passed"
    assert d["options"] == []
    assert d["mapping_ready"] is False


def test_a_slot_being_studied_is_not_an_open_question_either(plan):
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="CE1"),
        defaults={"status": "studying", "programme_term": 7},
    )
    d = _detail("CE1")
    assert d["your_status"] == "studying"
    assert d["status_ar"] == ("تدرس هذا المقرر حاليًا، ويلزم اجتيازه.")
    assert d["options"] == []


def test_an_unsettled_slot_still_reaches_the_gate(plan):
    """`not_taken` is not a settlement. Without this the fix could be "any row at
    all means done", and 209 students hold a `not_taken` FE1 row."""
    from core.services.elective_readiness import NOT_READY_AR

    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="CE1"),
        defaults={"status": "not_taken", "programme_term": 7},
    )
    d = _detail("CE1")
    assert d.get("your_status") is None
    assert d["message_ar"] == NOT_READY_AR
    assert d["mapping_ready"] is False


def test_the_settled_slot_is_the_cheapest_answer_of_all(plan):
    """It skips `slot_status` as well as the report -- the student with the least
    left to decide must not pay the most to be told so."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as unsettled:
        _detail("CE1")
    _passed("CE1")
    with CaptureQueriesContext(connection) as settled:
        _detail("CE1")
    assert len(settled) <= len(unsettled), (len(settled), len(unsettled))
    assert len(settled) < 10, len(settled)


# -- what the OPTIONS list shows, and what it must not claim --------


def test_a_published_option_shows_the_gate_it_carries(client_as_student):
    """`options[].prerequisites` shipped in the payload and the list dropped it.

    AI463 requires CS372 and 67 of 117 AI students have not passed it; DS487
    requires IS252 and 24 of 88 DS students have not. Code, name and hours alone
    turn a conditional option into an unconditional offer -- the display-completeness
    failure in its stronger form: not an empty value, a dropped one.
    """
    elective = ElectiveCourse.objects.create(
        course_code="CT401",
        course_name="Retrieval",
        credit_hours=3,
        programme=PROG,
        prerequisites_csv="CB201",
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    assert _detail("CE1")["options"][0]["prerequisites"] == [{"course_code": "CB201"}], (
        "the payload no longer carries the gate; this test is about rendering it"
    )

    body = _page(client_as_student, "CE1").content.decode()
    assert "CT401" in body and "Retrieval" in body
    assert "CB201" in body, "the option's prerequisite is not shown"


def test_the_options_list_makes_no_claim_about_this_student(client_as_student):
    """The gate is a fact about the OPTION. This list is not personalised, and a
    prerequisite the student happens to hold must not turn into permission."""
    elective = ElectiveCourse.objects.create(
        course_code="CT402",
        course_name="Vision",
        credit_hours=3,
        programme=PROG,
        prerequisites_csv="CB201",
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    _passed("CB201")
    body = _page(client_as_student, "CE1").content.decode()
    for permission in ("تستطيع تسجيل", "يمكنك تسجيل", "مؤهل", "eligible"):
        assert permission not in body, permission


def test_an_option_without_prerequisites_shows_no_empty_gate(client_as_student):
    elective = ElectiveCourse.objects.create(
        course_code="CT403", course_name="Free", credit_hours=3, programme=PROG
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="CE1",
        elective_id=elective.id,
        academic_year=YEAR,
        term=TERM,
    )
    url = reverse("student_course_detail_page", args=["CE1"])
    for language, label in (("ar", "يتطلب"), ("en", "requires")):
        body = client_as_student.get(url, headers={"accept-language": language}).content.decode()
        assert "CT403" in body, language
        # In the language actually rendered. Asserting the Arabic label against a
        # page that renders `en` by default is an assertion about nothing — the
        # first version of this test passed with the condition forced to `True`.
        assert label not in body, f"{language}: an empty gate rendered as a bare label"


# ── the status comparison is normalised, not lucky ───────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("passed", "passed"),
        ("PASSED", "passed"),
        (" passed ", "passed"),
        ("Passed", "passed"),
        ("studying", "studying"),
        ("Studying", "studying"),
        ("  STUDYING", "studying"),
    ],
)
def test_a_settled_slot_is_recognised_whatever_case_the_status_was_written_in(
    plan, stored, expected
):
    """`StudentCourse.status` is a bare TextField — no `choices`, default `''`.

    Lowercase is a convention of whoever writes the rows, not a constraint of the
    schema. All 16,434 live rows are canonical today, which is precisely the kind
    of fact that stops being true after one import by someone who did not know it
    was load-bearing.

    The failure mode is silent and it favours the wrong answer: an unmatched
    `"PASSED"` drops the student back into the not-published branch and tells them
    the options for a requirement they have completed are not ready yet.
    """
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="CE1"),
        defaults={"status": stored, "programme_term": 7},
    )
    d = _detail("CE1")
    assert d["your_status"] == expected, f"{stored!r} was not read as {expected}"
    assert d["options"] == []


@pytest.mark.parametrize("stored", ["not_taken", "NOT_TAKEN", " not_taken ", "", "  ", "withdrawn"])
def test_normalising_does_not_turn_an_unsettled_status_into_a_settlement(plan, stored):
    """The other direction, so the fix cannot become "any row means done".

    `withdrawn` is deliberately in the list: a status the registrar may invent
    later must fall through to the gate, not be guessed at.
    """
    from core.services.elective_readiness import NOT_READY_AR

    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="CE1"),
        defaults={"status": stored, "programme_term": 7},
    )
    d = _detail("CE1")
    assert d.get("your_status") is None, f"{stored!r} was read as a settlement"
    assert d["message_ar"] == NOT_READY_AR
