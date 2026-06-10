"""Agent-loop, capability-registry, and tool-calling client tests.

The legacy suite (tests/test_virtual_advisor.py) uses FakeClients WITHOUT
``chat_with_tools`` and therefore locks in the single-shot fallback path.
This file covers the new surfaces:

- core.services.virtual_advisor_capabilities (scope-guarded registry)
- core.services.virtual_advisor agent loop (iterations, dedup, budgets,
  fallback, grounding retry)
- core.services.local_llm.chat_with_tools parsing + HTTP 400 contract
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from core.models import Course, ProgrammeRequirement, Student, StudentCourse
from core.services.local_llm import (
    ChatResult,
    LocalLLMBadRequest,
    LocalLLMClient,
    LocalLLMUnavailable,
    ToolCallRequest,
    ToolChatResult,
)
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)
from core.services.virtual_advisor import answer_virtual_advisor
from core.services.virtual_advisor_capabilities import (
    AdvisorCapability,
    AdvisorCapabilityRegistry,
    _resolve_scoped_student_id,
    get_default_registry,
)

pytestmark = pytest.mark.django_db


# ── Helpers ──────────────────────────────────────────────────────


def _make_students() -> None:
    ai = Student.objects.create(
        student_id=6001001,
        name="Agent Loop AI Student",
        program="AI",
        section="F",
        advisor_id="A100",
        gpa=3.1,
        total_earned_credits=90,
    )
    Student.objects.create(
        student_id=6001002,
        name="Agent Loop CS Student",
        program="CS",
        section="M",
        advisor_id="A200",
        gpa=2.4,
        total_earned_credits=45,
    )
    course = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    StudentCourse.objects.create(student=ai, course=course, status="passed")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI331",
        course_name="Artificial Intelligence",
        type="Core",
        programme_term=5,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI431",
        course_name="Graduation Project",
        type="Core",
        programme_term=8,
        credit_hours=3,
    )


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def _tool_turn(
    tool_calls: tuple[ToolCallRequest, ...] = (),
    content: str = "",
) -> ToolChatResult:
    assistant: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        assistant["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in tool_calls
        ]
    return ToolChatResult(
        content=content,
        tool_calls=tool_calls,
        model="fake-tools",
        usage={"total_tokens": 10},
        raw={},
        assistant_message=assistant,
    )


class FakeToolClient:
    """Scripted tool-calling client.

    ``turns`` is a list of ToolChatResult returned by successive
    ``chat_with_tools`` calls (last entry repeats if exhausted).
    ``chat`` (plain) returns ``plain_answers`` in order.
    """

    def __init__(
        self,
        turns: list[ToolChatResult],
        plain_answers: list[str] | None = None,
        reject_tools: bool = False,
    ) -> None:
        self.turns = list(turns)
        self.plain_answers = list(plain_answers or ["plain fallback answer"])
        self.reject_tools = reject_tools
        self.tool_messages_seen: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []
        self.chat_calls: list[list[dict[str, Any]]] = []
        self._turn_idx = 0
        self._plain_idx = 0

    def resolve_model(self, requested_model=None):
        return requested_model or "fake-tools"

    def chat_with_tools(
        self,
        messages,
        *,
        tools,
        model=None,
        temperature=0.2,
        max_tokens=None,
        tool_choice="auto",
        timeout_seconds=None,
    ):
        if self.reject_tools:
            raise LocalLLMBadRequest("tools are not supported by this model")
        self.tool_messages_seen.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        self.last_max_tokens = max_tokens
        self.last_timeout_seconds = timeout_seconds
        turn = self.turns[min(self._turn_idx, len(self.turns) - 1)]
        self._turn_idx += 1
        return turn

    def chat(
        self, messages, *, model=None, temperature=0.2, max_tokens=None, assistant_prefill=None
    ):
        self.chat_calls.append([dict(m) for m in messages])
        answer = self.plain_answers[min(self._plain_idx, len(self.plain_answers) - 1)]
        self._plain_idx += 1
        return ChatResult(content=answer, model="fake-tools", usage={}, raw={})


# ── Capability registry: scope filtering ────────────────────────


def test_registry_filters_tools_by_role() -> None:
    registry = get_default_registry()

    student_tools = {
        schema["function"]["name"]
        for schema in registry.tool_schemas_for_scope({"role": ROLE_STUDENT, "student_id": 1})
    }
    assert "find_students" in student_tools
    assert "get_student_context" in student_tools
    assert "lookup_course" in student_tools
    assert "recommend_courses" in student_tools
    # Program-level and portfolio tools are staff-only.
    assert "course_eligibility" not in student_tools
    assert "aggregate_demand" not in student_tools
    assert "graduation_shortfall" not in student_tools
    assert "portfolio_triage" not in student_tools

    super_tools = {
        schema["function"]["name"]
        for schema in registry.tool_schemas_for_scope({"role": ROLE_SUPER_ADMIN})
    }
    assert {
        "find_students",
        "get_student_context",
        "lookup_course",
        "recommend_courses",
        "course_eligibility",
        "graduation_shortfall",
        "portfolio_triage",
        "aggregate_demand",
    } <= super_tools


def test_registry_execute_rejects_unknown_and_denied_tools() -> None:
    registry = get_default_registry()

    unknown = registry.execute("drop_tables", {}, scope={"role": ROLE_SUPER_ADMIN})
    assert unknown["ok"] is False
    assert "Unknown" in unknown["error"]

    denied = registry.execute("aggregate_demand", {}, scope={"role": ROLE_STUDENT, "student_id": 1})
    assert denied["ok"] is False
    assert "not allowed" in denied["error"]


def test_registry_execute_catches_executor_exceptions() -> None:
    registry = AdvisorCapabilityRegistry()

    def _boom(args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("executor exploded")

    registry.register(
        AdvisorCapability(
            name="explosive",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            allowed_roles=frozenset({ROLE_SUPER_ADMIN}),
            executor=_boom,
        )
    )
    result = registry.execute("explosive", {}, scope={"role": ROLE_SUPER_ADMIN})
    assert result["ok"] is False
    assert "failed" in result["error"]


def test_registry_refuses_mutating_capability() -> None:
    registry = AdvisorCapabilityRegistry()
    with pytest.raises(ValueError):
        registry.register(
            AdvisorCapability(
                name="writer",
                description="mutates",
                parameters={"type": "object", "properties": {}},
                allowed_roles=frozenset({ROLE_SUPER_ADMIN}),
                executor=lambda a, s, c: {"ok": True},
                read_only=False,
            )
        )


# ── Capability registry: identity scoping ───────────────────────


def test_student_scope_can_only_read_self() -> None:
    _make_students()

    own, error = _resolve_scoped_student_id({}, {"role": ROLE_STUDENT, "student_id": 6001001})
    assert error is None
    assert own == 6001001

    _denied, error = _resolve_scoped_student_id(
        {"student_id": 6001002}, {"role": ROLE_STUDENT, "student_id": 6001001}
    )
    assert error is not None
    assert "own records" in error


def test_advisor_scope_limited_to_portfolio() -> None:
    _make_students()

    allowed, error = _resolve_scoped_student_id(
        {"student_id": 6001001}, {"role": ROLE_ADVISOR, "advisor_id": "A100"}
    )
    assert error is None
    assert allowed == 6001001

    _denied, error = _resolve_scoped_student_id(
        {"student_id": 6001002}, {"role": ROLE_ADVISOR, "advisor_id": "A100"}
    )
    assert error is not None
    assert "portfolio" in error


def test_general_advisor_scope_limited_to_departments() -> None:
    _make_students()

    allowed, error = _resolve_scoped_student_id(
        {"student_id": 6001001},
        {"role": ROLE_GENERAL_ADVISOR, "departments": ["AI", "DS"]},
    )
    assert error is None
    assert allowed == 6001001

    _denied, error = _resolve_scoped_student_id(
        {"student_id": 6001002},
        {"role": ROLE_GENERAL_ADVISOR, "departments": ["AI", "DS"]},
    )
    assert error is not None
    assert "department" in error


def test_portfolio_triage_forces_own_advisor_id() -> None:
    _make_students()
    registry = get_default_registry()

    result = registry.execute(
        "portfolio_triage",
        {"advisor_id": "A200"},  # tries to read another advisor's portfolio
        scope={"role": ROLE_ADVISOR, "advisor_id": "A100"},
    )
    # forced_advisor_id makes the service refuse mismatched portfolios; the
    # executor pins advisor_id to the caller's own id so the call succeeds
    # but only ever returns the caller's students.
    assert result["ok"] is True
    ids = {row["student_id"] for row in result["students_sample"]}
    assert ids == {6001001}


def test_lookup_course_resolves_names_and_codes() -> None:
    _make_students()
    registry = get_default_registry()

    by_name = registry.execute(
        "lookup_course", {"query": "Graduation"}, scope={"role": ROLE_STUDENT, "student_id": 1}
    )
    assert by_name["ok"] is True
    codes = {row["course_code"] for row in by_name["courses"]}
    assert "AI431" in codes

    by_code = registry.execute(
        "lookup_course", {"query": "ai331"}, scope={"role": ROLE_STUDENT, "student_id": 1}
    )
    assert {row["course_code"] for row in by_code["courses"]} >= {"AI331"}


def test_get_student_context_tool_respects_scope() -> None:
    _make_students()
    registry = get_default_registry()

    own = registry.execute(
        "get_student_context",
        {},
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert own["ok"] is True
    assert own["student_context"]["student"]["student_id"] == 6001001

    other = registry.execute(
        "get_student_context",
        {"student_id": 6001002},
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
    )
    assert other["ok"] is False


# ── Agent loop ───────────────────────────────────────────────────


def test_agent_loop_executes_tool_then_answers() -> None:
    _make_students()
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}),)),
            _tool_turn(content="Found 1 AI student: 6001001."),
        ]
    )

    result = answer_virtual_advisor(
        question="How is the AI cohort doing?",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    assert result["answer"] == "Found 1 AI student: 6001001."
    agent = result["agent"]
    assert agent["loop_used"] is True
    assert agent["iterations"] == 2
    assert [t["name"] for t in agent["tools_called"]] == ["find_students"]
    assert agent["tools_called"][0]["ok"] is True
    assert agent["tool_results"][0]["count"] == 1

    # The second model turn must have seen the tool result message.
    second_turn_messages = fake.tool_messages_seen[1]
    tool_messages = [m for m in second_turn_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert "6001001" in tool_messages[0]["content"]


def test_agent_loop_deduplicates_identical_calls() -> None:
    _make_students()
    repeated = _tool_call("find_students", {"program": "AI"})
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(repeated,)),
            _tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}, "call_2"),)),
            _tool_turn(content="done"),
        ]
    )

    result = answer_virtual_advisor(
        question="AI students overview please",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    agent = result["agent"]
    # Only ONE real execution; the duplicate reused the cached result.
    assert len(agent["tools_called"]) == 1
    assert len(agent["tool_results"]) == 1
    assert result["answer"] == "done"


def test_agent_loop_iteration_cap_forces_final_answer(settings) -> None:
    _make_students()
    settings.VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS = 3
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}),))],
        plain_answers=["Forced final answer from gathered evidence."],
    )

    result = answer_virtual_advisor(
        question="Keep digging into AI students",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    agent = result["agent"]
    assert agent["iterations"] == 3
    assert agent["forced_final"] is True
    assert result["answer"] == "Forced final answer from gathered evidence."
    # The forced-final chat receives the no-more-tools instruction.
    final_messages = fake.chat_calls[0]
    assert "Do not request more tools" in final_messages[-1]["content"]


def test_agent_loop_falls_back_when_model_rejects_tools() -> None:
    fake = FakeToolClient(turns=[], reject_tools=True, plain_answers=["single-shot answer"])

    result = answer_virtual_advisor(
        question="hello there",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    agent = result["agent"]
    assert agent["loop_used"] is False
    assert agent["fallback_reason"] == "tools_rejected_by_model"
    assert result["answer"] == "single-shot answer"


def test_agent_loop_disabled_by_flag(settings) -> None:
    settings.VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED = False
    fake = FakeToolClient(
        turns=[_tool_turn(content="loop answer — should not appear")],
        plain_answers=["flag-off single-shot answer"],
    )

    result = answer_virtual_advisor(
        question="hello there",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    assert result["answer"] == "flag-off single-shot answer"
    assert result["agent"]["enabled"] is False
    assert result["agent"]["loop_used"] is False
    assert fake.tool_messages_seen == []  # chat_with_tools never invoked


def test_student_scope_gets_student_tool_schemas_only() -> None:
    _make_students()
    fake = FakeToolClient(
        turns=[_tool_turn(content="ok")],
    )

    answer_virtual_advisor(
        question="what should I take?",
        student_id=6001001,
        academic_year=1448,
        term=1,
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=fake,
    )

    sent_tools = {schema["function"]["name"] for schema in fake.tools_seen[0]}
    assert "portfolio_triage" not in sent_tools
    assert "aggregate_demand" not in sent_tools
    assert "get_student_context" in sent_tools


def test_grounding_retry_on_unverified_student_id() -> None:
    _make_students()
    fake = FakeToolClient(
        turns=[_tool_turn(content="You should contact student 9876543 about this.")],
        plain_answers=["The evidence does not name any such student."],
    )

    result = answer_virtual_advisor(
        question="who should I contact?",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    assert result["agent"]["grounding_retry"] is True
    assert result["answer"] == "The evidence does not name any such student."
    correction = fake.chat_calls[0][-1]["content"]
    assert "9876543" in correction


def test_agent_loop_survives_mid_loop_turn_failure() -> None:
    """A timeout/reasoning-budget failure on a tool turn must degrade to a
    forced final answer, never a 503."""
    _make_students()

    class FlakyToolClient(FakeToolClient):
        def chat_with_tools(self, messages, **kwargs):
            if self._turn_idx >= 1:
                self._turn_idx += 1
                raise LocalLLMUnavailable("Local LLM request timed out.")
            return super().chat_with_tools(messages, **kwargs)

    fake = FlakyToolClient(
        turns=[_tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}),))],
        plain_answers=["Recovered answer from evidence gathered before the timeout."],
    )

    result = answer_virtual_advisor(
        question="how are AI students doing?",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    agent = result["agent"]
    assert agent["loop_used"] is True
    assert agent["forced_final"] is True
    assert "timed out" in agent["turn_error"]
    assert result["answer"] == "Recovered answer from evidence gathered before the timeout."
    # Evidence from the successful first turn is preserved.
    assert agent["tool_results"][0]["count"] == 1


def test_loop_mode_skips_regex_seed_and_mirrors_agent_evidence() -> None:
    """In loop mode the regex planner must NOT seed the context (token
    bloat + misleading samples); ``tool_results`` mirrors agent evidence."""
    _make_students()
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}),)),
            _tool_turn(content="done"),
        ]
    )

    # This wording triggers the legacy regex planner ("find", "students").
    result = answer_virtual_advisor(
        question="find AI students with 90 credit hours",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    # Context sent to the model carries no seeded tool_results blob.
    first_turn_user = fake.tool_messages_seen[0][-1]["content"]
    assert '"tool_results"' not in first_turn_user
    # Response evidence mirrors what the agent actually fetched.
    assert result["tool_results"] == result["agent"]["tool_results"]
    assert result["tool_results"][0]["tool"] == "find_students"


def test_year_term_default_when_caller_omits_them() -> None:
    """WhatsApp callers pass no academic year/term; the service must
    default them so time-dependent capabilities work."""
    _make_students()
    fake = FakeToolClient(turns=[_tool_turn(content="ok")])

    result = answer_virtual_advisor(
        question="what should I take?",
        student_id=6001001,
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=fake,
    )

    term_context = result["verified_context"]["term_context"]
    assert term_context["academic_year"] is not None
    assert term_context["term"] is not None


def test_course_prerequisites_capability() -> None:
    _make_students()
    from core.models import Prerequisite

    Prerequisite.objects.create(program="AI", course_code="AI431", prerequisite_course_code="AI331")
    registry = get_default_registry()

    explicit = registry.execute(
        "course_prerequisites",
        {"course_code": "AI431", "program": "AI"},
        scope={"role": ROLE_SUPER_ADMIN},
    )
    assert explicit["ok"] is True
    assert explicit["per_program"][0]["program"] == "AI"
    assert "AI331" in explicit["per_program"][0]["prerequisites"]
    assert explicit["per_program"][0]["programme_term"] == 8

    # Student scope: program defaults to the student's own program.
    own = registry.execute(
        "course_prerequisites",
        {"course_code": "AI431"},
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
    )
    assert own["ok"] is True
    assert own["per_program"][0]["program"] == "AI"


def test_year_term_sanity_guard_rejects_gregorian_years() -> None:
    """Live testing caught the model passing academic_year=2024 (Gregorian);
    the registry must fall back to the configured Hijri defaults."""
    _make_students()
    registry = get_default_registry()

    result = registry.execute(
        "recommend_courses",
        {"academic_year": 2024, "term": 7},
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert result["ok"] is True
    assert result["academic_year"] == 1448
    assert result["term"] == 1


def test_loop_passes_budgets_to_tool_turns() -> None:
    _make_students()
    fake = FakeToolClient(turns=[_tool_turn(content="ok")])

    answer_virtual_advisor(
        question="hello",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    assert fake.last_max_tokens == 3000
    assert fake.last_timeout_seconds == 75.0


def test_find_students_name_contains_filter() -> None:
    _make_students()
    registry = get_default_registry()

    result = registry.execute(
        "find_students",
        {"name_contains": "Agent Loop AI"},
        scope={"role": ROLE_SUPER_ADMIN},
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["students"][0]["student_id"] == 6001001
    assert result["filters"]["name_contains"] == "Agent Loop AI"


def test_find_students_caps_rows_and_adds_summary_stats() -> None:
    Student.objects.bulk_create(
        Student(
            student_id=6100000 + i,
            name=f"Bulk Student {i}",
            program="AI",
            section="M",
            gpa=1.5 if i < 5 else 3.5,
            total_earned_credits=60 + i,
        )
        for i in range(40)
    )
    registry = get_default_registry()

    result = registry.execute(
        "find_students",
        {"program": "AI", "limit": 200},
        scope={"role": ROLE_SUPER_ADMIN},
    )
    assert result["count"] == 40
    # Message payload is capped; the omission is explicit.
    assert len(result["students"]) == 30
    assert result["students_omitted"] == 10
    stats = result["summary_stats"]
    assert stats["rows_in_stats"] == 40
    assert stats["gpa_below_2_count"] == 5
    assert stats["gpa_min"] == 1.5
    assert stats["gpa_max"] == 3.5


def test_student_context_includes_programme_totals() -> None:
    _make_students()
    registry = get_default_registry()

    result = registry.execute(
        "get_student_context",
        {},
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        ctx={"academic_year": 1448, "term": 1},
    )
    totals = result["student_context"]["course_evidence"]["programme_totals"]
    # Plan = AI331 (3cr, passed) + AI431 (3cr, remaining).
    assert totals["total_plan_credit_hours"] == 6
    assert totals["remaining_credit_hours"] == 3
    assert totals["remaining_course_count"] == 1


def test_answer_language_pinned_in_user_message() -> None:
    _make_students()
    fake = FakeToolClient(turns=[_tool_turn(content="ok")])
    answer_virtual_advisor(
        question="كم ساعة باقي علي؟",
        student_id=6001001,
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=fake,
    )
    assert "answer_language: Arabic" in fake.tool_messages_seen[0][-1]["content"]

    fake_en = FakeToolClient(turns=[_tool_turn(content="ok")])
    answer_virtual_advisor(
        question="how many hours left?",
        student_id=6001001,
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=fake_en,
    )
    assert "answer_language: English" in fake_en.tool_messages_seen[0][-1]["content"]


def test_portfolio_triage_missing_advisor_hint_points_to_find_students() -> None:
    registry = get_default_registry()
    result = registry.execute(
        "portfolio_triage",
        {"search": "سارة"},
        scope={"role": ROLE_SUPER_ADMIN},
    )
    assert result["ok"] is False
    assert "find_students" in result["error"]
    assert "name_contains" in result["error"]


def test_grounded_student_id_does_not_trigger_retry() -> None:
    _make_students()
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(_tool_call("find_students", {"program": "AI"}),)),
            _tool_turn(content="Student 6001001 is on track."),
        ]
    )

    result = answer_virtual_advisor(
        question="How is the AI cohort?",
        scope={"role": ROLE_SUPER_ADMIN},
        client=fake,
    )

    assert result["agent"]["grounding_retry"] is False
    assert result["answer"] == "Student 6001001 is on track."
    assert fake.chat_calls == []  # no plain-chat retry happened


# ── local_llm.chat_with_tools ────────────────────────────────────


def _client_with_fake_response(
    monkeypatch, response: dict[str, Any]
) -> tuple[LocalLLMClient, dict]:
    client = LocalLLMClient(base_url="http://localhost:1234/v1")
    captured: dict[str, Any] = {}

    def fake_request(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return response

    monkeypatch.setattr(client, "_request", fake_request)
    return client, captured


def test_chat_with_tools_sends_tools_and_parses_tool_calls(monkeypatch) -> None:
    response = {
        "model": "qwen-test",
        "usage": {"total_tokens": 42},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "find_students",
                                "arguments": '{"program": "AI", "min_gpa": 3}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    client, captured = _client_with_fake_response(monkeypatch, response)
    tools = [{"type": "function", "function": {"name": "find_students", "parameters": {}}}]

    result = client.chat_with_tools(
        [{"role": "user", "content": "hi"}], tools=tools, model="qwen-test"
    )

    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert result.content == ""
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "find_students"
    assert call.arguments == {"program": "AI", "min_gpa": 3}
    assert result.assistant_message["tool_calls"][0]["id"] == "call_abc"


def test_chat_with_tools_handles_malformed_arguments(monkeypatch) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "find_students", "arguments": "{not json"},
                        }
                    ],
                }
            }
        ],
    }
    client, _captured = _client_with_fake_response(monkeypatch, response)

    result = client.chat_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "find_students", "parameters": {}}}],
        model="m",
    )

    assert result.tool_calls[0].arguments == {}
    assert result.tool_calls[0].raw_arguments == "{not json"


def test_chat_with_tools_raises_on_empty_turn(monkeypatch) -> None:
    response = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    client, _captured = _client_with_fake_response(monkeypatch, response)

    with pytest.raises(LocalLLMUnavailable):
        client.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            tools=[],
            model="m",
        )


def test_http_400_raises_bad_request(monkeypatch) -> None:
    def fake_urlopen(request, timeout=0):
        raise HTTPError(
            "http://localhost:1234/v1/chat/completions",
            400,
            "Bad Request",
            None,
            io.BytesIO(b'{"error": "tools not supported"}'),
        )

    monkeypatch.setattr("core.services.local_llm.urlopen", fake_urlopen)
    client = LocalLLMClient(base_url="http://localhost:1234/v1")

    with pytest.raises(LocalLLMBadRequest):
        client._request("POST", "/chat/completions", {"model": "m"})
