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

EVIDENCE_AUDIT_SCHEMA_VERSION = "1"
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
    }
)

VALIDATION_OUTCOMES = frozenset(
    {"not_applicable", "passed", "repaired", "verified_fallback", "abstained"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")


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


def _repair_result(*, attempted: bool, outcome: str) -> str:
    if not attempted:
        return "not_attempted"
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
    inference_calls: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    turn_ms: int = 0,
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
    return {
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
            ),
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
    attempted = raw_repair.get("attempted") is True
    final_violations = _closed_categories(raw_validation.get("violations_after_repair"))
    repair_result = _repair_result(
        attempted=attempted,
        outcome=outcome,
    )

    return {
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
        "cost": {
            "inference_calls": _count(raw_cost.get("inference_calls")),
            "prompt_tokens": _count(raw_cost.get("prompt_tokens")),
            "completion_tokens": _count(raw_cost.get("completion_tokens")),
            "turn_ms": _count(raw_cost.get("turn_ms")),
        },
    }


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
