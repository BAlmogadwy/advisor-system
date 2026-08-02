"""Durable conversations: who may read them, and does the browser see what was stored.

The security tests come first because they protect everything after them. A
persistence bug loses a message; an ownership bug shows one student another
student's academic questions, which is a different category of wrong.
"""

from __future__ import annotations

import json
import uuid
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
