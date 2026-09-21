"""Handing one turn to a person, from what was committed and nothing else.

The case and the turn record different facts. The turn says why the ANSWER was
limited; the case says why a HUMAN was asked. A student who wants someone to look
at a perfectly good answer produces STUDENT_REQUESTED on the case while the turn
keeps its own reasons — which may be none at all.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.db import transaction
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorMessage,
    AdvisorMessageCitation,
    FinalDisposition,
    Student,
)
from core.services.advisor_outcome import ReasonCode
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

MINE = 9301001
THEIRS = 9301002


def _student(client, student_id: int = MINE):
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": "S", "program": "CS", "section": "M"},
    )
    client.force_login(student_otp.provision_student_user(student_id))


def _turn(
    student_id: int = MINE,
    *,
    disposition: str = FinalDisposition.ABSTAIN,
    reason_codes: list[str] | None = None,
    missing: list[dict] | None = None,
    question: str = "هل أقدر أنسحب؟",
    answer: str = "لا يمكن للنظام البت في حالتك.",
    with_citation: bool = True,
) -> AdvisorMessage:
    conversation = AdvisorConversation.objects.create(student_id=student_id)
    asked = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=question,
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    assistant = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content=answer,
        final_disposition=disposition,
        reason_codes=reason_codes
        if reason_codes is not None
        else [ReasonCode.PROHIBITED_FOR_DECISION],
        missing_information=missing or [],
        outcome_schema_version="1.0",
        status=AdvisorMessage.STATUS_ABSTAINED,
    )
    if with_citation:
        AdvisorMessageCitation.objects.create(
            message=assistant,
            policy_id="TU.WITHDRAWAL.MAXIMUM",
            document_title="الدليل الإرشادي للطالب والطالبة",
            edition="1447",
            page="24",
            effective_from="1447",
            effective_to="",
            authority_status="AUTHORITY_APPROVED",
            validation_status=AdvisorMessageCitation.VALID,
            source_version_hash="abc",
        )
    return assistant


def _escalate(client, message, **body):
    return client.post(
        reverse("advisor_escalation_create", args=[str(message.id)]),
        data=json.dumps(body),
        content_type="application/json",
    )


# ── 1-2. the turn and the case say different things ──────────────


def test_creating_a_case_marks_the_turn_as_escalated(client):
    """Otherwise the database holds an open case whose source answer still reads
    ABSTAIN, and adviser and student are looking at different accounts."""
    _student(client)
    message = _turn()
    assert _escalate(client, message).status_code == 201

    message.refresh_from_db()
    assert message.final_disposition == FinalDisposition.ESCALATE
    assert message.status == AdvisorMessage.STATUS_ESCALATED


def test_a_student_request_does_not_rewrite_why_the_answer_was_limited(client):
    """The case is STUDENT_REQUESTED; the turn keeps its own reasons.

    Collapsing the two would rewrite the record of what constrained the answer
    every time somebody pressed a button.
    """
    _student(client)
    message = _turn(
        disposition=FinalDisposition.PASS,
        reason_codes=[ReasonCode.POLICY_NOT_FOUND],
        answer="باقي لك ٣ مواد.",
    )
    response = _escalate(client, message, student_requested=True)
    assert response.status_code == 201
    assert response.json()["escalation"]["reason_code"] == ReasonCode.STUDENT_REQUESTED

    message.refresh_from_db()
    assert message.reason_codes == [ReasonCode.POLICY_NOT_FOUND], (
        "the turn's reasons were rewritten"
    )
    assert message.final_disposition == FinalDisposition.ESCALATE


def test_a_case_with_no_student_request_is_filed_under_the_turns_own_reason(client):
    _student(client)
    message = _turn(reason_codes=[ReasonCode.PROHIBITED_FOR_DECISION])
    response = _escalate(client, message)
    assert response.json()["escalation"]["reason_code"] == ReasonCode.PROHIBITED_FOR_DECISION


# ── 3-4. the snapshot is frozen, and matches what was stored ─────


def test_the_snapshot_equals_the_persisted_turn(client):
    _student(client)
    missing = [{"code": "WITHDRAWAL_HISTORY", "label_ar": "عدد مرات الانسحاب السابقة"}]
    message = _turn(missing=missing)
    _escalate(client, message)

    snapshot = AdvisorEscalation.objects.get().evidence_snapshot
    assert snapshot["question"] == message.in_reply_to.content
    assert snapshot["assistant_answer"] == message.content
    assert snapshot["final_disposition"] == message.final_disposition
    assert snapshot["reason_codes"] == message.reason_codes
    assert snapshot["missing_information"] == missing

    stored = message.citations.get()
    assert snapshot["citations"] == [
        {
            "policy_id": stored.policy_id,
            "document_title": stored.document_title,
            "edition": stored.edition,
            "page": stored.page,
            "effective_from": stored.effective_from,
            "effective_to": stored.effective_to,
        }
    ]
    # Not reconstructed from a live record, and not read out of the answer text.
    assert snapshot["relevant_student_facts"] == {}


def test_the_snapshot_does_not_move_when_the_world_does(client):
    """A case opened against today's answer must show today's evidence.

    Rebuilt at read time from live policies and a live student record, it would
    change its own facts between being raised and being read, and neither the
    adviser nor the student could tell.
    """
    _student(client)
    message = _turn()
    _escalate(client, message)
    before = AdvisorEscalation.objects.get().evidence_snapshot

    message.citations.update(page="99", edition="1450")
    Student.objects.filter(student_id=MINE).update(program="AI", gpa=1.2)

    after = AdvisorEscalation.objects.get().evidence_snapshot
    assert after == before
    assert after["citations"][0]["page"] == "24"


def test_building_the_snapshot_touches_no_live_policy_or_student_record(client):
    """The property, not the appearance of it: a query to either table during
    construction is the drift above, whatever the values happen to be today."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _student(client)
    message = _turn()
    with CaptureQueriesContext(connection) as queries:
        _escalate(client, message)

    touched = [
        q["sql"]
        for q in queries.captured_queries
        if "students" in q["sql"].lower() and "advisor" not in q["sql"].lower()
    ]
    assert touched == [], touched


# ── 5-6. duplicates ──────────────────────────────────────────────


def test_pressing_the_button_twice_returns_the_same_case(client):
    _student(client)
    message = _turn()
    first = _escalate(client, message)
    second = _escalate(client, message)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["escalation"]["reference"] == second.json()["escalation"]["reference"]
    assert AdvisorEscalation.objects.count() == 1


def test_the_database_refuses_two_live_cases_for_one_turn(client):
    """The enforcement point, tested where it lives.

    The endpoint's own short-circuit is the first layer and the index is the
    second; asserting only through the endpoint cannot tell which one is doing
    the work, so removing either would look harmless.
    """
    from django.db import IntegrityError

    _student(client)
    message = _turn()
    _escalate(client, message)
    case = AdvisorEscalation.objects.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        AdvisorEscalation.objects.create(
            conversation=message.conversation,
            source_message=message,
            student_id=MINE,
            reason_code=ReasonCode.STUDENT_REQUESTED,
            evidence_snapshot={},
        )

    # ...and it stops refusing once the first case is finished with.
    AdvisorEscalation.objects.filter(pk=case.pk).update(status=AdvisorEscalation.Status.CLOSED)
    with transaction.atomic():
        AdvisorEscalation.objects.create(
            conversation=message.conversation,
            source_message=message,
            student_id=MINE,
            reason_code=ReasonCode.STUDENT_REQUESTED,
            evidence_snapshot={},
        )
    assert AdvisorEscalation.objects.count() == 2


def test_pressing_the_button_twice_does_not_spend_two_escalations(client):
    """The short-circuit is not merely a nicety the index could replace.

    Letting the duplicate fall through to the database costs the student a unit of
    a five-per-hour budget for a request that creates nothing — and that budget is
    their only route to a person.
    """
    from core.models import RateLimitBucket
    from core.services import rate_limit

    _student(client)
    message = _turn()
    _escalate(client, message)
    spent = RateLimitBucket.objects.get(key=f"{rate_limit.ESCALATION}:{MINE}").count

    for _ in range(3):
        assert _escalate(client, message).status_code == 200
    after = RateLimitBucket.objects.get(key=f"{rate_limit.ESCALATION}:{MINE}").count
    assert after == spent, "a replay spent the budget that buys a human"


def test_an_existing_case_is_replayed_without_regenerating_its_summary(client):
    """An adviser may already have read it."""
    _student(client)
    message = _turn()
    _escalate(client, message)

    AdvisorEscalation.objects.update(generated_summary="ملخص كتبه المرشد.")
    second = _escalate(client, message)

    assert second.json()["escalation"]["generated_summary"] == "ملخص كتبه المرشد."


def test_a_closed_case_does_not_block_raising_the_question_again(client):
    _student(client)
    message = _turn()
    _escalate(client, message)
    AdvisorEscalation.objects.update(status=AdvisorEscalation.Status.RESOLVED)

    assert _escalate(client, message).status_code == 201
    assert AdvisorEscalation.objects.count() == 2


def test_two_simultaneous_submissions_produce_one_case(client):
    """The partial unique index is the enforcement point; the loser reloads."""
    _student(client)
    message = _turn()
    from core.services.advisor_escalation import build_evidence

    real_build = build_evidence
    created: list[AdvisorEscalation] = []

    def build_then_race(msg):
        # A competing request lands between the emptiness check and the insert.
        if not created:
            with transaction.atomic():
                created.append(
                    AdvisorEscalation.objects.create(
                        conversation=msg.conversation,
                        source_message=msg,
                        student_id=MINE,
                        reason_code=ReasonCode.PROHIBITED_FOR_DECISION,
                        generated_summary="سباق",
                        evidence_snapshot={},
                    )
                )
        return real_build(msg)

    with mock.patch("core.services.advisor_turn.build_evidence", side_effect=build_then_race):
        response = _escalate(client, message)

    assert response.status_code == 200
    assert AdvisorEscalation.objects.count() == 1
    assert response.json()["escalation"]["generated_summary"] == "سباق"


# ── 7. the summary must not need a model ─────────────────────────


def test_a_case_can_be_raised_while_the_model_is_down(client):
    """The model being down is frequently WHY a turn needs a person.

    A summary that required one would make the outage remove the only route to a
    human at exactly the moment it is needed.
    """
    _student(client)
    message = _turn()
    llm = mock.Mock(side_effect=AssertionError("no model may be reached"))
    with mock.patch("core.services.local_llm.LocalLLMClient", llm):
        response = _escalate(client, message)

    assert response.status_code == 201
    assert response.json()["escalation"]["generated_summary"].strip()


# ── ownership and eligibility ────────────────────────────────────


def test_another_students_message_cannot_be_escalated(client):
    _student(client)
    theirs = _turn(student_id=THEIRS)
    assert _escalate(client, theirs).status_code == 404
    assert AdvisorEscalation.objects.count() == 0


def test_a_student_message_cannot_be_the_source_of_a_case(client):
    _student(client)
    message = _turn()
    assert _escalate(client, message.in_reply_to).status_code == 404


def test_an_answer_with_no_question_attached_cannot_be_escalated(client):
    """A case an adviser has to reconstruct from the student's memory of the
    question is not an anchored case."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    orphan = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="جواب بلا سؤال",
        final_disposition=FinalDisposition.ABSTAIN,
    )
    assert _escalate(client, orphan).status_code == 404


def test_a_satisfactory_answer_is_not_escalated_unless_the_student_asks(client):
    """Not every unanswered question needs an adviser — some need redirecting."""
    _student(client)
    message = _turn(
        disposition=FinalDisposition.PASS,
        reason_codes=[ReasonCode.POLICY_NOT_FOUND],
        answer="باقي لك ٣ مواد.",
    )
    assert _escalate(client, message).status_code == 409
    assert AdvisorEscalation.objects.count() == 0

    assert _escalate(client, message, student_requested=True).status_code == 201


# ── 10. what the student may read ────────────────────────────────


def test_the_student_serialiser_withholds_the_advisers_working_notes(client):
    _student(client)
    message = _turn()
    _escalate(client, message)
    AdvisorEscalation.objects.update(
        adviser_notes="الطالب يبدو غير جاد؛ راجع سجله التأديبي.",
        assigned_adviser_id="A-1007",
    )

    detail = client.get(
        reverse(
            "advisor_escalation_detail",
            args=[AdvisorEscalation.objects.get().reference],
        )
    )
    assert detail.status_code == 200
    case = detail.json()["escalation"]
    assert set(case) == {
        "reference",
        "status",
        "status_label",
        "reason_code",
        "student_note",
        "generated_summary",
        "resolution_message",
        "created_at",
        "updated_at",
        "resolved_at",
    }
    body = detail.content.decode()
    for withheld in ("غير جاد", "A-1007", "evidence_snapshot", "adviser_notes"):
        assert withheld not in body, withheld


def test_a_student_sees_only_their_own_cases(client):
    _student(client)
    mine = _turn()
    theirs = _turn(student_id=THEIRS)
    _escalate(client, mine)
    AdvisorEscalation.objects.create(
        conversation=theirs.conversation,
        source_message=theirs,
        student_id=THEIRS,
        reason_code=ReasonCode.STUDENT_REQUESTED,
        evidence_snapshot={},
    )

    listed = client.get(reverse("advisor_escalation_list")).json()["escalations"]
    assert len(listed) == 1
    assert listed[0]["reference"] == AdvisorEscalation.objects.get(student_id=MINE).reference


def test_another_students_case_is_not_found_by_reference(client):
    _student(client)
    theirs = _turn(student_id=THEIRS)
    case = AdvisorEscalation.objects.create(
        conversation=theirs.conversation,
        source_message=theirs,
        student_id=THEIRS,
        reason_code=ReasonCode.STUDENT_REQUESTED,
        evidence_snapshot={},
    )
    url = reverse("advisor_escalation_detail", args=[case.reference])
    assert client.get(url).status_code == 404
