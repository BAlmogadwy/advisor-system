"""Every answer path must reach a policy basis, or say it could not.

The acceptance rule this file exists to hold:

    No production response path may answer a policy-dependent question unless it
    has either retrieved applicable AUTHORITY_APPROVED policies, or explicitly
    reported that no authoritative policy was available.

The single-shot fallback used to satisfy neither. It carried none of the policy
rules and its seed planner never consulted the store, so a regulation question
answered there came straight from parametric memory — uncited, unretrieved, and
invisible, because the citation check has nothing to object to when the model never
saw a policy id to misuse. It is reached whenever the agent loop is disabled by
settings or the model rejects tool calling mid-request, which is not a rare path.

Each test below drives a different way of ending up there.
"""

from __future__ import annotations

import pytest

from core.services.local_llm import LocalLLMBadRequest
from core.services.rbac import ROLE_STUDENT
from core.services.virtual_advisor import (
    POLICY_RULES_SEEDED,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_AGENT,
    answer_virtual_advisor,
)
from tests.test_virtual_advisor_agent_loop import FakeToolClient, _tool_call, _tool_turn

pytestmark = pytest.mark.django_db

POLICY_QUESTION = "كم مرة أقدر أنسحب من مقرر؟"
SCOPE = {"role": ROLE_STUDENT, "student_id": 6001001}


class _NoToolsClient:
    """A client without ``chat_with_tools`` — the loop cannot run at all."""

    def __init__(self, answer="لا أعرف."):
        self.answer = answer
        self.chat_calls: list[list[dict]] = []

    def resolve_model(self, requested_model=None):
        return requested_model or "fake-plain"

    def chat(
        self, messages, *, model=None, temperature=0.2, max_tokens=None, assistant_prefill=None
    ):
        from core.services.local_llm import ChatResult

        self.chat_calls.append([dict(m) for m in messages])
        return ChatResult(content=self.answer, model="fake-plain", usage={}, raw={})


class _ToolsRejectedClient(_NoToolsClient):
    """Accepts a tools request, then rejects it — the mid-request fallback."""

    def chat_with_tools(self, messages, **kwargs):
        raise LocalLLMBadRequest("this model does not support tools")


def _system_prompt_of(client) -> str:
    return client.chat_calls[0][0]["content"]


def _context_of(client) -> str:
    return next(m["content"] for m in client.chat_calls[0] if m["role"] == "user")


# ── the contract is in both prompts ──────────────────────────────


def test_both_prompts_carry_the_policy_contract():
    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_AGENT):
        assert "UNIVERSITY RULES" in prompt
        assert "[POLICY_ID]" in prompt
        assert "PROHIBITED_FOR_DECISION" in prompt


def test_the_single_shot_prompt_does_not_promise_a_tool_it_does_not_have():
    """Telling a tool-less path to "call policy_lookup first" invites invention."""
    assert "Call policy_lookup FIRST" not in SYSTEM_PROMPT
    assert "policy_evidence" in SYSTEM_PROMPT
    assert "NO tools on this path" in POLICY_RULES_SEEDED


# ── failure mode 1: no tool support at all ───────────────────────


def test_fallback_retrieves_policies_and_puts_them_in_the_context():
    fake = _NoToolsClient()
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    assert result["agent"]["loop_used"] is False
    assert result["agent"]["policy_grounding"] == "retrieved"
    # The policies reached the model...
    assert "policy_evidence" in _context_of(fake)
    assert "TU.WITHDRAWAL.MAXIMUM" in _context_of(fake)
    # ...and the citation contract applies here too.
    assert result["citations"], "the fallback must expose what it was entitled to cite"
    assert "TU.WITHDRAWAL.MAXIMUM" in {c["policy_id"] for c in result["citations"]}


def test_fallback_prompt_is_the_seeded_variant():
    fake = _NoToolsClient()
    answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)
    assert "UNIVERSITY RULES" in _system_prompt_of(fake)
    assert "policy_evidence" in _system_prompt_of(fake)


# ── failure mode 2: the model rejects tool calling mid-request ───


def test_tools_rejected_mid_request_still_reaches_the_policy_store():
    fake = _ToolsRejectedClient()
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    assert result["agent"]["fallback_reason"] == "tools_rejected_by_model"
    assert result["agent"]["policy_grounding"] == "retrieved"
    assert "TU.WITHDRAWAL.MAXIMUM" in _context_of(fake)


# ── failure mode 3: the loop is disabled by settings ─────────────


def test_agent_loop_disabled_by_settings_still_reaches_the_policy_store(settings):
    settings.VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED = False
    fake = FakeToolClient(turns=[_tool_turn(content="...")])
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    assert result["agent"]["loop_used"] is False
    assert result["agent"]["policy_grounding"] == "retrieved"


# ── failure mode 4: nothing applicable in the store ──────────────


def test_no_applicable_policy_is_reported_not_silently_omitted():
    """The second half of the acceptance rule: saying so is a valid outcome.

    What must never happen is the model receiving no policy block and no statement
    that there is none — that is the state in which it answers from memory.
    """
    fake = _NoToolsClient()
    result = answer_virtual_advisor(question="zzzz qqqq wwww", scope=SCOPE, client=fake)

    assert result["agent"]["policy_grounding"] == "none_matched"
    context = _context_of(fake)
    assert "policy_evidence" in context
    assert result["citations"] == [], "nothing retrieved means nothing citable"


# ── failure mode 5: the policy store itself fails ────────────────


def test_a_broken_policy_store_degrades_to_abstention_not_to_memory(monkeypatch):
    """An outage must not silently restore the ungrounded path."""

    def _boom(*args, **kwargs):
        raise RuntimeError("policy store on fire")

    monkeypatch.setattr(
        "core.services.virtual_advisor_capabilities.AdvisorCapabilityRegistry.execute", _boom
    )
    fake = _NoToolsClient()
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    assert result["agent"]["policy_grounding"] == "unavailable"
    assert result["citations"] == []
    # The model is told it may not state a rule, rather than being left to guess.
    context = _context_of(fake)
    assert "could not be consulted" in context or "policy_evidence" in context


def test_a_malformed_store_response_is_treated_as_unavailable(monkeypatch):
    monkeypatch.setattr(
        "core.services.virtual_advisor_capabilities.AdvisorCapabilityRegistry.execute",
        lambda *a, **k: "not a dict at all",
    )
    fake = _NoToolsClient()
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)
    assert result["agent"]["policy_grounding"] == "unavailable"
    assert result["citations"] == []


# ── failure mode 6: the agent path, for comparison ───────────────


def test_agent_path_records_when_the_model_never_consulted_the_store():
    """On the agent path the MODEL decides. That choice must be visible.

    This is the residual gap the fallback fix does not close: a model that answers a
    regulation question without calling the tool leaves nothing for the citation
    check to catch, because it never saw an id to misuse. Recording it is what makes
    it measurable, and catching the resulting claim is the semantic judge's job.
    """
    fake = FakeToolClient(turns=[_tool_turn(content="خمس مرات، بالتأكيد.")])
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    assert result["agent"]["loop_used"] is True
    assert result["agent"]["policy_grounding"] == "not_consulted"
    assert result["cited_policy_ids"] == []


def test_agent_path_records_a_successful_consultation():
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(_tool_call("policy_lookup", {"query": POLICY_QUESTION}),)),
            _tool_turn(content="«الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»"),
        ]
    )
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)
    assert result["agent"]["policy_grounding"] == "retrieved"
    assert result["cited_policy_ids"] == ["TU.WITHDRAWAL.MAXIMUM"]


# ── failure mode 7: citation enforcement covers the fallback ─────


def test_the_fallback_cannot_cite_a_policy_it_did_not_retrieve():
    """The enforcement must not be agent-path-only."""
    fake = _NoToolsClient(answer="حسب «الدليل الإرشادي للطالب، ص 25 [TU.DISMISSAL.THREE_WARNINGS]»")
    result = answer_virtual_advisor(question=POLICY_QUESTION, scope=SCOPE, client=fake)

    agent = result["agent"]
    assert agent["citation_retry"] is True
    assert agent["bad_citations"][0]["reason"] == "NOT_RETRIEVED_THIS_REQUEST"
