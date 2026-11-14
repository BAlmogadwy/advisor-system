"""The student's actions on a timetable-feedback case, what is new to them, and the
home banner, through the real URLs, views and templates.

Each action is offered only where the service would take it: the offers are
asserted status by status, and every refusal by its status, its sentence and
what it left unchanged. Unread is asserted from the fields the service reads,
and every Arabic assertion runs on a page proven to be in Arabic.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import TimetableFeedback, TimetableFeedbackMessage, TimetableFeedbackPoint
from core.services import rate_limit
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_workflow as workflow
from core.sidebar_context import get_sidebar_context
from tests.test_timetable_feedback_student_views import (
    LIST,
    OTHER_SID,
    SID,
    _assert_language,
    _client,
    _get,
    _html,
    _post,
    _send,
    make_world,
    set_flags,
)

pytestmark = pytest.mark.django_db

T = copy.strings("en")


@pytest.fixture(autouse=True)
def _flags(settings, monkeypatch):
    set_flags(settings, monkeypatch)
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (100, 3600))


@pytest.fixture
def world(db):
    return make_world()


def _case_in(world, status="SUBMITTED", *, points=1, answered_days_ago=1, **fields):
    """A case sent through the form, then moved to ``status`` as staff would, with
    ``points`` live points. Returns (client, case)."""
    client = _client()
    _send(client, world)
    case = TimetableFeedback.objects.get(student_id=SID)
    for n in range(points - 1):
        TimetableFeedbackPoint.objects.create(
            case=case,
            anchor_kind="TIMETABLE",
            anchor_key="TIMETABLE",
            reason_code="OTHER",
            note=f"point {n}",
            seen_fingerprint=f"{n:064d}",
        )
    changes = {"status": status, **fields}
    if status == "ANSWERED":
        changes.setdefault("answered_at", timezone.now() - timedelta(days=answered_days_ago))
    if status == "CLOSED":
        changes.setdefault("close_reason", "DUPLICATE")
        changes.setdefault("closed_at", timezone.now())
    TimetableFeedback.objects.filter(pk=case.pk).update(**changes)
    case.refresh_from_db()
    return client, case


def _url(name, case, *extra):
    return reverse(f"student_timetable_feedback_{name}", args=[case.reference, *extra])


def _page(client, case, language="en"):
    return _html(_get(client, _url("detail", case), language))


def _offers(html, case):
    """What the case page offers, by the controls it draws."""
    return {
        "reply": 'id="ttfReplyForm"' in html,
        "follow_up": 'id="ttfFollowUpForm"' in html,
        "confirm": 'id="ttfConfirmForm"' in html,
        "reopen": 'id="ttfReopenForm"' in html,
        "withdraw_case": f'href="{_url("withdraw", case)}"' in html,
        "withdraw_point": 'class="ttf-point-action"' in html,
    }


# ---------------------------------------------------------------------------
# What the case page offers
# ---------------------------------------------------------------------------


NOTHING = dict.fromkeys(
    ("reply", "follow_up", "confirm", "reopen", "withdraw_case", "withdraw_point"), False
)


@pytest.mark.parametrize(
    ("status", "points", "offered"),
    [
        ("SUBMITTED", 1, {"follow_up", "withdraw_case"}),
        ("SUBMITTED", 2, {"follow_up", "withdraw_case", "withdraw_point"}),
        ("IN_REVIEW", 2, {"follow_up", "withdraw_case", "withdraw_point"}),
        ("FORWARDED", 2, {"follow_up", "withdraw_case", "withdraw_point"}),
        ("NEEDS_INFORMATION", 2, {"reply", "withdraw_case", "withdraw_point"}),
        ("ANSWERED", 2, {"confirm", "reopen", "withdraw_case"}),
        ("CLOSED", 2, set()),
        ("WITHDRAWN", 2, set()),
    ],
)
def test_a_case_offers_what_the_service_would_take_in_its_status(world, status, points, offered):
    client, case = _case_in(world, status, points=points)
    html = _page(client, case)
    assert _offers(html, case) == {**NOTHING, **dict.fromkeys(offered, True)}
    # An action that does not belong to this status is simply absent: a reason is
    # given only for a limit reached or a time run out.
    assert "Withheld" not in html


def test_an_answer_past_its_window_can_be_accepted_but_not_reopened(world):
    client, case = _case_in(world, "ANSWERED", answered_days_ago=30)
    html = _page(client, case)
    assert _offers(html, case) == {**NOTHING, "confirm": True, "withdraw_case": True}
    withheld = html.split('id="ttfReopenWithheld"', 1)[1].split("</p>", 1)[0]
    assert str(copy.refusal("REOPEN_WINDOW_PASSED", "en")) in withheld
    assert "can be reopened until" not in html  # no date for a window already shut


def test_inside_its_window_an_answer_says_until_when_it_can_be_reopened(world):
    client, case = _case_in(world, "ANSWERED", answered_days_ago=3)
    until = (case.answered_at + timedelta(days=14)).astimezone(timezone.get_current_timezone())
    html = _page(client, case)
    assert f'<bdi dir="ltr">{until:%Y-%m-%d}</bdi>' in html.split('id="ttfAnswer"', 1)[1]


def test_with_every_follow_up_used_the_page_says_so_instead_of_offering_one(world):
    client, case = _case_in(world, "SUBMITTED")
    for _ in range(workflow.MAX_FOLLOW_UPS):
        workflow.follow_up(student_id=SID, reference=case.reference, body="more")
    html = _page(client, case)
    assert not _offers(html, case)["follow_up"]
    withheld = html.split('id="ttfFollowUpWithheld"', 1)[1].split("</p>", 1)[0]
    assert str(copy.refusal("TOO_MANY_FOLLOW_UPS", "en")) in withheld


def test_the_follow_up_form_counts_what_is_left(world):
    client, case = _case_in(world, "SUBMITTED")
    workflow.follow_up(student_id=SID, reference=case.reference, body="one")
    hint = copy.text(
        "detail_follow_up_hint",
        "en",
        left=copy.bdi(workflow.MAX_FOLLOW_UPS - 1),
        max=copy.bdi(workflow.STUDENT_TEXT_MAX_CHARS),
    )
    assert str(hint) in _page(client, case)


def test_each_live_point_gets_a_link_named_by_its_point(world):
    client, case = _case_in(world, "SUBMITTED", points=3)
    gone = case.points.get(note="point 0")
    TimetableFeedbackPoint.objects.filter(pk=gone.pk).update(withdrawn_at=timezone.now())
    html = _page(client, case)
    assert html.count('class="ttf-point-action"') == 2
    assert _url("withdraw_point", case, gone.pk) not in html
    for point in case.points.filter(withdrawn_at__isnull=True):
        assert (
            f'href="{_url("withdraw_point", case, point.pk)}" aria-describedby="ttfPoint-{point.pk}"'
            in html
        )
        assert f'id="ttfPoint-{point.pk}"' in html


# ---------------------------------------------------------------------------
# Each action, when it is taken
# ---------------------------------------------------------------------------


def _notice(client, response, code, case):
    """Whether the redirect lands on the case with exactly this one-time notice, in
    the notice itself: the page's own text may say something alike."""
    assert response.status_code == 302 and response["Location"] == _url("detail", case)
    html = _html(_get(client, response["Location"]))
    shown = html.split('id="ttfNotice"', 1)[1].split("</div>", 1)[0]
    return str(copy.notice(code, "en", reference=case.reference)) in shown


def test_a_reply_is_sent_and_said_once(world):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    response = _post(client, _url("reply", case), {"body": "Line one\r\nLine two"})
    assert _notice(client, response, "REPLY_SENT", case)
    case.refresh_from_db()
    assert case.status == "SUBMITTED"
    message = TimetableFeedbackMessage.objects.get(case=case)
    # The browser's CR LF is one line break, as everywhere else in the feedback.
    assert (message.author_kind, message.body) == ("STUDENT", "Line one\nLine two")
    notice = str(copy.notice("REPLY_SENT", "en", reference=case.reference))
    assert notice not in _page(client, case)


def test_a_follow_up_is_added(world):
    client, case = _case_in(world, "IN_REVIEW")
    response = _post(client, _url("follow_up", case), {"body": "One more thing"})
    assert _notice(client, response, "FOLLOW_UP_SENT", case)
    assert TimetableFeedbackMessage.objects.get(case=case).body == "One more thing"
    assert TimetableFeedback.objects.get(pk=case.pk).status == "IN_REVIEW"


@pytest.mark.parametrize(
    ("sent", "stored"), [("yes", True), ("no", False), ("", None), ("on", None), ("true", None)]
)
def test_accepting_an_answer_closes_it_and_keeps_only_a_real_yes_or_no(world, sent, stored):
    client, case = _case_in(world, "ANSWERED")
    response = _post(client, _url("confirm", case), {"helpful": sent})
    assert _notice(client, response, "ANSWER_CONFIRMED", case)
    case.refresh_from_db()
    assert (case.status, case.close_reason, case.answer_helpful) == (
        "CLOSED",
        "ANSWER_ACCEPTED",
        stored,
    )


def test_an_answer_is_reopened_with_the_reason(world):
    client, case = _case_in(world, "ANSWERED")
    response = _post(client, _url("reopen", case), {"body": "Still clashes on Sunday."})
    assert _notice(client, response, "ANSWER_REOPENED", case)
    case.refresh_from_db()
    assert case.status == "SUBMITTED" and case.answer_helpful is False
    assert TimetableFeedbackMessage.objects.get(case=case).body == "Still clashes on Sunday."


def test_withdrawing_the_case_asks_first_then_withdraws(world):
    client, case = _case_in(world, "SUBMITTED")
    asked = _get(client, _url("withdraw", case))
    html = _html(asked)
    assert asked.status_code == 200
    assert f'action="{_url("withdraw", case)}"' in html
    assert f'href="{_url("detail", case)}"' in html and T["confirm_keep"] in html
    assert "24 hours" in html
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert case.reference in title and "<" not in title and "&lt;" not in title
    assert TimetableFeedback.objects.get(pk=case.pk).status == "SUBMITTED"  # asking changed nothing

    assert _notice(client, _post(client, _url("withdraw", case), {}), "CASE_WITHDRAWN", case)
    assert TimetableFeedback.objects.get(pk=case.pk).status == "WITHDRAWN"


def test_withdrawing_a_point_asks_first_naming_it_then_withdraws_only_it(world):
    client, case = _case_in(world, "SUBMITTED", points=2)
    point = case.points.get(note="point 0")
    asked = _get(client, _url("withdraw_point", case, point.pk))
    html = _html(asked)
    assert asked.status_code == 200
    assert T["detail_whole_timetable"] in html  # the point it is about
    assert f'action="{_url("withdraw_point", case, point.pk)}"' in html
    assert TimetableFeedbackPoint.objects.filter(withdrawn_at__isnull=True).count() == 2

    response = _post(client, _url("withdraw_point", case, point.pk), {})
    assert _notice(client, response, "POINT_WITHDRAWN", case)
    point.refresh_from_db()
    assert point.withdrawn_at is not None
    assert TimetableFeedback.objects.get(pk=case.pk).status == "SUBMITTED"
    assert case.points.filter(withdrawn_at__isnull=True).count() == 1


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_confirmation_pages_read_in_the_students_language(world, language):
    client, case = _case_in(world, "SUBMITTED", points=2)
    point = case.points.get(note="point 0")
    for url, key in (
        (_url("withdraw", case), "confirm_case_yes"),
        (_url("withdraw_point", case, point.pk), "confirm_point_yes"),
    ):
        html = _html(_get(client, url, language))
        _assert_language(html, language)
        assert copy.strings(language)[key] in html
        assert copy.strings(language)["confirm_keep"] in html


# ---------------------------------------------------------------------------
# Each action, when it is refused
# ---------------------------------------------------------------------------


def _summary(html):
    return html.split('id="ttfErrors"', 1)[1].split("</div>", 1)[0]


@pytest.mark.parametrize(
    ("body", "code"),
    [("   ", "BODY_REQUIRED"), ("x" * 1001, "BODY_TOO_LONG"), ("bad" + chr(1), "BODY_INVALID")],
)
def test_a_reply_that_cannot_be_sent_is_said_at_its_field_and_keeps_the_text(world, body, code):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    response = _post(client, _url("reply", case), {"body": body})
    html = _html(response)
    assert response.status_code == 400
    message = str(copy.refusal(code, "en"))
    assert message in _summary(html)
    assert f'<p class="ttf-field-error" id="ttfError-0-field" dir="auto">{message}</p>' in html
    assert re.search(r'id="ttfReply"[^>]*aria-describedby="ttfReplyHint ttfError-0-field"', html)
    assert re.search(r'id="ttfReply"[^>]*aria-invalid="true"', html)
    if code == "BODY_TOO_LONG":
        assert ">" + body + "</textarea>" in html  # nothing typed is lost
    assert TimetableFeedback.objects.get(pk=case.pk).status == "NEEDS_INFORMATION"
    assert not TimetableFeedbackMessage.objects.exists()


@pytest.mark.parametrize(
    ("route", "field"),
    [("follow_up", "ttfFollowUp"), ("reopen", "ttfReopen")],
)
def test_the_other_texts_are_checked_and_kept_the_same_way(world, route, field):
    client, case = _case_in(world, "ANSWERED" if route == "reopen" else "SUBMITTED")
    response = _post(client, _url(route, case), {"body": " "})
    html = _html(response)
    assert response.status_code == 400
    assert re.search(rf'id="{field}"[^>]*aria-describedby="{field}Hint ttfError-0-field"', html)


def test_an_action_the_case_has_moved_past_is_refused_and_the_page_shows_it_as_it_is(world):
    """The page offered a reply; the advisor took the question back before the press."""
    client, case = _case_in(world, "NEEDS_INFORMATION")
    TimetableFeedback.objects.filter(pk=case.pk).update(status="IN_REVIEW")
    response = _post(client, _url("reply", case), {"body": "Here it is."})
    html = _html(response)
    assert response.status_code == 409
    assert str(copy.refusal("NOT_ALLOWED", "en")) in _summary(html)
    assert 'class="ttf-field-error"' not in html  # not the text's fault
    # Drawn as the case is now: a follow-up is what it takes, and the text is kept.
    assert _offers(html, case)["follow_up"] and not _offers(html, case)["reply"]
    follow_up = html.split('id="ttfFollowUp"', 1)[1].split("</textarea>", 1)[0]
    assert follow_up.endswith(">Here it is.")
    assert not TimetableFeedbackMessage.objects.exists()


def test_too_many_actions_wait_for_the_limiters_own_time(world, monkeypatch):
    client, case = _case_in(world, "SUBMITTED")
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (1, 3600))
    assert _post(client, _url("follow_up", case), {"body": "one"}).status_code == 302
    response = _post(client, _url("follow_up", case), {"body": "two"})
    html = _html(response)
    assert response.status_code == 429
    wait = int(response["Retry-After"])
    assert wait > 0
    assert str(copy.refusal("RATE_LIMITED", "en", retry_after=wait)) in _summary(html)
    # The same form is drawn again, and the wait is not the text's fault.
    assert 'id="ttfFollowUpForm"' in html and 'class="ttf-field-error"' not in html
    assert TimetableFeedbackMessage.objects.count() == 1


def test_a_case_that_changed_under_the_press_is_drawn_as_it_is_now(world, monkeypatch):
    """CASE_CHANGED means someone moved the case while the student pressed: the page
    must show the case as it now stands, not as the request first read it."""
    client, case = _case_in(world, "NEEDS_INFORMATION")

    def moved_meanwhile(**_):
        TimetableFeedback.objects.filter(pk=case.pk).update(status="IN_REVIEW")
        raise workflow.WorkflowRefused("CASE_CHANGED")

    monkeypatch.setattr(workflow, "reply", moved_meanwhile)
    response = _post(client, _url("reply", case), {"body": "Here it is."})
    html = _html(response)
    assert response.status_code == 409
    assert str(copy.refusal("CASE_CHANGED", "en")) in _summary(html)
    assert _offers(html, case)["follow_up"] and not _offers(html, case)["reply"]


def test_a_refused_acceptance_keeps_the_answer_chosen(world, monkeypatch):
    client, case = _case_in(world, "ANSWERED")
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (1, 3600))
    assert rate_limit.consume(rate_limit.TIMETABLE_FEEDBACK_REPLY, SID).allowed
    response = _post(client, _url("confirm", case), {"helpful": "no"})
    html = _html(response)
    assert response.status_code == 429
    assert re.search(r'name="helpful" value="no" checked', html)
    assert not re.search(r'name="helpful" value="yes" checked', html)
    assert TimetableFeedback.objects.get(pk=case.pk).status == "ANSWERED"


def test_the_last_point_is_not_asked_about_alone(world):
    client, case = _case_in(world, "SUBMITTED")
    point = case.points.get()
    response = _get(client, _url("withdraw_point", case, point.pk))
    assert response.status_code == 409
    assert str(copy.refusal("LAST_POINT", "en")) in _summary(_html(response))
    response = _post(client, _url("withdraw_point", case, point.pk), {})
    assert response.status_code == 409
    point.refresh_from_db()
    assert point.withdrawn_at is None


@pytest.mark.parametrize("point_id", ["not-a-point", "00000000-0000-0000-0000-000000000000"])
def test_a_point_that_is_not_this_cases_is_not_found(world, point_id):
    client, case = _case_in(world, "SUBMITTED", points=2)
    for response in (
        _get(client, _url("withdraw_point", case, point_id)),
        _post(client, _url("withdraw_point", case, point_id), {}),
    ):
        assert response.status_code == 404
        assert str(copy.refusal("POINT_NOT_FOUND", "en")) in _summary(_html(response))
    assert case.points.filter(withdrawn_at__isnull=True).count() == 2


def test_a_finished_case_is_not_asked_about(world):
    client, case = _case_in(world, "CLOSED")
    response = _get(client, _url("withdraw", case))
    assert response.status_code == 409
    assert str(copy.refusal("NOT_ALLOWED", "en")) in _summary(_html(response))


# ---------------------------------------------------------------------------
# Who may act, and when
# ---------------------------------------------------------------------------


def _routes(case, point):
    return [
        ("post", _url("reply", case), {"body": "x"}),
        ("post", _url("follow_up", case), {"body": "x"}),
        ("post", _url("confirm", case), {"helpful": "yes"}),
        ("post", _url("reopen", case), {"body": "x"}),
        ("get", _url("withdraw", case), {}),
        ("post", _url("withdraw", case), {}),
        ("get", _url("withdraw_point", case, point.pk), {}),
        ("post", _url("withdraw_point", case, point.pk), {}),
    ]


def test_another_students_case_is_not_found_by_any_action(world):
    _, case = _case_in(world, "NEEDS_INFORMATION", points=2)
    point = case.points.first()
    stranger = _client(OTHER_SID)
    before = TimetableFeedback.objects.values().get(pk=case.pk)
    for method, url, data in _routes(case, point):
        response = getattr(stranger, method)(url, data)
        assert response.status_code == 404, (method, url)
        assert str(copy.refusal("CASE_NOT_FOUND", "en")) in _html(response)
    assert TimetableFeedback.objects.values().get(pk=case.pk) == before
    assert not TimetableFeedbackMessage.objects.exists()


def test_with_the_feature_off_no_action_exists(world, settings):
    client, case = _case_in(world, "NEEDS_INFORMATION", points=2)
    point = case.points.first()
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    for method, url, data in _routes(case, point):
        assert getattr(client, method)(url, data).status_code == 404, (method, url)
    assert TimetableFeedback.objects.get(pk=case.pk).status == "NEEDS_INFORMATION"


@pytest.mark.parametrize(
    "setting", ["TIMETABLE_FEEDBACK_ACCEPTING", "TIMETABLE_FEEDBACK_PROGRAMMES"]
)
def test_closing_new_feedback_does_not_close_the_conversation(world, settings, setting):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    setattr(settings, setting, False if setting.endswith("ACCEPTING") else ("OTHER",))
    assert _offers(_page(client, case), case)["reply"]
    assert _post(client, _url("reply", case), {"body": "Here."}).status_code == 302


def test_staff_are_sent_to_their_dashboard(world):
    _, case = _case_in(world, "NEEDS_INFORMATION")
    staff = User.objects.create_user("advisor-ttf-actions", password="x")
    client = Client()
    client.force_login(staff)
    response = client.post(_url("reply", case), {"body": "x"})
    assert response.status_code == 302 and response["Location"] == reverse("dashboard")


def test_actions_need_the_forms_token(world):
    _, case = _case_in(world, "NEEDS_INFORMATION")
    client = _client(enforce_csrf_checks=True)
    assert client.post(_url("reply", case), {"body": "x"}).status_code == 403
    assert not TimetableFeedbackMessage.objects.exists()


@pytest.mark.parametrize("route", ["reply", "follow_up", "confirm", "reopen"])
def test_the_writing_routes_answer_only_a_post(world, route):
    client, case = _case_in(world, "SUBMITTED")
    assert _get(client, _url(route, case)).status_code == 405


# ---------------------------------------------------------------------------
# What is new to the student
# ---------------------------------------------------------------------------


def _news(case, minutes_ago=5):
    """What staff will do in PR 4: something the student may read, just now."""
    at = timezone.now() - timedelta(minutes=minutes_ago)
    TimetableFeedback.objects.filter(pk=case.pk).update(last_student_visible_at=at)
    return at


def _badge(html):
    match = re.search(
        r'<span class="nav-badge" aria-hidden="true">(\d+)</span>'
        r'<span class="visually-hidden"> \(New: (\d+)\)</span>',
        html,
    )
    return None if match is None else (int(match.group(1)), int(match.group(2)))


def test_news_is_marked_on_the_list_the_sidebar_and_the_home_card_until_opened(world):
    client, case = _case_in(world, "IN_REVIEW")
    listed = _html(_get(client, LIST))
    assert "ttf-unread" not in listed.split("<main", 1)[1] and _badge(listed) is None

    at = _news(case)
    listed = _html(_get(client, LIST))
    row = listed.split('class="ttf-case-link"', 1)[1].split("</a>", 1)[0]
    assert (
        f'class="ttf-unread"><span class="ttf-unread-dot" aria-hidden="true"></span>{T["unread"]}'
        in row
    )
    assert _badge(listed) == (1, 1)
    home = _html(_get(client, reverse("student_home")))
    card = home.split('id="student-home-feedback"', 1)[1].split("</div>", 1)[0]
    assert T["unread"] in card and _badge(home) == (1, 1)

    # Opening the case is seeing it: its own page already counts it as seen.
    opened = _page(client, case)
    assert _badge(opened) is None
    case.refresh_from_db()
    assert case.student_seen_at == at
    assert "ttf-unread" not in _html(_get(client, LIST)).split("<main", 1)[1]


def test_a_refused_action_marks_the_case_seen_as_its_page_shows_it(world):
    """A refusal redraws the whole case, the news included, so it is seen too, and
    the sidebar on that same page no longer counts it."""
    client, case = _case_in(world, "NEEDS_INFORMATION")
    at = _news(case)
    response = _post(client, _url("reply", case), {"body": "  "})
    assert response.status_code == 400
    assert _badge(_html(response)) is None
    assert TimetableFeedback.objects.get(pk=case.pk).student_seen_at == at


def test_opening_a_case_marks_it_without_disturbing_any_claim(world):
    client, case = _case_in(world, "IN_REVIEW")
    _news(case)
    case.refresh_from_db()
    claim = (case.version, case.updated_at, case.status)
    _page(client, case)
    case.refresh_from_db()
    assert (case.version, case.updated_at, case.status) == claim


def test_only_the_students_own_news_is_counted(world):
    _, case = _case_in(world, "IN_REVIEW")
    _news(case)
    assert _badge(_html(_get(_client(OTHER_SID), LIST))) is None
    # Opening someone else's case is not found, so it marks nothing either.
    _get(_client(OTHER_SID), _url("detail", case))
    assert TimetableFeedback.objects.get(pk=case.pk).student_seen_at is None


def test_the_students_own_actions_are_never_news_to_them(world):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    _post(client, _url("reply", case), {"body": "Here."})
    _post(client, _url("follow_up", case), {"body": "And this."})
    assert TimetableFeedback.objects.get(pk=case.pk).last_student_visible_at is None
    assert _badge(_html(_get(client, LIST))) is None


def _sidebar_queries(user, rf):
    request = rf.get("/")
    request.user = user
    with CaptureQueriesContext(connection) as queries:
        context = get_sidebar_context(request)
    feedback_queries = [
        q["sql"] for q in queries.captured_queries if "timetable_feedback" in q["sql"]
    ]
    return context["timetable_feedback_unread"], feedback_queries


def test_staff_pages_pay_no_query_for_the_students_badge(world, rf):
    staff = User.objects.create_user("advisor-ttf-badge", password="x")
    assert _sidebar_queries(staff, rf) == (0, [])


def test_with_the_feature_off_a_students_pages_pay_no_query_for_it(world, rf, settings):
    _, case = _case_in(world, "IN_REVIEW")
    _news(case)
    student = User.objects.get(username=str(SID))
    count, queries = _sidebar_queries(student, rf)
    assert count == 1 and len(queries) == 1  # on: one query
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    assert _sidebar_queries(student, rf) == (0, [])


# ---------------------------------------------------------------------------
# The home banner
# ---------------------------------------------------------------------------


def _banner(html):
    if 'id="student-home-feedback-banner"' not in html:
        return None
    return html.split('id="student-home-feedback-banner"', 1)[1].split("</section>", 1)[0]


def test_a_request_for_information_leads_the_home_page(world):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    banner = _banner(_html(_get(client, reverse("student_home"))))
    assert 'data-kind="reply"' in banner
    assert T["banner_reply_title"] in banner
    assert str(copy.text("banner_reply_body", "en", reference=copy.bdi(case.reference))) in banner
    assert f'href="{_url("detail", case)}"' in banner


def test_an_answer_leads_the_home_page_until_its_window_closes(world):
    client, case = _case_in(world, "ANSWERED", answered_days_ago=3)
    banner = _banner(_html(_get(client, reverse("student_home"))))
    assert 'data-kind="answer"' in banner
    until = (case.answered_at + timedelta(days=14)).astimezone(timezone.get_current_timezone())
    assert f'<bdi dir="ltr">{until:%Y-%m-%d}</bdi>' in banner

    TimetableFeedback.objects.filter(pk=case.pk).update(
        answered_at=timezone.now() - timedelta(days=30)
    )
    assert _banner(_html(_get(client, reverse("student_home")))) is None


def test_news_on_a_closed_case_of_this_term_still_leads_the_home_page(world):
    client, case = _case_in(world, "CLOSED")
    _news(case)
    banner = _banner(_html(_get(client, reverse("student_home"))))
    assert 'data-kind="news"' in banner
    assert str(copy.text("banner_news_body", "en", reference=copy.bdi(case.reference))) in banner


def test_nothing_new_and_nothing_waiting_means_no_banner(world):
    client, _ = _case_in(world, "IN_REVIEW")
    assert _banner(_html(_get(client, reverse("student_home")))) is None


def test_another_terms_news_is_counted_but_does_not_lead_this_terms_home_page(world):
    client, case = _case_in(world, "IN_REVIEW")
    TimetableFeedback.objects.filter(pk=case.pk).update(academic_year="1447", term="2")
    _news(case)
    home = _html(_get(client, reverse("student_home")))
    assert _banner(home) is None and _badge(home) == (1, 1)


def test_the_banner_stays_while_new_feedback_is_closed(world, settings):
    client, _ = _case_in(world, "NEEDS_INFORMATION")
    settings.TIMETABLE_FEEDBACK_ACCEPTING = False
    assert _banner(_html(_get(client, reverse("student_home")))) is not None


def test_the_banner_reads_in_arabic(world):
    client, case = _case_in(world, "NEEDS_INFORMATION")
    html = _html(_get(client, reverse("student_home"), "ar"))
    _assert_language(html, "ar")
    banner = _banner(html)
    assert copy.strings("ar")["banner_reply_title"] in banner
    assert str(copy.text("banner_reply_body", "ar", reference=copy.bdi(case.reference))) in banner
