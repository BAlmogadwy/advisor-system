"""Redacted, countable provenance for one persisted adviser answer.

The model-facing evidence itself is intentionally *not* an audit record.  It can
contain course registrations, grades, section labels, names, and opaque student
references.  Keeping a copy beside ``AdvisorMessage.content`` would make every
student conversation a second academic-record store.

This module therefore persists only a deterministic fingerprint of each typed
payload that crossed the provider boundary, plus closed server-owned outcome
categories.  It accepts no arguments, question text, answer text, student
identity, or raw evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from core.services.answer_consistency import ALL_CHECKS
from core.services.student_advisor_v21_plan import (
    EXPLICIT_CONSTRAINT_FIELD_PATHS,
    ClarificationKind,
    StudentRequestOutcome,
    TurnPlanDecision,
)

# The new constraint fields live inside the optional V2.1 plan_contract block.
# They change the persisted envelope contract, so this revision intentionally
# advances while legacy V2 records continue to omit the optional block.
EVIDENCE_AUDIT_SCHEMA_VERSION = "2"
STUDENT_V2_PROMPT_VERSION = "student-v2-evidence-boundary-v1"

# This is deliberately a closed list rather than a syntactic check.  A value such
# as ``student_4400000`` is a perfectly valid snake-case string but is not a tool
# name and must never be persisted through transient telemetry.
AUDITABLE_TOOL_NAMES = frozenset(
    {
        "get_student_context",
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
        "present_prior_artifact",
        # Student-reachable via the legacy adviser path and the registry;
        # while absent from this list their turns persisted tool_names: [],
        # indistinguishable from a turn that called no tool at all.
        "build_my_timetable",
        "course_eligibility",
        "recommend_feasible_course_addition",
        "rank_current_course_drop_impact",
        "improve_current_timetable",
    }
)

VALIDATION_OUTCOMES = frozenset(
    {"not_applicable", "passed", "repaired", "verified_fallback", "abstained"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_SEMANTIC_DECISIONS = frozenset(decision.value for decision in TurnPlanDecision)
_SEMANTIC_CLARIFICATION_KINDS = frozenset(kind.value for kind in ClarificationKind)
_SEMANTIC_OUTCOMES = frozenset(outcome.value for outcome in StudentRequestOutcome)
_SEMANTIC_COVERAGE_REASONS = frozenset(
    {
        "",
        "outcomes_missing",
        "duplicate_outcomes",
        "capability_not_advertised",
        "direct_outcome_mismatch",
        "unsupported_outcome_mismatch",
        "clarify_outcome_mismatch",
        "requested_outcome_uncovered",
        "requested_entity_uncovered",
        "invalid_control_combination",
        "unnecessary_capability",
        "evidence_missing",
        "constraint_coverage_failed",
        "semantic_policy_failed",
    }
)
_SEMANTIC_PLAN_FAILURE_REASONS = frozenset(
    {
        "plan_validation_failed",
        "argument_provenance_failed",
        "outcome_coverage_failed",
        "constraint_coverage_failed",
        "semantic_policy_failed",
    }
)


def _closed_constraint_field_paths(values: Any) -> list[str]:
    """Keep only reviewed schema paths; never an argument value or error prose."""

    if not isinstance(values, list | tuple):
        return []
    return list(
        dict.fromkeys(
            str(value)
            for value in values
            if isinstance(value, str) and value in EXPLICIT_CONSTRAINT_FIELD_PATHS
        )
    )


def _semantic_plan_summary(
    *,
    decision: Any,
    clarification_kind: Any,
    requested_outcomes: Any,
    coverage_valid: Any,
    coverage_reason: Any,
) -> dict[str, Any]:
    typed_decision = str(decision or "")
    if typed_decision not in _SEMANTIC_DECISIONS:
        return {}
    typed_clarification_kind = str(clarification_kind or "none")
    if typed_clarification_kind not in _SEMANTIC_CLARIFICATION_KINDS:
        return {}
    if (typed_decision == "clarify") != (typed_clarification_kind != "none"):
        return {}
    typed_outcomes: list[str] = []
    if isinstance(requested_outcomes, list | tuple):
        for raw in requested_outcomes:
            outcome = str(raw or "")
            if outcome in _SEMANTIC_OUTCOMES and outcome not in typed_outcomes:
                typed_outcomes.append(outcome)
    if not typed_outcomes:
        return {}
    reason = str(coverage_reason or "")
    if reason not in _SEMANTIC_COVERAGE_REASONS:
        reason = ""
    return {
        "decision": typed_decision,
        "clarification_kind": typed_clarification_kind,
        "requested_outcomes": typed_outcomes,
        "coverage": {
            "valid": coverage_valid is True,
            "reason": reason,
        },
    }


def canonical_evidence_json(value: Any) -> str:
    """Return one deterministic JSON spelling for a provider-visible payload.

    Provider projections are ordinary JSON-shaped values. ``default=str`` mirrors
    the existing model-message serializer for the few database scalar types that
    reach a local provider, while sorted keys and compact separators make the
    digest independent of dictionary insertion order.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def evidence_sha256(value: Any) -> str:
    """Fingerprint typed evidence without retaining any of its values."""

    return hashlib.sha256(canonical_evidence_json(value).encode("utf-8")).hexdigest()


def _closed_categories(values: Any) -> list[str]:
    if not isinstance(values, list | tuple):
        return []
    return list(
        dict.fromkeys(
            str(value) for value in values if isinstance(value, str) and value in ALL_CHECKS
        )
    )


_PROVIDER_ERROR_KINDS = frozenset(
    {
        "LLMTimeout",
        "LLMUnavailable",
        "LLMRateLimited",
        "LLMInvalidResponse",
        "LLMBadRequest",
        "LLMAuthenticationError",
        "LLMConfigError",
        "LLMError",
    }
)


def _provider_error(value: Any) -> str:
    kind = str(value or "")
    return kind if kind in _PROVIDER_ERROR_KINDS else ""


def _repair_result(*, attempted: bool, outcome: str, budget_skipped: bool = False) -> str:
    if not attempted:
        # "We did not bother" and "the clock forbade it" are different facts,
        # and the operator reading a violations-bearing abstention needs to
        # know which one happened.
        return "budget_skipped" if budget_skipped else "not_attempted"
    if outcome == "repaired":
        return "succeeded"
    if outcome == "verified_fallback":
        return "verified_fallback"
    if outcome == "abstained":
        return "abstained"
    # A bounded repair ran but did not produce a validated answer.  No exception
    # text is retained: its class and message are operational logs, not provenance.
    return "failed"


def build_evidence_audit(
    *,
    provider_evidence: Iterable[tuple[str, Any]],
    validation_outcome: str,
    violations: list[str] | tuple[str, ...] = (),
    violations_after_repair: list[str] | tuple[str, ...] = (),
    repair_attempted: bool = False,
    turn_budget_exhausted: bool = False,
    provider_error: str = "",
    inference_calls: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    turn_ms: int = 0,
    semantic_plan_decision: str = "",
    semantic_plan_clarification_kind: str = "none",
    semantic_plan_requested_outcomes: Iterable[str] = (),
    semantic_outcome_coverage_valid: bool | None = None,
    semantic_outcome_coverage_reason: str = "",
    semantic_plan_failure_reason: str = "",
    semantic_plan_repair_attempted: bool = False,
    semantic_plan_missing_constraint_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the transient audit shape from the exact payloads the provider saw."""

    hashes: list[dict[str, str]] = []
    tool_names: list[str] = []
    for raw_tool_name, payload in provider_evidence:
        tool_name = str(raw_tool_name or "")
        if tool_name not in AUDITABLE_TOOL_NAMES or not isinstance(payload, Mapping):
            continue
        tool_names.append(tool_name)
        hashes.append({"tool": tool_name, "sha256": evidence_sha256(payload)})

    outcome = (
        str(validation_outcome)
        if str(validation_outcome) in VALIDATION_OUTCOMES
        else "not_applicable"
    )
    initial = _closed_categories(violations)
    final = _closed_categories(violations_after_repair)
    attempted = bool(repair_attempted)
    audit = {
        "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
        "tool_names": tool_names,
        "evidence_hashes": hashes,
        "validation": {
            "outcome": outcome,
            "violations": initial,
            "violations_after_repair": final,
        },
        "repair": {
            "attempted": attempted,
            "result": _repair_result(
                attempted=attempted,
                outcome=outcome,
                budget_skipped=bool(turn_budget_exhausted) and bool(initial),
            ),
        },
        "flags": {
            "turn_budget_exhausted": bool(turn_budget_exhausted),
            "provider_error": _provider_error(provider_error),
        },
        # Cost/latency counters, never content.  These exist so a later
        # feature that adds inference calls has a measured BASELINE to be
        # judged against, and so the quality screen can read cost without a
        # second telemetry store.
        "cost": {
            "inference_calls": _count(inference_calls),
            "prompt_tokens": _count(prompt_tokens),
            "completion_tokens": _count(completion_tokens),
            "turn_ms": _count(turn_ms),
        },
    }
    semantic_plan = _semantic_plan_summary(
        decision=semantic_plan_decision,
        clarification_kind=semantic_plan_clarification_kind,
        requested_outcomes=tuple(semantic_plan_requested_outcomes),
        coverage_valid=semantic_outcome_coverage_valid,
        coverage_reason=semantic_outcome_coverage_reason,
    )
    if semantic_plan:
        audit["semantic_plan"] = semantic_plan
    plan_failure = str(semantic_plan_failure_reason or "")
    plan_repair_attempted = bool(semantic_plan_repair_attempted)
    missing_constraint_paths = _closed_constraint_field_paths(
        tuple(semantic_plan_missing_constraint_paths)
    )
    if plan_failure in _SEMANTIC_PLAN_FAILURE_REASONS or plan_repair_attempted:
        audit["plan_contract"] = {
            "failure_reason": (
                plan_failure if plan_failure in _SEMANTIC_PLAN_FAILURE_REASONS else ""
            ),
            "repair": {
                "attempted": plan_repair_attempted,
                "result": (
                    "failed" if plan_failure in _SEMANTIC_PLAN_FAILURE_REASONS else "succeeded"
                ),
            },
        }
        if missing_constraint_paths:
            audit["plan_contract"]["missing_field_paths"] = missing_constraint_paths
    return audit


def _count(value: Any) -> int:
    """A non-negative integer counter, or zero - never content, never floats."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, min(int(value), 1_000_000_000))


def normalise_evidence_audit(value: Any) -> dict[str, Any]:
    """Re-whitelist transient metadata immediately before database persistence.

    This intentionally rebuilds the object rather than deleting known-bad keys.
    A future caller can add arbitrary telemetry to ``result['agent']`` without
    silently widening what is stored in the conversation table.
    """

    if not isinstance(value, Mapping):
        return {}

    evidence_hashes: list[dict[str, str]] = []
    raw_hashes = value.get("evidence_hashes")
    if isinstance(raw_hashes, list | tuple):
        for row in raw_hashes:
            if not isinstance(row, Mapping):
                continue
            tool_name = str(row.get("tool") or "")
            digest = str(row.get("sha256") or "").lower()
            if tool_name in AUDITABLE_TOOL_NAMES and _SHA256.fullmatch(digest):
                evidence_hashes.append({"tool": tool_name, "sha256": digest})

    raw_validation = value.get("validation")
    raw_validation = raw_validation if isinstance(raw_validation, Mapping) else {}
    outcome = str(raw_validation.get("outcome") or "")
    if outcome not in VALIDATION_OUTCOMES:
        outcome = "not_applicable"

    raw_repair = value.get("repair")
    raw_repair = raw_repair if isinstance(raw_repair, Mapping) else {}
    raw_cost = value.get("cost")
    raw_cost = raw_cost if isinstance(raw_cost, Mapping) else {}
    raw_flags = value.get("flags")
    raw_flags = raw_flags if isinstance(raw_flags, Mapping) else {}
    attempted = raw_repair.get("attempted") is True
    final_violations = _closed_categories(raw_validation.get("violations_after_repair"))
    repair_result = _repair_result(
        attempted=attempted,
        outcome=outcome,
        budget_skipped=raw_flags.get("turn_budget_exhausted") is True
        and bool(_closed_categories(raw_validation.get("violations"))),
    )

    cleaned = {
        "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
        # Derive this from the accepted hashes rather than trusting a second list
        # that could disagree with them.
        "tool_names": [row["tool"] for row in evidence_hashes],
        "evidence_hashes": evidence_hashes,
        "validation": {
            "outcome": outcome,
            "violations": _closed_categories(raw_validation.get("violations")),
            "violations_after_repair": final_violations,
        },
        "repair": {"attempted": attempted, "result": repair_result},
        "flags": {
            "turn_budget_exhausted": raw_flags.get("turn_budget_exhausted") is True,
            "provider_error": _provider_error(raw_flags.get("provider_error")),
        },
        "cost": {
            "inference_calls": _count(raw_cost.get("inference_calls")),
            "prompt_tokens": _count(raw_cost.get("prompt_tokens")),
            "completion_tokens": _count(raw_cost.get("completion_tokens")),
            "turn_ms": _count(raw_cost.get("turn_ms")),
        },
    }
    raw_semantic = value.get("semantic_plan")
    if isinstance(raw_semantic, Mapping):
        raw_coverage = raw_semantic.get("coverage")
        raw_coverage = raw_coverage if isinstance(raw_coverage, Mapping) else {}
        semantic_plan = _semantic_plan_summary(
            decision=raw_semantic.get("decision"),
            clarification_kind=raw_semantic.get("clarification_kind"),
            requested_outcomes=raw_semantic.get("requested_outcomes"),
            coverage_valid=raw_coverage.get("valid"),
            coverage_reason=raw_coverage.get("reason"),
        )
        if semantic_plan:
            cleaned["semantic_plan"] = semantic_plan
    raw_plan_contract = value.get("plan_contract")
    if isinstance(raw_plan_contract, Mapping):
        raw_failure = str(raw_plan_contract.get("failure_reason") or "")
        raw_plan_repair = raw_plan_contract.get("repair")
        raw_plan_repair = raw_plan_repair if isinstance(raw_plan_repair, Mapping) else {}
        plan_repair_attempted = raw_plan_repair.get("attempted") is True
        if raw_failure in _SEMANTIC_PLAN_FAILURE_REASONS or plan_repair_attempted:
            cleaned["plan_contract"] = {
                "failure_reason": (
                    raw_failure if raw_failure in _SEMANTIC_PLAN_FAILURE_REASONS else ""
                ),
                "repair": {
                    "attempted": plan_repair_attempted,
                    "result": (
                        "failed" if raw_failure in _SEMANTIC_PLAN_FAILURE_REASONS else "succeeded"
                    ),
                },
            }
            missing_constraint_paths = _closed_constraint_field_paths(
                raw_plan_contract.get("missing_field_paths")
            )
            if missing_constraint_paths:
                cleaned["plan_contract"]["missing_field_paths"] = missing_constraint_paths
    return cleaned


def normalise_prompt_version(value: Any) -> str:
    """Accept a compact server revision token, never prompt text."""

    version = str(value or "").strip()
    if len(version) > 40 or not _VERSION_TOKEN.fullmatch(version):
        return ""
    return version


def normalise_model_revision(value: Any) -> str:
    """Accept only provider-shaped revision identifiers, never response prose."""

    revision = str(value or "").strip()
    if len(revision) > 120 or not _VERSION_TOKEN.fullmatch(revision):
        return ""
    return revision


__all__ = [
    "AUDITABLE_TOOL_NAMES",
    "EVIDENCE_AUDIT_SCHEMA_VERSION",
    "STUDENT_V2_PROMPT_VERSION",
    "build_evidence_audit",
    "canonical_evidence_json",
    "evidence_sha256",
    "normalise_evidence_audit",
    "normalise_model_revision",
    "normalise_prompt_version",
]
