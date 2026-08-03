"""The budget is shared, atomic, and belongs to the student.

The throttle this replaces kept its counters in a module-level dict, so with two
gunicorn workers every limit was quietly doubled and a restart erased it. These
tests are about the properties that fixes: one counter per student wherever the
request lands, retry drawing on the same allowance as asking, and a long window
that a short-window endpoint cannot sweep away.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AdvisorConversation, AdvisorMessage, RateLimitBucket, Student
from core.services import rate_limit
from core.services.rate_limit import (
    CONVERSATION,
    ESCALATION,
    FEEDBACK,
    GENERATION,
    HISTORY,
    consume,
    release,
)
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

MINE = 9001001
THEIRS = 9001002


def _student(client, student_id: int = MINE):
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": f"S{student_id}", "program": "CS", "section": "M"},
    )
    client.force_login(student_otp.provision_student_user(student_id))


def _post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def _fake_answer():
    return {
        "ok": True,
        "answer": "جواب.",
        "model": "stub",
        "citations": [],
        "cited_policy_ids": [],
        "agent": {"loop_used": True, "policy_grounding": "not_consulted"},
    }


# ── the counter itself ───────────────────────────────────────────


def test_the_budget_is_spent_down_and_then_refuses():
    limit, _window = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        assert consume(GENERATION, MINE).allowed
    refused = consume(GENERATION, MINE)
    assert refused.allowed is False
    assert refused.retry_after > 0


def test_one_students_spending_does_not_touch_another():
    limit, _ = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        consume(GENERATION, MINE)
    assert consume(GENERATION, MINE).allowed is False
    assert consume(GENERATION, THEIRS).allowed is True


def test_budgets_are_separate_from_each_other():
    """Exhausting answers must not cost the student their route to a human."""
    limit, _ = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        consume(GENERATION, MINE)
    assert consume(GENERATION, MINE).allowed is False
    for budget in (ESCALATION, FEEDBACK, HISTORY):
        assert consume(budget, MINE).allowed is True


def test_the_window_reopens_once_it_has_run_out():
    limit, window = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        consume(GENERATION, MINE)
    assert consume(GENERATION, MINE).allowed is False

    bucket = RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}")
    bucket.window_start = timezone.now() - timedelta(seconds=window + 1)
    bucket.save(update_fields=["window_start"])

    assert consume(GENERATION, MINE).allowed is True
    assert RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count == 1


def test_the_counter_lives_in_the_database_not_in_this_process():
    """The whole point: a second worker sees the same number.

    A process-local dict passes every assertion above and still doubles every
    limit in production, so the storage location is the property under test.
    """
    consume(GENERATION, MINE)
    assert RateLimitBucket.objects.filter(key=f"{GENERATION}:{MINE}", count=1).exists()


def test_the_row_is_claimed_under_a_lock_inside_a_transaction(monkeypatch):
    """Asserts the MECHANISM, because the effect is invisible here.

    `select_for_update` is what makes two workers take turns, and it is a no-op on
    SQLite — which is also the only backend these tests run on. So a behavioural
    assertion cannot distinguish a locked read from an unlocked one, and removing
    the lock passes every other test in this file while restoring the read-then-
    write race in production. This is the one test that goes red for it.
    """
    from django.db import transaction
    from django.db.models.query import QuerySet

    # An ORDERED log. Checking `connection.in_atomic_block` instead would be
    # vacuous: pytest-django already wraps each test in a transaction, so it reads
    # True whether or not `consume` opens one of its own.
    events: list[str] = []
    locked: list[bool] = []
    real_atomic = transaction.atomic
    real_get_or_create = QuerySet.get_or_create

    def spy_atomic(*args, **kwargs):
        events.append("atomic")
        return real_atomic(*args, **kwargs)

    def spy_read(self, *args, **kwargs):
        # Inspect the queryset that ACTUALLY reads the counter. Spying on
        # `select_for_update` itself only proves the method was called — a
        # careless extract-variable refactor that locks a throwaway queryset and
        # then reads an unlocked one passes that and loses the lock.
        events.append("read")
        locked.append(bool(self.query.select_for_update))
        return real_get_or_create(self, *args, **kwargs)

    monkeypatch.setattr(transaction, "atomic", spy_atomic)
    monkeypatch.setattr(QuerySet, "get_or_create", spy_read)
    consume(GENERATION, MINE)

    assert locked == [True], "the queryset that read the counter was not the locked one"
    assert "atomic" in events, "no transaction was opened, so the lock holds nothing"
    assert events.index("atomic") < events.index("read"), events


def test_the_allowance_cannot_be_spent_twice_across_a_window_boundary():
    """A plain reset hands the whole allowance back at the boundary.

    The student chooses when the window opens — it opens on their first request —
    and the 429 tells them exactly when it ends, so a fixed window lets them spend
    six at the end of one and six at the start of the next: eleven questions in two
    seconds against a limit that reads as six per ten minutes.
    """
    limit, window = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        assert consume(GENERATION, MINE).allowed
    assert consume(GENERATION, MINE).allowed is False

    # One second past the boundary.
    bucket = RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}")
    bucket.window_start = timezone.now() - timedelta(seconds=window + 1)
    bucket.save(update_fields=["window_start"])

    granted = sum(1 for _ in range(limit) if consume(GENERATION, MINE).allowed)
    assert granted <= 1, f"{granted} more granted immediately after the boundary"


def test_a_quiet_student_gets_their_full_allowance_back():
    """The positive control: carrying the previous window forward must not make
    the limit permanently half of itself."""
    limit, window = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        consume(GENERATION, MINE)

    # Two full windows of silence.
    RateLimitBucket.objects.filter(key=f"{GENERATION}:{MINE}").update(
        window_start=timezone.now() - timedelta(seconds=window * 2 + 1)
    )
    granted = sum(1 for _ in range(limit) if consume(GENERATION, MINE).allowed)
    assert granted == limit


def test_the_stated_wait_is_long_enough_to_be_worth_taking():
    """`retry_after > 0` accepts a constant 1, which tells the browser to hold its
    Send button for a second against a limiter that will refuse for ten minutes."""
    limit, window = rate_limit.LIMITS[GENERATION]
    for _ in range(limit):
        consume(GENERATION, MINE)
    refused = consume(GENERATION, MINE)
    assert refused.allowed is False
    assert window * 0.9 <= refused.retry_after <= window + 1, refused.retry_after


def test_housekeeping_removes_only_buckets_nobody_counts_against():
    consume(GENERATION, MINE)
    RateLimitBucket.objects.filter(key=f"{GENERATION}:{MINE}").update(
        window_start=timezone.now() - timedelta(days=3)
    )
    consume(GENERATION, THEIRS)
    assert rate_limit.purge_expired() == 1
    assert RateLimitBucket.objects.filter(key=f"{GENERATION}:{THEIRS}").exists()


def test_a_refund_returns_the_unit_and_never_goes_below_zero():
    consume(GENERATION, MINE)
    release(GENERATION, MINE)
    assert RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count == 0
    release(GENERATION, MINE)
    assert RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count == 0


def test_a_replayed_turn_costs_nothing_because_nothing_was_generated(client):
    """The client keeps the key until a send is known to have landed, so recovering
    from one dropped response used to cost as much as asking a new question."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "سؤال", "idempotency_key": "k-replay"}
    limit, _ = rate_limit.LIMITS[GENERATION]

    advisor = mock.Mock(return_value=_fake_answer())
    with mock.patch("core.services.virtual_advisor.answer_virtual_advisor", advisor):
        assert _post(client, url, body).status_code == 201
        for _ in range(limit * 2):
            assert _post(client, url, body).status_code == 200

    assert advisor.call_count == 1
    assert RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count == 1


def test_a_student_whose_record_is_missing_is_not_also_rate_limited(client):
    """Otherwise the 429 replaces the one message that says what is wrong."""
    _student(client)
    Student.objects.filter(student_id=MINE).delete()
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    limit, _ = rate_limit.LIMITS[GENERATION]

    llm = mock.Mock(side_effect=AssertionError("the model must never be reached"))
    with mock.patch("core.services.virtual_advisor.LocalLLMClient", llm):
        for _ in range(limit * 2):
            response = _post(client, url, {"message": "سؤال"})
            assert response.status_code == 409, response.content
            assert "عمادة القبول والتسجيل" in response.json()["error"]


# ── through the endpoints ────────────────────────────────────────


def test_asking_too_many_questions_is_refused_with_a_wait(client):
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    limit, _ = rate_limit.LIMITS[GENERATION]

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ):
        for i in range(limit):
            assert _post(client, url, {"message": f"سؤال {i}"}).status_code == 201
        refused = _post(client, url, {"message": "سؤال أخير"})

    assert refused.status_code == 429
    assert int(refused["Retry-After"]) > 0
    assert refused.json()["retry_after"] > 0


def test_retrying_draws_on_the_same_budget_as_asking(client):
    """Otherwise Retry is a route around the limit on asking.

    Today they share it only because retry reuses the send view — a fact no test
    held. Split that view and the allowance silently doubles.
    """
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    limit, _ = rate_limit.LIMITS[GENERATION]

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        assert _post(client, url, {"message": "سؤال", "idempotency_key": "k"}).status_code == 503

    # Every retry of that one failed turn spends from the same allowance.
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        for _ in range(limit - 1):
            _post(client, url, {"message": "سؤال", "idempotency_key": "k"})
        refused = _post(client, url, {"message": "سؤال", "idempotency_key": "k"})

    assert refused.status_code == 429


def test_creating_a_conversation_does_not_spend_the_budget_for_asking(client):
    """The client creates a conversation on its way to every question.

    Charging both doors made the real ceiling three questions per ten minutes
    against a limit that reads as six — and the refusal landed on the create call,
    where the client had no wait to show and reported a generic send failure.
    """
    _student(client)
    limit, _ = rate_limit.LIMITS[GENERATION]
    create_url = reverse("advisor_conversation_create")

    statuses = []
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ):
        for i in range(limit):
            created = _post(client, create_url, {})
            statuses.append(("create", created.status_code))
            conversation_id = created.json()["conversation"]["id"]
            sent = _post(
                client,
                reverse("advisor_conversation_send", args=[conversation_id]),
                {"message": f"سؤال {i}"},
            )
            statuses.append(("send", sent.status_code))

    assert all(code in (200, 201) for _kind, code in statuses), statuses
    assert sum(1 for kind, _ in statuses if kind == "send") == limit


def test_creating_conversations_is_still_bounded(client):
    """Loose is not unlimited: empty rows in the sidebar are still a cost."""
    _student(client)
    limit, _ = rate_limit.LIMITS[CONVERSATION]
    url = reverse("advisor_conversation_create")
    for _ in range(limit):
        assert _post(client, url, {}).status_code == 201
    assert _post(client, url, {}).status_code == 429


def test_reading_your_own_history_is_not_rationed_like_asking(client):
    """A student re-reading their conversation is not attacking anything."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_messages", args=[str(conversation.id)])
    generation_limit, _ = rate_limit.LIMITS[GENERATION]
    for _ in range(generation_limit * 4):
        assert client.get(url).status_code == 200


def test_a_runaway_script_reading_history_is_eventually_stopped(client):
    """Loose is not absent. Removing the history budget entirely passed every
    other assertion in this file."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    url = reverse("advisor_conversation_messages", args=[str(conversation.id)])
    limit, _ = rate_limit.LIMITS[HISTORY]
    for _ in range(limit):
        assert client.get(url).status_code == 200
    assert client.get(url).status_code == 429


def test_rating_answers_has_its_own_allowance(client):
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    url = reverse("advisor_message_feedback", args=[str(message.id)])
    limit, _ = rate_limit.LIMITS[FEEDBACK]
    for _ in range(limit):
        assert _post(client, url, {"rating": "HELPFUL"}).status_code == 200
    assert _post(client, url, {"rating": "HELPFUL"}).status_code == 429
