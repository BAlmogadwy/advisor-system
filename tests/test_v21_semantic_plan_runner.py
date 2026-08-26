from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from evals.advisor.run_v21_semantic_plan import (
    LiveLimits,
    SemanticPolicyReplayError,
    _contract_with_semantic_policy_context,
    _plan_mapping,
    _prepare_bounded_live_client,
    _resolve_bounded_live_model,
    _verified_http_attempt_deltas,
    _write_or_print,
    build_report,
    build_v2_baseline_report,
    collect_live_candidate,
    collect_v2_baseline,
    derive_v2_baseline_plan,
    estimated_call_token_ceiling,
    main,
    planner_messages,
    v2_baseline_inputs,
)
from evals.advisor.v21_semantic_plan_eval import load_contract, score_batch


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return load_contract()


@pytest.fixture(scope="module")
def schemas() -> list[dict[str, Any]]:
    from core.services.student_advisor_v2 import student_v21_tool_schemas

    return student_v21_tool_schemas()


@pytest.fixture(scope="module")
def v2_schemas() -> list[dict[str, Any]]:
    from core.services.student_advisor_v2 import student_v2_tool_schemas

    return student_v2_tool_schemas()


def _perfect_plan(case: dict[str, Any]) -> dict[str, Any]:
    names = list(case["required_tools"])
    for group in case["required_any"]:
        if not set(group) & set(names):
            names.append(group[0])
    return {
        "decision": case["expected_mode"],
        "clarification_kind": case.get("expected_clarification_kind", "none"),
        "requested_outcomes": list(case["expected_outcomes"]),
        "evidence_requests": [
            {
                "capability": name,
                "arguments": copy.deepcopy(case["required_arguments"].get(name, {})),
            }
            for name in names
        ],
        "clarification_question": (
            "Which course and section do you mean?" if case["expected_mode"] == "clarify" else ""
        ),
    }


def _perfect_rows(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows": [
            {
                "case_id": case["id"],
                "plan": _perfect_plan(case),
                "model": "test-model",
                "model_revision": "",
            }
            for case in contract["cases"]
        ]
    }


def _typed_planning_result(
    plan: dict[str, Any],
    *,
    total_tokens: int = 1,
) -> SimpleNamespace:
    from core.services.student_advisor_v21_plan import (
        ClarificationKind,
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )

    turn = SimpleNamespace(
        usage={
            "prompt_tokens": total_tokens,
            "completion_tokens": 0,
            "total_tokens": total_tokens,
        },
        model="same-model",
        model_revision="revision-1",
    )
    return SimpleNamespace(
        plan=StudentTurnPlan(
            decision=TurnPlanDecision(plan["decision"]),
            evidence_requests=tuple(
                PlannedCapabilityCall(
                    capability=request["capability"],
                    arguments=copy.deepcopy(request["arguments"]),
                )
                for request in plan.get("evidence_requests", ())
            ),
            clarification_kind=ClarificationKind(plan.get("clarification_kind", "none")),
            clarification_question=str(plan.get("clarification_question") or ""),
            requested_outcomes=tuple(
                StudentRequestOutcome(outcome) for outcome in plan.get("requested_outcomes", ())
            ),
        ),
        provider_turn=turn,
        provider_turns=(turn,),
    )


def _weaker_baseline(contract: dict[str, Any]) -> dict[str, Any]:
    baseline = copy.deepcopy(_perfect_rows(contract))
    for row in baseline["rows"][:6]:
        row["plan"] = {
            "decision": "direct",
            "clarification_kind": "none",
            "requested_outcomes": ["general_conversation"],
            "evidence_requests": [],
            "clarification_question": "",
        }
    return baseline


def _with_model(results: dict[str, Any], *, model: str, revision: str = "") -> dict[str, Any]:
    copied = copy.deepcopy(results)
    for row in copied["rows"]:
        row["model"] = model
        row["model_revision"] = revision
    return copied


def test_offline_report_reparses_all_cases_through_real_typed_planner(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    report = build_report(
        _perfect_rows(contract),
        _weaker_baseline(contract),
        contract=contract,
        advertised_tools=schemas,
    )

    assert report["typed_plan_errors"] == []
    assert report["candidate_report"]["all_passed"] is True
    assert report["comparison_gate"]["passed"] is True
    assert report["comparison_gate"]["absolute_lift"] >= 0.10
    assert len(report["rows"]) == 36


def test_invalid_capability_arguments_fail_typed_gate_before_scoring(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    candidate = _perfect_rows(contract)
    candidate["rows"][0]["plan"]["evidence_requests"][0]["arguments"]["student_id"] = 42

    report = build_report(
        candidate,
        _weaker_baseline(contract),
        contract=contract,
        advertised_tools=schemas,
    )

    assert report["typed_plan_errors"][0]["case_id"] == "V21-SP-001"
    assert report["comparison_gate"]["checks"]["typed_candidate_valid"] is False
    assert report["comparison_gate"]["passed"] is False


def test_semantic_policy_pin_context_matches_the_production_reducer(
    contract: dict[str, Any],
) -> None:
    active = _contract_with_semantic_policy_context(contract)
    annotated = [case for case in active["cases"] if "policy_explicit_pins" in case]

    assert annotated
    assert all(
        case["_semantic_policy_explicit_pins"] == case["policy_explicit_pins"] for case in annotated
    )


def test_semantic_policy_pin_annotation_drift_and_spoofed_marker_fail_closed(
    contract: dict[str, Any],
) -> None:
    poisoned = copy.deepcopy(contract)
    sp036 = next(case for case in poisoned["cases"] if case["id"] == "V21-SP-036")
    sp036["policy_explicit_pins"] = [{"course_code": "DS341", "section_label": "F2"}]
    poisoned["_semantic_policy_context_bound"] = True

    with pytest.raises(ValueError, match="production reducer"):
        _contract_with_semantic_policy_context(poisoned)


def test_pinned_policy_miss_fails_typed_replay_without_relabelling_coverage(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    candidate = _perfect_rows(contract)
    row = next(item for item in candidate["rows"] if item["case_id"] == "V21-SP-036")
    row["plan"]["evidence_requests"][0]["arguments"]["objective"] = "timetable_fit"

    report = build_report(
        candidate,
        _weaker_baseline(contract),
        contract=contract,
        advertised_tools=schemas,
    )

    error = next(item for item in report["typed_plan_errors"] if item["case_id"] == "V21-SP-036")
    assert error["error_category"] == SemanticPolicyReplayError.__name__
    assert report["comparison_gate"]["passed"] is False


def test_report_preserves_only_closed_repair_attribution(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    candidate = _perfect_rows(contract)
    row = next(item for item in candidate["rows"] if item["case_id"] == "V21-SP-034")
    row["repair"] = {
        "attempted": True,
        "reason": "semantic_policy_failed",
        "policy_ids": ["single_course_choice_balanced"],
    }

    report = build_report(
        candidate,
        _weaker_baseline(contract),
        contract=contract,
        advertised_tools=schemas,
    )

    serialized = next(item for item in report["rows"] if item["case_id"] == "V21-SP-034")
    assert serialized["repair"] == row["repair"]

    poisoned = copy.deepcopy(candidate)
    poisoned_row = next(item for item in poisoned["rows"] if item["case_id"] == "V21-SP-034")
    poisoned_row["repair"]["rejected_question"] = "PRIVATE DS341-M2"
    with pytest.raises(ValueError, match="invalid repair object") as exc_info:
        build_report(
            poisoned,
            _weaker_baseline(contract),
            contract=contract,
            advertised_tools=schemas,
        )
    assert "PRIVATE" not in str(exc_info.value)
    assert "DS341" not in str(exc_info.value)


@pytest.mark.parametrize(
    "limits",
    [
        LiveLimits(max_provider_calls=0, max_total_tokens=1000),
        LiveLimits(max_provider_calls=73, max_total_tokens=1000),
        LiveLimits(max_provider_calls=1, max_total_tokens=0),
        LiveLimits(max_provider_calls=1, max_total_tokens=1000, max_plan_tokens=2001),
        LiveLimits(max_provider_calls=1, max_total_tokens=1000, timeout_seconds=61),
    ],
)
def test_live_limits_are_explicit_and_bounded(
    limits: LiveLimits,
    contract: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        limits.validate(case_count=len(contract["cases"]))


def test_conservative_budget_stops_before_any_provider_call(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    calls = 0

    def forbidden_plan(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("the provider seam must not be reached")

    rows, metadata = collect_live_candidate(
        contract["cases"],
        client=object(),
        advertised_tools=schemas,
        plan_student_turn=forbidden_plan,
        limits=LiveLimits(max_provider_calls=1, max_total_tokens=1),
        model="unused",
        year=1448,
        term=1,
    )

    assert rows == []
    assert calls == 0
    assert metadata["stopped_for"].startswith("conservative token budget")


def test_token_ceiling_accounts_for_real_meta_schema(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    messages = planner_messages(contract["cases"][0], year=1448, term=1)
    ceiling = estimated_call_token_ceiling(
        messages,
        schemas,
        max_plan_tokens=900,
    )
    assert ceiling > len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 900


def test_live_policy_repair_uses_exact_second_ceiling_and_closed_row_metadata(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    from core.services.student_advisor_v21_plan import build_plan_repair_message

    case = next(item for item in contract["cases"] if item["id"] == "V21-SP-034")
    wrong = {
        "decision": "execute",
        "clarification_kind": "none",
        "requested_outcomes": ["course_priority"],
        "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
        "clarification_question": "",
    }
    scripted = [_typed_planning_result(wrong), _typed_planning_result(_perfect_plan(case))]
    calls: list[dict[str, Any]] = []

    def fake_plan(*_args, **kwargs):
        calls.append(dict(kwargs))
        return scripted.pop(0)

    rows, metadata = collect_live_candidate(
        [case],
        client=object(),
        advertised_tools=schemas,
        plan_student_turn=fake_plan,
        limits=LiveLimits(max_provider_calls=2, max_total_tokens=100_000_000),
        model="same-model",
        year=1448,
        term=1,
    )

    policy_id = "single_course_choice_balanced"
    repair = build_plan_repair_message(
        "semantic_policy_failed",
        {"policy_ids": (policy_id,)},
        advertised_tools=schemas,
    )
    messages = planner_messages(case, year=1448, term=1)
    expected_ceiling = estimated_call_token_ceiling(
        messages,
        schemas,
        max_plan_tokens=900,
    ) + estimated_call_token_ceiling(
        [*messages, repair],
        schemas,
        max_plan_tokens=900,
    )

    assert len(calls) == 2
    assert all(call["max_attempts"] == 1 for call in calls)
    assert calls[1]["repair_reason"] == "semantic_policy_failed"
    assert calls[1]["repair_details"] == {"policy_ids": (policy_id,)}
    assert metadata["usage"]["provider_calls"] == 2
    assert metadata["usage"]["total_tokens"] == 2
    assert metadata["usage"]["committed_token_ceiling"] == expected_ceiling
    assert metadata["repairs"] == {
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "reasons": {"semantic_policy_failed": 1},
    }
    assert rows[0]["repair"] == {
        "attempted": True,
        "reason": "semantic_policy_failed",
        "policy_ids": [policy_id],
    }


def test_live_schema_repair_and_schema_then_policy_failure_never_make_a_third_call(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    from core.services.student_advisor_v21_plan import TurnPlanValidationError

    case = next(item for item in contract["cases"] if item["id"] == "V21-SP-034")
    provider_turn = SimpleNamespace(
        usage={"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2},
        model="same-model",
        model_revision="revision-1",
    )
    wrong_policy = {
        "decision": "execute",
        "clarification_kind": "none",
        "requested_outcomes": ["course_priority"],
        "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
        "clarification_question": "",
    }

    for second, expected_error in (
        (_typed_planning_result(_perfect_plan(case), total_tokens=3), ""),
        (_typed_planning_result(wrong_policy, total_tokens=3), "SemanticPolicyReplayError"),
    ):
        calls: list[dict[str, Any]] = []

        def fake_plan(*_args, _calls=calls, _second=second, **kwargs):
            _calls.append(dict(kwargs))
            if len(_calls) == 1:
                raise TurnPlanValidationError(
                    "private rejected arguments",
                    provider_turns=(provider_turn,),
                )
            return _second

        rows, metadata = collect_live_candidate(
            [case],
            client=object(),
            advertised_tools=schemas,
            plan_student_turn=fake_plan,
            limits=LiveLimits(max_provider_calls=2, max_total_tokens=100_000_000),
            model="same-model",
            year=1448,
            term=1,
        )

        assert len(calls) == metadata["usage"]["provider_calls"] == 2
        assert calls[1]["repair_reason"] == "plan_validation_failed"
        assert rows[0]["repair"] == {
            "attempted": True,
            "reason": "plan_validation_failed",
            "policy_ids": [],
        }
        if expected_error:
            assert rows[0]["collection_error"] == {"error_category": expected_error}
            assert metadata["repairs"]["failed"] == 1
        else:
            assert "collection_error" not in rows[0]
            assert metadata["repairs"]["succeeded"] == 1
        assert "private rejected arguments" not in json.dumps({"rows": rows, "metadata": metadata})


def test_repair_is_not_marked_attempted_when_the_second_slot_is_not_reserved(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    case = next(item for item in contract["cases"] if item["id"] == "V21-SP-034")
    wrong = {
        "decision": "execute",
        "clarification_kind": "none",
        "requested_outcomes": ["course_priority"],
        "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
        "clarification_question": "",
    }
    calls = 0

    def fake_plan(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _typed_planning_result(wrong)

    rows, metadata = collect_live_candidate(
        [case],
        client=object(),
        advertised_tools=schemas,
        plan_student_turn=fake_plan,
        limits=LiveLimits(max_provider_calls=1, max_total_tokens=100_000_000),
        model="same-model",
        year=1448,
        term=1,
    )

    assert calls == metadata["usage"]["provider_calls"] == 1
    assert metadata["stopped_for"] == "provider-call budget reached"
    assert metadata["repairs"]["attempted"] == 0
    assert rows[0]["repair"] == {
        "attempted": False,
        "reason": "",
        "policy_ids": [],
    }


def test_planner_only_prompt_is_the_full_production_instruction(
    contract: dict[str, Any],
) -> None:
    prompt = " ".join(
        str(planner_messages(contract["cases"][28], year=1448, term=1)[0]["content"]).split()
    )

    assert "not corequisites" in prompt
    assert "clarification_kind=timetable_load" in prompt
    assert "clarification_kind=timetable_preference" in prompt
    assert "clarification_kind=course_or_section_identity" in prompt
    assert "FINAL EXACT CONTRACT CHECK" in prompt
    assert "Do not expose hidden reasoning" in prompt


def test_context_boundaries_use_real_role_history(contract: dict[str, Any]) -> None:
    by_id = {case["id"]: case for case in contract["cases"]}
    sp028 = by_id["V21-SP-028"]
    sp029 = by_id["V21-SP-029"]

    assert "context" not in sp028
    assert "history" not in sp028
    assert sp029["history"] == [
        {
            "role": "user",
            "content": "كنت أقارن بين شعبتي DS341-M2 وDS432-M2.",
        }
    ]
    messages = planner_messages(sp029, year=1448, term=1)
    assert [message["role"] for message in messages] == ["system", "user", "user"]
    assert messages[1] == sp029["history"][0]
    assert "evaluation_conversation_context" not in json.dumps(messages, ensure_ascii=False)


def test_shared_planner_prompt_import_needs_no_django_or_provider_setup() -> None:
    project_root = pathlib.Path(__file__).resolve().parents[1]
    code = r"""
import builtins
import sys

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if (
        name == "django"
        or name.startswith("django.")
        or name == "core.services.llm_backend"
        or name == "core.services.student_advisor_v2"
    ):
        raise AssertionError(f"forbidden import: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from core.services.student_advisor_v21_prompt import build_student_v21_planner_messages

messages = build_student_v21_planner_messages(
    question="hello",
    academic_year=1448,
    term=1,
)
assert messages[-1]["content"].endswith("student_question: hello")
assert not any(name == "django" or name.startswith("django.") for name in sys.modules)
assert "core.services.llm_backend" not in sys.modules
print("stdlib-only-ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "stdlib-only-ok"


def test_sp029_clarification_kind_survives_runner_serialization() -> None:
    from core.services.student_advisor_v21_plan import (
        ClarificationKind,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )

    mapped = _plan_mapping(
        StudentTurnPlan(
            decision=TurnPlanDecision.CLARIFY,
            clarification_kind=ClarificationKind.COURSE_OR_SECTION_IDENTITY,
            clarification_question="Which course owns M2?",
            requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
            evidence_requests=(),
        )
    )

    assert mapped["clarification_kind"] == "course_or_section_identity"
    assert mapped["clarification_question"] == "Which course owns M2?"


def test_live_runner_commits_the_ceiling_even_if_provider_usage_is_empty(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    from types import SimpleNamespace

    from core.services.student_advisor_v21_plan import (
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )

    first_messages = planner_messages(contract["cases"][0], year=1448, term=1)
    one_call_budget = estimated_call_token_ceiling(
        first_messages,
        schemas,
        max_plan_tokens=900,
    )
    calls = 0

    def fake_plan(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            plan=StudentTurnPlan(
                decision=TurnPlanDecision.EXECUTE,
                evidence_requests=(
                    PlannedCapabilityCall(
                        capability="build_timetable_proposal",
                        arguments={"mode": "around_current", "max_credits": 14},
                    ),
                ),
                requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
            ),
            provider_turn=SimpleNamespace(
                usage={},
                model="fake",
                model_revision="",
            ),
        )

    rows, metadata = collect_live_candidate(
        contract["cases"],
        client=object(),
        advertised_tools=schemas,
        plan_student_turn=fake_plan,
        limits=LiveLimits(max_provider_calls=2, max_total_tokens=one_call_budget),
        model="fake",
        year=1448,
        term=1,
    )

    assert len(rows) == calls == 1
    assert metadata["usage"]["total_tokens"] == 0
    assert metadata["usage"]["committed_token_ceiling"] == one_call_budget
    assert metadata["stopped_for"].startswith("conservative token budget")


def test_live_runner_records_invalid_failure_and_continues_without_secret_text(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    from types import SimpleNamespace

    from core.services.student_advisor_v21_plan import (
        ClarificationKind,
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )

    class SecretProviderError(RuntimeError):
        pass

    next_case = 0

    def fake_plan(*_args, **_kwargs):
        nonlocal next_case
        case = contract["cases"][next_case]
        next_case += 1
        if case["id"] == "V21-SP-001":
            raise SecretProviderError("private provider payload must never enter the report")
        plan = _perfect_plan(case)
        return SimpleNamespace(
            plan=StudentTurnPlan(
                decision=TurnPlanDecision(plan["decision"]),
                evidence_requests=tuple(
                    PlannedCapabilityCall(
                        capability=request["capability"],
                        arguments=request["arguments"],
                    )
                    for request in plan["evidence_requests"]
                ),
                clarification_question=plan["clarification_question"],
                clarification_kind=ClarificationKind(plan["clarification_kind"]),
                requested_outcomes=tuple(
                    StudentRequestOutcome(outcome) for outcome in plan["requested_outcomes"]
                ),
            ),
            provider_turn=SimpleNamespace(usage={}, model="same-model", model_revision=""),
        )

    rows, metadata = collect_live_candidate(
        contract["cases"],
        client=object(),
        advertised_tools=schemas,
        plan_student_turn=fake_plan,
        limits=LiveLimits(max_provider_calls=36, max_total_tokens=100_000_000),
        model="same-model",
        year=1448,
        term=1,
    )

    assert len(rows) == next_case == 36
    assert metadata["usage"]["provider_calls"] == 36
    assert metadata["collection_errors"] == [
        {"case_id": "V21-SP-001", "error_category": "SecretProviderError"}
    ]
    assert "private provider payload" not in json.dumps({"rows": rows, "metadata": metadata})

    baseline = _with_model(_weaker_baseline(contract), model="same-model", revision="")
    report = build_report(
        rows,
        baseline,
        contract=contract,
        advertised_tools=schemas,
        runner_metadata={"mode": "live", **metadata},
    )

    assert report["candidate_report"]["coverage"]["complete"] is True
    assert report["typed_plan_errors"] == [
        {"case_id": "V21-SP-001", "error_category": "SecretProviderError"}
    ]
    failed = next(
        row for row in report["candidate_report"]["rows"] if row["case_id"] == "V21-SP-001"
    )
    assert failed["dimensions"]["mode_correct"] is False
    assert failed["dimensions"]["required_tools_correct"] is False
    assert failed["dimensions"]["tool_minimality_correct"] is False
    assert failed["dimensions"]["arguments_correct"] is False
    assert failed["overall"] is False
    assert report["comparison_gate"]["checks"]["typed_candidate_valid"] is False
    assert report["comparison_gate"]["passed"] is False
    assert report["rows"][0]["collection_error"] == {"error_category": "SecretProviderError"}


def test_report_retains_matching_model_and_marks_missing_revision_unverified(
    contract: dict[str, Any], schemas: list[dict[str, Any]]
) -> None:
    candidate = _with_model(_perfect_rows(contract), model="same-model")
    baseline = _with_model(_weaker_baseline(contract), model="same-model")

    report = build_report(
        candidate,
        baseline,
        contract=contract,
        advertised_tools=schemas,
        runner_metadata={"mode": "live", "model": "same-model"},
    )

    assert {row["model"] for row in report["rows"]} == {"same-model"}
    assert {row["model_revision"] for row in report["rows"]} == {""}
    assert report["model_provenance"]["model_ids_match"] is True
    assert report["model_provenance"]["model_revisions_match"] is None
    assert report["model_provenance"]["same_snapshot_verified"] is False
    assert "do not prove the same provider snapshot" in " ".join(
        report["model_provenance"]["limitations"]
    )
    assert report["comparison_gate"]["checks"]["same_model_ids"] is True
    assert report["comparison_gate"]["passed"] is True


@pytest.mark.parametrize(
    (
        "candidate_model",
        "candidate_revision",
        "baseline_model",
        "baseline_revision",
        "model_ids_match",
        "revisions_match",
    ),
    [
        ("candidate-model", "revision-1", "baseline-model", "revision-1", False, True),
        ("same-model", "revision-2", "same-model", "revision-1", True, False),
    ],
)
def test_report_fails_comparison_for_model_or_available_revision_mismatch(
    contract: dict[str, Any],
    schemas: list[dict[str, Any]],
    candidate_model: str,
    candidate_revision: str,
    baseline_model: str,
    baseline_revision: str,
    model_ids_match: bool,
    revisions_match: bool,
) -> None:
    candidate = _with_model(
        _perfect_rows(contract),
        model=candidate_model,
        revision=candidate_revision,
    )
    baseline = _with_model(
        _weaker_baseline(contract),
        model=baseline_model,
        revision=baseline_revision,
    )

    report = build_report(
        candidate,
        baseline,
        contract=contract,
        advertised_tools=schemas,
    )

    assert report["model_provenance"]["model_ids_match"] is model_ids_match
    assert report["model_provenance"]["model_revisions_match"] is revisions_match
    assert report["comparison_gate"]["checks"]["same_model_ids"] is model_ids_match
    assert (
        report["comparison_gate"]["checks"]["same_model_revision_if_available"] is revisions_match
    )
    assert report["comparison_gate"]["passed"] is False


def test_offline_cli_never_resolves_or_calls_a_provider(
    contract: dict[str, Any], schemas: list[dict[str, Any]], tmp_path, monkeypatch
) -> None:
    import evals.advisor.run_v21_semantic_plan as runner
    from core.services.student_advisor_v21_plan import parse_turn_plan_result, plan_student_turn

    def forbidden_client():
        raise AssertionError("offline replay must not resolve a provider")

    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (forbidden_client, lambda: schemas, parse_turn_plan_result, plan_student_turn),
    )
    candidate_path = tmp_path / "candidate.json"
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "report.json"
    candidate_path.write_text(
        json.dumps(_perfect_rows(contract), ensure_ascii=False), encoding="utf-8"
    )
    baseline_path.write_text(
        json.dumps(_weaker_baseline(contract), ensure_ascii=False), encoding="utf-8"
    )

    exit_code = main(
        [
            "--candidate",
            str(candidate_path),
            "--baseline",
            str(baseline_path),
            "--output",
            str(output_path),
            "--compact",
        ]
    )

    assert exit_code == 0
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["runner"] == {"mode": "offline_replay", "provider_calls": 0}
    assert saved["comparison_gate"]["passed"] is True


def test_runner_stdout_falls_back_to_ascii_json_on_cp1252() -> None:
    project_root = pathlib.Path(__file__).resolve().parents[1]
    code = r"""
import json
import sys

sys.stdout.reconfigure(encoding="cp1252")
from evals.advisor.run_v21_semantic_plan import _write_or_print

_write_or_print({"query": "متى تفتح بوابة الحذف والإضافة؟"}, None, True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"query": "متى تفتح بوابة الحذف والإضافة؟"}
    assert completed.stdout.isascii()


def test_runner_output_file_remains_utf8_and_unescaped(tmp_path, capsys) -> None:
    report = {"query": "متى تفتح بوابة الحذف والإضافة؟"}
    output = tmp_path / "report.json"

    _write_or_print(report, output, True)

    rendered = output.read_text(encoding="utf-8")
    assert json.loads(rendered) == report
    assert report["query"] in rendered
    assert "\\u0645" not in rendered
    assert capsys.readouterr().out.strip() == f"wrote {output}"


def test_v2_baseline_collector_receives_no_gold_fields(contract: dict[str, Any]) -> None:
    projected = v2_baseline_inputs(contract)

    assert len(projected) == 36
    assert all(set(case) <= {"id", "language", "question", "history"} for case in projected)
    assert all(
        not ({"expected_mode", "required_tools", "forbidden_tools", "expected_goal"} & set(case))
        for case in projected
    )


def test_v2_gate_baseline_exposes_current_regex_regressions_without_gold(
    contract: dict[str, Any],
) -> None:
    by_id = {case["id"]: case for case in v2_baseline_inputs(contract)}

    text_edit, _trace, _warnings = derive_v2_baseline_plan(by_id["V21-SP-020"]["question"], [])
    past_history, history_trace, _warnings = derive_v2_baseline_plan(
        by_id["V21-SP-025"]["question"], []
    )
    ambiguous_pin, _trace, _warnings = derive_v2_baseline_plan(by_id["V21-SP-029"]["question"], [])
    portal_policy, _trace, _warnings = derive_v2_baseline_plan(by_id["V21-SP-032"]["question"], [])

    assert text_edit == {
        "mode": "execute",
        "tool_calls": [
            {
                "name": "graduation_progress",
                "arguments": {
                    "planning_baseline_kind": "recommended_current_term",
                    "search_better_replacements": True,
                },
            }
        ],
    }
    assert [call["name"] for call in past_history["tool_calls"]] == ["graduation_progress"]
    assert history_trace["model_first_turn_tools"] == []
    assert history_trace["deterministic_gate_tools"] == ["graduation_progress"]
    assert [call["name"] for call in ambiguous_pin["tool_calls"]] == ["build_timetable_proposal"]
    assert portal_policy["tool_calls"] == [
        {
            "name": "graduation_progress",
            "arguments": {"planning_baseline_kind": "registered_timetable"},
        },
        {
            "name": "policy_lookup",
            "arguments": {"query": by_id["V21-SP-032"]["question"]},
        },
    ]


def test_v2_baseline_applies_real_argument_normalizers_to_observed_calls() -> None:
    plan, trace, warnings = derive_v2_baseline_plan(
        "Build a timetable from scratch and do not exceed 12 credits.",
        [{"name": "build_timetable_proposal", "arguments": {}}],
    )

    assert warnings == []
    assert plan == {
        "mode": "execute",
        "tool_calls": [
            {
                "name": "build_timetable_proposal",
                "arguments": {"mode": "from_scratch", "max_credits": 12},
            }
        ],
    }
    assert trace["model_first_turn_tools"] == ["build_timetable_proposal"]
    assert trace["deterministic_gate_tools"] == ["build_timetable_proposal"]


@dataclass(frozen=True)
class _FakeLiveConfig:
    max_retries: int = 2
    max_tokens: int = 2000
    timeout_seconds: float = 45.0
    model: str = "v2-baseline-fake"


class _NoToolV2Client:
    backend = "local"

    def __init__(self) -> None:
        self.calls = 0
        self.http_calls = 0
        self.http_responses = 0
        self.config = _FakeLiveConfig()

    def resolve_model(self, requested=None):
        return requested or "v2-baseline-fake"

    def chat_with_tools(self, _messages, **_kwargs):
        from core.services.llm_backend import ToolChatResult

        self.calls += 1
        self.http_calls += 1
        self.http_responses += 1
        return ToolChatResult(
            content="A prose-only first turn.",
            tool_calls=(),
            model="v2-baseline-fake",
            usage={},
            assistant_message={"role": "assistant", "content": "A prose-only first turn."},
        )


def test_bounded_v2_collection_produces_reusable_explicit_baseline(
    contract: dict[str, Any], v2_schemas: list[dict[str, Any]]
) -> None:
    client = _NoToolV2Client()
    rows, metadata = collect_v2_baseline(
        v2_baseline_inputs(contract),
        client=client,
        advertised_tools=v2_schemas,
        limits=LiveLimits(
            max_provider_calls=36,
            max_total_tokens=10_000_000,
            max_plan_tokens=1800,
        ),
        model="v2-baseline-fake",
        year=1448,
        term=1,
    )
    artifact = build_v2_baseline_report(
        rows,
        contract=contract,
        collection_metadata=metadata,
    )

    assert len(rows) == client.calls == 36
    assert metadata["usage"]["provider_calls"] == 36
    assert metadata["usage"]["committed_token_ceiling"] <= 10_000_000
    assert artifact["baseline_collection"]["collection_valid"] is True
    assert artifact["baseline_collection"]["gold_labels_visible_to_collector"] is False
    assert artifact["baseline_collection"]["student_or_evidence_tools_executed"] is False
    assert len(artifact["baseline_collection"]["limitations"]) >= 5
    assert artifact["baseline_report"]["coverage"]["complete"] is True
    assert artifact["rows"] == rows
    assert score_batch(artifact, contract)["coverage"]["complete"] is True


def test_collect_v2_cli_requires_confirmation_before_runtime_or_provider(
    tmp_path, monkeypatch
) -> None:
    import evals.advisor.run_v21_semantic_plan as runner

    def forbidden_dependencies():
        raise AssertionError("safety validation must happen before provider setup")

    monkeypatch.setattr(runner, "_runtime_dependencies", forbidden_dependencies)
    with pytest.raises(SystemExit):
        main(
            [
                "--collect-v2-baseline",
                "--output",
                str(tmp_path / "baseline.json"),
                "--max-provider-calls",
                "36",
                "--max-total-tokens",
                "10000000",
            ]
        )


def test_live_cli_rejects_pin_annotation_drift_before_client_creation(
    contract: dict[str, Any], tmp_path, monkeypatch
) -> None:
    import evals.advisor.run_v21_semantic_plan as runner
    from core.services.student_advisor_v21_plan import parse_turn_plan_result, plan_student_turn

    poisoned = copy.deepcopy(contract)
    sp036 = next(case for case in poisoned["cases"] if case["id"] == "V21-SP-036")
    sp036["policy_explicit_pins"] = [{"course_code": "DS341", "section_label": "F2"}]
    poisoned["_semantic_policy_context_bound"] = True
    calls = {"client": 0, "schemas": 0}

    def forbidden_client():
        calls["client"] += 1
        raise AssertionError("annotation drift must fail before client creation")

    def forbidden_schemas():
        calls["schemas"] += 1
        raise AssertionError("annotation drift must fail before schema collection")

    monkeypatch.setattr(runner, "load_contract", lambda: poisoned)
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (forbidden_client, forbidden_schemas, parse_turn_plan_result, plan_student_turn),
    )

    with pytest.raises(SystemExit):
        main(
            [
                "--live",
                "--confirm-live-external-request",
                "--max-provider-calls",
                "72",
                "--max-total-tokens",
                "10000000",
                "--baseline",
                str(tmp_path / "unused-baseline.json"),
            ]
        )

    assert calls == {"client": 0, "schemas": 0}


def test_live_transport_retries_are_disabled_and_attempts_match_reservations(
    monkeypatch,
) -> None:
    from urllib.error import URLError

    import core.services.llm_backend as llm_backend
    from core.services.llm_backend import (
        LLMEndpointConfig,
        LLMUnavailable,
        OpenAICompatibleLLMClient,
        reset_circuit_breaker,
    )

    config = LLMEndpointConfig(
        backend="local",
        provider="local",
        base_url="http://127.0.0.1:1234/v1",
        model="fake",
        timeout_seconds=30.0,
        max_tokens=1200,
        max_retries=2,
    )
    client = OpenAICompatibleLLMClient(config)
    initial = _prepare_bounded_live_client(
        client,
        max_tokens=900,
        timeout_seconds=10.0,
    )
    transport_attempts = 0

    def retryable_timeout(*_args, **_kwargs):
        nonlocal transport_attempts
        transport_attempts += 1
        raise URLError(TimeoutError("private timeout detail"))

    reset_circuit_breaker()
    monkeypatch.setattr(llm_backend, "_http_open", retryable_timeout)
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "bounded test"}], max_tokens=10)

    assert client.config.max_retries == 0
    assert transport_attempts == 1
    assert _verified_http_attempt_deltas(
        client,
        initial=initial,
        logical_provider_calls=1,
    ) == {"http_calls": 1, "http_responses": 0, "max_retries": 0}


def test_blank_live_model_fails_before_unreserved_discovery() -> None:
    client = _NoToolV2Client()
    client.config = _FakeLiveConfig(model="")
    initial = _prepare_bounded_live_client(
        client,
        max_tokens=900,
        timeout_seconds=10.0,
    )

    with pytest.raises(ValueError, match="explicit --model"):
        _resolve_bounded_live_model(client, None)

    assert initial == (0, 0)
    assert client.http_calls == client.http_responses == 0


def test_collect_v2_cli_writes_scorer_compatible_artifact(
    contract: dict[str, Any],
    schemas: list[dict[str, Any]],
    v2_schemas: list[dict[str, Any]],
    tmp_path,
    monkeypatch,
) -> None:
    import evals.advisor.run_v21_semantic_plan as runner
    from core.services.student_advisor_v21_plan import parse_turn_plan_result, plan_student_turn

    client = _NoToolV2Client()
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (lambda: client, lambda: schemas, parse_turn_plan_result, plan_student_turn),
    )
    monkeypatch.setattr(runner, "_v2_tool_schemas", lambda: v2_schemas)
    output = tmp_path / "v2-baseline.json"
    exit_code = main(
        [
            "--collect-v2-baseline",
            "--confirm-live-external-request",
            "--max-provider-calls",
            "36",
            "--max-total-tokens",
            "10000000",
            "--output",
            str(output),
            "--compact",
        ]
    )

    assert exit_code == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert len(artifact["rows"]) == 36
    assert artifact["baseline_collection"]["collection_valid"] is True
    assert artifact["baseline_collection"]["method"].startswith("v2_first_model_turn")
    assert artifact["baseline_collection"]["budgets"]["max_retries"] == 0
    assert artifact["baseline_collection"]["transport"] == {
        "http_calls": 36,
        "http_responses": 36,
        "max_retries": 0,
    }
