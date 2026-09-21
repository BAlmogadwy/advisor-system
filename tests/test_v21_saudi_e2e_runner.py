from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.advisor.run_v21_saudi_e2e import (
    BudgetedLLMClient,
    BudgetState,
    BudgetStop,
    CaseContract,
    CorpusError,
    LiveLimits,
    _adviser_runtime_config_hmac_sha256,
    _artifact_hmac_sha256,
    _case_contract,
    _fixture_state_hmac_sha256,
    _FixtureQuerySpec,
    _git_worktree_identity,
    _load_resume,
    _new_artifact,
    _provider_config_hmac_sha256,
    _readiness_gate,
    _runtime_fingerprint_paths,
    _runtime_source_manifest,
    _runtime_source_sha256,
    _safe_control_values,
    _safe_plan_controls,
    _sanitize_text,
    _seal_artifact,
    _verify_artifact_hmac,
    category_aggregates,
    estimated_call_token_ceiling,
    load_corpus,
    main,
    score_plan,
)


@pytest.fixture
def corpus_path(tmp_path):
    path = tmp_path / "corpus.yaml"
    path.write_text(
        """
meta:
  name: tiny_saudi_corpus
grounding:
  fixture_student_id: 7654321
cases:
  - id: SA-TEST-001
    category_id: eligibility
    utterance_ar: "أقدر أضيف XXXX؟"
    grounded_utterance_ar: "أقدر أضيف IS362؟"
    contract:
      support: supported
      mode: execute
      expected_tools: [why_course_locked]
  - id: SA-TEST-002
    category_id: build
    grounded_utterance_ar: "ابنِ لي جدولاً خفيفاً"
    contract:
      support: read_only_partial
      expected_decisions: [execute]
      required_tools:
        any: [build_timetable_proposal, my_progress]
      allowed_tools: [build_timetable_proposal, my_progress]
      forbidden_tools: [policy_lookup]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _limits(**overrides):
    values = {
        "max_cases": 1,
        "max_provider_calls": 2,
        "max_total_tokens": 100_000,
        "max_tokens_per_call": 800,
        "timeout_seconds": 5.0,
        "max_wall_seconds": 30.0,
    }
    values.update(overrides)
    return LiveLimits(**values)


_RUNTIME_ENV = {
    "python_implementation": "CPython",
    "python_version": "3.11.0",
    "python_cache_tag": "cpython-311",
    "django_version": "5.2.17",
    "pyyaml_version": "6.0.3",
    "ortools_version": "9.15.6755",
    "sqlite_version": "3.45.0",
}
_ARTIFACT_TEST_SECRET = "artifact-test-secret-that-is-never-persisted"
_SOURCE_MANIFEST = [{"path": "core/test.py", "bytes": 1, "sha256": "a" * 64}]
_GIT_IDENTITY = {
    "available": True,
    "head": "b" * 40,
    "dirty_tracked": True,
    "has_untracked": True,
}


def test_frozen_corpus_loads_only_grounded_execution_projection():
    corpus = load_corpus(
        __import__("pathlib").Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml")
    )

    assert len(corpus.cases) == 108
    assert corpus.sha256 == "a4971174b1d498451f0c96a15b4326e3ed320f1c8afbd1131c79aa17f6a21941"
    assert corpus.execution_sha256 == (
        "b8adf5bf197e619d8bce646d9113e5e4569c9cb40e43e49351d7117d0eb8224b"
    )
    assert all("XXXX" not in case.question and "YYYY" not in case.question for case in corpus.cases)
    assert corpus.cases[0].question == "أقدر أضيف مقرر `IS362`؟"
    assert corpus.cases[0].contract.exact_tools == ("why_course_locked",)


def test_loader_accepts_records_json_and_rejects_live_placeholders(tmp_path):
    valid = tmp_path / "records.json"
    valid.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "case_id": "CASE-1",
                        "category": "test",
                        "question": "هل أقدر أخذ IS362؟",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert load_corpus(valid).cases[0].question.endswith("IS362؟")

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        "cases:\n  - {id: CASE-1, category: test, question: 'هل أقدر أخذ XXXX؟'}\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="placeholder"):
        load_corpus(invalid)

    pii = tmp_path / "pii.yaml"
    pii.write_text(
        "cases:\n  - {id: CASE-1, category: test, question: 'رقمي 7654321'}\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="identity-shaped"):
        load_corpus(pii)

    wildcard = tmp_path / "wildcard-alternative.yaml"
    wildcard.write_text(
        """
cases:
  - id: CASE-1
    category: test
    question: "هل أقدر أخذ IS362؟"
    contract:
      mode: execute
      requested_outcomes: [course_eligibility]
      expected_tools: [why_course_locked]
      acceptable_plans:
        - {allow_any_tool: true}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="unsupported keys"):
        load_corpus(wildcard)


def test_validate_only_is_zero_provider_call_and_discards_grounding_metadata(
    corpus_path, tmp_path, monkeypatch
):
    import evals.advisor.run_v21_saudi_e2e as runner

    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("provider/runtime must not load")),
    )
    output = tmp_path / "validation.json"

    assert main([str(corpus_path), "--output", str(output)]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    rendered = json.dumps(report)
    assert report["provider_calls"] == 0
    assert report["corpus"]["cases"] == 2
    assert "7654321" not in rendered
    assert "grounding" not in report


def test_live_confirmation_and_explicit_budgets_precede_runtime_setup(
    corpus_path, tmp_path, monkeypatch
):
    import evals.advisor.run_v21_saudi_e2e as runner

    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("safety gate must run first")),
    )
    with pytest.raises(SystemExit):
        main(
            [
                str(corpus_path),
                "--live",
                "--student-id",
                "7654321",
                "--model",
                "fixture-model",
                "--output",
                str(tmp_path / "run.json"),
            ]
        )


@pytest.mark.parametrize(
    "limits",
    [
        _limits(max_cases=0),
        _limits(max_provider_calls=0),
        _limits(max_total_tokens=0),
        _limits(max_tokens_per_call=127),
        _limits(timeout_seconds=61),
        _limits(max_wall_seconds=0),
    ],
)
def test_live_limits_are_positive_and_bounded(limits):
    with pytest.raises(ValueError):
        limits.validate(case_count=2)


def test_conservative_token_ceiling_includes_tools_and_completion():
    messages = [{"role": "user", "content": "أقدر أضيف IS362؟"}]
    tools = [{"type": "function", "function": {"name": "why_course_locked"}}]

    ceiling = estimated_call_token_ceiling(messages, tools=tools, max_tokens=800)

    assert ceiling > len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 800


class _BudgetInner:
    def __init__(self):
        self.config = SimpleNamespace(max_retries=0, max_tokens=1200)
        self.calls = 0
        self.kwargs = {}

    def resolve_model(self, requested=None):
        return requested

    def chat_with_tools(self, _messages, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return SimpleNamespace(
            usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
        )


def test_budget_reserves_and_checkpoints_before_single_no_retry_attempt():
    inner = _BudgetInner()
    budget = BudgetState(limits=_limits())
    snapshots = []
    client = BudgetedLLMClient(
        inner,
        budget,
        checkpoint=lambda: snapshots.append((budget.provider_calls, inner.calls)),
    )

    client.chat_with_tools(
        [{"role": "user", "content": "question"}],
        tools=[],
        model="fixture-model",
        max_tokens=2000,
        timeout_seconds=50,
    )

    assert snapshots[0] == (1, 0)
    assert snapshots[-1] == (1, 1)
    assert inner.calls == budget.provider_calls == budget.provider_responses == 1
    assert inner.kwargs["max_tokens"] == 800
    assert inner.kwargs["timeout_seconds"] <= 5.0
    assert budget.total_tokens == 13


def test_budget_stops_before_provider_when_conservative_ceiling_will_not_fit():
    inner = _BudgetInner()
    budget = BudgetState(limits=_limits(max_total_tokens=1))
    client = BudgetedLLMClient(inner, budget, checkpoint=lambda: None)

    with pytest.raises(BudgetStop, match="token budget"):
        client.chat_with_tools(
            [{"role": "user", "content": "question"}],
            tools=[],
            model="fixture-model",
        )

    assert budget.provider_calls == inner.calls == 0


def test_plan_scoring_covers_exact_and_flexible_contracts():
    exact = score_plan(
        CaseContract(
            support_level="supported",
            expected_decisions=("execute",),
            exact_tools=("my_progress",),
            required_all=("my_progress",),
            allowed_tools=("my_progress",),
        ),
        decision="execute",
        tools=["my_progress", "graduation_progress"],
    )
    flexible = score_plan(
        CaseContract(
            expected_decisions=("execute",),
            required_all=("my_progress",),
            required_any=(("my_timetable", "build_timetable_proposal"),),
            allowed_tools=("my_progress", "my_timetable", "build_timetable_proposal"),
            forbidden_tools=("policy_lookup",),
        ),
        decision="execute",
        tools=["my_progress", "my_timetable"],
    )

    assert exact["plan_exact"] is False
    assert exact["tool_minimality"] is False
    assert exact["passed"] is False
    assert flexible["checks"] == {
        "decision": True,
        "clarification_kind": True,
        "expected_outcomes": None,
        "outcome_coverage": None,
        "required_all": True,
        "required_any": True,
        "allowed_only": True,
        "forbidden_absent": True,
        "exact_tools": None,
        "required_controls": None,
    }
    assert flexible["passed"] is True


def test_saudi_contract_and_scorer_fail_closed_on_clarification_kind() -> None:
    with pytest.raises(CorpusError, match="clarification_kind"):
        _case_contract(
            {
                "contract": {
                    "mode": "clarify",
                    "requested_outcomes": ["timetable_build"],
                    "expected_tools": [],
                }
            }
        )
    with pytest.raises(CorpusError, match="clarify cannot share"):
        _case_contract(
            {
                "contract": {
                    "expected_decisions": ["execute", "clarify"],
                    "clarification_kind": "timetable_load",
                    "requested_outcomes": ["timetable_build"],
                    "expected_tools": [],
                }
            }
        )

    contract = CaseContract(
        expected_decisions=("clarify",),
        clarification_kind="timetable_load",
        expected_outcomes=("timetable_build",),
        exact_tools=(),
    )
    assert (
        score_plan(
            contract,
            decision="clarify",
            clarification_kind="",
            outcomes=["timetable_build"],
            tools=[],
        )["passed"]
        is False
    )
    assert (
        score_plan(
            contract,
            decision="clarify",
            clarification_kind="timetable_preference",
            outcomes=["timetable_build"],
            tools=[],
        )["passed"]
        is False
    )


def test_adjudicated_alternative_passes_without_erasing_canonical_diagnostics():
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_eligibility",),
        exact_tools=("why_course_locked",),
        required_all=("why_course_locked",),
        allowed_tools=("why_course_locked",),
        acceptable_plans=(
            CaseContract(
                expected_decisions=("execute",),
                expected_outcomes=("course_eligibility", "timetable_feasibility"),
                exact_tools=("why_course_locked", "my_clash_free_sections"),
                required_all=("why_course_locked", "my_clash_free_sections"),
                allowed_tools=("why_course_locked", "my_clash_free_sections"),
            ),
        ),
    )

    accepted = score_plan(
        contract,
        decision="execute",
        outcomes=["course_eligibility", "timetable_feasibility"],
        tools=["why_course_locked", "my_clash_free_sections"],
    )
    unreviewed = score_plan(
        contract,
        decision="execute",
        outcomes=["course_eligibility", "policy_rule"],
        tools=["why_course_locked", "policy_lookup"],
    )

    assert accepted["checks"]["expected_outcomes"] is False
    assert accepted["strict_canonical_passed"] is False
    assert accepted["matched_expectation"] == "alternative_1"
    assert accepted["acceptable_alternatives"][0]["passed"] is True
    assert accepted["passed"] is True
    assert unreviewed["matched_expectation"] == ""
    assert unreviewed["passed"] is False


def test_available_005_accepts_only_canonical_fit_or_adjudicated_balanced():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-AVAILABLE-005")

    def scored(objective: str):
        return score_plan(
            contract,
            decision="execute",
            outcomes=["course_addition"],
            tools=["recommend_feasible_course_addition"],
            controls={"recommend_feasible_course_addition": {"objective": objective}},
        )

    canonical = scored("timetable_fit")
    balanced = scored("balanced")
    unlock = scored("unlock_impact")
    faster = scored("faster_graduation")

    assert canonical["strict_canonical_passed"] is True
    assert canonical["matched_expectation"] == "canonical"
    assert balanced["strict_canonical_passed"] is False
    assert balanced["matched_expectation"] == "alternative_1"
    assert balanced["passed"] is True
    assert unlock["passed"] is False
    assert faster["passed"] is False


def test_accel_004_replacement_search_is_strict_canonical_only():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-ACCEL-004")
    common = {
        "decision": "execute",
        "tools": ["graduation_progress"],
        "controls": {
            "graduation_progress": {
                "planning_baseline_kind": "recommended_current_term",
                "search_better_replacements": True,
            }
        },
    }

    canonical = score_plan(
        contract,
        outcomes=["course_replacement"],
        **common,
    )
    impact_only = score_plan(
        contract,
        outcomes=["graduation_impact"],
        **common,
    )
    duplicated_criterion = score_plan(
        contract,
        outcomes=["course_replacement", "graduation_impact"],
        **common,
    )

    assert contract.acceptable_plans == ()
    assert canonical["strict_canonical_passed"] is True
    assert canonical["matched_expectation"] == "canonical"
    assert canonical["passed"] is True
    for rejected in (impact_only, duplicated_criterion):
        assert rejected["checks"]["expected_outcomes"] is False
        assert rejected["strict_canonical_passed"] is False
        assert rejected["matched_expectation"] == ""
        assert rejected["passed"] is False


@pytest.mark.parametrize(
    ("case_id", "observed_credit_load_policy"),
    [
        ("SA-REVIEW-010", "preserve"),
        ("SA-ACCEL-001", "within_policy"),
        ("SA-ACCEL-003", "preserve"),
        ("SA-ACCEL-006", "preserve"),
        ("SA-ACCEL-007", "within_policy"),
        ("SA-ACCEL-009", "within_policy"),
    ],
)
def test_graduation_criterion_timetable_reviews_are_strict_canonical_only(
    case_id,
    observed_credit_load_policy,
):
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == case_id)
    correct_controls = {
        "improve_current_timetable": {
            "objective": "faster_graduation",
            "credit_load_policy": observed_credit_load_policy,
            "allow_course_replacements": True,
        }
    }

    correct = score_plan(
        contract,
        decision="execute",
        outcomes=["timetable_review"],
        tools=["improve_current_timetable"],
        controls=correct_controls,
    )
    extra_outcome = score_plan(
        contract,
        decision="execute",
        outcomes=["timetable_review", "graduation_impact"],
        tools=["improve_current_timetable"],
        controls=correct_controls,
    )
    wrong_tool = score_plan(
        contract,
        decision="execute",
        outcomes=["timetable_review"],
        tools=["graduation_progress"],
        controls={},
    )
    wrong_objective = score_plan(
        contract,
        decision="execute",
        outcomes=["timetable_review"],
        tools=["improve_current_timetable"],
        controls={
            "improve_current_timetable": {
                "objective": "balanced",
                "allow_course_replacements": True,
            }
        },
    )
    replacements_disabled = score_plan(
        contract,
        decision="execute",
        outcomes=["timetable_review"],
        tools=["improve_current_timetable"],
        controls={
            "improve_current_timetable": {
                "objective": "faster_graduation",
                "allow_course_replacements": False,
            }
        },
    )

    assert contract.acceptable_plans == ()
    assert correct["strict_canonical_passed"] is True
    assert correct["matched_expectation"] == "canonical"
    assert correct["passed"] is True
    assert extra_outcome["passed"] is False
    assert wrong_tool["passed"] is False
    assert wrong_objective["passed"] is False
    assert replacements_disabled["passed"] is False


def test_whatif_010_graduation_impact_addition_probe_is_strict_canonical_only():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-WHATIF-010")
    correct_controls = {"recommend_feasible_course_addition": {"objective": "faster_graduation"}}

    correct = score_plan(
        contract,
        decision="execute",
        outcomes=["graduation_impact"],
        tools=["recommend_feasible_course_addition"],
        controls=correct_controls,
    )
    extra_outcome = score_plan(
        contract,
        decision="execute",
        outcomes=["course_addition", "graduation_impact"],
        tools=["recommend_feasible_course_addition"],
        controls=correct_controls,
    )
    wrong_tool = score_plan(
        contract,
        decision="execute",
        outcomes=["graduation_impact"],
        tools=["graduation_progress"],
        controls={
            "graduation_progress": {
                "planning_baseline_kind": "registered_timetable",
                "add_current_courses": ["DS341"],
            }
        },
    )
    wrong_objective = score_plan(
        contract,
        decision="execute",
        outcomes=["graduation_impact"],
        tools=["recommend_feasible_course_addition"],
        controls={"recommend_feasible_course_addition": {"objective": "balanced"}},
    )

    assert contract.acceptable_plans == ()
    assert correct["strict_canonical_passed"] is True
    assert correct["matched_expectation"] == "canonical"
    assert correct["passed"] is True
    assert extra_outcome["passed"] is False
    assert wrong_tool["passed"] is False
    assert wrong_objective["passed"] is False


def test_whatif_002_non_enrolment_requires_registered_removal_scenario():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-WHATIF-002")
    common = {
        "decision": "execute",
        "outcomes": ["graduation_impact"],
        "tools": ["graduation_progress"],
    }
    correct = score_plan(
        contract,
        **common,
        controls={
            "graduation_progress": {
                "planning_baseline_kind": "registered_timetable",
                "remove_current_courses": ["DS321"],
            }
        },
    )
    competing_interpretations = (
        {
            "planning_baseline_kind": "registered_timetable",
            "noncompletion_current_courses": ["DS321"],
        },
        {
            "planning_baseline_kind": "registered_timetable",
            "add_current_courses": ["DS321"],
        },
        {
            "planning_baseline_kind": "registered_timetable",
            "search_better_replacements": True,
        },
        {
            "planning_baseline_kind": "recommended_current_term",
            "remove_current_courses": ["DS321"],
        },
    )

    assert contract.required_controls == {
        "graduation_progress": {
            "planning_baseline_kind": "registered_timetable",
            "remove_current_courses": ["DS321"],
        }
    }
    assert correct["checks"]["required_controls"] is True
    assert correct["strict_canonical_passed"] is True
    assert correct["passed"] is True
    for observed in competing_interpretations:
        rejected = score_plan(
            contract,
            **common,
            controls={"graduation_progress": observed},
        )
        assert rejected["checks"]["required_controls"] is False
        assert rejected["strict_canonical_passed"] is False
        assert rejected["passed"] is False


def test_unambiguous_timetable_fit_canary_rejects_balanced_objective():
    canary = load_corpus(Path("evals/advisor/timetable_fit_semantic_canary.yaml"))
    assert len(canary.cases) == 1
    assert canary.sha256 == ("8586d364529affeaf2cdb78bda3ec95f3685f0feae8c9c48213e081266e34133")
    assert canary.execution_sha256 == (
        "954ce6a70d1c7bdcafc6fc55df9a616b273dfb2480a2d9cf064a0d841566bacf"
    )
    case = canary.cases[0]
    assert case.question == (
        "اختر لي المقرر الإضافي اللي عنده أكبر عدد من الشعب غير المتعارضة مع جدولي"
    )

    common = {
        "decision": "execute",
        "outcomes": ["course_addition"],
        "tools": ["recommend_feasible_course_addition"],
    }
    fit = score_plan(
        case.contract,
        **common,
        controls={"recommend_feasible_course_addition": {"objective": "timetable_fit"}},
    )
    balanced = score_plan(
        case.contract,
        **common,
        controls={"recommend_feasible_course_addition": {"objective": "balanced"}},
    )

    assert fit["strict_canonical_passed"] is True
    assert fit["passed"] is True
    assert balanced["strict_canonical_passed"] is False
    assert balanced["passed"] is False


def test_priority_top_five_scorer_rejects_wrong_missing_and_extra_controls():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-PRIORITY-010")
    common = {
        "decision": "execute",
        "outcomes": ["course_priority"],
        "tools": ["my_progress"],
    }

    correct = score_plan(
        contract,
        **common,
        controls={"my_progress": {"priority_limit": 5}},
    )
    wrong = score_plan(
        contract,
        **common,
        controls={"my_progress": {"priority_limit": 4}},
    )
    missing = score_plan(contract, **common, controls={"my_progress": {}})
    extra = score_plan(
        contract,
        **common,
        controls={
            "my_progress": {
                "priority_limit": 5,
                "unapproved_control": True,
            }
        },
    )

    assert correct["checks"]["required_controls"] is True
    assert correct["passed"] is True
    for rejected in (wrong, missing, extra):
        assert rejected["checks"]["required_controls"] is False
        assert rejected["passed"] is False


def test_composite_pinned_build_scorer_requires_every_literal_constraint():
    corpus = load_corpus(Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml"))
    contract = next(case.contract for case in corpus.cases if case.case_id == "SA-COMPOSITE-003")
    pin = {"course_code": "DS341", "section_label": "M2"}
    builder = {
        "mode": "from_scratch",
        "max_credits": 18,
        "must_take_courses": ["DS341"],
        "pinned_sections": [pin],
    }
    common = {
        "decision": "execute",
        "outcomes": ["timetable_build", "course_priority"],
        "tools": ["build_timetable_proposal", "my_progress"],
    }

    correct = score_plan(
        contract,
        **common,
        controls={"build_timetable_proposal": builder},
    )
    assert correct["checks"]["required_controls"] is True
    assert correct["passed"] is True

    for field in ("mode", "max_credits", "must_take_courses", "pinned_sections"):
        incomplete = dict(builder)
        incomplete.pop(field)
        rejected = score_plan(
            contract,
            **common,
            controls={"build_timetable_proposal": incomplete},
        )
        assert rejected["checks"]["required_controls"] is False
        assert rejected["passed"] is False


@pytest.mark.parametrize(
    "invalid",
    [True, 0, 21, "5"],
)
def test_priority_limit_safe_projection_matches_public_integer_bounds(invalid):
    valid = _safe_plan_controls(
        [
            {
                "name": "my_progress",
                "arguments": {
                    "priority_limit": 5,
                    "student_id": 7654321,
                    "unapproved_control": True,
                },
            }
        ]
    )
    rejected = _safe_plan_controls(
        [
            {
                "name": "my_progress",
                "arguments": {"priority_limit": invalid},
            }
        ]
    )

    assert valid == {"my_progress": {"priority_limit": 5}}
    assert rejected == {"my_progress": {}}


def test_grounded_adjacent_tool_does_not_cover_compound_addition_outcome():
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_addition",),
        exact_tools=("recommend_feasible_course_addition",),
        required_all=("recommend_feasible_course_addition",),
        allowed_tools=("recommend_feasible_course_addition",),
    )

    scored = score_plan(
        contract,
        decision="execute",
        outcomes=["course_addition"],
        # my_progress may return perfectly grounded open/priority rows, but it
        # does not own the joined feasible-addition conclusion.
        tools=["my_progress"],
    )

    assert scored["checks"]["expected_outcomes"] is True
    assert scored["checks"]["outcome_coverage"] is False
    assert scored["checks"]["exact_tools"] is False
    assert scored["passed"] is False


def test_compound_addition_owns_its_graduation_criterion_without_sibling_call():
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_addition", "graduation_impact"),
        exact_tools=("recommend_feasible_course_addition",),
    )

    covered = score_plan(
        contract,
        decision="execute",
        outcomes=["course_addition", "graduation_impact"],
        tools=["recommend_feasible_course_addition"],
    )
    redundant = score_plan(
        contract,
        decision="execute",
        outcomes=["course_addition", "graduation_impact"],
        tools=["recommend_feasible_course_addition", "graduation_progress"],
    )

    assert covered["checks"]["outcome_coverage"] is True
    assert covered["passed"] is True
    assert redundant["checks"]["outcome_coverage"] is False
    assert redundant["passed"] is False


@pytest.mark.parametrize(
    "boundary_outcome",
    ["registration_action", "credit_load_comparison"],
)
def test_execute_can_mix_server_owned_boundary_with_supported_analysis(
    boundary_outcome,
):
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_addition", boundary_outcome),
        exact_tools=("recommend_feasible_course_addition",),
    )

    scored = score_plan(
        contract,
        decision="execute",
        outcomes=["course_addition", boundary_outcome],
        tools=["recommend_feasible_course_addition"],
    )

    assert scored["checks"]["outcome_coverage"] is True
    assert scored["passed"] is True


def test_requested_outcome_order_is_not_a_scoring_failure():
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_addition", "registration_action"),
        exact_tools=("recommend_feasible_course_addition",),
    )

    scored = score_plan(
        contract,
        decision="execute",
        outcomes=["registration_action", "course_addition"],
        tools=["recommend_feasible_course_addition"],
    )

    assert scored["checks"]["expected_outcomes"] is True
    assert scored["checks"]["outcome_coverage"] is True
    assert scored["passed"] is True


def test_unsupported_is_typed_tool_free_and_rejects_mixed_supported_outcomes():
    contract = CaseContract(
        expected_decisions=("unsupported",),
        expected_outcomes=("unsupported_request",),
        exact_tools=(),
    )
    correct = score_plan(
        contract,
        decision="unsupported",
        outcomes=["unsupported_request"],
        tools=[],
    )
    legacy_direct = score_plan(
        contract,
        decision="direct",
        outcomes=["general_conversation"],
        tools=[],
    )
    mixed = score_plan(
        contract,
        decision="unsupported",
        outcomes=["unsupported_request", "course_addition"],
        tools=[],
    )

    assert correct["passed"] is True
    assert legacy_direct["passed"] is False
    assert mixed["checks"]["outcome_coverage"] is False
    assert mixed["passed"] is False


def test_critical_compound_controls_catch_wrong_but_well_formed_plan():
    contract = CaseContract(
        expected_decisions=("execute",),
        expected_outcomes=("course_addition",),
        exact_tools=("recommend_feasible_course_addition",),
        required_controls={
            "recommend_feasible_course_addition": {
                "objective": "balanced",
                "additional_credit_hours": 3,
            }
        },
    )
    common = {
        "decision": "execute",
        "outcomes": ["course_addition"],
        "tools": ["recommend_feasible_course_addition"],
    }

    correct = score_plan(
        contract,
        **common,
        controls={
            "recommend_feasible_course_addition": {
                "objective": "balanced",
                "additional_credit_hours": 3,
            }
        },
    )
    wrong = score_plan(
        contract,
        **common,
        controls={
            "recommend_feasible_course_addition": {
                "objective": "balanced",
                # A total timetable cap of 3 is not the requested three-hour addition.
            }
        },
    )

    assert correct["checks"]["required_controls"] is True
    assert correct["passed"] is True
    assert wrong["checks"]["required_controls"] is False
    assert wrong["passed"] is False


def test_control_projection_keeps_only_closed_identity_free_semantics():
    projected = _safe_plan_controls(
        [
            {
                "name": "recommend_feasible_course_addition",
                "arguments": {
                    "objective": "balanced",
                    "additional_credit_hours": 3,
                    "student_id": 7654321,
                    "free_text": "private",
                },
            }
        ]
    )

    assert projected == {
        "recommend_feasible_course_addition": {
            "objective": "balanced",
            "additional_credit_hours": 3,
        }
    }


def test_control_projection_keeps_closed_builder_and_compound_constraints():
    pin = {"course_code": "DS341", "section_label": "M2"}
    projected = _safe_plan_controls(
        [
            {
                "name": "build_timetable_proposal",
                "arguments": {
                    "mode": "from_scratch",
                    "max_credits": 18,
                    "target_credits": 18,
                    "must_take_courses": ["DS341"],
                    "pinned_sections": [pin],
                    "course_codes": ["SHOULD_NOT_PERSIST"],
                    "student_id": 7654321,
                },
            },
            {
                "name": "recommend_feasible_course_addition",
                "arguments": {
                    "objective": "balanced",
                    "max_credits": 18,
                    "pinned_sections": [pin],
                },
            },
            {
                "name": "rank_current_course_drop_impact",
                "arguments": {"objective": "balanced", "max_credits": 18},
            },
            {
                "name": "improve_current_timetable",
                "arguments": {
                    "objective": "balanced",
                    "max_credits": 18,
                },
            },
        ]
    )

    assert projected == {
        "build_timetable_proposal": {
            "mode": "from_scratch",
            "max_credits": 18,
            "target_credits": 18,
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        },
        "recommend_feasible_course_addition": {
            "objective": "balanced",
            "max_credits": 18,
            "pinned_sections": [pin],
        },
        "rank_current_course_drop_impact": {
            "objective": "balanced",
            "max_credits": 18,
        },
        "improve_current_timetable": {
            "objective": "balanced",
            "max_credits": 18,
        },
    }


@pytest.mark.parametrize("invalid", [True, 0, 100, "18"])
def test_max_credit_control_projection_is_a_bounded_integer(invalid):
    projected = _safe_plan_controls(
        [
            {
                "name": "build_timetable_proposal",
                "arguments": {"max_credits": invalid},
            }
        ]
    )

    assert projected == {"build_timetable_proposal": {}}


@pytest.mark.parametrize("invalid", [True, 0, 100, "18"])
def test_target_credit_control_projection_is_a_bounded_integer(invalid):
    projected = _safe_plan_controls(
        [
            {
                "name": "build_timetable_proposal",
                "arguments": {"target_credits": invalid},
            }
        ]
    )

    assert projected == {"build_timetable_proposal": {}}


@pytest.mark.parametrize(
    "controls",
    [
        {"mode": "invented_mode"},
        {"max_credits": True},
        {"max_credits": 100},
        {"must_take_courses": ["not-a-course"]},
        {"pinned_sections": [{"course_code": "DS341", "section_label": "M2", "raw_id": 9}]},
    ],
)
def test_builder_control_contract_rejects_open_or_malformed_values(controls):
    with pytest.raises(CorpusError):
        _safe_control_values(
            "build_timetable_proposal",
            controls,
            strict=True,
        )


def test_control_projection_keeps_typed_noncompletion_without_drop_aliasing():
    projected = _safe_plan_controls(
        [
            {
                "name": "graduation_progress",
                "arguments": {
                    "planning_baseline_kind": "registered_timetable",
                    "noncompletion_current_courses": ["DS332"],
                    "student_id": 7654321,
                },
            }
        ]
    )

    assert projected == {
        "graduation_progress": {
            "planning_baseline_kind": "registered_timetable",
            "noncompletion_current_courses": ["DS332"],
        }
    }


def test_answer_sanitizer_redacts_identity_contacts_but_keeps_academic_numbers():
    answer = _sanitize_text(
        "نورة 7654321 بريدها nora@example.com ورقمها 0551234567؛ الخطة 1448 وIS362.",
        student_id=7654321,
        student_name="نورة",
    )

    assert "نورة" not in answer
    assert "7654321" not in answer
    assert "nora@example.com" not in answer
    assert "0551234567" not in answer
    assert "1448" in answer and "IS362" in answer
    assert "[REDACTED_STUDENT_ID]" in _sanitize_text(
        "رقمي ٧ - ٦ - ٥ - ٤ - ٣ - ٢ - ١",
        student_id=7654321,
    )


def test_runtime_fingerprint_follows_lazy_transitive_local_imports(tmp_path, monkeypatch):
    import evals.advisor.run_v21_saudi_e2e as runner

    files = {
        "config/__init__.py": "",
        "config/settings.py": "VALUE = 1\n",
        "core/__init__.py": "",
        "core/services/__init__.py": "",
        "core/services/entry.py": (
            "def lazy():\n    from core.services import nested\n    return nested.VALUE\n"
        ),
        "core/services/nested.py": "from .deep import VALUE\n",
        "core/services/deep.py": "VALUE = 1\n",
        "core/services/unrelated.py": "VALUE = 'ignored'\n",
        "evals/__init__.py": "",
        "evals/advisor/__init__.py": "",
        "evals/advisor/runner.py": "from core.services.entry import lazy\n",
        "pyproject.toml": "[tool.fixture]\nversion = '1'\n",
        "requirements-dev.txt": "pytest==1\n",
        "requirements.txt": "Django==1\n",
    }
    for relative, payload in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(
        runner,
        "_RUNTIME_FINGERPRINT_ENTRYPOINTS",
        (
            "config/settings.py",
            "core/services/entry.py",
            "evals/advisor/runner.py",
        ),
    )
    relative_paths = {
        path.relative_to(tmp_path).as_posix() for path in _runtime_fingerprint_paths()
    }

    assert "core/services/deep.py" in relative_paths
    assert "core/services/unrelated.py" not in relative_paths
    manifest = _runtime_source_manifest()
    manifest_by_path = {row["path"]: row for row in manifest}
    deep_payload = (tmp_path / "core/services/deep.py").read_bytes()
    assert manifest_by_path["core/services/deep.py"] == {
        "path": "core/services/deep.py",
        "bytes": len(deep_payload),
        "sha256": hashlib.sha256(deep_payload).hexdigest(),
    }
    original = _runtime_source_sha256()
    (tmp_path / "core/services/deep.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _runtime_source_sha256() != original
    changed = _runtime_source_sha256()
    (tmp_path / "core/services/unrelated.py").write_text(
        "VALUE = 'still ignored'\n", encoding="utf-8"
    )
    assert _runtime_source_sha256() == changed
    (tmp_path / "requirements.txt").write_text("Django==2\n", encoding="utf-8")
    assert _runtime_source_sha256() != changed


def test_git_identity_records_only_commit_and_dirty_flags():
    identity = _git_worktree_identity()

    assert set(identity) == {
        "available",
        "head",
        "dirty_tracked",
        "has_untracked",
    }
    if identity["available"]:
        assert len(identity["head"]) in {40, 64}
        assert isinstance(identity["dirty_tracked"], bool)
        assert isinstance(identity["has_untracked"], bool)


def test_runtime_fingerprint_includes_top_n_contract_and_result_modules():
    paths = {path.resolve() for path in _runtime_fingerprint_paths()}
    expected = {
        Path("core/services/virtual_advisor_capabilities.py").resolve(),
        Path("core/services/student_advisor_v2.py").resolve(),
        Path("core/services/student_advisor_v21_prompt.py").resolve(),
        Path("core/services/answer_consistency.py").resolve(),
        Path("core/services/llm_remote_privacy.py").resolve(),
    }

    assert expected <= paths


def test_runtime_fingerprint_dependency_closure_is_hard_bounded(tmp_path, monkeypatch):
    import evals.advisor.run_v21_saudi_e2e as runner

    for relative, payload in {
        "core/__init__.py": "",
        "core/services/__init__.py": "",
        "core/services/entry.py": "from core.services import one, two\n",
        "core/services/one.py": "",
        "core/services/two.py": "",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "_RUNTIME_FINGERPRINT_ENTRYPOINTS", ("core/services/entry.py",))
    monkeypatch.setattr(runner, "_MAX_RUNTIME_FINGERPRINT_FILES", 3)

    with pytest.raises(ValueError, match="dependency closure is too large"):
        _runtime_fingerprint_paths()


class _FakeFixtureValues:
    def __init__(self, rows):
        self.rows = list(rows)

    def order_by(self, *_fields):
        self.rows.sort(key=lambda row: row[0])
        return self

    def distinct(self):
        self.rows = list(dict.fromkeys(self.rows))
        return self

    def iterator(self, *, chunk_size):
        assert chunk_size == 2000
        return iter(self.rows)


class _FakeFixtureQuery:
    def __init__(self, rows):
        fields = [SimpleNamespace(attname=name) for name in ("id", "name", "status")]
        self.model = SimpleNamespace(
            _meta=SimpleNamespace(
                concrete_fields=fields,
                pk=SimpleNamespace(attname="id"),
            )
        )
        self.rows = list(rows)

    def values_list(self, *fields):
        assert fields == ("id", "name", "status")
        return _FakeFixtureValues(self.rows)


def test_fixture_state_fingerprint_is_keyed_deterministic_and_drift_sensitive(tmp_path):
    import datetime as dt

    defaults = {
        "academic_year": 1448,
        "term": 1,
        "currentYear": 1448,
        "currentTerm": 1,
    }
    (tmp_path / "sources.yaml").write_text("private: policy-v1\n", encoding="utf-8")
    (tmp_path / "evidence_map.yaml").write_text("ignored: v1\n", encoding="utf-8")

    def fingerprint(rows):
        return _fixture_state_hmac_sha256(
            secret="fixture-secret",
            student_id=7654321,
            academic_year=1448,
            term=1,
            query_specs=[_FixtureQuerySpec("student", _FakeFixtureQuery(rows))],
            policy_root=tmp_path,
            site_defaults=defaults,
            as_of_date=dt.date(2026, 8, 24),
        )

    original = fingerprint([(7654321, "نورة الاختبار", "active")])
    assert original == fingerprint([(7654321, "نورة الاختبار", "active")])
    assert len(original) == 64
    assert "7654321" not in original and "نورة" not in original
    assert original != fingerprint([(7654321, "نورة الاختبار", "suspended")])

    (tmp_path / "evidence_map.yaml").write_text("ignored: v2\n", encoding="utf-8")
    assert original == fingerprint([(7654321, "نورة الاختبار", "active")])
    (tmp_path / "sources.yaml").write_text("private: policy-v2\n", encoding="utf-8")
    assert original != fingerprint([(7654321, "نورة الاختبار", "active")])


def test_artifact_hmac_covers_rows_plans_audits_usage_and_summary():
    artifact = {
        "schema_version": "fixture",
        "run": {"usage": {"provider_calls": 1}},
        "rows": [
            {
                "plan": {"passed": True},
                "evidence_audit": {"validation": {"outcome": "passed"}},
            }
        ],
        "summary": {"completed": 1},
    }
    _seal_artifact(artifact, secret=_ARTIFACT_TEST_SECRET, finalized=True)

    assert artifact["integrity"]["finalized"] is True
    assert _verify_artifact_hmac(_ARTIFACT_TEST_SECRET, artifact) is True
    assert (
        _artifact_hmac_sha256(_ARTIFACT_TEST_SECRET, artifact)
        == artifact["integrity"]["hmac_sha256"]
    )
    assert _ARTIFACT_TEST_SECRET not in json.dumps(artifact)

    mutations = (
        lambda value: value["run"]["usage"].update(provider_calls=2),
        lambda value: value["rows"][0]["plan"].update(passed=False),
        lambda value: value["rows"][0]["evidence_audit"]["validation"].update(outcome="abstained"),
        lambda value: value["summary"].update(completed=0),
    )
    for mutate in mutations:
        tampered = json.loads(json.dumps(artifact))
        mutate(tampered)
        assert _verify_artifact_hmac(_ARTIFACT_TEST_SECRET, tampered) is False


def test_readiness_gate_is_no_go_for_any_semantic_or_transport_failure():
    artifact = {
        "run": {
            "state": "complete",
            "runtime_source_stable": True,
            "fixture_state_stable": True,
        },
        "rows": [{"validation": {"outcome": "passed"}}],
    }
    summary = {
        "selected": 1,
        "recorded": 1,
        "completed": 1,
        "errors": 0,
        "plan_contract": {"passed": 1, "total": 1},
        "execution_incomplete": 0,
        "provider_errors": 0,
        "planner_contract_errors": 0,
    }

    assert _readiness_gate(artifact, summary)["status"] == "GO"
    for field in (
        "execution_incomplete",
        "provider_errors",
        "planner_contract_errors",
    ):
        failed = dict(summary)
        failed[field] = 1
        gate = _readiness_gate(artifact, failed)
        assert gate["status"] == "NO_GO"
    plan_failed = {**summary, "plan_contract": {"passed": 0, "total": 1}}
    assert _readiness_gate(artifact, plan_failed)["status"] == "NO_GO"


def test_readiness_gate_rejects_abstention_but_allows_not_applicable():
    summary = {
        "selected": 1,
        "recorded": 1,
        "completed": 1,
        "errors": 0,
        "plan_contract": {"passed": 1, "total": 1},
        "execution_incomplete": 0,
        "provider_errors": 0,
        "planner_contract_errors": 0,
    }
    run = {
        "state": "complete",
        "runtime_source_stable": True,
        "fixture_state_stable": True,
    }

    abstained = {
        "run": run,
        "rows": [{"validation": {"outcome": "abstained"}}],
    }
    gate = _readiness_gate(abstained, summary)
    assert gate["status"] == "NO_GO"
    assert gate["criteria"]["validation_abstention_free"] is False
    assert "validation_abstention_free" in gate["failed"]

    not_applicable = {
        "run": run,
        "rows": [{"validation": {"outcome": "not_applicable"}}],
    }
    gate = _readiness_gate(not_applicable, summary)
    assert gate["status"] == "GO"
    assert gate["criteria"]["validation_abstention_free"] is True


def test_resume_marks_reserved_in_progress_case_without_replaying(corpus_path, tmp_path):
    corpus = load_corpus(corpus_path)
    limits = _limits()
    selected = corpus.cases[:1]
    artifact = _new_artifact(
        corpus,
        selected,
        student_ref="a" * 64,
        backend="local",
        region="localhost",
        model="fixture-model",
        model_thinking_enabled=False,
        provider_config_hmac_sha256="d" * 64,
        adviser_runtime_config_hmac_sha256="e" * 64,
        database_vendor="sqlite",
        runtime_environment=_RUNTIME_ENV,
        prompt_version="fixture-v7",
        runtime_source_sha256="b" * 64,
        fixture_state_hmac_sha256="c" * 64,
        limits=limits,
        academic_year=1448,
        term=1,
        runtime_source_manifest=_SOURCE_MANIFEST,
        git_worktree_identity=_GIT_IDENTITY,
        artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
    )
    artifact["run"]["current_case_id"] = selected[0].case_id
    artifact["run"]["usage"] = {
        "provider_calls": 1,
        "provider_responses": 0,
        "committed_token_ceiling": 5000,
    }
    _seal_artifact(artifact, secret=_ARTIFACT_TEST_SECRET, finalized=False)
    output = tmp_path / "resume.json"
    output.write_text(json.dumps(artifact), encoding="utf-8")

    resumed = _load_resume(
        output,
        corpus=corpus,
        selected=selected,
        student_ref="a" * 64,
        backend="local",
        region="localhost",
        model="fixture-model",
        model_thinking_enabled=False,
        provider_config_hmac_sha256="d" * 64,
        adviser_runtime_config_hmac_sha256="e" * 64,
        database_vendor="sqlite",
        runtime_environment=_RUNTIME_ENV,
        prompt_version="fixture-v7",
        runtime_source_sha256="b" * 64,
        fixture_state_hmac_sha256="c" * 64,
        limits=limits,
        academic_year=1448,
        term=1,
        runtime_source_manifest=_SOURCE_MANIFEST,
        artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
    )

    assert resumed["rows"][0]["status"] == "interrupted"
    assert resumed["rows"][0]["error_category"] == "InterruptedPreviousRun"
    assert resumed["run"]["usage"]["provider_calls"] == 1
    assert resumed["run"]["current_case_id"] == ""


def test_resume_rejects_unknown_or_duplicate_rows(corpus_path, tmp_path):
    corpus = load_corpus(corpus_path)
    limits = _limits()
    selected = corpus.cases[:1]
    artifact = _new_artifact(
        corpus,
        selected,
        student_ref="a" * 64,
        backend="local",
        region="localhost",
        model="fixture-model",
        model_thinking_enabled=False,
        provider_config_hmac_sha256="d" * 64,
        adviser_runtime_config_hmac_sha256="e" * 64,
        database_vendor="sqlite",
        runtime_environment=_RUNTIME_ENV,
        prompt_version="fixture-v7",
        runtime_source_sha256="b" * 64,
        fixture_state_hmac_sha256="c" * 64,
        limits=limits,
        academic_year=None,
        term=None,
        runtime_source_manifest=_SOURCE_MANIFEST,
        git_worktree_identity=_GIT_IDENTITY,
        artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
    )
    artifact["rows"] = [{"case_id": "NOT-SELECTED"}]
    _seal_artifact(artifact, secret=_ARTIFACT_TEST_SECRET, finalized=False)
    output = tmp_path / "unsafe-resume.json"
    output.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown or duplicate"):
        _load_resume(
            output,
            corpus=corpus,
            selected=selected,
            student_ref="a" * 64,
            backend="local",
            region="localhost",
            model="fixture-model",
            model_thinking_enabled=False,
            provider_config_hmac_sha256="d" * 64,
            adviser_runtime_config_hmac_sha256="e" * 64,
            database_vendor="sqlite",
            runtime_environment=_RUNTIME_ENV,
            prompt_version="fixture-v7",
            runtime_source_sha256="b" * 64,
            fixture_state_hmac_sha256="c" * 64,
            limits=limits,
            academic_year=None,
            term=None,
            runtime_source_manifest=_SOURCE_MANIFEST,
            artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
        )


def test_resume_rejects_prompt_or_runtime_source_drift(corpus_path, tmp_path):
    corpus = load_corpus(corpus_path)
    limits = _limits()
    selected = corpus.cases[:1]
    artifact = _new_artifact(
        corpus,
        selected,
        student_ref="a" * 64,
        backend="local",
        region="localhost",
        model="fixture-model",
        model_thinking_enabled=False,
        provider_config_hmac_sha256="d" * 64,
        adviser_runtime_config_hmac_sha256="e" * 64,
        database_vendor="sqlite",
        runtime_environment=_RUNTIME_ENV,
        prompt_version="fixture-v7",
        runtime_source_sha256="b" * 64,
        fixture_state_hmac_sha256="c" * 64,
        limits=limits,
        academic_year=1448,
        term=1,
        runtime_source_manifest=_SOURCE_MANIFEST,
        git_worktree_identity=_GIT_IDENTITY,
        artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
    )
    output = tmp_path / "drifted-resume.json"
    output.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="prompt_version, runtime_source_sha256"):
        _load_resume(
            output,
            corpus=corpus,
            selected=selected,
            student_ref="a" * 64,
            backend="local",
            region="localhost",
            model="fixture-model",
            model_thinking_enabled=False,
            provider_config_hmac_sha256="d" * 64,
            adviser_runtime_config_hmac_sha256="e" * 64,
            database_vendor="sqlite",
            runtime_environment=_RUNTIME_ENV,
            prompt_version="fixture-v8",
            runtime_source_sha256="c" * 64,
            fixture_state_hmac_sha256="c" * 64,
            limits=limits,
            academic_year=1448,
            term=1,
            runtime_source_manifest=_SOURCE_MANIFEST,
            artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
        )


def test_resume_rejects_provider_region_or_fixture_state_drift(corpus_path, tmp_path):
    corpus = load_corpus(corpus_path)
    limits = _limits()
    selected = corpus.cases[:1]
    artifact = _new_artifact(
        corpus,
        selected,
        student_ref="a" * 64,
        backend="alibaba",
        region="international",
        model="fixture-model",
        model_thinking_enabled=False,
        provider_config_hmac_sha256="d" * 64,
        adviser_runtime_config_hmac_sha256="e" * 64,
        database_vendor="sqlite",
        runtime_environment=_RUNTIME_ENV,
        prompt_version="fixture-v7",
        runtime_source_sha256="b" * 64,
        fixture_state_hmac_sha256="c" * 64,
        limits=limits,
        academic_year=1448,
        term=1,
        runtime_source_manifest=_SOURCE_MANIFEST,
        git_worktree_identity=_GIT_IDENTITY,
        artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
    )
    output = tmp_path / "provider-state-drift.json"
    output.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=(
            "region, model_thinking_enabled, provider_config_hmac_sha256, "
            "adviser_runtime_config_hmac_sha256, database_vendor, "
            "runtime_environment, fixture_state_hmac_sha256"
        ),
    ):
        _load_resume(
            output,
            corpus=corpus,
            selected=selected,
            student_ref="a" * 64,
            backend="alibaba",
            region="cn-beijing",
            model="fixture-model",
            model_thinking_enabled=True,
            provider_config_hmac_sha256="f" * 64,
            adviser_runtime_config_hmac_sha256="g" * 64,
            database_vendor="postgresql",
            runtime_environment={**_RUNTIME_ENV, "python_version": "3.12.0"},
            prompt_version="fixture-v7",
            runtime_source_sha256="b" * 64,
            fixture_state_hmac_sha256="d" * 64,
            limits=limits,
            academic_year=1448,
            term=1,
            runtime_source_manifest=_SOURCE_MANIFEST,
            artifact_hmac_secret=_ARTIFACT_TEST_SECRET,
        )


@dataclasses.dataclass(frozen=True)
class _FakeConfig:
    backend: str = "local"
    provider: str = "fixture-provider"
    base_url: str = "http://localhost:1234/v1"
    model: str = "fixture-default"
    max_retries: int = 2
    max_tokens: int = 2000
    timeout_seconds: float = 75.0
    api_key: str = "fixture-api-key"
    enable_thinking: bool = False
    supports_assistant_prefill: bool = True
    allow_model_discovery: bool = True
    provider_options: dict = dataclasses.field(default_factory=dict)
    region: str = "localhost"


def test_provider_config_fingerprint_excludes_key_but_covers_effective_behavior():
    common = {
        "secret": "s" * 32,
        "requested_model": "fixture-model",
    }
    baseline = _provider_config_hmac_sha256(config=_FakeConfig(), **common)

    assert baseline == _provider_config_hmac_sha256(
        config=dataclasses.replace(_FakeConfig(), api_key="rotated-secret"),
        **common,
    )
    assert baseline != _provider_config_hmac_sha256(
        config=dataclasses.replace(_FakeConfig(), enable_thinking=True),
        **common,
    )
    assert baseline != _provider_config_hmac_sha256(
        config=dataclasses.replace(_FakeConfig(), provider_options={"response_format": "json"}),
        **common,
    )


def test_adviser_runtime_fingerprint_covers_effective_env_ceilings():
    settings = SimpleNamespace(
        STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS=900,
        STUDENT_ADVISOR_V21_PLAN_TIMEOUT_SECONDS=45,
        STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS=4,
        STUDENT_ADVISOR_V2_MAX_TOOL_CALLS=8,
        STUDENT_ADVISOR_V2_MAX_TOKENS=1800,
        STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS=75,
        STUDENT_ADVISOR_V2_TURN_BUDGET_SECONDS=90,
    )
    baseline = _adviser_runtime_config_hmac_sha256(
        secret="s" * 32, settings=settings, limits=_limits()
    )
    changed = SimpleNamespace(**vars(settings))
    changed.STUDENT_ADVISOR_V2_MAX_TOOL_CALLS = 7

    assert baseline != _adviser_runtime_config_hmac_sha256(
        secret="s" * 32, settings=changed, limits=_limits()
    )


class _FakeLiveClient:
    backend = "local"

    def __init__(self):
        self.config = _FakeConfig()
        self.calls = 0

    def resolve_model(self, requested=None):
        assert requested
        return requested

    def chat(self, _messages, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        )


class _FakeQuery:
    def filter(self, **kwargs):
        assert kwargs == {"student_id": 7654321}
        return self

    def values(self, *fields):
        assert fields == ("name",)
        return self

    def first(self):
        return {"name": "نورة الاختبار"}


class _FakeStudent:
    objects = _FakeQuery()


class _FakePrincipal:
    def __init__(self, *, role, student_id):
        self.role = role
        self.student_id = student_id


def _live_cli(corpus_path, output):
    return [
        str(corpus_path),
        "--live",
        "--confirm-live-external-request",
        "--student-id",
        "7654321",
        "--model",
        "fixture-model",
        "--academic-year",
        "1448",
        "--term",
        "1",
        "--max-cases",
        "1",
        "--max-provider-calls",
        "2",
        "--max-total-tokens",
        "100000",
        "--max-tokens-per-call",
        "800",
        "--timeout-seconds",
        "5",
        "--max-wall-seconds",
        "30",
        "--output",
        str(output),
    ]


def test_live_requires_private_fingerprint_key_before_runtime_setup(
    corpus_path, tmp_path, monkeypatch
):
    import evals.advisor.run_v21_saudi_e2e as runner

    monkeypatch.delenv("V21_EVAL_FINGERPRINT_KEY", raising=False)
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("runtime must not load")),
    )

    with pytest.raises(SystemExit):
        main(_live_cli(corpus_path, tmp_path / "missing-key.json"))


def test_live_main_calls_real_seam_without_history_and_saves_only_sanitized_audit(
    corpus_path, tmp_path, monkeypatch
):
    import evals.advisor.run_v21_saudi_e2e as runner

    monkeypatch.setenv("V21_EVAL_FINGERPRINT_KEY", "f" * 32)
    client = _FakeLiveClient()
    seen = {}

    @contextlib.contextmanager
    def fake_override_settings(**_kwargs):
        yield

    def fake_answer(**kwargs):
        seen.update(kwargs)
        turn = kwargs["llm_client"].chat(
            [{"role": "user", "content": kwargs["question"]}],
            model=kwargs["model"],
            max_tokens=1200,
            timeout_seconds=40,
        )
        return {
            "answer": "نورة الاختبار 7654321: يمكنك أخذ IS362. nora@example.com",
            "model": "fixture-model",
            "usage": turn.usage,
            "tool_results": [{"tool": "secret", "student_id": 7654321}],
            "agent": {
                "semantic_plan_decision": "execute",
                "semantic_plan_clarification_kind": "none",
                "semantic_plan_tools": ["why_course_locked"],
                "semantic_plan_execution_complete": True,
                "tools_called": [
                    {
                        "name": "why_course_locked",
                        "arguments": {"course_code": "IS362"},
                    }
                ],
                "model_revision": "revision-1",
                "evidence_audit": {
                    "schema_version": "1",
                    "evidence_hashes": [{"tool": "why_course_locked", "sha256": "a" * 64}],
                    "validation": {
                        "outcome": "passed",
                        "violations": [],
                        "violations_after_repair": [],
                    },
                    "repair": {"attempted": False},
                    "flags": {
                        "turn_budget_exhausted": False,
                        "provider_error": "",
                    },
                    "cost": {
                        "inference_calls": 1,
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "turn_ms": 15,
                    },
                },
            },
        }

    settings = SimpleNamespace(
        SECRET_KEY="test-secret",
        ALIBABA_LLM_ALLOW_LIVE_REQUESTS=False,
        STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS=900,
        STUDENT_ADVISOR_V2_MAX_TOKENS=1800,
        STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS=75,
    )
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: {
            "settings": settings,
            "database_vendor": "sqlite",
            "override_settings": fake_override_settings,
            "Student": _FakeStudent,
            "AdvisorPrincipal": _FakePrincipal,
            "ROLE_STUDENT": "student",
            "get_llm_client": lambda: client,
            "fixture_state_fingerprint": lambda **_kwargs: "e" * 64,
            "answer": fake_answer,
        },
    )
    output = tmp_path / "live.json"

    exit_code = main(_live_cli(corpus_path, output))

    assert exit_code == 0
    assert seen["history"] is None
    assert seen["prior_presentation"] is None
    assert "conversation" not in seen
    artifact = json.loads(output.read_text(encoding="utf-8"))
    rendered = json.dumps(artifact, ensure_ascii=False)
    assert artifact["run"]["conversation_persistence"] is False
    assert artifact["run"]["raw_evidence_persisted"] is False
    assert artifact["run"]["provider_retries"] == 0
    assert artifact["run"]["prompt_version"] == "unknown"
    assert artifact["run"]["model_thinking_enabled"] is False
    assert artifact["run"]["database_vendor"] == "sqlite"
    assert artifact["run"]["deterministic_replay"] is False
    assert set(artifact["run"]["runtime_environment"]) == set(_RUNTIME_ENV)
    assert len(artifact["run"]["provider_config_hmac_sha256"]) == 64
    assert len(artifact["run"]["adviser_runtime_config_hmac_sha256"]) == 64
    assert len(artifact["run"]["runtime_source_sha256"]) == 64
    assert artifact["run"]["runtime_source_manifest"]
    assert (
        artifact["run"]["runtime_source_end_manifest"] == artifact["run"]["runtime_source_manifest"]
    )
    assert set(artifact["run"]["git_worktree"]) == {
        "available",
        "head",
        "dirty_tracked",
        "has_untracked",
    }
    assert len(artifact["run"]["fixture_state_hmac_sha256"]) == 64
    assert artifact["run"]["runtime_source_stable"] is True
    assert artifact["run"]["fixture_state_stable"] is True
    assert artifact["run"]["usage"]["provider_calls"] == 1
    assert artifact["rows"][0]["plan"]["plan_exact"] is True
    assert artifact["rows"][0]["validation"]["outcome"] == "passed"
    assert artifact["summary"]["validation_outcomes"] == {"passed": 1}
    assert artifact["summary"]["categories"]["eligibility"]["plan_contract"]["rate"] == 1.0
    assert artifact["summary"]["readiness"]["status"] == "GO"
    assert artifact["integrity"]["finalized"] is True
    assert _verify_artifact_hmac("f" * 32, artifact) is True
    assert "7654321" not in rendered
    assert "نورة الاختبار" not in rendered
    assert "nora@example.com" not in rendered
    assert "tool_results" not in rendered
    assert '"arguments"' not in rendered
    assert "f" * 32 not in rendered
    assert "fixture-api-key" not in rendered
    assert "http://localhost:1234/v1" not in rendered

    # A completed checkpoint resumes as a no-op and cannot spend the case twice.
    assert main([*_live_cli(corpus_path, output), "--resume"]) == 0
    assert client.calls == 1


def test_live_marks_midrun_fixture_drift_and_refuses_resume(corpus_path, tmp_path, monkeypatch):
    import evals.advisor.run_v21_saudi_e2e as runner

    monkeypatch.setenv("V21_EVAL_FINGERPRINT_KEY", "f" * 32)
    sources = iter(
        (
            {"sha256": "b" * 64, "files": _SOURCE_MANIFEST},
            {"sha256": "d" * 64, "files": _SOURCE_MANIFEST},
        )
    )
    monkeypatch.setattr(runner, "_runtime_source_provenance", lambda: next(sources))
    states = iter(("a" * 64, "c" * 64))
    client = _FakeLiveClient()

    @contextlib.contextmanager
    def fake_override_settings(**_kwargs):
        yield

    def fake_answer(**_kwargs):
        return {
            "answer": "يمكنك أخذ IS362.",
            "model": "fixture-model",
            "usage": {},
            "agent": {
                "semantic_plan_decision": "execute",
                "semantic_plan_clarification_kind": "none",
                "semantic_plan_tools": ["why_course_locked"],
                "semantic_plan_execution_complete": True,
                "tools_called": [{"name": "why_course_locked", "arguments": {}}],
                "evidence_audit": {
                    "validation": {"outcome": "passed", "violations": []},
                    "flags": {},
                    "cost": {},
                },
            },
        }

    settings = SimpleNamespace(
        ALIBABA_LLM_ALLOW_LIVE_REQUESTS=False,
        STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS=900,
        STUDENT_ADVISOR_V2_MAX_TOKENS=1800,
        STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS=75,
    )
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: {
            "settings": settings,
            "database_vendor": "sqlite",
            "override_settings": fake_override_settings,
            "Student": _FakeStudent,
            "AdvisorPrincipal": _FakePrincipal,
            "ROLE_STUDENT": "student",
            "get_llm_client": lambda: client,
            "fixture_state_fingerprint": lambda **_kwargs: next(states),
            "answer": fake_answer,
        },
    )
    output = tmp_path / "state-drift.json"

    assert main(_live_cli(corpus_path, output)) == 1

    artifact = json.loads(output.read_text(encoding="utf-8"))
    run = artifact["run"]
    assert run["state"] == "state_drift"
    assert run["fixture_state_hmac_sha256"] == "a" * 64
    assert run["fixture_state_end_hmac_sha256"] == "c" * 64
    assert run["fixture_state_stable"] is False
    assert run["runtime_source_end_sha256"] == "d" * 64
    assert run["runtime_source_stable"] is False
    assert "changed during collection" in run["stopped_for"]
    assert artifact["summary"]["readiness"]["status"] == "NO_GO"
    assert _verify_artifact_hmac("f" * 32, artifact) is True

    corpus = load_corpus(corpus_path)
    with pytest.raises(ValueError, match="runtime provenance drift"):
        _load_resume(
            output,
            corpus=corpus,
            selected=corpus.cases[:1],
            student_ref=run["student_ref"],
            backend=run["backend"],
            region=run["region"],
            model=run["model"],
            model_thinking_enabled=run["model_thinking_enabled"],
            provider_config_hmac_sha256=run["provider_config_hmac_sha256"],
            adviser_runtime_config_hmac_sha256=run["adviser_runtime_config_hmac_sha256"],
            database_vendor=run["database_vendor"],
            runtime_environment=run["runtime_environment"],
            prompt_version=run["prompt_version"],
            runtime_source_sha256=run["runtime_source_sha256"],
            fixture_state_hmac_sha256=run["fixture_state_end_hmac_sha256"],
            limits=_limits(),
            academic_year=1448,
            term=1,
            runtime_source_manifest=run["runtime_source_manifest"],
            artifact_hmac_secret="f" * 32,
        )


def test_live_provider_failure_retains_only_class_and_reserved_cost(
    corpus_path, tmp_path, monkeypatch
):
    import evals.advisor.run_v21_saudi_e2e as runner

    class SecretProviderFailure(RuntimeError):
        pass

    monkeypatch.setenv("V21_EVAL_FINGERPRINT_KEY", "f" * 32)
    client = _FakeLiveClient()

    def failing_chat(_messages, **_kwargs):
        client.calls += 1
        raise SecretProviderFailure("private provider payload and student facts")

    client.chat = failing_chat

    @contextlib.contextmanager
    def fake_override_settings(**_kwargs):
        yield

    def fake_answer(**kwargs):
        kwargs["llm_client"].chat(
            [{"role": "user", "content": kwargs["question"]}],
            model=kwargs["model"],
        )
        raise AssertionError("unreachable")

    settings = SimpleNamespace(
        SECRET_KEY="test-secret",
        ALIBABA_LLM_ALLOW_LIVE_REQUESTS=False,
        STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS=900,
        STUDENT_ADVISOR_V2_MAX_TOKENS=1800,
        STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS=75,
    )
    monkeypatch.setattr(
        runner,
        "_runtime_dependencies",
        lambda: {
            "settings": settings,
            "database_vendor": "sqlite",
            "override_settings": fake_override_settings,
            "Student": _FakeStudent,
            "AdvisorPrincipal": _FakePrincipal,
            "ROLE_STUDENT": "student",
            "get_llm_client": lambda: client,
            "fixture_state_fingerprint": lambda **_kwargs: "e" * 64,
            "answer": fake_answer,
        },
    )
    output = tmp_path / "failed-live.json"

    assert main(_live_cli(corpus_path, output)) == 1

    artifact = json.loads(output.read_text(encoding="utf-8"))
    rendered = json.dumps(artifact, ensure_ascii=False)
    row = artifact["rows"][0]
    assert row["status"] == "error"
    assert row["error_category"] == "SecretProviderFailure"
    assert row["provider_calls"] == 1
    assert row["committed_token_ceiling"] > 0
    assert artifact["run"]["usage"]["provider_calls"] == 1
    assert artifact["summary"]["readiness"]["status"] == "NO_GO"
    assert artifact["integrity"]["finalized"] is True
    assert _verify_artifact_hmac("f" * 32, artifact) is True
    assert "private provider payload" not in rendered
    assert "student facts" not in rendered


def test_category_aggregate_reports_plan_validation_and_provider_errors():
    rows = [
        {
            "category": "drop",
            "status": "completed",
            "plan": {"passed": True},
            "validation": {"outcome": "passed"},
            "provider_error": "",
            "planner_contract_error": "plan_validation_failed",
        },
        {
            "category": "drop",
            "status": "error",
            "plan": {"passed": False},
            "validation": {},
            "provider_error": "LLMTimeout",
            "planner_contract_error": "",
        },
    ]

    aggregate = category_aggregates(rows)["drop"]

    assert aggregate["plan_contract"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert aggregate["validation_outcomes"] == {"passed": 1, "unavailable": 1}
    assert aggregate["provider_errors"] == 1
    assert aggregate["planner_contract_errors"] == 1
