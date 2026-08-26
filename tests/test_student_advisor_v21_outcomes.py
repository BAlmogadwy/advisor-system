from __future__ import annotations

import pytest

from core.services.student_advisor_v21_outcomes import (
    evaluate_outcome_coverage,
    minimise_redundant_capabilities,
)
from core.services.student_advisor_v21_plan import (
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
)


def _plan(
    decision: TurnPlanDecision,
    outcomes: list[StudentRequestOutcome],
    *tools: str,
) -> StudentTurnPlan:
    return StudentTurnPlan(
        decision=decision,
        requested_outcomes=tuple(outcomes),
        evidence_requests=tuple(
            PlannedCapabilityCall(capability=tool, arguments={}) for tool in tools
        ),
    )


def test_each_compound_outcome_requires_its_own_typed_capability():
    cases = (
        (
            StudentRequestOutcome.COURSE_ADDITION,
            "recommend_feasible_course_addition",
            "my_progress",
        ),
        (
            StudentRequestOutcome.COURSE_DROP_IMPACT,
            "rank_current_course_drop_impact",
            "graduation_progress",
        ),
        (
            StudentRequestOutcome.TIMETABLE_REVIEW,
            "improve_current_timetable",
            "my_timetable",
        ),
    )
    for outcome, compound, adjacent_fact_tool in cases:
        valid = evaluate_outcome_coverage(_plan(TurnPlanDecision.EXECUTE, [outcome], compound))
        wrong = evaluate_outcome_coverage(
            _plan(TurnPlanDecision.EXECUTE, [outcome], adjacent_fact_tool)
        )
        assert valid.valid is True
        assert wrong.valid is False
        assert wrong.uncovered_outcomes == (outcome,)


def test_execute_requires_every_requested_outcome_and_rejects_extra_tools():
    incomplete = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.CURRENT_TIMETABLE,
                StudentRequestOutcome.GRADUATION_FORECAST,
            ],
            "my_timetable",
        )
    )
    redundant = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [StudentRequestOutcome.CURRENT_TIMETABLE],
            "my_timetable",
            "my_progress",
        )
    )

    assert incomplete.valid is False
    assert incomplete.reason == "requested_outcome_uncovered"
    assert incomplete.uncovered_outcomes == (StudentRequestOutcome.GRADUATION_FORECAST,)
    assert redundant.valid is False
    assert redundant.reason == "unnecessary_capability"
    assert redundant.redundant_capabilities == ("my_progress",)


def test_a_proven_redundant_capability_can_be_minimised_without_losing_coverage():
    plan = _plan(
        TurnPlanDecision.EXECUTE,
        [StudentRequestOutcome.TIMETABLE_REVIEW],
        "my_timetable",
        "improve_current_timetable",
    )
    initial = evaluate_outcome_coverage(plan)

    minimised, report, removed = minimise_redundant_capabilities(
        plan,
        report=initial,
    )

    assert removed == ("my_timetable",)
    assert tuple(call.capability for call in minimised.evidence_requests) == (
        "improve_current_timetable",
    )
    assert report.valid is True


def test_minimisation_never_fills_or_hides_an_uncovered_outcome():
    plan = _plan(
        TurnPlanDecision.EXECUTE,
        [
            StudentRequestOutcome.CURRENT_TIMETABLE,
            StudentRequestOutcome.GRADUATION_FORECAST,
        ],
        "my_timetable",
    )
    initial = evaluate_outcome_coverage(plan)

    unchanged, report, removed = minimise_redundant_capabilities(
        plan,
        report=initial,
    )

    assert unchanged is plan
    assert report is initial
    assert removed == ()
    assert report.reason == "requested_outcome_uncovered"


def test_mixed_execute_is_valid_when_each_tool_has_a_requested_owner():
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.CURRENT_TIMETABLE,
                StudentRequestOutcome.GRADUATION_FORECAST,
            ],
            "my_timetable",
            "graduation_progress",
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()
    assert report.redundant_capabilities == ()


@pytest.mark.parametrize(
    ("primary", "compound"),
    (
        (
            StudentRequestOutcome.COURSE_ADDITION,
            "recommend_feasible_course_addition",
        ),
        (
            StudentRequestOutcome.COURSE_DROP_IMPACT,
            "rank_current_course_drop_impact",
        ),
        (
            StudentRequestOutcome.TIMETABLE_REVIEW,
            "improve_current_timetable",
        ),
    ),
)
def test_compound_primary_outcome_subsumes_its_graduation_criterion(
    primary: StudentRequestOutcome,
    compound: str,
) -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [primary, StudentRequestOutcome.GRADUATION_IMPACT],
            compound,
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


@pytest.mark.parametrize(
    "compound",
    (
        "recommend_feasible_course_addition",
        "rank_current_course_drop_impact",
        "feasible_course_replacements",
        "improve_current_timetable",
    ),
)
def test_compound_can_directly_own_the_graduation_impact_deliverable(
    compound: str,
) -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [StudentRequestOutcome.GRADUATION_IMPACT],
            compound,
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


def test_baseline_graduation_forecast_cannot_masquerade_as_change_impact() -> None:
    baseline = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
        evidence_requests=(
            PlannedCapabilityCall(
                capability="graduation_progress",
                arguments={"planning_baseline_kind": "registered_timetable"},
            ),
        ),
    )
    concrete = StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
        evidence_requests=(
            PlannedCapabilityCall(
                capability="graduation_progress",
                arguments={
                    "planning_baseline_kind": "registered_timetable",
                    "remove_current_courses": ["DS341"],
                },
            ),
        ),
    )

    baseline_report = evaluate_outcome_coverage(baseline)
    concrete_report = evaluate_outcome_coverage(concrete)

    assert baseline_report.valid is False
    assert baseline_report.reason == "requested_outcome_uncovered"
    assert concrete_report.valid is True


def test_registered_noncompletion_scenario_owns_graduation_impact() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={
                        "planning_baseline_kind": "registered_timetable",
                        "noncompletion_current_courses": ["DS341"],
                    },
                ),
            ),
        ),
        explicit_course_codes=("DS341",),
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "planning_baseline_kind": "recommended_current_term",
            "noncompletion_current_courses": ["DS341"],
        },
        {
            "planning_baseline_kind": "registered_timetable",
            "noncompletion_current_courses": ["DS341"],
            "remove_current_courses": ["DS342"],
        },
        {
            "planning_baseline_kind": "registered_timetable",
            "noncompletion_current_courses": ["DS341"],
            "add_current_courses": ["DS342"],
        },
        {
            "planning_baseline_kind": "registered_timetable",
            "noncompletion_current_courses": ["DS341"],
            "search_better_replacements": True,
        },
    ],
    ids=("wrong-baseline", "also-remove-other", "also-add-other", "also-search"),
)
def test_noncompletion_scenario_rejects_semantically_conflicting_controls(
    arguments: dict[str, object],
) -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments=arguments,
                ),
            ),
        )
    )

    assert report.valid is False
    assert report.reason == "invalid_control_combination"


def test_academic_replacement_search_is_owned_by_controlled_graduation_call() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.COURSE_REPLACEMENT,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={
                        "planning_baseline_kind": "recommended_current_term",
                        "search_better_replacements": True,
                    },
                ),
            ),
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


@pytest.mark.parametrize(
    "change_key",
    ["add_current_courses", "remove_current_courses", "noncompletion_current_courses"],
)
def test_graduation_search_cannot_mix_with_explicit_changes(change_key: str) -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.COURSE_REPLACEMENT,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={
                        "planning_baseline_kind": "recommended_current_term",
                        "search_better_replacements": True,
                        change_key: ["DS341"],
                    },
                ),
            ),
        )
    )

    assert report.valid is False
    assert report.reason == "invalid_control_combination"


def test_graduation_course_comparison_owns_its_impact_criterion() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(
                StudentRequestOutcome.COURSE_COMPARISON,
                StudentRequestOutcome.GRADUATION_IMPACT,
            ),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="course_choice_comparison",
                    arguments={
                        "course_codes": ["IS362", "AI201"],
                        "objective": "graduation",
                    },
                ),
            ),
        ),
        explicit_course_codes=("IS362", "AI201"),
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


@pytest.mark.parametrize("objective", ["balanced", "unlock_impact", "timetable_fit", ""])
def test_graduation_course_comparison_requires_the_graduation_objective(
    objective: str,
) -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(
                StudentRequestOutcome.COURSE_COMPARISON,
                StudentRequestOutcome.GRADUATION_IMPACT,
            ),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="course_choice_comparison",
                    arguments={
                        "course_codes": ["IS362", "AI201"],
                        **({"objective": objective} if objective else {}),
                    },
                ),
            ),
        ),
        explicit_course_codes=("IS362", "AI201"),
    )

    assert report.valid is False
    assert report.reason == "requested_outcome_uncovered"
    assert report.uncovered_outcomes == (StudentRequestOutcome.GRADUATION_IMPACT,)


def test_course_comparison_does_not_own_a_standalone_graduation_impact() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.GRADUATION_IMPACT,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="course_choice_comparison",
                    arguments={
                        "course_codes": ["IS362", "AI201"],
                        "objective": "graduation",
                    },
                ),
            ),
        ),
        explicit_course_codes=("IS362", "AI201"),
    )

    assert report.valid is False
    assert report.reason == "requested_outcome_uncovered"


def test_graduation_comparison_makes_a_separate_forecast_call_redundant() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(
                StudentRequestOutcome.COURSE_COMPARISON,
                StudentRequestOutcome.GRADUATION_IMPACT,
            ),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="course_choice_comparison",
                    arguments={
                        "course_codes": ["IS362", "AI201"],
                        "objective": "graduation",
                    },
                ),
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={"planning_baseline_kind": "registered_timetable"},
                ),
            ),
        ),
        explicit_course_codes=("IS362", "AI201"),
    )

    assert report.valid is False
    assert report.reason == "unnecessary_capability"
    assert report.redundant_capabilities == ("graduation_progress",)


def test_balanced_comparison_does_not_make_a_separate_impact_call_redundant() -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(
                StudentRequestOutcome.COURSE_COMPARISON,
                StudentRequestOutcome.GRADUATION_IMPACT,
            ),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="course_choice_comparison",
                    arguments={
                        "course_codes": ["IS362", "AI201"],
                        "objective": "balanced",
                    },
                ),
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={
                        "planning_baseline_kind": "registered_timetable",
                        "add_current_courses": ["IS362", "AI201"],
                    },
                ),
            ),
        ),
        explicit_course_codes=("IS362", "AI201"),
    )

    assert report.valid is True
    assert report.redundant_capabilities == ()


def test_addition_compound_owns_priority_when_priority_is_an_addition_criterion() -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.COURSE_ADDITION,
                StudentRequestOutcome.COURSE_PRIORITY,
            ],
            "recommend_feasible_course_addition",
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


def test_compound_owner_makes_a_separate_graduation_call_redundant() -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.TIMETABLE_REVIEW,
                StudentRequestOutcome.GRADUATION_IMPACT,
            ],
            "improve_current_timetable",
            "graduation_progress",
        )
    )

    assert report.valid is False
    assert report.reason == "unnecessary_capability"
    assert report.redundant_capabilities == ("graduation_progress",)


def test_improvement_subsumes_a_requested_replacement_criterion() -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.TIMETABLE_REVIEW,
                StudentRequestOutcome.COURSE_REPLACEMENT,
            ],
            "improve_current_timetable",
        )
    )

    assert report.valid is True
    assert report.uncovered_outcomes == ()


def test_control_decisions_have_closed_outcome_contracts():
    assert evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.DIRECT,
            [StudentRequestOutcome.GENERAL_CONVERSATION],
        )
    ).valid
    assert evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.UNSUPPORTED,
            [StudentRequestOutcome.REGISTRATION_ACTION],
        )
    ).valid
    assert evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.UNSUPPORTED,
            [StudentRequestOutcome.CREDIT_LOAD_COMPARISON],
        )
    ).valid
    assert not evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.DIRECT,
            [StudentRequestOutcome.UNSUPPORTED_REQUEST],
        )
    ).valid
    assert not evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.UNSUPPORTED,
            [StudentRequestOutcome.GENERAL_CONVERSATION],
        )
    ).valid
    assert not evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.UNSUPPORTED,
            [
                StudentRequestOutcome.REGISTRATION_ACTION,
                StudentRequestOutcome.COURSE_ADDITION,
            ],
        )
    ).valid


@pytest.mark.parametrize(
    "boundary_outcome",
    [
        StudentRequestOutcome.REGISTRATION_ACTION,
        StudentRequestOutcome.CREDIT_LOAD_COMPARISON,
    ],
)
def test_execute_can_combine_verified_advice_with_server_owned_boundary(
    boundary_outcome: StudentRequestOutcome,
):
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [
                StudentRequestOutcome.COURSE_ADDITION,
                boundary_outcome,
            ],
            "recommend_feasible_course_addition",
        )
    )

    assert report.valid is True
    assert report.covered_outcomes == (
        StudentRequestOutcome.COURSE_ADDITION,
        boundary_outcome,
    )
    assert report.uncovered_outcomes == ()


def test_credit_load_comparison_cannot_turn_an_unrelated_tool_into_execute() -> None:
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [StudentRequestOutcome.CREDIT_LOAD_COMPARISON],
            "my_progress",
        )
    )

    assert report.valid is False
    assert report.reason == "unnecessary_capability"


def test_contextual_course_code_does_not_turn_forecast_into_a_change_scenario():
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.GRADUATION_FORECAST,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="graduation_progress",
                    arguments={"planning_baseline_kind": "registered_timetable"},
                ),
            ),
        ),
        explicit_course_codes=("CS424",),
    )

    assert report.valid is True
    assert report.uncovered_course_codes == ()


@pytest.mark.parametrize(
    ("outcome", "capability", "arguments"),
    (
        (
            StudentRequestOutcome.COURSE_ADDITION,
            "recommend_feasible_course_addition",
            {"objective": "balanced"},
        ),
        (
            StudentRequestOutcome.COURSE_DROP_IMPACT,
            "rank_current_course_drop_impact",
            {"objective": "balanced"},
        ),
    ),
)
def test_named_compound_target_must_be_present_in_its_arguments(
    outcome: StudentRequestOutcome,
    capability: str,
    arguments: dict[str, str],
) -> None:
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(outcome,),
            evidence_requests=(PlannedCapabilityCall(capability=capability, arguments=arguments),),
        ),
        explicit_course_codes=("DS341",),
    )

    assert report.valid is False
    assert report.reason == "requested_entity_uncovered"
    assert report.uncovered_course_codes == ("DS341",)


def test_coverage_can_require_selected_tools_to_be_advertised():
    report = evaluate_outcome_coverage(
        _plan(
            TurnPlanDecision.EXECUTE,
            [StudentRequestOutcome.COURSE_ADDITION],
            "recommend_feasible_course_addition",
        ),
        advertised_capabilities={"my_progress"},
    )

    assert report.valid is False
    assert report.reason == "capability_not_advertised"


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "objective": "faster_graduation",
            "credit_load_policy": "preserve",
            "allow_course_replacements": False,
        },
        {
            "objective": "schedule_quality",
            "credit_load_policy": "preserve",
            "allow_course_replacements": True,
        },
    ),
)
def test_improvement_outcome_rejects_incompatible_branch_controls(arguments):
    report = evaluate_outcome_coverage(
        StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            requested_outcomes=(StudentRequestOutcome.TIMETABLE_REVIEW,),
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="improve_current_timetable",
                    arguments=arguments,
                ),
            ),
        )
    )

    assert report.valid is False
    assert report.reason == "invalid_control_combination"
