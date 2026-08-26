from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys

import pytest
import yaml

from evals.advisor.v21_semantic_plan_eval import (
    COMMON_COMPARISON_DIMENSIONS,
    OUTCOME_CAPABILITY_OWNERS,
    SCORE_DIMENSIONS,
    V21_TOOL_SURFACE,
    ContractValidationError,
    ResultValidationError,
    candidate_quality_gate,
    compare_quality_gate,
    load_contract,
    question_fingerprint,
    score_batch,
    score_case,
    validate_contract,
)


@pytest.fixture(scope="module")
def contract() -> dict:
    return load_contract()


def _perfect_plan(case: dict) -> dict:
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
        "clarification_question": "Which course?" if case["expected_mode"] == "clarify" else "",
    }


def _perfect_rows(contract: dict) -> dict:
    return {
        "rows": [{"case_id": case["id"], "plan": _perfect_plan(case)} for case in contract["cases"]]
    }


def _case(contract: dict, case_id: str) -> dict:
    return next(case for case in contract["cases"] if case["id"] == case_id)


def test_contract_is_versioned_focused_private_and_balanced(contract: dict) -> None:
    assert contract["meta"]["name"] == "advisor_v21_semantic_plan"
    assert contract["meta"]["version"] == "2.4"
    assert contract["meta"]["count"] == len(contract["cases"]) == 36
    assert tuple(contract["meta"]["scoring_dimensions"]) == SCORE_DIMENSIONS
    assert {case["language"] for case in contract["cases"]} == {"ar-SA", "en"}
    assert sum(case["case_type"] == "regex_false_positive" for case in contract["cases"]) >= 8
    assert all("student_id" not in case["required_arguments"] for case in contract["cases"])


def test_contract_tool_surface_matches_v21_runtime(contract: dict) -> None:
    from core.services.student_advisor_v2 import STUDENT_V21_TOOL_NAMES

    assert tuple(contract["meta"]["tool_surface"]) == V21_TOOL_SURFACE
    assert V21_TOOL_SURFACE == STUDENT_V21_TOOL_NAMES


def test_eval_outcome_owners_match_runtime_and_saudi_scorer() -> None:
    from core.services.student_advisor_v21_outcomes import OUTCOME_CAPABILITIES
    from core.services.student_advisor_v21_plan import SERVER_OWNED_EXECUTE_OUTCOMES
    from evals.advisor.run_v21_saudi_e2e import (
        _OUTCOME_CAPABILITY_OWNERS,
        _SERVER_OWNED_EXECUTE_OUTCOMES,
    )
    from evals.advisor.v21_semantic_plan_eval import (
        SERVER_OWNED_EXECUTE_OUTCOMES as EVAL_SERVER_OWNED_EXECUTE_OUTCOMES,
    )

    runtime_owners = {
        outcome.value: frozenset(capabilities)
        for outcome, capabilities in OUTCOME_CAPABILITIES.items()
    }

    assert OUTCOME_CAPABILITY_OWNERS == runtime_owners
    assert _OUTCOME_CAPABILITY_OWNERS == runtime_owners
    runtime_server_owned = frozenset(outcome.value for outcome in SERVER_OWNED_EXECUTE_OUTCOMES)
    assert EVAL_SERVER_OWNED_EXECUTE_OUTCOMES == runtime_server_owned
    assert _SERVER_OWNED_EXECUTE_OUTCOMES == runtime_server_owned


@pytest.mark.parametrize(
    ("decision", "outcomes", "tools"),
    [
        (
            "execute",
            ("course_addition", "graduation_impact"),
            ("recommend_feasible_course_addition",),
        ),
        (
            "execute",
            ("course_drop_impact", "graduation_impact"),
            ("rank_current_course_drop_impact",),
        ),
        (
            "execute",
            ("timetable_review", "graduation_impact", "course_replacement"),
            ("improve_current_timetable",),
        ),
        (
            "execute",
            ("graduation_impact",),
            ("recommend_feasible_course_addition",),
        ),
        (
            "execute",
            ("course_addition", "graduation_impact"),
            ("recommend_feasible_course_addition", "graduation_progress"),
        ),
        (
            "execute",
            ("course_addition", "registration_action"),
            ("recommend_feasible_course_addition",),
        ),
        (
            "execute",
            ("course_addition", "credit_load_comparison"),
            ("recommend_feasible_course_addition",),
        ),
        (
            "execute",
            ("credit_load_comparison",),
            ("recommend_feasible_course_addition",),
        ),
        (
            "execute",
            ("course_comparison", "graduation_impact"),
            ("course_choice_comparison",),
        ),
        (
            "execute",
            ("registration_action",),
            ("recommend_feasible_course_addition",),
        ),
        (
            "unsupported",
            ("registration_action", "unsupported_request"),
            (),
        ),
        ("unsupported", ("credit_load_comparison",), ()),
        ("clarify", ("registration_action",), ()),
    ],
)
def test_eval_outcome_coverage_matches_runtime_truth_table(
    decision: str,
    outcomes: tuple[str, ...],
    tools: tuple[str, ...],
) -> None:
    from core.services.student_advisor_v21_outcomes import evaluate_outcome_coverage
    from core.services.student_advisor_v21_plan import (
        ClarificationKind,
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )
    from evals.advisor.run_v21_saudi_e2e import (
        _outcome_coverage_correct as saudi_outcome_coverage_correct,
    )
    from evals.advisor.v21_semantic_plan_eval import (
        _outcome_coverage_correct as semantic_outcome_coverage_correct,
    )

    runtime_plan = StudentTurnPlan(
        decision=TurnPlanDecision(decision),
        requested_outcomes=tuple(StudentRequestOutcome(item) for item in outcomes),
        evidence_requests=tuple(
            PlannedCapabilityCall(
                capability=tool,
                arguments=(
                    {"objective": "graduation"}
                    if tool == "course_choice_comparison" and "graduation_impact" in outcomes
                    else {}
                ),
            )
            for tool in tools
        ),
        clarification_question="Which course?" if decision == "clarify" else "",
        clarification_kind=(
            ClarificationKind.GENERIC if decision == "clarify" else ClarificationKind.NONE
        ),
    )
    expected = evaluate_outcome_coverage(runtime_plan).valid

    assert (
        semantic_outcome_coverage_correct(
            mode=decision,
            outcomes=outcomes,
            calls=[
                {
                    "name": tool,
                    "arguments": (
                        {"objective": "graduation"}
                        if tool == "course_choice_comparison" and "graduation_impact" in outcomes
                        else {}
                    ),
                }
                for tool in tools
            ],
            errors=(),
        )
        is expected
    )
    assert (
        saudi_outcome_coverage_correct(
            decision=decision,
            outcomes=outcomes,
            tools=tools,
            controls=(
                {"course_choice_comparison": {"objective": "graduation"}}
                if "course_choice_comparison" in tools and "graduation_impact" in outcomes
                else {}
            ),
        )
        is expected
    )


@pytest.mark.parametrize(
    ("objective", "expected"),
    [
        ("graduation", True),
        ("balanced", False),
        ("unlock_impact", False),
        ("timetable_fit", False),
        ("", False),
    ],
)
def test_eval_comparison_impact_ownership_is_control_aware(
    objective: str,
    expected: bool,
) -> None:
    from evals.advisor.run_v21_saudi_e2e import (
        _outcome_coverage_correct as saudi_outcome_coverage_correct,
    )
    from evals.advisor.v21_semantic_plan_eval import (
        _outcome_coverage_correct as semantic_outcome_coverage_correct,
    )

    arguments = {"objective": objective} if objective else {}
    calls = [{"name": "course_choice_comparison", "arguments": arguments}]
    outcomes = ("course_comparison", "graduation_impact")

    assert (
        semantic_outcome_coverage_correct(
            mode="execute",
            outcomes=outcomes,
            calls=calls,
            errors=(),
        )
        is expected
    )
    assert (
        saudi_outcome_coverage_correct(
            decision="execute",
            outcomes=outcomes,
            tools=("course_choice_comparison",),
            controls={"course_choice_comparison": arguments},
        )
        is expected
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {
                "planning_baseline_kind": "recommended_current_term",
                "search_better_replacements": True,
            },
            True,
        ),
        (
            {
                "planning_baseline_kind": "recommended_current_term",
                "search_better_replacements": True,
                "add_current_courses": ["DS341"],
            },
            False,
        ),
        (
            {
                "planning_baseline_kind": "recommended_current_term",
                "search_better_replacements": True,
                "remove_current_courses": ["DS341"],
            },
            False,
        ),
    ],
)
def test_eval_graduation_replacement_controls_match_runtime(
    arguments: dict[str, object],
    expected: bool,
) -> None:
    from core.services.student_advisor_v21_outcomes import evaluate_outcome_coverage
    from core.services.student_advisor_v21_plan import (
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )
    from evals.advisor.run_v21_saudi_e2e import (
        _outcome_coverage_correct as saudi_outcome_coverage_correct,
    )
    from evals.advisor.v21_semantic_plan_eval import (
        _outcome_coverage_correct as semantic_outcome_coverage_correct,
    )

    plan = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        requested_outcomes=(StudentRequestOutcome.COURSE_REPLACEMENT,),
        evidence_requests=(
            PlannedCapabilityCall(
                capability="graduation_progress",
                arguments=dict(arguments),
            ),
        ),
    )
    runtime = evaluate_outcome_coverage(plan).valid
    semantic = semantic_outcome_coverage_correct(
        mode="execute",
        outcomes=("course_replacement",),
        calls=[{"name": "graduation_progress", "arguments": arguments}],
        errors=(),
    )
    saudi = saudi_outcome_coverage_correct(
        decision="execute",
        outcomes=("course_replacement",),
        tools=("graduation_progress",),
        controls={"graduation_progress": arguments},
    )

    assert runtime is expected
    assert semantic is runtime
    assert saudi is runtime


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {
                "planning_baseline_kind": "registered_timetable",
                "noncompletion_current_courses": ["DS332"],
            },
            True,
        ),
        (
            {
                "planning_baseline_kind": "recommended_current_term",
                "noncompletion_current_courses": ["DS332"],
            },
            False,
        ),
        (
            {
                "planning_baseline_kind": "registered_timetable",
                "noncompletion_current_courses": ["DS332"],
                "remove_current_courses": ["DS332"],
            },
            False,
        ),
        (
            {
                "planning_baseline_kind": "registered_timetable",
                "noncompletion_current_courses": ["DS332"],
                "search_better_replacements": True,
            },
            False,
        ),
    ],
)
def test_eval_graduation_noncompletion_controls_match_runtime(
    arguments: dict[str, object],
    expected: bool,
) -> None:
    from core.services.student_advisor_v21_outcomes import evaluate_outcome_coverage
    from core.services.student_advisor_v21_plan import (
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )
    from evals.advisor.run_v21_saudi_e2e import (
        _outcome_coverage_correct as saudi_outcome_coverage_correct,
    )
    from evals.advisor.v21_semantic_plan_eval import (
        _outcome_coverage_correct as semantic_outcome_coverage_correct,
    )

    plan = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
        evidence_requests=(
            PlannedCapabilityCall(
                capability="graduation_progress",
                arguments=dict(arguments),
            ),
        ),
    )
    runtime = evaluate_outcome_coverage(plan).valid
    semantic = semantic_outcome_coverage_correct(
        mode="execute",
        outcomes=("graduation_impact",),
        calls=[{"name": "graduation_progress", "arguments": arguments}],
        errors=(),
    )
    saudi = saudi_outcome_coverage_correct(
        decision="execute",
        outcomes=("graduation_impact",),
        tools=("graduation_progress",),
        controls={"graduation_progress": arguments},
    )

    assert runtime is expected
    assert semantic is runtime
    assert saudi is runtime


@pytest.mark.parametrize(
    "boundary_outcome",
    ["registration_action", "credit_load_comparison"],
)
def test_contract_accepts_mixed_execute_server_boundary(
    contract: dict,
    boundary_outcome: str,
) -> None:
    mixed = copy.deepcopy(contract)
    mixed["cases"][0]["expected_outcomes"] = [
        "timetable_build",
        boundary_outcome,
    ]

    validate_contract(mixed)

    boundary_only = copy.deepcopy(mixed)
    boundary_only["cases"][0]["expected_outcomes"] = [boundary_outcome]
    with pytest.raises(ContractValidationError, match="evidence-backed outcome"):
        validate_contract(boundary_only)


def test_questions_are_holdout_phrases_not_existing_eval_copies(contract: dict) -> None:
    with open("evals/advisor/questions.yaml", encoding="utf-8") as handle:
        general = yaml.safe_load(handle)
    with open("evals/advisor/planner_priority_eval_v1.yaml", encoding="utf-8") as handle:
        legacy = yaml.safe_load(handle)
    existing = {
        question_fingerprint(row["ar"])
        for row in general["questions"]
        if isinstance(row, dict) and row.get("ar")
    }
    existing.update(
        question_fingerprint(row["question_ar"])
        for row in legacy["cases"]
        if isinstance(row, dict) and row.get("question_ar")
    )
    holdout = {question_fingerprint(case["question"]) for case in contract["cases"]}
    assert not holdout & existing


def test_perfect_semantic_plans_score_exactly_and_pass_absolute_gate(contract: dict) -> None:
    report = score_batch(_perfect_rows(contract), contract)
    assert report["all_passed"] is True
    assert report["exact_match_rate"] == 1.0
    assert report["common_comparison_exact_match_rate"] == 1.0
    assert all(metric["rate"] == 1.0 for metric in report["dimensions"].values())
    assert candidate_quality_gate(report, contract)["passed"] is True


def test_runtime_dataclass_shape_is_supported(contract: dict) -> None:
    from core.services.student_advisor_v21_plan import (
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
    )

    case = _case(contract, "V21-SP-001")
    plan = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall(
                capability="build_timetable_proposal",
                arguments={"max_credits": 14, "mode": "around_current"},
            ),
        ),
        requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
    )
    assert score_case(case, plan)["overall"] is True


def test_argument_list_order_is_not_a_semantic_failure(contract: dict) -> None:
    case = _case(contract, "V21-SP-002")
    plan = _perfect_plan(case)
    plan["evidence_requests"][0]["arguments"]["must_take_courses"].reverse()
    assert score_case(case, plan)["dimensions"]["arguments_correct"] is True


def test_requested_outcome_order_is_not_a_semantic_failure(contract: dict) -> None:
    case = next(item for item in contract["cases"] if len(item["expected_outcomes"]) > 1)
    plan = _perfect_plan(case)
    plan["requested_outcomes"].reverse()

    assert score_case(case, plan)["dimensions"]["outcomes_correct"] is True


def test_wrong_constraint_fails_arguments_only_where_expected(contract: dict) -> None:
    case = _case(contract, "V21-SP-001")
    plan = _perfect_plan(case)
    plan["evidence_requests"][0]["arguments"]["max_credits"] = 15
    scored = score_case(case, plan)
    assert scored["dimensions"]["arguments_correct"] is False
    assert scored["dimensions"]["required_tools_correct"] is True
    assert scored["overall"] is False


def test_identity_argument_is_forbidden_even_when_nested(contract: dict) -> None:
    case = _case(contract, "V21-SP-001")
    plan = _perfect_plan(case)
    plan["evidence_requests"][0]["arguments"]["debug"] = {"student_id": 42}
    scored = score_case(
        case,
        plan,
        forbidden_model_arguments=contract["meta"]["forbidden_model_arguments"],
    )
    assert scored["dimensions"]["arguments_correct"] is False


@pytest.mark.parametrize(
    ("case_id", "bad_tool"),
    [
        ("V21-SP-019", "graduation_progress"),
        ("V21-SP-020", "feasible_course_replacements"),
        ("V21-SP-023", "policy_lookup"),
        ("V21-SP-024", "my_progress"),
        ("V21-SP-026", "build_timetable_proposal"),
        ("V21-SP-030", "my_clash_free_sections"),
        ("V21-SP-032", "why_course_locked"),
    ],
)
def test_regex_keyword_false_positives_are_explicit_failures(
    contract: dict, case_id: str, bad_tool: str
) -> None:
    case = _case(contract, case_id)
    plan = _perfect_plan(case)
    plan["evidence_requests"] = [{"capability": bad_tool, "arguments": {}}]
    scored = score_case(case, plan)
    assert scored["dimensions"]["forbidden_tools_correct"] is False
    assert scored["overall"] is False


def test_clarification_with_a_tool_call_fails_minimality(contract: dict) -> None:
    case = _case(contract, "V21-SP-028")
    scored = score_case(
        case,
        {
            "decision": "clarify",
            "requested_outcomes": list(case["expected_outcomes"]),
            "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
        },
    )
    assert scored["dimensions"]["mode_correct"] is True
    assert scored["dimensions"]["tool_minimality_correct"] is False
    assert scored["dimensions"]["forbidden_tools_correct"] is False


def test_batch_rejects_missing_duplicate_and_unknown_case_ids(contract: dict) -> None:
    rows = _perfect_rows(contract)["rows"]
    with pytest.raises(ResultValidationError, match="missing result ids"):
        score_batch({"rows": rows[:-1]}, contract)
    with pytest.raises(ResultValidationError, match="duplicate result ids"):
        score_batch({"rows": [*rows, rows[0]]}, contract)
    with pytest.raises(ResultValidationError, match="unknown result ids"):
        score_batch(
            {"rows": [*rows, {"case_id": "V21-SP-999", "plan": {}}]},
            contract,
        )


def test_comparison_gate_requires_absolute_quality_and_measurable_lift(contract: dict) -> None:
    candidate_rows = _perfect_rows(contract)
    baseline_rows = copy.deepcopy(candidate_rows)
    for row in baseline_rows["rows"][:6]:
        row["plan"] = {"decision": "direct", "evidence_requests": []}
    candidate = score_batch(candidate_rows, contract)
    baseline = score_batch(baseline_rows, contract)
    comparison = compare_quality_gate(candidate, baseline, contract)
    assert comparison["passed"] is True
    assert comparison["absolute_lift"] >= 0.10
    assert compare_quality_gate(candidate, candidate, contract)["passed"] is False


def test_missing_v2_outcome_field_cannot_create_artificial_comparative_lift(
    contract: dict,
) -> None:
    candidate_rows = _perfect_rows(contract)
    baseline_rows = copy.deepcopy(candidate_rows)
    for row in baseline_rows["rows"]:
        row["plan"].pop("requested_outcomes")

    candidate = score_batch(candidate_rows, contract)
    baseline = score_batch(baseline_rows, contract)
    comparison = compare_quality_gate(candidate, baseline, contract)

    assert baseline["exact_match_rate"] == 0.0
    assert baseline["common_comparison_exact_match_rate"] == 1.0
    assert comparison["comparison_metric"] == "common_comparison_exact_match_rate"
    assert comparison["common_comparison_dimensions"] == list(COMMON_COMPARISON_DIMENSIONS)
    assert comparison["absolute_lift"] == 0.0
    assert comparison["passed"] is False


def test_missing_v2_clarification_kind_cannot_create_artificial_comparative_lift(
    contract: dict,
) -> None:
    candidate_rows = _perfect_rows(contract)
    baseline_rows = copy.deepcopy(candidate_rows)
    for row in baseline_rows["rows"]:
        row["plan"].pop("clarification_kind")

    candidate = score_batch(candidate_rows, contract)
    baseline = score_batch(baseline_rows, contract)
    comparison = compare_quality_gate(candidate, baseline, contract)

    assert baseline["exact_match_rate"] == 0.0
    assert baseline["common_comparison_exact_match_rate"] == 1.0
    assert comparison["absolute_lift"] == 0.0
    assert comparison["passed"] is False


@pytest.mark.parametrize("observed", [None, "none", "timetable_preference"])
def test_sp029_requires_the_exact_closed_clarification_kind(
    contract: dict,
    observed: str | None,
) -> None:
    case = _case(contract, "V21-SP-029")
    plan = _perfect_plan(case)
    if observed is None:
        plan.pop("clarification_kind")
    else:
        plan["clarification_kind"] = observed

    scored = score_case(case, plan)

    assert scored["dimensions"]["clarification_kind_correct"] is False
    assert scored["overall"] is False


def test_clarification_kind_gate_is_exact(contract: dict) -> None:
    rows = _perfect_rows(contract)
    for row in rows["rows"]:
        if row["plan"]["decision"] == "clarify":
            row["plan"]["clarification_kind"] = "generic"

    report = score_batch(rows, contract)
    gate = candidate_quality_gate(report, contract)

    assert report["dimensions"]["clarification_kind_correct"]["rate"] < 1.0
    assert gate["checks"]["minimum_clarification_kind"] is False
    assert gate["passed"] is False


def test_semantic_policy_cases_are_non_vacuous_and_score_independently(
    contract: dict,
) -> None:
    from core.services.student_advisor_v21_policy import active_semantic_policy_ids

    policy_cases = [_case(contract, f"V21-SP-{index:03d}") for index in range(33, 37)]
    expected_ids = (
        "standalone_corequisite_unsupported",
        "single_course_choice_balanced",
        "plain_available_courses_only",
        "pinned_course_addition_balanced",
    )

    for case, expected_id in zip(policy_cases, expected_ids, strict=True):
        assert tuple(
            policy.value
            for policy in active_semantic_policy_ids(
                case["question"],
                explicit_pins=case.get("policy_explicit_pins"),
            )
        ) == (expected_id,)
        assert (
            score_case(case, _perfect_plan(case))["dimensions"]["semantic_policy_correct"] is True
        )

    pinned = policy_cases[-1]
    wrong = _perfect_plan(pinned)
    wrong["evidence_requests"][0]["arguments"]["objective"] = "timetable_fit"
    scored = score_case(pinned, wrong)

    assert scored["dimensions"]["outcome_coverage_correct"] is True
    assert scored["dimensions"]["semantic_policy_correct"] is False
    assert scored["dimensions"]["arguments_correct"] is False


def test_semantic_policy_gate_requires_every_closed_case(contract: dict) -> None:
    rows = _perfect_rows(contract)
    sp036 = next(row for row in rows["rows"] if row["case_id"] == "V21-SP-036")
    sp036["plan"]["evidence_requests"][0]["arguments"]["objective"] = "timetable_fit"

    report = score_batch(rows, contract)
    gate = candidate_quality_gate(report, contract)

    assert report["dimensions"]["semantic_policy_correct"]["rate"] < 1.0
    assert gate["checks"]["minimum_semantic_policy"] is False
    assert gate["passed"] is False


def test_direct_evaluator_cli_rejects_a_pinned_policy_miss(
    contract: dict,
    tmp_path,
) -> None:
    candidate = _perfect_rows(contract)
    sp036 = next(row for row in candidate["rows"] if row["case_id"] == "V21-SP-036")
    sp036["plan"]["evidence_requests"][0]["arguments"]["objective"] = "timetable_fit"
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    project_root = pathlib.Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            "evals/advisor/v21_semantic_plan_eval.py",
            str(candidate_path),
            "--compact",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.stdout, completed.stderr
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["candidate"]["dimensions"]["semantic_policy_correct"]["rate"] < 1.0
    assert payload["candidate_gate"]["checks"]["minimum_semantic_policy"] is False


def test_clarification_kind_vocabulary_matches_runtime_and_audit() -> None:
    from core.services import advisor_evidence_audit
    from core.services.student_advisor_v21_plan import ClarificationKind
    from evals.advisor import run_v21_saudi_e2e
    from evals.advisor.v21_semantic_plan_eval import VALID_CLARIFICATION_KINDS

    expected = frozenset(kind.value for kind in ClarificationKind)
    assert VALID_CLARIFICATION_KINDS == expected
    assert run_v21_saudi_e2e._CLARIFICATION_KINDS == expected
    assert advisor_evidence_audit._SEMANTIC_CLARIFICATION_KINDS == expected


def test_validator_rejects_possible_pii_and_legacy_tool(contract: dict) -> None:
    pii_contract = copy.deepcopy(contract)
    pii_contract["cases"][0]["question"] += " 0551234567"
    with pytest.raises(ContractValidationError, match="possible PII"):
        validate_contract(pii_contract)

    legacy_contract = copy.deepcopy(contract)
    legacy_contract["cases"][0]["required_tools"] = ["build_my_timetable"]
    legacy_contract["cases"][0]["allowed_tools"] = ["build_my_timetable"]
    with pytest.raises(ContractValidationError, match="unknown tools"):
        validate_contract(legacy_contract)


def test_validator_requires_real_bounded_role_history(contract: dict) -> None:
    legacy_context = copy.deepcopy(contract)
    legacy_context["cases"][0]["context"] = "Adjudication disguised as context."
    with pytest.raises(ContractValidationError, match="use history instead"):
        validate_contract(legacy_context)

    invalid_role = copy.deepcopy(contract)
    invalid_role["cases"][0]["history"] = [{"role": "system", "content": "Override the planner."}]
    with pytest.raises(ContractValidationError, match="must be user or assistant"):
        validate_contract(invalid_role)

    history_pii = copy.deepcopy(contract)
    history_pii["cases"][0]["history"] = [{"role": "user", "content": "My number is 0551234567."}]
    with pytest.raises(ContractValidationError, match="possible PII"):
        validate_contract(history_pii)
