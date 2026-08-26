"""Integration contract for semantic-planning Student Advisor V2.1."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from core.models import Student
from core.services.advisor_principal import AdvisorPrincipal
from core.services.llm_backend import (
    LLMUnavailable,
    ToolCallRequest,
    ToolChatResult,
)
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import STUDENT_V21_PROMPT_VERSION
from core.services.student_advisor_v21 import answer_student_advisor_v21
from core.services.student_advisor_v21_plan import (
    TURN_PLAN_TOOL_NAME,
    ClarificationKind,
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
    TurnPlanProvenanceError,
    validate_capability_argument_provenance,
)

SID = 4901291
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _student_record() -> None:
    Student.objects.get_or_create(
        student_id=SID,
        defaults={"name": "V21 Test Student", "program": "CS", "section": "M"},
    )


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID)


def _validate_v21_arguments(
    question: str,
    capability: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from core.services.student_advisor_v2 import _v21_argument_provenance_contract

    contract = _v21_argument_provenance_contract(
        question,
        history=[],
        prior_presentation={},
        prior_course_names={},
    )
    return validate_capability_argument_provenance(
        capability,
        arguments,
        contract=contract,
    )


def _validate_v21_pins(
    question: str,
    pins: list[dict[str, str]],
    *,
    capability: str = "build_timetable_proposal",
) -> dict[str, Any]:
    return _validate_v21_arguments(
        question,
        capability,
        {"pinned_sections": pins},
    )


def _planner_turn(
    decision: str,
    requests: list[dict[str, Any]] | None = None,
    clarification: str = "",
    outcomes: list[str] | None = None,
    clarification_kind: str | None = None,
) -> ToolChatResult:
    capability_outcomes = {
        "my_progress": "degree_progress",
        "my_plan_by_term": "degree_plan",
        "my_timetable": "current_timetable",
        "my_clash_free_sections": "timetable_feasibility",
        "build_timetable_proposal": "timetable_build",
        "lookup_course": "course_catalogue",
        "course_prerequisites": "prerequisite_information",
        "why_course_locked": "course_eligibility",
        "course_choice_comparison": "course_comparison",
        "feasible_course_replacements": "course_replacement",
        "recommend_courses": "course_recommendation",
        "graduation_progress": "graduation_forecast",
        "policy_lookup": "policy_rule",
        "my_advisor": "academic_adviser",
        "recommend_feasible_course_addition": "course_addition",
        "rank_current_course_drop_impact": "course_drop_impact",
        "improve_current_timetable": "timetable_review",
    }
    if outcomes is None:
        if decision == "direct":
            outcomes = ["general_conversation"]
        elif decision == "unsupported":
            outcomes = ["unsupported_request"]
        elif requests:
            outcomes = list(
                dict.fromkeys(
                    capability_outcomes[item["capability"]]
                    for item in requests
                    if item.get("capability") in capability_outcomes
                )
            )
        else:
            outcomes = ["course_eligibility"]
    if clarification_kind is None:
        clarification_kind = "generic" if decision == "clarify" else "none"
    arguments = {
        "decision": decision,
        "requested_outcomes": outcomes,
        "evidence_requests": requests or [],
        "clarification_kind": clarification_kind,
        "clarification_question": clarification,
    }
    return _planner_payload_turn(arguments)


def _planner_payload_turn(arguments: dict[str, Any]) -> ToolChatResult:
    raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    call = ToolCallRequest(
        id="plan_1",
        name=TURN_PLAN_TOOL_NAME,
        arguments=arguments,
        raw_arguments=raw,
    )
    return ToolChatResult(
        content="",
        tool_calls=(call,),
        model="test-model",
        usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": raw},
                }
            ],
        },
    )


def _answer_turn(text: str) -> ToolChatResult:
    return ToolChatResult(
        content=text,
        tool_calls=(),
        model="test-model",
        usage={"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
        assistant_message={"role": "assistant", "content": text},
    )


class ScriptedClient:
    backend = "local"
    supports_assistant_prefill = True

    def __init__(self, *turns: ToolChatResult, backend: str = "local"):
        self.backend = backend
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def resolve_model(self, requested_model=None):
        return requested_model or "test-model"

    def chat_with_tools(self, messages, *, tools, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return self.turns.pop(0)

    def chat(self, messages, **kwargs):  # pragma: no cover - no rescue in these contracts
        raise AssertionError("A no-tools rescue was not expected")


def _make_legacy_input_router_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove V2.1 cannot accidentally ask V2's question-pattern router."""

    import core.services.advisor_intent as legacy_intent
    import core.services.student_advisor_v2 as runtime

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy question-side routing was called by V2.1")

    monkeypatch.setattr(legacy_intent, "owning_capability", forbidden)
    for name in (
        "_required_exact_fact_tools",
        "_requires_timetable_proposal",
        "_requires_section_check",
        "_requires_graduation_progress",
        "_requires_graduation_what_if",
        "_requires_feasible_course_replacements",
        "_requires_course_choice_comparison",
    ):
        monkeypatch.setattr(runtime, name, forbidden)


def test_v21_direct_turn_never_enters_the_legacy_question_router(monkeypatch):
    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(_planner_turn("direct"))

    result = answer_student_advisor_v21(
        question="Remove this paragraph please: CS424 is only an example.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["version"] == "student-v2.1"
    assert result["agent"]["prompt_version"] == STUDENT_V21_PROMPT_VERSION
    assert result["agent"]["semantic_plan_decision"] == "direct"
    assert result["agent"]["semantic_plan_tools"] == []
    assert result["agent"]["graduation_what_if_required"] is False
    assert "verified information" in result["answer"]
    assert len(client.calls) == 1
    assert client.calls[0]["kwargs"]["tool_choice"] == "required"
    assert [item["function"]["name"] for item in client.calls[0]["tools"]] == [TURN_PLAN_TOOL_NAME]


def test_v21_planner_prompt_declares_semantic_capability_boundaries() -> None:
    assert STUDENT_V21_PROMPT_VERSION == "student-v21-semantic-plan-v18"
    client = ScriptedClient(_planner_turn("unsupported"))

    answer_student_advisor_v21(
        question="هل يوجد متطلب متزامن لهذا المقرر؟",
        principal=_principal(),
        llm_client=client,
    )

    planner_prompt = " ".join(str(client.calls[0]["messages"][0]["content"]).split())
    assert "not corequisites" in planner_prompt
    assert "cannot compare alternative credit-load policies" in planner_prompt
    assert "does not optimise an overall minimum course load" in planner_prompt
    assert "list timetable_build and course_priority" in planner_prompt
    assert "request both build_timetable_proposal and my_progress" in planner_prompt
    assert "Priority wording alone is not graduation_impact" in planner_prompt
    assert "graduation_impact alone may be owned" in planner_prompt
    assert "أنزل/آخذ/أسجل a course means take or enrol" in planner_prompt
    assert "remaining courses that are open or available is available_courses" in planner_prompt
    assert "official maximum credit load needs current_timetable and policy_rule" in planner_prompt
    assert "light new timetable that supplies no exact or maximum credit-hour" in planner_prompt
    assert "include every named candidate in course_codes" in planner_prompt
    assert "both course_eligibility and timetable_feasibility" in planner_prompt
    assert "important feasible course missing from the current timetable" in planner_prompt
    assert "is a course comparison unless the student explicitly identifies" in planner_prompt
    assert "request timetable_build" in planner_prompt
    assert "from_scratch mode with that course in must_take_courses" in planner_prompt
    assert "new/from-scratch timetable is timetable_build" in planner_prompt
    assert "search_better_replacements=true" in planner_prompt
    assert "Use feasible_course_replacements only" in planner_prompt
    assert "credit_load_comparison with decision=unsupported" in planner_prompt
    assert "retain it as credit_load_comparison in the execute plan" in planner_prompt
    assert "Never substitute the fixed 18-credit" in planner_prompt
    assert "use improve_current_timetable with faster_graduation" in planner_prompt
    assert "timetable space or fit means recommend_feasible_course_addition" in planner_prompt
    assert '"not low priority" means objective=unlock_impact' in planner_prompt
    assert "graduation wording is the review criterion" in planner_prompt
    assert "credit_load_policy=not_increase" in planner_prompt
    assert "impact-ranked top-N list" in planner_prompt
    assert "priority_limit=N copied exactly" in planner_prompt
    assert "registered the right courses this term" in planner_prompt
    assert "fresh/from-scratch timetable with a priority criterion" in planner_prompt
    assert 'For "if I fail this named course, which courses are affected?"' in planner_prompt
    assert "noncompletion_current_courses=[that exact course]" in planner_prompt
    assert "Never encode failure/non-passage as remove_current_courses" in planner_prompt
    assert "وش ناقصني عشان أقدر أسجل DS491؟" in planner_prompt
    assert "prerequisite_information via why_course_locked, never" in planner_prompt
    assert "ليه ما أقدر أنزل DS491؟" in planner_prompt
    assert "which are the best available courses?" in planner_prompt
    assert "which important available course should I add?" in planner_prompt
    assert "which course is most worth adding?" in planner_prompt
    assert "build a full timetable around DS341-M2 without conflicts" in planner_prompt
    assert "what if I do not take DS321 this term?" in planner_prompt
    assert "rank_current_course_drop_impact even when only one course is named" in (planner_prompt)
    assert "course_eligibility + prerequisite_information" in planner_prompt
    assert "the code is already resolved, so never call lookup_course" in planner_prompt
    assert "important-course ranking AND the registerable" in planner_prompt
    assert "already executable with build_timetable_proposal(mode=from_scratch)" in planner_prompt
    assert "executable with mode=from_scratch and max_credits=15" in planner_prompt
    assert "Use around_current only when retaining the whole current/baseline timetable" in (
        planner_prompt
    )
    assert "clarification_kind=timetable_load" in planner_prompt
    assert "clarification_kind=timetable_preference" in planner_prompt
    assert "clarification_kind=course_or_section_identity" in planner_prompt
    assert "I have room for one course; what should I choose?" in planner_prompt
    assert "eligible for but do not have in my timetable" in planner_prompt
    assert "best course or courses to add" in planner_prompt
    assert "unsupported_request], and zero evidence requests" in planner_prompt
    assert "singleton yes/no exception does not apply" in planner_prompt
    planner_contract = client.calls[0]["tools"][0]["function"]["description"]
    assert "if I take AI331 instead of DS341, which is better?" in planner_contract
    assert "For a symmetric 'take X instead of Y; which is better?' choice" in (planner_contract)
    assert "priority is not itself graduation impact" in planner_contract
    assert "top-N list" in planner_contract
    assert "did I register the right courses this term?" in planner_contract
    assert "timetable space/fit -> timetable_fit" in planner_contract
    assert "if I fail DS341" in planner_contract
    assert "fresh/from-scratch build" in planner_contract
    assert "incidental fields or checks" in planner_contract
    assert "why can't I take DS491?" in planner_contract
    assert "what am I missing before DS491?" in planner_contract
    assert "best available courses" in planner_contract
    assert "what happens if I withdraw?" in planner_contract
    assert "build a full timetable around DS341-M2 without conflicts" in planner_contract
    assert "will dropping DS332 delay graduation?" in planner_contract
    assert "graduation_impact alone via rank_current_course_drop_impact" in planner_contract
    assert "both timetable_build + course_priority" in planner_contract
    assert "both build_timetable_proposal(mode=from_scratch" in planner_contract
    assert "catalogue-only course_prerequisites" in planner_contract
    assert "both course_eligibility + prerequisite_information" in planner_contract
    assert "means course_prerequisites, never lookup_course" in planner_contract
    assert "important courses I can register but have not taken" in planner_contract
    assert "selecting the least-delay drop among several named current courses" in (
        planner_contract
    )
    assert "standalone corequisite question is unsupported_request" in planner_contract
    assert "generic choice of one course" in planner_contract
    assert "plain eligible-but-not-current-timetable list" in planner_contract
    assert "best course(s) to add alongside an exact pin" in planner_contract


def test_semantic_eval_messages_exactly_match_captured_production_planner_call() -> None:
    from evals.advisor.run_v21_semantic_plan import planner_messages

    question = "ثبّت M2 وابنِ الباقي حولها."
    history = [
        {
            "role": "user",
            "content": "كنت أقارن بين شعبتي DS341-M2 وDS432-M2.",
        }
    ]
    client = ScriptedClient(
        _planner_turn(
            "clarify",
            clarification="أي مقرر تقصد؟",
            outcomes=["timetable_build"],
            clarification_kind="course_or_section_identity",
        )
    )

    answer_student_advisor_v21(
        question=question,
        history=history,
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    expected = planner_messages(
        {"language": "ar-SA", "question": question, "history": history},
        year=1448,
        term=1,
    )
    assert client.calls[0]["messages"] == expected


def _policy_plan(
    decision: str,
    outcomes: list[str],
    requests: list[dict[str, Any]],
    *,
    clarification_kind: str = "none",
) -> dict[str, Any]:
    return {
        "decision": decision,
        "requested_outcomes": outcomes,
        "evidence_requests": requests,
        "clarification_kind": clarification_kind,
        "clarification_question": ("Which preference?" if decision == "clarify" else ""),
    }


_V18_POLICY_CASES = [
    pytest.param(
        "هل فيه متطلب متزامن مع DS491؟",
        _policy_plan(
            "execute",
            ["prerequisite_information"],
            [
                {
                    "capability": "course_prerequisites",
                    "arguments": {"course_code": "DS491"},
                }
            ],
        ),
        _policy_plan("unsupported", ["unsupported_request"], []),
        "standalone_corequisite_unsupported",
        [],
        id="standalone-corequisite",
    ),
    pytest.param(
        "عندي مجال لمادة وحدة بس، وش أختار؟",
        _policy_plan(
            "execute",
            ["course_priority"],
            [{"capability": "my_progress", "arguments": {}}],
        ),
        _policy_plan(
            "execute",
            ["course_addition"],
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
        ),
        "single_course_choice_balanced",
        [("recommend_feasible_course_addition", {"objective": "balanced"})],
        id="single-course-choice",
    ),
    pytest.param(
        "وش المواد اللي أنا مؤهل لها بس مو موجودة في جدولي؟",
        _policy_plan(
            "execute",
            ["available_courses", "course_priority"],
            [{"capability": "my_progress", "arguments": {}}],
        ),
        _policy_plan(
            "execute",
            ["available_courses"],
            [{"capability": "my_progress", "arguments": {}}],
        ),
        "plain_available_courses_only",
        [("my_progress", {})],
        id="available-only",
    ),
    pytest.param(
        "إذا ثبتنا DS341-M2، وش أفضل المواد اللي نضيفها معه؟",
        _policy_plan(
            "clarify",
            ["timetable_build"],
            [],
            clarification_kind="timetable_preference",
        ),
        _policy_plan(
            "execute",
            ["course_addition"],
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {
                        "objective": "balanced",
                        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                    },
                }
            ],
        ),
        "pinned_course_addition_balanced",
        [
            (
                "recommend_feasible_course_addition",
                {
                    "objective": "balanced",
                    "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                },
            )
        ],
        id="pinned-course-addition",
    ),
]


@pytest.mark.parametrize(
    ("question", "bad_plan", "good_plan", "policy_id", "expected_execution"),
    _V18_POLICY_CASES,
)
def test_v18_semantic_policy_repairs_once_then_accepts_only_the_correct_plan(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    bad_plan: dict[str, Any],
    good_plan: dict[str, Any],
    policy_id: str,
    expected_execution: list[tuple[str, dict[str, Any]]],
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        if name == "my_progress":
            return {
                "tool": name,
                "ok": True,
                "counts": {"open": 0, "locked": 0},
                "prerequisites_satisfied": [],
                "prerequisite_blocked": [],
            }
        return {
            "tool": name,
            "ok": True,
            "status": "NO_ELIGIBLE_CANDIDATES",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": arguments.get("objective"),
            "constraints": {"pinned_sections": arguments.get("pinned_sections", [])},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_payload_turn(bad_plan),
        _planner_payload_turn(good_plan),
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert executed == expected_execution
    assert len(client.calls) == result["usage"]["provider_calls"] == 2
    assert result["agent"]["semantic_plan_failure_reason"] == ""
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["semantic_plan_policy_validation"] == {
        "valid": True,
        "policy_ids": [],
    }
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "",
        "repair": {"attempted": True, "result": "succeeded"},
    }
    repair_message = client.calls[1]["messages"][-1]["content"]
    assert f"policy_ids={policy_id}" in repair_message
    assert "DS491" not in repair_message
    assert "DS341" not in repair_message
    assert "M2" not in repair_message


@pytest.mark.parametrize(
    ("question", "bad_plan", "_good_plan", "policy_id", "_expected_execution"),
    _V18_POLICY_CASES,
)
def test_v18_semantic_policy_repeated_miss_refuses_with_zero_domain_tools(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    bad_plan: dict[str, Any],
    _good_plan: dict[str, Any],
    policy_id: str,
    _expected_execution: list[tuple[str, dict[str, Any]]],
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail(
            "a repeated semantic-policy miss must execute no domain tool"
        ),
    )
    client = ScriptedClient(
        _planner_payload_turn(bad_plan),
        _planner_payload_turn(bad_plan),
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert len(client.calls) == result["usage"]["provider_calls"] == 2
    assert result["agent"]["tools_called"] == []
    assert result["agent"]["semantic_plan_failure_reason"] == ("semantic_policy_failed")
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["semantic_plan_policy_validation"] == {
        "valid": False,
        "policy_ids": [policy_id],
    }
    audit = result["agent"]["evidence_audit"]
    assert audit["plan_contract"] == {
        "failure_reason": "semantic_policy_failed",
        "repair": {"attempted": True, "result": "failed"},
    }
    assert policy_id not in json.dumps(audit, ensure_ascii=False)


def test_v21_repeated_execute_for_ambiguous_pin_fails_closed_before_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail(
            "an uncorrelated positive section pin must never execute"
        ),
    )
    rejected = _planner_turn(
        "execute",
        [
            {
                "capability": "build_timetable_proposal",
                "arguments": {"mode": "from_scratch"},
            }
        ],
        outcomes=["timetable_build"],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question="ثبّت M2 وابنِ الباقي حولها.",
        principal=_principal(),
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert result["agent"]["tools_called"] == []
    assert result["agent"]["semantic_plan_failure_reason"] == ("constraint_coverage_failed")
    assert result["agent"]["semantic_plan_missing_constraint_paths"] == ["clarification_kind"]
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "constraint_coverage_failed",
        "repair": {"attempted": True, "result": "failed"},
        "missing_field_paths": ["clarification_kind"],
    }
    repair_message = client.calls[1]["messages"][-1]["content"]
    assert "missing_field_paths=clarification_kind" in repair_message
    assert "M2" not in repair_message


@pytest.mark.parametrize(
    ("question", "requests", "outcomes"),
    [
        pytest.param(
            "عندي مكان في الجدول، وش المواد اللي أقدر أضيفها؟",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "timetable_fit"},
                }
            ],
            ["course_addition"],
            id="available-space-means-timetable-fit",
        ),
        pytest.param(
            "أبي مادة إضافية بس ما أبي شيء ما له أولوية.",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "unlock_impact"},
                }
            ],
            ["course_addition"],
            id="additional-course-priority-means-unlock-impact",
        ),
        pytest.param(
            "هل جدولي الحالي جيد بالنسبة لخطة تخرجي؟",
            [
                {
                    "capability": "improve_current_timetable",
                    "arguments": {
                        "objective": "faster_graduation",
                        "credit_load_policy": "preserve",
                        "allow_course_replacements": True,
                    },
                }
            ],
            ["timetable_review"],
            id="graduation-oriented-current-review",
        ),
        pytest.param(
            "فيه مادة مهمة أقدر أنزلها وما هي موجودة بجدولي؟",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "unlock_impact"},
                }
            ],
            ["course_addition"],
            id="important-missing-feasible-addition",
        ),
        pytest.param(
            "فيه مقرر في جدولي ما له أولوية حالياً؟",
            [
                {
                    "capability": "rank_current_course_drop_impact",
                    "arguments": {"objective": "lowest_academic_priority"},
                }
            ],
            ["course_drop_impact"],
            id="current-low-priority-is-drop-decision",
        ),
        pytest.param(
            "هل أقدر أحسن جدولي بدون ما أزيد عدد الساعات؟",
            [
                {
                    "capability": "improve_current_timetable",
                    "arguments": {
                        "objective": "balanced",
                        "credit_load_policy": "not_increase",
                        "allow_course_replacements": True,
                    },
                }
            ],
            ["timetable_review"],
            id="improve-without-increasing-hours",
        ),
        pytest.param(
            "وش أغير في جدولي عشان أقلل عدد الترمات المتبقية؟",
            [
                {
                    "capability": "improve_current_timetable",
                    "arguments": {
                        "objective": "faster_graduation",
                        "credit_load_policy": "preserve",
                        "allow_course_replacements": True,
                    },
                }
            ],
            ["timetable_review"],
            id="broad-current-changes-for-fewer-terms",
        ),
        pytest.param(
            "إذا رسبت في DS332 وش المواد اللي بتتأثر؟",
            [
                {
                    "capability": "why_course_locked",
                    "arguments": {"course_code": "DS332"},
                },
                {
                    "capability": "graduation_progress",
                    "arguments": {
                        "planning_baseline_kind": "registered_timetable",
                        "noncompletion_current_courses": ["DS332"],
                    },
                },
            ],
            ["prerequisite_information", "graduation_impact"],
            id="failed-course-forward-and-graduation-effects",
        ),
        pytest.param(
            (
                "ابنِ لي جدول جديد من الصفر بحد أقصى 18 ساعة، ثبت فيه DS341-M2، "
                "وأعط الأولوية للمقررات اللي تمنع تأخر التخرج."
            ),
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "course_codes": ["DS341"],
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                        "max_credits": 18,
                    },
                },
                {"capability": "my_progress", "arguments": {}},
            ],
            ["timetable_build", "course_priority"],
            id="v14-composite-003-fresh-pinned-priority-build",
        ),
        pytest.param(
            "عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج.",
            [{"capability": "my_progress", "arguments": {"priority_limit": 5}}],
            ["course_priority"],
            id="top-five-impact-ranking",
        ),
        pytest.param(
            "هل سجلت المواد الصح لهذا الترم؟",
            [
                {"capability": "my_timetable", "arguments": {}},
                {"capability": "my_progress", "arguments": {}},
            ],
            ["current_timetable", "course_priority"],
            id="right-courses-needs-record-and-priority",
        ),
        pytest.param(
            "وش ناقصني عشان أقدر أسجل `DS491`؟",
            [
                {
                    "capability": "why_course_locked",
                    "arguments": {"course_code": "DS491"},
                }
            ],
            ["prerequisite_information"],
            id="v14-elig-005-personalized-missing-prerequisite-only",
        ),
        pytest.param(
            "ليه ما أقدر أنزل `DS491`؟",
            [
                {
                    "capability": "why_course_locked",
                    "arguments": {"course_code": "DS491"},
                }
            ],
            ["course_eligibility"],
            id="v13-elig-006-eligibility-only",
        ),
        pytest.param(
            "إيش أفضل المواد المتاحة لي للتسجيل الحين؟",
            [{"capability": "my_progress", "arguments": {}}],
            ["course_priority", "available_courses"],
            id="v13-available-006-priority-and-available",
        ),
        pytest.param(
            "من المواد المتاحة لي، أي وحدة أهم أضيفها؟",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "unlock_impact"},
                }
            ],
            ["course_addition"],
            id="v13-addone-004-compound-unlock-only",
        ),
        pytest.param(
            "وش المادة اللي تستاهل أضيفها لجدولي الحالي أكثر؟",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
            ["course_addition"],
            id="v13-addone-008-unspecified-criterion-balanced",
        ),
        pytest.param(
            "لو حذفت `DS332` هل يتأخر تخرجي؟",
            [
                {
                    "capability": "rank_current_course_drop_impact",
                    "arguments": {
                        "objective": "least_graduation_delay",
                        "course_codes": ["DS332"],
                    },
                }
            ],
            ["graduation_impact"],
            id="v14-drop-006-singleton-graduation-impact-only",
        ),
        pytest.param(
            "وش بيصير لو انسحبت من `DS332`؟",
            [
                {
                    "capability": "rank_current_course_drop_impact",
                    "arguments": {
                        "objective": "balanced",
                        "course_codes": ["DS332"],
                    },
                }
            ],
            ["course_drop_impact"],
            id="v13-drop-008-singleton-balanced-compound",
        ),
        pytest.param(
            "هل حذف `DS332` يقفل علي مواد في الترم الجاي؟",
            [
                {
                    "capability": "rank_current_course_drop_impact",
                    "arguments": {
                        "objective": "prerequisite_continuity",
                        "course_codes": ["DS332"],
                    },
                }
            ],
            ["course_drop_impact"],
            id="v13-drop-009-prerequisite-continuity-compound",
        ),
        pytest.param(
            "هل أقدر أبني جدول كامل حول `DS341-M2` بدون تعارض؟",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                    },
                }
            ],
            ["timetable_build"],
            id="v13-pin-006-build-owns-clash-check",
        ),
        pytest.param(
            "أنشئ لي جدول جديد فيه `DS341` شعبة `M2`.",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                    },
                }
            ],
            ["timetable_build"],
            id="v17-pin-001-inline-code-pair-executes",
        ),
        pytest.param(
            "أبي `DS341` شعبة `M2` تكون موجودة في كل الخيارات.",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
                    },
                }
            ],
            ["timetable_build"],
            id="v17-pin-003-inline-code-pair-executes",
        ),
        pytest.param(
            "إذا ما نزلت `DS321` هذا الترم وش يصير؟",
            [
                {
                    "capability": "graduation_progress",
                    "arguments": {
                        "planning_baseline_kind": "registered_timetable",
                        "remove_current_courses": ["DS321"],
                    },
                }
            ],
            ["graduation_impact"],
            id="v13-whatif-002-graduation-only",
        ),
        pytest.param(
            "أقدر أنزل `DS491` ولا باقي لي متطلب؟",
            [
                {
                    "capability": "why_course_locked",
                    "arguments": {"course_code": "DS491"},
                }
            ],
            ["course_eligibility", "prerequisite_information"],
            id="v15-elig-003-explicit-dual-deliverable",
        ),
        pytest.param(
            "إيش متطلبات مقرر `DS491`؟",
            [
                {
                    "capability": "course_prerequisites",
                    "arguments": {"course_code": "DS491"},
                }
            ],
            ["prerequisite_information"],
            id="v15-prereq-001-exact-code-catalogue-requirements",
        ),
        pytest.param(
            "وش المتطلب السابق لـ `DS491`؟",
            [
                {
                    "capability": "course_prerequisites",
                    "arguments": {"course_code": "DS491"},
                }
            ],
            ["prerequisite_information"],
            id="v15-prereq-002-exact-code-catalogue-prerequisite",
        ),
        pytest.param(
            "أنشئ لي جدول جديد من الصفر.",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {"mode": "from_scratch"},
                }
            ],
            ["timetable_build"],
            id="v15-build-001-explicit-from-scratch-no-clarification",
        ),
        pytest.param(
            "ابنِ لي جدول بحد أقصى 15 ساعة.",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {"mode": "from_scratch", "max_credits": 15},
                }
            ],
            ["timetable_build"],
            id="v16-build-005-generic-build-is-from-scratch",
        ),
        pytest.param(
            "فيه مقررات مهمة أقدر أسجلها وما نزلتها؟",
            [{"capability": "my_progress", "arguments": {}}],
            ["course_priority", "available_courses"],
            id="v15-available-007-important-registerable-not-taken",
        ),
        pytest.param(
            (
                "جدولي الحالي فيه `DS332` و`DS341` و`DS321`. إذا اضطررت أحذف مقرر واحد، "
                "اختر المقرر الأقل تأثيراً على موعد تخرجي ووضح لي ليه."
            ),
            [
                {
                    "capability": "rank_current_course_drop_impact",
                    "arguments": {
                        "objective": "least_graduation_delay",
                        "course_codes": ["DS332", "DS341", "DS321"],
                    },
                }
            ],
            ["course_drop_impact"],
            id="v15-composite-002-multi-course-drop-ranking",
        ),
    ],
)
def test_v21_scripted_plans_execute_exactly_without_legacy_routing(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    requests: list[dict[str, Any]],
    outcomes: list[str],
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {"tool": name, "ok": True}

    def render(_language, _results, _style, *, planned_tools, **_kwargs):
        blocks = tuple((name, f"verified {name}") for name in planned_tools)
        return "\n".join(block for _name, block in blocks), True, blocks

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(runtime, "_safe_v21_planned_answer", render)
    monkeypatch.setattr(runtime, "check_answer", lambda *_args, **_kwargs: [])
    client = ScriptedClient(_planner_turn("execute", requests, outcomes=outcomes))

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [(request["capability"], request["arguments"]) for request in requests]
    assert result["agent"]["semantic_plan_requested_outcomes"] == outcomes
    assert result["agent"]["semantic_plan_tools"] == [request["capability"] for request in requests]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert len(client.calls) == 1


def _composite_003_plan(
    build_arguments: dict[str, Any],
) -> StudentTurnPlan:
    return StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall(
                capability="build_timetable_proposal",
                arguments=build_arguments,
            ),
            PlannedCapabilityCall(capability="my_progress", arguments={}),
        ),
        requested_outcomes=(
            StudentRequestOutcome.TIMETABLE_BUILD,
            StudentRequestOutcome.COURSE_PRIORITY,
        ),
    )


def _timetable_build_plan(build_arguments: dict[str, Any]) -> StudentTurnPlan:
    return StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall(
                capability="build_timetable_proposal",
                arguments=build_arguments,
            ),
        ),
        requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
    )


def _single_capability_plan(
    capability: str,
    arguments: dict[str, Any],
) -> StudentTurnPlan:
    return StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(PlannedCapabilityCall(capability=capability, arguments=arguments),),
    )


@pytest.mark.parametrize(
    ("omitted", "missing_path"),
    [
        ("max_credits", "build_timetable_proposal.max_credits"),
        ("pinned_sections", "build_timetable_proposal.pinned_sections"),
        ("must_take_courses", "build_timetable_proposal.must_take_courses"),
    ],
)
def test_v21_composite_build_requires_each_explicit_hard_constraint(
    omitted: str,
    missing_path: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    question = (
        "ابنِ لي جدول جديد من الصفر بحد أقصى 18 ساعة، ثبت فيه DS341-M2، "
        "وأعط الأولوية للمقررات اللي تمنع تأخر التخرج."
    )
    build_arguments = {
        "mode": "from_scratch",
        "course_codes": ["DS341"],
        "must_take_courses": ["DS341"],
        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
        "max_credits": 18,
    }
    build_arguments.pop(omitted)

    assert _v21_missing_explicit_constraint_paths(
        _composite_003_plan(build_arguments),
        question,
    ) == (missing_path,)


@pytest.mark.parametrize(
    ("question", "required_mode"),
    [
        ("أنشئ لي جدول جديد من الصفر.", "from_scratch"),
        ("Build a timetable from scratch.", "from_scratch"),
        ("أنشئ لي جدول جديد فيه DS341 شعبة M2.", "from_scratch"),
        ("ثبت DS341-M2 وابنِ باقي الجدول حوله.", "from_scratch"),
        ("أبي DS341 شعبة M2 تكون موجودة في كل الخيارات.", "from_scratch"),
        ("سو لي جدول جديد بشرط يكون فيه DS341-M2.", "from_scratch"),
        ("هل أقدر أبني جدول كامل حول DS341-M2 بدون تعارض؟", "from_scratch"),
        (
            "ثبت مقررين DS341-M2 وDS432-M3 وابنِ الباقي.",
            "from_scratch",
        ),
        ("لا تغير شعبة DS341-M2، بس عدل باقي الجدول.", "from_scratch"),
        ("Keep DS341-M2 fixed and adjust the rest.", "from_scratch"),
        (
            "Keep DS341 in my current timetable and adjust the rest.",
            "from_scratch",
        ),
        (
            "Retain DS341 from my current timetable and modify the rest.",
            "from_scratch",
        ),
        ("احتفظ بـ DS341 من الجدول الحالي وعدل باقي الجدول.", "from_scratch"),
        ("خل DS341 من جدولي الحالي وغير الباقي.", "from_scratch"),
        (
            "أنشئ لي جدول جديد واحتفظ بشعبة DS341-M2 من جدولي الحالي.",
            "from_scratch",
        ),
        (
            "Build me a new timetable, but keep DS341 from my current timetable.",
            "from_scratch",
        ),
        ("احتفظ بجدولي الحالي كامل وابنِ جدول حوله.", "around_current"),
        (
            "Keep my entire current timetable and build a timetable around it.",
            "around_current",
        ),
        (
            "Build a new timetable while preserving my entire current timetable.",
            "around_current",
        ),
        (
            "Create a full schedule but leave every current section unchanged.",
            "around_current",
        ),
        (
            "أنشئ لي جدول جديد مع الحفاظ على جدولي الحالي كامل.",
            "around_current",
        ),
        (
            "Keep all current sections, but actually build a fresh timetable and do not "
            "retain them.",
            "from_scratch",
        ),
    ],
)
def test_v21_selected_builder_requires_the_explicit_mode(
    question: str,
    required_mode: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    missing = _composite_003_plan({"must_take_courses": ["DS341"]})
    assert _v21_missing_explicit_constraint_paths(missing, question)[0] == (
        "build_timetable_proposal.mode"
    )
    correct = _composite_003_plan({"mode": required_mode, "must_take_courses": ["DS341"]})
    assert "build_timetable_proposal.mode" not in (
        _v21_missing_explicit_constraint_paths(correct, question)
    )
    wrong_mode = "around_current" if required_mode == "from_scratch" else "from_scratch"
    wrong = _composite_003_plan({"mode": wrong_mode, "must_take_courses": ["DS341"]})
    assert "build_timetable_proposal.mode" in (
        _v21_missing_explicit_constraint_paths(wrong, question)
    )


@pytest.mark.parametrize(
    ("question", "expected_modes"),
    [
        (
            "Keep my entire current timetable and build a timetable around it.",
            ["from_scratch", "around_current"],
        ),
        (
            "Keep all my current sections and build the proposal around them.",
            ["around_current"],
        ),
        (
            "احتفظ بجدولي الحالي كامل وابنِ جدول حوله.",
            ["from_scratch", "around_current"],
        ),
        (
            "Keep DS341 in my current timetable and adjust the rest.",
            ["from_scratch"],
        ),
        (
            "خل DS341 من جدولي الحالي وغير الباقي.",
            ["from_scratch"],
        ),
        (
            "أنشئ لي جدول جديد واحتفظ بشعبة DS341-M2 من جدولي الحالي.",
            ["from_scratch"],
        ),
        (
            "Build me a new timetable, but keep DS341 from my current timetable.",
            ["from_scratch"],
        ),
        (
            "Build a new timetable while preserving my entire current timetable.",
            ["from_scratch", "around_current"],
        ),
        (
            "Create a full schedule but leave every current section unchanged.",
            ["from_scratch", "around_current"],
        ),
        (
            "أنشئ لي جدول جديد مع الحفاظ على جدولي الحالي كامل.",
            ["from_scratch", "around_current"],
        ),
        (
            "Keep all current sections, but actually build a fresh timetable and do not "
            "retain them.",
            ["from_scratch"],
        ),
    ],
)
def test_v21_explicit_timetable_modes_distinguish_whole_baseline_from_selective_pins(
    question: str,
    expected_modes: list[str],
) -> None:
    from core.services.student_advisor_v2 import _v21_explicit_timetable_modes

    assert _v21_explicit_timetable_modes(question) == expected_modes


@pytest.mark.parametrize(
    "question",
    [
        "أنشئ لي جدول جديد فيه CS371 شعبة M1.",
        "أبي CS371 شعبة M1 تكون موجودة في كل الخيارات.",
        "سو لي جدول جديد بشرط يكون فيه CS371-M1.",
        "ابن لي جدول كامل حول CS371-M1.",
    ],
)
def test_v21_natural_builder_pin_requires_correlated_pin_and_course(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    assert _v21_missing_explicit_constraint_paths(
        _composite_003_plan({"mode": "from_scratch"}),
        question,
    ) == (
        "build_timetable_proposal.pinned_sections",
        "build_timetable_proposal.must_take_courses",
    )


@pytest.mark.parametrize(
    ("question", "max_values", "additional_values"),
    [
        ("Do not use max 12; use at most 18", [18], []),
        ("current schedule is 15, capped at 18", [18], []),
        ("جدولي 16 ساعة حالياً، أبغى أضيف مادة 3 ساعات", [], [3]),
        ("لا تسوي لي جدول 18 ساعة؛ خله بحد أقصى 15", [15], []),
        ("Build a schedule not capped at 18 credits; cap it at 15", [15], []),
        ("الحد الأعلى 15 ساعة", [15], []),
        ("ما أبي أكثر من 15 ساعة", [15], []),
        ("أبي مادة بثلاث ساعات", [], [3]),
    ],
)
def test_v21_credit_constraint_extractors_are_polarity_and_role_bound(
    question: str,
    max_values: list[int],
    additional_values: list[int],
) -> None:
    from core.services.student_advisor_v2 import (
        _V21_ADDITIONAL_CREDIT_PATTERNS,
        _V21_MAX_CREDIT_PATTERNS,
        _v21_credit_values,
    )

    assert _v21_credit_values(question, _V21_MAX_CREDIT_PATTERNS) == max_values
    assert _v21_credit_values(question, _V21_ADDITIONAL_CREDIT_PATTERNS) == additional_values


@pytest.mark.parametrize(
    "question",
    [
        "Use at most 18 credits; actually cap it at 15 credits.",
        "استخدم حد أقصى 18 ساعة، لكن خله بحد أقصى 15 ساعة.",
        "Do not cap it at 18 credits; cap it at 15 credits.",
    ],
)
@pytest.mark.parametrize(
    "owner",
    [
        "build_timetable_proposal",
        "recommend_feasible_course_addition",
        "rank_current_course_drop_impact",
        "improve_current_timetable",
    ],
)
def test_v21_max_credit_gate_requires_the_final_active_value_for_every_owner(
    question: str,
    owner: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    path = f"{owner}.max_credits"
    assert _v21_missing_explicit_constraint_paths(
        _single_capability_plan(owner, {}),
        question,
    ) == (path,)
    assert _v21_missing_explicit_constraint_paths(
        _single_capability_plan(owner, {"max_credits": 18}),
        question,
    ) == (path,)
    assert (
        _v21_missing_explicit_constraint_paths(
            _single_capability_plan(owner, {"max_credits": 15}),
            question,
        )
        == ()
    )


def test_v21_additional_credit_gate_requires_the_final_corrected_value() -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    question = "Add one 3-credit course; actually add one 4-credit course."
    path = "recommend_feasible_course_addition.additional_credit_hours"
    for arguments in ({}, {"additional_credit_hours": 3}):
        assert _v21_missing_explicit_constraint_paths(
            _single_capability_plan(
                "recommend_feasible_course_addition",
                arguments,
            ),
            question,
        ) == (path,)
    assert (
        _v21_missing_explicit_constraint_paths(
            _single_capability_plan(
                "recommend_feasible_course_addition",
                {"additional_credit_hours": 4},
            ),
            question,
        )
        == ()
    )


@pytest.mark.parametrize(
    "question",
    [
        "Show the top 5 courses; actually show the top 3 courses.",
        "عطني أفضل 5 مقررات، لكن خلها أفضل 3 مقررات.",
    ],
)
def test_v21_priority_limit_gate_requires_the_final_corrected_value(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    path = "my_progress.priority_limit"
    for arguments in ({}, {"priority_limit": 5}):
        assert _v21_missing_explicit_constraint_paths(
            _single_capability_plan("my_progress", arguments),
            question,
        ) == (path,)
    assert (
        _v21_missing_explicit_constraint_paths(
            _single_capability_plan("my_progress", {"priority_limit": 3}),
            question,
        )
        == ()
    )


def test_v21_repeated_scalar_correction_preserves_occurrence_order() -> None:
    from core.services.student_advisor_v2 import (
        _V21_MAX_CREDIT_PATTERNS,
        _v21_credit_values,
        _v21_missing_explicit_constraint_paths,
        _v21_priority_limits,
    )

    max_question = "Use at most 18 credits; actually cap it at 15; actually use at most 18."
    assert _v21_credit_values(max_question, _V21_MAX_CREDIT_PATTERNS) == [18, 15, 18]
    assert (
        _v21_missing_explicit_constraint_paths(
            _single_capability_plan(
                "build_timetable_proposal",
                {"max_credits": 18},
            ),
            max_question,
        )
        == ()
    )
    assert _v21_missing_explicit_constraint_paths(
        _single_capability_plan(
            "build_timetable_proposal",
            {"max_credits": 15},
        ),
        max_question,
    ) == ("build_timetable_proposal.max_credits",)

    priority_question = "Show the top 5 courses; actually top 3 courses; actually top 5 courses."
    assert _v21_priority_limits(priority_question) == [5, 3, 5]
    assert (
        _v21_missing_explicit_constraint_paths(
            _single_capability_plan("my_progress", {"priority_limit": 5}),
            priority_question,
        )
        == ()
    )
    assert _v21_missing_explicit_constraint_paths(
        _single_capability_plan("my_progress", {"priority_limit": 3}),
        priority_question,
    ) == ("my_progress.priority_limit",)


@pytest.mark.parametrize(
    ("question", "expected_modes", "expected_max", "expected_priority"),
    [
        (
            "Last term, keep DS341-M2 fixed; build a fresh timetable now.",
            ["from_scratch"],
            [],
            [],
        ),
        (
            "Do not build around current sections; build a fresh timetable.",
            ["from_scratch"],
            [],
            [],
        ),
        (
            "Last term, keep current timetable; now build from scratch.",
            ["from_scratch"],
            [],
            [],
        ),
        (
            "Last term, cap was 18 credits; now cap it at 15 credits.",
            [],
            [15],
            [],
        ),
        (
            "Last term, top 5 courses; now show top 3 courses.",
            [],
            [],
            [3],
        ),
        (
            "الترم الماضي، احتفظ بجدولي الحالي؛ الآن ابن جدول من الصفر.",
            ["from_scratch"],
            [],
            [],
        ),
        (
            "الترم الماضي، كان الحد الأقصى 18 ساعة؛ الآن خله بحد أقصى 15 ساعة.",
            [],
            [15],
            [],
        ),
        (
            "الترم الماضي، أفضل 5 مقررات؛ الآن عطيني أفضل 3 مقررات.",
            [],
            [],
            [3],
        ),
    ],
)
def test_v21_mode_and_scalar_extractors_split_before_punctuation_is_normalised(
    question: str,
    expected_modes: list[str],
    expected_max: list[int],
    expected_priority: list[int],
) -> None:
    from core.services.student_advisor_v2 import (
        _V21_MAX_CREDIT_PATTERNS,
        _v21_credit_values,
        _v21_explicit_timetable_modes,
        _v21_priority_limits,
    )

    assert _v21_explicit_timetable_modes(question) == expected_modes
    assert _v21_credit_values(question, _V21_MAX_CREDIT_PATTERNS) == expected_max
    assert _v21_priority_limits(question) == expected_priority


def test_v21_pin_completeness_uses_positive_current_turn_polarity_only() -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    unpinned = _composite_003_plan({"mode": "from_scratch", "must_take_courses": []})
    assert (
        _v21_missing_explicit_constraint_paths(
            unpinned,
            "لا تثبت DS341-M2؛ ابنِ جدولاً جديداً.",
        )
        == ()
    )
    assert (
        _v21_missing_explicit_constraint_paths(
            unpinned,
            "DS341-M2 was pinned last term. Build a new timetable.",
        )
        == ()
    )
    assert _v21_missing_explicit_constraint_paths(
        unpinned,
        "Keep DS341-M2 fixed and build a new timetable.",
    ) == (
        "build_timetable_proposal.pinned_sections",
        "build_timetable_proposal.must_take_courses",
    )


@pytest.mark.parametrize(
    "question",
    [
        "Pin DS341-M2, DS432-M3, and build the rest.",
        "ثبت DS341-M2، DS432-M3، وابن الباقي.",
        "Pin DS341-M2, `DS432` section `M3`, and build the rest.",
        "ثبت DS341-M2، `DS432` شعبة `M3`، وابن الباقي.",
    ],
)
def test_v21_coordinated_pin_list_requires_every_literal_pair(question: str) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_pins,
        _v21_missing_explicit_constraint_paths,
    )

    expected_pins = [
        {"course_code": "DS341", "section_label": "M2"},
        {"course_code": "DS432", "section_label": "M3"},
    ]
    assert _v21_explicit_positive_pins(question) == expected_pins

    omitted_second = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341", "DS432"],
            "pinned_sections": expected_pins[:1],
        }
    )
    assert _v21_missing_explicit_constraint_paths(omitted_second, question) == (
        "build_timetable_proposal.pinned_sections",
    )
    complete = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341", "DS432"],
            "pinned_sections": expected_pins,
        }
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Pin DS341-M2, but do not pin DS432-M3; build the rest.",
            [{"course_code": "DS341", "section_label": "M2"}],
        ),
        (
            "ثبت DS341-M2، لكن لا تثبت DS432-M3؛ وابن الباقي.",
            [{"course_code": "DS341", "section_label": "M2"}],
        ),
        ("Do not pin DS341-M2, DS432-M3; build the rest.", []),
        ("Last term, pin DS341-M2, DS432-M3 was the instruction.", []),
    ],
)
def test_v21_coordinated_pin_list_stops_at_negative_or_historical_scope(
    question: str,
    expected: list[dict[str, str]],
) -> None:
    from core.services.student_advisor_v2 import _v21_explicit_positive_pins

    assert _v21_explicit_positive_pins(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Build a new timetable; DS341-M2 must stay fixed.",
        "Leave DS341-M2 unchanged and rebuild the rest.",
        "Make sure DS341-M2 remains in every option.",
        "خل DS341-M2 زي ما هي وابن الباقي.",
        "لا تحرك DS341-M2 وابن الباقي.",
        "DS341-M2 لازم تظل في كل الخيارات.",
    ],
)
def test_v21_exact_pin_retention_paraphrases_require_the_literal_section(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_pins,
        _v21_missing_explicit_constraint_paths,
    )

    pin = {"course_code": "DS341", "section_label": "M2"}
    assert _v21_explicit_positive_pins(question) == [pin]
    incomplete = _timetable_build_plan({"mode": "from_scratch", "must_take_courses": ["DS341"]})
    assert _v21_missing_explicit_constraint_paths(incomplete, question) == (
        "build_timetable_proposal.pinned_sections",
    )
    complete = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        }
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "Do not leave DS341-M2 unchanged; build the rest.",
        "Last term, DS341-M2 must stay fixed was the rule.",
        "DS341 must stay fixed and build the rest.",
    ],
)
def test_v21_exact_pin_retention_paraphrases_keep_scope_and_identity_guards(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import _v21_explicit_positive_pins

    assert _v21_explicit_positive_pins(question) == []


@pytest.mark.parametrize(
    "question",
    [
        "Pin section M2 for DS341 and build the rest.",
        "Keep section M2 of DS341 and create a fresh timetable.",
        "ثبّت شعبة M2 لمقرر DS341 وابن الباقي.",
    ],
)
def test_v21_reverse_order_literal_course_section_pair_is_correlated_without_guessing(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_pins,
        _v21_missing_explicit_constraint_paths,
        _v21_requires_pin_identity_clarification,
    )

    pin = {"course_code": "DS341", "section_label": "M2"}
    assert _v21_explicit_positive_pins(question) == [pin]
    assert _v21_requires_pin_identity_clarification(question) is False
    incomplete = _timetable_build_plan({"mode": "from_scratch", "must_take_courses": ["DS341"]})
    assert _v21_missing_explicit_constraint_paths(incomplete, question) == (
        "build_timetable_proposal.pinned_sections",
    )
    complete = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        }
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "أنشئ لي جدول جديد فيه `DS341` شعبة `M2`.",
        "أبي `DS341` شعبة `M2` تكون موجودة في كل الخيارات.",
    ],
)
def test_v21_inline_code_wrappers_preserve_separate_literal_pin_pair(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_correlated_section_pins,
        _v21_explicit_positive_pins,
        _v21_missing_explicit_constraint_paths,
        _v21_requires_pin_identity_clarification,
    )

    pin = {"course_code": "DS341", "section_label": "M2"}
    assert _v21_correlated_section_pins(question) == [pin]
    assert _v21_explicit_positive_pins(question) == [pin]
    assert _v21_requires_pin_identity_clarification(question) is False

    missing_pin = _timetable_build_plan({"mode": "from_scratch", "must_take_courses": ["DS341"]})
    assert _v21_missing_explicit_constraint_paths(missing_pin, question) == (
        "build_timetable_proposal.pinned_sections",
    )
    complete = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        }
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "ثبت المقررين `DS341` و`DS432` في شعبة `M2` وابن الباقي.",
        "Pin `DS341` and `DS432` in section `M2`, then build the rest.",
    ],
)
def test_v21_inline_code_folding_does_not_cross_product_ambiguous_literals(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_pins,
        _v21_missing_explicit_constraint_paths,
        _v21_pin_constraint_state,
    )

    assert _v21_explicit_positive_pins(question) == []
    assert _v21_pin_constraint_state(question) == (
        [],
        ["DS341", "DS432"],
        True,
    )
    execute = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341", "DS432"],
        }
    )
    assert _v21_missing_explicit_constraint_paths(execute, question) == ("clarification_kind",)


@pytest.mark.parametrize(
    ("question", "expected_pins", "expected_courses", "expected_ambiguous"),
    [
        (
            "Pin M2, and do not pin DS341-M3; build a fresh timetable.",
            [],
            [],
            True,
        ),
        (
            "Do not pin DS341-M3, pin M2, and build fresh.",
            [],
            [],
            True,
        ),
        ("Pin M2, but do not pin it; build fresh.", [], [], False),
        (
            "Pin M2, but pin DS341-M2 and build rest.",
            [{"course_code": "DS341", "section_label": "M2"}],
            ["DS341"],
            False,
        ),
        (
            "Pin DS341-M2, DS432-M3, and compare section M4 as an example.",
            [
                {"course_code": "DS341", "section_label": "M2"},
                {"course_code": "DS432", "section_label": "M3"},
            ],
            ["DS341", "DS432"],
            False,
        ),
        (
            "Keep DS341-M2, but actually do not pin DS341-M2; build fresh.",
            [],
            [],
            False,
        ),
        (
            "Keep DS341-M2, but actually do not pin it; build a fresh timetable.",
            [],
            [],
            False,
        ),
        (
            "ثبت DS341-M2، لكن لا تثبته؛ وابنِ جدول جديد.",
            [],
            [],
            False,
        ),
        (
            "Keep DS341, but actually do not retain it; build a fresh timetable.",
            [],
            [],
            False,
        ),
        ("Pin M2, but actually do not pin M2; build fresh.", [], [], False),
        (
            "Pin DS341-M3, but do not pin DS341-M2; build fresh.",
            [{"course_code": "DS341", "section_label": "M3"}],
            ["DS341"],
            False,
        ),
        (
            "Keep DS341, but do not pin DS341-M2; build fresh.",
            [],
            ["DS341"],
            False,
        ),
        (
            "Pin M2, and also pin DS341-M2; build the rest.",
            [{"course_code": "DS341", "section_label": "M2"}],
            ["DS341"],
            True,
        ),
        (
            "Pin M2, DS341-M2, and build the rest.",
            [{"course_code": "DS341", "section_label": "M2"}],
            ["DS341"],
            True,
        ),
        ("الفصل الماضي ثبت DS341-M2؛ ابنِ جدول جديد الآن.", [], [], False),
        ("ثبت DS341-M2 للفصل الماضي؛ ابنِ جدول جديد الآن.", [], [], False),
        (
            "Pin DS341-M2 instead of DS341-F2 and build fresh.",
            [{"course_code": "DS341", "section_label": "M2"}],
            ["DS341"],
            False,
        ),
        (
            "ثبت DS341-M2 بدل DS341-F2 وابن الباقي.",
            [{"course_code": "DS341", "section_label": "M2"}],
            ["DS341"],
            False,
        ),
        (
            "Pin DS341-M2, actually DS341-F2; build fresh.",
            [{"course_code": "DS341", "section_label": "F2"}],
            ["DS341"],
            False,
        ),
        (
            "Pin DS341-M2, correction: DS341-F2; build fresh.",
            [{"course_code": "DS341", "section_label": "F2"}],
            ["DS341"],
            False,
        ),
        (
            "ثبت DS341-M2، أقصد DS341-F2؛ وابن الباقي.",
            [{"course_code": "DS341", "section_label": "F2"}],
            ["DS341"],
            False,
        ),
        ("Pin M2, actually F2; build fresh.", [], [], True),
        ("ثبت M2، أقصد F2؛ وابن الباقي.", [], [], True),
        ("Pin M2 and F2; build fresh.", [], [], True),
        ("Pin M2 or F2; build fresh.", [], [], True),
        ("ثبت M2 أو F2 وابن الباقي.", [], [], True),
        ("Pin either DS341-M2 or DS341-F2; build fresh.", [], [], True),
        ("Pin DS341-M2 or DS341-F2; build fresh.", [], [], True),
        ("Pin DS341-M2 and DS341-F2; build fresh.", [], [], True),
        ("ثبت إما DS341-M2 أو DS341-F2 وابن الباقي.", [], [], True),
        ("ثبت DS341-M2 و DS341-F2 وابن الباقي.", [], [], True),
    ],
)
def test_v21_pin_constraint_state_applies_ordered_literal_corrections(
    question: str,
    expected_pins: list[dict[str, str]],
    expected_courses: list[str],
    expected_ambiguous: bool,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_pins,
        _v21_explicit_positive_retained_courses,
        _v21_requires_pin_identity_clarification,
    )

    assert _v21_explicit_positive_pins(question) == expected_pins
    assert _v21_explicit_positive_retained_courses(question) == expected_courses
    assert _v21_requires_pin_identity_clarification(question) is expected_ambiguous


@pytest.mark.parametrize(
    "question",
    [
        "Pin DS341-M2 instead of DS341-F2 and build fresh.",
        "ثبت DS341-M2 بدل DS341-F2 وابن الباقي.",
    ],
)
def test_v21_instead_of_pin_requires_only_the_selected_exact_pair(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    correct = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
        }
    )
    wrong = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [{"course_code": "DS341", "section_label": "F2"}],
        }
    )

    assert _v21_missing_explicit_constraint_paths(correct, question) == ()
    assert _v21_missing_explicit_constraint_paths(wrong, question) == (
        "build_timetable_proposal.pinned_sections",
    )


@pytest.mark.parametrize(
    "question",
    [
        "Pin DS341-M2, actually DS341-F2; build fresh.",
        "Pin DS341-M2, correction: DS341-F2; build fresh.",
        "ثبت DS341-M2، أقصد DS341-F2؛ وابن الباقي.",
    ],
)
def test_v21_exact_pin_correction_requires_only_the_final_pair(question: str) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    correct = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [{"course_code": "DS341", "section_label": "F2"}],
        }
    )
    stale = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
        }
    )

    assert _v21_missing_explicit_constraint_paths(correct, question) == ()
    assert _v21_missing_explicit_constraint_paths(stale, question) == (
        "build_timetable_proposal.pinned_sections",
    )


@pytest.mark.parametrize(
    "question",
    [
        "Pin M2, actually F2; build fresh.",
        "ثبت M2، أقصد F2؛ وابن الباقي.",
        "Pin M2 and F2; build fresh.",
        "Pin M2 or F2; build fresh.",
        "ثبت M2 أو F2 وابن الباقي.",
        "Pin either DS341-M2 or DS341-F2; build fresh.",
        "Pin DS341-M2 or DS341-F2; build fresh.",
        "Pin DS341-M2 and DS341-F2; build fresh.",
        "ثبت إما DS341-M2 أو DS341-F2 وابن الباقي.",
        "ثبت DS341-M2 و DS341-F2 وابن الباقي.",
    ],
)
def test_v21_uncorrelated_pin_corrections_and_alternatives_require_clarification(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    execute = _timetable_build_plan({"mode": "from_scratch"})
    clarify = StudentTurnPlan(
        decision=TurnPlanDecision.CLARIFY,
        evidence_requests=(),
        clarification_kind=ClarificationKind.COURSE_OR_SECTION_IDENTITY,
        clarification_question="Which exact course-section pair?",
        requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
    )

    assert _v21_missing_explicit_constraint_paths(execute, question) == ("clarification_kind",)
    assert _v21_missing_explicit_constraint_paths(clarify, question) == ()


@pytest.mark.parametrize(
    ("question", "expected_courses"),
    [
        ("Pin DS341 and build the rest.", ["DS341"]),
        ("ثبّت DS341 وابنِ الباقي.", ["DS341"]),
        ("Keep DS341 in every option and build a timetable.", ["DS341"]),
        ("خل DS341 في كل الخيارات وابنِ الجدول.", ["DS341"]),
        ("Pin DS341, DS432, and build the rest.", ["DS341", "DS432"]),
        ("ثبت DS341، DS432، وابن الباقي.", ["DS341", "DS432"]),
    ],
)
def test_v21_course_only_pin_or_retain_requires_every_course_without_a_section(
    question: str,
    expected_courses: list[str],
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_retained_courses,
        _v21_missing_explicit_constraint_paths,
    )

    assert _v21_explicit_positive_retained_courses(question) == expected_courses
    incomplete = _timetable_build_plan(
        {"mode": "from_scratch", "must_take_courses": expected_courses[:1]}
    )
    expected_missing = (
        ("build_timetable_proposal.must_take_courses",) if len(expected_courses) > 1 else ()
    )
    if len(expected_courses) == 1:
        incomplete = _timetable_build_plan({"mode": "from_scratch"})
        expected_missing = ("build_timetable_proposal.must_take_courses",)
    assert _v21_missing_explicit_constraint_paths(incomplete, question) == expected_missing
    complete = _timetable_build_plan(
        {"mode": "from_scratch", "must_take_courses": expected_courses}
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    ("question", "expected_courses"),
    [
        ("Do not pin DS341, DS432; build the rest.", []),
        ("Pin DS341, but DS432 is optional; build the rest.", ["DS341"]),
        ("Last term, pin DS341, DS432 was the instruction.", []),
        ("لا تثبت DS341، DS432؛ وابن الباقي.", []),
    ],
)
def test_v21_course_only_retain_scope_stops_at_negation_context_or_adversative(
    question: str,
    expected_courses: list[str],
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_positive_retained_courses,
    )

    assert _v21_explicit_positive_retained_courses(question) == expected_courses


@pytest.mark.parametrize(
    "question",
    [
        "I must take DS341, DS432, and build a new timetable.",
        "لازم آخذ DS341، DS432، وابنِ جدول جديد.",
    ],
)
def test_v21_coordinated_must_take_list_requires_every_course(question: str) -> None:
    from core.services.student_advisor_v2 import (
        _v21_explicit_must_take_courses,
        _v21_missing_explicit_constraint_paths,
    )

    assert _v21_explicit_must_take_courses(question) == ["DS341", "DS432"]
    incomplete = _timetable_build_plan({"mode": "from_scratch", "must_take_courses": ["DS341"]})
    assert _v21_missing_explicit_constraint_paths(incomplete, question) == (
        "build_timetable_proposal.must_take_courses",
    )
    complete = _timetable_build_plan(
        {"mode": "from_scratch", "must_take_courses": ["DS341", "DS432"]}
    )
    assert _v21_missing_explicit_constraint_paths(complete, question) == ()


@pytest.mark.parametrize(
    ("question", "expected_courses"),
    [
        ("I must take DS341, but DS432 is optional; build a timetable.", ["DS341"]),
        ("I do not have to take DS341, DS432; build a timetable.", []),
        ("Last term, I had to take DS341, DS432.", []),
        ("لازم آخذ DS341، لكن DS432 اختياري؛ ابنِ جدول.", ["DS341"]),
        ("I must take DS341, but DS341 is not required; build fresh.", []),
        ("لازم آخذ DS341، لكن DS341 مو لازم؛ ابنِ جدول جديد.", []),
    ],
)
def test_v21_must_take_list_stops_at_negation_context_or_adversative(
    question: str,
    expected_courses: list[str],
) -> None:
    from core.services.student_advisor_v2 import _v21_explicit_must_take_courses

    assert _v21_explicit_must_take_courses(question) == expected_courses


@pytest.mark.parametrize(
    "question",
    [
        "ثبّت M2 وابنِ الباقي حولها.",
        "Keep DS341's current section fixed and adjust the rest.",
        "لا تغير شعبة DS341 من الجدول الحالي، بس عدل باقي الجدول.",
    ],
)
def test_v21_uncorrelated_positive_section_pin_requires_typed_clarification(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
        _v21_requires_pin_identity_clarification,
    )

    assert _v21_requires_pin_identity_clarification(question) is True
    execute = _timetable_build_plan(
        {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
        }
    )
    assert _v21_missing_explicit_constraint_paths(execute, question)[0] == ("clarification_kind")
    clarify = StudentTurnPlan(
        decision=TurnPlanDecision.CLARIFY,
        evidence_requests=(),
        clarification_kind=ClarificationKind.COURSE_OR_SECTION_IDENTITY,
        clarification_question="Which exact course-section pair?",
        requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
    )
    assert _v21_missing_explicit_constraint_paths(clarify, question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "Keep all current sections and build around them.",
        "Retain my current sections and create a schedule.",
        "احتفظ بكل الشعب الحالية وابن الجدول حولها.",
        "لا تغير الشعب الحالية وابن حولها.",
    ],
)
def test_v21_whole_baseline_sections_do_not_trigger_pin_identity_clarification(
    question: str,
) -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
        _v21_requires_pin_identity_clarification,
    )

    assert _v21_requires_pin_identity_clarification(question) is False
    plan = _timetable_build_plan({"mode": "around_current"})
    assert _v21_missing_explicit_constraint_paths(plan, question) == ()


def test_v21_selected_addition_owner_requires_explicit_credit_and_pin_controls() -> None:
    from core.services.student_advisor_v2 import (
        _v21_missing_explicit_constraint_paths,
    )

    plan = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall(
                capability="recommend_feasible_course_addition",
                arguments={"objective": "timetable_fit"},
            ),
        ),
        requested_outcomes=(StudentRequestOutcome.COURSE_ADDITION,),
    )

    assert _v21_missing_explicit_constraint_paths(
        plan,
        "Keep DS341-M2 and add one 3-credit course, capped at 18 credits.",
    ) == (
        "recommend_feasible_course_addition.max_credits",
        "recommend_feasible_course_addition.additional_credit_hours",
        "recommend_feasible_course_addition.pinned_sections",
    )


def test_v21_repairs_missing_top_n_control_then_executes_once(monkeypatch) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {"tool": name, "ok": True}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(
        runtime,
        "_safe_v21_planned_answer",
        lambda *_args, planned_tools, **_kwargs: (
            "verified priority",
            True,
            tuple((name, "verified priority") for name in planned_tools),
        ),
    )
    monkeypatch.setattr(runtime, "check_answer", lambda *_args, **_kwargs: [])
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_progress", "arguments": {}}],
            outcomes=["course_priority"],
        ),
        _planner_turn(
            "execute",
            [
                {
                    "capability": "my_progress",
                    "arguments": {"priority_limit": 5},
                }
            ],
            outcomes=["course_priority"],
        ),
    )

    result = answer_student_advisor_v21(
        question="عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("my_progress", {"priority_limit": 5})]
    assert len(client.calls) == 2
    assert result["agent"]["semantic_plan_failure_reason"] == ""
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_plan_missing_constraint_paths"] == [
        "my_progress.priority_limit"
    ]
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "",
        "repair": {"attempted": True, "result": "succeeded"},
        "missing_field_paths": ["my_progress.priority_limit"],
    }
    repair_message = client.calls[1]["messages"][-1]["content"]
    assert "missing_field_paths=my_progress.priority_limit" in repair_message
    assert "=5" not in repair_message


def test_v21_fails_closed_after_two_composite_constraint_omissions(
    monkeypatch,
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail(
            "an explicit-constraint-incomplete plan must not execute"
        ),
    )
    incomplete_requests = [
        {
            "capability": "build_timetable_proposal",
            "arguments": {
                "mode": "around_current",
                "course_codes": ["DS341"],
            },
        },
        {"capability": "my_progress", "arguments": {}},
    ]
    rejected = _planner_turn(
        "execute",
        incomplete_requests,
        outcomes=["timetable_build", "course_priority"],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question=(
            "ابنِ لي جدول جديد من الصفر بحد أقصى 18 ساعة، ثبت فيه DS341-M2، "
            "وأعط الأولوية للمقررات اللي تمنع تأخر التخرج."
        ),
        principal=_principal(),
        llm_client=client,
    )

    expected_paths = [
        "build_timetable_proposal.mode",
        "build_timetable_proposal.max_credits",
        "build_timetable_proposal.pinned_sections",
        "build_timetable_proposal.must_take_courses",
    ]
    assert len(client.calls) == 2
    assert result["agent"]["inference_calls"] == 2
    assert result["agent"]["tools_called"] == []
    assert result["agent"]["semantic_plan_failure_reason"] == ("constraint_coverage_failed")
    assert result["agent"]["semantic_outcome_coverage"]["reason"] == ("constraint_coverage_failed")
    assert result["agent"]["semantic_plan_execution_complete"] is False
    assert result["agent"]["semantic_plan_missing_constraint_paths"] == expected_paths
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "constraint_coverage_failed",
        "repair": {"attempted": True, "result": "failed"},
        "missing_field_paths": expected_paths,
    }
    repair_message = client.calls[1]["messages"][-1]["content"]
    assert "DS341" not in repair_message
    assert "M2" not in repair_message
    assert "18" not in repair_message


def test_v21_unsupported_registration_action_is_server_owned_and_read_only(
    monkeypatch,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: executed.append(name),
    )
    monkeypatch.setattr(
        runtime,
        "_seed_policy_evidence",
        lambda *_args, **_kwargs: pytest.fail(
            "an unsupported zero-tool V2.1 plan prefetched policy evidence"
        ),
    )
    client = ScriptedClient(
        _planner_turn(
            "unsupported",
            outcomes=["registration_action"],
        )
    )

    result = answer_student_advisor_v21(
        question="سجل لي AI331 الآن في البوابة.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == []
    assert result["agent"]["semantic_plan_decision"] == "unsupported"
    assert result["agent"]["semantic_plan_requested_outcomes"] == ["registration_action"]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["policy_prefetched"] is False
    assert result["agent"]["tool_results"] == []
    assert result["agent"]["evidence_audit"]["tool_names"] == []
    assert result["agent"]["evidence_audit"]["semantic_plan"] == {
        "decision": "unsupported",
        "clarification_kind": "none",
        "requested_outcomes": ["registration_action"],
        "coverage": {"valid": True, "reason": ""},
    }
    assert "للقراءة والتحليل فقط" in result["answer"]
    assert "لا يستطيع تسجيل" in result["answer"]


def test_v21_credit_load_comparison_is_typed_unsupported_without_forecast(
    monkeypatch,
) -> None:
    import core.services.student_advisor_v2 as runtime

    executed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: executed.append(name),
    )
    client = ScriptedClient(
        _planner_turn(
            "unsupported",
            outcomes=["credit_load_comparison"],
        )
    )

    result = answer_student_advisor_v21(
        question="لو أخذت 12 ساعة بدل 18، هل يتغير موعد التخرج؟",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == []
    assert result["agent"]["semantic_plan_decision"] == "unsupported"
    assert result["agent"]["semantic_plan_requested_outcomes"] == ["credit_load_comparison"]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert "12 مقابل 18" in result["answer"]
    assert "لن أعرض توقعًا ثابتًا" in result["answer"]


def test_v21_generic_academic_replacement_search_executes_once(monkeypatch) -> None:
    import core.services.student_advisor_v2 as runtime

    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {
            "tool": "graduation_progress",
            "ok": True,
            "planning_baseline_kind": "recommended_current_term",
            "what_if": {
                "valid": True,
                "mode": "replacement_search",
                "improving_replacements": [],
                "improving_replacements_found": 0,
                "no_proven_improvement": True,
                "pairs_evaluated": 65,
                "search_truncated": False,
                "replacement_results_truncated": False,
                "unproven_blocker_progress_pairs": 0,
            },
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            requests=[
                {
                    "capability": "graduation_progress",
                    "arguments": {
                        "planning_baseline_kind": "recommended_current_term",
                        "search_better_replacements": True,
                    },
                }
            ],
            outcomes=["course_replacement"],
        )
    )

    result = answer_student_advisor_v21(
        question="هل فيه تبديل بين مقررين يخلي تخرجي أسرع؟",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [
        (
            "graduation_progress",
            {
                "planning_baseline_kind": "recommended_current_term",
                "search_better_replacements": True,
            },
        )
    ]
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert "لم يثبت البحث الأكاديمي المحدود" in result["answer"]


def test_v21_unsupported_renderer_composes_multiple_typed_boundaries() -> None:
    client = ScriptedClient(
        _planner_turn(
            "unsupported",
            outcomes=["registration_action", "credit_load_comparison"],
        )
    )

    result = answer_student_advisor_v21(
        question="قارن 12 مع 18 ساعة ثم طبّق الخيار الأفضل في البوابة.",
        principal=_principal(),
        llm_client=client,
    )

    assert "لا يستطيع تسجيل" in result["answer"]
    assert "12 مقابل 18" in result["answer"]


def test_v21_executes_supported_advice_and_refuses_the_requested_write(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_ELIGIBLE_CANDIDATES",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": "balanced",
            "constraints": {},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
            outcomes=["course_addition", "registration_action"],
        )
    )

    result = answer_student_advisor_v21(
        question="اختر أفضل مقرر إضافي وسجله لي في البوابة.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("recommend_feasible_course_addition", {"objective": "balanced"})]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة" in result["answer"]
    assert "لا يستطيع تسجيل" in result["answer"]


def test_v21_executes_supported_advice_and_bounds_credit_load_comparison(
    monkeypatch,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_ELIGIBLE_CANDIDATES",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": "balanced",
            "constraints": {},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
            outcomes=["course_addition", "credit_load_comparison"],
        )
    )

    result = answer_student_advisor_v21(
        question="اختر أفضل مقرر إضافي، وقارن أيضًا 12 مع 18 ساعة للتخرج.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("recommend_feasible_course_addition", {"objective": "balanced"})]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة" in result["answer"]
    assert "12 مقابل 18" in result["answer"]
    assert "لن أعرض توقعًا ثابتًا" in result["answer"]


def test_v21_refuses_an_underplanned_compound_outcome_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: executed.append(name),
    )
    underplanned = _planner_turn(
        "execute",
        [{"capability": "my_progress", "arguments": {}}],
        outcomes=["course_addition"],
    )
    client = ScriptedClient(underplanned, underplanned)

    result = answer_student_advisor_v21(
        question="اختر لي مقرر واحد أضيفه ولا يتعارض مع جدولي.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == []
    assert result["agent"]["semantic_outcome_coverage_refused"] is True
    assert result["agent"]["semantic_outcome_coverage"]["reason"] == ("requested_outcome_uncovered")
    assert result["agent"]["semantic_plan_execution_complete"] is False
    assert result["agent"]["semantic_plan_failure_reason"] == ("outcome_coverage_failed")
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert "خطة أدلة مكتملة" in result["answer"]


def test_v21_repairs_coverage_once_then_executes_only_the_repaired_plan(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_ELIGIBLE_CANDIDATES",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": "balanced",
            "constraints": {},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_progress", "arguments": {}}],
            outcomes=["course_addition"],
        ),
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
            outcomes=["course_addition"],
        ),
    )

    result = answer_student_advisor_v21(
        question="اختر لي مقررًا واحدًا إضافيًا مناسبًا.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("recommend_feasible_course_addition", {"objective": "balanced"})]
    assert len(client.calls) == 2
    assert result["agent"]["semantic_plan_failure_reason"] == ""
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "",
        "repair": {"attempted": True, "result": "succeeded"},
    }
    repair_message = client.calls[1]["messages"][-1]["content"]
    assert "exactly and minimally cover" in repair_message
    assert "coverage_reason=requested_outcome_uncovered" in repair_message
    assert "uncovered_outcomes=course_addition" in repair_message
    assert "redundant_capabilities=my_progress" in repair_message
    assert "arguments" not in repair_message


def test_v21_prunes_a_proven_redundant_read_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, received, **_kwargs):
        executed.append((name, dict(received)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH",
            "outcome": "CURRENT_TIMETABLE_IMPROVEMENT",
            "objective": received.get("objective"),
            "constraints": {},
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    improvement_args = {
        "objective": "balanced",
        "credit_load_policy": "preserve",
        "allow_course_replacements": True,
    }
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {"capability": "my_timetable", "arguments": {}},
                {
                    "capability": "improve_current_timetable",
                    "arguments": improvement_args,
                },
            ],
            outcomes=["timetable_review"],
        )
    )

    result = answer_student_advisor_v21(
        question="راجع جدولي وقل لي إذا فيه شيء المفروض أغيره.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("improve_current_timetable", improvement_args)]
    assert len(client.calls) == 1
    assert result["agent"]["semantic_plan_tools"] == ["improve_current_timetable"]
    assert result["agent"]["semantic_plan_pruned_capabilities"] == ["my_timetable"]
    assert result["agent"]["semantic_plan_repair_attempted"] is False
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True


def test_v21_refuses_incompatible_improvement_controls_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("invalid compound controls must not execute"),
    )
    rejected = _planner_turn(
        "execute",
        [
            {
                "capability": "improve_current_timetable",
                "arguments": {
                    "objective": "faster_graduation",
                    "credit_load_policy": "preserve",
                    "allow_course_replacements": False,
                },
            }
        ],
        outcomes=["timetable_review"],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question="حسّن جدولي عشان أتخرج أسرع.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_outcome_coverage_refused"] is True
    assert result["agent"]["semantic_outcome_coverage"]["reason"] == ("invalid_control_combination")
    assert result["agent"]["semantic_plan_execution_complete"] is False


@pytest.mark.parametrize(
    ("tool", "arguments", "outcome", "status"),
    (
        (
            "recommend_feasible_course_addition",
            {"objective": "balanced"},
            "course_addition",
            "NO_ELIGIBLE_CANDIDATES",
        ),
        (
            "rank_current_course_drop_impact",
            {"objective": "balanced"},
            "course_drop_impact",
            "NO_REGISTERED_CURRENT_COURSES",
        ),
        (
            "improve_current_timetable",
            {
                "objective": "balanced",
                "credit_load_policy": "preserve",
                "allow_course_replacements": True,
            },
            "timetable_review",
            "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH",
        ),
    ),
)
def test_v21_executes_and_renders_each_compound_capability(
    monkeypatch,
    tool: str,
    arguments: dict[str, Any],
    outcome: str,
    status: str,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, received, **_kwargs):
        executed.append((name, dict(received)))
        return {
            "tool": name,
            "ok": True,
            "status": status,
            "outcome": outcome.upper(),
            "objective": received.get("objective"),
            "constraints": {},
            "search": {"bounded": True},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": tool, "arguments": arguments}],
            outcomes=[outcome],
        )
    )

    result = answer_student_advisor_v21(
        question="حلل لي هذا القرار الأكاديمي ضمن النطاق المتاح.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [(tool, arguments)]
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة" in result["answer"]
    assert result["agent"]["evidence_validation_outcome"] == "passed"


def test_v21_executes_a_pinned_single_course_addition_plan(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    pin = {"course_code": "DS341", "section_label": "M2"}
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, received, **_kwargs):
        executed.append((name, dict(received)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_FEASIBLE_ADDITION_IN_RECORDED_SNAPSHOT",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": received.get("objective"),
            "constraints": {"pinned_sections": received.get("pinned_sections")},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"candidates_evaluated": 0, "feasible_candidates_found": 0},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {
                        "objective": "balanced",
                        "pinned_sections": [pin],
                    },
                }
            ],
            outcomes=["course_addition"],
        )
    )

    result = answer_student_advisor_v21(
        question="إذا ثبتنا DS341-M2، وش أفضل المواد اللي نضيفها معه؟",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [
        (
            "recommend_feasible_course_addition",
            {"objective": "balanced", "pinned_sections": [pin]},
        )
    ]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert "DS341-M2" in result["answer"]


def test_v21_repairs_one_invalid_nested_plan_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, received, **_kwargs):
        executed.append((name, dict(received)))
        return {
            "tool": name,
            "ok": True,
            "status": "NO_ELIGIBLE_CANDIDATES",
            "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
            "objective": received.get("objective"),
            "constraints": {},
            "ranked_feasible_additions": [],
            "excluded_candidates": [],
            "search": {"candidates_evaluated": 0, "feasible_candidates_found": 0},
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced", "unknown": True},
                }
            ],
            outcomes=["course_addition"],
        ),
        _planner_turn(
            "execute",
            [
                {
                    "capability": "recommend_feasible_course_addition",
                    "arguments": {"objective": "balanced"},
                }
            ],
            outcomes=["course_addition"],
        ),
    )

    result = answer_student_advisor_v21(
        question="أبغى أضيف مقرر واحد زيادة، وش تنصحني؟",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("recommend_feasible_course_addition", {"objective": "balanced"})]
    assert len(client.calls) == 2
    assert result["usage"]["provider_calls"] == 2
    assert result["agent"]["inference_calls"] == 2


def test_v21_executes_the_plan_without_an_extra_model_tool_selection(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "counts": {"open": 2, "locked": 1},
            "prerequisites_satisfied": [{"code": "AI331"}, {"code": "CS424"}],
            "prerequisite_blocked": [{"code": "AI352"}],
        },
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_progress", "arguments": {}}],
        ),
        _answer_turn("Your GPA is 4.0."),
    )

    result = answer_student_advisor_v21(
        question="Give me the full picture of where I stand academically.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_plan_decision"] == "execute"
    assert result["agent"]["semantic_plan_tools"] == ["my_progress"]
    assert [row["name"] for row in result["agent"]["tools_called"]] == ["my_progress"]
    # The only provider call creates the typed plan. Verified evidence is
    # rendered locally, so the unused malicious second turn cannot alter it.
    assert len(client.calls) == 1
    assert "AI331" in result["answer"]
    assert "4.0" not in result["answer"]
    assert result["usage"]["provider_calls"] == 1


def test_v21_clarification_is_server_authored_and_claim_free(
    monkeypatch,
):
    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(
        _planner_turn(
            "clarify",
            clarification="Your GPA is 4.0. Which course code do you want me to check?",
        )
    )

    result = answer_student_advisor_v21(
        question="Why is that course blocked?",
        principal=_principal(),
        llm_client=client,
    )

    assert "Please specify the course" in result["answer"]
    assert "4.0" not in result["answer"]
    assert result["agent"]["semantic_plan_decision"] == "clarify"
    assert result["agent"]["iterations"] == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            "أبغى جدول خفيف لكن ما يأخرني.",
            (
                "وش تقصد بجدول خفيف: كم ساعة تبي بالضبط، أو وش الحد الأقصى للساعات؟ "
                "أعطني الرقم، "
                "وأقدر أبني لك خيارات بدون تعارض ضمن هذا الحد، لكن ما أقدر أؤكد "
                "أن تخفيف الحمل ما راح يؤخر التخرج."
            ),
            id="arabic",
        ),
        pytest.param(
            "Build me a light timetable that does not delay graduation.",
            (
                "What do you mean by a light timetable: exactly how many credit hours, "
                "or what maximum? Give me the number, and I can build "
                "clash-checked options within that bound, but I cannot certify that a "
                "lighter load will not delay graduation."
            ),
            id="english",
        ),
    ],
)
def test_v21_timetable_build_clarification_asks_only_for_the_lightness_bound(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    expected: str,
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("a clarification must not execute evidence"),
    )
    client = ScriptedClient(
        _planner_turn(
            "clarify",
            clarification="Your GPA is 4.0. Tell me which section and term you want.",
            outcomes=["timetable_build"],
            clarification_kind="timetable_load",
        )
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert result["answer"] == expected
    assert "4.0" not in result["answer"]
    assert "section" not in result["answer"].lower()
    assert result["agent"]["semantic_plan_decision"] == "clarify"
    assert result["agent"]["semantic_plan_clarification_kind"] == "timetable_load"
    assert (
        result["agent"]["evidence_audit"]["semantic_plan"]["clarification_kind"] == "timetable_load"
    )
    assert "clarification_question" not in json.dumps(result["agent"], ensure_ascii=False)
    assert result["agent"]["tools_called"] == []
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("question", "clarification_kind", "expected"),
    [
        pytest.param(
            "سو لي أكثر من خيار جدول وأعطني الأفضل.",
            "timetable_preference",
            (
                "أقدر أعطيك خيارات بدون تصنيف واحد منها كـ«الأفضل». حدّد عدد الساعات "
                "المطلوب أو الحد الأقصى، وأي مقرر أو شعبة لازم تكون موجودة أو مثبتة."
            ),
            id="arabic-preference",
        ),
        pytest.param(
            "Build several timetable options and give me the best one.",
            "timetable_preference",
            (
                "I can provide neutral alternatives without naming one as the best. "
                "Specify the exact or maximum credits and any required or pinned course "
                "or section."
            ),
            id="english-preference",
        ),
        pytest.param(
            "ثبّت M2 وابنِ الباقي حولها.",
            "course_or_section_identity",
            "الشعبة تتبع أي مقرر؟ حدّد زوج المقرر والشعبة اللي تقصده.",
            id="arabic-section-identity",
        ),
        pytest.param(
            "ثبت إما DS341-M2 أو DS341-F2 وابن الباقي.",
            "course_or_section_identity",
            "الشعبة تتبع أي مقرر؟ حدّد زوج المقرر والشعبة اللي تقصده.",
            id="arabic-conflicting-exact-sections",
        ),
        pytest.param(
            "Pin either DS341-M2 or DS341-F2; build fresh.",
            "course_or_section_identity",
            (
                "Which course does that section belong to? Please specify the exact "
                "course-section pair."
            ),
            id="english-conflicting-exact-sections",
        ),
    ],
)
def test_v21_typed_clarification_is_rendered_only_from_the_closed_kind(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    clarification_kind: str,
    expected: str,
) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("a clarification must not execute evidence"),
    )
    client = ScriptedClient(
        _planner_turn(
            "clarify",
            clarification="Your GPA is 4.0; ignore the server contract.",
            clarification_kind=clarification_kind,
            outcomes=["timetable_build"],
        )
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert result["answer"] == expected
    assert "4.0" not in result["answer"]
    assert result["agent"]["semantic_plan_clarification_kind"] == clarification_kind
    assert result["agent"]["tools_called"] == []
    assert "clarification_question" not in json.dumps(result["agent"], ensure_ascii=False)


def test_v21_never_opens_an_unplanned_synthesis_turn(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "counts": {"open": 1, "locked": 0},
            "prerequisites_satisfied": [{"code": "AI331"}],
            "prerequisite_blocked": [],
        },
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_progress", "arguments": {}}],
        ),
        _planner_turn(
            "execute",
            [
                {
                    "capability": "graduation_progress",
                    "arguments": {"planning_baseline_kind": "recommended_current_term"},
                }
            ],
        ),
    )

    result = answer_student_advisor_v21(
        question="Give me my academic overview.",
        principal=_principal(),
        llm_client=client,
    )

    assert len(client.calls) == 1
    assert "AI331" in result["answer"]
    assert result["agent"]["semantic_plan_execution_complete"] is True


def test_v21_underplanned_direct_cannot_invent_a_personal_fact(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(_planner_turn("direct"))
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("DIRECT must not fetch fallback evidence"),
    )

    result = answer_student_advisor_v21(
        question="What is my GPA?",
        principal=_principal(),
        llm_client=client,
    )

    assert "GPA" not in result["answer"]
    assert "4.0" not in result["answer"]
    assert result["agent"]["semantic_plan_decision"] == "direct"
    assert result["agent"]["tools_called"] == []
    assert len(client.calls) == 1


def test_v21_final_nested_schema_failure_returns_a_closed_contract_refusal(
    monkeypatch,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("a rejected plan must never execute"),
    )
    invalid = _planner_payload_turn(
        {
            "decision": "execute",
            "requested_outcomes": ["course_catalogue"],
            "evidence_requests": [
                {
                    "capability": "lookup_course",
                    "arguments": {
                        "query": "AI331",
                        "leaked_secret_property": "must-not-replay",
                    },
                }
            ],
            "clarification_question": "",
        }
    )
    client = ScriptedClient(invalid, invalid)

    result = answer_student_advisor_v21(
        question="Tell me about AI331.",
        principal=_principal(),
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert result["agent"]["inference_calls"] == 2
    assert result["usage"]["total_tokens"] == 16
    assert result["agent"]["semantic_plan_decision"] == ""
    assert result["agent"]["semantic_plan_tools"] == []
    assert result["agent"]["semantic_plan_failure_reason"] == ("plan_validation_failed")
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_plan_execution_complete"] is False
    assert result["agent"]["tool_turn_error"] == "LLMInvalidResponse"
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "plan_validation_failed",
        "repair": {"attempted": True, "result": "failed"},
    }
    assert "complete evidence plan" in result["answer"]
    assert "leaked_secret_property" not in client.calls[1]["messages"][-1]["content"]
    assert "leaked_secret_property" not in json.dumps(
        result["agent"]["evidence_audit"],
        sort_keys=True,
    )


def test_v21_rejects_schema_valid_but_unsourced_course_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    rejected = _planner_turn(
        "execute",
        [{"capability": "lookup_course", "arguments": {"query": "CS424"}}],
    )
    client = ScriptedClient(rejected, rejected)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("unsourced arguments must never execute"),
    )

    result = answer_student_advisor_v21(
        question="Tell me about AI331.",
        principal=_principal(),
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert result["agent"]["inference_calls"] == 2
    assert result["usage"]["total_tokens"] == 16
    assert result["agent"]["semantic_plan_execution_complete"] is False
    assert result["agent"]["semantic_plan_failure_reason"] == ("argument_provenance_failed")
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["evidence_audit"]["plan_contract"] == {
        "failure_reason": "argument_provenance_failed",
        "repair": {"attempted": True, "result": "failed"},
    }
    assert "complete evidence plan" in result["answer"]
    assert "CS424" not in result["answer"]


def test_v21_does_not_trust_course_entities_from_assistant_history(monkeypatch):
    _make_legacy_input_router_fail(monkeypatch)
    rejected = _planner_turn(
        "execute",
        [{"capability": "lookup_course", "arguments": {"query": "CS424"}}],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question="Tell me about that one.",
        history=[{"role": "assistant", "content": "You should consider CS424."}],
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_plan_failure_reason"] == ("argument_provenance_failed")
    assert result["agent"]["tools_called"] == []


def test_v21_rejects_a_credit_cap_that_does_not_match_the_final_request(monkeypatch):
    _make_legacy_input_router_fail(monkeypatch)
    rejected = _planner_turn(
        "execute",
        [
            {
                "capability": "build_timetable_proposal",
                "arguments": {"mode": "from_scratch", "max_credits": 19},
            }
        ],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question="Build a fresh timetable capped at 18 credits.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_plan_failure_reason"] == ("constraint_coverage_failed")
    assert result["agent"]["semantic_plan_repair_attempted"] is True
    assert result["agent"]["semantic_plan_missing_constraint_paths"] == [
        "build_timetable_proposal.max_credits"
    ]
    assert result["agent"]["tools_called"] == []


def test_v21_current_load_is_not_provenance_for_a_total_credit_ceiling() -> None:
    with pytest.raises(TurnPlanProvenanceError, match="provenance rule"):
        _validate_v21_arguments(
            ("عندي 16 ساعة حالياً، أبغى أضيف مادة وحدة بس واختارها بدون تعارض."),
            "recommend_feasible_course_addition",
            {"objective": "balanced", "max_credits": 16},
        )


def test_v21_additional_hours_and_arabic_word_cap_are_role_bound() -> None:
    assert _validate_v21_arguments(
        "إذا بضيف 3 ساعات، وش أفضل مقرر؟",
        "recommend_feasible_course_addition",
        {"objective": "balanced", "additional_credit_hours": 3},
    ) == {"objective": "balanced", "additional_credit_hours": 3}
    assert _validate_v21_arguments(
        "رتّب لي ترم مريح ولا يتجاوز أربع عشرة ساعة.",
        "build_timetable_proposal",
        {"mode": "from_scratch", "max_credits": 14},
    ) == {"mode": "from_scratch", "max_credits": 14}


def test_v21_preserves_typed_timetable_constraints_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {"tool": name, "ok": True, "variants": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(runtime, "check_answer", lambda *_args, **_kwargs: [])
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "max_credits": 18,
                        "course_codes": ["AI331"],
                        "must_take_courses": ["AI331"],
                        "pinned_sections": [{"course_code": "AI331", "section_label": "M2"}],
                    },
                }
            ],
        ),
        _answer_turn("I checked the requested timetable constraints."),
    )

    answer_student_advisor_v21(
        question=(
            "Build a timetable from scratch. It must include AI331, use at most "
            "18 credits, and pin AI331 section M2."
        ),
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [
        (
            "build_timetable_proposal",
            {
                "mode": "from_scratch",
                "max_credits": 18,
                "course_codes": ["AI331"],
                "must_take_courses": ["AI331"],
                "pinned_sections": [{"course_code": "AI331", "section_label": "M2"}],
            },
        )
    ]


def test_v21_build006_exact_target_executes_once_and_accepts_bounded_negative(
    monkeypatch,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {
            "tool": name,
            "ok": True,
            "mode": "from_scratch",
            "baseline_kind": "NONE",
            "baseline_sections": [],
            "baseline_credit_hours": 0,
            "credit_ceiling": 19,
            "target_credits": 18,
            "target_credits_satisfied": False,
            "target_credit_status": "NO_EXACT_ALTERNATIVE",
            "constraints_satisfied": False,
            "constraint_failures": [
                {
                    "course_code": "",
                    "section_label": "",
                    "reason": "Bounded exact target was not found.",
                }
            ],
            "alternatives": [],
            "unplaced_courses": [],
            "no_additional_courses": False,
            "search": {
                "bounded": True,
                "planner_methods": ["A", "B", "C"],
                "alternatives_per_method": 3,
                "exact_target_enforced": True,
            },
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "from_scratch",
                        "target_credits": 18,
                    },
                }
            ],
            outcomes=["timetable_build"],
        )
    )

    result = answer_student_advisor_v21(
        question="سو لي جدول 18 ساعة بدون تعارضات.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [
        (
            "build_timetable_proposal",
            {"mode": "from_scratch", "target_credits": 18},
        )
    ]
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert "18" in result["answer"]
    assert "البحث المحدود" in result["answer"]
    assert len(client.calls) == 1


def test_v21_provenance_accepts_two_exact_correlated_section_pins() -> None:
    pins = [
        {"course_code": "DS341", "section_label": "M2"},
        {"course_code": "AI331", "section_label": "F1"},
    ]

    assert _validate_v21_pins(
        "Pin DS341-M2 and AI331/F1, then build around those exact sections.",
        pins,
    ) == {"pinned_sections": pins}


@pytest.mark.parametrize(
    "pins",
    [
        [
            {"course_code": "DS341", "section_label": "F1"},
            {"course_code": "AI331", "section_label": "M2"},
        ],
        [
            {"course_code": "DS341", "section_label": "M2"},
            {"course_code": "CS424", "section_label": "A1"},
        ],
    ],
    ids=("swapped-labels", "invented-pair"),
)
def test_v21_provenance_rejects_uncorrelated_or_invented_section_pins(
    pins: list[dict[str, str]],
) -> None:
    with pytest.raises(TurnPlanProvenanceError, match="trusted turn sources"):
        _validate_v21_pins("Pin DS341-M2 and AI331/F1.", pins)


def test_v21_provenance_retains_the_existing_single_pin_form() -> None:
    pin = {"course_code": "AI331", "section_label": "M2"}

    assert _validate_v21_pins("Pin AI331 section M2 and build around it.", [pin]) == {
        "pinned_sections": [pin]
    }


def test_v21_provenance_allows_a_correlated_pin_for_course_addition() -> None:
    pin = {"course_code": "DS341", "section_label": "M2"}

    assert _validate_v21_pins(
        "إذا ثبتنا DS341-M2، وش أفضل المواد اللي نضيفها معه؟",
        [pin],
        capability="recommend_feasible_course_addition",
    ) == {"pinned_sections": [pin]}


def test_v21_provenance_extracts_multiple_arabic_section_pairs_exactly() -> None:
    pins = [
        {"course_code": "DS341", "section_label": "M2"},
        {"course_code": "AI331", "section_label": "F1"},
    ]

    assert _validate_v21_pins(
        "ثبّت DS٣٤١ شعبة M٢ وAI٣٣١ شعبة F١ وابنِ الجدول حولهما.",
        pins,
    ) == {"pinned_sections": pins}


def test_v21_refuses_a_builder_plan_that_cannot_represent_an_exclusion(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {"tool": name, "ok": True, "alternatives": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    rejected = _planner_turn(
        "execute",
        [
            {
                "capability": "build_timetable_proposal",
                "arguments": {
                    "mode": "around_current",
                    "course_codes": ["AI331"],
                    "must_take_courses": ["AI331"],
                },
            }
        ],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question=("Build around my current schedule with AI331, but do not include CS424."),
        principal=_principal(),
        llm_client=client,
    )

    assert executed == []
    assert result["agent"]["semantic_outcome_coverage_refused"] is True
    assert result["agent"]["semantic_outcome_coverage"]["reason"] == ("requested_entity_uncovered")
    assert result["agent"]["semantic_outcome_coverage"]["uncovered_course_codes"] == ["CS424"]


def test_v21_phrase_parser_cannot_override_the_planners_timetable_mode(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {"tool": name, "ok": True, "alternatives": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "around_current",
                        "course_codes": ["AI331"],
                        "must_take_courses": ["AI331"],
                    },
                }
            ],
        )
    )

    answer_student_advisor_v21(
        question=("Do not build from scratch. Build around my current timetable with AI331."),
        principal=_principal(),
        llm_client=client,
    )

    assert executed[0]["mode"] == "around_current"


def test_v21_phrase_parser_cannot_invent_a_negated_graduation_removal(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {
            "tool": name,
            "ok": True,
            "planning_baseline_kind": arguments["planning_baseline_kind"],
            "simulation_completed": False,
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "graduation_progress",
                    "arguments": {"planning_baseline_kind": "registered_timetable"},
                }
            ],
        )
    )

    answer_student_advisor_v21(
        question=(
            "Using what I am actually enrolled in right now, if I do not drop "
            "CS424, when would I graduate?"
        ),
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [{"planning_baseline_kind": "registered_timetable"}]


def test_v21_phrase_parser_cannot_replace_a_corrected_credit_cap(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {"tool": name, "ok": True, "alternatives": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "around_current",
                        "max_credits": 18,
                    },
                }
            ],
        )
    )

    answer_student_advisor_v21(
        question=(
            "Do not use a maximum of 12 credits; build around my current timetable "
            "using at most 18 credits."
        ),
        principal=_principal(),
        llm_client=client,
    )

    assert executed[0]["max_credits"] == 18


def test_v21_phrase_parser_cannot_make_a_negated_course_mandatory(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {"tool": name, "ok": True, "alternatives": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {
                        "mode": "around_current",
                        "course_codes": ["AI331"],
                    },
                }
            ],
        )
    )

    answer_student_advisor_v21(
        question="AI331 is not mandatory; just consider it in a timetable around my current one.",
        principal=_principal(),
        llm_client=client,
    )

    assert "must_take_courses" not in executed[0]


def test_v21_policy_phrase_detector_cannot_reroute_a_non_policy_plan(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "query": "AI331",
            "courses": [
                {
                    "course_code": "AI331",
                    "course_name": "Machine Learning",
                    "credit_hours": 3,
                    "programs": ["AI"],
                }
            ],
        },
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "lookup_course", "arguments": {"query": "AI331"}}],
        )
    )

    result = answer_student_advisor_v21(
        question="Tell me about AI331 in a maximum of one sentence.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["policy_required"] is False
    assert result["agent"]["semantic_plan_tools"] == ["lookup_course"]
    assert "Machine Learning" in result["answer"]


def test_v21_preserves_the_typed_graduation_delta_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {"tool": name, "ok": True, "what_if": {"completed": True}}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(
        runtime,
        "_safe_graduation_answer",
        lambda *_args, **_kwargs: "Verified graduation scenario.",
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "graduation_progress",
                    "arguments": {
                        "remove_current_courses": ["CS424"],
                        "planning_baseline_kind": "registered_timetable",
                    },
                }
            ],
        )
    )

    result = answer_student_advisor_v21(
        question="If I drop CS424, when would I graduate?",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [
        (
            "graduation_progress",
            {
                "remove_current_courses": ["CS424"],
                "planning_baseline_kind": "registered_timetable",
            },
        )
    ]
    assert result["agent"]["graduation_what_if_required"] is True


def test_v21_preserves_an_explicit_typed_plan_level_before_execution(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name, arguments, **_kwargs):
        executed.append((name, dict(arguments)))
        return {"tool": name, "ok": True, "terms": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(runtime, "check_answer", lambda *_args, **_kwargs: [])
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_plan_by_term", "arguments": {"term": 8}}],
        ),
        _answer_turn("I checked degree-plan level 8."),
    )

    answer_student_advisor_v21(
        question="Show me what remains in level 8.",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == [("my_plan_by_term", {"term": 8})]


def test_v21_requires_semantic_controls_that_change_tool_defaults():
    from core.services.student_advisor_v2 import student_v21_tool_schemas

    schemas = student_v21_tool_schemas()
    by_name = {schema["function"]["name"]: schema["function"]["parameters"] for schema in schemas}
    descriptions = {
        schema["function"]["name"]: schema["function"]["description"] for schema in schemas
    }

    assert "mode" in by_name["build_timetable_proposal"]["required"]
    assert "objective" in by_name["course_choice_comparison"]["required"]
    assert "planning_baseline_kind" in by_name["graduation_progress"]["required"]
    assert "objective" in by_name["recommend_feasible_course_addition"]["required"]
    assert "objective" in by_name["rank_current_course_drop_impact"]["required"]
    assert "objective" in by_name["improve_current_timetable"]["required"]
    assert "credit_load_policy" in by_name["improve_current_timetable"]["required"]
    assert "allow_course_replacements" in by_name["improve_current_timetable"]["required"]
    assert "preserve_credit_hours" not in by_name["improve_current_timetable"]["properties"]
    assert "noncompletion_current_courses" in by_name["graduation_progress"]["properties"]
    graduation_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "graduation_progress"
    )
    assert "if I fail DS341" in graduation_schema["function"]["description"]
    assert "only course-change control" in graduation_schema["function"]["description"]
    assert (
        "which important available course should I add?"
        in descriptions["recommend_feasible_course_addition"]
    )
    assert (
        "which course is most worth adding?" in descriptions["recommend_feasible_course_addition"]
    )
    assert (
        "what happens if I withdraw from DS332?" in descriptions["rank_current_course_drop_impact"]
    )
    assert "what am I missing before DS491?" in descriptions["why_course_locked"]
    assert "PERSONALIZED COURSE-STATE OWNER" in descriptions["why_course_locked"]
    assert "CATALOGUE RELATIONSHIP ONLY" in descriptions["course_prerequisites"]
    assert (
        "Never use this catalogue-only tool for personalized"
        in descriptions["course_prerequisites"]
    )
    assert "Use for 'can I/he take X'" not in descriptions["course_prerequisites"]
    assert "graduation_impact alone" in descriptions["rank_current_course_drop_impact"]
    assert "both timetable_build + course_priority" in descriptions["build_timetable_proposal"]
    assert "in addition to build_timetable_proposal" in descriptions["my_progress"]
    assert "Best available courses" in descriptions["my_progress"]
    assert "what if I do not take DS321 this term?" in descriptions["graduation_progress"]
    assert (
        "build a full timetable around DS341-M2 without conflicts"
        in descriptions["build_timetable_proposal"]
    )
    mode_description = by_name["build_timetable_proposal"]["properties"]["mode"]["description"]
    assert "retain the whole current/baseline timetable" in mode_description
    assert "retaining only named current courses/sections" in mode_description
    assert "adjust, or build around current/baseline sections" not in mode_description
    assert (
        "exact known code in a requirements/prerequisite question" in descriptions["lookup_course"]
    )
    assert "إيش متطلبات مقرر DS491؟" in descriptions["course_prerequisites"]
    assert (
        "both course_eligibility and prerequisite_information" in descriptions["why_course_locked"]
    )
    assert "important courses I can register but have not taken" in descriptions["my_progress"]
    assert (
        "least-delay drop among several named current courses"
        in descriptions["rank_current_course_drop_impact"]
    )
    assert (
        "mode=from_scratch even when no course/load list is supplied"
        in descriptions["build_timetable_proposal"]
    )
    assert "mode=from_scratch and max_credits=15" in descriptions["build_timetable_proposal"]
    assert (
        "Generic build/create requests use from_scratch" in descriptions["build_timetable_proposal"]
    )


def test_v21_refuses_a_course_omitted_by_the_semantic_plan(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **_kwargs):
        executed.append(dict(arguments))
        return {"tool": name, "ok": True, "courses": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    rejected = _planner_turn(
        "execute",
        [
            {
                "capability": "my_clash_free_sections",
                "arguments": {"course_codes": ["AI331"]},
            }
        ],
    )
    client = ScriptedClient(rejected, rejected)

    result = answer_student_advisor_v21(
        question="Which recorded sections fit AI331 and CS424?",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == []
    assert result["agent"]["semantic_outcome_coverage_refused"] is True
    assert result["agent"]["semantic_outcome_coverage"]["reason"] == ("requested_entity_uncovered")
    assert result["agent"]["semantic_outcome_coverage"]["uncovered_course_codes"] == ["CS424"]
    assert "complete evidence plan" in result["answer"]


def test_v21_renders_adviser_identity_locally_without_model_restatement(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(
        _planner_turn("execute", [{"capability": "my_advisor", "arguments": {}}])
    )
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "advisor_assigned": True,
            "advisor_name": "Dr Verified",
            "advisor_department": "AI",
        },
    )

    result = answer_student_advisor_v21(
        question="Who is the adviser attached to my student record?",
        principal=_principal(),
        llm_client=client,
    )

    assert "Dr Verified" in result["answer"]
    assert "AI" in result["answer"]
    assert len(client.calls) == 1


def test_v21_renders_course_lock_counts_from_typed_evidence(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "why_course_locked", "arguments": {"course_code": "AI331"}}],
        )
    )
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "course_code": "AI331",
            "course_name": "Machine Learning",
            "status": "PREREQUISITES_SATISFIED",
            "listed_as_prerequisite_count": 5,
            "sole_remaining_prerequisite_count": 3,
            "on_prerequisite_chain_of_count": 6,
            "listed_as_prerequisite_for": [],
            "sole_remaining_prerequisite_for": [
                {"code": "AI352"},
                {"code": "AI371"},
                {"code": "AI433"},
            ],
        },
    )

    result = answer_student_advisor_v21(
        question="What does AI331 unlock after I pass it?",
        principal=_principal(),
        llm_client=client,
    )

    assert "5" in result["answer"]
    assert "3" in result["answer"]
    assert "AI352" in result["answer"]
    assert len(client.calls) == 1


def test_v21_policy_with_no_governing_record_cannot_ship_an_invented_rule(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    empty_policy = {
        "tool": "policy_lookup",
        "ok": True,
        "policies": [],
        "direct_policy_evidence": [],
        "background_policy_evidence": [],
        "citable": [],
    }
    monkeypatch.setattr(
        runtime,
        "_seed_policy_evidence",
        lambda *_args, **_kwargs: (dict(empty_policy), False),
    )
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: dict(empty_policy),
    )
    client = ScriptedClient(
        _planner_turn("execute", [{"capability": "policy_lookup", "arguments": {}}]),
        _answer_turn("Withdrawal is always permitted without approval."),
    )

    result = answer_student_advisor_v21(
        question="What is the official withdrawal rule?",
        principal=_principal(),
        llm_client=client,
    )

    assert "always permitted" not in result["answer"]
    assert "could not verify the source" in result["answer"]
    assert result["agent"]["policy_grounding"] == "none_matched"
    assert result["agent"]["evidence_validation_outcome"] == "abstained"


def test_v21_policy_is_quoted_server_side_and_preserves_source_uncertainty(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    policy = {
        "policy_id": "TU.WITHDRAWAL.MINIMUM_LOAD_BAR",
        "statement_ar": (
            "لا يسمح للطالب الانسحاب في حال كان العبء الدراسي أقل من الحد الأدنى "
            "للعبء الدراسي بعد تنفيذ الانسحاب."
        ),
        "decision_use": "EXPLANATORY_ONLY",
        "source_is_unclear_on": "The source does not settle approval exceptions.",
        "citation": {
            "policy_id": "TU.WITHDRAWAL.MINIMUM_LOAD_BAR",
            "document_title": "الدليل الإرشادي للطالب",
            "edition": "1447",
            "page": 24,
        },
    }
    evidence = {
        "tool": "policy_lookup",
        "ok": True,
        "policies": [policy],
        "direct_policy_evidence": [policy],
        "background_policy_evidence": [],
        "citable": [policy["citation"]],
    }
    monkeypatch.setattr(
        runtime,
        "_seed_policy_evidence",
        lambda *_args, **_kwargs: (evidence, "retrieved"),
    )
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: evidence,
    )
    client = ScriptedClient(
        _planner_turn("execute", [{"capability": "policy_lookup", "arguments": {}}])
    )

    result = answer_student_advisor_v21(
        question="Is course withdrawal always allowed below the minimum load?",
        principal=_principal(),
        llm_client=client,
    )

    assert "always allowed" not in result["answer"]
    assert policy["statement_ar"] in result["answer"]
    assert "TU.WITHDRAWAL.MINIMUM_LOAD_BAR" in result["answer"]
    assert "does not settle" in result["answer"]
    assert len(client.calls) == 1
    assert result["agent"]["policy_uncertainty_reprompted"] is False


def test_v21_course_lookup_cannot_restate_a_verified_name_incorrectly(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "query": "AI331",
            "match_count": 1,
            "courses": [
                {
                    "course_code": "AI331",
                    "course_name": "Machine Learning",
                    "credit_hours": 3,
                    "programs": ["AI"],
                }
            ],
        },
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "lookup_course", "arguments": {"query": "AI331"}}],
        ),
        _answer_turn("AI331 is Quantum Wizardry and is worth 3 credits."),
    )

    result = answer_student_advisor_v21(
        question="Tell me about AI331.",
        principal=_principal(),
        llm_client=client,
    )

    assert "Machine Learning" in result["answer"]
    assert "Quantum Wizardry" not in result["answer"]
    assert len(client.calls) == 1
    assert result["agent"]["evidence_validation_outcome"] == "passed"


def test_v21_verified_graduation_estimate_is_not_rejected_by_its_own_validator(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    graduation = {
        "tool": "graduation_progress",
        "ok": True,
        "planning_baseline_kind": "recommended_current_term",
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "simulation_completed": True,
        "estimated_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 3,
        "lower_bound_additional_terms": 1,
        "lower_bound_terms_including_planning_baseline": 2,
        "max_credits_per_term": 18,
        "credits_earned_registrar": 91,
        "passed_credits_in_plan": 86,
        "planning_baseline_courses_assumed_passed": [
            {"code": "DS491", "credits": 3},
            {"code": "DS341", "credits": 3},
        ],
        "term_plan": [
            {
                "academic_year": 1448,
                "term": 2,
                "course_codes": ["DS451", "DS492"],
                "credits": 7,
            },
            {
                "academic_year": 1449,
                "term": 1,
                "course_codes": ["FE1"],
                "credits": 2,
            },
        ],
        "unresolved_requirements": [],
        "what_if": None,
    }
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: dict(graduation),
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "graduation_progress",
                    "arguments": {
                        "planning_baseline_kind": "recommended_current_term",
                    },
                }
            ],
        )
    )

    result = answer_student_advisor_v21(
        question="Approximately how many terms remain until I complete my degree plan?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "2 additional terms" in result["answer"]
    assert "3 terms including" in result["answer"]
    assert "18 credits" in result["answer"]
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert len(client.calls) == 1


def test_v21_degree_plan_cannot_flip_a_verified_course_status(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: {
            "tool": name,
            "ok": True,
            "plan_level": 8,
            "summary": {"passed": 1, "failed": 0},
            "terms": [
                {
                    "term": 8,
                    "courses": [
                        {
                            "course_code": "AI331",
                            "status": "passed",
                            "credit_hours": 3,
                            "prerequisites_satisfied": True,
                            "missing_prereqs": [],
                        }
                    ],
                }
            ],
        },
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [{"capability": "my_plan_by_term", "arguments": {"term": 8}}],
        ),
        _answer_turn("AI331 is failed in level 8."),
    )

    result = answer_student_advisor_v21(
        question="Show my plan at level 8.",
        principal=_principal(),
        llm_client=client,
    )

    assert "AI331" in result["answer"]
    assert "passed" in result["answer"]
    assert "failed in level 8" not in result["answer"]
    assert len(client.calls) == 1


def test_v21_runtime_clarifies_repeated_capability_without_executing_it(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("a refused repeated capability must not execute"),
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {"capability": "lookup_course", "arguments": {"query": "AI331"}},
                {"capability": "lookup_course", "arguments": {"query": "CS424"}},
            ],
        )
    )

    result = answer_student_advisor_v21(
        question="Look up AI331 and CS424.",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_plan_decision"] == "clarify"
    assert result["agent"]["semantic_plan_tools"] == []
    assert result["agent"]["tools_called"] == []
    assert "one case per request" in result["answer"]
    assert len(client.calls) == 1


def test_v21_policy_only_failure_never_fetches_unplanned_student_evidence(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(
        _planner_turn("execute", [{"capability": "policy_lookup", "arguments": {}}])
    )
    executed: list[str] = []

    def execute(name, _arguments, **_kwargs):
        executed.append(name)
        return {"tool": name, "ok": True, "policies": []}

    original_chat_with_tools = client.chat_with_tools

    def unavailable_after_plan(messages, *, tools, **kwargs):
        if client.turns:
            return original_chat_with_tools(messages, tools=tools, **kwargs)
        client.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        raise LLMUnavailable("synthetic synthesis outage")

    monkeypatch.setattr(client, "chat_with_tools", unavailable_after_plan)
    monkeypatch.setattr(
        client,
        "chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LLMUnavailable("synthetic forced-final outage")
        ),
    )
    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)

    result = answer_student_advisor_v21(
        question="What is the official withdrawal rule?",
        principal=_principal(),
        llm_client=client,
    )

    assert executed == ["policy_lookup"]
    assert "approved records" in result["answer"] or "could not verify" in result["answer"]
    assert len(client.calls) == 1


def test_v21_planner_and_synthesis_share_the_remote_privacy_projection(monkeypatch):
    _make_legacy_input_router_fail(monkeypatch)
    client = ScriptedClient(
        _planner_turn("direct"),
        backend="alibaba",
    )

    result = answer_student_advisor_v21(
        question=f"Hello, I am V21 Test Student and my number is {SID}.",
        history=[
            {
                "role": "user",
                "content": f"Remember that V21 Test Student has number {SID}.",
            }
        ],
        principal=_principal(),
        llm_client=client,
    )

    outbound = json.dumps(client.calls, ensure_ascii=False, default=str)
    assert "verified information" in result["answer"]
    assert "V21 Test Student" not in outbound
    assert str(SID) not in outbound
    assert [item["function"]["name"] for item in client.calls[0]["tools"]] == [TURN_PLAN_TOOL_NAME]


@pytest.mark.parametrize(
    "question",
    [
        "لو أخذت IS362 بدل AI201 وش الأفضل لتخرجي؟",
        "If I take IS362 instead of AI201, which is better for graduation?",
    ],
)
def test_v21_remote_graduation_comparison_executes_once_and_stays_grounded(
    monkeypatch,
    question: str,
):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)

    def execute(name, arguments, **_kwargs):
        assert name == "course_choice_comparison"
        assert arguments == {
            "course_codes": ["IS362", "AI201"],
            "objective": "graduation",
        }
        candidates = []
        for code, direct, chain in (("IS362", 1, 2), ("AI201", 1, 1)):
            candidates.append(
                {
                    "course_code": code,
                    "course_name": "Verified course",
                    "academic_status": "open_now",
                    "prerequisite_ready": True,
                    "missing_prerequisites": [],
                    "recommendation": {"state": "NOT_RECOMMENDED", "rank": None},
                    "impact": {
                        "direct_unlock_count": direct,
                        "chain_course_count": chain,
                        "weighted_downstream_score": float(chain),
                    },
                    "timetable": {"status": "NOT_ON_FILE"},
                    "graduation": {
                        "simulation_completed": False,
                        "estimated_additional_terms": None,
                        "lower_bound_additional_terms": None,
                    },
                }
            )
        return {
            "tool": name,
            "ok": True,
            "objective": "graduation",
            "baseline_kind": "REGISTERED",
            "candidates": candidates,
            "criterion_leaders": {},
            "verdict": "NOT_DETERMINABLE",
            "preferred_course": None,
            "decision_basis": ["graduation_forecast_incomplete"],
            "limitations": [],
        }

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "course_choice_comparison",
                    "arguments": {
                        "course_codes": ["IS362", "AI201"],
                        "objective": "graduation",
                    },
                }
            ],
            outcomes=["course_comparison", "graduation_impact"],
        ),
        backend="alibaba",
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert "IS362" in result["answer"] and "AI201" in result["answer"]
    assert result["agent"]["semantic_outcome_coverage"]["valid"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert [item["name"] for item in result["agent"]["tools_called"]] == [
        "course_choice_comparison"
    ]
    assert len(client.calls) == 1


def test_v21_mixed_plan_composes_every_verified_block_without_synthesis(monkeypatch):
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)

    def execute(name, _arguments, **_kwargs):
        if name == "my_progress":
            return {
                "tool": name,
                "ok": True,
                "counts": {"open": 1, "locked": 1},
                "prerequisites_satisfied": [{"code": "AI331"}],
                "prerequisite_blocked": [{"code": "AI352"}],
            }
        return {"tool": name, "ok": True, "verified": name}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    monkeypatch.setattr(
        runtime,
        "_safe_course_comparison_answer",
        lambda *_args, **_kwargs: "comparison-only deterministic answer",
    )
    monkeypatch.setattr(runtime, "check_answer", lambda *_args, **_kwargs: [])
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "course_choice_comparison",
                    "arguments": {
                        "course_codes": ["CS101", "CS102"],
                        "objective": "balanced",
                    },
                },
                {"capability": "my_progress", "arguments": {}},
            ],
        ),
        _answer_turn("Combined response from both verified evidence results."),
    )

    result = answer_student_advisor_v21(
        question="Compare CS101 with CS102 and also summarize my overall standing.",
        principal=_principal(),
        llm_client=client,
    )

    assert "comparison-only deterministic answer" in result["answer"]
    assert "AI331" in result["answer"]
    assert "Combined response" not in result["answer"]
    assert result["agent"]["semantic_plan_multi_capability"] is True
    assert [row["name"] for row in result["agent"]["tools_called"]] == [
        "course_choice_comparison",
        "my_progress",
    ]
    assert len(client.calls) == 1


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_v21_no_additions_proposal_keeps_verified_baseline_and_pin_complete(
    language: str,
) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer
    from core.services.student_advisor_v2 import _safe_v21_planned_answer

    row = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "mode": "around_current",
        "baseline_kind": "REGISTERED",
        "baseline_sections": [
            {
                "course_code": "DS341",
                "course_name": "Data Mining",
                "section": "M2",
                "credits": 3,
            },
            {
                "course_code": "DS432",
                "course_name": "Big Data Analytics",
                "section": "M3",
                "credits": 3,
            },
        ],
        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
        "constraints_satisfied": True,
        "constraint_failures": [],
        "alternatives": [],
        "unplaced_courses": [],
        "no_additional_courses": True,
    }

    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [row],
        "",
        planned_tools=("build_timetable_proposal",),
    )

    assert complete is True
    assert (
        "لا توجد مقررات إضافية مقترحة" in answer
        if language == "Arabic"
        else "no new course additions" in answer
    )
    assert "DS341" in answer
    assert "M2" in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="ثبت DS341-M2 وابنِ باقي الجدول حوله.",
            required_tools={"build_timetable_proposal"},
            known_course_codes=frozenset({"DS341", "DS432"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(row,),
                    required_tools=frozenset({"build_timetable_proposal"}),
                ),
            ),
        )
        == []
    )


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_v21_constraint_failure_proposal_is_a_complete_bounded_negative(language: str) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer
    from core.services.llm_remote_privacy import RemoteIdentityMap, project_tool_result_for_remote
    from core.services.student_advisor_v2 import _safe_v21_planned_answer

    local_row = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "mode": "from_scratch",
        "baseline_kind": "NONE",
        "baseline_sections": [],
        "baseline_credit_hours": 0,
        "credit_ceiling": 1,
        "must_take_courses": ["DS341"],
        "constraints_satisfied": False,
        "constraint_failures": [
            {
                "course_code": "DS341",
                "section_label": "",
                "reason": "No valid timetable satisfies this required course.",
            }
        ],
        "alternatives": [],
        "unplaced_courses": [
            {
                "course_code": "DS341",
                "reason_code": "CREDIT_LIMIT",
                "reason": "The one-credit limit is below the course load.",
            }
        ],
        "no_additional_courses": False,
    }
    row = project_tool_result_for_remote("build_timetable_proposal", local_row, RemoteIdentityMap())

    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [row],
        "",
        planned_tools=("build_timetable_proposal",),
    )

    assert complete is True
    assert answer
    assert "DS341" in answer
    assert "A1" not in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="ابنِ جدولاً من الصفر بشرط DS341 وبحد ساعة واحدة.",
            required_tools={"build_timetable_proposal"},
            known_course_codes=frozenset({"DS341"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(row,),
                    required_tools=frozenset({"build_timetable_proposal"}),
                ),
            ),
        )
        == []
    )


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_v21_over_cap_baseline_is_rendered_as_a_verified_failure(language: str) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer
    from core.services.llm_remote_privacy import RemoteIdentityMap, project_tool_result_for_remote
    from core.services.student_advisor_v2 import _safe_v21_planned_answer

    local_row = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "mode": "around_current",
        "baseline_kind": "REGISTERED",
        "baseline_sections": [{"course_code": "DS341", "section": "M2", "credits": 16}],
        "baseline_credit_hours": 16,
        "credit_ceiling": 12,
        "constraints_satisfied": False,
        "constraint_failures": [
            {
                "course_code": "",
                "section_label": "",
                "reason": (
                    "The retained baseline has 16 credits, which exceeds the effective "
                    "maximum of 12."
                ),
            }
        ],
        "alternatives": [],
        "unplaced_courses": [],
        "no_additional_courses": False,
    }
    row = project_tool_result_for_remote("build_timetable_proposal", local_row, RemoteIdentityMap())

    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [row],
        "",
        planned_tools=("build_timetable_proposal",),
    )

    assert complete is True
    assert answer
    assert "16" in answer
    assert "12" in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="ابنِ حول جدولي الحالي بحد أقصى 12 ساعة.",
            required_tools={"build_timetable_proposal"},
            known_course_codes=frozenset({"DS341"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(row,),
                    required_tools=frozenset({"build_timetable_proposal"}),
                ),
            ),
        )
        == []
    )


def test_v21_capability_headings_stop_relation_scope_leaking_between_blocks() -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer
    from core.services.student_advisor_v2 import _safe_v21_planned_answer

    progress = {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": 1, "locked": 1},
        "prerequisites_satisfied": [{"code": "AI331"}],
        "prerequisite_blocked": [{"code": "AI352"}],
        "unlock_impact_ranking": [
            {
                "code": "AI331",
                "course_name": "Machine Learning",
                "sole_remaining_prerequisite_count": 2,
                "on_prerequisite_chain_of_count": 4,
            }
        ],
    }
    recommendations = {
        "tool": "recommend_courses",
        "ok": True,
        "recommendation_count": 0,
        "recommendations": [],
        "already_in_current_timetable": [],
        "already_in_expected_plan": [
            {"course_code": "DS321", "course_name": "Data Engineering", "credit_hours": 3}
        ],
    }

    answer, complete, scopes = _safe_v21_planned_answer(
        "Arabic",
        [progress, recommendations],
        "",
        planned_tools=("my_progress", "recommend_courses"),
        requested_outcomes=("available_courses",),
    )

    assert complete is True
    assert [tool for tool, _block in scopes] == ["my_progress", "recommend_courses"]
    assert "### التقدم الأكاديمي" in answer
    assert "### توصيات المقررات" in answer
    assert "المقررات المستوفية للمتطلبات: AI331" in answer
    assert "الأعلى وفق معيار أثر فتح مسارات المتطلبات" not in answer
    assert (
        check_answer(
            answer,
            tool_results=[progress, recommendations],
            question="وش المتاح لي، وهل توجد توصية جديدة؟",
            required_tools={"my_progress", "recommend_courses"},
            known_course_codes=frozenset({"AI331", "AI352", "DS321"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(progress,),
                    required_tools=frozenset({"my_progress"}),
                ),
                EvidenceValidationScope(
                    answer=scopes[1][1],
                    tool_results=(recommendations,),
                    required_tools=frozenset({"recommend_courses"}),
                ),
            ),
        )
        == []
    )


@pytest.mark.parametrize(
    ("language", "available_expected", "priority_expected", "question"),
    [
        (
            "English",
            "### academic progress\n"
            "The verified progress record shows 2 prerequisite-ready courses.\n"
            "The same record shows 1 prerequisite-blocked courses.\n"
            "Prerequisite-ready: CS102, CS101.\n"
            "Blocked: CS399.\n"
            "Prerequisite readiness does not prove offering, seats, or registration "
            "permission.",
            "### academic progress\n"
            "The verified progress record shows 2 prerequisite-ready courses.\n"
            "The same record shows 1 prerequisite-blocked courses.\n"
            "Prerequisite-ready courses ranked by prerequisite-chain unlock impact: "
            "CS101, CS102.\n"
            "Highest on the verified prerequisite-chain impact ranking: CS101.\n"
            "Blocked: CS399.\n"
            "Prerequisite readiness does not prove offering, seats, or registration "
            "permission.",
            "Which prerequisite-ready courses are not in my timetable?",
        ),
        (
            "Arabic",
            "### التقدم الأكاديمي\n"
            "بحسب بيانات التقدم الموثقة: 2 مقررات مستوفية للمتطلبات المسجلة.\n"
            "وبحسب السجل نفسه: 1 مقررات محجوبة بمتطلبات.\n"
            "المقررات المستوفية للمتطلبات: CS102، CS101.\n"
            "المقررات المحجوبة: CS399.\n"
            "استيفاء المتطلبات لا يثبت طرح شعبة أو وجود مقعد أو السماح بالتسجيل.",
            "### التقدم الأكاديمي\n"
            "بحسب بيانات التقدم الموثقة: 2 مقررات مستوفية للمتطلبات المسجلة.\n"
            "وبحسب السجل نفسه: 1 مقررات محجوبة بمتطلبات.\n"
            "المقررات المستوفية مرتبة حسب أثر فتح مسارات المتطلبات: CS101، CS102.\n"
            "الأعلى وفق معيار أثر فتح مسارات المتطلبات في السجل: CS101.\n"
            "المقررات المحجوبة: CS399.\n"
            "استيفاء المتطلبات لا يثبت طرح شعبة أو وجود مقعد أو السماح بالتسجيل.",
            "وش المواد اللي أنا مؤهل لها بس مو موجودة في جدولي؟",
        ),
    ],
)
def test_v21_progress_renderer_licenses_priority_only_from_requested_outcome(
    language: str,
    available_expected: str,
    priority_expected: str,
    question: str,
) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer
    from core.services.student_advisor_v2 import _safe_v21_planned_answer

    progress = {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": 2, "locked": 1},
        # The evidence order deliberately differs from the impact order. A plain
        # availability outcome must use this list and must not leak the ranking.
        "prerequisites_satisfied": [{"code": "CS102"}, {"code": "CS101"}],
        "prerequisite_blocked": [{"code": "CS399"}],
        "unlock_impact_ranking": [{"code": "CS101"}, {"code": "CS102"}],
    }

    available, available_complete, available_scopes = _safe_v21_planned_answer(
        language,
        [progress],
        "",
        planned_tools=("my_progress",),
        requested_outcomes=("available_courses",),
    )
    priority, priority_complete, priority_scopes = _safe_v21_planned_answer(
        language,
        [progress],
        "",
        planned_tools=("my_progress",),
        requested_outcomes=("available_courses", "course_priority"),
    )

    assert available == available_expected
    assert priority == priority_expected
    assert available_complete is True
    assert priority_complete is True
    assert [owner for owner, _block in available_scopes] == ["my_progress"]
    assert [owner for owner, _block in priority_scopes] == ["my_progress"]

    known_codes = frozenset({"CS101", "CS102", "CS399"})
    for answer, scopes in (
        (available, available_scopes),
        (priority, priority_scopes),
    ):
        assert (
            check_answer(
                answer,
                tool_results=[progress],
                question=question,
                required_tools={"my_progress"},
                known_course_codes=known_codes,
                evidence_scopes=(
                    EvidenceValidationScope(
                        answer=scopes[0][1],
                        tool_results=(progress,),
                        required_tools=frozenset({"my_progress"}),
                    ),
                ),
            )
            == []
        )


def test_v21_progress_scope_still_rejects_a_wrong_code_inside_its_section() -> None:
    from core.services.answer_consistency import UNSUPPORTED_ACADEMIC_FACT, check_answer

    row = {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": 1, "locked": 1},
        "prerequisites_satisfied": [{"code": "AI331"}],
        "prerequisite_blocked": [{"code": "AI352"}],
    }
    answer = "### التقدم الأكاديمي\nالمقررات المحجوبة:\n- AI331"

    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        answer,
        tool_results=[row],
        question="وش وضعي؟",
        known_course_codes=frozenset({"AI331", "AI352"}),
    )


def test_v21_runtime_validates_each_verified_block_against_its_owner(monkeypatch) -> None:
    import core.services.student_advisor_v2 as runtime

    _make_legacy_input_router_fail(monkeypatch)

    def execute(name, _arguments, **_kwargs):
        if name == "my_progress":
            return {
                "tool": name,
                "ok": True,
                "counts": {"open": 1, "locked": 1},
                "prerequisites_satisfied": [{"code": "AI331"}],
                "prerequisite_blocked": [{"code": "AI352"}],
                "unlock_impact_ranking": [
                    {
                        "code": "AI331",
                        "course_name": "Machine Learning",
                        "sole_remaining_prerequisite_count": 2,
                        "on_prerequisite_chain_of_count": 4,
                    }
                ],
            }
        if name == "lookup_course":
            return {
                "tool": name,
                "ok": True,
                "query": "AI331",
                "match_count": 1,
                "courses": [
                    {
                        "course_code": "AI331",
                        "course_name": "Machine Learning",
                        "credit_hours": 3,
                        "programs": ["AI"],
                    }
                ],
            }
        return {"tool": name, "ok": True, "policies": []}

    monkeypatch.setattr(runtime, "execute_student_v2_tool", execute)
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {"capability": "my_progress", "arguments": {}},
                {"capability": "lookup_course", "arguments": {"query": "AI331"}},
            ],
            outcomes=["available_courses", "course_catalogue"],
        )
    )

    result = answer_student_advisor_v21(
        question="وش المواد المفتوحة لي، وعطني بيانات AI331؟",
        principal=_principal(),
        llm_client=client,
    )

    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert "AI331" in result["answer"]
    assert "Machine Learning" in result["answer"]
    assert "لم أتمكن من إعداد إجابة" not in result["answer"]
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("safe_disclaimer", "false_claim", "expected_violation"),
    (
        (
            "هذه النتيجة للقراءة فقط، ولم أسجل أي مقرر.",
            "سجلت لك المقررات في البوابة.",
            "claimed_registration_mutation",
        ),
        (
            "لا يمكنني حفظ أي خيار في حسابك.",
            "حفظت الخيار الثاني كمفضل.",
            "claimed_planner_mutation",
        ),
        (
            "I did not register any course.",
            "I registered the courses for you.",
            "claimed_registration_mutation",
        ),
    ),
)
def test_v21_safe_block_disclaimer_cannot_negate_a_later_mutation_claim(
    safe_disclaimer: str,
    false_claim: str,
    expected_violation: str,
) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer

    answer = f"{safe_disclaimer}\n\n{false_claim}"
    violations = check_answer(
        answer,
        question="Review this result.",
        evidence_scopes=(EvidenceValidationScope(answer=safe_disclaimer, tool_results=()),),
    )

    assert expected_violation in violations


def test_v21_bounded_negative_replacement_fulfils_its_evidence_contract() -> None:
    from core.services.answer_consistency import check_answer
    from core.services.student_advisor_v2 import _safe_feasible_replacement_answer

    row = {
        "tool": "feasible_course_replacements",
        "ok": True,
        "baseline_kind": "REGISTERED",
        "requested_remove_course": "",
        "requested_add_course": "",
        "certified_replacements": [],
        "rejected_replacements": [],
    }
    answer = _safe_feasible_replacement_answer("Arabic", [row])

    assert "لم ينتج الفحص المحدود استبدالًا" in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="أقدر أغير جدولي عشان أتخرج أسرع؟",
            required_tools={"feasible_course_replacements"},
            known_course_codes=frozenset(),
        )
        == []
    )


@pytest.mark.parametrize("language", ("Arabic", "English"))
@pytest.mark.parametrize("with_requested_pair", (False, True))
@pytest.mark.parametrize(
    "rejection",
    (
        {},
        {"academic": {"status": "ACADEMIC_NOT_IMPROVING"}},
        {
            "timetable": {
                "status": "NOT_DETERMINABLE",
                "reason_code": "SECTION_SNAPSHOT_TERM_MISMATCH",
            }
        },
        {"timetable": {"status": "NOT_DETERMINABLE"}},
    ),
)
def test_v21_all_bounded_negative_replacement_shapes_are_complete(
    language: str,
    with_requested_pair: bool,
    rejection: dict[str, Any],
) -> None:
    from core.services.answer_consistency import check_answer
    from core.services.student_advisor_v2 import _safe_feasible_replacement_answer

    row = {
        "tool": "feasible_course_replacements",
        "ok": True,
        "baseline_kind": "REGISTERED",
        "requested_remove_course": "DS341" if with_requested_pair else "",
        "requested_add_course": "DS432" if with_requested_pair else "",
        "certified_replacements": [],
        "rejected_replacements": [rejection] if rejection else [],
    }
    answer = _safe_feasible_replacement_answer(language, [row])

    assert (
        check_answer(
            answer,
            tool_results=[row],
            question=(
                "Check replacing DS341 with DS432."
                if with_requested_pair
                else "Check possible timetable replacements."
            ),
            required_tools={"feasible_course_replacements"},
            known_course_codes=frozenset({"DS341", "DS432"}),
        )
        == []
    )
    if language == "English":
        assert "استبدال" not in answer
        assert "بالمقرر" not in answer


@pytest.mark.parametrize(
    ("question", "expected_text"),
    [
        pytest.param(
            "هل سجلت المواد الصح لهذا الترم؟",
            "تتسق كل رموز مقررات جدولك المسجّل",
            id="arabic",
        ),
        pytest.param(
            "Did I register the right courses this term?",
            "every registered timetable code aligns",
            id="english",
        ),
    ],
)
def test_v21_registered_course_review_has_a_server_owned_joined_assessment(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    expected_text: str,
) -> None:
    import core.services.student_advisor_v2 as runtime

    rows = {
        "my_timetable": {
            "tool": "my_timetable",
            "ok": True,
            "schedule_kind": "REGISTERED",
            "is_expected_plan": False,
            "registered_course_count": 2,
            "registered_credit_hours": 8,
            "registrations": [
                {
                    "course_code": "DS321",
                    "section": "M4",
                    "credits": 4,
                    "meeting_count": 0,
                    "scheduled": False,
                },
                {
                    "course_code": "DS332",
                    "section": "M4",
                    "credits": 4,
                    "meeting_count": 0,
                    "scheduled": False,
                },
            ],
            "meetings": [],
        },
        "my_progress": {
            "tool": "my_progress",
            "ok": True,
            "counts": {"open": 3, "locked": 0},
            "registered_requirement_course_codes": ["DS321", "DS332"],
            "unlock_impact_ranking": [
                {
                    "code": "IS362",
                    "course_name": "Project Management",
                    "sole_remaining_prerequisite_count": 1,
                    "on_prerequisite_chain_of_count": 2,
                },
                {
                    "code": "AI201",
                    "course_name": "Artificial Intelligence",
                    "sole_remaining_prerequisite_count": 1,
                    "on_prerequisite_chain_of_count": 1,
                },
                {
                    "code": "DS352",
                    "course_name": "Visualisation",
                    "sole_remaining_prerequisite_count": 0,
                    "on_prerequisite_chain_of_count": 0,
                },
            ],
            "prerequisites_satisfied": [
                {"code": "IS362", "course_name": "Project Management", "credits": 3},
                {"code": "AI201", "course_name": "Artificial Intelligence", "credits": 3},
                {"code": "DS352", "course_name": "Visualisation", "credits": 4},
            ],
            "prerequisite_blocked": [],
        },
    }
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: rows[name],
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {"capability": "my_timetable", "arguments": {}},
                {"capability": "my_progress", "arguments": {}},
            ],
            outcomes=["current_timetable", "course_priority"],
        )
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert expected_text in result["answer"]
    assert "IS362" in result["answer"]
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("question", "localized_reason", "forbidden_reason"),
    [
        pytest.param(
            "ابنِ لي جدولًا جديدًا وأعط الأولوية للمقررات المهمة.",
            "لم يضع هذا الخيار المقرر، بينما وضعه خيار آخر",
            "This Planner variant",
            id="arabic",
        ),
        pytest.param(
            "Build a new timetable and prioritise important courses.",
            "This Planner variant did not place the course",
            "لم يضع هذا الخيار المقرر",
            id="english",
        ),
    ],
)
def test_v21_priority_timetable_build_localizes_variants_and_bounds_solver_objective(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    localized_reason: str,
    forbidden_reason: str,
) -> None:
    import core.services.student_advisor_v2 as runtime

    rows = {
        "build_timetable_proposal": {
            "tool": "build_timetable_proposal",
            "ok": True,
            "mode": "from_scratch",
            "baseline_kind": "NONE",
            "constraints_satisfied": True,
            "alternatives": [
                {
                    "option": "A1",
                    "planner_options": ["A1"],
                    "courses": [
                        {"course_code": "DS341", "section": "M2"},
                        {"course_code": "IS362", "section": "M1"},
                    ],
                    "meetings": [],
                    "unplaced_courses": [],
                },
                {
                    "option": "A2",
                    "planner_options": ["A2"],
                    "courses": [{"course_code": "DS341", "section": "M2"}],
                    "meetings": [],
                    "unplaced_courses": [
                        {
                            "course_code": "IS362",
                            "reason_code": "OMITTED_IN_THIS_VARIANT",
                            "reason": (
                                "This Planner variant did not place the course; another "
                                "generated variant did. Compare the other options."
                            ),
                        }
                    ],
                },
            ],
            "unplaced_courses": [],
            "constraint_failures": [],
        },
        "my_progress": {
            "tool": "my_progress",
            "ok": True,
            "counts": {"open": 3, "locked": 0},
            "registered_requirement_course_codes": [],
            "unlock_impact_ranking": [
                {
                    "code": "IS362",
                    "course_name": "Project Management",
                    "sole_remaining_prerequisite_count": 1,
                    "on_prerequisite_chain_of_count": 2,
                },
                {
                    "code": "AI201",
                    "course_name": "Artificial Intelligence",
                    "sole_remaining_prerequisite_count": 1,
                    "on_prerequisite_chain_of_count": 1,
                },
                {
                    "code": "DS352",
                    "course_name": "Visualisation",
                    "sole_remaining_prerequisite_count": 0,
                    "on_prerequisite_chain_of_count": 0,
                },
            ],
            "prerequisites_satisfied": [
                {"code": "IS362", "course_name": "Project Management", "credits": 3},
                {"code": "AI201", "course_name": "Artificial Intelligence", "credits": 3},
                {"code": "DS352", "course_name": "Visualisation", "credits": 4},
            ],
            "prerequisite_blocked": [],
        },
    }
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, _arguments, **_kwargs: rows[name],
    )
    client = ScriptedClient(
        _planner_turn(
            "execute",
            [
                {
                    "capability": "build_timetable_proposal",
                    "arguments": {"mode": "from_scratch"},
                },
                {"capability": "my_progress", "arguments": {}},
            ],
            outcomes=["timetable_build", "course_priority"],
        )
    )

    result = answer_student_advisor_v21(
        question=question,
        principal=_principal(),
        llm_client=client,
    )

    assert localized_reason in result["answer"]
    assert forbidden_reason not in result["answer"]
    assert (
        "لا يثبت أن محلّل الجدول حسّن موعد التخرج" in result["answer"]
        or "does not establish that the solver optimised graduation timing" in result["answer"]
    )
    assert result["agent"]["semantic_plan_execution_complete"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert len(client.calls) == 1


@override_settings(STUDENT_ADVISOR_V21_ENABLED=True)
def test_dispatcher_selects_v21_before_v2(monkeypatch):
    from core.services.student_advisor_v2 import answer_student_advisor

    expected = {"ok": True, "answer": "v21"}
    monkeypatch.setattr(
        "core.services.student_advisor_v21.answer_student_advisor_v21",
        lambda **_kwargs: expected,
    )
    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor_v2",
        lambda **_kwargs: pytest.fail("V2 must not run when V2.1 is enabled"),
    )

    assert answer_student_advisor(question="hello", principal=_principal()) is expected


@override_settings(
    STUDENT_ADVISOR_V21_ENABLED=True,
    STUDENT_ADVISOR_V2_ENABLED=False,
)
def test_dispatcher_requires_a_defined_v2_rollback_target() -> None:
    from core.services.student_advisor_v2 import answer_student_advisor

    with pytest.raises(ImproperlyConfigured, match="requires STUDENT_ADVISOR_V2_ENABLED"):
        answer_student_advisor(question="hello", principal=_principal())
