"""Run and report the versioned V2.1 semantic-plan evaluation.

Offline replay is the default and makes no provider calls::

    python evals/advisor/run_v21_semantic_plan.py \
        --candidate runtime/evals/v21-candidate.json \
        --baseline runtime/evals/v2-baseline.json \
        --output runtime/evals/v21-comparison.json

Candidate rows are reparsed through the real ``StudentTurnPlan`` validator and the
actual V2.1 capability schemas before they are scored.  A baseline is explicit: this
runner never invents a V2 score or silently substitutes the keyword mock provider.

Live planning is opt-in and bounded by limits the operator must supply::

    python evals/advisor/run_v21_semantic_plan.py --live \
        --confirm-live-external-request --max-provider-calls 72 \
        --max-total-tokens 2000000 --baseline runtime/evals/v2-baseline.json

The live path invokes only the typed semantic planner. It does not access a student
record, execute evidence tools, or generate final adviser answers.

An explicit V2 approximation can be collected separately, with the same opt-in and
budget controls::

    python evals/advisor/run_v21_semantic_plan.py --collect-v2-baseline \
        --confirm-live-external-request --max-provider-calls 36 \
        --max-total-tokens 3000000 --output runtime/evals/v2-baseline.json

That artifact records one real V2 model turn plus the current deterministic evidence
gates. Its embedded limitations are part of the report and must travel with the score.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.services.student_advisor_v21_policy import (  # noqa: E402
    SEMANTIC_POLICY_IDS,
    semantic_policy_violations,
)
from core.services.student_advisor_v21_prompt import (  # noqa: E402
    build_student_v21_planner_messages,
)
from evals.advisor.v21_semantic_plan_eval import (  # noqa: E402
    candidate_quality_gate,
    compare_quality_gate,
    load_contract,
    score_batch,
)

DEFAULT_MAX_PLAN_TOKENS = 900
DEFAULT_V2_BASELINE_MAX_TOKENS = 1800
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_EVIDENCE_CALLS = 4


class SemanticPolicyReplayError(ValueError):
    """A schema-valid offline/live plan failed the production V2.1 policy seam."""


def _production_explicit_pins(question: str) -> list[dict[str, str]]:
    """Use the production final-active reducer; never infer pins from a plan."""

    from core.services.student_advisor_v2 import _v21_explicit_positive_pins

    return _v21_explicit_positive_pins(question)


def _contract_with_semantic_policy_context(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach transient production-derived context used only during evaluation."""

    active = dict(contract)
    decorated_cases: list[dict[str, Any]] = []
    for raw_case in contract.get("cases", ()):
        case = dict(raw_case)
        derived = _production_explicit_pins(str(case.get("question") or ""))
        declared = case.get("policy_explicit_pins")
        if declared is not None:
            normalized_declared = sorted(
                (
                    str(pin.get("course_code") or "").upper(),
                    str(pin.get("section_label") or "").upper(),
                )
                for pin in declared
                if isinstance(pin, Mapping)
            )
            normalized_derived = sorted(
                (
                    str(pin.get("course_code") or "").upper(),
                    str(pin.get("section_label") or "").upper(),
                )
                for pin in derived
            )
            if normalized_declared != normalized_derived:
                raise ValueError("semantic policy pin context differs from the production reducer")
        case["_semantic_policy_explicit_pins"] = derived
        decorated_cases.append(case)
    active["cases"] = decorated_cases
    return active


V2_BASELINE_LIMITATIONS = (
    "One provider turn is observed; evidence tools, tool-result feedback, bounded repair turns, "
    "and final-answer validation are intentionally not run.",
    "Rows combine observed first-turn model calls with current V2 deterministic evidence "
    "obligations. A gate-only call records an obligation the runtime would repair toward, not an "
    "observed model selection.",
    "No student record is loaded and no governing policy payload is sent to the model. The "
    "current deterministic policy-intent gate may consult its local topic index; the effect of "
    "prefetched policy text is outside this planner-only baseline.",
    "V2 has no typed clarification decision. A first turn with no evidence call is represented "
    "as direct even if its unrecorded prose might have asked a question.",
    "V2 does not emit typed requested_outcomes. Comparative lift is therefore computed over the "
    "mode/tool/minimality/argument dimensions observable on both systems; V2.1 separately must "
    "pass the strict typed-outcome correctness and coverage gates.",
    "The comparison measures evidence planning only; it is not an answer-quality, grounding, or "
    "end-to-end adviser baseline.",
    "The artifact records model and revision per row. Provider sampling and deployment drift remain; "
    "a candidate comparison should use the same provider snapshot and explicit budgets.",
)


@dataclass(frozen=True)
class LiveLimits:
    """Hard operator-selected ceilings for the opt-in provider path."""

    max_provider_calls: int
    max_total_tokens: int
    max_plan_tokens: int = DEFAULT_MAX_PLAN_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_evidence_calls: int = DEFAULT_MAX_EVIDENCE_CALLS

    def validate(self, *, case_count: int) -> None:
        if (
            isinstance(self.max_provider_calls, bool)
            or not isinstance(self.max_provider_calls, int)
            or not 1 <= self.max_provider_calls <= case_count * 2
        ):
            raise ValueError(f"max_provider_calls must be between 1 and {case_count * 2}")
        if (
            isinstance(self.max_total_tokens, bool)
            or not isinstance(self.max_total_tokens, int)
            or self.max_total_tokens < 1
        ):
            raise ValueError("max_total_tokens must be a positive integer")
        if (
            isinstance(self.max_plan_tokens, bool)
            or not isinstance(self.max_plan_tokens, int)
            or not 128 <= self.max_plan_tokens <= 2000
        ):
            raise ValueError("max_plan_tokens must be between 128 and 2000")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise ValueError("timeout_seconds must be between 1 and 60")
        if (
            isinstance(self.max_evidence_calls, bool)
            or not isinstance(self.max_evidence_calls, int)
            or not 1 <= self.max_evidence_calls <= 8
        ):
            raise ValueError("max_evidence_calls must be between 1 and 8")


def _prepare_bounded_live_client(
    client: Any,
    *,
    max_tokens: int,
    timeout_seconds: float,
) -> tuple[int, int]:
    """Disable transport retries on one fresh eval client and snapshot counters."""

    if not hasattr(client, "http_calls") or not hasattr(client, "http_responses"):
        raise ValueError("the live eval client does not expose HTTP attempt counters")
    initial_calls = client.http_calls
    initial_responses = client.http_responses
    if (
        isinstance(initial_calls, bool)
        or not isinstance(initial_calls, int)
        or isinstance(initial_responses, bool)
        or not isinstance(initial_responses, int)
        or initial_calls != 0
        or initial_responses != 0
    ):
        raise ValueError("the live eval client must be fresh with zero HTTP counters")
    config = getattr(client, "config", None)
    if config is None or not dataclasses.is_dataclass(config):
        raise ValueError("the live eval client has no replaceable bounded config")
    config_updates: dict[str, Any] = {
        "max_retries": 0,
        "max_tokens": min(int(config.max_tokens), int(max_tokens)),
        "timeout_seconds": min(float(config.timeout_seconds), float(timeout_seconds)),
    }
    if hasattr(config, "allow_model_discovery"):
        config_updates["allow_model_discovery"] = False
    client.config = dataclasses.replace(config, **config_updates)
    if int(getattr(client.config, "max_retries", -1)) != 0:
        raise ValueError("the live eval client must have transport retries disabled")
    return initial_calls, initial_responses


def _resolve_bounded_live_model(client: Any, requested_model: str | None) -> str:
    """Resolve only an explicit/configured model without an unbudgeted discovery call."""

    configured = str(getattr(getattr(client, "config", None), "model", "") or "")
    candidate = str(requested_model or configured).strip()
    if not candidate:
        raise ValueError(
            "the live eval requires an explicit --model or a configured non-empty model"
        )
    before = (client.http_calls, client.http_responses)
    resolved = str(client.resolve_model(candidate) or "").strip()
    after = (client.http_calls, client.http_responses)
    if not resolved or after != before:
        raise ValueError("live eval model resolution must not perform HTTP discovery")
    return resolved


def _verified_http_attempt_deltas(
    client: Any,
    *,
    initial: tuple[int, int],
    logical_provider_calls: int,
) -> dict[str, int]:
    """Return transport deltas only when one reservation equalled one attempt."""

    current_calls = getattr(client, "http_calls", None)
    current_responses = getattr(client, "http_responses", None)
    if (
        isinstance(current_calls, bool)
        or not isinstance(current_calls, int)
        or isinstance(current_responses, bool)
        or not isinstance(current_responses, int)
    ):
        raise ValueError("the live eval client lost its HTTP attempt counters")
    calls = current_calls - initial[0]
    responses = current_responses - initial[1]
    if calls != logical_provider_calls or not 0 <= responses <= calls:
        raise ValueError("live eval HTTP attempts differ from reserved provider calls")
    return {"http_calls": calls, "http_responses": responses, "max_retries": 0}


def _runtime_dependencies() -> tuple[Any, Any, Any, Any]:
    """Load Django and the real typed planner only after CLI safety checks."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from core.services.llm_backend import get_llm_client
    from core.services.student_advisor_v2 import student_v21_tool_schemas
    from core.services.student_advisor_v21_plan import (
        parse_turn_plan_result,
        plan_student_turn,
    )

    return get_llm_client, student_v21_tool_schemas, parse_turn_plan_result, plan_student_turn


def _v2_tool_schemas() -> list[dict[str, Any]]:
    """Return V2's broader first-turn surface after Django has been initialized."""

    from core.services.student_advisor_v2 import student_v2_tool_schemas

    return cast(list[dict[str, Any]], student_v2_tool_schemas())


def _plan_mapping(plan: Any) -> dict[str, Any]:
    decision = getattr(getattr(plan, "decision", ""), "value", getattr(plan, "decision", ""))
    clarification_kind = getattr(
        getattr(plan, "clarification_kind", ""),
        "value",
        getattr(plan, "clarification_kind", ""),
    )
    return {
        "decision": str(decision),
        "clarification_kind": str(clarification_kind),
        "requested_outcomes": [
            str(getattr(outcome, "value", outcome))
            for outcome in getattr(plan, "requested_outcomes", ())
        ],
        "evidence_requests": [
            {
                "capability": str(request.capability),
                "arguments": dict(request.arguments),
            }
            for request in getattr(plan, "evidence_requests", ())
        ],
        "clarification_question": str(getattr(plan, "clarification_question", "") or ""),
    }


def _raw_rows(results: Any) -> list[dict[str, Any]]:
    if isinstance(results, Mapping) and "rows" in results:
        results = results["rows"]
    elif isinstance(results, Mapping):
        results = [{"case_id": str(case_id), "plan": plan} for case_id, plan in results.items()]
    if not isinstance(results, list):
        raise ValueError("candidate results must contain a rows list")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, Mapping):
            raise ValueError(f"candidate row {index} must be an object")
        case_id = row.get("case_id", row.get("id"))
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"candidate row {index} has no case_id")
        plan = row.get("plan")
        if not isinstance(plan, Mapping):
            raise ValueError(f"candidate row {case_id} has no plan object")
        normalized: dict[str, Any] = {"case_id": case_id, "plan": dict(plan)}
        if isinstance(row.get("usage"), Mapping):
            normalized["usage"] = dict(row["usage"])
        for key in ("model", "model_revision"):
            if key in row:
                normalized[key] = str(row.get(key) or "")
        repair = row.get("repair")
        if repair is not None:
            if not isinstance(repair, Mapping):
                raise ValueError(f"candidate row {case_id} has an invalid repair object")
            extra_keys = set(repair) - {"attempted", "reason", "policy_ids"}
            attempted = repair.get("attempted")
            reason = repair.get("reason")
            policy_ids = repair.get("policy_ids")
            if (
                extra_keys
                or not isinstance(attempted, bool)
                or not isinstance(reason, str)
                or not isinstance(policy_ids, list)
                or any(not isinstance(item, str) for item in policy_ids)
                or len(policy_ids) != len(set(policy_ids))
                or any(item not in SEMANTIC_POLICY_IDS for item in policy_ids)
                or (not attempted and (reason != "" or policy_ids))
                or (
                    attempted
                    and reason
                    not in {
                        "plan_validation_failed",
                        "semantic_policy_failed",
                    }
                )
                or (reason == "plan_validation_failed" and policy_ids)
                or (reason == "semantic_policy_failed" and not policy_ids)
            ):
                raise ValueError(f"candidate row {case_id} has an invalid repair object")
            normalized["repair"] = {
                "attempted": attempted,
                "reason": reason,
                "policy_ids": list(policy_ids),
            }
        collection_error = row.get("collection_error")
        if isinstance(collection_error, Mapping):
            category = _safe_error_category_name(collection_error.get("error_category"))
            if category:
                normalized["collection_error"] = {"error_category": category}
        rows.append(normalized)
    return rows


def _safe_error_category_name(value: Any) -> str:
    category = str(value or "").strip()
    return category if category.isidentifier() and len(category) <= 100 else "EvaluationError"


def _safe_error_category(exc: Exception) -> str:
    """Return a bounded class name without serializing provider exception text."""

    return _safe_error_category_name(type(exc).__name__)


def _explicit_invalid_plan() -> dict[str, Any]:
    """A scorer-readable sentinel that can never pass the production plan parser."""

    return {
        "decision": "__invalid__",
        "clarification_kind": "none",
        "requested_outcomes": [],
        "evidence_requests": [
            {
                "capability": "__invalid_collection__",
                "arguments": "__invalid__",
            }
        ],
        "clarification_question": "",
    }


def _tool_result_for_plan(plan: Mapping[str, Any]) -> Any:
    """Wrap replay JSON as one provider meta-call for the production parser."""

    from core.services.llm_backend import ToolCallRequest, ToolChatResult
    from core.services.student_advisor_v21_plan import TURN_PLAN_TOOL_NAME

    raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    call = ToolCallRequest(
        id="eval_plan_1",
        name=TURN_PLAN_TOOL_NAME,
        arguments=dict(plan),
        raw_arguments=raw,
    )
    return ToolChatResult(
        content="",
        tool_calls=(call,),
        model="offline-replay",
        usage={},
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


def validate_candidate_through_typed_planner(
    results: Any,
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
    max_evidence_calls: int = DEFAULT_MAX_EVIDENCE_CALLS,
    cases_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse every replay row through the real provider-envelope validator."""

    _get_client, _schemas, parse_turn_plan_result, _plan_turn = _runtime_dependencies()
    validated: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row in _raw_rows(results):
        collected_error = row.get("collection_error")
        if isinstance(collected_error, Mapping):
            category = _safe_error_category_name(collected_error.get("error_category"))
            errors.append(
                {
                    "case_id": row["case_id"],
                    "error_category": category,
                }
            )
            plan = _explicit_invalid_plan()
            validated.append({**row, "plan": plan})
            continue
        try:
            typed = parse_turn_plan_result(
                _tool_result_for_plan(row["plan"]),
                advertised_tools=advertised_tools,
                max_calls=max_evidence_calls,
            )
            plan = _plan_mapping(typed)
            case = (cases_by_id or {}).get(str(row["case_id"]))
            if case is not None and semantic_policy_violations(
                str(case.get("question") or ""),
                plan,
                explicit_pins=case.get("_semantic_policy_explicit_pins"),
            ):
                raise SemanticPolicyReplayError
        except Exception as exc:  # noqa: BLE001 - malformed rows belong in the report
            errors.append(
                {
                    "case_id": row["case_id"],
                    "error_category": _safe_error_category(exc),
                }
            )
            plan = _explicit_invalid_plan()
        validated.append({**row, "plan": plan})
    return validated, errors


def planner_messages(case: Mapping[str, Any], *, year: int, term: int) -> list[dict[str, Any]]:
    """Build the default-web production planner transcript for one contract case."""

    history = case.get("history") or []
    return build_student_v21_planner_messages(
        question=str(case["question"]),
        academic_year=year,
        term=term,
        history=cast(Sequence[Mapping[str, Any]], history),
    )


def v2_baseline_inputs(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project cases to prompt-only fields so collection code cannot inspect gold labels."""

    return [
        {
            "id": str(case["id"]),
            "language": str(case["language"]),
            "question": str(case["question"]),
            **(
                {"history": [dict(message) for message in case["history"]]}
                if case.get("history")
                else {}
            ),
        }
        for case in contract["cases"]
    ]


def v2_first_turn_messages(
    case: Mapping[str, Any], *, year: int, term: int
) -> list[dict[str, str]]:
    """Reproduce V2's identity-free first-turn prompt without retrieving evidence."""

    from core.services import student_advisor_v2 as runtime

    question = str(case["question"])
    language = runtime._answer_language(question)
    answer_style = runtime._answer_style(question)
    history = [dict(message) for message in (case.get("history") or [])]
    return [
        {"role": "system", "content": runtime.SYSTEM_PROMPT},
        *history,
        {
            "role": "user",
            "content": (
                f"answer_language: {language}\n"
                f"answer_style: {answer_style}\n"
                f"configured_planning_term_hijri: {year}/{term}\n"
                f"graduation_system_current_term_hijri: {year}/{term}\n"
                "Use this configured term unless the student explicitly asks about another. "
                "Graduation scenarios use graduation_system_current_term_hijri and the "
                "deterministic planning_baseline_kind; do not substitute the planning/import "
                "term. Do not ask for a Gregorian year.\n"
                f"student_question: {question}\n"
                "verified_policy_evidence: {}"
            ),
        },
    ]


def _v2_deterministic_gates(question: str) -> dict[str, Any]:
    """Run the current V2 question-side evidence obligations without any execution."""

    from core.services import student_advisor_v2 as runtime
    from core.services.advisor_intent import owning_capability

    exact_fact_owner = owning_capability(question)
    requires_feasible_replacement = runtime._requires_feasible_course_replacements(question)
    requires_course_comparison = (
        runtime._requires_course_choice_comparison(question) and not requires_feasible_replacement
    )
    requires_timetable_proposal = (
        runtime._requires_timetable_proposal(question)
        and exact_fact_owner != "my_timetable"
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    requires_section_check = (
        runtime._requires_section_check(question)
        and not requires_timetable_proposal
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    requires_graduation_progress = (
        runtime._requires_graduation_progress(question)
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    graduation_change_follow_up = bool(
        runtime._CURRENT_COURSE_CHANGE_PATTERN.search(question)
        and not runtime._REPLACEMENT_TIMETABLE_PROOF_PATTERN.search(question)
        and (
            runtime._COURSE_CODE_TOKEN_PATTERN.search(question)
            or runtime._DIRECT_COURSE_CHANGE_ACTION_PATTERN.search(question)
        )
    )
    requires_graduation_what_if = (
        (runtime._requires_graduation_what_if(question) or graduation_change_follow_up)
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    required = runtime._required_exact_fact_tools(
        question,
        graduation_required=requires_graduation_progress,
        allow_owner=not any(
            (
                requires_timetable_proposal,
                requires_section_check,
                requires_course_comparison,
                requires_feasible_replacement,
                requires_graduation_what_if,
            )
        ),
    )
    if requires_timetable_proposal:
        required.add("build_timetable_proposal")
    if requires_section_check:
        required.add("my_clash_free_sections")
    if requires_course_comparison:
        required.add("course_choice_comparison")
    if requires_feasible_replacement:
        required.add("feasible_course_replacements")
    if requires_graduation_what_if:
        required.add("graduation_progress")
    policy_required = runtime.requires_policy_contract(question)
    if policy_required:
        required.add("policy_lookup")
    return {
        "tools": required,
        "requires_graduation_what_if": requires_graduation_what_if,
        "policy_required": policy_required,
        "exact_fact_owner": exact_fact_owner or "",
    }


def _v2_effective_arguments(
    question: str,
    name: str,
    arguments: Mapping[str, Any],
    *,
    gates: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Apply the same deterministic argument corrections V2 uses before execution."""

    from core.services import student_advisor_v2 as runtime

    supplied = dict(arguments)
    reasons: list[str] = []
    if name == "graduation_progress":
        normalized, reason = runtime._normalise_graduation_scenario_args(
            question,
            supplied,
            prior_course_names={},
            allow_prior_scenario=bool(gates["requires_graduation_what_if"]),
        )
        if reason:
            reasons.append(reason)
        return normalized, reasons
    if name == "course_choice_comparison":
        return cast(
            tuple[dict[str, Any], list[str]],
            runtime._normalise_course_comparison_args(question, supplied),
        )
    if name == "feasible_course_replacements":
        return cast(
            tuple[dict[str, Any], list[str]],
            runtime._normalise_feasible_replacement_args(question, supplied),
        )
    if name == "build_timetable_proposal":
        normalized, reasons = runtime._normalise_timetable_proposal_args(question, supplied)
        normalized.pop("_constraint_input_error", None)
        return normalized, reasons
    return supplied, reasons


def derive_v2_baseline_plan(
    question: str,
    model_calls: Sequence[Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Combine observed first-turn calls with V2's independent evidence obligations."""

    from core.services.student_advisor_v2 import STUDENT_V2_TOOL_NAMES

    gates = _v2_deterministic_gates(question)
    planned: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, raw_call in enumerate(model_calls):
        if isinstance(raw_call, Mapping):
            name = str(raw_call.get("name") or raw_call.get("capability") or "").strip()
            arguments = raw_call.get("arguments") or {}
        else:
            name = str(getattr(raw_call, "name", "") or "").strip()
            arguments = getattr(raw_call, "arguments", {}) or {}
        if not name:
            errors.append(f"model call {index} has no capability name")
            continue
        if not isinstance(arguments, Mapping):
            errors.append(f"model call {index} arguments are not an object")
            arguments = {}
        normalized, reasons = _v2_effective_arguments(
            question,
            name,
            arguments,
            gates=gates,
        )
        if name in planned:
            errors.append(f"model selected {name} more than once on its first turn")
            planned[name]["sources"].append("duplicate_model_first_turn")
            continue
        planned[name] = {
            "name": name,
            "arguments": normalized,
            "sources": ["model_first_turn"],
            "argument_normalizations": reasons,
        }

    gate_order = [*STUDENT_V2_TOOL_NAMES, *sorted(set(gates["tools"]) - set(STUDENT_V2_TOOL_NAMES))]
    for name in gate_order:
        if name not in gates["tools"]:
            continue
        if name in planned:
            planned[name]["sources"].append("deterministic_evidence_gate")
            if name == "policy_lookup":
                planned[name]["arguments"] = {"query": question}
            continue
        arguments, reasons = _v2_effective_arguments(question, name, {}, gates=gates)
        if name == "policy_lookup":
            # V2 prefetched policy evidence from the exact question before the
            # model's first turn. Represent the obligation without running lookup.
            arguments = {"query": question}
        planned[name] = {
            "name": name,
            "arguments": arguments,
            "sources": ["deterministic_evidence_gate"],
            "argument_normalizations": reasons,
        }

    calls = list(planned.values())
    plan = {
        "mode": "execute" if calls else "direct",
        "tool_calls": [{"name": call["name"], "arguments": call["arguments"]} for call in calls],
    }
    trace = {
        "model_first_turn_tools": [
            call["name"] for call in calls if "model_first_turn" in call["sources"]
        ],
        "deterministic_gate_tools": [
            call["name"] for call in calls if "deterministic_evidence_gate" in call["sources"]
        ],
        "combined_calls": calls,
        "exact_fact_owner": gates["exact_fact_owner"],
    }
    return plan, trace, errors


def estimated_call_token_ceiling(
    messages: Sequence[Mapping[str, Any]],
    advertised_tools: Sequence[Mapping[str, Any]],
    *,
    max_plan_tokens: int,
    max_evidence_calls: int = DEFAULT_MAX_EVIDENCE_CALLS,
) -> int:
    """Return a conservative byte-based ceiling used before each live request."""

    from core.services.student_advisor_v21_plan import build_turn_plan_tool_schema

    planner_tool = build_turn_plan_tool_schema(
        advertised_tools,
        max_calls=max_evidence_calls,
    )
    serialized = json.dumps(
        {"messages": list(messages), "tools": [planner_tool]},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    # A byte-level tokenizer cannot emit more ordinary tokens than non-empty input
    # bytes. The extra 512 covers provider framing and special tokens.
    return len(serialized.encode("utf-8")) + max_plan_tokens + 512


def collect_live_candidate(
    cases: Sequence[Mapping[str, Any]],
    *,
    client: Any,
    advertised_tools: Sequence[Mapping[str, Any]],
    plan_student_turn: Any,
    limits: LiveLimits,
    model: str | None,
    year: int,
    term: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect plans with a true total-two provider-turn validation lifecycle."""

    from core.services.student_advisor_v21_plan import (
        TurnPlanValidationError,
        build_plan_repair_message,
    )

    limits.validate(case_count=len(cases))
    rows: list[dict[str, Any]] = []
    totals = {
        "provider_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "committed_token_ceiling": 0,
    }
    stopped_for = ""
    collection_errors: list[dict[str, str]] = []
    repair_summary: dict[str, Any] = {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "reasons": {},
    }

    def account_usage(
        aggregate: dict[str, int],
        planning_result_or_error: Any,
    ) -> tuple[str, str]:
        turns = tuple(getattr(planning_result_or_error, "provider_turns", ()) or ())
        if not turns:
            turn = getattr(planning_result_or_error, "provider_turn", None)
            turns = (turn,) if turn is not None else ()
        last_model = ""
        last_revision = ""
        for turn in turns:
            turn_usage = dict(getattr(turn, "usage", {}) or {})
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                amount = int(turn_usage.get(key) or 0)
                totals[key] += amount
                aggregate[key] = int(aggregate.get(key) or 0) + amount
            last_model = str(getattr(turn, "model", "") or last_model)
            last_revision = str(getattr(turn, "model_revision", "") or last_revision)
        return last_model, last_revision

    for case in cases:
        messages = planner_messages(case, year=year, term=term)
        explicit_pins = _production_explicit_pins(str(case.get("question") or ""))
        row_usage: dict[str, int] = {}
        row_model = str(model or "")
        row_revision = ""
        repair_reason = ""
        repair_details: dict[str, tuple[str, ...]] = {}
        repair_audit: dict[str, Any] = {
            "attempted": False,
            "reason": "",
            "policy_ids": [],
        }
        final_plan: dict[str, Any] | None = None
        final_error = ""
        stop_before_first = False

        for attempt in range(2):
            repair_message = build_plan_repair_message(
                repair_reason,
                repair_details,
                advertised_tools=advertised_tools,
            )
            ceiling_messages = [*messages]
            if repair_message is not None:
                ceiling_messages.append(repair_message)
            token_ceiling = estimated_call_token_ceiling(
                ceiling_messages,
                advertised_tools,
                max_plan_tokens=limits.max_plan_tokens,
                max_evidence_calls=limits.max_evidence_calls,
            )
            provider_budget_available = totals["provider_calls"] < limits.max_provider_calls
            token_budget_available = (
                totals["committed_token_ceiling"] + token_ceiling <= limits.max_total_tokens
            )
            if not provider_budget_available or not token_budget_available:
                stopped_for = (
                    "provider-call budget reached"
                    if not provider_budget_available
                    else "conservative token budget reached before the next request"
                )
                if attempt == 0:
                    stop_before_first = True
                else:
                    final_error = "SemanticPlanRepairBudgetError"
                break

            # Every invocation is forced to one provider turn. The loop itself
            # owns the sole second slot for either schema or semantic-policy repair.
            totals["provider_calls"] += 1
            totals["committed_token_ceiling"] += token_ceiling
            if attempt == 1:
                repair_audit = {
                    "attempted": True,
                    "reason": repair_reason,
                    "policy_ids": list(repair_details.get("policy_ids", ())),
                }
            try:
                result = plan_student_turn(
                    client,
                    messages,
                    advertised_tools=advertised_tools,
                    max_calls=limits.max_evidence_calls,
                    model=model,
                    max_tokens=limits.max_plan_tokens,
                    timeout_seconds=limits.timeout_seconds,
                    max_attempts=1,
                    repair_reason=repair_reason,
                    repair_details=repair_details,
                )
            except Exception as exc:  # noqa: BLE001 - invalid rows remain scoreable
                used_model, used_revision = account_usage(row_usage, exc)
                row_model = used_model or row_model
                row_revision = used_revision or row_revision
                if attempt == 0 and isinstance(exc, TurnPlanValidationError):
                    repair_reason = "plan_validation_failed"
                    repair_details = {}
                    continue
                final_error = _safe_error_category(exc)
                break

            used_model, used_revision = account_usage(row_usage, result)
            row_model = used_model or row_model
            row_revision = used_revision or row_revision
            plan = _plan_mapping(result.plan)
            violations = semantic_policy_violations(
                str(case.get("question") or ""),
                plan,
                explicit_pins=explicit_pins,
            )
            if violations:
                if attempt == 0:
                    repair_reason = "semantic_policy_failed"
                    repair_details = {
                        "policy_ids": tuple(violation.value for violation in violations)
                    }
                    continue
                final_error = SemanticPolicyReplayError.__name__
                break
            final_plan = plan
            break

        if stop_before_first:
            break
        if repair_audit["attempted"]:
            repair_summary["attempted"] += 1
            result_key = "succeeded" if final_plan is not None else "failed"
            repair_summary[result_key] += 1
            reason = str(repair_audit["reason"])
            reasons = repair_summary["reasons"]
            reasons[reason] = int(reasons.get(reason) or 0) + 1
        if final_plan is None:
            category = final_error or "EvaluationError"
            collection_errors.append({"case_id": str(case["id"]), "error_category": category})
            rows.append(
                {
                    "case_id": str(case["id"]),
                    "plan": _explicit_invalid_plan(),
                    "usage": row_usage,
                    "model": row_model,
                    "model_revision": row_revision,
                    "repair": repair_audit,
                    "collection_error": {"error_category": category},
                }
            )
            continue
        rows.append(
            {
                "case_id": str(case["id"]),
                "plan": final_plan,
                "usage": row_usage,
                "model": row_model,
                "model_revision": row_revision,
                "repair": repair_audit,
            }
        )
    return rows, {
        "usage": totals,
        "stopped_for": stopped_for,
        "collection_errors": collection_errors,
        "repairs": repair_summary,
    }


def estimated_v2_call_token_ceiling(
    messages: Sequence[Mapping[str, Any]],
    advertised_tools: Sequence[Mapping[str, Any]],
    *,
    max_tokens: int,
) -> int:
    """Conservative ceiling for one ordinary V2 first-turn tool request."""

    serialized = json.dumps(
        {"messages": list(messages), "tools": list(advertised_tools)},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return len(serialized.encode("utf-8")) + max_tokens + 512


def collect_v2_baseline(
    cases: Sequence[Mapping[str, Any]],
    *,
    client: Any,
    advertised_tools: Sequence[Mapping[str, Any]],
    limits: LiveLimits,
    model: str | None,
    year: int,
    term: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Observe one V2 model turn per gold-blind input and add deterministic gates."""

    limits.validate(case_count=len(cases))
    rows: list[dict[str, Any]] = []
    totals = {
        "provider_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "committed_token_ceiling": 0,
    }
    stopped_for = ""
    provider_errors: list[dict[str, str]] = []
    derivation_warnings: list[dict[str, Any]] = []
    for case in cases:
        if totals["provider_calls"] >= limits.max_provider_calls:
            stopped_for = "provider-call budget reached"
            break
        messages = v2_first_turn_messages(case, year=year, term=term)
        token_ceiling = estimated_v2_call_token_ceiling(
            messages,
            advertised_tools,
            max_tokens=limits.max_plan_tokens,
        )
        if totals["committed_token_ceiling"] + token_ceiling > limits.max_total_tokens:
            stopped_for = "conservative token budget reached before the next request"
            break
        try:
            turn = client.chat_with_tools(
                messages,
                tools=list(advertised_tools),
                model=model,
                max_tokens=limits.max_plan_tokens,
                timeout_seconds=limits.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - provider failure invalidates the baseline
            provider_errors.append(
                {
                    "case_id": str(case["id"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            stopped_for = "provider error; baseline collection stopped"
            break

        usage = dict(turn.usage or {})
        totals["provider_calls"] += 1
        totals["committed_token_ceiling"] += token_ceiling
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += int(usage.get(key) or 0)
        plan, trace, warnings = derive_v2_baseline_plan(
            str(case["question"]), tuple(turn.tool_calls)
        )
        if warnings:
            derivation_warnings.append({"case_id": str(case["id"]), "warnings": warnings})
        rows.append(
            {
                "case_id": str(case["id"]),
                "plan": plan,
                "baseline_trace": {
                    **trace,
                    "model_returned_prose_without_tools": bool(
                        str(turn.content or "").strip() and not turn.tool_calls
                    ),
                },
                "usage": usage,
                "model": str(turn.model or model or ""),
                "model_revision": str(getattr(turn, "model_revision", "") or ""),
            }
        )
    return rows, {
        "usage": totals,
        "stopped_for": stopped_for,
        "provider_errors": provider_errors,
        "derivation_warnings": derivation_warnings,
    }


def build_v2_baseline_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    collection_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a reusable, scorer-compatible artifact with explicit caveats."""

    active = _contract_with_semantic_policy_context(
        dict(contract) if contract is not None else load_contract()
    )
    normalized_rows = [dict(row) for row in rows]
    report = score_batch({"rows": normalized_rows}, active, require_complete=False)
    metadata = dict(collection_metadata or {})
    provider_errors = list(metadata.get("provider_errors") or [])
    collection_valid = bool(report["coverage"]["complete"] and not provider_errors)
    return {
        "rows": normalized_rows,
        "contract": report["contract"],
        "baseline_collection": {
            **metadata,
            "method": "v2_first_model_turn_plus_deterministic_evidence_gates",
            "gold_labels_visible_to_collector": False,
            "student_or_evidence_tools_executed": False,
            "collection_valid": collection_valid,
            "limitations": list(V2_BASELINE_LIMITATIONS),
        },
        "baseline_report": report,
    }


def _result_metadata_rows(results: Any) -> list[Mapping[str, Any]]:
    """Return only explicit result rows used for model provenance checks."""

    if isinstance(results, Mapping) and "rows" in results:
        results = results["rows"]
    if not isinstance(results, list):
        return []
    return [row for row in results if isinstance(row, Mapping)]


def _side_model_provenance(results: Any) -> dict[str, Any]:
    rows = _result_metadata_rows(results)
    model_ids = sorted(
        {str(row.get("model") or "").strip() for row in rows if str(row.get("model") or "").strip()}
    )
    revision_ids = sorted(
        {
            str(row.get("model_revision") or "").strip()
            for row in rows
            if str(row.get("model_revision") or "").strip()
        }
    )
    return {
        "row_count": len(rows),
        "model_ids": model_ids,
        "rows_missing_model_id": sum(not str(row.get("model") or "").strip() for row in rows),
        "revision_ids": revision_ids,
        "rows_missing_revision": sum(
            not str(row.get("model_revision") or "").strip() for row in rows
        ),
    }


def _model_provenance(candidate_results: Any, baseline_results: Any) -> dict[str, Any]:
    """Compare A/B model identity without claiming an unavailable provider snapshot."""

    candidate = _side_model_provenance(candidate_results)
    baseline = _side_model_provenance(baseline_results)
    model_ids_match = bool(
        candidate["row_count"]
        and baseline["row_count"]
        and candidate["rows_missing_model_id"] == 0
        and baseline["rows_missing_model_id"] == 0
        and len(candidate["model_ids"]) == 1
        and candidate["model_ids"] == baseline["model_ids"]
    )

    revisions_complete = bool(
        candidate["row_count"]
        and baseline["row_count"]
        and candidate["rows_missing_revision"] == 0
        and baseline["rows_missing_revision"] == 0
    )
    revisions_match: bool | None = None
    revision_drift_observed = bool(
        len(candidate["revision_ids"]) > 1 or len(baseline["revision_ids"]) > 1
    )
    if revision_drift_observed:
        revisions_match = False
    elif revisions_complete:
        revisions_match = bool(
            len(candidate["revision_ids"]) == 1
            and candidate["revision_ids"] == baseline["revision_ids"]
        )

    limitations: list[str] = []
    if not model_ids_match:
        limitations.append(
            "Candidate and baseline must each record one complete, identical model ID."
        )
    if revisions_match is None:
        limitations.append(
            "Provider model revision is unavailable on one or more rows; matching model IDs "
            "do not prove the same provider snapshot."
        )
    elif not revisions_match:
        limitations.append("Candidate and baseline model revisions differ or changed within a run.")

    return {
        "candidate": candidate,
        "baseline": baseline,
        "model_ids_match": model_ids_match,
        "model_revisions_match": revisions_match,
        "same_snapshot_verified": bool(model_ids_match and revisions_match is True),
        "limitations": limitations,
    }


def build_report(
    candidate_results: Any,
    baseline_results: Any,
    *,
    contract: Mapping[str, Any] | None = None,
    advertised_tools: Sequence[Mapping[str, Any]] | None = None,
    max_evidence_calls: int = DEFAULT_MAX_EVIDENCE_CALLS,
    runner_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate candidate types, score both explicit runs, and compare the gate."""

    active = _contract_with_semantic_policy_context(
        dict(contract) if contract is not None else load_contract()
    )
    if advertised_tools is None:
        _get_client, schema_factory, _parse, _plan = _runtime_dependencies()
        advertised_tools = schema_factory()
    candidate_rows, typed_errors = validate_candidate_through_typed_planner(
        candidate_results,
        advertised_tools=advertised_tools,
        max_evidence_calls=max_evidence_calls,
        cases_by_id={str(case["id"]): case for case in active["cases"]},
    )
    candidate = score_batch({"rows": candidate_rows}, active, require_complete=False)
    baseline = score_batch(baseline_results, active)
    candidate_gate = candidate_quality_gate(candidate, active)
    comparison = compare_quality_gate(candidate, baseline, active)
    model_provenance = _model_provenance(candidate_rows, baseline_results)
    if typed_errors:
        candidate_gate["passed"] = False
        comparison["checks"]["typed_candidate_valid"] = False
    else:
        comparison["checks"]["typed_candidate_valid"] = True
    comparison["checks"]["same_model_ids"] = model_provenance["model_ids_match"]
    comparison["checks"]["same_model_revision_if_available"] = (
        model_provenance["model_revisions_match"] is not False
    )
    comparison["passed"] = all(comparison["checks"].values())
    return {
        "rows": candidate_rows,
        "contract": candidate["contract"],
        "runner": dict(runner_metadata or {}),
        "model_provenance": model_provenance,
        "typed_plan_errors": typed_errors,
        "candidate_report": candidate,
        "candidate_gate": candidate_gate,
        "baseline_report": baseline,
        "comparison_gate": comparison,
    }


def _load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_or_print(report: Mapping[str, Any], output: pathlib.Path | None, compact: bool) -> None:
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=None if compact else 2,
        sort_keys=True,
    )
    if output is None:
        try:
            print(rendered)
        except UnicodeEncodeError:
            print(
                json.dumps(
                    report,
                    ensure_ascii=True,
                    indent=None if compact else 2,
                    sort_keys=True,
                )
            )
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(f"wrote {output}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--candidate", type=pathlib.Path, help="offline typed-plan replay JSON")
    mode.add_argument("--live", action="store_true", help="opt in to provider planning calls")
    mode.add_argument(
        "--collect-v2-baseline",
        action="store_true",
        help="opt in to bounded V2 first-turn baseline collection",
    )
    parser.add_argument("--baseline", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--year", type=int, default=1448)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--max-provider-calls", type=int, default=0)
    parser.add_argument("--max-total-tokens", type=int, default=0)
    parser.add_argument("--max-plan-tokens", type=int, default=DEFAULT_MAX_PLAN_TOKENS)
    parser.add_argument(
        "--v2-baseline-max-tokens",
        type=int,
        default=DEFAULT_V2_BASELINE_MAX_TOKENS,
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-evidence-calls", type=int, default=DEFAULT_MAX_EVIDENCE_CALLS)
    parser.add_argument("--confirm-live-external-request", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    contract = load_contract()
    if args.candidate is None and not args.live and not args.collect_v2_baseline:
        parser.error(
            "choose --candidate, explicitly choose --live, or explicitly choose "
            "--collect-v2-baseline"
        )
    if args.max_evidence_calls < 1 or args.max_evidence_calls > 8:
        parser.error("--max-evidence-calls must be between 1 and 8")
    if args.collect_v2_baseline:
        if args.baseline is not None:
            parser.error("--collect-v2-baseline creates the baseline; do not pass --baseline")
        if args.output is None:
            parser.error("--collect-v2-baseline requires --output for the explicit artifact")
        if not args.confirm_live_external_request:
            parser.error("--collect-v2-baseline requires --confirm-live-external-request")
        baseline_limits = LiveLimits(
            max_provider_calls=args.max_provider_calls,
            max_total_tokens=args.max_total_tokens,
            max_plan_tokens=args.v2_baseline_max_tokens,
            timeout_seconds=args.timeout_seconds,
        )
        try:
            baseline_limits.validate(case_count=len(contract["cases"]))
        except ValueError as exc:
            parser.error(str(exc))
    elif args.baseline is None:
        parser.error("--baseline is required; the comparison baseline must be explicit")
    if args.live:
        if not args.confirm_live_external_request:
            parser.error("--live requires --confirm-live-external-request")
        live_limits = LiveLimits(
            max_provider_calls=args.max_provider_calls,
            max_total_tokens=args.max_total_tokens,
            max_plan_tokens=args.max_plan_tokens,
            timeout_seconds=args.timeout_seconds,
            max_evidence_calls=args.max_evidence_calls,
        )
        try:
            live_limits.validate(case_count=len(contract["cases"]))
        except ValueError as exc:
            parser.error(str(exc))

    get_client, schema_factory, _parse, plan_student_turn = _runtime_dependencies()
    # Bind and verify all frozen semantic-policy context before a client is
    # created or any provider request can be made. A drifted annotation is a
    # zero-egress contract failure.
    try:
        contract = _contract_with_semantic_policy_context(contract)
    except ValueError as exc:
        parser.error(str(exc))
    advertised_tools = schema_factory()
    if args.collect_v2_baseline:
        v2_advertised_tools = _v2_tool_schemas()
        client = get_client()
        try:
            initial_http = _prepare_bounded_live_client(
                client,
                max_tokens=baseline_limits.max_plan_tokens,
                timeout_seconds=baseline_limits.timeout_seconds,
            )
        except ValueError as exc:
            parser.error(str(exc))
        backend = str(getattr(client, "backend", "")).strip().lower()
        if backend != "local":
            from django.conf import settings

            if not bool(getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False)):
                parser.error("the configured external-provider egress kill switch is closed")
        try:
            model = _resolve_bounded_live_model(client, args.model or None)
        except ValueError as exc:
            parser.error(str(exc))
        rows, collection_metadata = collect_v2_baseline(
            v2_baseline_inputs(contract),
            client=client,
            advertised_tools=v2_advertised_tools,
            limits=baseline_limits,
            model=model,
            year=args.year,
            term=args.term,
        )
        try:
            transport = _verified_http_attempt_deltas(
                client,
                initial=initial_http,
                logical_provider_calls=int(
                    (collection_metadata.get("usage") or {}).get("provider_calls") or 0
                ),
            )
        except ValueError as exc:
            parser.error(str(exc))
        baseline_report = build_v2_baseline_report(
            rows,
            contract=contract,
            collection_metadata={
                "mode": "live_v2_baseline",
                "backend": backend,
                "budgets": {
                    "max_provider_calls": baseline_limits.max_provider_calls,
                    "max_total_tokens": baseline_limits.max_total_tokens,
                    "max_tokens_per_turn": baseline_limits.max_plan_tokens,
                    "timeout_seconds": baseline_limits.timeout_seconds,
                    "max_retries": 0,
                },
                "transport": transport,
                **collection_metadata,
            },
        )
        _write_or_print(baseline_report, args.output, args.compact)
        return 0 if baseline_report["baseline_collection"]["collection_valid"] else 1

    metadata: dict[str, Any]
    if args.live:
        client = get_client()
        try:
            initial_http = _prepare_bounded_live_client(
                client,
                max_tokens=live_limits.max_plan_tokens,
                timeout_seconds=live_limits.timeout_seconds,
            )
        except ValueError as exc:
            parser.error(str(exc))
        backend = str(getattr(client, "backend", "")).strip().lower()
        if backend != "local":
            from django.conf import settings

            if not bool(getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False)):
                parser.error("the configured external-provider egress kill switch is closed")
        try:
            model = _resolve_bounded_live_model(client, args.model or None)
        except ValueError as exc:
            parser.error(str(exc))
        candidate_results, live_metadata = collect_live_candidate(
            contract["cases"],
            client=client,
            advertised_tools=advertised_tools,
            plan_student_turn=plan_student_turn,
            limits=live_limits,
            model=model,
            year=args.year,
            term=args.term,
        )
        try:
            transport = _verified_http_attempt_deltas(
                client,
                initial=initial_http,
                logical_provider_calls=int(
                    (live_metadata.get("usage") or {}).get("provider_calls") or 0
                ),
            )
        except ValueError as exc:
            parser.error(str(exc))
        metadata = {
            "mode": "live",
            "backend": backend,
            "model": str(model or ""),
            "budgets": {
                "max_provider_calls": live_limits.max_provider_calls,
                "max_total_tokens": live_limits.max_total_tokens,
                "max_plan_tokens": live_limits.max_plan_tokens,
                "timeout_seconds": live_limits.timeout_seconds,
                "max_retries": 0,
            },
            "transport": transport,
            **live_metadata,
        }
    else:
        candidate_results = _load_json(cast(pathlib.Path, args.candidate))
        metadata = {"mode": "offline_replay", "provider_calls": 0}

    report = build_report(
        candidate_results,
        _load_json(cast(pathlib.Path, args.baseline)),
        contract=contract,
        advertised_tools=advertised_tools,
        max_evidence_calls=args.max_evidence_calls,
        runner_metadata=metadata,
    )
    _write_or_print(report, args.output, args.compact)
    return 0 if report["comparison_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
