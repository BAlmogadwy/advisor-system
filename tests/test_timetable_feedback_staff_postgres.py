"""PostgreSQL only: staff claims racing each other and the student.

Every staff action locks the case through the person's scope, then refuses a case
whose version is not the one their page showed. A claim that queued behind another
therefore finds the version moved on and is refused as changed, never made on top
of the first. The races are forced and proven with the schema's helper: the first
writer holds its transaction until PostgreSQL shows the second blocked BY it. The
rate-limit budgets are switched off so the writers meet only at the case. SQLite
runs one writer at a time, so these skip there and are required in the
``planner (PostgreSQL 16)`` job.
"""

from __future__ import annotations

import os

import pytest
from django.db import connection
from django.utils import timezone

from core.models import TimetableFeedback, TimetableFeedbackMessage
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_workflow as workflow
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR
from tests.test_timetable_feedback_postgres import _race
from tests.test_timetable_feedback_staff import (
    ADV,
    ADV_B,
    PROG,
    S_MINE,
    S_OTHER_ADVISOR,
    case_for,
    make_students,
    staff_user,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _postgresql_only():
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL staff feedback race tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row locks.")


@pytest.fixture(autouse=True)
def _no_budgets(monkeypatch):
    monkeypatch.setattr(workflow, "_spend", lambda *_args: None)


@pytest.fixture
def people(_postgresql_only):
    make_students({S_MINE: (PROG, ADV), S_OTHER_ADVISOR: (PROG, ADV_B)})
    return {
        "advisor": staff_service.staff_of(staff_user("race-advisor", ROLE_ADVISOR, advisor_id=ADV)),
        "twin": staff_service.staff_of(staff_user("race-twin", ROLE_ADVISOR, advisor_id=ADV)),
        "department": staff_service.staff_of(
            staff_user("race-dept", ROLE_GENERAL_ADVISOR, departments=PROG)
        ),
        "other": staff_service.staff_of(staff_user("race-other", ROLE_ADVISOR, advisor_id=ADV_B)),
    }


def _racer(action, *args, **kwargs):
    """``action`` as one side of a race: what it returns, or ``("refused", code)``."""

    def run():
        try:
            return action(*args, **kwargs)
        except workflow.WorkflowRefused as refused:
            return ("refused", refused.code)

    return run


def test_two_people_taking_one_case_at_once_leave_it_with_one_of_them(people):
    """Both read version 0. The second waits on the row the first is claiming, and
    then finds the version moved on: refused, never taken on top."""
    case = case_for(S_MINE)

    first, second = _race(
        _racer(staff_service.take, people["advisor"], case.reference, version=0),
        _racer(staff_service.take, people["twin"], case.reference, version=0),
    )

    assert first.reference == case.reference
    assert second == ("refused", "CASE_CHANGED")
    case.refresh_from_db()
    assert (case.status, case.version, case.handled_by_username) == (
        "IN_REVIEW",
        1,
        "race-advisor",
    )
    assert list(case.events.values_list("kind", "actor_username")) == [("TAKEN", "race-advisor")]


def test_an_answer_written_while_the_student_added_a_message_is_not_sent_over_it(people):
    """The student's message moved the case on while the advisor wrote, so the
    answer is refused: the advisor reads the message before answering."""
    case = case_for(S_MINE, "IN_REVIEW")

    first, second = _race(
        _racer(workflow.follow_up, student_id=S_MINE, reference=case.reference, body="One more."),
        _racer(
            staff_service.answer, people["advisor"], case.reference, version=0, body="Answered."
        ),
    )

    assert isinstance(first, TimetableFeedbackMessage)
    assert second == ("refused", "CASE_CHANGED")
    case.refresh_from_db()
    assert (case.status, case.version, case.answered_at) == ("IN_REVIEW", 1, None)
    assert list(case.messages.values_list("author_kind", "body")) == [("STUDENT", "One more.")]


def test_a_withdrawal_while_staff_answer_leaves_the_case_withdrawn_and_unanswered(people):
    case = case_for(S_MINE)

    first, second = _race(
        _racer(workflow.withdraw_case, student_id=S_MINE, reference=case.reference),
        _racer(staff_service.answer, people["department"], case.reference, version=0, body="Late."),
    )

    assert first is None
    assert second == ("refused", "CASE_CHANGED")
    case.refresh_from_db()
    assert (case.status, case.last_student_visible_at) == ("WITHDRAWN", None)
    assert not case.messages.exists()


def test_assigning_while_an_advisor_takes_the_case_lets_only_the_first_stand(people):
    """The department read the case before the advisor took it; its assignment,
    which would also clear who handles it, is refused rather than undoing the take."""
    case = case_for(S_MINE)

    first, second = _race(
        _racer(staff_service.take, people["advisor"], case.reference, version=0),
        _racer(
            staff_service.assign, people["department"], case.reference, version=0, advisor_id=ADV_B
        ),
    )

    assert first.reference == case.reference
    assert second == ("refused", "CASE_CHANGED")
    case.refresh_from_db()
    assert (case.handled_by_username, case.assigned_advisor_id) == ("race-advisor", "")


def test_a_bulk_answer_skips_a_case_answered_by_hand_meanwhile(people):
    """The bulk action claims each case on its own, so the one a colleague answered
    while it waited is listed as changed and keeps that colleague's answer."""
    contested, free = case_for(S_MINE), case_for(S_MINE)
    picks = [(contested.reference, "0"), (free.reference, "0")]

    first, second = _race(
        _racer(staff_service.answer, people["twin"], contested.reference, version=0, body="Mine."),
        _racer(staff_service.bulk_answer, people["advisor"], picks, template="NOTED_FOR_PLANNING"),
    )

    assert isinstance(first, TimetableFeedbackMessage)
    assert [(o.reference, o.refused) for o in second] == [
        (contested.reference, "CASE_CHANGED"),
        (free.reference, ""),
    ]
    assert list(contested.messages.values_list("body", flat=True)) == ["Mine."]
    assert TimetableFeedback.objects.get(pk=free.pk).status == "ANSWERED"
    contested.refresh_from_db()
    assert contested.answered_at is not None and contested.answered_at <= timezone.now()
