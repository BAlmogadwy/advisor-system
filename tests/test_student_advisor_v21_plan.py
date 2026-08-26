from __future__ import annotations

import json
from typing import Any

import pytest

from core.services.llm_backend import ToolCallRequest, ToolChatResult
from core.services.student_advisor_v21_plan import (
    EVIDENCE_BACKED_REQUEST_OUTCOMES,
    TURN_PLAN_TOOL_NAME,
    UNSUPPORTED_REQUEST_OUTCOMES,
    ArgumentProvenanceContract,
    ArgumentProvenanceMode,
    ArgumentProvenanceRule,
    ClarificationKind,
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
    TurnPlanProvenanceError,
    TurnPlanSchemaError,
    TurnPlanValidationError,
    build_plan_repair_message,
    build_turn_plan_tool_schema,
    normalise_provenance_identifier,
    normalise_provenance_text,
    parse_turn_plan_result,
    plan_student_turn,
    synthesize_tool_calls,
    synthesize_tool_chat_result,
    validate_capability_argument_provenance,
    validate_capability_arguments,
    validate_plan_argument_provenance,
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_course",
            "description": "Resolve one course name or code.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "course_choice_comparison",
            "description": "Compare two to four exact course codes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "course_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    }
                },
                "required": ["course_codes"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "my_progress",
            "description": "Return verified degree progress.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def test_request_outcome_vocabulary_is_closed_and_control_sets_are_disjoint() -> None:
    assert [outcome.value for outcome in StudentRequestOutcome] == [
        "course_catalogue",
        "course_eligibility",
        "prerequisite_information",
        "available_courses",
        "course_priority",
        "course_recommendation",
        "course_addition",
        "course_drop_impact",
        "degree_progress",
        "degree_plan",
        "current_timetable",
        "timetable_review",
        "timetable_build",
        "timetable_feasibility",
        "course_comparison",
        "course_replacement",
        "graduation_forecast",
        "graduation_impact",
        "credit_load_comparison",
        "policy_rule",
        "academic_adviser",
        "prior_result",
        "registration_action",
        "general_conversation",
        "unsupported_request",
    ]
    assert UNSUPPORTED_REQUEST_OUTCOMES == {
        StudentRequestOutcome.REGISTRATION_ACTION,
        StudentRequestOutcome.CREDIT_LOAD_COMPARISON,
        StudentRequestOutcome.UNSUPPORTED_REQUEST,
    }
    assert not EVIDENCE_BACKED_REQUEST_OUTCOMES & UNSUPPORTED_REQUEST_OUTCOMES
    assert StudentRequestOutcome.GENERAL_CONVERSATION not in (
        EVIDENCE_BACKED_REQUEST_OUTCOMES | UNSUPPORTED_REQUEST_OUTCOMES
    )
    assert EVIDENCE_BACKED_REQUEST_OUTCOMES | UNSUPPORTED_REQUEST_OUTCOMES | {
        StudentRequestOutcome.GENERAL_CONVERSATION
    } == set(StudentRequestOutcome)


def _tool_result(
    raw_arguments: str,
    *,
    name: str = TURN_PLAN_TOOL_NAME,
    content: str = "",
    parsed_arguments: dict[str, Any] | None = None,
) -> ToolChatResult:
    call = ToolCallRequest(
        id="plan_call_1",
        name=name,
        arguments=parsed_arguments or {},
        raw_arguments=raw_arguments,
    )
    return ToolChatResult(
        content=content,
        tool_calls=(call,),
        model="planner-model",
        usage={"total_tokens": 17},
        assistant_message={
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_arguments},
                }
            ],
        },
        model_revision="planner-r1",
    )


def _raw_plan(
    *,
    decision: str = "execute",
    outcomes: list[str] | None = None,
    requests: list[dict[str, Any]] | None = None,
    clarification: str = "",
    clarification_kind: str | None = None,
) -> str:
    if outcomes is None:
        outcomes = {
            "direct": ["general_conversation"],
            "unsupported": ["unsupported_request"],
        }.get(decision, ["course_catalogue"])
    if requests is None:
        requests = [{"capability": "lookup_course", "arguments": {"query": "AI331"}}]
    if clarification_kind is None:
        clarification_kind = "generic" if decision == "clarify" else "none"
    return json.dumps(
        {
            "decision": decision,
            "requested_outcomes": outcomes,
            "evidence_requests": requests,
            "clarification_kind": clarification_kind,
            "clarification_question": clarification,
        },
        ensure_ascii=False,
    )


def test_schema_is_one_bounded_meta_tool_over_only_the_advertised_capabilities() -> None:
    schema = build_turn_plan_tool_schema(TOOLS[:2], max_calls=3)

    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == TURN_PLAN_TOOL_NAME
    parameters = function["parameters"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == {
        "decision",
        "requested_outcomes",
        "evidence_requests",
        "clarification_kind",
        "clarification_question",
    }
    assert parameters["properties"]["decision"]["enum"] == [
        "execute",
        "clarify",
        "direct",
        "unsupported",
    ]
    assert parameters["properties"]["clarification_kind"]["enum"] == [
        kind.value for kind in ClarificationKind
    ]
    outcomes = parameters["properties"]["requested_outcomes"]
    assert outcomes["minItems"] == 1
    assert outcomes["maxItems"] == len(StudentRequestOutcome)
    assert outcomes["uniqueItems"] is True
    assert outcomes["items"]["enum"] == [outcome.value for outcome in StudentRequestOutcome]
    requests = parameters["properties"]["evidence_requests"]
    assert requests["maxItems"] == 3
    assert requests["items"]["properties"]["capability"]["enum"] == [
        "lookup_course",
        "course_choice_comparison",
    ]
    assert "lookup_course" in function["description"]
    assert "Resolve one course name or code." in function["description"]
    assert "arguments_schema" not in function["description"]
    branches = requests["items"]["oneOf"]
    assert len(branches) == 2
    by_capability = {
        branch["properties"]["capability"]["enum"][0]: branch["properties"]["arguments"]
        for branch in branches
    }
    assert set(by_capability) == {"lookup_course", "course_choice_comparison"}
    assert by_capability["lookup_course"]["required"] == ["query"]
    assert by_capability["lookup_course"]["additionalProperties"] is False
    assert by_capability["course_choice_comparison"]["required"] == ["course_codes"]
    assert "clarification_question MUST be the empty string" in function["description"]
    assert "every distinct deliverable" in function["description"]
    assert "registration_action" in function["description"]
    assert "credit_load_comparison" in function["description"]
    assert "unsupported_request" in function["description"]
    assert (
        parameters["properties"]["clarification_question"]["description"]
        == "A concise question only when decision is clarify; otherwise this MUST be the "
        "empty string."
    )


@pytest.mark.parametrize("max_calls", [0, -1, True, 1.5])
def test_schema_refuses_an_invalid_call_budget(max_calls: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_turn_plan_tool_schema(TOOLS, max_calls=max_calls)


def test_schema_refuses_duplicate_or_malformed_advertised_contracts() -> None:
    with pytest.raises(TurnPlanSchemaError, match="unique"):
        build_turn_plan_tool_schema([TOOLS[0], TOOLS[0]], max_calls=2)
    malformed = {
        "type": "function",
        "function": {"name": "broken", "parameters": {"type": "array"}},
    }
    with pytest.raises(TurnPlanSchemaError, match="object parameter"):
        build_turn_plan_tool_schema([malformed], max_calls=2)


def test_forced_planner_call_uses_the_singleton_contract_and_returns_provider_metadata() -> None:
    provider_turn = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []
            self.kwargs: dict[str, Any] = {}

        def chat_with_tools(
            self,
            messages: list[dict[str, Any]],
            **kwargs: Any,
        ) -> ToolChatResult:
            self.messages = messages
            self.kwargs = kwargs
            return provider_turn

    client = Client()
    source_messages = [{"role": "user", "content": "Tell me about AI331"}]

    result = plan_student_turn(
        client,
        source_messages,
        advertised_tools=TOOLS,
        max_calls=4,
        model="planner-model",
        max_tokens=700,
        timeout_seconds=12.5,
        deadline_monotonic=99.0,
    )

    assert result.plan.evidence_requests[0].capability == "lookup_course"
    assert result.provider_turn is provider_turn
    assert client.kwargs["tool_choice"] == "required"
    assert client.kwargs["temperature"] == 0.0
    assert client.kwargs["model"] == "planner-model"
    assert client.kwargs["max_tokens"] == 700
    assert client.kwargs["timeout_seconds"] == 12.5
    assert client.kwargs["deadline_monotonic"] == 99.0
    assert len(client.kwargs["tools"]) == 1
    assert client.kwargs["tools"][0]["function"]["name"] == TURN_PLAN_TOOL_NAME
    assert client.messages == source_messages
    assert client.messages is not source_messages


def test_planner_repairs_one_nested_schema_failure_without_replaying_raw_output() -> None:
    invalid = _tool_result(
        _raw_plan(
            requests=[
                {
                    "capability": "lookup_course",
                    "arguments": {"query": "AI331", "rogue": True},
                }
            ]
        )
    )
    valid = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.turns = [invalid, valid]
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return self.turns.pop(0)

    client = Client()
    result = plan_student_turn(
        client,
        [{"role": "user", "content": "Tell me about AI331"}],
        advertised_tools=TOOLS,
        max_calls=2,
    )

    assert result.plan.evidence_requests == (
        PlannedCapabilityCall("lookup_course", {"query": "AI331"}),
    )
    assert result.provider_turn is valid
    assert result.provider_turns == (invalid, valid)
    assert len(client.calls) == 2
    assert client.calls[1][-1] == build_plan_repair_message(
        "plan_validation_failed",
        {},
        advertised_tools=TOOLS,
    )
    repair_message = client.calls[1][-1]["content"]
    assert "unexpected properties" not in repair_message
    assert "rogue" not in repair_message
    assert "Tell me about AI331" in client.calls[1][0]["content"]


def test_final_nested_schema_failure_retains_both_provider_turns_for_accounting() -> None:
    invalid = _tool_result(
        _raw_plan(
            requests=[
                {
                    "capability": "lookup_course",
                    "arguments": {"query": "AI331", "secret_rogue": True},
                }
            ]
        )
    )

    class Client:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return invalid

    client = Client()
    with pytest.raises(TurnPlanValidationError) as raised:
        plan_student_turn(
            client,
            [{"role": "user", "content": "Tell me about AI331"}],
            advertised_tools=TOOLS,
            max_calls=2,
        )

    assert raised.value.provider_turns == (invalid, invalid)
    assert len(client.calls) == 2
    assert "secret_rogue" not in client.calls[1][-1]["content"]


def test_provenance_repair_instruction_is_closed_and_does_not_replay_a_plan() -> None:
    valid = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return valid

    client = Client()
    result = plan_student_turn(
        client,
        [{"role": "user", "content": "Tell me about AI331"}],
        advertised_tools=TOOLS,
        max_calls=2,
        max_attempts=1,
        repair_reason="argument_provenance_failed",
    )

    assert result.provider_turns == (valid,)
    repair_message = client.calls[0][-1]["content"]
    assert "not grounded" in repair_message
    assert "AI331" not in repair_message
    assert "evidence_requests" not in repair_message


def test_coverage_repair_can_include_only_closed_server_diagnostics() -> None:
    valid = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return valid

    client = Client()
    plan_student_turn(
        client,
        [{"role": "user", "content": "Tell me about AI331"}],
        advertised_tools=TOOLS,
        max_calls=2,
        max_attempts=1,
        repair_reason="outcome_coverage_failed",
        repair_details={
            "coverage_reason": ["requested_entity_uncovered"],
            "uncovered_outcomes": ["course_catalogue"],
            "uncovered_course_codes": ["AI331"],
            "redundant_capabilities": ["lookup_course"],
        },
    )

    repair_message = client.calls[0][-1]["content"]
    assert "coverage_reason=requested_entity_uncovered" in repair_message
    assert "uncovered_outcomes=course_catalogue" in repair_message
    assert "uncovered_course_codes=AI331" in repair_message
    assert "redundant_capabilities=lookup_course" in repair_message
    assert "evidence_requests" not in repair_message


def test_coverage_repair_rejects_open_ended_diagnostics_before_provider_call() -> None:
    class Client:
        def chat_with_tools(self, *_args, **_kwargs):
            pytest.fail("open-ended repair details must not reach the provider")

    with pytest.raises(ValueError, match="closed field"):
        plan_student_turn(
            Client(),
            [{"role": "user", "content": "Tell me about AI331"}],
            advertised_tools=TOOLS,
            max_calls=2,
            max_attempts=1,
            repair_reason="outcome_coverage_failed",
            repair_details={"raw_invalid_plan": ["do not replay this"]},
        )


def test_constraint_repair_receives_only_closed_field_paths() -> None:
    valid = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return valid

    client = Client()
    plan_student_turn(
        client,
        [{"role": "user", "content": "Show my top five courses"}],
        advertised_tools=TOOLS,
        max_calls=2,
        max_attempts=1,
        repair_reason="constraint_coverage_failed",
        repair_details={"missing_field_paths": ["my_progress.priority_limit"]},
    )

    repair_message = client.calls[0][-1]["content"]
    assert "missing_field_paths=my_progress.priority_limit" in repair_message
    assert "evidence_requests" not in repair_message
    assert "priority_limit" in repair_message
    assert "five" not in repair_message


def test_semantic_policy_repair_receives_only_closed_policy_ids() -> None:
    valid = _tool_result(_raw_plan())

    class Client:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        def chat_with_tools(self, messages, **_kwargs):
            self.calls.append(messages)
            return valid

    client = Client()
    plan_student_turn(
        client,
        [{"role": "user", "content": "private original turn"}],
        advertised_tools=TOOLS,
        max_calls=2,
        max_attempts=1,
        repair_reason="semantic_policy_failed",
        repair_details={"policy_ids": ["single_course_choice_balanced"]},
    )

    repair_message = client.calls[0][-1]["content"]
    assert "policy_ids=single_course_choice_balanced" in repair_message
    assert "private original turn" not in repair_message
    assert "rejected plan" in repair_message


@pytest.mark.parametrize(
    "details",
    [
        {"policy_ids": ["DS341-M2"]},
        {"policy_ids": ["single_course_choice_balanced=DS341"]},
        {"raw_arguments": ["single_course_choice_balanced"]},
    ],
)
def test_semantic_policy_repair_rejects_values_and_open_fields(
    details: dict[str, list[str]],
) -> None:
    class Client:
        def chat_with_tools(self, *_args, **_kwargs):
            pytest.fail("invalid policy repair details must not reach the provider")

    with pytest.raises(ValueError):
        plan_student_turn(
            Client(),
            [{"role": "user", "content": "private original turn"}],
            advertised_tools=TOOLS,
            max_calls=2,
            max_attempts=1,
            repair_reason="semantic_policy_failed",
            repair_details=details,
        )


@pytest.mark.parametrize(
    "details",
    [
        {"missing_field_paths": ["my_progress.priority_limit=5"]},
        {"missing_field_paths": ["lookup_course.query"]},
        {"raw_rejected_arguments": ["DS341/M2"]},
    ],
)
def test_constraint_repair_rejects_values_and_open_paths_before_provider_call(
    details: dict[str, list[str]],
) -> None:
    class Client:
        def chat_with_tools(self, *_args, **_kwargs):
            pytest.fail("open-ended constraint details must not reach the provider")

    with pytest.raises(ValueError):
        plan_student_turn(
            Client(),
            [{"role": "user", "content": "Build the timetable"}],
            advertised_tools=TOOLS,
            max_calls=2,
            max_attempts=1,
            repair_reason="constraint_coverage_failed",
            repair_details=details,
        )


def test_planner_rejects_an_open_ended_repair_reason_before_provider_call() -> None:
    class Client:
        def chat_with_tools(self, *_args, **_kwargs):
            pytest.fail("an unrecognised repair category must not reach the provider")

    with pytest.raises(ValueError, match="closed failure category"):
        plan_student_turn(
            Client(),
            [{"role": "user", "content": "Tell me about AI331"}],
            advertised_tools=TOOLS,
            max_calls=2,
            max_attempts=1,
            repair_reason="raw invalid plan goes here",
        )


def test_parser_uses_strict_raw_json_not_the_backends_lossy_arguments_field() -> None:
    result = _tool_result(
        "{not-json",
        parsed_arguments={
            "decision": "direct",
            "evidence_requests": [],
            "clarification_question": "",
        },
    )

    with pytest.raises(TurnPlanValidationError, match="strict JSON"):
        parse_turn_plan_result(result, advertised_tools=TOOLS, max_calls=3)


@pytest.mark.parametrize(
    "raw",
    [
        (
            '{"decision":"direct","decision":"execute",'
            '"evidence_requests":[],"clarification_question":""}'
        ),
        (
            '{"decision":"direct","evidence_requests":[],'
            '"clarification_question":"","unexpected":true}'
        ),
        "[]",
        '{"decision":NaN,"evidence_requests":[],"clarification_question":""}',
    ],
)
def test_parser_rejects_ambiguous_or_nonstandard_json(raw: str) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(_tool_result(raw), advertised_tools=TOOLS, max_calls=3)


def test_parser_requires_exactly_one_clean_meta_tool_call() -> None:
    valid = _tool_result(_raw_plan(decision="direct", requests=[]))
    two_calls = ToolChatResult(
        content="",
        tool_calls=valid.tool_calls * 2,
        model="m",
        usage={},
        assistant_message={"role": "assistant", "content": ""},
    )
    with pytest.raises(TurnPlanValidationError, match="exactly one"):
        parse_turn_plan_result(two_calls, advertised_tools=TOOLS, max_calls=3)
    with pytest.raises(TurnPlanValidationError, match="wrong function"):
        parse_turn_plan_result(
            _tool_result(_raw_plan(), name="lookup_course"),
            advertised_tools=TOOLS,
            max_calls=3,
        )
    with pytest.raises(TurnPlanValidationError, match="prose"):
        parse_turn_plan_result(
            _tool_result(_raw_plan(), content="Here is the plan"),
            advertised_tools=TOOLS,
            max_calls=3,
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "missing required"),
        ({"query": "AI331", "extra": True}, "unexpected"),
        ({"query": 331}, "wrong JSON type"),
        ({"query": ""}, "shorter"),
    ],
)
def test_exact_capability_argument_schema_is_enforced(
    arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(TurnPlanValidationError, match=message):
        validate_capability_arguments(
            "lookup_course",
            arguments,
            advertised_tools=TOOLS,
        )


def test_discriminated_request_rejects_arguments_from_another_capability() -> None:
    raw = _raw_plan(
        requests=[
            {
                "capability": "lookup_course",
                "arguments": {"course_codes": ["AI331", "AI352"]},
            }
        ]
    )

    with pytest.raises(TurnPlanValidationError, match="lookup_course.*missing required"):
        parse_turn_plan_result(
            _tool_result(raw),
            advertised_tools=TOOLS,
            max_calls=2,
        )


def test_public_argument_validator_returns_a_defensive_copy() -> None:
    arguments = {"course_codes": ["AI331", "AI352"]}
    validated = validate_capability_arguments(
        "course_choice_comparison",
        arguments,
        advertised_tools=TOOLS,
    )
    arguments["course_codes"].append("AI371")

    assert validated == {"course_codes": ["AI331", "AI352"]}
    with pytest.raises(TurnPlanValidationError, match="unavailable"):
        validate_capability_arguments("not_advertised", {}, advertised_tools=TOOLS)


def test_repeated_capability_with_distinct_arguments_is_allowed() -> None:
    parsed = parse_turn_plan_result(
        _tool_result(
            _raw_plan(
                requests=[
                    {"capability": "lookup_course", "arguments": {"query": "AI331"}},
                    {"capability": "lookup_course", "arguments": {"query": "AI352"}},
                ]
            )
        ),
        advertised_tools=TOOLS,
        max_calls=2,
    )

    assert [request.arguments for request in parsed.evidence_requests] == [
        {"query": "AI331"},
        {"query": "AI352"},
    ]


@pytest.mark.parametrize(
    "requests",
    [
        [
            {"capability": "lookup_course", "arguments": {"query": "AI331"}},
            {"capability": "lookup_course", "arguments": {"query": "AI331"}},
        ],
        [
            {"capability": "lookup_course", "arguments": {"query": "AI331"}},
            {
                "capability": "course_choice_comparison",
                "arguments": {"course_codes": ["AI331", "AI352"]},
            },
            {"capability": "my_progress", "arguments": {}},
        ],
    ],
)
def test_exact_duplicate_and_over_budget_plans_are_rejected(
    requests: list[dict[str, Any]],
) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(_raw_plan(requests=requests)),
            advertised_tools=TOOLS,
            max_calls=2,
        )


@pytest.mark.parametrize(
    ("decision", "requests", "clarification"),
    [
        ("execute", [], ""),
        ("execute", [{"capability": "my_progress", "arguments": {}}], "Which term?"),
        ("clarify", [], ""),
        ("clarify", [{"capability": "my_progress", "arguments": {}}], "Which term?"),
        ("direct", [{"capability": "my_progress", "arguments": {}}], ""),
        ("direct", [], "Which term?"),
    ],
)
def test_decision_state_invariants_are_enforced(
    decision: str,
    requests: list[dict[str, Any]],
    clarification: str,
) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(
                _raw_plan(
                    decision=decision,
                    requests=requests,
                    clarification=clarification,
                )
            ),
            advertised_tools=TOOLS,
            max_calls=3,
        )


@pytest.mark.parametrize(
    "raw",
    [
        ('{"decision":"direct","evidence_requests":[],"clarification_question":""}'),
        _raw_plan(outcomes=[]),
        _raw_plan(outcomes=["course_catalogue", "course_catalogue"]),
        _raw_plan(outcomes=["model_invented_outcome"]),
        _raw_plan(outcomes="course_catalogue"),  # type: ignore[arg-type]
    ],
)
def test_requested_outcomes_are_required_nonempty_unique_and_closed(raw: str) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(raw),
            advertised_tools=TOOLS,
            max_calls=3,
        )


def test_parser_preserves_every_typed_outcome_in_request_order() -> None:
    parsed = parse_turn_plan_result(
        _tool_result(
            _raw_plan(
                outcomes=[
                    "course_addition",
                    "timetable_feasibility",
                    "graduation_impact",
                ],
                requests=[
                    {"capability": "my_progress", "arguments": {}},
                ],
            )
        ),
        advertised_tools=TOOLS,
        max_calls=3,
    )

    assert parsed.requested_outcomes == (
        StudentRequestOutcome.COURSE_ADDITION,
        StudentRequestOutcome.TIMETABLE_FEASIBILITY,
        StudentRequestOutcome.GRADUATION_IMPACT,
    )


@pytest.mark.parametrize(
    ("decision", "outcomes", "requests", "clarification"),
    [
        (
            "execute",
            ["unsupported_request"],
            [{"capability": "my_progress", "arguments": {}}],
            "",
        ),
        (
            "execute",
            ["credit_load_comparison"],
            [{"capability": "my_progress", "arguments": {}}],
            "",
        ),
        (
            "execute",
            ["general_conversation"],
            [{"capability": "my_progress", "arguments": {}}],
            "",
        ),
        (
            "execute",
            ["registration_action"],
            [{"capability": "my_progress", "arguments": {}}],
            "",
        ),
        ("clarify", ["registration_action"], [], "Which course?"),
        ("direct", ["degree_progress"], [], ""),
        ("unsupported", ["course_addition"], [], ""),
        (
            "unsupported",
            ["course_addition", "registration_action"],
            [],
            "",
        ),
        (
            "unsupported",
            ["unsupported_request", "general_conversation"],
            [],
            "",
        ),
    ],
)
def test_decisions_reject_incompatible_request_outcome_classes(
    decision: str,
    outcomes: list[str],
    requests: list[dict[str, Any]],
    clarification: str,
) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(
                _raw_plan(
                    decision=decision,
                    outcomes=outcomes,
                    requests=requests,
                    clarification=clarification,
                )
            ),
            advertised_tools=TOOLS,
            max_calls=3,
        )


@pytest.mark.parametrize(
    "outcomes",
    [
        ["unsupported_request"],
        ["registration_action"],
        ["credit_load_comparison"],
    ],
)
def test_unsupported_is_a_typed_tool_free_decision(outcomes: list[str]) -> None:
    parsed = parse_turn_plan_result(
        _tool_result(
            _raw_plan(
                decision="unsupported",
                outcomes=outcomes,
                requests=[],
            )
        ),
        advertised_tools=TOOLS,
        max_calls=3,
    )

    assert parsed.decision is TurnPlanDecision.UNSUPPORTED
    assert [outcome.value for outcome in parsed.requested_outcomes] == outcomes
    assert parsed.evidence_requests == ()
    assert parsed.clarification_question == ""
    assert parsed.requires_evidence is False
    assert synthesize_tool_calls(parsed) == ()


@pytest.mark.parametrize(
    "boundary_outcome",
    ["registration_action", "credit_load_comparison"],
)
def test_execute_can_pair_evidence_with_a_server_owned_partial_boundary(
    boundary_outcome: str,
) -> None:
    parsed = parse_turn_plan_result(
        _tool_result(
            _raw_plan(
                decision="execute",
                outcomes=["degree_progress", boundary_outcome],
                requests=[{"capability": "my_progress", "arguments": {}}],
            )
        ),
        advertised_tools=TOOLS,
        max_calls=3,
    )

    assert parsed.decision is TurnPlanDecision.EXECUTE
    assert parsed.requested_outcomes == (
        StudentRequestOutcome.DEGREE_PROGRESS,
        StudentRequestOutcome(boundary_outcome),
    )
    assert [call.capability for call in parsed.evidence_requests] == ["my_progress"]


@pytest.mark.parametrize(
    ("requests", "clarification"),
    [
        ([{"capability": "my_progress", "arguments": {}}], ""),
        ([], "Which registration action?"),
    ],
)
def test_unsupported_cannot_request_evidence_or_clarification(
    requests: list[dict[str, Any]],
    clarification: str,
) -> None:
    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(
                _raw_plan(
                    decision="unsupported",
                    outcomes=["registration_action"],
                    requests=requests,
                    clarification=clarification,
                )
            ),
            advertised_tools=TOOLS,
            max_calls=3,
        )


def test_valid_clarification_and_direct_plans_remain_tool_free() -> None:
    clarification = parse_turn_plan_result(
        _tool_result(_raw_plan(decision="clarify", requests=[], clarification="Which course?")),
        advertised_tools=TOOLS,
        max_calls=3,
    )
    direct = parse_turn_plan_result(
        _tool_result(_raw_plan(decision="direct", requests=[])),
        advertised_tools=TOOLS,
        max_calls=3,
    )

    assert clarification.decision is TurnPlanDecision.CLARIFY
    assert clarification.requested_outcomes == (StudentRequestOutcome.COURSE_CATALOGUE,)
    assert clarification.clarification_question == "Which course?"
    assert clarification.clarification_kind is ClarificationKind.GENERIC
    assert direct.decision is TurnPlanDecision.DIRECT
    assert direct.requested_outcomes == (StudentRequestOutcome.GENERAL_CONVERSATION,)
    assert synthesize_tool_calls(clarification) == ()
    assert synthesize_tool_calls(direct) == ()


def test_parser_requires_the_closed_clarification_kind_field() -> None:
    payload = json.loads(_raw_plan(decision="clarify", requests=[], clarification="Which course?"))
    payload.pop("clarification_kind")

    with pytest.raises(TurnPlanValidationError):
        parse_turn_plan_result(
            _tool_result(json.dumps(payload)),
            advertised_tools=TOOLS,
            max_calls=3,
        )


@pytest.mark.parametrize(
    ("decision", "clarification", "clarification_kind"),
    [
        ("clarify", "Which course?", "none"),
        ("execute", "", "timetable_load"),
        ("direct", "", "generic"),
        ("unsupported", "", "term_or_choice"),
    ],
)
def test_parser_rejects_clarification_kind_decision_mismatches(
    decision: str,
    clarification: str,
    clarification_kind: str,
) -> None:
    requests = (
        [{"capability": "lookup_course", "arguments": {"query": "AI331"}}]
        if decision == "execute"
        else []
    )
    with pytest.raises(TurnPlanValidationError, match=r"clarification(?:_| )kind"):
        parse_turn_plan_result(
            _tool_result(
                _raw_plan(
                    decision=decision,
                    requests=requests,
                    clarification=clarification,
                    clarification_kind=clarification_kind,
                )
            ),
            advertised_tools=TOOLS,
            max_calls=3,
        )


def test_synthesis_produces_an_ordinary_validated_tool_chat_turn() -> None:
    plan = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall("lookup_course", {"query": "ذكاء اصطناعي"}),
            PlannedCapabilityCall(
                "course_choice_comparison",
                {"course_codes": ["AI331", "AI352"]},
            ),
        ),
        requested_outcomes=(
            StudentRequestOutcome.COURSE_CATALOGUE,
            StudentRequestOutcome.COURSE_COMPARISON,
        ),
    )

    result = synthesize_tool_chat_result(
        plan,
        model="planner-model",
        usage={"total_tokens": 21},
        model_revision="r1",
        call_id_prefix="semantic",
    )

    assert [call.name for call in result.tool_calls] == [
        "lookup_course",
        "course_choice_comparison",
    ]
    assert [call.id for call in result.tool_calls] == ["semantic_1", "semantic_2"]
    assert json.loads(result.tool_calls[0].raw_arguments) == {"query": "ذكاء اصطناعي"}
    assert result.assistant_message["tool_calls"][1]["function"]["name"] == (
        "course_choice_comparison"
    )
    assert result.model == "planner-model"
    assert result.model_revision == "r1"
    assert result.usage == {"total_tokens": 21}

    plan.evidence_requests[0].arguments["query"] = "changed after synthesis"
    assert result.tool_calls[0].arguments == {"query": "ذكاء اصطناعي"}


def test_non_execute_plan_cannot_be_synthesized_as_a_tool_result() -> None:
    direct = StudentTurnPlan(
        TurnPlanDecision.DIRECT,
        (),
        requested_outcomes=(StudentRequestOutcome.GENERAL_CONVERSATION,),
    )
    with pytest.raises(ValueError, match="execute plan"):
        synthesize_tool_chat_result(direct, model="planner-model")


def test_schema_valid_course_argument_still_requires_turn_provenance() -> None:
    plan = parse_turn_plan_result(
        _tool_result(
            _raw_plan(requests=[{"capability": "lookup_course", "arguments": {"query": "AI352"}}])
        ),
        advertised_tools=TOOLS,
        max_calls=2,
    )
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.identifier(("lookup_course", "query"), "AI331")
    )

    with pytest.raises(TurnPlanProvenanceError, match="not supported by trusted"):
        validate_plan_argument_provenance(plan, contract=contract)


def test_identifier_provenance_allows_only_harmless_spelling_differences() -> None:
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.identifier(
            ("course_choice_comparison", "course_codes", "*"),
            "AI331",
            "AI352",
        )
    )
    arguments = {"course_codes": ["ai-331", "AI 352"]}

    validated = validate_capability_argument_provenance(
        "course_choice_comparison",
        arguments,
        contract=contract,
    )
    arguments["course_codes"].append("AI371")

    assert validated == {"course_codes": ["ai-331", "AI 352"]}
    assert normalise_provenance_identifier("AI_٣٣١") == "ai331"
    assert normalise_provenance_identifier("AI/331") != "ai331"


def test_nested_provenance_is_field_specific_and_semantic_controls_are_explicit() -> None:
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.semantic_choice(("build_timetable_proposal", "mode")),
        ArgumentProvenanceRule.identifier(
            ("build_timetable_proposal", "course_codes", "*"), "AI331"
        ),
        ArgumentProvenanceRule.structured_exact(
            ("build_timetable_proposal", "pinned_sections", "*"),
            {"course_code": "AI-331", "section_label": "a-1"},
        ),
        ArgumentProvenanceRule.exact(("build_timetable_proposal", "max_credits"), 18),
    )
    valid = {
        "mode": "balanced",
        "course_codes": ["AI331"],
        "pinned_sections": [{"course_code": "AI-331", "section_label": "a-1"}],
        "max_credits": 18,
    }

    assert (
        validate_capability_argument_provenance(
            "build_timetable_proposal", valid, contract=contract
        )
        == valid
    )

    hallucinated_section = dict(valid)
    hallucinated_section["pinned_sections"] = [{"course_code": "AI331", "section_label": "B9"}]
    with pytest.raises(TurnPlanProvenanceError, match="not supported by trusted"):
        validate_capability_argument_provenance(
            "build_timetable_proposal", hallucinated_section, contract=contract
        )

    wrong_number = dict(valid, max_credits=19)
    with pytest.raises(TurnPlanProvenanceError, match="not supported by trusted"):
        validate_capability_argument_provenance(
            "build_timetable_proposal", wrong_number, contract=contract
        )


def test_structured_provenance_rejects_swapped_course_section_bindings() -> None:
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.structured_exact(
            ("build_timetable_proposal", "pinned_sections", "*"),
            {"course_code": "AI331", "section_label": "M1"},
            {"course_code": "DS341", "section_label": "F2"},
        )
    )
    swapped = {
        "pinned_sections": [
            {"course_code": "AI331", "section_label": "F2"},
            {"course_code": "DS341", "section_label": "M1"},
        ]
    }

    with pytest.raises(TurnPlanProvenanceError, match="not supported by trusted"):
        validate_capability_argument_provenance(
            "build_timetable_proposal", swapped, contract=contract
        )


def test_text_span_provenance_uses_unicode_digit_and_whitespace_normalization() -> None:
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.text_span(
            ("lookup_course", "query"),
            "  أبغى معلومات عن   الذكاء الاصطناعي ١ ",
        )
    )

    assert validate_capability_argument_provenance(
        "lookup_course",
        {"query": "الذكاء الاصطناعي 1"},
        contract=contract,
    ) == {"query": "الذكاء الاصطناعي 1"}
    assert normalise_provenance_text("  AI٣٣١\n") == "ai331"

    with pytest.raises(TurnPlanProvenanceError, match="not supported by trusted"):
        validate_capability_argument_provenance(
            "lookup_course",
            {"query": "هندسة البرمجيات"},
            contract=contract,
        )


def test_provenance_is_default_deny_for_new_scalar_paths_but_allows_empty_arguments() -> None:
    empty_contract = ArgumentProvenanceContract(rules=())

    assert validate_capability_argument_provenance("my_progress", {}, contract=empty_contract) == {}
    with pytest.raises(TurnPlanProvenanceError, match="no approved provenance rule"):
        validate_capability_argument_provenance(
            "future_tool",
            {"new_argument": "model supplied"},
            contract=empty_contract,
        )


def test_exact_provenance_keeps_boolean_and_number_distinct() -> None:
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.exact(("tool", "value"), 1)
    )

    assert validate_capability_argument_provenance("tool", {"value": 1}, contract=contract) == {
        "value": 1
    }
    with pytest.raises(TurnPlanProvenanceError):
        validate_capability_argument_provenance("tool", {"value": True}, contract=contract)


@pytest.mark.parametrize(
    "rule",
    [
        ArgumentProvenanceRule((), ArgumentProvenanceMode.SEMANTIC_CHOICE),
        ArgumentProvenanceRule(("*", "value"), ArgumentProvenanceMode.SEMANTIC_CHOICE),
        ArgumentProvenanceRule.exact(("tool", "value")),
        ArgumentProvenanceRule.identifier(("tool", "value"), ""),
        ArgumentProvenanceRule.text_span(("tool", "value"), ""),
        ArgumentProvenanceRule(
            ("tool", "value"),
            ArgumentProvenanceMode.STRUCTURED_EXACT,
            allowed_values=("not structured",),
        ),
        ArgumentProvenanceRule(
            ("tool", "value"),
            ArgumentProvenanceMode.SEMANTIC_CHOICE,
            allowed_values=("unexpected",),
        ),
    ],
)
def test_malformed_provenance_contracts_are_server_errors(
    rule: ArgumentProvenanceRule,
) -> None:
    with pytest.raises(TurnPlanSchemaError, match="provenance contract"):
        validate_capability_argument_provenance(
            "tool",
            {"value": "anything"},
            contract=ArgumentProvenanceContract.from_rules(rule),
        )


def test_plan_provenance_returns_a_defensive_copy_and_preserves_decision() -> None:
    source_arguments = {"query": "AI331"}
    plan = StudentTurnPlan(
        TurnPlanDecision.EXECUTE,
        (PlannedCapabilityCall("lookup_course", source_arguments),),
        requested_outcomes=(StudentRequestOutcome.COURSE_CATALOGUE,),
    )
    contract = ArgumentProvenanceContract.from_rules(
        ArgumentProvenanceRule.identifier(("lookup_course", "query"), "AI331")
    )

    validated = validate_plan_argument_provenance(plan, contract=contract)
    source_arguments["query"] = "AI999"

    assert validated.decision is TurnPlanDecision.EXECUTE
    assert validated.requested_outcomes == (StudentRequestOutcome.COURSE_CATALOGUE,)
    assert validated.evidence_requests[0].arguments == {"query": "AI331"}
