"""The staff side of timetable feedback through the real URLs, views and templates,
and what each staff action leaves on the student's own pages.

Every wording assertion runs in a language forced by the request, and an Arabic one
first proves the page rendered in Arabic.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape

from core.models import (
    AuditLog,
    Student,
    StudentTermSection,
    TimetableFeedback,
    TimetableFeedbackEvent,
)
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_staff_copy as scopy
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
)
from core.services.student_term_timetable import TimetableUnavailable
from tests.test_timetable_feedback_staff import case_for, point, staff_user
from tests.test_timetable_feedback_student_views import (
    OTHER_SID,
    PROG,
    REG_SID,
    SID,
    _assert_language,
    _client,
    _get,
    _html,
    _send,
    make_world,
    set_flags,
)

pytestmark = pytest.mark.django_db

ADV = "ADV-7"  # make_world's advisor id for every student
QUEUE = "/ops/timetable-feedback/"
GUIDE = "/ops/timetable-feedback/guide/"
BULK = "/ops/timetable-feedback/bulk/"
T = scopy.strings("en")
TA = scopy.strings("ar")
SENTINEL_NOTE = "INTERNAL-SENTINEL-7Q"
SENTINEL_FORWARD = "FORWARD-SENTINEL-8R"
#: The student list's own "something new" mark on a case.
UNREAD_MARK = '<span class="ttf-unread">'


@pytest.fixture(autouse=True)
def _flags(settings, monkeypatch):
    set_flags(settings, monkeypatch)
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14


@pytest.fixture
def world(db):
    return make_world()


@pytest.fixture
def staff(db):
    return {
        "advisor": staff_user("adv-one", ROLE_ADVISOR, advisor_id=ADV),
        "twin": staff_user("adv-one-b", ROLE_ADVISOR, advisor_id=ADV),
        "other": staff_user("adv-two", ROLE_ADVISOR, advisor_id="ADV-9"),
        "no_id": staff_user("adv-none", ROLE_ADVISOR),
        "department": staff_user("dept-one", ROLE_GENERAL_ADVISOR, departments=PROG),
        "department_empty": staff_user("dept-none", ROLE_GENERAL_ADVISOR),
        "admin": staff_user("admin-one", ROLE_SUPER_ADMIN),
        "committee": staff_user("committee-one", ROLE_EXAM_COMMITTEE),
    }


def _as(user, **kwargs):
    client = Client(**kwargs)
    client.force_login(user)
    return client


def _case_url(case, suffix=""):
    return f"/ops/timetable-feedback/{case.reference}/{suffix}"


def _sent_case(world, **changes):
    """A case the student sent through their own form, then left as staff would."""
    student = _client()
    _send(student, world, note="It clashes with my lab.")
    case = TimetableFeedback.objects.get(student_id=SID)
    if changes:
        TimetableFeedback.objects.filter(pk=case.pk).update(**changes)
        case.refresh_from_db()
    return student, case


def _act(client, case, action, language="en", **fields):
    data = {"action": action, "version": case.version, **fields}
    return client.post(_case_url(case, "action/"), data, headers={"accept-language": language})


STAFF_URLS = [QUEUE, GUIDE, "/ops/timetable-feedback/TTF-2026-00001/"]


# ---------------------------------------------------------------------------
# Who gets in
# ---------------------------------------------------------------------------


def test_the_routes_are_where_the_pages_link_to():
    assert reverse("staff_timetable_feedback_queue") == QUEUE
    assert reverse("staff_timetable_feedback_guide") == GUIDE
    assert reverse("staff_timetable_feedback_bulk") == BULK
    assert reverse("staff_timetable_feedback_case", args=["TTF-2026-00012"]) == (
        "/ops/timetable-feedback/TTF-2026-00012/"
    )
    # A reference route takes only a reference, so guide/ and bulk/ never read as one.
    for not_a_reference in ("guide", "bulk", "ttf-2026-00012", "TTF-2026-12"):
        with pytest.raises(NoReverseMatch):
            reverse("staff_timetable_feedback_case", args=[not_a_reference])


@pytest.mark.parametrize("url", STAFF_URLS)
def test_an_anonymous_visitor_is_sent_to_the_staff_login(db, url):
    response = Client().get(url)
    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.parametrize("url", STAFF_URLS)
def test_a_student_is_refused_every_staff_page(world, url):
    assert _client().get(url).status_code == 403


@pytest.mark.parametrize("url", STAFF_URLS)
def test_the_exam_committee_is_refused_every_staff_page(staff, url):
    assert _as(staff["committee"]).get(url).status_code == 403


@pytest.mark.parametrize("url", STAFF_URLS)
@pytest.mark.parametrize("language", ["en", "ar"])
def test_with_the_feature_off_every_staff_page_is_absent(staff, settings, url, language):
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    response = _as(staff["admin"]).get(url, headers={"accept-language": language})
    assert response.status_code == 404
    html = _html(response)
    _assert_language(html, language)
    assert scopy.strings(language)["unavailable_title"] in html
    assert 'href="/ops/timetable-feedback/"' not in html


def test_with_the_feature_off_nothing_is_written(world, staff, settings):
    _student, case = _sent_case(world)
    start = case.version
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    client = _as(staff["advisor"])
    assert _act(client, case, "take").status_code == 404
    assert client.post(BULK, {"bulk": "answer", "pick": [f"{case.reference}@0"]}).status_code == 404
    case.refresh_from_db()
    assert (case.version, case.status) == (start, "SUBMITTED")


@pytest.mark.parametrize("url", [QUEUE, GUIDE])
def test_no_staff_page_is_ever_cached(staff, url):
    response = _as(staff["advisor"]).get(url)
    assert "no-cache" in response["Cache-Control"] and "no-store" in response["Cache-Control"]


def test_a_case_outside_the_scope_reads_exactly_like_one_that_does_not_exist(world, staff):
    _student, case = _sent_case(world)
    start = case.version
    client = _as(staff["other"])
    outside = client.get(_case_url(case))
    missing = client.get("/ops/timetable-feedback/TTF-1999-00001/")
    assert outside.status_code == missing.status_code == 404
    sentence = str(scopy.refusal("CASE_NOT_FOUND", "en"))
    assert sentence in _html(outside) and sentence in _html(missing)
    # Nothing of the case itself: the reference is only the address that was typed.
    assert f"Student {SID}" not in _html(outside) and "my lab" not in _html(outside)
    # Nor can it be acted on.
    assert _act(client, case, "take").status_code == 404
    case.refresh_from_db()
    # No staff event at all, not even an opening.
    assert case.version == start and not case.events.filter(actor__isnull=False).exists()


def test_a_post_without_the_csrf_token_is_refused(world, staff):
    _student, case = _sent_case(world)
    start = case.version
    client = _as(staff["advisor"], enforce_csrf_checks=True)
    assert _act(client, case, "take").status_code == 403
    case.refresh_from_db()
    assert case.version == start


def test_actions_are_posted_never_fetched(world, staff):
    _student, case = _sent_case(world)
    client = _as(staff["advisor"])
    assert client.get(_case_url(case, "action/")).status_code == 405
    assert client.get(BULK).status_code == 405


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def _queue(client, language="en", **params):
    return _html(client.get(QUEUE, params, headers={"accept-language": language}))


def _listed(html):
    return set(re.findall(r'href="/ops/timetable-feedback/(TTF-\d{4}-\d{5})/"', html))


def _case(sid, status="SUBMITTED", **fields):
    return case_for(sid, status, **fields)


def test_an_advisor_sees_their_own_students_cases_and_no_one_elses(world, staff):
    mine = _case(SID)
    Student.objects.filter(student_id=OTHER_SID).update(advisor_id="ADV-9")
    theirs = _case(OTHER_SID)
    html = _queue(_as(staff["advisor"]), tab="all")
    assert _listed(html) == {mine.reference}
    assert _listed(_queue(_as(staff["other"]), tab="all")) == {theirs.reference}
    assert _listed(_queue(_as(staff["department"]), tab="all")) == {
        mine.reference,
        theirs.reference,
    }


def test_the_tabs_count_their_cases_and_mark_the_current_one(world, staff):
    _case(SID)
    _case(SID, "NEEDS_INFORMATION")
    _case(SID, "FORWARDED")
    _case(SID, "CLOSED")
    html = _queue(_as(staff["advisor"]))
    counts = dict(
        re.findall(r'id="ttfsTab-(\w+)">[^<]*<span class="ttfs-count"><bdi dir="ltr">(\d+)', html)
    )
    assert counts == {
        "action": "1",
        "open": "3",
        "student": "1",
        "department": "1",
        "closed": "1",
        "all": "4",
    }
    assert re.search(r'aria-current="page" id="ttfsTab-action"', html)
    assert re.search(
        r'aria-current="page" id="ttfsTab-closed"', _queue(_as(staff["advisor"]), tab="closed")
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"reason": "TIME_CLASH"}, {"clash"}),
        ({"reason": "LOAD_TOO_HIGH"}, {"load"}),
        ({"reason": "MADE_UP"}, {"clash", "load", "registered"}),
        ({"registered": "yes"}, {"registered"}),
        ({"registered": "no"}, {"clash", "load"}),
        ({"new": "1"}, {"clash", "registered"}),
        ({"graduating": "1"}, {"load"}),
        ({"programme": PROG}, {"clash", "load", "registered"}),
        ({"programme": "NONE"}, set()),
        ({"q": str(REG_SID)}, {"registered"}),
        ({"q": "garbage"}, set()),
    ],
)
def test_the_filters_narrow_the_queue(world, staff, params, expected):
    Student.objects.filter(student_id=OTHER_SID).update(status="GRADUATION EXPECTED")
    now = timezone.now()
    cases = {
        "clash": _case(
            SID, last_student_activity_at=now, last_staff_action_at=now - timedelta(hours=1)
        ),
        "load": _case(
            OTHER_SID,
            anchors=("TIMETABLE",),
            last_student_activity_at=now - timedelta(hours=2),
            last_staff_action_at=now,
        ),
        "registered": _case(REG_SID, anchors=("NO_FORECAST",), has_registration_at_submission=True),
    }
    # A point taken back no longer says what the case is about.
    point(cases["load"], "SECTION", 7, withdrawn=True)
    html = _queue(_as(staff["advisor"]), tab="all", **params)
    assert _listed(html) == {cases[name].reference for name in expected}


def test_a_reference_finds_its_case(world, staff):
    case = _case(SID)
    _case(OTHER_SID)
    html = _queue(_as(staff["advisor"]), tab="all", q=case.reference.lower())
    assert _listed(html) == {case.reference}


def test_the_departments_catch_all_is_its_own_filter(world, staff):
    Student.objects.filter(student_id=OTHER_SID).update(advisor_id="سعد الأحمد")
    routed, unrouted = _case(SID), _case(OTHER_SID)
    department = _queue(_as(staff["department"]), tab="all", unrouted="1")
    assert _listed(department) == {unrouted.reference}
    assert department.count("ttfs-unrouted") == 1 and T["marker_unrouted"] in department
    # An advisor is offered no such filter, and sending it filters nothing.
    advisor = _queue(_as(staff["advisor"]), tab="all", unrouted="1")
    assert 'name="unrouted"' not in advisor
    assert _listed(advisor) == {routed.reference}
    # Nor is any of the advisor's own cases marked as without an advisor.
    assert "ttfs-unrouted" not in advisor


def test_a_page_holds_fifty_cases_and_its_links_keep_the_filters(world, staff):
    for _ in range(51):
        _case(SID)
    client = _as(staff["advisor"])
    first = _queue(client, tab="all", reason="TIME_CLASH")
    assert len(_listed(first)) == 50
    assert 'rel="next"' in first and 'rel="prev"' not in first
    link = re.search(r'href="\?([^"]+)" rel="next"', first).group(1).replace("&amp;", "&")
    assert "reason=TIME_CLASH" in link and "tab=all" in link and "page=2" in link
    second = _html(client.get(f"{QUEUE}?{link}"))
    assert len(_listed(second)) == 1 and 'rel="prev"' in second
    assert not _listed(first) & _listed(second)


def test_the_queue_costs_the_same_however_many_cases_it_shows(world, staff):
    client = _as(staff["department"])
    for _ in range(3):
        _case(SID)

    def queries():
        with CaptureQueriesContext(connection) as captured:
            assert client.get(QUEUE, {"tab": "all"}).status_code == 200
        return len(captured)

    queries()  # the first request also sets up the role groups, once
    few = queries()
    for _ in range(12):
        _case(OTHER_SID)
    assert queries() == few


@pytest.mark.parametrize(
    ("who", "key"), [("no_id", "empty_NO_ADVISOR_ID"), ("department_empty", "empty_NO_DEPARTMENTS")]
)
def test_an_empty_scope_says_why_instead_of_showing_an_empty_table(world, staff, who, key):
    html = _queue(_as(staff[who]))
    assert 'id="ttfsScopeEmpty"' in html and escape(T[key]) in html
    assert 'id="ttfsScopeEmpty"' not in _queue(_as(staff["advisor"]))


def test_a_row_says_who_the_student_is_now_and_what_was_raised(world, staff):
    Student.objects.filter(student_id=SID).update(name="Live Name", status="GRADUATION EXPECTED")
    case = _case(
        SID,
        anchors=("SECTION", "TIMETABLE"),
        has_registration_at_submission=True,
        handled_by_username="adv-one",
    )
    gone = _case(4994999, program_at_submission="OLDPROG")
    html = _queue(_as(staff["admin"]), tab="all")
    assert "Live Name" in html
    assert f'<span class="ttf-tag">{T["marker_graduating"]}</span>' in html
    assert f'<span class="ttf-tag">{T["marker_registered"]}</span>' in html
    assert "SA101 M1 — Clashes with another course" in html
    assert 'and <bdi dir="ltr">1</bdi> more' in html
    assert T["student_unavailable"] in html and "OLDPROG" in html
    assert f'value="{case.reference}@0"' in html and f'value="{gone.reference}@0"' in html
    assert html.count(T["nobody"]) == 1


@pytest.mark.parametrize(
    ("student_minutes_ago", "staff_minutes_ago", "marked"),
    [(5, None, True), (5, 10, True), (10, 5, False), (5, 5, False)],
)
def test_a_row_says_when_the_student_wrote_since_staff_last_acted(
    world, staff, student_minutes_ago, staff_minutes_ago, marked
):
    now = timezone.now()
    _case(
        SID,
        last_student_activity_at=now - timedelta(minutes=student_minutes_ago),
        last_staff_action_at=(
            None if staff_minutes_ago is None else now - timedelta(minutes=staff_minutes_ago)
        ),
    )
    html = _queue(_as(staff["advisor"]), tab="all")
    assert html.count('class="ttf-tag ttfs-new"') == (1 if marked else 0)


def test_the_queue_reads_in_arabic(world, staff):
    _case(SID)
    html = _queue(_as(staff["advisor"]), "ar")
    _assert_language(html, "ar")
    for key in ("queue_title", "tab_action", "filter_search", "bulk_answer"):
        assert TA[key] in html, key
    assert scopy.reason_label("TIME_CLASH", "ar") in html


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def _page(client, case, language="en"):
    return _html(client.get(_case_url(case), headers={"accept-language": language}))


def test_the_case_page_shows_the_student_now_and_what_they_sent(world, staff):
    _student, case = _sent_case(world, handled_by_username="adv-one-b", routed_advisor_id="ADV-OLD")
    html = _page(_as(staff["advisor"]), case)
    assert f"Student {SID}" in html and f'<bdi dir="ltr">{PROG}</bdi>' in html
    assert T["cohort_M"] in html and "منتظم" in html
    assert "It clashes with my lab." in html and "Clashes with another course" in html
    assert '<bdi dir="ltr">adv-one-b</bdi>' in html
    assert '<bdi dir="ltr">ADV-OLD</bdi>' in html and f'<bdi dir="ltr">{ADV}</bdi>' in html
    # Two accounts hold the advisor id; both see the case.
    assert re.search(r'id="ttfsVisibleTo"[^>]*>.*?<bdi dir="ltr">2</bdi>', html)
    assert 'id="ttfsUnrouted"' not in html


def test_a_student_with_no_advisor_account_is_said_to_be_the_departments(world, staff):
    Student.objects.filter(student_id=SID).update(advisor_id="")
    _student, case = _sent_case(world)
    html = _page(_as(staff["department"]), case)
    assert 'id="ttfsUnrouted"' in html and 'id="ttfsVisibleTo"' not in html


def test_a_missing_student_record_shows_what_was_frozen(world, staff):
    _student, case = _sent_case(world)
    TimetableFeedback.objects.filter(pk=case.pk).update(
        program_at_submission="FROZENPROG", standing_at_submission="FROZENSTANDING"
    )
    Student.objects.filter(student_id=SID).delete()
    html = _page(_as(staff["admin"]), case)
    assert T["student_unavailable"] in html and T["case_frozen_note"] in html
    assert "FROZENPROG" in html and "FROZENSTANDING" in html


def test_the_record_link_is_offered_only_where_it_opens(world, staff):
    _student, case = _sent_case(world)
    link = reverse("advisor_portfolio_student_graduation_page", args=[SID])
    assert link in _page(_as(staff["advisor"]), case)
    assert link in _page(_as(staff["department"]), case)
    TimetableFeedback.objects.filter(pk=case.pk).update(assigned_advisor_id="ADV-9")
    # The department assigned it there; the record is still not that advisor's.
    other = _page(_as(staff["other"]), case)
    assert case.reference in other and link not in other


def test_the_current_timetable_is_shown_and_a_changed_one_is_marked(world, staff):
    _student, case = _sent_case(world)
    client = _as(staff["advisor"])
    html = _page(client, case)
    assert '<bdi dir="ltr">SA101 M1</bdi>' in html and escape(T["point_changed"]) not in html
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=world["SC201-M1"],
        source="registration_plan_1448_t1",
    )
    html = _page(client, case)
    assert '<bdi dir="ltr">SC201 M1</bdi>' in html and escape(T["point_changed"]) in html


def test_a_case_about_another_term_shows_only_what_was_sent(world, staff, monkeypatch):
    _student, case = _sent_case(world)
    TimetableFeedback.objects.filter(pk=case.pk).update(term="2")

    def never(*_args, **_kwargs):
        raise AssertionError("the timetable must not be read for another term")

    monkeypatch.setattr("core.staff_timetable_feedback_views.read_student_term_timetable", never)
    html = _page(_as(staff["advisor"]), case)
    assert "no longer the term being planned" in html and escape(T["point_changed"]) not in html


def test_a_timetable_that_cannot_be_read_is_said_so(world, staff, monkeypatch):
    _student, case = _sent_case(world)

    def fail(*_args, **_kwargs):
        raise TimetableUnavailable("down")

    monkeypatch.setattr("core.staff_timetable_feedback_views.read_student_term_timetable", fail)
    html = _page(_as(staff["advisor"]), case)
    assert escape(T["current_unavailable"]) in html and case.reference in html


def _offers(html):
    return {
        name
        for name, marker in {
            "take": 'id="ttfsTakeForm"',
            "answer": 'id="ttfsAnswerForm"',
            "ask": 'id="ttfsAskForm"',
            "forward": 'id="ttfsForwardForm"',
            "assign": 'id="ttfsAssignForm"',
            "close": 'id="ttfsCloseForm"',
            "note": 'id="ttfsNoteForm"',
            "redact": "/redact/?",
        }.items()
        if marker in html
    }


@pytest.mark.parametrize(
    ("status", "advisor", "department"),
    [
        (
            "SUBMITTED",
            {"take", "answer", "ask", "forward", "close", "note"},
            {"take", "answer", "ask", "forward", "close", "note", "assign", "redact"},
        ),
        (
            "NEEDS_INFORMATION",
            {"take", "answer", "close", "note"},
            {"take", "answer", "close", "note", "assign", "redact"},
        ),
        (
            "FORWARDED",
            {"take", "answer", "close", "note"},
            {"take", "answer", "close", "note", "assign", "redact"},
        ),
        ("ANSWERED", {"close", "note"}, {"close", "note", "redact"}),
        ("CLOSED", {"note"}, {"note", "redact"}),
        ("WITHDRAWN", {"note"}, {"note", "redact"}),
    ],
)
def test_a_case_offers_each_person_what_they_can_do_in_its_status(
    world, staff, status, advisor, department
):
    fields = {"status": status, "answered_at": timezone.now()}
    if status == "CLOSED":
        fields["close_reason"] = "OTHER"
    _student, case = _sent_case(world, **fields)
    assert _offers(_page(_as(staff["advisor"]), case)) == advisor
    assert _offers(_page(_as(staff["department"]), case)) == department


def test_the_thread_says_who_wrote_and_who_can_read_each_message(world, staff):
    _student, case = _sent_case(world)
    staff_service.add_note(
        staff_service.staff_of(staff["advisor"]),
        case.reference,
        version=case.version,
        body="For colleagues.",
    )
    staff_service.ask(
        staff_service.staff_of(staff["department"]),
        case.reference,
        version=case.version + 1,
        body="Which section?",
    )
    html = _page(_as(staff["advisor"]), case)
    internal = html.index("For colleagues.")
    asked = html.index("Which section?")
    assert T["thread_internal"] in html[html.rindex("ttfs-internal", 0, internal) : internal]
    assert "Shown to the student as: The department" in html[:asked]
    assert '(<bdi dir="ltr">dept-one</bdi>)' in html


def test_the_history_names_staff_and_says_the_student_and_the_system(world, staff):
    _student, case = _sent_case(world)
    TimetableFeedbackEvent.objects.create(case=case, kind="AUTO_CLOSED")
    staff_service.take(
        staff_service.staff_of(staff["advisor"]), case.reference, version=case.version
    )
    html = _page(_as(staff["advisor"]), case)
    history = html[html.index('id="ttfsHistoryTitle"') :]
    assert (
        f'{T["history_student"]}</span>: <span dir="auto">{scopy.event_label("SUBMITTED", "en")}'
        in history
    )
    assert (
        f'{T["history_system"]}</span>: <span dir="auto">{scopy.event_label("AUTO_CLOSED", "en")}'
        in history
    )
    assert (
        'Adv-One Staff</span>: <span dir="auto">Took the case</span> (<span dir="auto">New → In review</span>)'
        in history
    )


def test_opening_a_case_is_audited_every_time_and_kept_in_its_history_once_an_hour(world, staff):
    _student, case = _sent_case(world)
    client = _as(staff["advisor"])
    before = AuditLog.objects.filter(action="timetable_feedback.case_view").count()
    start = case.version
    _page(client, case)
    _page(client, case)
    assert AuditLog.objects.filter(action="timetable_feedback.case_view").count() == before + 2
    assert case.events.filter(kind="VIEWED", actor=staff["advisor"]).count() == 1
    case.refresh_from_db()
    assert case.version == start


def test_the_case_page_reads_in_arabic(world, staff):
    _student, case = _sent_case(world)
    html = _page(_as(staff["advisor"]), case, "ar")
    _assert_language(html, "ar")
    for key in ("case_facts", "points_title", "thread_title", "actions_title", "action_take"):
        assert TA[key] in html, key
    assert "يتعارض مع مقرر آخر" in html


# ---------------------------------------------------------------------------
# Acting on one case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "fields", "status", "notice"),
    [
        ("take", {}, "IN_REVIEW", "TAKEN"),
        ("note", {"body": "Called."}, "SUBMITTED", "NOTE_ADDED"),
        ("ask", {"body": "Which section?"}, "NEEDS_INFORMATION", "ASKED"),
        ("answer", {"body": "It cannot move."}, "ANSWERED", "ANSWERED"),
        ("answer", {"template": "NOTED_FOR_PLANNING"}, "ANSWERED", "ANSWERED"),
        ("forward", {"body": "Needs the department."}, "FORWARDED", "FORWARDED"),
        ("close", {"reason": "DUPLICATE", "body": "Same as before."}, "CLOSED", "CLOSED"),
    ],
)
def test_an_action_is_taken_said_once_and_audited(world, staff, action, fields, status, notice):
    _student, case = _sent_case(world)
    start = case.version
    client = _as(staff["advisor"])
    response = _act(client, case, action, **fields)
    assert response.status_code == 302 and response.url == _case_url(case)
    case.refresh_from_db()
    assert (case.status, case.version) == (status, start + 1)
    page = _html(client.get(response.url))
    assert re.search(
        r'id="ttfsNotice"[^>]*><span dir="auto">' + re.escape(str(scopy.notice(notice, "en"))), page
    )
    # Said once: a reload does not repeat it.
    assert 'id="ttfsNotice"' not in _page(client, case)
    audit = AuditLog.objects.filter(action=f"timetable_feedback.{action}").latest("id")
    assert audit.status == "success" and case.reference in audit.details_json


def test_the_department_assigns_through_the_page(world, staff):
    _student, case = _sent_case(world)
    client = _as(staff["department"])
    response = _act(client, case, "assign", advisor_id="ADV-9")
    assert response.status_code == 302
    page = _html(client.get(response.url))
    assert str(scopy.notice("ASSIGNED", "en", advisor=copy.bdi("ADV-9"))) in page
    case.refresh_from_db()
    assert case.assigned_advisor_id == "ADV-9"


@pytest.mark.parametrize(
    ("action", "fields", "code", "field_id"),
    [
        ("ask", {"body": "   "}, "BODY_REQUIRED", "ttfsAsk"),
        ("answer", {"body": ""}, "BODY_REQUIRED", "ttfsAnswer"),
        (
            "note",
            {"body": "x" * (staff_service.STAFF_TEXT_MAX_CHARS + 1)},
            "BODY_TOO_LONG",
            "ttfsNote",
        ),
        ("forward", {"body": "bad" + chr(1)}, "BODY_INVALID", "ttfsForward"),
        ("close", {"reason": "", "body": "Kept text."}, "CLOSE_REASON_REQUIRED", "ttfsCloseReason"),
        ("answer", {"template": "NOPE"}, "TEMPLATE_UNKNOWN", "ttfsAnswerTemplate"),
    ],
)
def test_a_refused_action_says_why_at_the_field_and_keeps_what_was_typed(
    world, staff, action, fields, code, field_id
):
    _student, case = _sent_case(world)
    start = case.version
    response = _act(_as(staff["advisor"]), case, action, **fields)
    assert response.status_code == 400
    html = _html(response)
    sentence = str(scopy.refusal(code, "en"))
    assert re.search(r'id="ttfsErrors"[^>]*>.*' + re.escape(sentence), html, re.S)
    assert (
        re.search(rf'id="{field_id}"[^>]*aria-invalid="true"', html)
        or re.search(rf'aria-invalid="true"[^>]*id="{field_id}"', html)
        or re.search(rf'id="{field_id}"[^>]*aria-describedby="[^"]*ttfsError-0-field', html)
    )
    assert html.count(sentence) == 2  # the summary and the field
    typed = fields.get("body", "").strip()
    if typed and code != "BODY_INVALID":
        assert typed[:200] in html
    case.refresh_from_db()
    assert case.version == start
    audit = AuditLog.objects.filter(action=f"timetable_feedback.{action}").latest("id")
    assert audit.status == "deny" and code in audit.details_json


def test_a_case_changed_since_the_page_was_drawn_says_who_changed_it(world, staff):
    _student, case = _sent_case(world)
    staff_service.take(staff_service.staff_of(staff["twin"]), case.reference, version=case.version)
    response = _act(_as(staff["advisor"]), case, "answer", body="My answer, kept.")
    assert response.status_code == 409
    html = _html(response)
    assert str(scopy.refusal("CASE_CHANGED", "en")) in html
    assert "Last changed by Adv-One-B Staff on" in html
    assert "My answer, kept." in html
    # The page drawn again names the case as it is now.
    assert f'name="version" value="{case.version + 1}"' in html
    case.refresh_from_db()
    assert (case.status, case.handled_by) == ("IN_REVIEW", staff["twin"])


def test_a_refusal_is_not_drawn_for_someone_who_has_just_lost_the_case(world, staff, monkeypatch):
    """The page is drawn again after a refusal; if the case left the person's scope
    in the meantime (the student moved to another advisor), they get the
    not-found page, not the case with its comments."""
    _student, case = _sent_case(world)
    staff_service.add_note(
        staff_service.staff_of(staff["twin"]),
        case.reference,
        version=case.version,
        body=SENTINEL_NOTE,
    )
    case.refresh_from_db()

    def moved_then_refused(*_args, **_kwargs):
        Student.objects.filter(student_id=SID).update(advisor_id="ADV-9")
        raise staff_service.WorkflowRefused("BODY_REQUIRED", status=400)

    monkeypatch.setattr(staff_service, "ask", moved_then_refused)
    response = _act(_as(staff["advisor"]), case, "ask", body=" ")
    assert response.status_code == 404
    html = _html(response)
    assert str(scopy.refusal("CASE_NOT_FOUND", "en")) in html and SENTINEL_NOTE not in html


def test_a_bulk_answer_leaves_a_colleagues_case_and_says_so(world, staff):
    mine = _case(SID)
    colleagues = _case(SID, "NEEDS_INFORMATION", handled_by=staff["twin"])
    response = _as(staff["advisor"]).post(
        BULK,
        {
            "bulk": "answer",
            "template": "NOTED_FOR_PLANNING",
            "pick": [f"{mine.reference}@0", f"{colleagues.reference}@0"],
        },
    )
    html = _html(response)
    skipped = html[html.index('id="ttfsSkipped"') :]
    assert colleagues.reference in skipped
    assert str(scopy.refusal("HANDLED_BY_OTHER", "en")) in skipped
    colleagues.refresh_from_db()
    assert colleagues.status == "NEEDS_INFORMATION"


def test_an_unknown_action_is_refused(world, staff):
    _student, case = _sent_case(world)
    start = case.version
    response = _act(_as(staff["admin"]), case, "publish")
    assert response.status_code == 400
    case.refresh_from_db()
    assert case.version == start


def test_an_advisor_cannot_assign_by_posting_what_the_page_never_offered(world, staff):
    _student, case = _sent_case(world)
    response = _act(_as(staff["advisor"]), case, "assign", advisor_id="ADV-9")
    assert response.status_code == 403
    assert str(scopy.refusal("DEPARTMENT_ONLY", "en")) in _html(response)
    case.refresh_from_db()
    assert case.assigned_advisor_id == ""


def test_a_refused_action_reads_in_arabic(world, staff):
    _student, case = _sent_case(world)
    response = _act(_as(staff["advisor"]), case, "ask", "ar", body=" ")
    html = _html(response)
    _assert_language(html, "ar")
    assert str(scopy.refusal("BODY_REQUIRED", "ar")) in html and TA["errors_title"] in html


# ---------------------------------------------------------------------------
# What the student sees of it
# ---------------------------------------------------------------------------


def _student_page(student, case, language="en"):
    return _html(
        _get(student, reverse("student_timetable_feedback_detail", args=[case.reference]), language)
    )


def test_a_question_reaches_the_student_as_new_from_their_advisor(world, staff):
    student, case = _sent_case(world)
    _act(_as(staff["advisor"]), case, "ask", body="Which section suits you?")
    home = _html(_get(student, "/student/"))
    assert copy.STRINGS["banner_reply_title"][0] in home
    listing = _html(_get(student, "/student/timetable-feedback/"))
    assert UNREAD_MARK in listing
    page = _student_page(student, case)
    assert "Which section suits you?" in page
    assert re.search(r"<span>Your academic advisor</span> — ", page)
    assert 'id="ttfReplyForm"' in page
    # Seen now: the mark clears, and the next answer is new again.
    assert UNREAD_MARK not in _html(_get(student, "/student/timetable-feedback/"))


def test_an_answer_from_the_department_is_labelled_the_department(world, staff):
    student, case = _sent_case(world)
    _act(_as(staff["department"]), case, "answer", body="The department's answer.")
    page = _student_page(student, case)
    assert re.search(r"<span>The department</span> — ", page)
    assert 'id="ttfConfirmForm"' in page and 'id="ttfReopenForm"' in page
    home = _html(_get(student, "/student/"))
    # The page above marked it seen; the banner still asks for a verdict.
    assert copy.STRINGS["banner_answer_title"][0] in home


def test_a_ready_made_answer_reaches_an_arabic_student_in_arabic(world, staff):
    student, case = _sent_case(world)
    TimetableFeedback.objects.filter(pk=case.pk).update(submitted_language="ar")
    _act(_as(staff["advisor"]), case, "answer", template="REGISTERED_IN_PORTAL")
    page = _student_page(student, case, "ar")
    _assert_language(page, "ar")
    assert copy.REPLY_TEMPLATES["REGISTERED_IN_PORTAL"][1] in page


def test_staff_only_words_and_staff_names_never_reach_the_student(world, staff):
    student, case = _sent_case(world)
    client = _as(staff["advisor"])
    _act(client, case, "note", body=SENTINEL_NOTE)
    case.refresh_from_db()
    _act(client, case, "forward", body=SENTINEL_FORWARD)
    case.refresh_from_db()
    _act(_as(staff["department"]), case, "answer", body="Visible answer.")
    for language in ("en", "ar"):
        for url in (
            reverse("student_timetable_feedback_detail", args=[case.reference]),
            "/student/timetable-feedback/",
            "/student/",
        ):
            html = _html(_get(student, url, language))
            for secret in (
                SENTINEL_NOTE,
                SENTINEL_FORWARD,
                "adv-one",
                "Adv-One",
                "dept-one",
                "Dept-One",
                ADV,
            ):
                assert secret not in html, (url, language, secret)
    assert "Visible answer." in _student_page(student, case)


def test_a_closing_reaches_the_student_with_its_reason(world, staff):
    student, case = _sent_case(world)
    _act(_as(staff["advisor"]), case, "close", reason="NOT_APPLICABLE", body=SENTINEL_NOTE)
    listing = _html(_get(student, "/student/timetable-feedback/"))
    assert UNREAD_MARK in listing
    page = _student_page(student, case)
    assert copy.CLOSE_REASON["NOT_APPLICABLE"][0] in page and SENTINEL_NOTE not in page


def test_removed_words_are_gone_from_the_students_page(world, staff):
    student, case = _sent_case(world)
    point = case.points.get()
    client = _as(staff["department"])
    url = _case_url(case, "redact/")
    response = client.post(url, {"point": point.pk, "version": case.version, "remove": "my lab"})
    assert response.status_code == 302
    page = _student_page(student, case)
    assert "It clashes with […]." in page and "my lab" not in page
    assert copy.STRINGS["detail_note_removed"][0] in page


def test_forwarding_and_comments_are_not_news_to_the_student(world, staff):
    student, case = _sent_case(world)
    client = _as(staff["advisor"])
    _act(client, case, "note", body="A comment.")
    case.refresh_from_db()
    _act(client, case, "forward", body="To the department.")
    assert UNREAD_MARK not in _html(_get(student, "/student/timetable-feedback/"))
    page = _student_page(student, case)
    assert copy.STATUS["FORWARDED"][0] in page


# ---------------------------------------------------------------------------
# Removing words, through its page
# ---------------------------------------------------------------------------


def test_the_department_removes_words_from_a_message_through_its_page(world, staff):
    student, case = _sent_case(world)
    student.post(
        reverse("student_timetable_feedback_follow_up", args=[case.reference]),
        {"body": "My father is in hospital."},
    )
    message = case.messages.get()
    case.refresh_from_db()
    client = _as(staff["department"])
    url = _case_url(case, "redact/")
    form = _html(client.get(url, {"message": message.pk}))
    assert "My father is in hospital." in form and 'id="ttfsRedactForm"' in form
    response = client.post(
        url, {"message": message.pk, "version": case.version, "remove": "father is in hospital"}
    )
    assert response.status_code == 302
    message.refresh_from_db()
    assert message.body == "My […]."
    assert str(scopy.notice("REDACTED", "en")) in _html(client.get(response.url))
    assert AuditLog.objects.filter(action="timetable_feedback.redact", status="success").exists()


def test_a_removal_that_matches_nothing_is_said_and_keeps_what_was_typed(world, staff):
    _student, case = _sent_case(world)
    point = case.points.get()
    response = _as(staff["department"]).post(
        _case_url(case, "redact/"),
        {"point": point.pk, "version": case.version, "remove": "not in it"},
    )
    assert response.status_code == 400
    html = _html(response)
    assert str(scopy.refusal("REDACT_TEXT_NOT_FOUND", "en")) in html and "not in it" in html
    point.refresh_from_db()
    assert point.note == "It clashes with my lab."


def test_only_the_department_reaches_the_removal_page(world, staff):
    _student, case = _sent_case(world)
    point = case.points.get()
    response = _as(staff["advisor"]).get(_case_url(case, "redact/"), {"point": point.pk})
    assert response.status_code == 403
    assert str(scopy.refusal("DEPARTMENT_ONLY", "en")) in _html(response)


@pytest.mark.parametrize(
    "query",
    [{"point": "nope"}, {"message": "12"}, {}, {"point": "00000000-0000-0000-0000-000000000000"}],
)
def test_the_removal_page_finds_only_this_cases_own_text(world, staff, query):
    _student, case = _sent_case(world)
    assert _as(staff["department"]).get(_case_url(case, "redact/"), query).status_code == 404


# ---------------------------------------------------------------------------
# Many cases at once, through the queue
# ---------------------------------------------------------------------------


def test_a_bulk_answer_answers_what_it_can_and_lists_the_rest(world, staff):
    first, second = _case(SID), _case(OTHER_SID)
    response = _as(staff["advisor"]).post(
        BULK,
        {
            "bulk": "answer",
            "template": "NOTED_FOR_PLANNING",
            "pick": [f"{first.reference}@0", f"{second.reference}@5"],
        },
    )
    assert response.status_code == 200
    html = _html(response)
    assert re.search(r'id="ttfsDone".*' + first.reference, html, re.S)
    assert re.search(r'id="ttfsSkipped".*' + second.reference, html, re.S)
    assert str(scopy.refusal("CASE_CHANGED", "en")) in html
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.status, second.status) == ("ANSWERED", "SUBMITTED")
    audit = AuditLog.objects.filter(action="timetable_feedback.bulk_answer").latest("id")
    assert first.reference in audit.details_json and second.reference in audit.details_json


def test_a_bulk_close_without_a_selection_says_so(world, staff):
    response = _as(staff["advisor"]).post(BULK, {"bulk": "close", "note": "Dup."})
    assert response.status_code == 400
    assert str(scopy.refusal("BULK_NOTHING", "en")) in _html(response)


def test_the_queue_offers_a_bulk_action_on_the_cases_it_lists(world, staff):
    case = _case(SID)
    html = _queue(_as(staff["advisor"]))
    assert f'name="pick" value="{case.reference}@0"' in html
    assert 'name="bulk" value="answer"' in html and 'name="bulk" value="close"' in html
    for key in copy.REPLY_TEMPLATES:
        assert f'<option value="{key}">' in html


# ---------------------------------------------------------------------------
# The sidebar, and the MRI page
# ---------------------------------------------------------------------------


def _sidebar_link(html):
    return 'href="/ops/timetable-feedback/"' in html


@pytest.mark.parametrize("who", ["advisor", "department", "admin"])
def test_staff_find_the_queue_in_the_sidebar_with_what_waits_on_them(world, staff, who):
    _case(SID)
    _case(SID, "FORWARDED")
    html = _html(_as(staff[who]).get(GUIDE))
    assert _sidebar_link(html)
    count = staff_service.action_count(staff_service.staff_of(staff[who]))
    assert count >= 1
    assert f'<span class="nav-badge" aria-hidden="true">{count}</span>' in html
    assert f"(Waiting on you: {count})" in html


def test_the_sidebar_link_is_lit_on_every_staff_feedback_page(world, staff):
    case = _case(SID)
    for url in (QUEUE, GUIDE, _case_url(case)):
        html = _html(_as(staff["advisor"]).get(url))
        assert re.search(
            r'nav-link active" href="/ops/timetable-feedback/" tabindex="0" aria-current="page"',
            html,
        ), url


def test_no_count_is_drawn_when_nothing_waits(world, staff):
    html = _html(_as(staff["advisor"]).get(GUIDE))
    assert _sidebar_link(html) and "Waiting on you" not in html


def test_the_sidebar_link_is_absent_for_students_and_while_the_feature_is_off(
    world, staff, settings
):
    assert not _sidebar_link(_html(_get(_client(), "/student/")))
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    assert not _sidebar_link(_html(_as(staff["advisor"]).get("/advisor-portfolio/")))


@pytest.mark.parametrize(
    ("who", "offered"),
    [
        ("advisor", True),
        ("department", True),
        ("admin", True),
        ("committee", False),
        ("student", False),
    ],
)
def test_the_staff_link_is_offered_to_staff_roles_only(world, staff, who, offered):
    """The flag itself, not only what the sidebar draws: a student's or the
    committee's sidebar has no staff group, so the drawing alone would not show a
    wrong flag."""
    from django.test import RequestFactory

    from core.services import student_otp
    from core.sidebar_context import get_sidebar_context

    user = student_otp.provision_student_user(SID) if who == "student" else staff[who]
    request = RequestFactory().get(GUIDE)
    request.user = user
    context = get_sidebar_context(request)
    assert context["can_timetable_feedback_staff"] is offered
    if not offered:
        assert context["timetable_feedback_staff_count"] == 0


def test_a_failing_count_never_takes_the_page_down(world, staff, monkeypatch):
    def broken(_staff):
        raise RuntimeError("count failed")

    monkeypatch.setattr(staff_service, "action_count", broken)
    response = _as(staff["advisor"]).get(GUIDE)
    assert response.status_code == 200 and _sidebar_link(_html(response))


def test_the_mri_page_shows_no_feedback_link_and_runs_no_feedback_query(world, staff):
    """The owner's rule: the MRI page stays exactly as it is. Read only; no MRI file
    is touched by this feature."""
    _case(SID)
    client = _as(staff["admin"])
    with CaptureQueriesContext(connection) as captured:
        response = client.get(reverse("timetable_workspace_mri_page"))
    assert response.status_code == 200
    html = _html(response)
    assert "/ops/timetable-feedback/" not in html
    assert not [q["sql"] for q in captured if "timetable_feedback" in q["sql"]]
    # The same person on any other page does get the link.
    assert _sidebar_link(_html(client.get("/advisor-portfolio/")))


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_guide_explains_each_step_rule_and_issue(staff, language):
    html = _html(_as(staff["advisor"]).get(GUIDE, headers={"accept-language": language}))
    _assert_language(html, language)
    index = 1 if language == "ar" else 0
    for title, _body in scopy.GUIDE_STEPS:
        assert title[index] in html
    for rule in scopy.GUIDE_RULES:
        assert str(scopy.fill(rule, language)) in html
    for advice in scopy.GUIDE_REASONS.values():
        assert str(scopy.fill(advice, language)) in html
    days = "14 days" if language == "en" else "14 يومًا"
    assert days in html
