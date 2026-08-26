"""Deterministic contract validation and scoring for the V2.1 semantic planner.

This module makes no provider calls and imports no adviser runtime.  It accepts either
the V2.1 plan shape (``decision`` plus ``evidence_requests``) or a JSON-friendly shape
(``mode`` plus ``tool_calls``), then scores only observable planning decisions.

Example::

    python evals/advisor/v21_semantic_plan_eval.py v21-results.json \
        --baseline v2-results.json

Each result file contains ``{"rows": [{"case_id": "V21-SP-001", "plan": ...}]}``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.services.student_advisor_v21_policy import (  # noqa: E402
    semantic_policy_violations,
)

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONTRACT_PATH = HERE / "v21_semantic_plan_cases.yaml"

CONTRACT_NAME = "advisor_v21_semantic_plan"
CONTRACT_VERSION = "2.4"
VALID_MODES = frozenset({"execute", "clarify", "direct", "unsupported"})
VALID_CLARIFICATION_KINDS = frozenset(
    {
        "none",
        "timetable_load",
        "timetable_preference",
        "course_or_section_identity",
        "term_or_choice",
        "generic",
    }
)
VALID_CASE_TYPES = frozenset({"unseen_paraphrase", "regex_false_positive", "context_boundary"})
V21_TOOL_SURFACE = (
    "my_progress",
    "my_plan_by_term",
    "my_timetable",
    "my_clash_free_sections",
    "build_timetable_proposal",
    "lookup_course",
    "course_prerequisites",
    "why_course_locked",
    "course_choice_comparison",
    "feasible_course_replacements",
    "recommend_courses",
    "graduation_progress",
    "policy_lookup",
    "my_advisor",
    "recommend_feasible_course_addition",
    "rank_current_course_drop_impact",
    "improve_current_timetable",
)
VALID_OUTCOMES = frozenset(
    {
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
    }
)
OUTCOME_CAPABILITY_OWNERS: dict[str, frozenset[str]] = {
    "course_catalogue": frozenset({"lookup_course"}),
    "course_eligibility": frozenset({"why_course_locked"}),
    "prerequisite_information": frozenset(
        {"course_prerequisites", "why_course_locked", "my_progress"}
    ),
    "available_courses": frozenset({"my_progress"}),
    "course_priority": frozenset({"my_progress"}),
    "course_recommendation": frozenset({"recommend_courses"}),
    "course_addition": frozenset({"recommend_feasible_course_addition"}),
    "course_drop_impact": frozenset({"rank_current_course_drop_impact"}),
    "degree_progress": frozenset({"my_progress"}),
    "degree_plan": frozenset({"my_plan_by_term"}),
    "current_timetable": frozenset({"my_timetable"}),
    "timetable_review": frozenset({"improve_current_timetable"}),
    "timetable_build": frozenset({"build_timetable_proposal"}),
    "timetable_feasibility": frozenset({"my_clash_free_sections", "build_timetable_proposal"}),
    "course_comparison": frozenset({"course_choice_comparison"}),
    "course_replacement": frozenset({"feasible_course_replacements"}),
    "graduation_forecast": frozenset({"graduation_progress"}),
    "graduation_impact": frozenset(),
    "credit_load_comparison": frozenset(),
    "policy_rule": frozenset({"policy_lookup"}),
    "academic_adviser": frozenset({"my_advisor"}),
    "prior_result": frozenset({"present_prior_artifact"}),
    "registration_action": frozenset(),
    "general_conversation": frozenset(),
    "unsupported_request": frozenset(),
}
UNSUPPORTED_OUTCOMES = frozenset(
    {"registration_action", "credit_load_comparison", "unsupported_request"}
)
SERVER_OWNED_EXECUTE_OUTCOMES = frozenset({"registration_action", "credit_load_comparison"})
EXECUTE_FORBIDDEN_OUTCOMES = frozenset({"general_conversation", "unsupported_request"})


def _owners_for_outcome(
    outcome: str,
    requested: frozenset[str],
    calls: Sequence[Mapping[str, Any]] = (),
) -> frozenset[str]:
    owners = set(OUTCOME_CAPABILITY_OWNERS.get(outcome, frozenset()))
    if outcome == "graduation_impact":
        if any(
            str(call.get("name") or "") == "graduation_progress"
            and (
                bool((call.get("arguments") or {}).get("add_current_courses"))
                or bool((call.get("arguments") or {}).get("remove_current_courses"))
                or bool((call.get("arguments") or {}).get("noncompletion_current_courses"))
                or (call.get("arguments") or {}).get("search_better_replacements") is True
            )
            for call in calls
            if isinstance(call.get("arguments"), Mapping)
        ):
            owners.add("graduation_progress")
        owners.update(
            {
                "feasible_course_replacements",
                "improve_current_timetable",
                "rank_current_course_drop_impact",
                "recommend_feasible_course_addition",
            }
        )
        if "course_comparison" in requested and any(
            str(call.get("name") or "") == "course_choice_comparison"
            and str((call.get("arguments") or {}).get("objective") or "") == "graduation"
            for call in calls
            if isinstance(call.get("arguments"), Mapping)
        ):
            owners.add("course_choice_comparison")
    if outcome == "course_priority" and "course_addition" in requested:
        owners.add("recommend_feasible_course_addition")
    if outcome == "course_replacement" and "timetable_review" in requested:
        owners.add("improve_current_timetable")
    if outcome == "course_replacement" and any(
        str(call.get("name") or "") == "graduation_progress"
        and isinstance(call.get("arguments"), Mapping)
        and (call.get("arguments") or {}).get("search_better_replacements") is True
        for call in calls
    ):
        owners.add("graduation_progress")
    return frozenset(owners)


# V2 exposed this aggregate, while the launch-safe V2.1 planner deliberately does
# not. It remains valid only in ``forbidden_tools`` so the comparative scorer can
# detect a broad-context baseline call without advertising it to V2.1.
BASELINE_ONLY_FORBIDDEN_TOOLS = frozenset({"get_student_context"})
SCORE_DIMENSIONS = (
    "mode_correct",
    "clarification_kind_correct",
    "outcomes_correct",
    "outcome_coverage_correct",
    "semantic_policy_correct",
    "required_tools_correct",
    "forbidden_tools_correct",
    "tool_minimality_correct",
    "arguments_correct",
)
# V2 does not emit typed requested_outcomes. Comparative lift must therefore use
# only dimensions observable on both systems; otherwise adding the V2.1 outcome
# field would mechanically drive the V2 baseline exact-match rate to zero. The
# candidate still has to pass every SCORE_DIMENSIONS check and both 100% outcome
# gates above, so this does not weaken V2.1 acceptance.
COMMON_COMPARISON_DIMENSIONS = (
    "mode_correct",
    "required_tools_correct",
    "forbidden_tools_correct",
    "tool_minimality_correct",
    "arguments_correct",
)
_CASE_ID = re.compile(r"V21-SP-\d{3}\Z")
_LONG_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d{7,}(?![A-Za-z0-9])")
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_POLICY_COURSE_CODE = re.compile(r"[A-Z]{2,8}[0-9]{2,4}[A-Z]?\Z")
_POLICY_SECTION_LABEL = re.compile(r"[A-Z][0-9]{1,3}\Z")


class ContractValidationError(ValueError):
    """The versioned evaluation contract is malformed or internally inconsistent."""


class ResultValidationError(ValueError):
    """A result batch cannot be compared deterministically with the contract."""


def question_fingerprint(value: str) -> str:
    """Return a stable exact-utterance fingerprint for holdout-overlap checks."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{path} must be an object")
    return value


def _string_list(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ContractValidationError(f"{path} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ContractValidationError(f"{path} must not contain duplicates")
    return list(value)


def _role_history(value: Any, *, path: str) -> list[Mapping[str, str]]:
    """Validate history in the same bounded role/content shape production accepts."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        raise ContractValidationError(f"{path} must be a list of at most 8 role messages")
    history: list[Mapping[str, str]] = []
    for index, raw_message in enumerate(value):
        message = _mapping(raw_message, path=f"{path}[{index}]")
        if set(message) != {"role", "content"}:
            raise ContractValidationError(f"{path}[{index}] must contain only role and content")
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"}:
            raise ContractValidationError(f"{path}[{index}].role must be user or assistant")
        if (
            not isinstance(content, str)
            or not content
            or content != content.strip()
            or len(content) > 3000
        ):
            raise ContractValidationError(
                f"{path}[{index}].content must be a trimmed non-empty string of at most 3000 characters"
            )
        history.append({"role": str(role), "content": content})
    return history


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 6) if total else 0.0


def _validate_gate(meta: Mapping[str, Any]) -> None:
    gate = _mapping(meta.get("quality_gate"), path="meta.quality_gate")
    rate_fields = (
        "candidate_min_exact_match_rate",
        "candidate_min_unseen_paraphrase_rate",
        "candidate_min_regex_false_positive_rate",
        "candidate_min_required_tools_rate",
        "candidate_min_forbidden_tools_rate",
        "candidate_min_outcomes_rate",
        "candidate_min_outcome_coverage_rate",
        "candidate_min_semantic_policy_rate",
        "candidate_min_clarification_kind_rate",
        "minimum_absolute_lift_vs_v2",
    )
    for field in rate_fields:
        value = gate.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
            raise ContractValidationError(f"meta.quality_gate.{field} must be between 0 and 1")
    if not isinstance(gate.get("no_dimension_regressions"), bool):
        raise ContractValidationError(
            "meta.quality_gate.no_dimension_regressions must be a boolean"
        )


def validate_contract(contract: Mapping[str, Any]) -> None:
    """Validate the complete V2.1 contract, including privacy and pair invariants."""

    root = _mapping(contract, path="contract")
    meta = _mapping(root.get("meta"), path="meta")
    if meta.get("name") != CONTRACT_NAME:
        raise ContractValidationError(f"meta.name must be {CONTRACT_NAME!r}")
    if str(meta.get("version")) != CONTRACT_VERSION:
        raise ContractValidationError(f"meta.version must be {CONTRACT_VERSION!r}")

    modes = set(_string_list(meta.get("modes"), path="meta.modes"))
    if modes != VALID_MODES:
        raise ContractValidationError("meta.modes must exactly match the V2.1 planner decisions")
    tool_surface = _string_list(meta.get("tool_surface"), path="meta.tool_surface")
    if tuple(tool_surface) != V21_TOOL_SURFACE:
        raise ContractValidationError(
            "meta.tool_surface must match the audited V2 capability surface"
        )
    if tuple(_string_list(meta.get("scoring_dimensions"), path="meta.scoring_dimensions")) != (
        SCORE_DIMENSIONS
    ):
        raise ContractValidationError("meta.scoring_dimensions must match the deterministic scorer")
    forbidden_model_arguments = set(
        _string_list(
            meta.get("forbidden_model_arguments"),
            path="meta.forbidden_model_arguments",
        )
    )
    _validate_gate(meta)

    cases = root.get("cases")
    if not isinstance(cases, list):
        raise ContractValidationError("cases must be a list")
    if not 20 <= len(cases) <= 40:
        raise ContractValidationError("the focused contract must contain between 20 and 40 cases")
    if meta.get("count") != len(cases):
        raise ContractValidationError("meta.count must equal the number of cases")

    languages = set(_string_list(meta.get("languages"), path="meta.languages"))
    ids: set[str] = set()
    questions: set[str] = set()
    pair_members: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    type_counts: Counter[str] = Counter()

    required_fields = {
        "id",
        "case_type",
        "language",
        "question",
        "expected_goal",
        "expected_outcomes",
        "expected_mode",
        "required_tools",
        "required_any",
        "allowed_tools",
        "forbidden_tools",
        "required_arguments",
        "novelty_note",
    }
    for index, raw_case in enumerate(cases):
        path = f"cases[{index}]"
        case = _mapping(raw_case, path=path)
        missing = sorted(required_fields - set(case))
        if missing:
            raise ContractValidationError(f"{path} is missing required fields: {missing}")
        if "context" in case:
            raise ContractValidationError(
                f"{path}.context is not a role message; use history instead"
            )

        case_id = case.get("id")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise ContractValidationError(f"{path}.id must match V21-SP-NNN")
        if case_id in ids:
            raise ContractValidationError(f"duplicate case id: {case_id}")
        ids.add(case_id)

        case_type = case.get("case_type")
        if case_type not in VALID_CASE_TYPES:
            raise ContractValidationError(f"{case_id}.case_type is not recognized")
        type_counts[str(case_type)] += 1
        if case.get("language") not in languages:
            raise ContractValidationError(f"{case_id}.language is not declared by meta.languages")

        question = case.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ContractValidationError(f"{case_id}.question must be a non-empty string")
        fingerprint = question_fingerprint(question)
        if fingerprint in questions:
            raise ContractValidationError(f"duplicate question in {case_id}")
        questions.add(fingerprint)
        policy_pins = case.get("policy_explicit_pins")
        if policy_pins is not None:
            if not isinstance(policy_pins, list) or not 1 <= len(policy_pins) <= 8:
                raise ContractValidationError(
                    f"{case_id}.policy_explicit_pins must be a bounded non-empty list"
                )
            normalized_policy_pins: list[tuple[str, str]] = []
            for pin_index, pin in enumerate(policy_pins):
                if not isinstance(pin, Mapping) or set(pin) != {
                    "course_code",
                    "section_label",
                }:
                    raise ContractValidationError(
                        f"{case_id}.policy_explicit_pins[{pin_index}] has invalid fields"
                    )
                course_code = str(pin.get("course_code") or "")
                section_label = str(pin.get("section_label") or "")
                if not _POLICY_COURSE_CODE.fullmatch(
                    course_code
                ) or not _POLICY_SECTION_LABEL.fullmatch(section_label):
                    raise ContractValidationError(
                        f"{case_id}.policy_explicit_pins[{pin_index}] has invalid literals"
                    )
                normalized_policy_pins.append((course_code, section_label))
            if len(normalized_policy_pins) != len(set(normalized_policy_pins)):
                raise ContractValidationError(
                    f"{case_id}.policy_explicit_pins must not contain duplicates"
                )
        history = _role_history(case.get("history"), path=f"{case_id}.history")
        text_for_privacy = " ".join(
            (
                question,
                *(message["content"] for message in history),
                str(case.get("novelty_note") or ""),
            )
        )
        if _LONG_NUMBER.search(text_for_privacy) or _EMAIL.search(text_for_privacy):
            raise ContractValidationError(f"{case_id} contains possible PII")

        if not isinstance(case.get("expected_goal"), str) or not case["expected_goal"].strip():
            raise ContractValidationError(f"{case_id}.expected_goal must describe the case")
        outcomes = _string_list(case.get("expected_outcomes"), path=f"{case_id}.expected_outcomes")
        unknown_outcomes = set(outcomes) - VALID_OUTCOMES
        if unknown_outcomes:
            raise ContractValidationError(
                f"{case_id}.expected_outcomes has unknown values: {sorted(unknown_outcomes)}"
            )
        mode = case.get("expected_mode")
        if mode not in VALID_MODES:
            raise ContractValidationError(f"{case_id}.expected_mode is not recognized")
        expected_clarification_kind = str(case.get("expected_clarification_kind", "none") or "")
        if expected_clarification_kind not in VALID_CLARIFICATION_KINDS:
            raise ContractValidationError(
                f"{case_id}.expected_clarification_kind is not recognized"
            )
        if mode == "clarify" and expected_clarification_kind == "none":
            raise ContractValidationError(
                f"{case_id} clarify mode requires a non-none clarification kind"
            )
        if mode != "clarify" and expected_clarification_kind != "none":
            raise ContractValidationError(
                f"{case_id} non-clarify mode requires clarification kind none"
            )
        novelty = case.get("novelty_note")
        if not isinstance(novelty, str) or len(novelty.strip()) < 20:
            raise ContractValidationError(f"{case_id}.novelty_note is too weak")

        required = set(_string_list(case.get("required_tools"), path=f"{case_id}.required_tools"))
        allowed = set(_string_list(case.get("allowed_tools"), path=f"{case_id}.allowed_tools"))
        forbidden = set(
            _string_list(case.get("forbidden_tools"), path=f"{case_id}.forbidden_tools")
        )
        for label, values in (("required", required), ("allowed", allowed)):
            unknown = values - set(V21_TOOL_SURFACE)
            if unknown:
                raise ContractValidationError(
                    f"{case_id}.{label}_tools has unknown tools: {sorted(unknown)}"
                )
        unknown_forbidden = forbidden - (set(V21_TOOL_SURFACE) | BASELINE_ONLY_FORBIDDEN_TOOLS)
        if unknown_forbidden:
            raise ContractValidationError(
                f"{case_id}.forbidden_tools has unknown tools: {sorted(unknown_forbidden)}"
            )
        if not required <= allowed:
            raise ContractValidationError(f"{case_id}.required_tools must be allowed")
        if allowed & forbidden:
            raise ContractValidationError(f"{case_id} cannot both allow and forbid a tool")

        outcome_set = set(outcomes)
        if mode == "direct" and outcomes != ["general_conversation"]:
            raise ContractValidationError(f"{case_id}.direct must expect only general_conversation")
        if mode == "unsupported" and (not outcome_set or not outcome_set <= UNSUPPORTED_OUTCOMES):
            raise ContractValidationError(
                f"{case_id}.unsupported must expect only typed unsupported outcomes"
            )
        if mode == "execute" and (
            outcome_set & EXECUTE_FORBIDDEN_OUTCOMES
            or not outcome_set - SERVER_OWNED_EXECUTE_OUTCOMES
        ):
            raise ContractValidationError(
                f"{case_id}.execute must expect an evidence-backed outcome and may "
                "combine it only with registration_action and/or credit_load_comparison"
            )
        if mode == "clarify" and outcome_set & (UNSUPPORTED_OUTCOMES | {"general_conversation"}):
            raise ContractValidationError(
                f"{case_id}.clarify cannot expect direct/unsupported outcomes"
            )

        raw_groups = case.get("required_any")
        if not isinstance(raw_groups, list):
            raise ContractValidationError(f"{case_id}.required_any must be a list")
        groups: list[set[str]] = []
        for group_index, raw_group in enumerate(raw_groups):
            group = set(_string_list(raw_group, path=f"{case_id}.required_any[{group_index}]"))
            if not group:
                raise ContractValidationError(f"{case_id}.required_any groups cannot be empty")
            if not group <= allowed:
                raise ContractValidationError(f"{case_id}.required_any tools must be allowed")
            groups.append(group)

        required_arguments = _mapping(
            case.get("required_arguments"), path=f"{case_id}.required_arguments"
        )
        if not set(required_arguments) <= allowed:
            raise ContractValidationError(
                f"{case_id}.required_arguments may only name allowed tools"
            )
        for tool, arguments in required_arguments.items():
            _mapping(arguments, path=f"{case_id}.required_arguments.{tool}")

        local_forbidden_arguments = case.get("forbidden_arguments", [])
        local_forbidden = set(
            _string_list(local_forbidden_arguments, path=f"{case_id}.forbidden_arguments")
        )
        if local_forbidden & forbidden_model_arguments:
            raise ContractValidationError(
                f"{case_id}.forbidden_arguments duplicates the global contract"
            )

        if mode == "execute" and not (required or groups):
            raise ContractValidationError(f"{case_id} execute mode must require evidence")
        if mode != "execute" and (required or groups or allowed or required_arguments):
            raise ContractValidationError(f"{case_id} non-execute mode cannot permit tool calls")

        pair_id = case.get("pair_id")
        if pair_id is not None:
            if not isinstance(pair_id, str) or not pair_id.strip():
                raise ContractValidationError(f"{case_id}.pair_id must be a non-empty string")
            pair_members[pair_id].append(case)

    if type_counts["unseen_paraphrase"] < 10:
        raise ContractValidationError("the contract needs at least ten unseen paraphrases")
    if type_counts["regex_false_positive"] < 8:
        raise ContractValidationError("the contract needs at least eight regex false positives")

    for pair_id, pair in pair_members.items():
        if len(pair) != 2:
            raise ContractValidationError(f"pair {pair_id!r} must contain exactly two cases")
        signatures = {
            (
                case["expected_mode"],
                tuple(case["required_tools"]),
                tuple(tuple(group) for group in case["required_any"]),
            )
            for case in pair
        }
        if len(signatures) != 2:
            raise ContractValidationError(f"pair {pair_id!r} must require different plans")


def load_contract(path: str | pathlib.Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    """Load and validate a semantic-plan contract from YAML."""

    contract_path = pathlib.Path(path)
    with contract_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    validate_contract(loaded)
    return dict(loaded)


def contract_by_id(contract: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return the validated cases indexed by stable case id."""

    active = dict(contract) if contract is not None else load_contract()
    validate_contract(active)
    return {str(case["id"]): dict(case) for case in active["cases"]}


def _object_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _decision_value(value: Any) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value or "").strip().lower()


def _normalise_arguments(value: Any) -> tuple[dict[str, Any], str]:
    if value is None:
        return {}, ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            return {}, "tool arguments are not valid JSON"
    if not isinstance(value, Mapping):
        return {}, "tool arguments are not an object"
    return dict(value), ""


def _normalise_plan(
    plan: Any,
) -> tuple[str, str, list[str], list[dict[str, Any]], list[str]]:
    """Normalize runtime dataclasses, eval JSON, or provider-style function calls."""

    mode = _decision_value(_object_field(plan, "decision", _object_field(plan, "mode", "")))
    clarification_kind = _decision_value(_object_field(plan, "clarification_kind", ""))
    raw_outcomes = _object_field(
        plan,
        "requested_outcomes",
        _object_field(plan, "outcomes", []),
    )
    outcome_errors: list[str] = []
    outcomes: list[str] = []
    if not isinstance(raw_outcomes, Sequence) or isinstance(raw_outcomes, str | bytes):
        outcome_errors.append("requested outcomes must be a list")
    else:
        for index, raw_outcome in enumerate(raw_outcomes):
            outcome = _decision_value(raw_outcome)
            if not outcome:
                outcome_errors.append(f"requested outcome {index} is empty")
            elif outcome not in VALID_OUTCOMES:
                outcome_errors.append(f"requested outcome {index} is not recognized")
            else:
                outcomes.append(outcome)
        if len(outcomes) != len(set(outcomes)):
            outcome_errors.append("requested outcomes must not contain duplicates")
    requests = _object_field(plan, "evidence_requests", None)
    if requests is None:
        requests = _object_field(plan, "tool_calls", [])
    if not isinstance(requests, Sequence) or isinstance(requests, str | bytes):
        return (
            mode,
            clarification_kind,
            outcomes,
            [],
            [*outcome_errors, "tool calls must be a list"],
        )

    calls: list[dict[str, Any]] = []
    errors: list[str] = list(outcome_errors)
    for index, raw_call in enumerate(requests):
        function = _object_field(raw_call, "function", None)
        source = function if function is not None else raw_call
        name = _object_field(source, "capability", _object_field(source, "name", ""))
        name = str(name or "").strip()
        arguments, argument_error = _normalise_arguments(_object_field(source, "arguments", {}))
        if not name:
            errors.append(f"tool call {index} has no capability name")
        if argument_error:
            errors.append(f"tool call {index}: {argument_error}")
        calls.append({"name": name, "arguments": arguments})
    return mode, clarification_kind, outcomes, calls, errors


def _outcome_coverage_correct(
    *,
    mode: str,
    outcomes: Sequence[str],
    calls: Sequence[Mapping[str, Any]],
    errors: Sequence[str],
) -> bool:
    """Mirror the closed runtime outcome/capability launch postcondition."""

    if errors or not outcomes or len(outcomes) != len(set(outcomes)):
        return False
    tools = [str(call.get("name") or "") for call in calls]
    outcome_set = frozenset(outcomes)
    tool_set = frozenset(tools)
    if mode == "direct":
        return outcome_set == {"general_conversation"} and not tools
    if mode == "unsupported":
        return bool(outcome_set) and outcome_set <= UNSUPPORTED_OUTCOMES and not tools
    if mode == "clarify":
        return not tools and not outcome_set & (UNSUPPORTED_OUTCOMES | {"general_conversation"})
    if mode != "execute" or not tools:
        return False
    if outcome_set & EXECUTE_FORBIDDEN_OUTCOMES:
        return False
    for call in calls:
        if str(call.get("name") or "") != "graduation_progress":
            continue
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return False
        additions = set(arguments.get("add_current_courses") or [])
        removals = set(arguments.get("remove_current_courses") or [])
        noncompletion = set(arguments.get("noncompletion_current_courses") or [])
        explicit_changes = bool(additions or removals or noncompletion)
        if arguments.get("search_better_replacements") is True and explicit_changes:
            return False
        if noncompletion and (
            arguments.get("planning_baseline_kind") != "registered_timetable"
            or bool(noncompletion & (additions | removals))
        ):
            return False
    covered = all(
        outcome in SERVER_OWNED_EXECUTE_OUTCOMES
        or bool(_owners_for_outcome(outcome, outcome_set, calls) & tool_set)
        for outcome in outcomes
    )
    justified = frozenset(
        tool for outcome in outcomes for tool in _owners_for_outcome(outcome, outcome_set, calls)
    )
    if not covered or not tool_set <= justified or len(tools) != len(tool_set):
        return False
    return all(
        not all(
            outcome in SERVER_OWNED_EXECUTE_OUTCOMES
            or bool(_owners_for_outcome(outcome, outcome_set, calls) & (tool_set - {tool}))
            for outcome in outcomes
        )
        for tool in tools
    )


def _json_scalar_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(type(actual) is type(expected) and actual == expected)
    return bool(actual == expected)


def _partial_match(actual: Any, expected: Any) -> bool:
    """Recursively match expected arguments; list order is semantically irrelevant."""

    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _partial_match(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        unmatched = list(actual)
        for expected_item in expected:
            for index, actual_item in enumerate(unmatched):
                if _partial_match(actual_item, expected_item):
                    unmatched.pop(index)
                    break
            else:
                return False
        return True
    return _json_scalar_equal(actual, expected)


def _nested_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            keys.add(str(key))
            keys.update(_nested_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_nested_keys(nested))
    return keys


def score_case(
    case: Mapping[str, Any],
    actual_plan: Any,
    *,
    forbidden_model_arguments: Sequence[str] = ("student_id",),
) -> dict[str, Any]:
    """Score one observable plan against one contract case."""

    mode, clarification_kind, outcomes, calls, errors = _normalise_plan(actual_plan)
    names = [str(call["name"]) for call in calls]
    name_set = set(names)
    required = set(case.get("required_tools") or [])
    required_any = [set(group) for group in case.get("required_any") or []]
    allowed = set(case.get("allowed_tools") or [])
    forbidden = set(case.get("forbidden_tools") or [])

    mode_correct = mode == case.get("expected_mode")
    clarification_kind_correct = clarification_kind == str(
        case.get("expected_clarification_kind", "none")
    )
    # Outcomes are a unique closed set of requested deliverables. Their JSON
    # order has no runtime meaning, and coverage validation below independently
    # rejects duplicates, so scoring must not manufacture a failure from order.
    outcomes_correct = frozenset(outcomes) == frozenset(case.get("expected_outcomes") or [])
    outcome_coverage_correct = _outcome_coverage_correct(
        mode=mode,
        outcomes=outcomes,
        calls=calls,
        errors=errors,
    )
    policy_violations = semantic_policy_violations(
        str(case.get("question") or ""),
        {
            "decision": mode,
            "requested_outcomes": outcomes,
            "evidence_requests": calls,
        },
        explicit_pins=case.get(
            "_semantic_policy_explicit_pins",
            case.get("policy_explicit_pins"),
        ),
    )
    semantic_policy_correct = not policy_violations
    required_tools_correct = required <= name_set and all(
        name_set & group for group in required_any
    )
    forbidden_tools_correct = not bool(name_set & forbidden)
    tool_minimality_correct = (
        not errors
        and name_set <= allowed
        and len(names) == len(name_set)
        and (case.get("expected_mode") == "execute" or not names)
    )

    forbidden_arguments = set(forbidden_model_arguments) | set(
        case.get("forbidden_arguments") or []
    )
    arguments_correct = not errors and not any(
        _nested_keys(call["arguments"]) & forbidden_arguments for call in calls
    )
    expected_arguments = case.get("required_arguments") or {}
    for tool, expected in expected_arguments.items():
        arguments_correct = arguments_correct and any(
            call["name"] == tool and _partial_match(call["arguments"], expected) for call in calls
        )

    dimensions = {
        "mode_correct": mode_correct,
        "clarification_kind_correct": clarification_kind_correct,
        "outcomes_correct": outcomes_correct,
        "outcome_coverage_correct": outcome_coverage_correct,
        "semantic_policy_correct": semantic_policy_correct,
        "required_tools_correct": required_tools_correct,
        "forbidden_tools_correct": forbidden_tools_correct,
        "tool_minimality_correct": tool_minimality_correct,
        "arguments_correct": arguments_correct,
    }
    common_comparison_overall = all(
        dimensions[dimension] for dimension in COMMON_COMPARISON_DIMENSIONS
    )
    return {
        "case_id": case["id"],
        "case_type": case["case_type"],
        "language": case["language"],
        "expected_goal": case["expected_goal"],
        "expected": {
            "mode": case["expected_mode"],
            "clarification_kind": case.get("expected_clarification_kind", "none"),
            "outcomes": list(case.get("expected_outcomes") or []),
            "required_tools": list(case.get("required_tools") or []),
            "required_any": list(case.get("required_any") or []),
            "allowed_tools": list(case.get("allowed_tools") or []),
            "forbidden_tools": list(case.get("forbidden_tools") or []),
            "required_arguments": dict(expected_arguments),
        },
        "actual": {
            "mode": mode,
            "clarification_kind": clarification_kind,
            "requested_outcomes": outcomes,
            "tool_calls": calls,
            "normalisation_errors": errors,
            "semantic_policy_violations": [violation.value for violation in policy_violations],
        },
        "dimensions": dimensions,
        "common_comparison_overall": common_comparison_overall,
        "overall": all(dimensions.values()),
    }


def _result_rows(results: Any) -> list[tuple[str, Any]]:
    if isinstance(results, Mapping) and "rows" in results:
        results = results["rows"]
    elif isinstance(results, Mapping):
        return [(str(case_id), plan) for case_id, plan in results.items()]
    if not isinstance(results, list):
        raise ResultValidationError("results must be an id-to-plan object or a rows list")

    rows: list[tuple[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, Mapping):
            raise ResultValidationError(f"results row {index} must be an object")
        case_id = row.get("case_id", row.get("id"))
        if not isinstance(case_id, str) or not case_id:
            raise ResultValidationError(f"results row {index} has no case_id")
        plan = row.get("plan")
        if plan is None:
            plan = {key: value for key, value in row.items() if key not in {"case_id", "id"}}
        rows.append((case_id, plan))
    return rows


def _breakdown(
    scored: Sequence[Mapping[str, Any]],
    field: str,
    *,
    pass_field: str = "overall",
) -> dict[str, dict[str, Any]]:
    values: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in scored:
        values[str(row[field])].append(row)
    return {
        value: {
            "passed": sum(bool(row[pass_field]) for row in rows),
            "total": len(rows),
            "rate": _rate(sum(bool(row[pass_field]) for row in rows), len(rows)),
        }
        for value, rows in sorted(values.items())
    }


def score_batch(
    results: Any,
    contract: Mapping[str, Any] | None = None,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Score a unique result per case and return deterministic aggregate metrics."""

    active = dict(contract) if contract is not None else load_contract()
    validate_contract(active)
    cases = {str(case["id"]): case for case in active["cases"]}
    rows = _result_rows(results)
    row_ids = [case_id for case_id, _plan in rows]
    duplicates = sorted(case_id for case_id, count in Counter(row_ids).items() if count > 1)
    if duplicates:
        raise ResultValidationError(f"duplicate result ids: {duplicates}")
    unknown = sorted(set(row_ids) - set(cases))
    if unknown:
        raise ResultValidationError(f"unknown result ids: {unknown}")
    missing = sorted(set(cases) - set(row_ids))
    if require_complete and missing:
        raise ResultValidationError(f"missing result ids: {missing}")

    plans = dict(rows)
    forbidden_arguments = active["meta"]["forbidden_model_arguments"]
    scored = [
        score_case(
            cases[case_id],
            plans[case_id],
            forbidden_model_arguments=forbidden_arguments,
        )
        for case_id in cases
        if case_id in plans
    ]
    dimensions = {
        dimension: {
            "passed": sum(bool(row["dimensions"][dimension]) for row in scored),
            "total": len(scored),
            "rate": _rate(sum(bool(row["dimensions"][dimension]) for row in scored), len(scored)),
        }
        for dimension in SCORE_DIMENSIONS
    }
    passed = sum(bool(row["overall"]) for row in scored)
    common_passed = sum(bool(row["common_comparison_overall"]) for row in scored)
    return {
        "contract": {"name": CONTRACT_NAME, "version": CONTRACT_VERSION},
        "coverage": {
            "scored": len(scored),
            "contract_cases": len(cases),
            "complete": not missing,
            "missing": missing,
        },
        "passed": passed,
        "total": len(scored),
        "exact_match_rate": _rate(passed, len(scored)),
        "common_comparison_passed": common_passed,
        "common_comparison_exact_match_rate": _rate(common_passed, len(scored)),
        "all_passed": bool(scored) and passed == len(scored) and not missing,
        "dimensions": dimensions,
        "by_case_type": _breakdown(scored, "case_type"),
        "by_language": _breakdown(scored, "language"),
        "common_comparison_by_case_type": _breakdown(
            scored,
            "case_type",
            pass_field="common_comparison_overall",
        ),
        "common_comparison_by_language": _breakdown(
            scored,
            "language",
            pass_field="common_comparison_overall",
        ),
        "rows": scored,
    }


def candidate_quality_gate(
    report: Mapping[str, Any], contract: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Apply the absolute V2.1 candidate thresholds without a baseline report."""

    active = dict(contract) if contract is not None else load_contract()
    validate_contract(active)
    gate = active["meta"]["quality_gate"]
    by_type = report.get("by_case_type") or {}
    dimensions = report.get("dimensions") or {}
    checks = {
        "complete_contract": bool((report.get("coverage") or {}).get("complete")),
        "minimum_exact_match": float(report.get("exact_match_rate") or 0)
        >= gate["candidate_min_exact_match_rate"],
        "minimum_unseen_paraphrase": float(
            (by_type.get("unseen_paraphrase") or {}).get("rate") or 0
        )
        >= gate["candidate_min_unseen_paraphrase_rate"],
        "minimum_regex_false_positive": float(
            (by_type.get("regex_false_positive") or {}).get("rate") or 0
        )
        >= gate["candidate_min_regex_false_positive_rate"],
        "minimum_required_tools": float(
            (dimensions.get("required_tools_correct") or {}).get("rate") or 0
        )
        >= gate["candidate_min_required_tools_rate"],
        "minimum_forbidden_tools": float(
            (dimensions.get("forbidden_tools_correct") or {}).get("rate") or 0
        )
        >= gate["candidate_min_forbidden_tools_rate"],
        "minimum_outcomes": float((dimensions.get("outcomes_correct") or {}).get("rate") or 0)
        >= gate["candidate_min_outcomes_rate"],
        "minimum_outcome_coverage": float(
            (dimensions.get("outcome_coverage_correct") or {}).get("rate") or 0
        )
        >= gate["candidate_min_outcome_coverage_rate"],
        "minimum_semantic_policy": float(
            (dimensions.get("semantic_policy_correct") or {}).get("rate") or 0
        )
        >= gate["candidate_min_semantic_policy_rate"],
        "minimum_clarification_kind": float(
            (dimensions.get("clarification_kind_correct") or {}).get("rate") or 0
        )
        >= gate["candidate_min_clarification_kind_rate"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def compare_quality_gate(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require absolute quality, measurable lift over V2, and no metric regression."""

    active = dict(contract) if contract is not None else load_contract()
    validate_contract(active)
    gate = active["meta"]["quality_gate"]
    candidate_gate = candidate_quality_gate(candidate, active)
    candidate_rate = float(candidate.get("common_comparison_exact_match_rate") or 0)
    baseline_rate = float(baseline.get("common_comparison_exact_match_rate") or 0)
    lift = round(candidate_rate - baseline_rate, 6)
    dimensions_not_regressed = all(
        float((candidate.get("dimensions") or {}).get(dimension, {}).get("rate") or 0)
        >= float((baseline.get("dimensions") or {}).get(dimension, {}).get("rate") or 0)
        for dimension in COMMON_COMPARISON_DIMENSIONS
    )
    comparison_checks = {
        "candidate_absolute_gate": candidate_gate["passed"],
        "baseline_complete": bool((baseline.get("coverage") or {}).get("complete")),
        "minimum_absolute_lift_vs_v2": lift >= gate["minimum_absolute_lift_vs_v2"],
        "no_dimension_regressions": (
            dimensions_not_regressed if gate["no_dimension_regressions"] else True
        ),
    }
    return {
        "passed": all(comparison_checks.values()),
        "candidate_rate": candidate_rate,
        "baseline_rate": baseline_rate,
        "absolute_lift": lift,
        "comparison_metric": "common_comparison_exact_match_rate",
        "common_comparison_dimensions": list(COMMON_COMPARISON_DIMENSIONS),
        "candidate_typed_exact_match_rate": float(candidate.get("exact_match_rate") or 0),
        "baseline_typed_exact_match_rate": float(baseline.get("exact_match_rate") or 0),
        "checks": comparison_checks,
        "candidate_checks": candidate_gate["checks"],
    }


def _load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", nargs="?", type=pathlib.Path)
    parser.add_argument("--baseline", type=pathlib.Path)
    parser.add_argument("--contract", type=pathlib.Path, default=DEFAULT_CONTRACT_PATH)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    contract = load_contract(args.contract)
    if args.candidate is None:
        if args.baseline is not None:
            parser.error("--baseline requires a candidate result file")
        output: dict[str, Any] = {
            "contract": {"name": CONTRACT_NAME, "version": CONTRACT_VERSION},
            "cases": len(contract["cases"]),
            "valid": True,
        }
        passed = True
    else:
        candidate = score_batch(_load_json(args.candidate), contract)
        output = {
            "candidate": candidate,
            "candidate_gate": candidate_quality_gate(candidate, contract),
        }
        passed = bool(output["candidate_gate"]["passed"])
        if args.baseline is not None:
            baseline = score_batch(_load_json(args.baseline), contract)
            output["baseline"] = baseline
            output["comparison_gate"] = compare_quality_gate(candidate, baseline, contract)
            passed = bool(output["comparison_gate"]["passed"])

    rendered = json.dumps(
        output,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=True,
    )
    try:
        print(rendered)
    except UnicodeEncodeError:
        print(
            json.dumps(
                output,
                ensure_ascii=True,
                indent=None if args.compact else 2,
                sort_keys=True,
            )
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
