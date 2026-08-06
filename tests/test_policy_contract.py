"""Retrieval is the server's job, and the answer owes a governing citation.

Two claims, tested separately because they fail separately:

  * `requires_policy_contract` decides what an answer OWES, never whether to
    retrieve. Retrieval is unconditional on both paths, so a detector miss costs
    an obligation and can never reopen the grounding bypass.

  * `PolicyContractState` decides whether the obligation was met. The failure it
    exists to catch is subtle and entirely plausible: an answer that cites a
    BACKGROUND record. The id is real, it was retrieved this request, and it
    passes every check the citation validator makes — so nothing before this
    would object.
"""

from __future__ import annotations

import json

import pytest

from core.services.advisor_outcome import ReasonCode, derive_outcome
from core.services.policy_contract import (
    PolicyContractState,
    build_policy_contract_state,
    policy_intent,
    requires_policy_contract,
)

pytestmark = pytest.mark.django_db


class _PlainClient:
    """A client with no `chat_with_tools`, so the single-shot path runs.

    Defined here rather than imported from another test module. The import made
    `test_policy_grounding_paths` reachable as both `tests.X` and `X`, which is
    enough to stop mypy resolving the package at all — and reaching into another
    test file's private helper is a dependency between test modules that nothing
    declares.
    """

    def __init__(self, answer: str = "لا أعرف.") -> None:
        self.answer = answer
        self.chat_calls: list[list[dict]] = []

    backend = "local"
    supports_assistant_prefill = True

    def resolve_model(self, requested_model=None):
        return requested_model or "fake-plain"

    def chat(self, messages, **kwargs):
        from core.services.local_llm import ChatResult

        self.chat_calls.append([dict(m) for m in messages])
        return ChatResult(content=self.answer, model="fake-plain", usage={})


# ── 1. the detector ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "أقدر أسجل ساعات أكثر من الحد المسموح؟",
        "كم مرة مسموح أعيد نفس المادة؟",
        "هل حذف المادة يأثر على معدلي؟",
        "متى آخر موعد لطلب معادلة المقررات؟",
        "وش شروط التدريب التعاوني؟",
        "هل أحتاج موافقة المرشد عشان أحذف مادة؟",
        "How many hours may I register?",
        "What is the maximum load allowed?",
    ],
)
def test_a_question_that_asks_for_a_rule_is_held_to_the_contract(question: str) -> None:
    assert requires_policy_contract(question) is True
    assert policy_intent(question), "the reason must be recorded, not just the verdict"


@pytest.mark.parametrize(
    "question",
    [
        "وش عندي بكرة الأحد؟",
        "GS112 وين قاعتها؟",
        "كم الفراغ بالضبط بين محاضراتي يوم الثلاثاء؟",
        "MGT324 كم ساعة معتمدة، ومتى محاضراتها ووين؟",
        "الجدول اللي تعرضه لي هذا لأي سنة وأي فصل بالضبط؟",
        "",
        "   ",
    ],
)
def test_a_pure_record_question_is_not(question: str) -> None:
    """The class where a false positive is NOT a cautious refusal.

    For these the abstention would replace a completely answerable answer with a
    referral, because it takes the whole turn. Every other question in the corpus
    can absorb an unnecessary citation requirement; these cannot absorb an
    unnecessary abstention, which is why they are pinned by name.
    """
    assert requires_policy_contract(question) is False


def test_the_detector_never_decides_whether_to_retrieve() -> None:
    """The guarantee that answers the objection recorded in `virtual_advisor`:
    "a classifier that says no is exactly how the bypass comes back".

    A `required=False` question still carries a real grounding state, because
    retrieval ran before anything was generated. The flag can only add an
    obligation; it can never subtract a lookup.
    """
    from core.models import Student
    from core.services.advisor_principal import AdvisorPrincipal
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor import answer_virtual_advisor

    Student.objects.get_or_create(
        student_id=6001001, defaults={"name": "S", "program": "CS", "section": "M"}
    )
    result = answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=6001001),
        client=_PlainClient(),
    )
    assert result["agent"]["policy_required"] is False
    assert result["agent"]["policy_grounding"] in {
        "retrieved",
        "none_governing",
        "none_matched",
        "unavailable",
    }
    assert result["agent"]["policy_grounding"] != "not_consulted"


# ── 2. the state ─────────────────────────────────────────────────


def _state(**kwargs) -> PolicyContractState:
    base = {
        "required": True,
        "grounding_state": "retrieved",
        "direct_policy_ids": frozenset({"TU.A"}),
        "citable_policy_ids": frozenset({"TU.A", "TU.B"}),
    }
    return PolicyContractState(**{**base, **kwargs})


def test_citing_a_background_record_does_not_satisfy_the_contract() -> None:
    """THE case this state exists for.

    `TU.B` was retrieved this request and is in `citable`, so the citation
    validator accepts it, the id resolves, the page checks out — and the answer
    still cites something the applicability layer decided does NOT govern the
    question. Checking against `citable_policy_ids` here instead of
    `direct_policy_ids` would make the direct/background split decorative.
    """
    state = _state()
    assert state.missing_governing_citation({"TU.B"}) is True
    assert state.missing_governing_citation({"TU.A"}) is False
    assert state.missing_governing_citation({"TU.A", "TU.B"}) is False
    assert state.missing_governing_citation(set()) is True


def test_no_governing_evidence_means_abstain_not_improvise() -> None:
    for grounding in ("none_governing", "none_matched", "unavailable"):
        state = _state(grounding_state=grounding, direct_policy_ids=frozenset())
        assert state.must_abstain is True
        # …and nothing is owed in citations, because there is nothing to cite.
        assert state.missing_governing_citation(set()) is False


def test_a_question_that_owes_nothing_never_abstains() -> None:
    for grounding in ("retrieved", "none_governing", "none_matched", "unavailable"):
        state = _state(required=False, grounding_state=grounding, direct_policy_ids=frozenset())
        assert state.must_abstain is False
        assert state.missing_governing_citation(set()) is False


def test_retrieval_that_never_ran_is_a_programming_failure() -> None:
    """Unreachable through any normal path now. Kept because "we never looked"
    must never be able to become "here is the rule"."""
    assert _state(grounding_state="not_consulted").retrieval_missing is True
    assert _state(required=False, grounding_state="not_consulted").retrieval_missing is False
    assert _state().retrieval_missing is False


def test_the_state_is_built_from_every_policy_result_not_just_the_first() -> None:
    """A turn holds the prefetch AND any credit-policy evidence injected when a
    tool returns a credit block. Both are stamped `policy_lookup`; a contract
    built from only the first understates what the answer may cite."""
    state = build_policy_contract_state(
        "كم ساعة مسموح أسجل؟",
        [
            {
                "tool": "policy_lookup",
                "direct_policy_evidence": [{"policy_id": "TU.PREFETCH"}],
                "citable": [{"policy_id": "TU.PREFETCH"}],
            },
            {"tool": "find_students", "direct_policy_evidence": [{"policy_id": "TU.WRONG"}]},
            {
                "tool": "policy_lookup",
                "direct_policy_evidence": [{"policy_id": "TU.CREDIT"}],
                "citable": [{"policy_id": "TU.CREDIT"}],
            },
        ],
        grounding_state="retrieved",
    )
    assert state.direct_policy_ids == frozenset({"TU.PREFETCH", "TU.CREDIT"})
    assert "TU.WRONG" not in state.citable_policy_ids, "only policy_lookup results count"
    assert state.required is True


def test_telemetry_carries_counts_and_ids_never_policy_text() -> None:
    telemetry = _state().as_telemetry()
    assert telemetry["policy_required"] is True
    assert telemetry["direct_policy_count"] == 1
    assert telemetry["citable_policy_count"] == 2
    # No prose, no records, no internal fields.
    assert not any("statement" in k or "note" in k for k in telemetry)


# ── 3. the outcome layer ─────────────────────────────────────────


def test_a_record_question_with_no_governing_policy_is_still_a_pass() -> None:
    """The disposition inversion that unconditional retrieval would otherwise
    cause. «وين قاعة GS112؟» legitimately finds nothing governing; before the
    prefetch that recorded `not_consulted` and produced no reason code. Ungated,
    the same turn now records `none_matched` -> POLICY_NOT_FOUND -> ABSTAIN, for
    83 of the 284 corpus questions, none of which asked for a rule.
    """
    outcome = derive_outcome(
        {
            "answer": "قاعتها 2-14.",
            "agent": {"policy_required": False, "policy_grounding": "none_matched"},
        }
    )
    assert outcome.disposition == "PASS"
    assert ReasonCode.POLICY_NOT_FOUND not in outcome.reason_codes


def test_a_rule_question_with_no_governing_policy_abstains() -> None:
    outcome = derive_outcome(
        {"answer": "…", "agent": {"policy_required": True, "policy_grounding": "none_matched"}}
    )
    assert outcome.disposition == "ABSTAIN"
    assert ReasonCode.POLICY_NOT_FOUND in outcome.reason_codes


def test_conflicting_records_only_escalate_when_a_rule_was_owed() -> None:
    """CONFLICTING_AUTHORITIES escalates unconditionally, and unconditional
    retrieval surfaces conflicts on 23 corpus questions. Ungated, a timetable
    question whose retrieval touched two disagreeing records would open a real
    case in an adviser's queue."""
    conflicted = [{"tool": "policy_lookup", "conflicting_policy_evidence": [{"policy_id": "TU.X"}]}]
    escalating = derive_outcome(
        {
            "answer": "…",
            "agent": {
                "policy_required": True,
                "policy_grounding": "retrieved",
                "tool_results": conflicted,
            },
        }
    )
    assert escalating.disposition == "ESCALATE"

    quiet = derive_outcome(
        {
            "answer": "قاعتها 2-14.",
            "agent": {
                "policy_required": False,
                "policy_grounding": "retrieved",
                "tool_results": conflicted,
            },
        }
    )
    assert quiet.disposition == "PASS"


def test_an_older_stored_turn_keeps_the_behaviour_it_was_written_under() -> None:
    """No `policy_required` key at all — a row persisted before the flag existed.
    Defaulting to False would silently reclassify every historical abstention."""
    outcome = derive_outcome({"answer": "…", "agent": {"policy_grounding": "none_matched"}})
    assert ReasonCode.POLICY_NOT_FOUND in outcome.reason_codes


def test_a_grounding_refusal_is_reported_as_an_abstention() -> None:
    """The output contract replaces the answer; the outcome layer has to know.

    Without this the turn persists as COMPLETED with a refusal in the body: the UI
    shows no status note, and `may_escalate` refuses the student a human with
    «هذه الإجابة لا تحتاج إلى مراجعة» — a refused answer presented as a resolved
    one. `citation_refused` was wired; its sibling was not.
    """
    outcome = derive_outcome(
        {
            "answer": "لم أتمكن من التحقق…",
            "agent": {
                "policy_required": False,
                "policy_grounding": "retrieved",
                "grounding_refused": True,
            },
        }
    )
    assert outcome.disposition == "ABSTAIN"
    assert ReasonCode.OUTPUT_NOT_GROUNDED in outcome.reason_codes
    # …and the escalation layer will now let the student reach a person.
    from core.services.advisor_escalation import may_escalate

    class _Message:
        final_disposition = "ABSTAIN"
        reason_codes = [ReasonCode.OUTPUT_NOT_GROUNDED]

    assert may_escalate(_Message()) is True


def test_the_model_sees_the_credit_policy_before_it_states_a_limit() -> None:
    """Item 6, and the defect it names.

    The backing records used to be assembled AFTER generation, immediately before
    validating citations — so the validator knew which policies could have been
    cited while the model composed the answer having never seen them. That is
    exactly how a correct 19-hour limit arrives attributed to nothing.

    Asserted on the FIRST request, because "before it answers" is the whole
    claim: finding the evidence anywhere in the transcript would pass equally on
    the arrangement this replaces.
    """
    from core.models import Student
    from core.services.advisor_principal import AdvisorPrincipal
    from core.services.credit_policy import BACKING_POLICY_IDS
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor import answer_virtual_advisor

    Student.objects.get_or_create(
        student_id=6001001, defaults={"name": "S", "program": "CS", "section": "M"}
    )
    client = _PlainClient()
    answer_virtual_advisor(
        question="كم ساعة أقدر أسجل هذا الترم؟",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=6001001),
        client=client,
    )
    first_request = "\n".join(m["content"] for m in client.chat_calls[0])

    # Asserted on the DEDICATED key, not merely on "a backing id appears
    # somewhere". The general policy prefetch retrieves credit-load records for
    # this question too, so a loose substring check passes whether or not the
    # credit block was seeded — it cannot tell the two channels apart, and a
    # mutation that removes the seeding survives it.
    assert "credit_policy_evidence" in first_request, (
        "the credit block's backing records were not seeded before generation"
    )
    seeded = first_request.split("credit_policy_evidence", 1)[1][:4000]
    assert any(pid in seeded for pid in BACKING_POLICY_IDS.values()), (
        "the seeded credit evidence names no backing policy id"
    )


def test_a_governing_policy_is_never_shown_twice_with_opposite_directness() -> None:
    """One record, one classification.

    The prompt trim set `policies` to the governing rows and left
    `direct_policy_evidence` beside it, so after projection the same record
    carried `is_direct_evidence: false` under one key and `true` under the other.
    A contract that turns on directness cannot be built on a record whose
    directness depends on which list you read.
    """
    from core.services.advisor_remote_boundary import RemoteToolBoundary
    from core.services.llm_remote_privacy import RemoteIdentityMap
    from core.services.virtual_advisor import _policy_evidence_for_prompt

    raw = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {"policy_id": "TU.GOVERNS", "statement_ar": "نص", "decision_use": "PERMITTED"}
        ],
        "background_policy_evidence": [{"policy_id": "TU.BACKGROUND"}],
        "citable": [{"policy_id": "TU.GOVERNS", "page": 24}],
    }
    boundary = RemoteToolBoundary(
        scope={"role": "STUDENT", "student_id": 1}, identities=RemoteIdentityMap()
    )
    shaped = _policy_evidence_for_prompt(boundary.project_tool_result("policy_lookup", raw))

    assert "direct_policy_evidence" not in shaped, "the governing rows are in one bucket only"
    assert [r["policy_id"] for r in shaped["policies"]] == ["TU.GOVERNS"]
    assert all(r["is_direct_evidence"] is True for r in shaped["policies"])
    assert "TU.BACKGROUND" not in json.dumps(shaped, ensure_ascii=False)

    # …and putting it through the context projector does not re-classify it.
    twice = boundary.project_context({"policy_evidence": shaped})
    assert all(r["is_direct_evidence"] is True for r in twice["policy_evidence"]["policies"])
