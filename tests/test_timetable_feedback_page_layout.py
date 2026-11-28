"""What the feedback pages put first, and the marks that say where a case stands.

The student's progress line and the order of their case page; the status pill on
their list; on the staff side, a case untouched for over two days marked in the
queue, the composer's forms, and the rarer moves behind disclosures that open
when one of them was refused. The browser behaviour (tabs, the bulk bar) is in
test_timetable_feedback_staff_browser.py.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone
from django.utils.html import escape

from core.models import TimetableFeedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff_copy as scopy
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR
from core.student_timetable_feedback_views import _progress
from tests.test_timetable_feedback_staff import staff_user
from tests.test_timetable_feedback_student_views import (
    LIST,
    PROG,
    SID,
    _client,
    _detail_url,
    _get,
    _html,
    _send,
    make_world,
    set_flags,
)

pytestmark = pytest.mark.django_db

ADV = "ADV-7"  # make_world's advisor id for every student
QUEUE = "/ops/timetable-feedback/"


@pytest.fixture(autouse=True)
def _flags(settings, monkeypatch):
    set_flags(settings, monkeypatch)


@pytest.fixture
def world(db):
    return make_world()


def _sent(world, **changes):
    student = _client()
    _send(student, world, note="It clashes with my lab.")
    case = TimetableFeedback.objects.get(student_id=SID)
    if changes:
        TimetableFeedback.objects.filter(pk=case.pk).update(**changes)
        case.refresh_from_db()
    return student, case


def _steps(status, language="en"):
    return [
        (step["label"], step["done"], step["current"], step["waiting"])
        for step in _progress(status, language)
    ]


# ---------------------------------------------------------------------------
# The student's case
# ---------------------------------------------------------------------------


def _label(code, language="en"):
    return copy.status_label(code, language)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "SUBMITTED",
            [
                ("SUBMITTED", False, True),
                ("IN_REVIEW", False, False),
                ("ANSWERED", False, False),
                ("CLOSED", False, False),
            ],
        ),
        (
            "IN_REVIEW",
            [
                ("SUBMITTED", True, False),
                ("IN_REVIEW", False, True),
                ("ANSWERED", False, False),
                ("CLOSED", False, False),
            ],
        ),
        # The middle step says more than "being reviewed" when there is more to say.
        (
            "NEEDS_INFORMATION",
            [
                ("SUBMITTED", True, False),
                ("NEEDS_INFORMATION", False, True),
                ("ANSWERED", False, False),
                ("CLOSED", False, False),
            ],
        ),
        (
            "FORWARDED",
            [
                ("SUBMITTED", True, False),
                ("FORWARDED", False, True),
                ("ANSWERED", False, False),
                ("CLOSED", False, False),
            ],
        ),
        (
            "ANSWERED",
            [
                ("SUBMITTED", True, False),
                ("IN_REVIEW", True, False),
                ("ANSWERED", False, True),
                ("CLOSED", False, False),
            ],
        ),
        # Closed is every step done, none still to come.
        (
            "CLOSED",
            [
                ("SUBMITTED", True, False),
                ("IN_REVIEW", True, False),
                ("ANSWERED", True, False),
                ("CLOSED", True, False),
            ],
        ),
    ],
)
def test_the_progress_line_marks_where_a_case_has_got_to(status, expected):
    steps = _steps(status)
    assert [(label, done, current) for label, done, current, _ in steps] == [
        (_label(code), done, current) for code, done, current in expected
    ]
    # Waiting on the student is its own mark, on the step it is at.
    assert [waiting for *_, waiting in steps] == [
        status == "NEEDS_INFORMATION" and current for _code, _done, current in expected
    ]


def test_a_withdrawn_case_has_no_progress_line():
    assert _progress("WITHDRAWN", "en") == []


def test_the_progress_line_reads_in_arabic():
    assert [step["label"] for step in _progress("IN_REVIEW", "ar")] == [
        _label(code, "ar") for code in ("SUBMITTED", "IN_REVIEW", "ANSWERED", "CLOSED")
    ]


def test_the_case_page_draws_the_line_and_puts_the_conversation_first(world):
    student, case = _sent(world, status="NEEDS_INFORMATION")
    html = _html(_get(student, _detail_url(case.reference)))
    line = html.split('id="ttfProgress"')[1].split("</ol>")[0]
    assert f'aria-label="{escape(copy.strings("en")["detail_progress"])}"' in html
    # One step is current, and it is the one the case is at.
    assert line.count('aria-current="step"') == 1
    current = re.search(r'aria-current="step">.*?ttf-step-label">([^<]+)<', line).group(1)
    assert current == _label("NEEDS_INFORMATION")
    assert "is-waiting" in line
    # The conversation, where the reply goes, before what was sent.
    assert html.index('id="ttfThreadTitle"') < html.index('id="ttfPointsTitle"')


def test_the_answer_sits_after_the_conversation_and_before_what_was_sent(world):
    student, case = _sent(world, status="ANSWERED", answered_at=timezone.now())
    html = _html(_get(student, _detail_url(case.reference)))
    thread, answer, points = (
        html.index('id="ttfThreadTitle"'),
        html.index('id="ttfAnswer"'),
        html.index('id="ttfPointsTitle"'),
    )
    assert thread < answer < points


def test_the_list_shows_each_case_status_as_a_pill(world):
    student, case = _sent(world)
    html = _html(_get(student, LIST))
    assert (
        f'<span class="ttf-status" data-status="{case.status}">{_label(case.status)}</span>' in html
    )


# ---------------------------------------------------------------------------
# The staff side
# ---------------------------------------------------------------------------


@pytest.fixture
def advisor(db):
    return staff_user("layout-adv", ROLE_ADVISOR, advisor_id=ADV)


def _as(user):
    client = Client()
    client.force_login(user)
    return client


def _row(html, reference):
    for row in re.findall(r"<tr>(.*?)</tr>", html, flags=re.S):
        if f"/ops/timetable-feedback/{reference}/" in row:
            return row
    raise AssertionError(f"{reference} is not in the queue")


UNTOUCHED = (
    f'<span class="ttf-tag ttfs-untouched">{escape(scopy.strings("en")["kpi_untouched"])}</span>'
)


def test_a_case_nobody_touched_for_over_two_days_is_marked_in_the_queue(world, advisor):
    _student, case = _sent(world)
    old = timezone.now() - timedelta(days=3)
    client = _as(advisor)

    TimetableFeedback.objects.filter(pk=case.pk).update(created_at=old)
    assert UNTOUCHED in _row(_html(client.get(QUEUE, {"tab": "all"})), case.reference)
    # Acted on: no longer untouched, however old.
    TimetableFeedback.objects.filter(pk=case.pk).update(first_staff_action_at=timezone.now())
    assert UNTOUCHED not in _row(_html(client.get(QUEUE, {"tab": "all"})), case.reference)
    # Not yet two days old: not marked.
    TimetableFeedback.objects.filter(pk=case.pk).update(
        first_staff_action_at=None, created_at=timezone.now() - timedelta(days=1)
    )
    assert UNTOUCHED not in _row(_html(client.get(QUEUE, {"tab": "all"})), case.reference)
    # Waiting on the student, not on staff: not marked either.
    TimetableFeedback.objects.filter(pk=case.pk).update(created_at=old, status="NEEDS_INFORMATION")
    assert UNTOUCHED not in _row(_html(client.get(QUEUE, {"tab": "all"})), case.reference)


def test_the_queue_scrolls_with_the_page_and_offers_the_bulk_count(world, advisor):
    _sent(world)
    html = _html(_as(advisor).get(QUEUE, {"tab": "all"}))
    assert 'class="table-wrap ttfs-queue-wrap"' in html
    assert 'id="ttfsBulkCount" hidden' in html


def _case_page(client, case, **data):
    url = f"/ops/timetable-feedback/{case.reference}/"
    if data:
        return _html(client.post(f"{url}action/", {"version": case.version, **data}))
    return _html(client.get(url))


def _details_of(html, form_id):
    """The <details> opening tag around the form ``form_id``."""
    before = html[: html.index(f'id="{form_id}"')]
    return before[before.rindex("<details") :].split(">")[0]


def test_writing_is_a_composer_and_the_rarer_moves_are_disclosures(world, advisor):
    _student, case = _sent(world)
    html = _case_page(_as(advisor), case)
    for form in ("ttfsAnswerForm", "ttfsAskForm"):
        assert re.search(rf'class="[^"]*ttfs-compose[^"]*" id="{form}"', html), form
    assert 'data-composer-label="' in html
    # Forward and close sit behind their own disclosures, shut until needed.
    assert 'id="ttfsMore"' in html
    for form in ("ttfsForwardForm", "ttfsCloseForm"):
        assert " open" not in _details_of(html, form), form


def test_a_refused_move_reopens_its_disclosure(world, advisor):
    _student, case = _sent(world)
    client = _as(advisor)
    html = _case_page(client, case, action="close", reason="", body="")
    assert " open" in _details_of(html, "ttfsCloseForm")
    assert " open" not in _details_of(html, "ttfsForwardForm")
    assert 'id="ttfsErrors"' in html


@pytest.mark.parametrize(
    ("action", "form", "fields"),
    [
        ("forward", "ttfsForwardForm", {"body": "The department should see this."}),
        ("close", "ttfsCloseForm", {"reason": "DUPLICATE", "body": "Same as before."}),
    ],
)
def test_a_stale_version_reopens_the_refused_moves_disclosure(world, advisor, action, form, fields):
    """CASE_CHANGED is no field's error, yet the form that was sent must be open,
    with what was typed in it."""
    _student, case = _sent(world)
    client = _as(advisor)
    url = f"/ops/timetable-feedback/{case.reference}/action/"
    html = _html(client.post(url, {"action": action, "version": case.version + 7, **fields}))
    assert 'id="ttfsErrors"' in html
    assert " open" in _details_of(html, form)
    others = {"ttfsForwardForm", "ttfsCloseForm"} - {form}
    for other in others:
        assert " open" not in _details_of(html, other), other
    assert escape(fields["body"]) in html


def test_a_stale_assignment_reopens_its_disclosure_with_the_typed_id(world, advisor):
    """To an advisor who exists, so the refusal is the stale version itself, no
    field's error: only the refused action can open the disclosure."""
    department = staff_user("layout-dept", ROLE_GENERAL_ADVISOR, departments=PROG)
    _student, case = _sent(world)
    client = _as(department)
    html = _case_page(client, case)
    assert 'id="ttfsAssignForm"' in html
    assert " open" not in _details_of(html, "ttfsAssignForm")
    url = f"/ops/timetable-feedback/{case.reference}/action/"
    html = _html(
        client.post(url, {"action": "assign", "version": case.version + 7, "advisor_id": ADV})
    )
    assert str(scopy.refusal("CASE_CHANGED", "en")) in html
    assert " open" in _details_of(html, "ttfsAssignForm")
    assert f'value="{ADV}"' in html


def test_a_refused_question_comes_back_in_its_own_form_for_the_composer_to_open(world, advisor):
    """A stale question keeps its text in the question's form: what the composer
    opens on (an empty one is a field's error, which it opens on too)."""
    _student, case = _sent(world)
    url = f"/ops/timetable-feedback/{case.reference}/action/"
    html = _html(
        _as(advisor).post(url, {"action": "ask", "version": case.version + 7, "body": "Which day?"})
    )
    form = html.split('id="ttfsAskForm"')[1].split("</form>")[0]
    assert "Which day?" in form
    assert "Which day?" not in html.split('id="ttfsAnswerForm"')[1].split("</form>")[0]


def test_each_step_says_its_state_and_the_line_stays_a_list(world):
    student, case = _sent(world, status="IN_REVIEW")
    html = _html(_get(student, _detail_url(case.reference)))
    line = html.split('id="ttfProgress"')[1].split("</ol>")[0]
    words = copy.strings("en")
    hidden = re.findall(r'<span class="visually-hidden">([^<]*)</span>', line)
    separator = words["list_separator"]
    assert hidden == [
        separator + words["detail_step_done"],
        separator + words["detail_step_current"],
        separator + words["detail_step_next"],
        separator + words["detail_step_next"],
    ]
    assert 'id="ttfProgress" role="list"' in html


def test_the_case_page_lays_the_side_out_before_the_work(world, advisor):
    _student, case = _sent(world)
    html = _case_page(_as(advisor), case)
    # Who and what first in the page, then the work on it.
    assert html.index('class="ttfs-case-side"') < html.index('class="ttfs-case-main"')
    assert html.index('id="ttfsFactsTitle"') < html.index('id="ttfsPointsTitle"')
    assert html.index('id="ttfsThreadTitle"') < html.index('id="ttfsActionsTitle"')
