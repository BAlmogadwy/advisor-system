"""The stored outcome describes the answer, not the policies it consulted.

That distinction is the whole point of this module. A rule the university will not
have adjudicated automatically still explains itself perfectly well in general
terms, so deriving the disposition from the rule would mark a plain definition as
an abstention — and, once escalation is live, offer a human hand-off to a student
who asked what a word means.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.urls import reverse

from core.models import AdvisorConversation, AdvisorMessage, FinalDisposition, Student
from core.services.advisor_outcome import (
    OUTCOME_SCHEMA_VERSION,
    OutcomeError,
    ReasonCode,
    derive_outcome,
    validate_missing_information,
    validate_reason_codes,
)
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

MINE = 9201001

PROHIBITED = {
    "policy_id": "TU.PROBATION.DISMISSAL",
    "decision_use": "PROHIBITED_FOR_DECISION",
}
ORDINARY = {"policy_id": "TU.WITHDRAWAL.MAXIMUM", "decision_use": "EXPLANATORY_ONLY"}


def _result(
    *,
    answer: str = "جواب.",
    cited: list[str] | None = None,
    direct: list[dict] | None = None,
    background: list[dict] | None = None,
    conflicting: list[dict] | None = None,
    tools: list[str] | None = None,
    grounding: str = "retrieved",
    **agent_extra,
) -> dict:
    """One turn's result, shaped exactly as `answer_virtual_advisor` returns it."""
    policy_result = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": direct or [],
        "background_policy_evidence": background or [],
        "conflicting_policy_evidence": conflicting or [],
    }
    tool_results = [policy_result] + [{"tool": name, "ok": True} for name in (tools or [])]
    return {
        "ok": True,
        "answer": answer,
        "citations": [],
        "cited_policy_ids": cited or [],
        "agent": {
            "loop_used": True,
            "policy_grounding": grounding,
            "tool_results": tool_results,
            **agent_extra,
        },
    }


# ── 1-3. a prohibited rule constrains, it does not decide ────────


def test_a_prohibited_rule_applied_to_this_student_is_recorded():
    """Direct evidence, cited, with the student's own file open."""
    outcome = derive_outcome(
        _result(
            direct=[PROHIBITED],
            cited=[PROHIBITED["policy_id"]],
            tools=["my_progress"],
        )
    )
    assert ReasonCode.PROHIBITED_FOR_DECISION in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.ABSTAIN


def test_the_same_rule_as_background_evidence_has_no_effect():
    """A record that does not govern the question says nothing about it.

    Reading `policies` rather than `direct_policy_evidence` would attach the
    constraint to every turn that happened to retrieve a related rule.
    """
    outcome = derive_outcome(
        _result(
            direct=[ORDINARY],
            background=[PROHIBITED],
            cited=[ORDINARY["policy_id"]],
            tools=["my_progress"],
        )
    )
    assert ReasonCode.PROHIBITED_FOR_DECISION not in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.PASS


def test_a_general_explanation_of_a_prohibited_rule_still_passes():
    """«وش معنى الإنذار الأكاديمي؟» — the rule is cited, no file is opened.

    Marking this as an abstention would offer a human adviser to a student who
    asked what a word means.
    """
    outcome = derive_outcome(
        _result(
            answer="الإنذار الأكاديمي هو تنبيه يُسجَّل عند انخفاض المعدل.",
            direct=[PROHIBITED],
            cited=[PROHIBITED["policy_id"]],
            tools=[],
        )
    )
    assert outcome.reason_codes == []
    assert outcome.disposition == FinalDisposition.PASS


def test_a_cited_background_rule_is_still_not_a_governing_one():
    """The two guards must not lean on each other.

    Checking only "was it cited" would let a background prohibited record count as
    soon as anything cited it — and the citation and applicability layers are
    exactly the pair whose separation this depends on.
    """
    outcome = derive_outcome(
        _result(
            direct=[ORDINARY],
            background=[PROHIBITED],
            cited=[ORDINARY["policy_id"], PROHIBITED["policy_id"]],
            tools=["my_progress"],
        )
    )
    assert ReasonCode.PROHIBITED_FOR_DECISION not in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.PASS


def test_a_prohibited_rule_that_the_answer_never_cited_is_not_recorded():
    """Retrieved and governing, but the answer did not rest on it."""
    outcome = derive_outcome(
        _result(direct=[PROHIBITED, ORDINARY], cited=[ORDINARY["policy_id"]], tools=["my_progress"])
    )
    assert ReasonCode.PROHIBITED_FOR_DECISION not in outcome.reason_codes


# ── 4-5. what the response ended up being ────────────────────────


def test_a_prohibited_personal_decision_abstains_until_a_case_exists():
    base = _result(direct=[PROHIBITED], cited=[PROHIBITED["policy_id"]], tools=["my_progress"])
    assert derive_outcome(base).disposition == FinalDisposition.ABSTAIN
    assert derive_outcome(base, escalated=True).disposition == FinalDisposition.ESCALATE


def test_a_judge_escalation_overrides_a_turn_that_would_otherwise_pass():
    passing = _result(direct=[ORDINARY], cited=[ORDINARY["policy_id"]])
    assert derive_outcome(passing).disposition == FinalDisposition.PASS

    judged = _result(direct=[ORDINARY], cited=[ORDINARY["policy_id"]], judge_action="ESCALATE")
    outcome = derive_outcome(judged)
    assert outcome.disposition == FinalDisposition.ESCALATE
    assert ReasonCode.JUDGE_REJECTED in outcome.reason_codes


def test_a_student_asking_for_a_person_is_an_escalation_not_a_failure():
    outcome = derive_outcome(_result(direct=[ORDINARY]), student_requested=True)
    assert outcome.disposition == FinalDisposition.ESCALATE
    assert ReasonCode.STUDENT_REQUESTED in outcome.reason_codes


def test_conflicting_authorities_need_a_person():
    outcome = derive_outcome(_result(direct=[ORDINARY], conflicting=[PROHIBITED]))
    assert outcome.disposition == FinalDisposition.ESCALATE
    assert ReasonCode.CONFLICTING_AUTHORITIES in outcome.reason_codes


def test_infrastructure_failure_is_not_an_abstention():
    """Abstaining is a decision about the evidence; failing is never getting to
    make one. A queue that cannot tell them apart fills up with outages."""
    outcome = derive_outcome(_result(turn_error="connection refused"))
    assert outcome.disposition == FinalDisposition.FAILED
    assert outcome.reason_codes == [ReasonCode.MODEL_UNAVAILABLE]


def test_a_refused_citation_abstains():
    outcome = derive_outcome(_result(direct=[ORDINARY], citation_refused=True))
    assert outcome.disposition == FinalDisposition.ABSTAIN


def test_no_governing_policy_and_nothing_cited_abstains():
    outcome = derive_outcome(_result(grounding="none_matched", direct=[], cited=[]))
    assert ReasonCode.POLICY_NOT_FOUND in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.ABSTAIN


def test_a_missing_rule_beside_an_answered_question_is_a_caveat_not_the_outcome():
    """A turn that answered the student-data half and said the guide is silent on
    the rest has still answered something."""
    outcome = derive_outcome(
        _result(grounding="none_matched", direct=[], cited=["TU.LOAD.SEMESTER_RANGE"])
    )
    assert ReasonCode.POLICY_NOT_FOUND in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.PASS


# ── 7-9. the vocabularies are closed ─────────────────────────────


def test_an_unknown_reason_code_is_rejected():
    with pytest.raises(OutcomeError, match="unknown reason code"):
        validate_reason_codes(["PROHIBITED_FOR_DECISION", "SOMETHING_ELSE"])


def test_reason_codes_keep_their_order_and_do_not_repeat():
    codes = validate_reason_codes(
        [ReasonCode.POLICY_NOT_FOUND, ReasonCode.MODEL_UNAVAILABLE, ReasonCode.POLICY_NOT_FOUND]
    )
    assert codes == [ReasonCode.POLICY_NOT_FOUND, ReasonCode.MODEL_UNAVAILABLE]


def test_structured_missing_information_is_accepted():
    entries = validate_missing_information(
        [{"code": "WITHDRAWAL_HISTORY", "label_ar": "عدد مرات الانسحاب السابقة"}]
    )
    assert entries == [{"code": "WITHDRAWAL_HISTORY", "label_ar": "عدد مرات الانسحاب السابقة"}]


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"code": "NOT_A_CODE", "label_ar": "شيء"}, "unknown missing_information code"),
        ({"code": "WITHDRAWAL_HISTORY", "label_ar": ""}, "carries no label"),
        ({"code": "WITHDRAWAL_HISTORY", "label_ar": "withdrawal history"}, "not in Arabic"),
        ({"code": "WITHDRAWAL_HISTORY", "label_ar": "انسحب 3 مرات"}, "carries a value"),
        ({"code": "WITHDRAWAL_HISTORY"}, "exactly code and label_ar"),
        (
            {"code": "WITHDRAWAL_HISTORY", "label_ar": "عدد", "student_id": 123},
            "exactly code and label_ar",
        ),
        ("WITHDRAWAL_HISTORY", "must be an object"),
    ],
)
def test_invalid_missing_information_is_rejected_rather_than_stored(entry, why):
    with pytest.raises(OutcomeError, match=why):
        validate_missing_information([entry])


def test_missing_information_is_bounded():
    """Matched on the BOUND's own message.

    There are only eight valid codes, so a list long enough to exceed the limit
    must repeat one — and a bare `raises(OutcomeError)` is then satisfied by the
    duplicate check whether or not the bound exists at all.
    """
    from core.services.advisor_outcome import MAX_MISSING_ITEMS, MISSING_INFORMATION_CODES

    many = [{"code": code, "label_ar": "وصف"} for code in sorted(MISSING_INFORMATION_CODES)] * 2
    assert len(many) > MAX_MISSING_ITEMS
    with pytest.raises(OutcomeError, match="more than"):
        validate_missing_information(many)


def test_a_runtime_that_produces_nothing_stores_an_empty_list():
    """`[]`, not a guess. The runtime emits no structured account of what it
    lacked, and parsing the Arabic answer for one would invent a
    machine-readable field out of a sentence written for a person."""
    outcome = derive_outcome(_result(direct=[ORDINARY]))
    assert outcome.missing_information == []
    assert ReasonCode.STUDENT_DATA_MISSING not in outcome.reason_codes


def test_declared_missing_information_becomes_a_reason_and_abstains():
    result = _result(direct=[ORDINARY])
    result["missing_information"] = [
        {"code": "WITHDRAWAL_HISTORY", "label_ar": "عدد مرات الانسحاب السابقة"}
    ]
    outcome = derive_outcome(result)
    assert ReasonCode.STUDENT_DATA_MISSING in outcome.reason_codes
    assert outcome.disposition == FinalDisposition.ABSTAIN


# ── 6, 10-12. what is persisted ──────────────────────────────────


def _student(client, student_id: int = MINE):
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": "S", "program": "CS", "section": "M"},
    )
    client.force_login(student_otp.provision_student_user(student_id))


def _send(client, conversation, result, message="سؤال", key=None):
    body = {"message": message}
    if key:
        body["idempotency_key"] = key
    with mock.patch("core.services.student_advisor_v2.answer_student_advisor", return_value=result):
        return client.post(
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            data=json.dumps(body),
            content_type="application/json",
        )


def test_the_typed_outcome_is_persisted_with_the_answer(client):
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _send(
        client,
        conversation,
        _result(direct=[PROHIBITED], cited=[PROHIBITED["policy_id"]], tools=["my_progress"]),
    )

    assistant = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT)
    assert assistant.final_disposition == FinalDisposition.ABSTAIN
    assert assistant.reason_codes == [ReasonCode.PROHIBITED_FOR_DECISION]
    assert assistant.missing_information == []
    assert assistant.outcome_schema_version == OUTCOME_SCHEMA_VERSION


def test_a_retry_persists_only_the_final_outcome(client):
    """One turn, one stored outcome — the one belonging to the answer that
    survived."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor",
        side_effect=RuntimeError("model down"),
    ):
        client.post(
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            data=json.dumps({"message": "سؤال", "idempotency_key": "k"}),
            content_type="application/json",
        )

    _send(
        client,
        conversation,
        _result(direct=[ORDINARY], cited=[ORDINARY["policy_id"]]),
        key="k",
    )

    assistants = AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT)
    assert assistants.count() == 1
    assert assistants.get().final_disposition == FinalDisposition.PASS


def test_a_message_written_before_this_contract_reads_as_unversioned(client):
    """Existing rows migrate with explicit defaults, and are distinguishable."""
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    old = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="قديم"
    )
    old.refresh_from_db()
    assert old.reason_codes == []
    assert old.missing_information == []
    assert old.outcome_schema_version == ""
    assert old.final_disposition == ""


def test_the_student_never_receives_the_typed_outcome(client):
    """It is the system's account of its own reasoning. `status` is what the
    screen needs, and an adviser reads the rest."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _send(
        client,
        conversation,
        _result(direct=[PROHIBITED], cited=[PROHIBITED["policy_id"]], tools=["my_progress"]),
    )

    payload = client.get(
        reverse("advisor_conversation_messages", args=[str(conversation.id)])
    ).json()
    assistant = payload["messages"][1]
    # `language` is "ar" or "en" — the student's own question reflected back, so the
    # browser can lay the answer out in the direction the server already pinned the
    # model to. Deliberately allowed; everything below is what stays out.
    assert set(assistant) == {
        "id",
        "role",
        "content",
        "status",
        "created_at",
        "citations",
        "language",
    }
    assert assistant["language"] in {"ar", "en"}
    # The exact key set above is the real guard. This is belt-and-braces against a
    # value smuggled into a field that IS allowed — and deliberately does not test
    # for "ABSTAIN", which is a substring of the ABSTAINED message status the screen
    # legitimately needs in order to say the answer was withheld.
    body = json.dumps(payload, ensure_ascii=False)
    for leaked in (
        "PROHIBITED_FOR_DECISION",
        "final_disposition",
        "reason_codes",
        "missing_information",
        "outcome_schema_version",
    ):
        assert leaked not in body, leaked
