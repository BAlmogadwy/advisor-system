from __future__ import annotations

import json

import pytest

from core.advisor_conversation_views import _message_json
from core.models import AdvisorConversation, AdvisorMessage, Student
from core.services.advisor_channel_privacy import TELEGRAM_SAFE_PROFILE
from core.services.advisor_evidence_audit import (
    STUDENT_V2_PROMPT_VERSION,
    build_evidence_audit,
    evidence_sha256,
    normalise_evidence_audit,
    normalise_model_revision,
    normalise_prompt_version,
)
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_turn import CREATED, run_advisor_turn
from core.services.answer_consistency import (
    REQUESTED_EVIDENCE_OMITTED,
    UNSUPPORTED_ACADEMIC_FACT,
)
from core.services.llm_backend import (
    ToolCallRequest,
    ToolChatResult,
    _provider_model_revision,
)
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import answer_student_advisor_v2

pytestmark = pytest.mark.django_db

SID = 9876543


def _provider_payload() -> dict[str, object]:
    return {
        "tool": "my_timetable",
        "ok": True,
        "student_ref": "STUDENT_REF_secret",
        "student_id": SID,
        "email": "tu9876543@taibahu.edu.sa",
        "gpa": "4.91",
        "meetings": [
            {
                "course_code": "AI463",
                "section": "F2",
                "room": "F-SECRET-12",
            }
        ],
    }


def _audit() -> dict[str, object]:
    return build_evidence_audit(
        provider_evidence=[
            ("policy_lookup", {"tool": "policy_lookup", "ok": True}),
            ("my_timetable", _provider_payload()),
        ],
        validation_outcome="repaired",
        violations=[REQUESTED_EVIDENCE_OMITTED],
        violations_after_repair=[],
        repair_attempted=True,
    )


def _result() -> dict[str, object]:
    return {
        "ok": True,
        "answer": "هذه إجابة تم التحقق منها.",
        "model": "qwen3.7-plus",
        "citations": [],
        "cited_policy_ids": [],
        "missing_information": [],
        "agent": {
            "loop_used": True,
            "policy_grounding": "not_consulted",
            "prompt_version": STUDENT_V2_PROMPT_VERSION,
            "model_revision": "fp_actual_20260822",
            "evidence_audit": _audit(),
        },
    }


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID)


class _AuditClient:
    backend = "local"
    supports_assistant_prefill = True

    def __init__(self) -> None:
        call = ToolCallRequest(
            id="call_timetable",
            name="my_timetable",
            arguments={},
            raw_arguments="{}",
        )
        self.turns = [
            ToolChatResult(
                content="",
                tool_calls=(call,),
                model="qwen3.7-plus",
                usage={},
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_timetable",
                            "type": "function",
                            "function": {"name": "my_timetable", "arguments": "{}"},
                        }
                    ],
                },
                model_revision="fp_tool_actual",
            ),
            ToolChatResult(
                content=(
                    "The registered timetable contains 1 course totaling 3 credits: "
                    "AI463, section F2."
                ),
                tool_calls=(),
                model="qwen3.7-plus",
                usage={},
                assistant_message={"role": "assistant", "content": "verified answer"},
                model_revision="fp_answer_actual",
            ),
        ]

    def resolve_model(self, requested_model=None):
        return requested_model or "qwen3.7-plus"

    def chat_with_tools(self, messages, *, tools, **kwargs):  # noqa: ARG002
        return self.turns.pop(0)

    def chat(self, messages, **kwargs):  # pragma: no cover - no repair is expected
        raise AssertionError("unexpected repair")


def test_evidence_hash_is_canonical_across_dictionary_order():
    left = {"tool": "my_timetable", "ok": True, "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "ok": True, "tool": "my_timetable"}

    assert evidence_sha256(left) == evidence_sha256(right)


def test_model_revision_is_only_taken_from_explicit_provider_metadata():
    assert _provider_model_revision({"model": "qwen3.7-plus"}) == ""
    assert _provider_model_revision({"system_fingerprint": "fp_actual"}) == "fp_actual"
    assert _provider_model_revision({"model_revision": "2026-08-22-r1"}) == "2026-08-22-r1"


def test_audit_contains_hashes_and_closed_categories_but_no_academic_values():
    audit = _audit()
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True)

    assert audit["tool_names"] == ["policy_lookup", "my_timetable"]
    assert audit["evidence_hashes"][1] == {
        "tool": "my_timetable",
        "sha256": evidence_sha256(_provider_payload()),
    }
    assert audit["validation"] == {
        "outcome": "repaired",
        "violations": [REQUESTED_EVIDENCE_OMITTED],
        "violations_after_repair": [],
    }
    assert audit["repair"] == {"attempted": True, "result": "succeeded"}

    for forbidden in (
        str(SID),
        "tu9876543@taibahu.edu.sa",
        "STUDENT_REF_secret",
        "4.91",
        "AI463",
        "F2",
        "F-SECRET-12",
    ):
        assert forbidden not in encoded


def test_persistence_rewhitelists_transient_audit_metadata():
    valid_digest = evidence_sha256({"tool": "my_timetable", "ok": True})
    raw = {
        "schema_version": "future-untrusted",
        "tool_names": ["my_timetable", f"student_{SID}"],
        "evidence_hashes": [
            {"tool": "my_timetable", "sha256": valid_digest, "raw": "AI463/F2"},
            {"tool": f"student_{SID}", "sha256": valid_digest},
            {"tool": "my_timetable", "sha256": "AI463"},
        ],
        "validation": {
            "outcome": "passed",
            "violations": [UNSUPPORTED_ACADEMIC_FACT, "student_9876543"],
            "violations_after_repair": [],
            "answer": "AI463 section F2",
        },
        "repair": {"attempted": False, "result": "not_attempted", "error": "AI463"},
        "raw_evidence": _provider_payload(),
        "student_id": SID,
    }

    cleaned = normalise_evidence_audit(raw)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)

    assert cleaned == {
        "schema_version": "1",
        "tool_names": ["my_timetable"],
        "evidence_hashes": [{"tool": "my_timetable", "sha256": valid_digest}],
        "validation": {
            "outcome": "passed",
            "violations": [UNSUPPORTED_ACADEMIC_FACT],
            "violations_after_repair": [],
        },
        "repair": {"attempted": False, "result": "not_attempted"},
        "flags": {"turn_budget_exhausted": False, "provider_error": ""},
        "cost": {
            "inference_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "turn_ms": 0,
        },
    }
    assert str(SID) not in encoded
    assert "AI463" not in encoded
    assert "F2" not in encoded


def test_v2_builds_the_audit_from_provider_visible_evidence(monkeypatch):
    Student.objects.create(student_id=SID, name="Audit Student", program="AI", section="F")

    def fake_execute(name, arguments, *, principal, context=None):  # noqa: ARG001
        assert name == "my_timetable"
        return _provider_payload() | {
            "registered_course_count": 1,
            "registered_credit_hours": 3,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    result = answer_student_advisor_v2(
        question="Show my current timetable",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=_AuditClient(),
    )

    audit = result["agent"]["evidence_audit"]
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    assert audit["tool_names"] == ["policy_lookup", "my_timetable"]
    assert audit["validation"]["outcome"] == "passed"
    assert result["agent"]["prompt_version"] == STUDENT_V2_PROMPT_VERSION
    assert result["agent"]["model_revision"] == "fp_answer_actual"
    for forbidden in (str(SID), "AI463", "F2", "4.91", "tu9876543@taibahu.edu.sa"):
        assert forbidden not in encoded


def test_web_and_telegram_turns_persist_the_same_redacted_audit(monkeypatch):
    Student.objects.create(student_id=SID, name="Audit Student", program="AI", section="F")
    web = AdvisorConversation.objects.create(student_id=SID)
    telegram = AdvisorConversation.objects.create(student_id=SID)
    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor", lambda **kwargs: _result()
    )

    web_turn = run_advisor_turn(
        principal=_principal(),
        conversation=web,
        question="اعرض جدولي",
        idempotency_key="audit-web",
    )
    telegram_turn = run_advisor_turn(
        principal=_principal(),
        conversation=telegram,
        question="اعرض جدولي",
        idempotency_key="audit-telegram",
        channel_profile=TELEGRAM_SAFE_PROFILE,
    )

    assert web_turn.outcome == telegram_turn.outcome == CREATED
    assert web_turn.assistant_message is not None
    assert telegram_turn.assistant_message is not None
    assert web_turn.assistant_message.evidence_audit == _audit()
    assert telegram_turn.assistant_message.evidence_audit == _audit()
    assert web_turn.assistant_message.prompt_version == STUDENT_V2_PROMPT_VERSION
    assert web_turn.assistant_message.model_revision == "fp_actual_20260822"
    assert web_turn.student_message.evidence_audit == {}
    assert telegram_turn.student_message.evidence_audit == {}

    # The student-facing serializer remains an explicit whitelist and never emits
    # operator provenance or model/prompt revision metadata.
    shown = _message_json(web_turn.assistant_message)
    assert "evidence_audit" not in shown
    assert "prompt_version" not in shown
    assert "model_revision" not in shown


def test_old_and_manually_created_rows_have_a_safe_empty_default():
    conversation = AdvisorConversation.objects.create(student_id=SID)
    message = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="historical answer",
    )

    message.refresh_from_db()
    assert message.evidence_audit == {}


def test_the_build_side_rejects_a_plausible_snake_case_non_tool():
    """The whitelist is closed on purpose, and both sides must honour it.

    The module says why: «student_4400000» is a perfectly valid snake-case
    string and is not a tool name. Only the persistence side was tested, so the
    build side could be relaxed to a syntactic check - letting an opaque student
    reference into transient telemetry - with every test still green.
    """
    audit = build_evidence_audit(
        provider_evidence=[
            ("my_timetable", {"tool": "my_timetable", "ok": True}),
            ("student_4400000", {"tool": "student_4400000", "ok": True}),
            ("internal_debug_dump", {"secret": "x"}),
        ],
        validation_outcome="passed",
    )

    assert audit["tool_names"] == ["my_timetable"]
    assert [row["tool"] for row in audit["evidence_hashes"]] == ["my_timetable"]
    assert "4400000" not in json.dumps(audit)


@pytest.mark.parametrize(
    "prose",
    [
        "جدولك يحتوي على AI331 في الشعبة M6 وقد اجتزت CS111 بتقدير ممتاز",
        "The student passed MATH101 with 95 and is registered in AI331.",
        "has spaces and, punctuation!",
    ],
)
def test_the_revision_fields_take_a_token_or_nothing(prose: str):
    """They are documented as never carrying prose, and nothing checked it.

    Both normalisers could be reduced to truncating passthroughs undetected,
    which would drop the first 120 characters of an Arabic answer - grades and
    course codes included - into the audit column, the second academic-record
    store this module exists to avoid.
    """
    assert normalise_model_revision(prose) == ""
    assert normalise_prompt_version(prose) == ""
    # A real revision token still survives.
    assert normalise_model_revision("qwen3.7-plus/2026-08-01") == "qwen3.7-plus/2026-08-01"
    assert normalise_prompt_version(STUDENT_V2_PROMPT_VERSION) == STUDENT_V2_PROMPT_VERSION
    # Over-long tokens are refused rather than truncated into something new.
    assert normalise_prompt_version("v" * 41) == ""
    assert normalise_model_revision("v" * 121) == ""


def test_cost_counters_are_clamped_integers_never_content():
    """The cost block is counters only, re-whitelisted like everything else.

    A future caller that puts prose, floats-with-meaning, or negative numbers
    into the transient shape must find them clamped at persistence - the audit
    column stays free of content by construction, not by caller discipline.
    """
    audit = build_evidence_audit(
        provider_evidence=[("my_timetable", {"tool": "my_timetable", "ok": True})],
        validation_outcome="passed",
        inference_calls=3,
        prompt_tokens=1200,
        completion_tokens=450,
        turn_ms=15750,
    )
    assert audit["cost"] == {
        "inference_calls": 3,
        "prompt_tokens": 1200,
        "completion_tokens": 450,
        "turn_ms": 15750,
    }

    cleaned = normalise_evidence_audit(
        {
            **audit,
            "cost": {
                "inference_calls": "AI463 registered",
                "prompt_tokens": -50,
                "completion_tokens": 12.9,
                "turn_ms": 10**15,
                "note": "must vanish",
            },
        }
    )
    assert cleaned["cost"] == {
        "inference_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 12,
        "turn_ms": 1_000_000_000,
    }
    assert "note" not in cleaned["cost"]
    assert "AI463" not in json.dumps(cleaned)


def test_every_student_reachable_capability_is_auditable():
    """A turn's tool use must be recordable for every tool a student can reach.

    build_evidence_audit silently skips an unlisted name, so a capability
    outside this list persists tool_names: [] - indistinguishable from a
    no-tool turn, which blinds the quality screen and the weekly outcome
    series to exactly the turns most worth reading.
    """
    from core.services.advisor_evidence_audit import AUDITABLE_TOOL_NAMES
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import get_default_registry

    student_tools = {
        name
        for name, capability in get_default_registry().capabilities.items()
        if ROLE_STUDENT in capability.allowed_roles
    }
    assert student_tools - AUDITABLE_TOOL_NAMES == set()
