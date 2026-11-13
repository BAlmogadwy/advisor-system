"""PostgreSQL only: a student's actions on a case racing each other, or a new point.

Every action takes the case's row lock before it reads anything it decides on, so
an action that queued behind another decides on what that one left, and is refused
for what it finds. The races are forced and proven with the schema's helper: the
first writer holds its transaction until PostgreSQL shows the second blocked BY
it. The rate-limit budgets are switched off so the writers meet only at the case;
spending them is tested elsewhere. SQLite runs one writer at a time, so these skip
there and are required in the ``planner (PostgreSQL 16)`` job.
"""

from __future__ import annotations

import os

import pytest
from django.db import connection
from django.utils import timezone

from core.models import TimetableFeedback, TimetableFeedbackMessage, TimetableFeedbackPoint
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_workflow as workflow
from tests.test_timetable_feedback_postgres import _race
from tests.test_timetable_feedback_submission_postgres import (
    SID,
    TERM,
    YEAR,
    _fields_for,
    seed_one_section_student,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _postgresql_only():
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL feedback-workflow race tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row locks.")


@pytest.fixture(autouse=True)
def _accepting(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    settings.TIMETABLE_FEEDBACK_ACCEPTING = True
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = ()
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))
    monkeypatch.setattr(feedback, "_spend", lambda *_args: None)
    monkeypatch.setattr(workflow, "_spend", lambda *_args: None)


def _racer(action, **kwargs):
    """``action`` as one side of a race: what it returns, or ``("refused", code)``."""

    def run():
        try:
            return action(**kwargs)
        except (workflow.WorkflowRefused, feedback.FeedbackRefused) as refused:
            return ("refused", refused.code)

    return run


def _case(status, **fields):
    return TimetableFeedback.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, status=status, **fields
    )


def _point(case, section_id):
    return TimetableFeedbackPoint.objects.create(
        case=case,
        anchor_kind="SECTION",
        term_section_id=section_id,
        section_label="M1",
        anchor_key=f"SECTION|{section_id}",
        reason_code="TIME_CLASH",
        seen_fingerprint="a" * 64,
    )


def test_withdrawing_the_case_while_a_new_point_waits_starts_the_cool_down():
    """The submission saw the case open, so it skipped the early cap check and
    queued on the case's row lock. Once the withdrawal commits the case is no longer
    open, and the cap check under the lock refuses a fresh case."""
    anchor = seed_one_section_student()
    opened = feedback.submit_point(**_fields_for(anchor, "OTHER", note="opened the case"))

    first, second = _race(
        _racer(workflow.withdraw_case, student_id=SID, reference=opened.reference),
        _racer(feedback.submit_point, **_fields_for(anchor, "TIME_CLASH")),
    )

    assert first is None
    assert second == ("refused", "COOLING_DOWN")
    case = TimetableFeedback.objects.get()
    assert (case.reference, case.status) == (opened.reference, "WITHDRAWN")
    assert case.closed_at is not None
    assert list(TimetableFeedbackPoint.objects.values_list("reason_code", flat=True)) == ["OTHER"]
    assert case.events.filter(kind="WITHDRAWN").count() == 1


def test_accepting_and_reopening_an_answer_at_once_leaves_exactly_one_of_them():
    case = _case("ANSWERED", answered_at=timezone.now())

    first, second = _race(
        _racer(workflow.confirm_answer, student_id=SID, reference=case.reference, helpful=True),
        _racer(
            workflow.reopen_answer,
            student_id=SID,
            reference=case.reference,
            body="It still clashes.",
        ),
    )

    assert first is None
    # Refused for the closed case it read under the lock, not merely for a
    # version that had moved on underneath it.
    assert second == ("refused", "NOT_ALLOWED")
    case.refresh_from_db()
    assert (case.status, case.close_reason, case.answer_helpful, case.version) == (
        "CLOSED",
        "ANSWER_ACCEPTED",
        True,
        1,
    )
    assert list(case.events.values_list("kind", flat=True)) == ["ANSWER_CONFIRMED"]
    assert not TimetableFeedbackMessage.objects.exists()


def test_two_points_withdrawn_at_once_never_empty_the_case():
    """Each withdrawal on its own leaves a point; together they would leave none.
    The second counts the live points after the first has committed."""
    case = _case("SUBMITTED")
    kept_first, kept_second = _point(case, 11), _point(case, 12)

    first, second = _race(
        _racer(
            workflow.withdraw_point,
            student_id=SID,
            reference=case.reference,
            point_id=str(kept_first.pk),
        ),
        _racer(
            workflow.withdraw_point,
            student_id=SID,
            reference=case.reference,
            point_id=str(kept_second.pk),
        ),
    )

    assert first is None
    assert second == ("refused", "LAST_POINT")
    live = TimetableFeedbackPoint.objects.filter(case=case, withdrawn_at__isnull=True)
    assert list(live.values_list("pk", flat=True)) == [kept_second.pk]
    case.refresh_from_db()
    assert case.version == 1
    assert list(case.events.values_list("kind", "point_id")) == [("POINT_WITHDRAWN", kept_first.pk)]
