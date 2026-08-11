from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from django.test import override_settings

from core.models import AdvisorConversation, AdvisorMessage, Student
from core.services.advisor_channel_privacy import (
    TELEGRAM_SAFE_IDEMPOTENCY_PREFIX,
    TELEGRAM_SAFE_PROFILE,
    TELEGRAM_WITHHELD_TOOLS,
    project_history,
    project_tool_result,
    project_tool_schemas,
)
from core.services.advisor_history import load_profiled_history
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_turn import KEY_CONFLICT, run_advisor_turn
from core.services.llm_backend import ToolCallRequest, ToolChatResult
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import (
    answer_student_advisor,
    answer_student_advisor_v2,
    student_v2_tool_schemas,
)

pytestmark = pytest.mark.django_db
SID = 4909123


def _principal() -> AdvisorPrincipal:
    Student.objects.get_or_create(
        student_id=SID,
        defaults={"name": "Channel Student", "program": "CS", "section": "M"},
    )
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID)


def _tool_turn(name: str) -> ToolChatResult:
    call = ToolCallRequest(id="call-1", name=name, arguments={}, raw_arguments="{}")
    return ToolChatResult(
        content="",
        tool_calls=(call,),
        model="test-model",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
    )


def _answer_turn(text: str) -> ToolChatResult:
    return ToolChatResult(
        content=text,
        tool_calls=(),
        model="test-model",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        assistant_message={"role": "assistant", "content": text},
    )


class _Client:
    backend = "local"
    supports_assistant_prefill = True

    def __init__(self, *turns: ToolChatResult) -> None:
        self.turns = list(turns)
        self.schemas: list[list[dict[str, Any]]] = []
        self.messages: list[list[dict[str, Any]]] = []

    def resolve_model(self, requested_model=None):
        return requested_model or "test-model"

    def chat_with_tools(self, messages, *, tools, **kwargs):
        self.schemas.append(tools)
        self.messages.append(messages)
        return self.turns.pop(0)

    def chat(self, messages, **kwargs):
        raise AssertionError("forced fallback was not expected")


def test_telegram_profile_withholds_transcript_shaped_tools_and_shared_history():
    schemas = project_tool_schemas(student_v2_tool_schemas(), profile=TELEGRAM_SAFE_PROFILE)
    names = {schema["function"]["name"] for schema in schemas}

    assert not (names & TELEGRAM_WITHHELD_TOOLS)
    assert {"recommend_courses", "graduation_progress", "my_timetable"} <= names
    assert (
        project_history(
            [{"role": "assistant", "content": "Your old GPA was 2.86"}],
            profile=TELEGRAM_SAFE_PROFILE,
        )
        == []
    )
    safe_history = [
        {
            "role": "assistant",
            "content": "A safe timetable answer.",
            "channel_profile": TELEGRAM_SAFE_PROFILE,
        }
    ]
    assert project_history(safe_history, profile=TELEGRAM_SAFE_PROFILE) == safe_history


def test_telegram_projection_removes_exact_results_and_status_derived_policy():
    projected = project_tool_result(
        "graduation_progress",
        {
            "ok": True,
            "gpa": 2.86,
            "failed_results": [{"course_code": "DS341", "grade": "F", "mark": 42}],
            "credit_policy": {
                "qualification": {"applies_to": "GRADUATION EXPECTED"},
                "max_recommended_credit_hours": 18,
            },
            "courses": [
                {"course_code": "DS341", "status": "failed"},
                {"course_code": "CS211", "status": "studying"},
            ],
        },
        profile=TELEGRAM_SAFE_PROFILE,
    )
    encoded = json.dumps(projected, sort_keys=True).casefold()

    assert "2.86" not in encoded
    assert "grade" not in encoded and "mark" not in encoded
    assert "failed" not in encoded and "graduation expected" not in encoded
    assert projected["credit_policy"]["max_recommended_credit_hours"] == 18
    assert projected["courses"][1]["status"] == "studying"


def test_v2_model_sees_only_projected_tools_results_and_no_web_history(monkeypatch):
    client = _Client(_tool_turn("my_advisor"), _answer_turn("Your adviser is Dr Safe."))
    raw = {
        "tool": "my_advisor",
        "ok": True,
        "advisor_name": "Dr Safe",
        "gpa": 2.86,
        "failed_results": [{"course_code": "DS341", "grade": "F", "mark": 42}],
        "student_status": "ACADEMIC PROBATION",
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda *args, **kwargs: raw,
    )

    result = answer_student_advisor_v2(
        question="Who is my adviser?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        history=[{"role": "assistant", "content": "Your old GPA was 2.86"}],
        channel_profile=TELEGRAM_SAFE_PROFILE,
        llm_client=client,
    )

    schema_names = {row["function"]["name"] for row in client.schemas[0]}
    assert not (schema_names & TELEGRAM_WITHHELD_TOOLS)
    model_transcript = json.dumps(client.messages, ensure_ascii=False).casefold()
    assert "old gpa" not in model_transcript
    assert "2.86" not in model_transcript
    assert "ds341" not in model_transcript
    assert "academic probation" not in model_transcript
    result_evidence = json.dumps(result["agent"]["tool_results"]).casefold()
    assert "2.86" not in result_evidence and "failed" not in result_evidence


@override_settings(STUDENT_ADVISOR_V2_ENABLED=False)
def test_telegram_profile_never_downgrades_to_the_legacy_runtime(monkeypatch):
    expected = {"ok": True, "answer": "v2"}
    called: dict[str, Any] = {}

    def v2(**kwargs):
        called.update(kwargs)
        return expected

    monkeypatch.setattr("core.services.student_advisor_v2.answer_student_advisor_v2", v2)
    monkeypatch.setattr(
        "core.services.virtual_advisor.answer_virtual_advisor",
        lambda **kwargs: pytest.fail("legacy runtime was selected for Telegram"),
    )

    assert (
        answer_student_advisor(
            question="hello",
            principal=_principal(),
            channel_profile=TELEGRAM_SAFE_PROFILE,
        )
        is expected
    )
    assert called["channel_profile"] == TELEGRAM_SAFE_PROFILE


def test_client_supplied_idempotency_prefix_cannot_forge_safe_history(monkeypatch):
    """A web client controls its key, but never the server-owned profile field."""

    question = "Show my exact academic record"
    key = f"{TELEGRAM_SAFE_IDEMPOTENCY_PREFIX}forged-by-web"
    conversation = AdvisorConversation.objects.create(student_id=SID)
    web_question = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=question,
        idempotency_key=key,
        request_hash=hashlib.sha256(question.encode("utf-8")).hexdigest(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="Your exact GPA is 2.86.",
        in_reply_to=web_question,
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    assert (
        load_profiled_history(
            conversation,
            channel_profile=TELEGRAM_SAFE_PROFILE,
        )
        == []
    )

    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor",
        lambda **kwargs: pytest.fail("a cross-profile key collision reached the model"),
    )
    replay = run_advisor_turn(
        principal=_principal(),
        conversation=conversation,
        question=question,
        idempotency_key=key,
        channel_profile=TELEGRAM_SAFE_PROFILE,
    )

    assert replay.outcome == KEY_CONFLICT
    assert replay.assistant_message is None
