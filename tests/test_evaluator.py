"""The evaluator, tested — because a scorer nobody checks reports what it assumes.

The failure mode this guards is specific: a fake provider that writes its answer from
the spec, scored against the spec, producing a perfect report about nothing. So the
tests below check that the mock reads TOOL RESULTS, that the scorer reads the
CONTRACT, and that the two disagree when the system under test is wrong.
"""

from __future__ import annotations

import pytest

from evals.advisor.contract import ContractError, load_contract
from evals.advisor.score_planner_priority import score_row


def test_the_canonical_contract_loads_and_is_complete() -> None:
    cases = load_contract()
    assert len(cases) == 50
    assert {c["id"] for c in cases} >= {"TT01", "TT30", "CP01", "CP20"}


def test_a_malformed_contract_fails_before_question_one(tmp_path) -> None:
    """A per-case skip would produce a report measuring 49 things and claiming 50."""
    import yaml

    doc = {"meta": {"scoring_dimensions": ["intent_recognition"]}, "cases": []}
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ContractError, match="expected 50"):
        load_contract(bad)


def _case(**routing):
    base = {
        "id": "XX01",
        "question_ar": "س",
        "routing": {
            "mode": "exact",
            "domain": "COURSE_DATA",
            "expected_family": "COURSE_PRIORITY",
            "allowed_families": ["COURSE_PRIORITY"],
            "composition": "SINGLE",
            "clarification_reason": None,
            "requires_prior_context": False,
        },
        "tool_contract": {
            "required_all": ["my_progress"],
            "required_any": [],
            "allowed": ["my_progress"],
            "forbidden": ["why_course_locked"],
        },
        "expected_action": None,
        "policy_contract": {"mode": "data_only"},
    }
    base["routing"].update(routing)
    return base


def _row(**over):
    base = {
        "id": "XX01",
        "answer": "إجابة.",
        "error": None,
        "action": None,
        "intent_family": "COURSE_PRIORITY",
        "policy_domain": "COURSE_DATA",
        "policy_required": False,
        "policy_grounding": "retrieved",
        "exposed_tools": ["my_progress"],
        "tools_called": ["my_progress"],
        "output_violations": [],
        "usage": {"provider_calls": 2},
    }
    base.update(over)
    return base


def test_the_surface_and_the_calls_are_scored_independently() -> None:
    """The distinction the last live batch could not make.

    Calling the right tool while five wrong ones were also on offer is a different
    result from calling it when it was the only option — the first is a model that
    happened to choose well, the second is an orchestration that gave it no way to
    choose badly. Collapsing them is what made the first batch undiagnosable.
    """
    wide = score_row(_case(), _row(exposed_tools=["my_progress", "why_course_locked"]))
    assert wide["scores"]["tool_calls_correct"] is True
    assert wide["scores"]["tool_surface_correct"] is False

    narrow = score_row(_case(), _row())
    assert narrow["scores"]["tool_surface_correct"] is True
    assert narrow["scores"]["tool_calls_correct"] is True


def test_a_forbidden_tool_fails_the_calls_dimension() -> None:
    scored = score_row(_case(), _row(tools_called=["my_progress", "why_course_locked"]))
    assert scored["scores"]["tool_calls_correct"] is False


def test_a_required_any_group_needs_one_member() -> None:
    case = _case()
    case["tool_contract"] = {
        "required_all": [],
        "required_any": [["my_progress", "why_course_locked"]],
        "allowed": ["my_progress", "why_course_locked"],
        "forbidden": [],
    }
    assert score_row(case, _row(tools_called=["why_course_locked"]))["scores"]["tool_calls_correct"]
    assert not score_row(case, _row(tools_called=["my_timetable"]))["scores"]["tool_calls_correct"]


def test_a_deterministic_action_must_cost_no_inference() -> None:
    """`provider_calls == 0` is a product contract now, not an optimisation, so the
    scorer treats a hand-off that cost a call as an action failure."""
    case = _case()
    case["expected_action"] = {
        "type": "OPEN_STUDENT_PLANNER",
        "intent": "EDIT_DRAFT",
        "registration_modified": False,
    }
    free = _row(
        action={
            "type": "OPEN_STUDENT_PLANNER",
            "intent": "EDIT_DRAFT",
            "registration_modified": False,
        },
        exposed_tools=[],
        tools_called=[],
        usage={"provider_calls": 0},
    )
    assert score_row(case, free)["scores"]["action_correct"] is True

    paid = {**free, "usage": {"provider_calls": 1}}
    assert score_row(case, paid)["scores"]["action_correct"] is False

    # And no evidence may have run either, whoever ran it. A hand-off the server
    # decided before generation has nothing to retrieve; a row that shows a tool
    # result is not the deterministic path, it is the loop wearing its name.
    for key in ("model_tools_called", "executed_evidence_tools"):
        assert score_row(case, {**free, key: ["my_progress"]})["scores"]["action_correct"] is False


def test_a_data_only_case_refused_on_policy_fails_policy_compliance() -> None:
    """The exact defect 6B removed, kept as a scored dimension so it cannot return."""
    scored = score_row(
        _case(), _row(policy_required=True, policy_contract_failure="no_governing_evidence")
    )
    assert scored["scores"]["policy_compliance"] is False


def test_a_clarify_case_that_executes_a_tool_fails() -> None:
    case = _case(mode="clarify", expected_family=None, clarification_reason="MISSING_COURSE")
    case["tool_contract"]["required_all"] = []
    assert not score_row(case, _row(tools_called=["my_progress"]))["scores"][
        "final_answer_correctness"
    ]
    assert score_row(case, _row(tools_called=[]))["scores"]["final_answer_correctness"]


def test_a_safety_violation_fails_safety_and_grounding() -> None:
    scored = score_row(_case(), _row(output_violations=["claimed_registration_mutation"]))
    assert scored["scores"]["safety"] is False
    assert scored["scores"]["factual_grounding"] is False


def test_the_mock_renders_from_tool_results_and_never_from_the_contract() -> None:
    """The tautology guard.

    A fake that wrote its answer from `must_assert` would make the report say
    spec → fake → scorer → spec. This asserts the renderer's output is a function of
    the tool RESULT: change the result, the sentence changes; the contract is never
    read at all.
    """
    import ast
    import inspect

    from evals.advisor import mock_provider

    # STRUCTURAL, not textual: the module's own docstring names the anti-pattern it
    # avoids, so grepping the source finds the warning and calls it the crime.
    tree = ast.parse(inspect.getsource(mock_provider))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any("contract" in name for name in imported), "the mock imports the contract"

    for forbidden in ("must_assert", "must_not_claim", "expected_action", "answer_sketch_ar"):
        subscripts = [
            n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == forbidden
        ]
        assert not subscripts, f"the mock reads {forbidden} from the spec"

    rendered = mock_provider._render(
        [{"tool": "my_progress", "ok": True, "counts": {"open": 7, "locked": 8}}], "س"
    )
    assert "7" in rendered and "8" in rendered
    other = mock_provider._render(
        [{"tool": "my_progress", "ok": True, "counts": {"open": 1, "locked": 2}}], "س"
    )
    assert rendered != other


def test_evidence_the_server_completed_still_satisfies_the_contract() -> None:
    """TT20, the second failure the live canary reported.

    The route required two capabilities; the provider called one and the server
    completed the other, so the answer WAS built on both. Scoring the model's request
    list alone marked a correctly served answer wrong — and would have rewarded
    removing the completion step, which is the opposite of the intent. The contract
    says which evidence the answer must rest on, not who had to fetch it.
    """
    scored = score_row(
        _case(),
        _row(model_tools_called=[], executed_evidence_tools=["my_progress"]),
    )
    assert scored["scores"]["tool_calls_correct"] is True
    # And the gap is REPORTED, not buried: a model that stops choosing tools must be
    # visible even while the turn keeps passing.
    assert scored["tools"]["server_completed"] == ["my_progress"]
    assert scored["tools"]["model_called"] == []


def test_evidence_that_never_ran_at_all_still_fails() -> None:
    """The narrowing above is not a waiver — nothing fetched it, so nothing grounds it."""
    scored = score_row(_case(), _row(model_tools_called=[], executed_evidence_tools=[]))
    assert scored["scores"]["tool_calls_correct"] is False


def test_a_forbidden_tool_fails_even_when_only_the_server_ran_it() -> None:
    """The required half is satisfied here on purpose.

    A first version of this test left the required tool out as well, so the row failed
    for the wrong reason and a mutant that checked `forbidden` against the model's
    list alone survived it. The evidence a turn rests on is forbidden or it is not,
    whoever fetched it — a contract that forbids a capability the router then
    completes is a disagreement worth failing, not one worth hiding.
    """
    scored = score_row(
        _case(),
        _row(
            model_tools_called=["my_progress"],
            executed_evidence_tools=["my_progress", "why_course_locked"],
        ),
    )
    assert scored["scores"]["tool_calls_correct"] is False
