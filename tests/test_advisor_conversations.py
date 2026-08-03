"""Durable conversations: who may read them, and does the browser see what was stored.

The security tests come first because they protect everything after them. A
persistence bug loses a message; an ownership bug shows one student another
student's academic questions, which is a different category of wrong.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorFeedback,
    AdvisorMessage,
    AdvisorMessageCitation,
    Student,
)
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

MINE = 6001001
THEIRS = 6001002


def _student(client, student_id: int) -> User:
    """A signed-in student whose scope carries their own id.

    Uses the production provisioning helper rather than constructing a UserScope
    by hand: a test that builds its own session can pass while the real login
    path produces a differently-shaped scope.
    """
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id, defaults={"name": f"S{student_id}", "program": "CS", "section": "M"}
    )
    user = student_otp.provision_student_user(student_id)
    client.force_login(user)
    return user


def _post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def _conversation(student_id: int, **kw) -> AdvisorConversation:
    return AdvisorConversation.objects.create(student_id=student_id, **kw)


def _fake_answer(answer="باقي لك ٣ مواد.", citations=None, agent=None):
    return {
        "ok": True,
        "answer": answer,
        "model": "fake-model",
        "citations": citations or [],
        "cited_policy_ids": [],
        "agent": {"loop_used": True, "policy_grounding": "not_consulted", **(agent or {})},
    }


# ── 1-4. identity and ownership ──────────────────────────────────


def test_session_identity_overrides_any_payload_identity(client):
    """A request that names a student id is describing what it wants, not who it is."""
    _student(client, MINE)
    response = _post(
        client,
        reverse("advisor_conversation_create"),
        {"title": "t", "student_id": THEIRS},
    )
    assert response.status_code == 201
    created = AdvisorConversation.objects.get(id=response.json()["conversation"]["id"])
    assert created.student_id == MINE


def test_a_conversation_belonging_to_another_student_is_not_found(client):
    """404, not 403: a refusal would confirm the conversation exists."""
    theirs = _conversation(THEIRS)
    _student(client, MINE)
    url = reverse("advisor_conversation_messages", args=[str(theirs.id)])
    assert client.get(url).status_code == 404


def test_posting_into_another_students_conversation_is_not_found(client):
    theirs = _conversation(THEIRS)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(theirs.id)])
    assert _post(client, url, {"message": "سؤال"}).status_code == 404


def test_rating_a_message_in_another_students_conversation_is_not_found(client):
    theirs = _conversation(THEIRS)
    message = AdvisorMessage.objects.create(
        conversation=theirs, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    _student(client, MINE)
    url = reverse("advisor_message_feedback", args=[str(message.id)])
    assert _post(client, url, {"rating": "HELPFUL"}).status_code == 404
    assert AdvisorFeedback.objects.count() == 0


def test_the_list_contains_only_the_students_own_conversations(client):
    _conversation(THEIRS, title="theirs")
    mine = _conversation(MINE, title="mine")
    _student(client, MINE)
    data = client.get(reverse("advisor_conversation_list")).json()
    assert [c["id"] for c in data["conversations"]] == [str(mine.id)]


def test_a_malformed_conversation_id_is_not_found_rather_than_a_server_error(client):
    _student(client, MINE)
    url = reverse("advisor_conversation_messages", args=["not-a-uuid"])
    assert client.get(url).status_code == 404


def test_a_signed_out_visitor_is_redirected_not_served(client):
    assert client.get(reverse("advisor_conversation_list")).status_code in {302, 403}


# ── 5-6. idempotency ─────────────────────────────────────────────


def test_the_same_key_and_question_replays_instead_of_asking_again(client):
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "وش باقي لي وأتخرج؟", "idempotency_key": "k1"}

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ) as agent:
        first = _post(client, url, body)
        second = _post(client, url, body)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert agent.call_count == 1, "a retry must not generate a second answer"
    assert conversation.messages.filter(role=AdvisorMessage.ROLE_STUDENT).count() == 1


def test_the_same_key_with_a_different_question_is_refused(client):
    """Answering it would attach one question's answer to another's key."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ):
        _post(client, url, {"message": "سؤال أول", "idempotency_key": "k1"})
        clash = _post(client, url, {"message": "سؤال مختلف", "idempotency_key": "k1"})

    assert clash.status_code == 409


def test_the_same_key_in_a_different_conversation_is_allowed(client):
    first = _conversation(MINE)
    second = _conversation(MINE)
    _student(client, MINE)
    body = {"message": "سؤال", "idempotency_key": "shared"}
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ):
        a = _post(client, reverse("advisor_conversation_send", args=[str(first.id)]), body)
        b = _post(client, reverse("advisor_conversation_send", args=[str(second.id)]), body)
    assert a.status_code == 201 and b.status_code == 201


# ── 7. reload ────────────────────────────────────────────────────


def test_messages_survive_a_reload_in_order(client):
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=[_fake_answer("جواب ١"), _fake_answer("جواب ٢")],
    ):
        _post(client, url, {"message": "سؤال ١"})
        _post(client, url, {"message": "سؤال ٢"})

    data = client.get(reverse("advisor_conversation_messages", args=[str(conversation.id)])).json()
    assert [m["content"] for m in data["messages"]] == ["سؤال ١", "جواب ١", "سؤال ٢", "جواب ٢"]


def test_a_conversation_gets_a_title_from_the_first_question(client):
    conversation = _conversation(MINE)
    _student(client, MINE)
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor", return_value=_fake_answer()
    ):
        _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "وش باقي لي وأتخرج؟"},
        )
    conversation.refresh_from_db()
    assert conversation.title
    assert conversation.last_message_at is not None


# ── 8-10. citations ──────────────────────────────────────────────

_CITATION = {
    "policy_id": "TU.WITHDRAWAL.MAXIMUM",
    "document_id": "TU_STUDENT_GUIDE_V3_1447",
    "document_title": "الدليل الإرشادي للطالب والطالبة",
    "edition": "1447",
    "page": 24,
    "effective_from": None,
    "effective_to": None,
}


def test_the_returned_citation_equals_the_stored_snapshot(client):
    """Rendering from one object while persisting another is how provenance drifts."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    answer = "خمسة انسحابات «الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»"
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(answer, citations=[_CITATION]),
    ):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "كم مرة أنسحب؟"},
        )

    returned = response.json()["assistant_message"]["citations"]
    stored = AdvisorMessageCitation.objects.get()
    assert returned == [
        {
            "policy_id": stored.policy_id,
            "document_title": stored.document_title,
            "edition": stored.edition,
            "page": stored.page,
            "effective_from": stored.effective_from or None,
            "effective_to": stored.effective_to or None,
        }
    ]
    assert stored.page == "24"
    assert stored.source_version_hash


def test_only_citations_the_answer_actually_made_are_stored(client):
    """Storing everything RETRIEVED would attach authority to records the answer
    never used, which reads to a student as "these support this"."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    other = {**_CITATION, "policy_id": "TU.WITHDRAWAL.PROCEDURE", "page": 24}
    answer = "خمسة انسحابات «الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»"
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(answer, citations=[_CITATION, other]),
    ):
        _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "كم مرة أنسحب؟"},
        )
    assert [c.policy_id for c in AdvisorMessageCitation.objects.all()] == ["TU.WITHDRAWAL.MAXIMUM"]


def test_a_citation_the_request_was_not_entitled_to_is_never_stored(client):
    """The answer names a policy that was not retrieved. It must not gain a snapshot."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    answer = "حسب «الدليل الإرشادي للطالب، ص 25 [TU.DISMISSAL.THREE_WARNINGS]»"
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(answer, citations=[_CITATION]),
    ):
        _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    assert AdvisorMessageCitation.objects.count() == 0


def test_a_student_data_answer_persists_with_no_citations(client):
    conversation = _conversation(MINE)
    _student(client, MINE)
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("لديك 3 مواد متبقية."),
    ):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "كم مادة باقي لي؟"},
        )
    assert response.status_code == 201
    assert response.json()["assistant_message"]["citations"] == []
    assert AdvisorMessageCitation.objects.count() == 0


# ── 11. failure ──────────────────────────────────────────────────


def test_a_generation_failure_saves_the_question_and_reports_it(client):
    """The student's question must not vanish, and must not silently duplicate."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    assert response.status_code == 503
    assert response.json()["assistant_message"] is None
    saved = AdvisorMessage.objects.get()
    assert saved.role == AdvisorMessage.ROLE_STUDENT
    assert saved.status == AdvisorMessage.STATUS_FAILED


def test_retrying_a_failed_turn_generates_the_answer_it_never_got(client):
    """Idempotency must not turn a failure into a permanent one.

    The key exists so a retry cannot produce a SECOND answer. Applied to a turn
    that failed, it replayed the failure instead — returning the saved question
    with no answer, forever, however many times the student pressed Retry.
    """
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "سؤال", "idempotency_key": "k-1"}

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        assert _post(client, url, body).status_code == 503

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("باقي لك ٣ مواد."),
    ):
        retried = _post(client, url, body)

    assert retried.status_code == 201
    assert retried.json()["assistant_message"]["content"] == "باقي لك ٣ مواد."
    # Resumed, not duplicated: still one question, now marked done.
    student_messages = AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_STUDENT)
    assert student_messages.count() == 1
    assert student_messages.get().status == AdvisorMessage.STATUS_COMPLETED


def test_a_succeeded_turn_is_still_replayed_rather_than_regenerated(client):
    """The guard above must not reopen turns that already have an answer."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "سؤال", "idempotency_key": "k-2"}

    advisor = mock.Mock(return_value=_fake_answer("الأولى."))
    with mock.patch("core.services.virtual_advisor.answer_virtual_advisor", advisor):
        assert _post(client, url, body).status_code == 201
        replay = _post(client, url, body)

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert advisor.call_count == 1
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1


def test_an_abandoned_pending_turn_becomes_answerable_again(client):
    """PENDING is only trustworthy while something is still working on the turn.

    A killed worker or a deploy restart leaves it set forever, and treating that as
    permanently in-flight rebuilds — one state over — the same trap that made a
    failed question unanswerable.
    """
    from django.utils import timezone

    from core.advisor_conversation_views import STALE_GENERATION

    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "سؤال", "idempotency_key": "k-stale"}

    stranded = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال",
        idempotency_key="k-stale",
        request_hash=hashlib.sha256("سؤال".encode()).hexdigest(),
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now() - STALE_GENERATION - timedelta(minutes=1),
    )

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("أخيرًا."),
    ):
        response = _post(client, url, body)

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "أخيرًا."
    stranded.refresh_from_db()
    assert stranded.status == AdvisorMessage.STATUS_COMPLETED


def test_a_turn_still_generating_is_not_started_a_second_time(client):
    """The counterpart: a genuinely in-flight turn must be left alone."""
    from django.utils import timezone

    conversation = _conversation(MINE)
    _student(client, MINE)
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال",
        idempotency_key="k-live",
        request_hash=hashlib.sha256("سؤال".encode()).hexdigest(),
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now(),
    )
    advisor = mock.Mock(return_value=_fake_answer())
    with mock.patch("core.services.virtual_advisor.answer_virtual_advisor", advisor):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال", "idempotency_key": "k-live"},
        )
    assert response.status_code == 200
    assert advisor.call_count == 0


def test_replay_returns_the_answer_to_that_question_not_a_later_one(client):
    """Pairing by "first assistant row at or after the question" breaks whenever
    turns finish out of order — a resumed retry is written AFTER answers to later
    questions, so the student is handed a cited answer to something else."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        _post(client, url, {"message": "السؤال الأول", "idempotency_key": "k-a"})

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("جواب الثاني"),
    ):
        _post(client, url, {"message": "السؤال الثاني", "idempotency_key": "k-b"})

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("جواب الأول"),
    ):
        resumed = _post(client, url, {"message": "السؤال الأول", "idempotency_key": "k-a"})
    assert resumed.json()["assistant_message"]["content"] == "جواب الأول"

    replayed = _post(client, url, {"message": "السؤال الأول", "idempotency_key": "k-a"})
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["assistant_message"]["content"] == "جواب الأول"


def test_a_multi_page_policy_cites_one_page_a_student_can_turn_to(client):
    """A policy spanning pages stores its page as a LIST, which rendered as
    "p. [24, 25]" and, on PostgreSQL, overflowed the column mid-transaction."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    citations = [
        {
            "policy_id": "TU.WITHDRAWAL.MAXIMUM",
            "document_id": "TU.GUIDE",
            "document_title": "الدليل",
            "edition": "1447",
            "page": [24, 25, 26],
            "effective_from": "",
            "effective_to": "",
        }
    ]
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(
            "خمسة «الدليل، ص 24 [TU.WITHDRAWAL.MAXIMUM]».", citations=citations
        ),
    ):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    assert response.json()["assistant_message"]["citations"][0]["page"] == "24"
    assert AdvisorMessageCitation.objects.get().page == "24"


def test_an_abandoned_turn_offers_the_student_a_way_out(client):
    """A turn stuck on PENDING shows "preparing the answer" for ever.

    Without a token there is no Retry button on it, so a worker killed
    mid-generation costs the student the question with no sign anything is wrong.
    """
    from django.utils import timezone

    from core.advisor_conversation_views import STALE_GENERATION

    conversation = _conversation(MINE)
    _student(client, MINE)
    fresh = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال جديد",
        idempotency_key="k-fresh",
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now(),
    )
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال مهجور",
        idempotency_key="k-gone",
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now() - STALE_GENERATION - timedelta(minutes=1),
    )

    messages = client.get(
        reverse("advisor_conversation_messages", args=[str(conversation.id)])
    ).json()["messages"]
    by_content = {m["content"]: m for m in messages}
    # Still working: no escape hatch, or the student interrupts a live answer.
    assert "retry_token" not in by_content["سؤال جديد"]
    assert by_content["سؤال مهجور"]["retry_token"] == "k-gone"
    assert fresh.status == AdvisorMessage.STATUS_PENDING


def test_only_one_of_two_simultaneous_retries_may_claim_the_turn(client):
    """The race a double-clicked Retry actually runs.

    Reading the status and then writing it are two statements with a gap between
    them, and both clicks fit inside that gap: both see FAILED, both claim it, both
    call the model, and the student gets two different answers to one question. The
    two in-memory copies below are exactly what two concurrent requests hold.
    """
    from core.advisor_conversation_views import _resume_or_replay

    conversation = _conversation(MINE)
    _student(client, MINE)
    request_hash = hashlib.sha256("سؤال".encode()).hexdigest()
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال",
        idempotency_key="k-race",
        request_hash=request_hash,
        status=AdvisorMessage.STATUS_FAILED,
    )
    # Two separate reads, both taken before either writes — the interleaving.
    first = AdvisorMessage.objects.get(idempotency_key="k-race")
    second = AdvisorMessage.objects.get(idempotency_key="k-race")
    assert first.status == second.status == AdvisorMessage.STATUS_FAILED

    won, response = _resume_or_replay(conversation, first, MINE, request_hash)
    assert won is not None and response is None

    lost, response = _resume_or_replay(conversation, second, MINE, request_hash)
    assert lost is None, "both requests claimed the same turn and will both generate"
    assert response.status_code == 200


def test_an_integrity_error_with_no_key_is_not_treated_as_a_retry(client):
    """The unique constraint only covers non-empty keys, so a keyless send can
    never collide on it. Recovering as if it had matched the conversation's oldest
    keyless turn and served its stored answer as this question's."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("جواب قديم"),
    ):
        _post(client, url, {"message": "سؤال"})

    from django.db import IntegrityError

    with mock.patch.object(
        AdvisorMessage.objects, "create", side_effect=IntegrityError("some other constraint")
    ):
        with pytest.raises(IntegrityError):
            _post(client, url, {"message": "سؤال"})

    # And nothing was invented: still exactly the one original turn.
    assert conversation.messages.filter(role=AdvisorMessage.ROLE_STUDENT).count() == 1


def test_a_conversation_with_no_activity_sorts_last_not_first(client):
    """`-last_message_at` alone means different things on SQLite and PostgreSQL.

    Descending order puts NULLs last on SQLite and first on PostgreSQL, so a
    conversation whose first send failed would sit at the bottom in development
    and at the top in production — and the screen opens whatever is at the top.
    """
    from django.utils import timezone

    _student(client, MINE)
    active = _conversation(MINE, title="active", last_message_at=timezone.now())
    idle = _conversation(MINE, title="idle")

    response = client.get(reverse("advisor_conversation_list"))
    order = [c["id"] for c in response.json()["conversations"]]
    assert order == [str(active.id), str(idle.id)]

    # That assertion alone is vacuous on SQLite, which already sorts NULLs last on
    # a descending column — it would pass just as happily with the bug present, and
    # only PostgreSQL would disagree. So assert the instruction was actually
    # emitted, which is true on every backend or on none.
    sql = str(AdvisorConversation.objects.all().query)
    assert "NULLS LAST" in sql, sql


def test_a_refused_answer_is_stored_as_abstained(client):
    conversation = _conversation(MINE)
    _student(client, MINE)
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer("لم أتمكن من التحقق.", agent={"citation_refused": True}),
    ):
        _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    assistant = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT)
    assert assistant.status == AdvisorMessage.STATUS_ABSTAINED
    assert assistant.final_disposition == "ABSTAIN"


# ── 12. nothing internal escapes ─────────────────────────────────


def test_internal_traces_never_reach_the_browser(client):
    """Tool results and judge reasoning name database tables and quote cohort
    statistics. They belong in an operator record, not in front of a student."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    leaky = _fake_answer()
    leaky["agent"]["tool_results"] = [
        {"tool": "get_student_context", "note": "StudentCourse.grade empty on 159,778 rows"}
    ]
    leaky["verified_context"] = {"student": {"gpa": 2.76}}
    leaky["tool_results"] = leaky["agent"]["tool_results"]

    with mock.patch("core.services.virtual_advisor.answer_virtual_advisor", return_value=leaky):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    body = response.content.decode()
    for leak in ("tool_results", "verified_context", "159,778", "StudentCourse"):
        assert leak not in body, leak


def test_the_browser_receives_exactly_these_fields_and_no_others(client):
    """An allowlist, because a denylist only catches the leaks already imagined.

    Adding `grounding_state` or `model_name` to the serialiser passes every
    string-matching test in this file while shipping the system's account of its
    own reasoning to the person who asked the question.
    """
    conversation = _conversation(MINE)
    _student(client, MINE)
    citations = [
        {
            "policy_id": "TU.WITHDRAWAL.MAXIMUM",
            "document_id": "TU.GUIDE",
            "document_title": "الدليل",
            "edition": "1447",
            "page": 24,
            "effective_from": "1447",
            "effective_to": "",
        }
    ]
    answer = "خمسة «الدليل، ص 24 [TU.WITHDRAWAL.MAXIMUM]»."
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(answer, citations=citations),
    ):
        _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )

    payload = client.get(
        reverse("advisor_conversation_messages", args=[str(conversation.id)])
    ).json()
    assert set(payload) == {"conversation", "messages"}
    assert set(payload["conversation"]) == {
        "id",
        "title",
        "status",
        "created_at",
        "updated_at",
        "last_message_at",
    }

    student_message, assistant = payload["messages"]
    assert set(student_message) == {"id", "role", "content", "status", "created_at"}
    assert set(assistant) == {
        "id",
        "role",
        "content",
        "status",
        "created_at",
        "citations",
    }
    assert set(assistant["citations"][0]) == {
        "policy_id",
        "document_title",
        "edition",
        "page",
        "effective_from",
        "effective_to",
    }


def test_a_generation_failure_does_not_name_the_subsystem_that_broke(client):
    """`ConnectionError` vs `OperationalError` is a free map of the backend."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=ZeroDivisionError("secret internals"),
    ):
        response = _post(
            client,
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            {"message": "سؤال"},
        )
    assert response.status_code == 503
    body = response.content.decode()
    assert "ZeroDivisionError" not in body
    assert "secret internals" not in body
    assert set(response.json()) == {
        "conversation",
        "student_message",
        "assistant_message",
        "error",
    }


def test_a_failed_turn_carries_the_token_that_retries_it(client):
    """A key kept only in page memory is gone after a reload, and the retry then
    asks the question a second time instead of resuming it."""
    conversation = _conversation(MINE)
    _student(client, MINE)
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        _post(client, url, {"message": "سؤال", "idempotency_key": "k-9"})

    messages = client.get(
        reverse("advisor_conversation_messages", args=[str(conversation.id)])
    ).json()["messages"]
    assert messages[0]["retry_token"] == "k-9"

    # And only there: a completed turn has nothing to resume.
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        return_value=_fake_answer(),
    ):
        _post(client, url, {"message": "سؤال", "idempotency_key": "k-9"})
    messages = client.get(
        reverse("advisor_conversation_messages", args=[str(conversation.id)])
    ).json()["messages"]
    assert "retry_token" not in messages[0]


# ── 14. ordering and feedback ────────────────────────────────────


def test_the_list_is_ordered_by_actual_last_activity(client):
    import datetime as dt

    from django.utils import timezone

    # An explicit gap: two now() calls in the same statement can land on the same
    # microsecond, which makes the ordering ambiguous rather than wrong.
    now = timezone.now()
    older = _conversation(MINE, title="older", last_message_at=now - dt.timedelta(hours=1))
    newer = _conversation(MINE, title="newer", last_message_at=now)
    _student(client, MINE)
    ids = [
        c["id"] for c in client.get(reverse("advisor_conversation_list")).json()["conversations"]
    ]
    assert ids == [str(newer.id), str(older.id)]


def test_feedback_is_one_current_verdict_per_student_and_message(client):
    conversation = _conversation(MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    _student(client, MINE)
    url = reverse("advisor_message_feedback", args=[str(message.id)])

    assert _post(client, url, {"rating": "HELPFUL"}).status_code == 200
    _post(client, url, {"rating": "NOT_HELPFUL", "reason_codes": ["answer_incorrect"]})

    feedback = AdvisorFeedback.objects.get()
    assert feedback.rating == AdvisorFeedback.NOT_HELPFUL
    assert feedback.reason_codes == ["answer_incorrect"]


def test_an_unknown_reason_code_is_dropped_rather_than_stored(client):
    conversation = _conversation(MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    _student(client, MINE)
    _post(
        client,
        reverse("advisor_message_feedback", args=[str(message.id)]),
        {"rating": "NOT_HELPFUL", "reason_codes": ["answer_incorrect", "made_up_code"]},
    )
    assert AdvisorFeedback.objects.get().reason_codes == ["answer_incorrect"]


def test_feedback_cannot_be_attached_to_the_students_own_message(client):
    """Only assistant turns are rateable; a student rating their own question is
    meaningless and would pollute the counts."""
    conversation = _conversation(MINE)
    mine = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_STUDENT, content="q"
    )
    _student(client, MINE)
    url = reverse("advisor_message_feedback", args=[str(mine.id)])
    assert _post(client, url, {"rating": "HELPFUL"}).status_code == 404


def test_an_invalid_rating_is_rejected(client):
    conversation = _conversation(MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    _student(client, MINE)
    url = reverse("advisor_message_feedback", args=[str(message.id)])
    assert _post(client, url, {"rating": "AMAZING"}).status_code == 400


def test_feedback_is_returned_with_the_conversation(client):
    conversation = _conversation(MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    _student(client, MINE)
    _post(
        client,
        reverse("advisor_message_feedback", args=[str(message.id)]),
        {"rating": "HELPFUL"},
    )
    data = client.get(reverse("advisor_conversation_messages", args=[str(conversation.id)])).json()
    assert data["messages"][0]["feedback"]["rating"] == "HELPFUL"


def test_another_students_feedback_is_not_shown_on_a_shared_message(client):
    """Feedback is per student. Seeing someone else's verdict would be a leak even
    on a message they could not otherwise reach."""
    conversation = _conversation(MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="a"
    )
    AdvisorFeedback.objects.create(
        message=message, student_id=THEIRS, rating=AdvisorFeedback.NOT_HELPFUL
    )
    _student(client, MINE)
    data = client.get(reverse("advisor_conversation_messages", args=[str(conversation.id)])).json()
    assert "feedback" not in data["messages"][0]


def test_a_missing_message_id_is_not_found(client):
    _student(client, MINE)
    url = reverse("advisor_message_feedback", args=[str(uuid.uuid4())])
    assert _post(client, url, {"rating": "HELPFUL"}).status_code == 404
