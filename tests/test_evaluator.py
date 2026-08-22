"""The evaluator, tested — because a scorer nobody checks reports what it assumes.

The failure mode this guards is specific: a fake provider that writes its answer from
the spec, scored against the spec, producing a perfect report about nothing. So the
tests below check that the mock reads TOOL RESULTS, that the scorer reads the
CONTRACT, and that the two disagree when the system under test is wrong.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest
import yaml

from evals.advisor.contract import ContractError, load_contract
from evals.advisor.run_planner_priority import (
    _load_cases,
    _prior_presentation_for_case,
    _ProviderEvidenceTrace,
    _tool_names,
)
from evals.advisor.score_planner_priority import score_row

GOLDEN_STUDENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "evals"
    / "advisor"
    / "fixtures"
    / "golden_student_academic_state_v1.yaml"
)
EVIDENCE_CONTRACTS = GOLDEN_STUDENT.with_name("evidence_answer_contracts_v1.yaml")


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
        "provider_tool_results": [],
        "usage": {"provider_calls": 2},
    }
    base.update(over)
    return base


def _evidence_case() -> dict:
    case = _case()
    case["tool_contract"] = {
        "required_all": ["my_timetable"],
        "required_any": [],
        "allowed": ["my_timetable"],
        "forbidden": [],
    }
    case["answer_evidence_contract"] = {
        "source_tool": "my_timetable",
        "require": [
            "all_registration_course_codes",
            "all_registration_sections",
            "all_course_section_rows",
        ],
        "verify": [
            "course_codes",
            "section_labels",
            "credit_quantities",
            "course_section_rows",
            "course_time_rows",
        ],
    }
    return case


def _golden_registered_result() -> dict:
    world = yaml.safe_load(GOLDEN_STUDENT.read_text(encoding="utf-8"))
    return world["current_term"]["registered"]


def _golden_world() -> dict:
    return yaml.safe_load(GOLDEN_STUDENT.read_text(encoding="utf-8"))


def _progress_case(source_tool: str = "graduation_progress") -> dict:
    case = _case()
    case["tool_contract"] = {
        "required_all": [source_tool],
        "required_any": [],
        "allowed": [source_tool],
        "forbidden": [],
    }
    contracts = yaml.safe_load(EVIDENCE_CONTRACTS.read_text(encoding="utf-8"))["contracts"]
    case["answer_evidence_contract"] = copy.deepcopy(contracts["remaining_plan_progress"])
    case["answer_evidence_contract"]["source_tool"] = source_tool
    return case


def _proposal_case() -> dict:
    case = _case()
    case["tool_contract"] = {
        "required_all": ["build_timetable_proposal"],
        "required_any": [],
        "allowed": ["build_timetable_proposal"],
        "forbidden": [],
    }
    contracts = yaml.safe_load(EVIDENCE_CONTRACTS.read_text(encoding="utf-8"))["contracts"]
    case["answer_evidence_contract"] = copy.deepcopy(contracts["timetable_proposal"])
    return case


def test_the_surface_and_the_calls_are_scored_independently() -> None:
    """The distinction the last live batch could not make.

    Calling the right tool while five wrong ones were also on offer is a different
    result from calling it when it was the only option — the first is a model that
    happened to choose well, the second is an orchestration that gave it no way to
    choose badly. Collapsing them is what made the first batch undiagnosable.
    """
    wide = score_row(_case(), _row(exposed_tools=["my_progress", "why_course_locked"]))
    assert wide["scores"]["evidence_acquisition_correct"] is True
    assert wide["scores"]["tool_surface_correct"] is False

    narrow = score_row(_case(), _row())
    assert narrow["scores"]["tool_surface_correct"] is True
    assert narrow["scores"]["evidence_acquisition_correct"] is True


def test_a_forbidden_tool_fails_the_calls_dimension() -> None:
    scored = score_row(_case(), _row(tools_called=["my_progress", "why_course_locked"]))
    assert scored["scores"]["evidence_acquisition_correct"] is False


def test_a_required_any_group_needs_one_member() -> None:
    case = _case()
    case["tool_contract"] = {
        "required_all": [],
        "required_any": [["my_progress", "why_course_locked"]],
        "allowed": ["my_progress", "why_course_locked"],
        "forbidden": [],
    }
    assert score_row(case, _row(tools_called=["why_course_locked"]))["scores"][
        "evidence_acquisition_correct"
    ]
    assert not score_row(case, _row(tools_called=["my_timetable"]))["scores"][
        "evidence_acquisition_correct"
    ]


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
    assert scored["scores"]["final_answer_correctness"] is False


def test_final_correctness_depends_on_grounding_even_for_non_safety_postconditions() -> None:
    scored = score_row(_case(), _row(output_violations=["inconsistent_credit_cap"]))
    assert scored["scores"]["safety"] is True
    assert scored["scores"]["factual_grounding"] is False
    assert scored["scores"]["final_answer_correctness"] is False


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

    timetable = mock_provider._render([_golden_registered_result()], "اعرض جدولي")
    assert all(code in timetable for code in ("AI433", "CS372", "MGT405"))
    assert all(section in timetable for section in ("M6", "M7"))


def test_the_mock_reads_the_v2_student_question_without_seeded_evidence() -> None:
    from evals.advisor.mock_provider import _latest_question, _relevant_tools

    question = "كم فصل دراسي متبقٍ لي تقريبًا حتى أنهي متطلبات الخطة؟"
    messages = [
        {
            "role": "user",
            "content": (
                f"student_question: {question}\n"
                'verified_policy_evidence: {"text":"متطلب مقرر وتعارض شعبة"}'
            ),
        }
    ]

    extracted = _latest_question(messages)
    assert extracted == question
    assert _relevant_tools(extracted, ["why_course_locked", "graduation_progress"]) == [
        "graduation_progress"
    ]


def test_v2_tool_results_are_counted_as_executed_evidence() -> None:
    assert _tool_names([{"tool": "graduation_progress", "ok": True}]) == ["graduation_progress"]


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
    assert scored["scores"]["evidence_acquisition_correct"] is True
    # And the gap is REPORTED, not buried: a model that stops choosing tools must be
    # visible even while the turn keeps passing.
    assert scored["tools"]["server_completed"] == ["my_progress"]
    assert scored["tools"]["model_called"] == []


def test_evidence_that_never_ran_at_all_still_fails() -> None:
    """The narrowing above is not a waiver — nothing fetched it, so nothing grounds it."""
    scored = score_row(_case(), _row(model_tools_called=[], executed_evidence_tools=[]))
    assert scored["scores"]["evidence_acquisition_correct"] is False


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
    assert scored["scores"]["evidence_acquisition_correct"] is False


def test_seeded_evidence_satisfies_a_contract_that_names_it() -> None:
    """The third source, and the reason the gate is about ACQUISITION.

    CP09/CP17 ask about recommendations the adviser has already seeded into the turn
    before the model sees the question. Requiring a tool call for those marked a
    correct answer wrong and pushed the model to re-fetch what it had been handed.
    """
    case = _case()
    case["tool_contract"]["required_all"] = []
    case["evidence_required"] = ["verified_context.recommendations"]
    row = _row(
        model_tools_called=[],
        executed_evidence_tools=[],
        exposed_tools=[],
        verified_context_evidence=["verified_context.recommendations"],
    )
    assert score_row(case, row)["scores"]["evidence_acquisition_correct"] is True

    # Named but not carried is a failure, not a formality.
    bare = {**row, "verified_context_evidence": ["verified_context.student"]}
    scored = score_row(case, bare)
    assert scored["scores"]["evidence_acquisition_correct"] is False
    assert scored["tools"]["missing_evidence"] == ["verified_context.recommendations"]


def test_a_payload_field_note_is_documentation_and_never_gates() -> None:
    """`evidence_required` carries two vocabularies. Bare payload-field names record
    which part of a result an answer should rest on, and nothing in a trace can
    confirm them — gating on evidence no trace records is a check that always passes
    or always fails, never a measurement."""
    case = _case()
    case["tool_contract"]["required_all"] = []
    case["evidence_required"] = ["student_requested_courses"]
    row = _row(
        model_tools_called=[],
        executed_evidence_tools=[],
        exposed_tools=[],
        verified_context_evidence=[],
    )
    assert score_row(case, row)["scores"]["evidence_acquisition_correct"] is True


def test_the_models_tool_choice_is_reported_and_never_gates() -> None:
    """Do not hide the provider's behaviour, and do not let it decide correctness.

    A turn where the server completed every required capability is CORRECT and the
    model still chose nothing. Both facts belong in the report; only the first is a
    claim about whether the product works.
    """
    scored = score_row(
        _case(), _row(model_tools_called=[], executed_evidence_tools=["my_progress"])
    )
    assert scored["scores"]["evidence_acquisition_correct"] is True
    assert scored["diagnostics"]["model_tool_choice"] is False
    assert "model_tool_choice" not in scored["scores"]
    assert scored["tools"]["model_tool_recall"] == "0/1"

    chose = score_row(_case(), _row(model_tools_called=["my_progress"]))
    assert chose["diagnostics"]["model_tool_choice"] is True


def test_an_unknown_evidence_path_fails_before_question_one(tmp_path) -> None:
    """A contract may require seeded evidence instead of a tool call, so a typo in
    that path would otherwise be satisfiable by nothing at all — the case would look
    green while requiring something no turn could ever carry."""
    import evals.advisor.contract as contract_module

    doc = yaml.safe_load(
        pathlib.Path(contract_module.__file__)
        .parent.joinpath("planner_priority_eval_v1.yaml")
        .read_text(encoding="utf-8")
    )
    doc["cases"][0]["evidence_required"] = ["verified_context.recomendations"]
    bad = tmp_path / "typo.yaml"
    bad.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown evidence path"):
        load_contract(bad)


def test_an_unknown_answer_evidence_profile_fails_before_question_one(tmp_path) -> None:
    import evals.advisor.contract as contract_module

    doc = yaml.safe_load(
        pathlib.Path(contract_module.__file__)
        .parent.joinpath("planner_priority_eval_v1.yaml")
        .read_text(encoding="utf-8")
    )
    tt11 = next(case for case in doc["cases"] if case["id"] == "TT11")
    tt11["answer_evidence_contract"]["profile"] = "arabic_question_regex"
    bad = tmp_path / "bad-profile.yaml"
    bad.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown answer evidence profile"):
        load_contract(bad)


def test_golden_student_carries_the_cross_tool_edge_cases() -> None:
    """Keep one compact world with the contradictions that reached production.

    This is a fixture-shape assertion, not application logic.  Its purpose is to
    stop future evals quietly simplifying the student back to a happy path where
    StudentCourse, registered evidence, expected evidence, aliases and catalog
    provenance all agree.
    """

    world = yaml.safe_load(GOLDEN_STUDENT.read_text(encoding="utf-8"))
    registered = world["current_term"]["registered"]
    expected = world["current_term"]["expected"]
    studying = set(world["student"]["student_course"]["studying"])

    assert registered["schedule_kind"] == "REGISTERED"
    assert expected["schedule_kind"] == "EXPECTED_PLAN"
    assert {row["course_code"] for row in registered["registrations"]} - studying == {"CS372"}
    assert registered["courses_without_a_time"] == ["CS372"]
    assert world["course_aliases"] == {"AI1": "AI463"}
    assert {row["section"] for row in world["catalog_rows_excluded_from_student_tools"]} == {
        "YF4",
        "YM3",
    }
    assert {row["schedule_kind"] for row in world["conflicting_raw_provenance"]} == {
        "REGISTERED",
        "EXPECTED_PLAN",
    }


def test_evidence_contracts_are_bounded_to_typed_facts_not_question_phrases() -> None:
    document = yaml.safe_load(EVIDENCE_CONTRACTS.read_text(encoding="utf-8"))
    assert set(document["contracts"]) == {
        "remaining_plan_progress",
        "timetable_proposal",
    }
    serialized = str(document["contracts"])
    assert "question" not in serialized
    assert "regex" not in serialized


@pytest.mark.parametrize(
    "answer",
    [
        ("جدولك المسجل هو: AI433 في الشعبة M6، وCS372 في M7، وMGT405 في M7. المجموع 9 ساعات."),
        (
            "| المقرر | الشعبة |\n| AI433 | M6 |\n| CS372 | M7 |\n| MGT405 | M7 |\n"
            "إجمالي العبء: ٩ ساعات معتمدة."
        ),
        (
            "المواد الحالية ثلاث: الشعبة M6 لمقرر AI-433، ثم M7 لكل من "
            "CS-372 وMGT-405؛ وعدد الساعات المعتمدة 9 ساعات."
        ),
    ],
)
def test_answer_evidence_accepts_natural_arabic_variants(answer: str) -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer=answer,
            tools_called=["my_timetable"],
            provider_tool_results=[_golden_registered_result()],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is True
    assert scored["scores"]["factual_grounding"] is True
    assert scored["scores"]["final_answer_correctness"] is True


def test_correct_tool_but_ignored_result_fails_answer_completeness() -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer="لقد عرضت لك الجدول بالفعل، أخبرني إن كنت تريد بدائل.",
            tools_called=["my_timetable"],
            provider_tool_results=[_golden_registered_result()],
        ),
    )

    # Acquisition and fulfilment are intentionally independent: the exact live
    # defect fetched the right evidence, then never displayed any of it.
    assert scored["scores"]["evidence_acquisition_correct"] is True
    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is False
    assert scored["answer_evidence"]["missing"] == {
        "course_codes": ["AI433", "CS372", "MGT405"],
        "section_labels": ["M6", "M7"],
        "course_section_rows": ["AI433/M6", "CS372/M7", "MGT405/M7"],
    }
    assert scored["scores"]["final_answer_correctness"] is False


@pytest.mark.parametrize(
    ("answer", "unsupported_kind", "unsupported_value"),
    [
        (
            "المسجل: AI433 شعبة M6، وCS372 شعبة M7، وMGT405 شعبة M7، وكذلك AI999. المجموع 9 ساعات.",
            "course_codes",
            "AI999",
        ),
        (
            "المسجل: AI433 شعبة M6، وCS372 شعبة M7، وMGT405 شعبة F11. المجموع 9 ساعات.",
            "section_labels",
            "F11",
        ),
        (
            "المسجل: AI433 شعبة M6، وCS372 شعبة M7، وMGT405 شعبة M7. الإجمالي 12 ساعة.",
            "credit_quantities",
            12,
        ),
    ],
)
def test_fabricated_exact_academic_facts_fail_support(
    answer: str, unsupported_kind: str, unsupported_value: object
) -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer=answer,
            tools_called=["my_timetable"],
            provider_tool_results=[_golden_registered_result()],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is False
    assert unsupported_value in scored["answer_evidence"]["unsupported"][unsupported_kind]
    assert scored["scores"]["factual_grounding"] is False
    assert scored["scores"]["final_answer_correctness"] is False


def test_contracted_answer_without_saved_tool_payload_cannot_pass_by_default() -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer="AI433 في M6 وCS372 وMGT405 في M7، بمجموع 9 ساعات.",
            tools_called=["my_timetable"],
            # The exact defect: a rich local result exists, but no provider-facing
            # payload was saved. The evaluator must not use the local record as an
            # answer key for facts the model may never have received.
            tool_results=[_golden_registered_result()],
            provider_tool_results=[],
        ),
    )

    assert scored["answer_evidence"]["evaluable"] is False
    assert scored["scores"]["factual_grounding"] is False
    assert scored["scores"]["final_answer_correctness"] is False


def test_provider_projection_wins_when_local_and_remote_evidence_disagree() -> None:
    provider = _golden_registered_result()
    local = copy.deepcopy(provider)
    local["registrations"][0]["course_code"] = "LOCAL999"
    scored = score_row(
        _evidence_case(),
        _row(
            answer=(
                "AI433 في M6، وCS372 في M7، وMGT405 في M7، "
                "أما LOCAL999 فليس في البيانات المعروضة. المجموع 9 ساعات."
            ),
            tools_called=["my_timetable"],
            tool_results=[local],
            provider_tool_results=[provider],
        ),
    )

    assert scored["answer_evidence"]["evidence_source"] == "provider_tool_results"
    assert scored["answer_evidence"]["unsupported"]["course_codes"] == ["LOCAL999"]
    assert scored["scores"]["factual_grounding"] is False


def test_provider_trace_records_only_projected_tool_messages() -> None:
    class Delegate:
        def chat_with_tools(self, messages, **kwargs):
            return "sentinel"

    traced = _ProviderEvidenceTrace(Delegate())
    result = traced.chat_with_tools(
        [
            {"role": "user", "content": "do not retain this question"},
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps(
                    {"tool": "my_timetable", "ok": True, "registered_course_count": 3}
                ),
            },
        ],
        tools=[],
    )

    assert result == "sentinel"
    assert traced.provider_tool_results == [
        {"tool": "my_timetable", "ok": True, "registered_course_count": 3}
    ]


def test_unknown_course_prefix_is_not_silently_discarded() -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer=("AI433 في M6، وCS372 في M7، وMGT405 في M7، ويوجد أيضًا ZX999. المجموع 9 ساعات."),
            tools_called=["my_timetable"],
            provider_tool_results=[_golden_registered_result()],
        ),
    )

    assert scored["answer_evidence"]["unsupported"]["course_codes"] == ["ZX999"]


def test_provider_supplied_room_that_looks_like_a_course_is_not_misclassified() -> None:
    provider = copy.deepcopy(_golden_registered_result())
    provider["meetings"][0]["room"] = "LAB201"
    scored = score_row(
        _evidence_case(),
        _row(
            answer=("AI433 في M6 وقاعته LAB201، وCS372 في M7، وMGT405 في M7. المجموع 9 ساعات."),
            tools_called=["my_timetable"],
            provider_tool_results=[provider],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is True


def test_timetable_course_section_pairs_are_relational_not_independent_sets() -> None:
    scored = score_row(
        _evidence_case(),
        _row(
            answer="AI433 في M7، وCS372 في M6، وMGT405 في M7. المجموع 9 ساعات.",
            tools_called=["my_timetable"],
            provider_tool_results=[_golden_registered_result()],
        ),
    )

    assert scored["answer_evidence"]["unsupported"]["course_section_rows"] == [
        "AI433/M7",
        "CS372/M6",
    ]
    assert scored["scores"]["factual_grounding"] is False


def test_projected_registration_count_without_all_rows_cannot_be_scored_complete() -> None:
    local = _golden_registered_result()
    provider = copy.deepcopy(local)
    provider.pop("registrations")
    # A course with no scheduled meeting disappears from this projected shape,
    # while the registrar count still proves that a third row exists.
    scored = score_row(
        _evidence_case(),
        _row(
            answer="AI433 في M6، وMGT405 في M7. الإجمالي 9 ساعات.",
            tools_called=["my_timetable"],
            tool_results=[local],
            provider_tool_results=[provider],
        ),
    )

    assert scored["answer_evidence"]["missing"]["provider_registration_rows"] == ["2/3 visible"]
    assert scored["answer_evidence"]["completeness_ok"] is False


@pytest.mark.parametrize(
    "answer",
    [
        (
            "اجتزت 32 مقررًا. إجمالي الخطة 48 مقررًا. بقي 16 مقررًا و48 ساعة. "
            "واجتزت من الخطة 96 ساعة. إجمالي ساعات الخطة 144 ساعة."
        ),
        (
            "المكتمل: ٣٢ مادة. مجموع مواد الخطة: ٤٨ مادة. "
            "الباقي: ١٦ مادة و٤٨ ساعة. الساعات المجتازة: ٩٦ ساعة. "
            "مجموع ساعات الخطة: ١٤٤ ساعة."
        ),
        (
            "Completed: 32 courses. Total: 48 courses. Remaining: 16 courses and "
            "48 credits. Passed in the plan: 96 credits. Total plan: 144 credits."
        ),
        (
            "عدد المقررات المجتازة: 32. إجمالي المقررات في الخطة: 48. "
            "المقررات المتبقية: 16. الساعات المجتازة من الخطة: 96. "
            "الساعات المتبقية: 48. إجمالي ساعات الخطة: 144."
        ),
    ],
)
def test_progress_contract_accepts_natural_wording_with_exact_figures(answer: str) -> None:
    evidence = _golden_world()["progress_evidence"]["graduation"]
    scored = score_row(
        _progress_case(),
        _row(
            answer=answer,
            tools_called=["graduation_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is True
    assert scored["scores"]["factual_grounding"] is True
    assert scored["scores"]["final_answer_correctness"] is True


@pytest.mark.parametrize(
    ("extra_claim", "fact_type", "wrong_value"),
    [
        ("واجتزت 31 مقررًا.", "passed_course_counts", 31),
        ("وإجمالي الخطة 50 مقررًا.", "total_course_counts", 50),
        ("والمتبقي 18 مقررًا.", "remaining_course_counts", 18),
        ("والمتبقي 50 ساعة.", "remaining_credit_counts", 50),
        ("واجتزت 95 ساعة.", "passed_credit_counts", 95),
        ("وإجمالي الخطة 145 ساعة.", "total_credit_counts", 145),
        ("وعدد المقررات المجتازة: 31.", "passed_course_counts", 31),
        ("والساعات المتبقية: 50.", "remaining_credit_counts", 50),
    ],
)
def test_progress_contract_rejects_invented_plan_figures(
    extra_claim: str, fact_type: str, wrong_value: int
) -> None:
    evidence = _golden_world()["progress_evidence"]["graduation"]
    answer = (
        "اجتزت 32 مقررًا. إجمالي الخطة 48 مقررًا. "
        "المتبقي 16 مقررًا و48 ساعة. اجتزت 96 ساعة. إجمالي الخطة 144 ساعة. " + extra_claim
    )
    scored = score_row(
        _progress_case(),
        _row(
            answer=answer,
            tools_called=["graduation_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert scored["answer_evidence"]["completeness_ok"] is True
    assert wrong_value in scored["answer_evidence"]["unsupported"][fact_type]
    assert scored["scores"]["factual_grounding"] is False
    assert scored["scores"]["final_answer_correctness"] is False


def test_my_progress_does_not_authorise_invented_plan_totals() -> None:
    evidence = _golden_world()["progress_evidence"]["prerequisite_state"]
    case = _progress_case("my_progress")
    case["answer_evidence_contract"]["require"] = ["passed_course_counts"]
    scored = score_row(
        case,
        _row(
            answer="بحسب التقدم اجتزت 32 مقررًا، وإجمالي الخطة 48 مقررًا.",
            tools_called=["my_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert scored["answer_evidence"]["completeness_ok"] is True
    assert scored["answer_evidence"]["unsupported"]["total_course_counts"] == [48]
    assert scored["scores"]["factual_grounding"] is False


def test_graduation_ratios_percent_and_term_labels_are_preserved() -> None:
    evidence = _golden_world()["progress_evidence"]["graduation"]
    scored = score_row(
        _progress_case(),
        _row(
            answer=(
                "أنجزت 32 من أصل 48 مقررًا، وبقي 16 مقررًا. "
                "وأنجزت من الخطة 96 من أصل 144 ساعة، والمتبقي 48 ساعة. "
                "نسبة الإنجاز 67%. التقدير 2 فصل إضافي، و3 فصول شاملة فصل البداية."
            ),
            tools_called=["graduation_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is True
    assert scored["answer_evidence"]["observed"]["passed_course_counts"] == [32]
    assert scored["answer_evidence"]["observed"]["total_course_counts"] == [48]


def test_plan_credits_and_registrar_earned_credits_are_not_interchangeable() -> None:
    evidence = _golden_world()["progress_evidence"]["graduation"]
    scored = score_row(
        _progress_case(),
        _row(
            answer=(
                "اجتزت 32 مقررًا من إجمالي 48، وبقي 16 مقررًا و48 ساعة. "
                "اجتزت من الخطة 100 ساعة، وإجمالي ساعات الخطة 144 ساعة."
            ),
            tools_called=["graduation_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert scored["answer_evidence"]["unsupported"]["passed_plan_credit_counts"] == [100]
    assert scored["scores"]["factual_grounding"] is False


@pytest.mark.parametrize(
    ("claim", "fact_type", "wrong_value"),
    [
        ("نسبة الإنجاز 66%.", "completion_percentages", 66),
        ("أحتاج 4 فصول إضافية.", "additional_term_counts", 4),
        ("عدد الفصول الإضافية: 4.", "additional_term_counts", 4),
        (
            "أحتاج 4 فصول شاملة فصل البداية.",
            "including_baseline_term_counts",
            4,
        ),
        (
            "بعد اجتياز مقررات البداية سيبقى 16 مقررًا و48 ساعة.",
            "post_baseline_remaining_course_counts",
            16,
        ),
    ],
)
def test_labeled_graduation_metrics_cannot_borrow_an_unrelated_valid_number(
    claim: str, fact_type: str, wrong_value: int
) -> None:
    evidence = _golden_world()["progress_evidence"]["graduation"]
    answer = (
        "اجتزت 32 مقررًا. إجمالي الخطة 48 مقررًا. المتبقي 16 مقررًا و48 ساعة. "
        "اجتزت من الخطة 96 ساعة. إجمالي ساعات الخطة 144 ساعة. " + claim
    )
    scored = score_row(
        _progress_case(),
        _row(
            answer=answer,
            tools_called=["graduation_progress"],
            provider_tool_results=[evidence],
        ),
    )

    assert wrong_value in scored["answer_evidence"]["unsupported"][fact_type]
    assert scored["scores"]["final_answer_correctness"] is False


@pytest.mark.parametrize(
    "answer",
    [
        "تم إعداد البديل، وتفاصيله موضحة في العرض المنظم أدناه.",
        ("البديل يضع AI463 في الشعبة M4 من 09:00 إلى 10:15، ويضع CS424 في M9 من 13:00 إلى 14:15."),
        ("الخيار المقترح: AI-463 / M4 عند ٠٩:٠٠–١٠:١٥، ثم CS-424 / M9 عند ١٣:٠٠–١٤:١٥."),
    ],
)
def test_proposal_contract_accepts_natural_prose_when_structured_rows_match(answer: str) -> None:
    proposal = _golden_world()["timetable_proposal"]
    scored = score_row(
        _proposal_case(),
        _row(
            answer=answer,
            tools_called=["build_timetable_proposal"],
            provider_tool_results=[proposal["tool_result"]],
            presentation=proposal["presentation"],
        ),
    )

    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is True
    assert scored["scores"]["final_answer_correctness"] is True


def test_plausible_proposal_prose_without_structured_presentation_fails() -> None:
    proposal = _golden_world()["timetable_proposal"]
    scored = score_row(
        _proposal_case(),
        _row(
            answer="جهزت لك جدولًا متوازنًا بلا تعارضات وبأوقات مناسبة.",
            tools_called=["build_timetable_proposal"],
            provider_tool_results=[proposal["tool_result"]],
            presentation=None,
        ),
    )

    assert scored["scores"]["evidence_acquisition_correct"] is True
    assert scored["answer_evidence"]["support_ok"] is True
    assert scored["answer_evidence"]["completeness_ok"] is False
    assert scored["answer_evidence"]["missing"] == {
        "structured_presentation": ["timetable_proposals"]
    }
    assert scored["scores"]["final_answer_correctness"] is False


def test_proposal_presentation_must_match_the_same_tool_rows() -> None:
    proposal = _golden_world()["timetable_proposal"]
    stale = copy.deepcopy(proposal["presentation"])
    stale["alternatives"][0]["courses"][0]["section"] = "M5"
    scored = score_row(
        _proposal_case(),
        _row(
            answer="تفاصيل المقترح معروضة أدناه.",
            tools_called=["build_timetable_proposal"],
            provider_tool_results=[proposal["tool_result"]],
            presentation=stale,
        ),
    )

    assert scored["answer_evidence"]["completeness_ok"] is False
    assert "presentation_course_rows" in scored["answer_evidence"]["missing"]
    assert scored["answer_evidence"]["support_ok"] is False
    assert scored["answer_evidence"]["unsupported"]["presentation_course_rows"] == ["AI463/M5"]
    assert scored["scores"]["final_answer_correctness"] is False


@pytest.mark.parametrize(
    ("answer", "fact_type", "wrong_value"),
    [
        ("البديل يضع AI463 في الشعبة F11.", "section_labels", "F11"),
        ("موعد AI463 من 11:00 إلى 12:15.", "meeting_times", "11:00"),
        ("أضفت AI999 إلى المقترح.", "course_codes", "AI999"),
    ],
)
def test_proposal_contract_rejects_invented_courses_sections_and_times(
    answer: str, fact_type: str, wrong_value: object
) -> None:
    proposal = _golden_world()["timetable_proposal"]
    scored = score_row(
        _proposal_case(),
        _row(
            answer=answer,
            tools_called=["build_timetable_proposal"],
            provider_tool_results=[proposal["tool_result"]],
            presentation=proposal["presentation"],
        ),
    )

    assert scored["answer_evidence"]["completeness_ok"] is True
    assert wrong_value in scored["answer_evidence"]["unsupported"][fact_type]
    assert scored["scores"]["factual_grounding"] is False
    assert scored["scores"]["final_answer_correctness"] is False


def test_proposal_course_section_and_time_claims_must_match_the_same_rows() -> None:
    proposal = _golden_world()["timetable_proposal"]
    scored = score_row(
        _proposal_case(),
        _row(
            answer=(
                "الخيار يضع AI463 في M9 من 13:00 إلى 14:15، ويضع CS424 في M4 من 09:00 إلى 10:15."
            ),
            tools_called=["build_timetable_proposal"],
            provider_tool_results=[proposal["tool_result"]],
            presentation=proposal["presentation"],
        ),
    )

    assert scored["answer_evidence"]["unsupported"]["course_section_rows"] == [
        "AI463/M9",
        "CS424/M4",
    ]
    assert set(scored["answer_evidence"]["unsupported"]["course_time_rows"]) >= {
        "AI463/13:00",
        "CS424/09:00",
    }
    assert scored["scores"]["factual_grounding"] is False


@pytest.mark.django_db
def test_course_name_follow_up_canary_executes_with_the_prior_structured_artifact(
    monkeypatch,
) -> None:
    from core.models import Student
    from core.services.advisor_principal import AdvisorPrincipal
    from core.services.llm_backend import ChatResult, ToolCallRequest, ToolChatResult
    from core.services.rbac import ROLE_STUDENT
    from core.services.student_advisor_v2 import answer_student_advisor_v2

    case = _load_cases("evidence_boundary_canary")[0]
    prior_presentation = _prior_presentation_for_case(case)
    assert prior_presentation is not None

    class PriorArtifactClient:
        backend = "local"
        supports_assistant_prefill = True

        def __init__(self) -> None:
            self.checked_prior_artifact = False

        def resolve_model(self, requested_model=None):
            return requested_model or "eval-prior-artifact"

        def chat_with_tools(self, messages, *, tools, **kwargs):
            prior_prompt = next(
                message
                for message in messages
                if message.get("role") == "user"
                and "verified_prior_presentation: " in str(message.get("content") or "")
            )
            presentation = json.loads(
                str(prior_prompt["content"]).split("verified_prior_presentation: ", 1)[1]
            )
            assert presentation["graph"]["nameOf"] == {
                "DS321": "ذكاء الأعمال",
                "STAT301": "الاحتمالات والإحصاء",
            }
            self.checked_prior_artifact = True
            arguments = dict(case["expected_arguments"])
            call = ToolCallRequest(
                id="call_prior_1",
                name=case["expected_tool"],
                arguments=arguments,
                raw_arguments=json.dumps(arguments),
            )
            return ToolChatResult(
                content="",
                tool_calls=(call,),
                model="eval-prior-artifact",
                usage={},
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments,
                            },
                        }
                    ],
                },
            )

        def chat(self, messages, **kwargs):
            return ChatResult(
                content="تمت مقارنة حذف STAT301 وإضافة DS321 من دون تغيير التسجيل الفعلي.",
                model="eval-prior-artifact",
                usage={},
            )

    student_id = 4999001
    Student.objects.create(
        student_id=student_id,
        name="Evidence Eval Student",
        program="AI",
        section="M",
    )
    executed: list[dict[str, Any]] = []

    def execute(name, arguments, **kwargs):
        executed.append(dict(arguments))
        return {
            "tool": "graduation_progress",
            "ok": True,
            "program": "AI",
            "planning_baseline_kind": "registered_timetable",
            "simulation_completed": True,
            "estimated_additional_terms": 2,
            "estimated_terms_including_planning_baseline": 3,
            "lower_bound_additional_terms": 2,
            "lower_bound_terms_including_planning_baseline": 3,
            "what_if": {
                "valid": True,
                "mode": "current_course_changes",
                "removed_current_courses": [
                    {"code": "STAT301", "name": "الاحتمالات والإحصاء", "credits": 3}
                ],
                "added_current_courses": [{"code": "DS321", "name": "ذكاء الأعمال", "credits": 3}],
                "baseline": {"lower_bound_additional_terms": 2},
                "scenario": {"lower_bound_additional_terms": 2},
                "comparison": {
                    "timing_effect": "SAME",
                    "term_difference": 0,
                    "terms_saved": 0,
                    "blockers_resolved": [],
                    "blockers_improved": [],
                    "blockers_introduced": [],
                    "plan_changed": False,
                    "term_plan_changes": [],
                },
                "outside_plan_additions": [],
            },
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", execute)
    client = PriorArtifactClient()
    result = answer_student_advisor_v2(
        question=case["question_ar"],
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=student_id),
        academic_year=1448,
        term=1,
        llm_client=client,
        prior_presentation=prior_presentation,
    )

    assert client.checked_prior_artifact is True
    assert executed == [case["expected_arguments"]]
    assert result["agent"]["graduation_what_if_required"] is True
    assert result["agent"]["graduation_what_if_missing"] is False
    assert "STAT301" in result["answer"] and "DS321" in result["answer"], {
        "outcome": result["agent"]["evidence_validation_outcome"],
        "violations": result["agent"]["evidence_validation_violations"],
        "after_repair": result["agent"]["evidence_validation_violations_after_repair"],
    }
