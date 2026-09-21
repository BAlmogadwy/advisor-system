#!/usr/bin/env python
"""Run a bounded, resumable V2.1 end-to-end corpus against one local student.

Validation is the default and performs zero provider calls::

    python evals/advisor/run_v21_saudi_e2e.py CORPUS.yaml

Live collection is deliberately noisy and opt-in.  Every cost and identity input is
explicit, ``V21_EVAL_FINGERPRINT_KEY`` is a required unpersisted 32+ character
HMAC key, and an artifact is checkpointed before each outbound inference attempt::

    python evals/advisor/run_v21_saudi_e2e.py CORPUS.yaml --live \
      --confirm-live-external-request --student-id 1234567 --model MODEL \
      --max-cases 10 --max-provider-calls 30 --max-total-tokens 1000000 \
      --max-tokens-per-call 1800 --timeout-seconds 45 --max-wall-seconds 1800 \
      --output runtime/evals/v21-saudi-e2e.json

The runner calls ``answer_student_advisor_v21`` directly with no history and never
creates adviser conversations or messages.  Saved rows contain a PII-scrubbed answer,
closed planner/validation fields, counters, and the runtime's normalized evidence
audit.  Raw evidence, tool arguments/results, student ids, and names are never saved.
Provider retries are disabled so ``max-provider-calls`` is also an HTTP-attempt cap.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import datetime as dt
import hashlib
import hmac
import importlib.metadata
import json
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "v21-saudi-e2e-v5"
MAX_CASES = 5000
MAX_PROVIDER_CALLS = 4096
MAX_TOTAL_TOKENS = 250_000_000
MAX_ANSWER_CHARS = 24_000
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_COURSE_CODE = re.compile(r"[A-Z]{2,8}[0-9]{2,4}[A-Z]?\Z")
_SECTION_LABEL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,15}\Z")
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_PLACEHOLDER = re.compile(r"\b(?:X{4}|Y{4})\b", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?966|00966|0)?5[\s-]?\d(?:[\s-]?\d){7}(?!\d)")
_LONG_NUMBER = re.compile(r"(?<![0-9٠-٩۰-۹])[0-9٠-٩۰-۹]{6,}(?![0-9٠-٩۰-۹])")
_RUNTIME_FINGERPRINT_ENTRYPOINTS = (
    "config/settings.py",
    "core/services/student_advisor_v21.py",
    "evals/advisor/run_v21_saudi_e2e.py",
)
_RUNTIME_FINGERPRINT_MANIFESTS = (
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
)
_RUNTIME_FINGERPRINT_NAMESPACES = frozenset({"config", "core", "evals"})
_MAX_RUNTIME_FINGERPRINT_FILES = 512
_MAX_RUNTIME_FINGERPRINT_BYTES = 32 * 1024 * 1024
_FIXTURE_STATE_FINGERPRINT_VERSION = "v21-fixture-state-v1"
_FINGERPRINT_KEY_ENV = "V21_EVAL_FINGERPRINT_KEY"
_ARTIFACT_HMAC_SCOPE_VERSION = "v21-saudi-e2e-artifact-v1"
_MAX_FIXTURE_STATE_ROWS = 250_000
_MAX_FIXTURE_STATE_BYTES = 64 * 1024 * 1024
_POLICY_FIXTURE_SKIP_DIRS = frozenset({"calendar", "evidence", "sources", "tools"})
_POLICY_FIXTURE_SKIP_FILES = frozenset({"evidence_map.yaml"})
_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "student_id",
        "student_name",
        "tool_results",
        "student_facts",
        "verified_context",
        "provider_evidence",
        "raw_evidence",
        "arguments",
    }
)
_VALID_OUTCOMES = frozenset(
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
_UNSUPPORTED_OUTCOMES = frozenset(
    {"registration_action", "credit_load_comparison", "unsupported_request"}
)
_SERVER_OWNED_EXECUTE_OUTCOMES = frozenset({"registration_action", "credit_load_comparison"})
_EXECUTE_FORBIDDEN_OUTCOMES = frozenset({"general_conversation", "unsupported_request"})
_CLARIFICATION_KINDS = frozenset(
    {
        "none",
        "timetable_load",
        "timetable_preference",
        "course_or_section_identity",
        "term_or_choice",
        "generic",
    }
)


_OUTCOME_CAPABILITY_OWNERS: dict[str, frozenset[str]] = {
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


def _owners_for_outcome(
    outcome: str,
    requested: frozenset[str],
    controls: Mapping[str, Mapping[str, Any]] | None = None,
) -> frozenset[str]:
    owners = set(_OUTCOME_CAPABILITY_OWNERS.get(outcome, frozenset()))
    if outcome == "graduation_impact":
        graduation_controls = (controls or {}).get("graduation_progress") or {}
        if (
            bool(graduation_controls.get("add_current_courses"))
            or bool(graduation_controls.get("remove_current_courses"))
            or bool(graduation_controls.get("noncompletion_current_courses"))
            or graduation_controls.get("search_better_replacements") is True
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
        comparison_controls = (controls or {}).get("course_choice_comparison") or {}
        if (
            "course_comparison" in requested
            and str(comparison_controls.get("objective") or "") == "graduation"
        ):
            owners.add("course_choice_comparison")
    if outcome == "course_priority" and "course_addition" in requested:
        owners.add("recommend_feasible_course_addition")
    if outcome == "course_replacement" and "timetable_review" in requested:
        owners.add("improve_current_timetable")
    if (
        outcome == "course_replacement"
        and ((controls or {}).get("graduation_progress") or {}).get("search_better_replacements")
        is True
    ):
        owners.add("graduation_progress")
    return frozenset(owners)


_SAFE_CONTROL_KEYS: dict[str, frozenset[str]] = {
    "my_progress": frozenset({"priority_limit"}),
    "build_timetable_proposal": frozenset(
        {
            "mode",
            "max_credits",
            "target_credits",
            "must_take_courses",
            "pinned_sections",
        }
    ),
    "course_choice_comparison": frozenset({"objective"}),
    "graduation_progress": frozenset(
        {
            "add_current_courses",
            "noncompletion_current_courses",
            "planning_baseline_kind",
            "remove_current_courses",
            "search_better_replacements",
        }
    ),
    "recommend_feasible_course_addition": frozenset(
        {
            "objective",
            "additional_credit_hours",
            "candidate_courses",
            "max_credits",
            "pinned_sections",
        }
    ),
    "rank_current_course_drop_impact": frozenset({"objective", "course_codes", "max_credits"}),
    "improve_current_timetable": frozenset(
        {
            "objective",
            "credit_load_policy",
            "allow_course_replacements",
            "max_credits",
        }
    ),
}


class CorpusError(ValueError):
    """The corpus is not safe or usable for this runner."""


class BudgetStop(RuntimeError):
    """A provider call was refused before egress because a hard limit was reached."""


@dataclass(frozen=True)
class CaseContract:
    support_level: str = ""
    expected_decisions: tuple[str, ...] = ()
    expected_outcomes: tuple[str, ...] = ()
    clarification_kind: str = "none"
    exact_tools: tuple[str, ...] | None = None
    required_all: tuple[str, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    required_controls: dict[str, dict[str, Any]] | None = None
    acceptable_plans: tuple[CaseContract, ...] = ()


@dataclass(frozen=True)
class CorpusCase:
    case_id: str
    category: str
    question: str
    contract: CaseContract


@dataclass(frozen=True)
class CorpusData:
    corpus_id: str
    sha256: str
    execution_sha256: str
    cases: tuple[CorpusCase, ...]


@dataclass(frozen=True)
class LiveLimits:
    max_cases: int
    max_provider_calls: int
    max_total_tokens: int
    max_tokens_per_call: int
    timeout_seconds: float
    max_wall_seconds: float

    def validate(self, *, case_count: int) -> None:
        if isinstance(self.max_cases, bool) or not 1 <= self.max_cases <= min(
            case_count, MAX_CASES
        ):
            raise ValueError(f"max_cases must be between 1 and {min(case_count, MAX_CASES)}")
        if isinstance(self.max_provider_calls, bool) or not 1 <= self.max_provider_calls <= (
            MAX_PROVIDER_CALLS
        ):
            raise ValueError(f"max_provider_calls must be between 1 and {MAX_PROVIDER_CALLS}")
        if isinstance(self.max_total_tokens, bool) or not 1 <= self.max_total_tokens <= (
            MAX_TOTAL_TOKENS
        ):
            raise ValueError(f"max_total_tokens must be between 1 and {MAX_TOTAL_TOKENS}")
        if (
            isinstance(self.max_tokens_per_call, bool)
            or not 128 <= (self.max_tokens_per_call) <= 4000
        ):
            raise ValueError("max_tokens_per_call must be between 128 and 4000")
        if isinstance(self.timeout_seconds, bool) or not 1.0 <= self.timeout_seconds <= 60.0:
            raise ValueError("timeout_seconds must be between 1 and 60")
        if (
            isinstance(self.max_wall_seconds, bool)
            or not 1.0 <= (self.max_wall_seconds) <= 86_400.0
        ):
            raise ValueError("max_wall_seconds must be between 1 and 86400")


@dataclass
class BudgetState:
    limits: LiveLimits
    provider_calls: int = 0
    provider_responses: int = 0
    committed_token_ceiling: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prior_wall_seconds: float = 0.0
    segment_started: float = field(default_factory=time.monotonic)
    stopped_for: str = ""

    @property
    def wall_seconds(self) -> float:
        return self.prior_wall_seconds + max(0.0, time.monotonic() - self.segment_started)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "provider_responses": self.provider_responses,
            "committed_token_ceiling": self.committed_token_ceiling,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "active_wall_seconds": round(self.wall_seconds, 3),
        }

    def stop(self, reason: str) -> None:
        self.stopped_for = reason
        raise BudgetStop(reason)

    def observe_usage(self, usage: Any) -> None:
        if not isinstance(usage, Mapping):
            return
        prompt = _non_negative_int(usage.get("prompt_tokens"))
        completion = _non_negative_int(usage.get("completion_tokens"))
        total = _non_negative_int(usage.get("total_tokens")) or prompt + completion
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total


class BudgetedLLMClient:
    """Clamp every model call and reserve its worst-case budget before egress."""

    def __init__(
        self,
        inner: Any,
        budget: BudgetState,
        *,
        checkpoint: Callable[[], None],
    ) -> None:
        if int(getattr(getattr(inner, "config", None), "max_retries", -1)) != 0:
            raise ValueError("the bounded provider client must have retries disabled")
        self._inner = inner
        self._budget = budget
        self._checkpoint = checkpoint

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def resolve_model(self, requested: str | None = None) -> str:
        if not str(requested or "").strip():
            raise ValueError("the bounded runner requires an explicit model")
        return str(self._inner.resolve_model(requested))

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self._invoke("chat", messages, kwargs)

    def chat_with_tools(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self._invoke("chat_with_tools", messages, kwargs)

    def _invoke(self, method: str, messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> Any:
        remaining_wall = self._budget.limits.max_wall_seconds - self._budget.wall_seconds
        if remaining_wall < 1.0:
            self._budget.stop("active wall-time budget reached before the next request")
        if self._budget.provider_calls >= self._budget.limits.max_provider_calls:
            self._budget.stop("provider-call budget reached before the next request")

        configured_max = int(
            kwargs.get("max_tokens")
            or getattr(getattr(self._inner, "config", None), "max_tokens", 0)
            or self._budget.limits.max_tokens_per_call
        )
        max_tokens = min(configured_max, self._budget.limits.max_tokens_per_call)
        kwargs["max_tokens"] = max_tokens
        tools = kwargs.get("tools") if method == "chat_with_tools" else None
        token_ceiling = estimated_call_token_ceiling(
            messages,
            tools=tools,
            max_tokens=max_tokens,
        )
        if (
            self._budget.committed_token_ceiling + token_ceiling
            > self._budget.limits.max_total_tokens
        ):
            self._budget.stop("conservative token budget reached before the next request")

        timeout = min(
            float(kwargs.get("timeout_seconds") or self._budget.limits.timeout_seconds),
            self._budget.limits.timeout_seconds,
            remaining_wall,
        )
        now = time.monotonic()
        deadline = now + timeout
        prior_deadline = kwargs.get("deadline_monotonic")
        if isinstance(prior_deadline, int | float):
            deadline = min(deadline, float(prior_deadline))
        kwargs["timeout_seconds"] = max(1.0, timeout)
        kwargs["deadline_monotonic"] = deadline

        # Reservation and checkpoint happen before the socket can be opened.  A
        # killed process therefore cannot resume and silently spend the same call twice.
        self._budget.provider_calls += 1
        self._budget.committed_token_ceiling += token_ceiling
        self._checkpoint()
        result = getattr(self._inner, method)(messages, **kwargs)
        self._budget.provider_responses += 1
        self._budget.observe_usage(getattr(result, "usage", None))
        self._checkpoint()
        return result


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def estimated_call_token_ceiling(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Any,
    max_tokens: int,
) -> int:
    """Return a conservative UTF-8 byte ceiling for prompt plus completion."""

    payload = {"messages": list(messages), "tools": tools or []}
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return len(serialized.encode("utf-8")) + int(max_tokens) + 512


def _string_tuple(value: Any, *, field_name: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list | tuple):
        raise CorpusError(f"{field_name} must be a string or list")
    output: list[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if not pattern.fullmatch(item):
            raise CorpusError(f"{field_name} contains an invalid token")
        output.append(item)
    return tuple(output)


def _any_groups(value: Any, *, field_name: str) -> tuple[tuple[str, ...], ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, str):
        return ((_string_tuple(value, field_name=field_name, pattern=_TOOL_NAME)[0],),)
    if not isinstance(value, list | tuple):
        raise CorpusError(f"{field_name} must be a string or list")
    if all(isinstance(item, str) for item in value):
        return (_string_tuple(value, field_name=field_name, pattern=_TOOL_NAME),)
    groups: list[tuple[str, ...]] = []
    for raw_group in value:
        group = _string_tuple(raw_group, field_name=field_name, pattern=_TOOL_NAME)
        if not group:
            raise CorpusError(f"{field_name} contains an empty group")
        groups.append(group)
    return tuple(groups)


_CONTROL_ENUMS: dict[tuple[str, str], frozenset[str]] = {
    (
        "build_timetable_proposal",
        "mode",
    ): frozenset({"around_current", "from_scratch"}),
    (
        "graduation_progress",
        "planning_baseline_kind",
    ): frozenset({"recommended_current_term", "registered_timetable"}),
    (
        "course_choice_comparison",
        "objective",
    ): frozenset({"balanced", "graduation", "unlock_impact", "timetable_fit"}),
    (
        "recommend_feasible_course_addition",
        "objective",
    ): frozenset({"balanced", "faster_graduation", "unlock_impact", "timetable_fit"}),
    (
        "rank_current_course_drop_impact",
        "objective",
    ): frozenset(
        {
            "least_graduation_delay",
            "prerequisite_continuity",
            "lowest_academic_priority",
            "balanced",
        }
    ),
    (
        "improve_current_timetable",
        "objective",
    ): frozenset({"balanced", "faster_graduation", "academic_priority", "schedule_quality"}),
    (
        "improve_current_timetable",
        "credit_load_policy",
    ): frozenset({"preserve", "not_increase", "within_policy"}),
}


def _safe_control_values(tool: str, values: Mapping[str, Any], *, strict: bool) -> dict[str, Any]:
    """Whitelist bounded, identity-free semantic controls for scoring artifacts."""

    output: dict[str, Any] = {}

    def reject(message: str) -> None:
        if strict:
            raise CorpusError(message)

    for raw_key, value in values.items():
        key = str(raw_key)
        if key not in _SAFE_CONTROL_KEYS.get(tool, frozenset()):
            continue
        enum = _CONTROL_ENUMS.get((tool, key))
        if enum is not None:
            token = str(value or "").strip()
            if token not in enum:
                reject(f"required_controls.{tool}.{key} is not a supported enum value")
                continue
            output[key] = token
            continue
        if key == "additional_credit_hours":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
                reject(
                    "required_controls.recommend_feasible_course_addition."
                    "additional_credit_hours must be an integer from 1 to 12"
                )
                continue
            output[key] = value
            continue
        if key in {"max_credits", "target_credits"}:
            # Evidence schemas have a positive-integer floor but no useful
            # artifact ceiling. Keep the scorer identity-free and bounded while
            # retaining every plausible academic credit limit.
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99:
                reject(f"required_controls.{tool}.{key} must be an integer from 1 to 99")
                continue
            output[key] = value
            continue
        if key == "priority_limit":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
                reject(
                    "required_controls.my_progress.priority_limit must be an integer from 1 to 20"
                )
                continue
            output[key] = value
            continue
        if key in {"allow_course_replacements", "search_better_replacements"}:
            if not isinstance(value, bool):
                reject(f"required_controls.{tool}.{key} must be a boolean")
                continue
            output[key] = value
            continue
        if key in {
            "candidate_courses",
            "course_codes",
            "must_take_courses",
            "add_current_courses",
            "noncompletion_current_courses",
            "remove_current_courses",
        }:
            if not isinstance(value, list | tuple):
                reject(f"required_controls.{tool}.{key} must be a course-code list")
                continue
            codes: list[str] = []
            valid = True
            for raw_code in value:
                code = str(raw_code or "").strip().upper().replace(" ", "")
                if not _COURSE_CODE.fullmatch(code) or code in codes:
                    valid = False
                    break
                codes.append(code)
            if not valid or not codes or len(codes) > 20:
                reject(f"required_controls.{tool}.{key} is not a bounded unique course list")
                continue
            output[key] = codes
            continue
        if key == "pinned_sections":
            if not isinstance(value, list | tuple):
                reject(f"required_controls.{tool}.pinned_sections must be a bounded list")
                continue
            pins: list[dict[str, str]] = []
            valid = True
            for raw_pin in value:
                if not isinstance(raw_pin, Mapping) or set(raw_pin) != {
                    "course_code",
                    "section_label",
                }:
                    valid = False
                    break
                code = str(raw_pin.get("course_code") or "").strip().upper().replace(" ", "")
                label = str(raw_pin.get("section_label") or "").strip().upper()
                pin = {"course_code": code, "section_label": label}
                if (
                    not _COURSE_CODE.fullmatch(code)
                    or not _SECTION_LABEL.fullmatch(label)
                    or pin in pins
                ):
                    valid = False
                    break
                pins.append(pin)
            if not valid or not pins or len(pins) > 10:
                reject(f"required_controls.{tool}.pinned_sections is not a bounded unique pin list")
                continue
            output[key] = pins
    return output


def _case_contract(
    raw_case: Mapping[str, Any], *, _allow_acceptable_plans: bool = True
) -> CaseContract:
    nested = raw_case.get("contract")
    values: dict[str, Any] = dict(nested) if isinstance(nested, Mapping) else {}
    for name in (
        "support_level",
        "support",
        "mode",
        "expected_decision",
        "expected_decisions",
        "expected_outcomes",
        "requested_outcomes",
        "clarification_kind",
        "expected_tools",
        "required_tools",
        "required_tools_all",
        "required_tools_any",
        "allowed_tools",
        "forbidden_tools",
        "required_controls",
        "acceptable_plans",
    ):
        if name in raw_case and name not in values:
            values[name] = raw_case[name]

    support = str(values.get("support_level", values.get("support", "")) or "").strip()
    if support and not _SAFE_TOKEN.fullmatch(support):
        raise CorpusError("support_level contains an invalid token")
    decisions = _string_tuple(
        values.get(
            "expected_decisions",
            values.get("expected_decision", values.get("mode")),
        ),
        field_name="expected_decisions",
        pattern=_SAFE_TOKEN,
    )
    outcomes = _string_tuple(
        values.get("expected_outcomes", values.get("requested_outcomes")),
        field_name="expected_outcomes",
        pattern=_TOOL_NAME,
    )
    unknown_outcomes = set(outcomes) - _VALID_OUTCOMES
    if unknown_outcomes:
        raise CorpusError(f"expected_outcomes contains unknown values: {sorted(unknown_outcomes)}")
    clarification_kind = str(values.get("clarification_kind") or "none").strip()
    if clarification_kind not in _CLARIFICATION_KINDS:
        raise CorpusError("clarification_kind contains an unknown value")
    if "clarify" in decisions and decisions != ("clarify",):
        raise CorpusError("clarify cannot share expected_decisions; use a typed acceptable_plan")
    if decisions:
        if (decisions == ("clarify",)) != (clarification_kind != "none"):
            raise CorpusError("clarification_kind must be non-none only for a clarify decision")

    exact_tools: tuple[str, ...] | None = None
    expected = values.get("expected_tools")
    required_all: tuple[str, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()
    if isinstance(expected, Mapping):
        required_all = _string_tuple(
            expected.get("all", expected.get("all_of")),
            field_name="expected_tools.all",
            pattern=_TOOL_NAME,
        )
        required_any = _any_groups(
            expected.get("any", expected.get("any_of")),
            field_name="expected_tools.any",
        )
    elif expected is not None:
        exact_tools = _string_tuple(
            expected,
            field_name="expected_tools",
            pattern=_TOOL_NAME,
        )
        required_all = exact_tools

    required = values.get("required_tools")
    if isinstance(required, Mapping):
        required_all += _string_tuple(
            required.get("all", required.get("all_of")),
            field_name="required_tools.all",
            pattern=_TOOL_NAME,
        )
        required_any += _any_groups(
            required.get("any", required.get("any_of")),
            field_name="required_tools.any",
        )
    elif required is not None:
        required_all += _string_tuple(
            required,
            field_name="required_tools",
            pattern=_TOOL_NAME,
        )
    required_all += _string_tuple(
        values.get("required_tools_all"),
        field_name="required_tools_all",
        pattern=_TOOL_NAME,
    )
    required_any += _any_groups(values.get("required_tools_any"), field_name="required_tools_any")
    allowed = _string_tuple(
        values.get("allowed_tools"), field_name="allowed_tools", pattern=_TOOL_NAME
    )
    forbidden = _string_tuple(
        values.get("forbidden_tools"), field_name="forbidden_tools", pattern=_TOOL_NAME
    )
    raw_controls = values.get("required_controls") or {}
    if not isinstance(raw_controls, Mapping):
        raise CorpusError("required_controls must be an object")
    required_controls: dict[str, dict[str, Any]] = {}
    for raw_tool, raw_values in raw_controls.items():
        tool = str(raw_tool or "")
        if not _TOOL_NAME.fullmatch(tool) or tool not in _SAFE_CONTROL_KEYS:
            raise CorpusError("required_controls contains an unsupported capability")
        if not isinstance(raw_values, Mapping):
            raise CorpusError(f"required_controls.{tool} must be an object")
        extras = set(str(key) for key in raw_values) - _SAFE_CONTROL_KEYS[tool]
        if extras:
            raise CorpusError(
                f"required_controls.{tool} contains unsupported keys: {sorted(extras)}"
            )
        required_controls[tool] = _safe_control_values(tool, raw_values, strict=True)
    if exact_tools is not None and not allowed:
        allowed = tuple(dict.fromkeys(exact_tools))
    canonical = CaseContract(
        support_level=support,
        expected_decisions=decisions,
        expected_outcomes=outcomes,
        clarification_kind=clarification_kind,
        exact_tools=exact_tools,
        required_all=required_all,
        required_any=required_any,
        allowed_tools=allowed,
        forbidden_tools=forbidden,
        required_controls=required_controls,
    )
    raw_acceptable = values.get("acceptable_plans") or []
    if not _allow_acceptable_plans:
        if raw_acceptable:
            raise CorpusError("acceptable_plans cannot be nested")
        return canonical
    if not isinstance(raw_acceptable, list | tuple):
        raise CorpusError("acceptable_plans must be a list")
    if len(raw_acceptable) > 8:
        raise CorpusError("acceptable_plans cannot contain more than 8 alternatives")
    allowed_alternative_keys = {
        "mode",
        "expected_decision",
        "expected_decisions",
        "expected_outcomes",
        "requested_outcomes",
        "clarification_kind",
        "expected_tools",
        "required_tools",
        "required_tools_all",
        "required_tools_any",
        "allowed_tools",
        "forbidden_tools",
        "required_controls",
    }
    acceptable: list[CaseContract] = []
    seen_alternatives: set[str] = set()
    canonical_projection = json.dumps(
        dataclasses.asdict(canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for index, raw_alternative in enumerate(raw_acceptable, 1):
        if not isinstance(raw_alternative, Mapping):
            raise CorpusError(f"acceptable_plans[{index}] must be an object")
        extras = set(str(key) for key in raw_alternative) - allowed_alternative_keys
        if extras:
            raise CorpusError(
                f"acceptable_plans[{index}] contains unsupported keys: {sorted(extras)}"
            )
        merged = dict(values)
        merged.pop("acceptable_plans", None)
        merged.update(raw_alternative)
        alternative = _case_contract({"contract": merged}, _allow_acceptable_plans=False)
        projection = json.dumps(
            dataclasses.asdict(alternative),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if projection == canonical_projection or projection in seen_alternatives:
            raise CorpusError("acceptable_plans must be distinct from canonical and each other")
        acceptable.append(alternative)
        seen_alternatives.add(projection)
    return dataclasses.replace(canonical, acceptable_plans=tuple(acceptable))


def load_corpus(path: pathlib.Path) -> CorpusData:
    """Load the small public contract projection; discard all grounding evidence."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError("corpus could not be read") from exc
    try:
        loaded = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CorpusError("corpus is not valid YAML/JSON") from exc
    if isinstance(loaded, list):
        raw_cases = loaded
        meta: Mapping[str, Any] = {}
    elif isinstance(loaded, Mapping):
        raw_cases = loaded.get("cases", loaded.get("records"))
        raw_meta = loaded.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    else:
        raise CorpusError("corpus root must be a list or object")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CorpusError("corpus must contain a non-empty cases/records list")
    if len(raw_cases) > MAX_CASES:
        raise CorpusError(f"corpus cannot exceed {MAX_CASES} cases")

    cases: list[CorpusCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, Mapping):
            raise CorpusError(f"case {index} must be an object")
        case_id = str(raw.get("case_id", raw.get("id", "")) or "").strip()
        if not _CASE_ID.fullmatch(case_id) or case_id in seen:
            raise CorpusError(f"case {index} needs a unique safe id")
        question = str(
            raw.get(
                "grounded_utterance_ar",
                raw.get("question", raw.get("utterance_ar", raw.get("prompt", ""))),
            )
            or ""
        ).strip()
        if not question or len(question) > 4000:
            raise CorpusError(f"{case_id} question must contain 1-4000 characters")
        if _PLACEHOLDER.search(question):
            raise CorpusError(f"{case_id} live question contains an unresolved placeholder")
        if _EMAIL.search(question) or _PHONE.search(question) or _LONG_NUMBER.search(question):
            raise CorpusError(f"{case_id} live question contains identity-shaped content")
        category = str(raw.get("category_id", raw.get("category", "uncategorized")) or "")
        category = category.strip()
        if not _SAFE_TOKEN.fullmatch(category):
            raise CorpusError(f"{case_id} category contains an invalid token")
        cases.append(
            CorpusCase(
                case_id=case_id,
                category=category,
                question=question,
                contract=_case_contract(raw),
            )
        )
        seen.add(case_id)

    corpus_id = str(meta.get("name", meta.get("id", "corpus")) or "corpus").strip()
    if not _SAFE_TOKEN.fullmatch(corpus_id):
        corpus_id = "corpus"
    canonical = [
        {
            "case_id": case.case_id,
            "category": case.category,
            "question": case.question,
            "contract": dataclasses.asdict(case.contract),
        }
        for case in cases
    ]
    execution_digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return CorpusData(
        corpus_id=corpus_id,
        sha256=source_digest,
        execution_sha256=execution_digest,
        cases=tuple(cases),
    )


def _sanitize_text(value: Any, *, student_id: int | None = None, student_name: str = "") -> str:
    text = str(value or "")
    if isinstance(student_id, int) and student_id > 0:
        digit_variants = {
            "0": "[0٠۰]",
            "1": "[1١۱]",
            "2": "[2٢۲]",
            "3": "[3٣۳]",
            "4": "[4٤۴]",
            "5": "[5٥۵]",
            "6": "[6٦۶]",
            "7": "[7٧۷]",
            "8": "[8٨۸]",
            "9": "[9٩۹]",
        }
        id_pattern = r"[\s\u200e\u200f-]*".join(digit_variants[digit] for digit in str(student_id))
        text = re.sub(
            rf"(?<![0-9٠-٩۰-۹]){id_pattern}(?![0-9٠-٩۰-۹])",
            "[REDACTED_STUDENT_ID]",
            text,
        )
    clean_name = str(student_name or "").strip()
    if len(clean_name) >= 3:
        # Redact the stored full name and its non-trivial components.  Models can
        # address a student by a first/family name even when evidence held the full
        # string; retaining either component would defeat the artifact's purpose.
        candidates = {clean_name, *(part for part in clean_name.split() if len(part) >= 3)}
        for candidate in sorted(candidates, key=len, reverse=True):
            text = re.sub(
                re.escape(candidate),
                "[REDACTED_STUDENT_NAME]",
                text,
                flags=re.I,
            )
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    text = _LONG_NUMBER.sub("[REDACTED_LONG_NUMBER]", text)
    return text[:MAX_ANSWER_CHARS]


def _error_category(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _SAFE_TOKEN.fullmatch(name) else "RuntimeError"


def _student_ref(secret: str, student_id: int) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"v21-saudi-e2e:{student_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _live_fingerprint_secret() -> str:
    """Return the operator-supplied artifact HMAC key, never Django's dev key."""

    value = os.environ.get(_FINGERPRINT_KEY_ENV, "")
    encoded = value.encode("utf-8")
    if not 32 <= len(encoded) <= 4096:
        raise ValueError(f"--live requires {_FINGERPRINT_KEY_ENV} to contain 32-4096 UTF-8 bytes")
    return value


def _provider_config_hmac_sha256(*, secret: str, config: Any, requested_model: str) -> str:
    """Fingerprint effective result-changing client settings without credentials."""

    projection = {
        "backend": str(getattr(config, "backend", "") or ""),
        "provider": str(getattr(config, "provider", "") or ""),
        "base_url": str(getattr(config, "base_url", "") or ""),
        "configured_model": str(getattr(config, "model", "") or ""),
        "requested_model": str(requested_model or ""),
        "timeout_seconds": float(getattr(config, "timeout_seconds", 0.0) or 0.0),
        "max_tokens": int(getattr(config, "max_tokens", 0) or 0),
        "max_retries": int(getattr(config, "max_retries", 0) or 0),
        "enable_thinking": bool(getattr(config, "enable_thinking", False)),
        "supports_assistant_prefill": bool(getattr(config, "supports_assistant_prefill", False)),
        "allow_model_discovery": bool(getattr(config, "allow_model_discovery", False)),
        "provider_options": getattr(config, "provider_options", {}) or {},
        "region": str(getattr(config, "region", "") or ""),
    }
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"v21-saudi-e2e:provider-config\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def _adviser_runtime_config_hmac_sha256(*, secret: str, settings: Any, limits: LiveLimits) -> str:
    """Fingerprint effective env-derived V2.1 ceilings used inside the runner."""

    max_iterations = max(1, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS", 4)))
    configured_inference = getattr(settings, "STUDENT_ADVISOR_V2_MAX_INFERENCE_CALLS", None)
    projection = {
        "plan_max_tokens": min(
            max(
                256,
                min(
                    int(getattr(settings, "STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS", 900)),
                    limits.max_tokens_per_call,
                ),
            ),
            limits.max_tokens_per_call,
        ),
        "plan_timeout_seconds": min(
            max(
                1.0,
                float(getattr(settings, "STUDENT_ADVISOR_V21_PLAN_TIMEOUT_SECONDS", 45)),
            ),
            limits.timeout_seconds,
        ),
        "max_tool_iterations": max_iterations,
        "max_tool_calls": max(1, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOOL_CALLS", 8))),
        "max_inference_calls": (
            max(2, int(configured_inference))
            if configured_inference is not None
            else max_iterations + 2
        ),
        "answer_max_tokens": min(
            max(
                256,
                min(
                    int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOKENS", 1800)),
                    limits.max_tokens_per_call,
                ),
            ),
            limits.max_tokens_per_call,
        ),
        "tool_timeout_seconds": min(
            max(
                1.0,
                min(
                    float(getattr(settings, "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS", 75)),
                    limits.timeout_seconds,
                ),
            ),
            limits.timeout_seconds,
        ),
        "turn_budget_seconds": max(
            15.0,
            float(getattr(settings, "STUDENT_ADVISOR_V2_TURN_BUDGET_SECONDS", 90)),
        ),
    }
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"v21-saudi-e2e:adviser-runtime-config\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def _runtime_environment_identity() -> dict[str, str]:
    """Closed, credential-free interpreter/framework identity for artifact scope."""

    import django

    try:
        ortools_version = importlib.metadata.version("ortools")
    except importlib.metadata.PackageNotFoundError:
        ortools_version = ""
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_cache_tag": str(getattr(sys.implementation, "cache_tag", "") or ""),
        "django_version": django.get_version(),
        "pyyaml_version": str(getattr(yaml, "__version__", "") or ""),
        "ortools_version": ortools_version,
        "sqlite_version": sqlite3.sqlite_version,
    }


def _iso_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _local_module_path(module: str) -> pathlib.Path | None:
    """Resolve one import name to a project Python source without importing it."""

    parts = tuple(part for part in module.split(".") if part)
    if not parts or parts[0] not in _RUNTIME_FINGERPRINT_NAMESPACES:
        return None
    stem = ROOT.joinpath(*parts)
    candidates = (stem.with_suffix(".py"), stem / "__init__.py")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(ROOT.resolve(strict=True))
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _module_name_for_path(path: pathlib.Path) -> tuple[str, bool]:
    relative = path.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    parts = list(relative.with_suffix("").parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _source_imports(path: pathlib.Path) -> set[str]:
    """Return statically reachable local import names, including lazy imports."""

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        relative = path.relative_to(ROOT).as_posix()
        raise ValueError(f"runtime fingerprint source is unreadable: {relative}") from exc

    module, is_package = _module_name_for_path(path)
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            keep = len(package_parts) - (node.level - 1)
            if keep <= 0:
                continue
            base_parts = package_parts[:keep]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = str(node.module or "")
        if base:
            found.add(base)
        for alias in node.names:
            if alias.name != "*":
                found.add(f"{base}.{alias.name}" if base else alias.name)
    return found


def _runtime_fingerprint_paths() -> tuple[pathlib.Path, ...]:
    """Build a bounded AST import closure for the exact local adviser runtime.

    Parsing source rather than importing it captures imports inside capability
    executors while avoiding Django setup or provider construction.  Project-local
    package initializers are included because Python executes them during import.
    External dependencies are intentionally outside this source fingerprint.
    """

    root = ROOT.resolve(strict=True)
    pending: set[str] = set()
    paths_by_module: dict[str, pathlib.Path] = {}

    def enqueue(module: str) -> None:
        path = _local_module_path(module)
        if path is None or module in paths_by_module or module in pending:
            return
        pending.add(module)
        parent = module.rpartition(".")[0]
        while parent:
            parent_path = _local_module_path(parent)
            if parent_path is not None and parent not in paths_by_module:
                pending.add(parent)
            parent = parent.rpartition(".")[0]

    for relative in _RUNTIME_FINGERPRINT_ENTRYPOINTS:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
            module, _ = _module_name_for_path(candidate)
        except (OSError, ValueError) as exc:
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}") from exc
        if not candidate.is_file():
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}")
        enqueue(module)

    total_bytes = 0
    while pending:
        module = min(pending)
        pending.remove(module)
        path = _local_module_path(module)
        if path is None or module in paths_by_module:
            continue
        paths_by_module[module] = path
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            relative = path.relative_to(root).as_posix()
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}") from exc
        if len(paths_by_module) > _MAX_RUNTIME_FINGERPRINT_FILES:
            raise ValueError("runtime fingerprint dependency closure is too large")
        if total_bytes > _MAX_RUNTIME_FINGERPRINT_BYTES:
            raise ValueError("runtime fingerprint dependency closure is too large")
        for imported in _source_imports(path):
            enqueue(imported)

    return tuple(
        sorted(paths_by_module.values(), key=lambda item: item.relative_to(root).as_posix())
    )


def _runtime_source_provenance() -> dict[str, Any]:
    """Return a bounded aggregate digest and per-file content manifest."""

    digest = hashlib.sha256()
    root = ROOT.resolve(strict=True)
    paths = list(_runtime_fingerprint_paths())
    for relative in _RUNTIME_FINGERPRINT_MANIFESTS:
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}") from exc
        if not path.is_file():
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}")
        paths.append(path)
    if len(paths) > _MAX_RUNTIME_FINGERPRINT_FILES:
        raise ValueError("runtime fingerprint dependency closure is too large")
    try:
        total_bytes = sum(path.stat().st_size for path in paths)
    except OSError as exc:
        raise ValueError("runtime fingerprint source is unreadable") from exc
    if total_bytes > _MAX_RUNTIME_FINGERPRINT_BYTES:
        raise ValueError("runtime fingerprint dependency closure is too large")
    manifest: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"runtime fingerprint source is unreadable: {relative}") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        manifest.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {"sha256": digest.hexdigest(), "files": manifest}


def _runtime_source_sha256() -> str:
    """Fingerprint local import closure plus pinned dependency/tool manifests."""

    return str(_runtime_source_provenance()["sha256"])


def _runtime_source_manifest() -> list[dict[str, Any]]:
    """Expose the safe bounded per-file source identity used by the aggregate."""

    return list(_runtime_source_provenance()["files"])


def _git_worktree_identity() -> dict[str, Any]:
    """Record commit and dirty flags without persisting changed file names."""

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        head_result = run("rev-parse", "--verify", "HEAD")
        head = head_result.stdout.strip().lower()
        if head_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            return {
                "available": False,
                "head": "",
                "dirty_tracked": None,
                "has_untracked": None,
            }
        worktree = run("diff", "--quiet", "--ignore-submodules", "--")
        index = run("diff", "--cached", "--quiet", "--ignore-submodules", "--")
        untracked = run("ls-files", "--others", "--exclude-standard")
        if any(result.returncode not in {0, 1} for result in (worktree, index)) or (
            untracked.returncode != 0
        ):
            raise ValueError("git status unavailable")
        return {
            "available": True,
            "head": head,
            "dirty_tracked": worktree.returncode == 1 or index.returncode == 1,
            "has_untracked": bool(untracked.stdout),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "available": False,
            "head": "",
            "dirty_tracked": None,
            "has_untracked": None,
        }


@dataclass(frozen=True)
class _FixtureQuerySpec:
    label: str
    queryset: Any
    fields: tuple[str, ...] = ()
    order_by: tuple[str, ...] = ()
    distinct: bool = False


def _fixture_query_specs(student_id: int) -> tuple[_FixtureQuerySpec, ...]:
    """Return the bounded ORM scopes that can change a student-adviser result."""

    from core.models import (
        AcademicAdvisor,
        Course,
        ElectiveCourse,
        ElectiveTermMapping,
        Prerequisite,
        ProgrammeRequirement,
        Student,
        StudentCourse,
        StudentTermSection,
        TermSection,
        TermSectionMeeting,
        TermSectionProgram,
    )

    linked_advisor_ids = Student.objects.filter(student_id=student_id).values("advisor_id")
    return (
        _FixtureQuerySpec("student", Student.objects.filter(student_id=student_id)),
        _FixtureQuerySpec("student_courses", StudentCourse.objects.filter(student_id=student_id)),
        _FixtureQuerySpec("course_catalogue", Course.objects.all()),
        _FixtureQuerySpec("programme_requirements", ProgrammeRequirement.objects.all()),
        _FixtureQuerySpec("prerequisites", Prerequisite.objects.all()),
        _FixtureQuerySpec("elective_courses", ElectiveCourse.objects.all()),
        _FixtureQuerySpec("elective_term_mappings", ElectiveTermMapping.objects.all()),
        _FixtureQuerySpec(
            "linked_academic_advisor",
            AcademicAdvisor.objects.filter(advisor_id__in=linked_advisor_ids),
        ),
        _FixtureQuerySpec(
            "global_term_sections", TermSection.objects.filter(scenario__isnull=True)
        ),
        _FixtureQuerySpec(
            "scenario_term_section_name_fallbacks",
            TermSection.objects.filter(scenario__isnull=False),
            fields=("id", "course_key", "course_name"),
        ),
        _FixtureQuerySpec(
            "global_term_section_programs",
            TermSectionProgram.objects.filter(term_section__scenario__isnull=True),
        ),
        _FixtureQuerySpec(
            "global_term_section_meetings",
            TermSectionMeeting.objects.filter(term_section__scenario__isnull=True),
        ),
        _FixtureQuerySpec(
            "student_global_term_sections",
            StudentTermSection.objects.filter(
                student_id=student_id,
                term_section__scenario__isnull=True,
            ),
        ),
        _FixtureQuerySpec(
            "published_term_pairs",
            StudentTermSection.objects.filter(term_section__scenario__isnull=True),
            fields=("academic_year", "term"),
            order_by=("academic_year", "term"),
            distinct=True,
        ),
    )


def _fixture_state_hmac_sha256(
    *,
    secret: str,
    student_id: int,
    academic_year: int | None,
    term: int | None,
    query_specs: Sequence[_FixtureQuerySpec] | None = None,
    policy_root: pathlib.Path | None = None,
    site_defaults: Mapping[str, Any] | None = None,
    as_of_date: dt.date | None = None,
) -> str:
    """Keyed fingerprint of result-changing DB rows and approved policy inputs.

    Only the HMAC is persisted.  Student identifiers, names, marks, adviser details,
    timetable rows, and policy contents never leave this function.  Every query is
    explicitly student-scoped or fixture-static, rows are streamed in primary-key
    order, and hard row/byte limits fail closed instead of silently truncating.
    """

    if not secret:
        raise ValueError("fixture-state fingerprint secret is empty")
    digest = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)

    def update(label: str, payload: bytes) -> None:
        label_bytes = label.encode("utf-8")
        digest.update(len(label_bytes).to_bytes(4, "big"))
        digest.update(label_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def json_default(value: Any) -> str:
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
        if isinstance(value, bytes):
            return value.hex()
        return str(value)

    update("version", _FIXTURE_STATE_FINGERPRINT_VERSION.encode("ascii"))
    update(
        "evaluation_scope",
        json.dumps(
            {"academic_year": academic_year, "term": term},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    if site_defaults is None:
        from core.settings_views import load_defaults

        site_defaults = load_defaults()
    update(
        "site_defaults",
        json.dumps(
            {
                key: site_defaults.get(key)
                for key in ("academic_year", "term", "currentYear", "currentTerm")
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    update(
        "policy_as_of_date",
        (as_of_date or dt.date.today()).isoformat().encode("ascii"),
    )

    row_count = 0
    effective_query_specs = (
        query_specs if query_specs is not None else _fixture_query_specs(student_id)
    )
    for spec in effective_query_specs:
        label = spec.label
        queryset = spec.queryset
        model = getattr(queryset, "model", None)
        meta = getattr(model, "_meta", None)
        concrete_fields = tuple(getattr(meta, "concrete_fields", ()) or ())
        fields = spec.fields or tuple(str(field.attname) for field in concrete_fields)
        primary_key = str(getattr(getattr(meta, "pk", None), "attname", "") or "")
        if not fields or not primary_key:
            raise ValueError(f"fixture-state query has no concrete model: {label}")
        update(f"{label}:fields", json.dumps(fields).encode("utf-8"))
        values = queryset.values_list(*fields).order_by(*(spec.order_by or (primary_key,)))
        if spec.distinct:
            values = values.distinct()
        rows = values.iterator(chunk_size=2000)
        table_rows = 0
        for row in rows:
            row_count += 1
            table_rows += 1
            if row_count > _MAX_FIXTURE_STATE_ROWS:
                raise ValueError("fixture-state fingerprint row limit exceeded")
            encoded = json.dumps(
                list(row),
                ensure_ascii=False,
                separators=(",", ":"),
                default=json_default,
            ).encode("utf-8")
            update(f"{label}:row", encoded)
        update(f"{label}:count", str(table_rows).encode("ascii"))

    if policy_root is None:
        from core.services.policy_store import policy_root as configured_policy_root

        policy_root = configured_policy_root()
    try:
        resolved_policy_root = pathlib.Path(policy_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError("fixture-state policy root is unreadable") from exc
    policy_bytes = 0
    for path in sorted(resolved_policy_root.rglob("*.yaml")):
        relative = path.relative_to(resolved_policy_root)
        if path.name in _POLICY_FIXTURE_SKIP_FILES or _POLICY_FIXTURE_SKIP_DIRS & set(
            relative.parts
        ):
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError("fixture-state policy input is unreadable") from exc
        policy_bytes += len(payload)
        if policy_bytes > _MAX_FIXTURE_STATE_BYTES:
            raise ValueError("fixture-state fingerprint byte limit exceeded")
        update(f"policy:{relative.as_posix()}", payload)
    update("policy:bytes", str(policy_bytes).encode("ascii"))
    return digest.hexdigest()


def _atomic_write(path: pathlib.Path, artifact: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _artifact_hmac_sha256(secret: str, artifact: Mapping[str, Any]) -> str:
    """Authenticate the complete artifact except the digest value itself."""

    if not secret:
        raise ValueError("artifact HMAC secret is empty")
    integrity_raw = artifact.get("integrity")
    integrity = dict(integrity_raw) if isinstance(integrity_raw, Mapping) else {}
    integrity.pop("hmac_sha256", None)
    payload = {str(key): value for key, value in artifact.items() if str(key) != "integrity"}
    payload["integrity"] = integrity
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"v21-saudi-e2e:artifact\0" + rendered,
        hashlib.sha256,
    ).hexdigest()


def _seal_artifact(artifact: dict[str, Any], *, secret: str, finalized: bool) -> None:
    artifact["integrity"] = {
        "algorithm": "HMAC-SHA256",
        "scope_version": _ARTIFACT_HMAC_SCOPE_VERSION,
        "covers": "all_artifact_fields_except_integrity.hmac_sha256",
        "finalized": bool(finalized),
        "hmac_sha256": "",
    }
    artifact["integrity"]["hmac_sha256"] = _artifact_hmac_sha256(secret, artifact)


def _verify_artifact_hmac(secret: str, artifact: Mapping[str, Any]) -> bool:
    integrity = artifact.get("integrity")
    if not isinstance(integrity, Mapping):
        return False
    if (
        integrity.get("algorithm") != "HMAC-SHA256"
        or integrity.get("scope_version") != _ARTIFACT_HMAC_SCOPE_VERSION
        or integrity.get("covers") != "all_artifact_fields_except_integrity.hmac_sha256"
        or not isinstance(integrity.get("finalized"), bool)
    ):
        return False
    supplied = str(integrity.get("hmac_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    try:
        expected = _artifact_hmac_sha256(secret, artifact)
    except ValueError:
        return False
    return hmac.compare_digest(supplied, expected)


def _assert_safe_artifact(
    artifact: Mapping[str, Any], *, student_id: int, student_name: str
) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key) in _FORBIDDEN_ARTIFACT_KEYS:
                    raise ValueError(f"unsafe artifact key: {key}")
                visit(child)
        elif isinstance(value, list | tuple):
            for child in value:
                visit(child)

    visit(artifact)
    rendered = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
    if str(student_id) in rendered:
        raise ValueError("student identity reached the artifact")
    clean_name = str(student_name or "").strip()
    if len(clean_name) >= 3 and clean_name.casefold() in rendered.casefold():
        raise ValueError("student name reached the artifact")


def _tool_names(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    output: list[str] = []
    for raw in value:
        if isinstance(raw, str):
            name = raw
        elif isinstance(raw, Mapping):
            name = str(raw.get("name", raw.get("tool", raw.get("capability", ""))) or "")
        else:
            continue
        if _TOOL_NAME.fullmatch(name):
            output.append(name)
    return output


def _outcome_names(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    output: list[str] = []
    for raw in value:
        outcome = str(getattr(raw, "value", raw) or "").strip().lower()
        if outcome in _VALID_OUTCOMES:
            output.append(outcome)
    return output


def _safe_plan_controls(value: Any) -> dict[str, dict[str, Any]]:
    """Project executed effective arguments to closed semantic controls only."""

    if not isinstance(value, list | tuple):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            continue
        tool = str(row.get("name", row.get("tool", "")) or "")
        raw = row.get("arguments")
        if tool not in _SAFE_CONTROL_KEYS or not isinstance(raw, Mapping):
            continue
        output[tool] = _safe_control_values(tool, raw, strict=False)
    return output


def _partial_match(actual: Any, expected: Any) -> bool:
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
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _outcome_coverage_correct(
    *,
    decision: str,
    outcomes: Sequence[str],
    tools: Sequence[str],
    controls: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    if not outcomes or len(outcomes) != len(set(outcomes)):
        return False
    outcome_set = frozenset(outcomes)
    tool_set = frozenset(tools)
    if decision == "direct":
        return outcome_set == {"general_conversation"} and not tools
    if decision == "unsupported":
        return bool(outcome_set) and outcome_set <= _UNSUPPORTED_OUTCOMES and not tools
    if decision == "clarify":
        return not tools and not outcome_set & (_UNSUPPORTED_OUTCOMES | {"general_conversation"})
    if decision != "execute" or not tools:
        return False
    if outcome_set & _EXECUTE_FORBIDDEN_OUTCOMES:
        return False
    graduation_controls = (controls or {}).get("graduation_progress") or {}
    additions = set(graduation_controls.get("add_current_courses") or [])
    removals = set(graduation_controls.get("remove_current_courses") or [])
    noncompletion = set(graduation_controls.get("noncompletion_current_courses") or [])
    explicit_changes = bool(additions or removals or noncompletion)
    if graduation_controls.get("search_better_replacements") is True and explicit_changes:
        return False
    if noncompletion and (
        graduation_controls.get("planning_baseline_kind") != "registered_timetable"
        or bool(noncompletion & (additions | removals))
    ):
        return False
    covered = all(
        outcome in _SERVER_OWNED_EXECUTE_OUTCOMES
        or bool(_owners_for_outcome(outcome, outcome_set, controls) & tool_set)
        for outcome in outcomes
    )
    justified = frozenset(
        tool for outcome in outcomes for tool in _owners_for_outcome(outcome, outcome_set, controls)
    )
    if not covered or not tool_set <= justified or len(tools) != len(tool_set):
        return False
    return all(
        not all(
            outcome in _SERVER_OWNED_EXECUTE_OUTCOMES
            or bool(_owners_for_outcome(outcome, outcome_set, controls) & (tool_set - {tool}))
            for outcome in outcomes
        )
        for tool in tools
    )


def _score_plan_expectation(
    contract: CaseContract,
    *,
    decision: str,
    clarification_kind: str = "none",
    tools: Sequence[str],
    outcomes: Sequence[str] = (),
    controls: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    actual = list(tools)
    actual_outcomes = list(outcomes)
    actual_controls = {
        str(tool): dict(values)
        for tool, values in (controls or {}).items()
        if isinstance(values, Mapping)
    }
    counts = collections.Counter(actual)
    expected_counts = (
        collections.Counter(contract.exact_tools) if contract.exact_tools is not None else None
    )
    checks: dict[str, bool | None] = {
        "decision": (
            decision in contract.expected_decisions if contract.expected_decisions else None
        ),
        "clarification_kind": (clarification_kind == contract.clarification_kind),
        "expected_outcomes": (
            frozenset(actual_outcomes) == frozenset(contract.expected_outcomes)
            if contract.expected_outcomes
            else None
        ),
        "outcome_coverage": (
            _outcome_coverage_correct(
                decision=decision,
                outcomes=actual_outcomes,
                tools=actual,
                controls=actual_controls,
            )
            if contract.expected_outcomes
            else None
        ),
        "required_all": (
            all(
                counts[name] >= count
                for name, count in collections.Counter(contract.required_all).items()
            )
            if contract.required_all
            else None
        ),
        "required_any": (
            all(any(counts[name] for name in group) for group in contract.required_any)
            if contract.required_any
            else None
        ),
        "allowed_only": (
            set(actual) <= set(contract.allowed_tools) if contract.allowed_tools else None
        ),
        "forbidden_absent": (
            not (set(actual) & set(contract.forbidden_tools)) if contract.forbidden_tools else None
        ),
        "exact_tools": counts == expected_counts if expected_counts is not None else None,
        "required_controls": (
            all(
                tool in actual_controls
                and _partial_match(actual_controls[tool], expected)
                and (
                    tool != "my_progress"
                    or "priority_limit" not in expected
                    or set(actual_controls[tool]) == set(expected)
                )
                for tool, expected in (contract.required_controls or {}).items()
            )
            if contract.required_controls
            else None
        ),
    }
    applicable = [value for value in checks.values() if value is not None]
    if expected_counts is not None:
        minimality: bool | None = counts == expected_counts
    elif contract.allowed_tools:
        minimality = set(actual) <= set(contract.allowed_tools)
    elif contract.required_all and not contract.required_any:
        minimality = counts == collections.Counter(contract.required_all)
    else:
        minimality = None
    return {
        "support_level": contract.support_level,
        "expected_decisions": list(contract.expected_decisions),
        "observed_decision": decision,
        "expected_clarification_kind": contract.clarification_kind,
        "observed_clarification_kind": clarification_kind,
        "expected_outcomes": list(contract.expected_outcomes),
        "observed_outcomes": actual_outcomes,
        "expected_tools": list(contract.exact_tools) if contract.exact_tools is not None else None,
        "required_all": list(contract.required_all),
        "required_any": [list(group) for group in contract.required_any],
        "allowed_tools": list(contract.allowed_tools),
        "forbidden_tools": list(contract.forbidden_tools),
        "observed_tools": actual,
        "required_controls": dict(contract.required_controls or {}),
        "observed_controls": actual_controls,
        "checks": checks,
        "plan_exact": checks["exact_tools"],
        "tool_minimality": minimality,
        "passed": all(applicable) if applicable else None,
    }


def score_plan(
    contract: CaseContract,
    *,
    decision: str,
    clarification_kind: str = "none",
    tools: Sequence[str],
    outcomes: Sequence[str] = (),
    controls: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score one observed plan without losing strict-canonical diagnostics.

    ``passed`` accepts either the canonical contract or one explicitly adjudicated
    alternative.  The canonical checks remain at the top level and
    ``strict_canonical_passed`` never changes merely because an alternative matched.
    """

    canonical = _score_plan_expectation(
        contract,
        decision=decision,
        clarification_kind=clarification_kind,
        tools=tools,
        outcomes=outcomes,
        controls=controls,
    )
    alternative_rows: list[dict[str, Any]] = []
    matched_expectation = "canonical" if canonical["passed"] is True else ""
    for index, alternative in enumerate(contract.acceptable_plans, 1):
        scored = _score_plan_expectation(
            alternative,
            decision=decision,
            clarification_kind=clarification_kind,
            tools=tools,
            outcomes=outcomes,
            controls=controls,
        )
        alternative_rows.append(
            {
                "index": index,
                "expected_decisions": scored["expected_decisions"],
                "expected_clarification_kind": scored["expected_clarification_kind"],
                "expected_outcomes": scored["expected_outcomes"],
                "expected_tools": scored["expected_tools"],
                "required_all": scored["required_all"],
                "required_any": scored["required_any"],
                "allowed_tools": scored["allowed_tools"],
                "forbidden_tools": scored["forbidden_tools"],
                "required_controls": scored["required_controls"],
                "checks": scored["checks"],
                "plan_exact": scored["plan_exact"],
                "tool_minimality": scored["tool_minimality"],
                "passed": scored["passed"],
            }
        )
        if not matched_expectation and scored["passed"] is True:
            matched_expectation = f"alternative_{index}"
    canonical["strict_canonical_passed"] = canonical["passed"]
    canonical["acceptable_alternatives"] = alternative_rows
    canonical["matched_expectation"] = matched_expectation
    canonical["passed"] = bool(matched_expectation) if canonical["passed"] is not None else None
    return canonical


def _safe_evidence_audit(value: Any) -> dict[str, Any]:
    from core.services.advisor_evidence_audit import normalise_evidence_audit

    return normalise_evidence_audit(value)


def result_row(
    case: CorpusCase,
    result: Mapping[str, Any],
    *,
    student_id: int,
    student_name: str,
    latency_ms: int,
    call_delta: int,
    token_ceiling_delta: int,
) -> dict[str, Any]:
    raw_agent = result.get("agent")
    agent: Mapping[str, Any] = raw_agent if isinstance(raw_agent, Mapping) else {}
    decision = str(agent.get("semantic_plan_decision") or "")
    if not _SAFE_TOKEN.fullmatch(decision):
        decision = ""
    clarification_kind = str(agent.get("semantic_plan_clarification_kind") or "")
    if clarification_kind not in _CLARIFICATION_KINDS:
        clarification_kind = ""
    plan_outcomes = _outcome_names(agent.get("semantic_plan_requested_outcomes"))
    plan_tools = _tool_names(agent.get("semantic_plan_tools"))
    executed_tools = _tool_names(agent.get("tools_called"))
    plan_controls = _safe_plan_controls(agent.get("tools_called"))
    raw_answer = str(result.get("answer") or "")
    answer = _sanitize_text(raw_answer, student_id=student_id, student_name=student_name)
    raw_usage = result.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    safe_usage = {
        key: _non_negative_int(usage.get(key))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    evidence_audit = _safe_evidence_audit(agent.get("evidence_audit"))
    model = str(result.get("model") or "").strip()
    if not _SAFE_TOKEN.fullmatch(model):
        model = ""
    revision = str(agent.get("model_revision") or "").strip()
    if not _SAFE_TOKEN.fullmatch(revision):
        revision = ""
    raw_flags = evidence_audit.get("flags")
    flags = raw_flags if isinstance(raw_flags, Mapping) else {}
    raw_plan_contract = evidence_audit.get("plan_contract")
    plan_contract = raw_plan_contract if isinstance(raw_plan_contract, Mapping) else {}
    planner_contract_error = str(plan_contract.get("failure_reason") or "")
    raw_provider_error = str(flags.get("provider_error") or "")
    provider_error = (
        ""
        if planner_contract_error and raw_provider_error == "LLMInvalidResponse"
        else raw_provider_error
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "question": _sanitize_text(case.question, student_id=student_id, student_name=student_name),
        "status": "completed",
        "answer": answer,
        "answer_truncated": len(raw_answer) > MAX_ANSWER_CHARS,
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "model": model,
        "model_revision": revision,
        "usage": safe_usage,
        "provider_calls": call_delta,
        "committed_token_ceiling": token_ceiling_delta,
        "latency_ms": max(0, int(latency_ms)),
        "plan": score_plan(
            case.contract,
            decision=decision,
            clarification_kind=clarification_kind,
            outcomes=plan_outcomes,
            tools=plan_tools,
            controls=plan_controls,
        ),
        "execution": {
            "complete": agent.get("semantic_plan_execution_complete") is True,
            "executed_tools": executed_tools,
        },
        "validation": evidence_audit.get("validation", {}),
        "provider_error": provider_error,
        "planner_contract_error": planner_contract_error,
        "evidence_audit": evidence_audit,
    }


def error_row(
    case: CorpusCase,
    *,
    status: str,
    error_category: str,
    student_id: int,
    student_name: str,
    latency_ms: int = 0,
    call_delta: int = 0,
    token_ceiling_delta: int = 0,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "category": case.category,
        "question": _sanitize_text(case.question, student_id=student_id, student_name=student_name),
        "status": status,
        "answer": "",
        "answer_truncated": False,
        "answer_sha256": hashlib.sha256(b"").hexdigest(),
        "error_category": error_category,
        "latency_ms": max(0, int(latency_ms)),
        "provider_calls": max(0, int(call_delta)),
        "committed_token_ceiling": max(0, int(token_ceiling_delta)),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "plan": score_plan(case.contract, decision="", clarification_kind="", tools=[]),
        "execution": {"complete": False, "executed_tools": []},
        "validation": {},
        "provider_error": error_category,
        "planner_contract_error": "",
        "evidence_audit": {},
    }


def category_aggregates(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row.get("category") or "uncategorized")].append(row)
    report: dict[str, Any] = {}
    for category, items in sorted(grouped.items()):
        scored = [
            row
            for row in items
            if isinstance(row.get("plan"), Mapping)
            and (row.get("plan") or {}).get("passed") is not None
        ]
        passed = sum((row.get("plan") or {}).get("passed") is True for row in scored)
        strict_passed = sum(
            (row.get("plan") or {}).get("strict_canonical_passed") is True for row in scored
        )
        outcomes = collections.Counter(
            str((row.get("validation") or {}).get("outcome") or "unavailable") for row in items
        )
        report[category] = {
            "total": len(items),
            "completed": sum(row.get("status") == "completed" for row in items),
            "errors": sum(row.get("status") != "completed" for row in items),
            "plan_contract": {
                "passed": passed,
                "total": len(scored),
                "rate": round(passed / len(scored), 6) if scored else None,
            },
            "strict_canonical_plan_contract": {
                "passed": strict_passed,
                "total": len(scored),
                "rate": round(strict_passed / len(scored), 6) if scored else None,
            },
            "validation_outcomes": dict(sorted(outcomes.items())),
            "execution_incomplete": sum(
                not bool((row.get("execution") or {}).get("complete")) for row in items
            ),
            "provider_errors": sum(bool(row.get("provider_error")) for row in items),
            "planner_contract_errors": sum(
                bool(row.get("planner_contract_error")) for row in items
            ),
        }
    return report


def _runtime_dependencies() -> dict[str, Any]:
    """Import Django and live dependencies only after CLI safety gates pass."""

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.db import connection
    from django.test.utils import override_settings

    from core.models import Student
    from core.services.advisor_principal import AdvisorPrincipal
    from core.services.llm_backend import get_llm_client
    from core.services.rbac import ROLE_STUDENT
    from core.services.student_advisor_v2 import STUDENT_V21_PROMPT_VERSION
    from core.services.student_advisor_v21 import answer_student_advisor_v21

    return {
        "settings": settings,
        "database_vendor": str(connection.vendor or "").strip().lower(),
        "override_settings": override_settings,
        "Student": Student,
        "AdvisorPrincipal": AdvisorPrincipal,
        "ROLE_STUDENT": ROLE_STUDENT,
        "prompt_version": STUDENT_V21_PROMPT_VERSION,
        "get_llm_client": get_llm_client,
        "fixture_state_fingerprint": _fixture_state_hmac_sha256,
        "answer": answer_student_advisor_v21,
    }


def _artifact_limits(limits: LiveLimits) -> dict[str, Any]:
    return dataclasses.asdict(limits)


def _new_artifact(
    corpus: CorpusData,
    selected: Sequence[CorpusCase],
    *,
    student_ref: str,
    backend: str,
    region: str,
    model: str,
    model_thinking_enabled: bool,
    provider_config_hmac_sha256: str,
    adviser_runtime_config_hmac_sha256: str,
    database_vendor: str,
    runtime_environment: Mapping[str, str],
    prompt_version: str,
    runtime_source_sha256: str,
    fixture_state_hmac_sha256: str,
    limits: LiveLimits,
    academic_year: int | None,
    term: int | None,
    runtime_source_manifest: Sequence[Mapping[str, Any]],
    git_worktree_identity: Mapping[str, Any],
    artifact_hmac_secret: str,
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "corpus": {
            "id": corpus.corpus_id,
            "sha256": corpus.sha256,
            "execution_sha256": corpus.execution_sha256,
            "total_cases": len(corpus.cases),
            "selected_case_ids": [case.case_id for case in selected],
        },
        "run": {
            "run_id": uuid.uuid4().hex,
            "mode": "live",
            "state": "running",
            "started_at": _iso_now(),
            "updated_at": _iso_now(),
            "student_ref": student_ref,
            "backend": backend,
            "region": region,
            "model": model,
            "model_thinking_enabled": model_thinking_enabled,
            "provider_config_hmac_sha256": provider_config_hmac_sha256,
            "adviser_runtime_config_hmac_sha256": adviser_runtime_config_hmac_sha256,
            "database_vendor": database_vendor,
            "runtime_environment": dict(runtime_environment),
            "deterministic_replay": False,
            "prompt_version": prompt_version,
            "runtime_source_sha256": runtime_source_sha256,
            "runtime_source_manifest": [dict(row) for row in runtime_source_manifest],
            "runtime_source_end_sha256": "",
            "runtime_source_end_manifest": [],
            "runtime_source_stable": None,
            "git_worktree": dict(git_worktree_identity),
            "fixture_state_hmac_sha256": fixture_state_hmac_sha256,
            "fixture_state_fingerprint_version": _FIXTURE_STATE_FINGERPRINT_VERSION,
            "fixture_state_end_hmac_sha256": "",
            "fixture_state_stable": None,
            "academic_year": academic_year,
            "term": term,
            "limits": _artifact_limits(limits),
            "usage": {},
            "current_case_id": "",
            "stopped_for": "",
            "resume_count": 0,
            "provider_retries": 0,
            "provider_call_accounting": "reserved_http_attempts",
            "conversation_persistence": False,
            "raw_evidence_persisted": False,
        },
        "summary": {},
        "rows": [],
    }
    _seal_artifact(artifact, secret=artifact_hmac_secret, finalized=False)
    return artifact


def _load_resume(
    path: pathlib.Path,
    *,
    corpus: CorpusData,
    selected: Sequence[CorpusCase],
    student_ref: str,
    backend: str,
    region: str,
    model: str,
    model_thinking_enabled: bool,
    provider_config_hmac_sha256: str,
    adviser_runtime_config_hmac_sha256: str,
    database_vendor: str,
    runtime_environment: Mapping[str, str],
    prompt_version: str,
    runtime_source_sha256: str,
    fixture_state_hmac_sha256: str,
    limits: LiveLimits,
    academic_year: int | None,
    term: int | None,
    student_id: int | None = None,
    student_name: str = "",
    runtime_source_manifest: Sequence[Mapping[str, Any]],
    artifact_hmac_secret: str,
) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("resume artifact is unreadable") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("resume artifact has the wrong schema")
    if not _verify_artifact_hmac(artifact_hmac_secret, artifact):
        raise ValueError("resume artifact HMAC does not match")
    raw_run = artifact.get("run")
    run: dict[str, Any] = dict(raw_run) if isinstance(raw_run, Mapping) else {}
    fixture_stable = run.get("fixture_state_stable")
    fixture_end = str(run.get("fixture_state_end_hmac_sha256") or "")
    if run.get("state") == "state_drift":
        raise ValueError("resume artifact recorded runtime provenance drift")
    if fixture_stable is False:
        raise ValueError("resume artifact recorded fixture state drift")
    if fixture_stable not in {None, True}:
        raise ValueError("resume artifact fixture-state status is invalid")
    if fixture_stable is True and fixture_end != run.get("fixture_state_hmac_sha256"):
        raise ValueError("resume artifact fixture-state status is invalid")
    if fixture_stable is None and fixture_end:
        raise ValueError("resume artifact fixture-state status is invalid")
    source_stable = run.get("runtime_source_stable")
    source_end = str(run.get("runtime_source_end_sha256") or "")
    if source_stable is False:
        raise ValueError("resume artifact recorded runtime source drift")
    if source_stable not in {None, True}:
        raise ValueError("resume artifact runtime-source status is invalid")
    if source_stable is True and source_end != run.get("runtime_source_sha256"):
        raise ValueError("resume artifact runtime-source status is invalid")
    if source_stable is None and source_end:
        raise ValueError("resume artifact runtime-source status is invalid")
    raw_corpus_meta = artifact.get("corpus")
    corpus_meta: Mapping[str, Any] = raw_corpus_meta if isinstance(raw_corpus_meta, Mapping) else {}
    expected = {
        "corpus_sha256": (corpus_meta.get("sha256"), corpus.sha256),
        "execution_sha256": (
            corpus_meta.get("execution_sha256"),
            corpus.execution_sha256,
        ),
        "selected_case_ids": (
            corpus_meta.get("selected_case_ids"),
            [case.case_id for case in selected],
        ),
        "student_ref": (run.get("student_ref"), student_ref),
        "backend": (run.get("backend"), backend),
        "region": (run.get("region"), region),
        "model": (run.get("model"), model),
        "model_thinking_enabled": (
            run.get("model_thinking_enabled"),
            model_thinking_enabled,
        ),
        "provider_config_hmac_sha256": (
            run.get("provider_config_hmac_sha256"),
            provider_config_hmac_sha256,
        ),
        "adviser_runtime_config_hmac_sha256": (
            run.get("adviser_runtime_config_hmac_sha256"),
            adviser_runtime_config_hmac_sha256,
        ),
        "database_vendor": (run.get("database_vendor"), database_vendor),
        "runtime_environment": (
            run.get("runtime_environment"),
            dict(runtime_environment),
        ),
        "deterministic_replay": (run.get("deterministic_replay"), False),
        "prompt_version": (run.get("prompt_version"), prompt_version),
        "runtime_source_sha256": (
            run.get("runtime_source_sha256"),
            runtime_source_sha256,
        ),
        "runtime_source_manifest": (
            run.get("runtime_source_manifest"),
            [dict(row) for row in runtime_source_manifest],
        ),
        "fixture_state_hmac_sha256": (
            run.get("fixture_state_hmac_sha256"),
            fixture_state_hmac_sha256,
        ),
        "fixture_state_fingerprint_version": (
            run.get("fixture_state_fingerprint_version"),
            _FIXTURE_STATE_FINGERPRINT_VERSION,
        ),
        "limits": (run.get("limits"), _artifact_limits(limits)),
        "academic_year": (run.get("academic_year"), academic_year),
        "term": (run.get("term"), term),
        "provider_retries": (run.get("provider_retries"), 0),
        "provider_call_accounting": (
            run.get("provider_call_accounting"),
            "reserved_http_attempts",
        ),
    }
    mismatched = [name for name, (actual, wanted) in expected.items() if actual != wanted]
    if mismatched:
        raise ValueError("resume artifact does not match: " + ", ".join(mismatched))
    raw_usage = run.get("usage")
    if not isinstance(raw_usage, Mapping):
        raise ValueError("resume artifact usage counters are invalid")
    usage_counters: dict[str, int] = {}
    for key in (
        "provider_calls",
        "provider_responses",
        "committed_token_ceiling",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ):
        raw_value = raw_usage.get(key, 0)
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
            raise ValueError("resume artifact usage counters are invalid")
        usage_counters[key] = raw_value
    if (
        usage_counters["provider_calls"] > limits.max_provider_calls
        or usage_counters["provider_responses"] > usage_counters["provider_calls"]
        or usage_counters["committed_token_ceiling"] > limits.max_total_tokens
    ):
        raise ValueError("resume artifact usage counters exceed the run limits")
    rows = artifact.get("rows")
    if not isinstance(rows, list):
        raise ValueError("resume artifact rows are invalid")
    selected_by_id = {case.case_id: case for case in selected}
    selected_ids = set(selected_by_id)
    row_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("resume artifact contains an invalid row")
        case_id = str(row.get("case_id") or "")
        if case_id not in selected_ids or case_id in row_ids:
            raise ValueError("resume artifact contains unknown or duplicate case rows")
        case = selected_by_id[case_id]
        status = str(row.get("status") or "")
        if status not in {"completed", "error", "budget_stopped", "interrupted"}:
            raise ValueError("resume artifact contains an invalid row status")
        expected_question = _sanitize_text(
            case.question,
            student_id=student_id,
            student_name=student_name,
        )
        if row.get("category") != case.category or row.get("question") != expected_question:
            raise ValueError("resume artifact row no longer matches its corpus case")
        answer = row.get("answer")
        if (
            not isinstance(answer, str)
            or _sanitize_text(
                answer,
                student_id=student_id,
                student_name=student_name,
            )
            != answer
        ):
            raise ValueError("resume artifact contains an unsanitized answer")
        if row.get("answer_sha256") != hashlib.sha256(answer.encode("utf-8")).hexdigest():
            raise ValueError("resume artifact answer checksum does not match")
        plan = row.get("plan")
        if not isinstance(plan, Mapping):
            raise ValueError("resume artifact plan is invalid")
        observed_decision = str(plan.get("observed_decision") or "")
        observed_clarification_kind = str(plan.get("observed_clarification_kind") or "")
        if observed_clarification_kind not in _CLARIFICATION_KINDS:
            raise ValueError("resume artifact plan clarification kind is invalid")
        observed_outcomes = _outcome_names(plan.get("observed_outcomes"))
        observed_tools = _tool_names(plan.get("observed_tools"))
        raw_observed_controls = plan.get("observed_controls")
        observed_controls = (
            {
                str(tool): dict(values)
                for tool, values in raw_observed_controls.items()
                if isinstance(values, Mapping)
            }
            if isinstance(raw_observed_controls, Mapping)
            else {}
        )
        if dict(plan) != score_plan(
            case.contract,
            decision=observed_decision,
            clarification_kind=observed_clarification_kind,
            outcomes=observed_outcomes,
            tools=observed_tools,
            controls=observed_controls,
        ):
            raise ValueError("resume artifact plan score does not match the corpus contract")
        for counter in ("provider_calls", "committed_token_ceiling"):
            value = row.get(counter, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("resume artifact row counters are invalid")
        row_ids.append(case_id)
    if (
        sum(int(row.get("provider_calls") or 0) for row in rows) > usage_counters["provider_calls"]
        or sum(int(row.get("committed_token_ceiling") or 0) for row in rows)
        > usage_counters["committed_token_ceiling"]
    ):
        raise ValueError("resume artifact row counters exceed cumulative usage")
    current = str(run.get("current_case_id") or "")
    if current and not any(str(row.get("case_id") or "") == current for row in rows):
        if current in selected_by_id:
            rows.append(
                error_row(
                    selected_by_id[current],
                    status="interrupted",
                    error_category="InterruptedPreviousRun",
                    student_id=student_id or -1,
                    student_name=student_name,
                )
            )
    run["current_case_id"] = ""
    run["state"] = "running"
    run["stopped_for"] = ""
    run["resume_count"] = _non_negative_int(run.get("resume_count")) + 1
    run["fixture_state_end_hmac_sha256"] = ""
    run["fixture_state_stable"] = None
    run["runtime_source_end_sha256"] = ""
    run["runtime_source_end_manifest"] = []
    run["runtime_source_stable"] = None
    artifact["run"] = dict(run)
    _seal_artifact(artifact, secret=artifact_hmac_secret, finalized=False)
    return artifact


def _readiness_gate(artifact: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    run = artifact.get("run") if isinstance(artifact.get("run"), Mapping) else {}
    rows = artifact.get("rows") if isinstance(artifact.get("rows"), list) else []
    plan = summary.get("plan_contract") if isinstance(summary.get("plan_contract"), Mapping) else {}
    selected = _non_negative_int(summary.get("selected"))
    criteria = {
        "collection_complete": (
            run.get("state") == "complete"
            and _non_negative_int(summary.get("recorded")) == selected
            and _non_negative_int(summary.get("completed")) == selected
            and _non_negative_int(summary.get("errors")) == 0
        ),
        "accepted_plan_contracts_complete": (
            selected > 0
            and _non_negative_int(plan.get("total")) == selected
            and _non_negative_int(plan.get("passed")) == selected
        ),
        "semantic_execution_complete": (
            _non_negative_int(summary.get("execution_incomplete")) == 0
        ),
        "provider_error_free": _non_negative_int(summary.get("provider_errors")) == 0,
        "planner_contract_error_free": (
            _non_negative_int(summary.get("planner_contract_errors")) == 0
        ),
        "validation_abstention_free": not any(
            isinstance(row, Mapping)
            and isinstance(row.get("validation"), Mapping)
            and (row.get("validation") or {}).get("outcome") == "abstained"
            for row in rows
        ),
        "provenance_stable": (
            run.get("runtime_source_stable") is True and run.get("fixture_state_stable") is True
        ),
    }
    failed = [name for name, passed in criteria.items() if not passed]
    return {
        "status": "GO" if not failed else "NO_GO",
        "criteria": criteria,
        "failed": failed,
    }


def _checkpoint(
    artifact: dict[str, Any],
    output: pathlib.Path,
    budget: BudgetState,
    *,
    student_id: int,
    student_name: str,
    artifact_hmac_secret: str,
    finalized: bool = False,
) -> None:
    run = artifact["run"]
    run["updated_at"] = _iso_now()
    run["usage"] = budget.as_dict()
    run["stopped_for"] = budget.stopped_for
    rows = artifact.get("rows") or []
    scored_rows = [
        row
        for row in rows
        if isinstance(row.get("plan"), Mapping)
        and (row.get("plan") or {}).get("passed") is not None
    ]
    plan_passed = sum((row.get("plan") or {}).get("passed") is True for row in scored_rows)
    strict_plan_passed = sum(
        (row.get("plan") or {}).get("strict_canonical_passed") is True for row in scored_rows
    )
    validation_outcomes = collections.Counter(
        str((row.get("validation") or {}).get("outcome") or "unavailable") for row in rows
    )
    summary: dict[str, Any] = {
        "selected": len((artifact.get("corpus") or {}).get("selected_case_ids") or []),
        "recorded": len(rows),
        "completed": sum(row.get("status") == "completed" for row in rows),
        "errors": sum(row.get("status") != "completed" for row in rows),
        "plan_contract": {
            "passed": plan_passed,
            "total": len(scored_rows),
            "rate": round(plan_passed / len(scored_rows), 6) if scored_rows else None,
        },
        "strict_canonical_plan_contract": {
            "passed": strict_plan_passed,
            "total": len(scored_rows),
            "rate": (round(strict_plan_passed / len(scored_rows), 6) if scored_rows else None),
        },
        "execution_incomplete": sum(
            not bool((row.get("execution") or {}).get("complete")) for row in rows
        ),
        "provider_errors": sum(bool(row.get("provider_error")) for row in rows),
        "planner_contract_errors": sum(bool(row.get("planner_contract_error")) for row in rows),
        "validation_outcomes": dict(sorted(validation_outcomes.items())),
        "categories": category_aggregates(rows),
    }
    summary["readiness"] = _readiness_gate(artifact, summary)
    artifact["summary"] = summary
    _seal_artifact(
        artifact,
        secret=artifact_hmac_secret,
        finalized=finalized,
    )
    _assert_safe_artifact(artifact, student_id=student_id, student_name=student_name)
    _atomic_write(output, artifact)


def _validate_only_report(corpus: CorpusData) -> dict[str, Any]:
    categories = collections.Counter(case.category for case in corpus.cases)
    support = collections.Counter(
        case.contract.support_level or "unlabelled" for case in corpus.cases
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "validate_only",
        "provider_calls": 0,
        "corpus": {
            "id": corpus.corpus_id,
            "sha256": corpus.sha256,
            "execution_sha256": corpus.execution_sha256,
            "cases": len(corpus.cases),
            "categories": dict(sorted(categories.items())),
            "support_levels": dict(sorted(support.items())),
            "placeholder_free": True,
        },
    }


def _write_or_print(report: Mapping[str, Any], output: pathlib.Path | None) -> None:
    if output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _atomic_write(output, report)
        print(f"wrote {output}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=pathlib.Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live-external-request", action="store_true")
    parser.add_argument("--student-id", type=int)
    parser.add_argument("--model", default="")
    parser.add_argument("--academic-year", type=int)
    parser.add_argument("--term", type=int)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--max-provider-calls", type=int, default=0)
    parser.add_argument("--max-total-tokens", type=int, default=0)
    parser.add_argument("--max-tokens-per-call", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("--max-wall-seconds", type=float, default=0.0)
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    try:
        corpus = load_corpus(args.corpus)
    except CorpusError as exc:
        parser.error(str(exc))
    if args.output is not None:
        try:
            if args.output.resolve() == args.corpus.resolve():
                parser.error("--output must not overwrite the source corpus")
        except OSError:
            parser.error("corpus/output path could not be resolved")

    if not args.live:
        forbidden = [
            args.confirm_live_external_request,
            args.student_id is not None,
            bool(args.model),
            args.resume,
            bool(args.case_ids),
            any(
                value
                for value in (
                    args.max_cases,
                    args.max_provider_calls,
                    args.max_total_tokens,
                    args.max_tokens_per_call,
                    args.timeout_seconds,
                    args.max_wall_seconds,
                )
            ),
        ]
        if any(forbidden):
            parser.error("live-only arguments require --live")
        _write_or_print(_validate_only_report(corpus), args.output)
        return 0

    # Every safety check precedes Django/provider setup.
    if not args.confirm_live_external_request:
        parser.error("--live requires --confirm-live-external-request")
    if args.output is None:
        parser.error("--live requires --output for durable checkpoints")
    if args.student_id is None or args.student_id <= 0:
        parser.error("--live requires a positive --student-id")
    if not _SAFE_TOKEN.fullmatch(str(args.model or "").strip()):
        parser.error("--live requires an explicit safe --model")
    try:
        fingerprint_secret = _live_fingerprint_secret()
    except ValueError as exc:
        parser.error(str(exc))
    if (args.academic_year is None) != (args.term is None):
        parser.error("--academic-year and --term must be supplied together")
    limits = LiveLimits(
        max_cases=args.max_cases,
        max_provider_calls=args.max_provider_calls,
        max_total_tokens=args.max_total_tokens,
        max_tokens_per_call=args.max_tokens_per_call,
        timeout_seconds=args.timeout_seconds,
        max_wall_seconds=args.max_wall_seconds,
    )
    try:
        limits.validate(case_count=len(corpus.cases))
    except ValueError as exc:
        parser.error(str(exc))

    selected_pool = list(corpus.cases)
    if args.case_ids.strip():
        requested = list(
            dict.fromkeys(value.strip() for value in args.case_ids.split(",") if value.strip())
        )
        by_id = {case.case_id: case for case in corpus.cases}
        unknown = [case_id for case_id in requested if case_id not in by_id]
        if unknown:
            parser.error("--case-ids contains unknown ids")
        selected_pool = [by_id[case_id] for case_id in requested]
        if len(selected_pool) > limits.max_cases:
            parser.error("--case-ids exceeds --max-cases")
    selected = selected_pool[: limits.max_cases]

    deps = _runtime_dependencies()
    settings = deps["settings"]
    student = deps["Student"].objects.filter(student_id=args.student_id).values("name").first()
    if student is None:
        parser.error("the specified local student does not exist")
    student_name = str(student.get("name") or "").strip()
    student_ref = _student_ref(fingerprint_secret, args.student_id)

    inner = deps["get_llm_client"]()
    backend = str(getattr(inner, "backend", "") or "").strip().lower()
    if backend != "local" and not bool(getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False)):
        parser.error("the configured external-provider egress kill switch is closed")
    # One wrapper reservation equals one HTTP attempt.  This changes only the fresh
    # eval client, never process-global production settings.
    inner.config = dataclasses.replace(
        inner.config,
        max_retries=0,
        max_tokens=min(int(inner.config.max_tokens), limits.max_tokens_per_call),
        timeout_seconds=min(float(inner.config.timeout_seconds), limits.timeout_seconds),
    )
    model = str(args.model).strip()
    region = str(getattr(inner.config, "region", "") or "")
    model_thinking_enabled = bool(getattr(inner.config, "enable_thinking", False))
    provider_config_hmac_sha256 = _provider_config_hmac_sha256(
        secret=fingerprint_secret,
        config=inner.config,
        requested_model=model,
    )
    adviser_runtime_config_hmac_sha256 = _adviser_runtime_config_hmac_sha256(
        secret=fingerprint_secret,
        settings=settings,
        limits=limits,
    )
    database_vendor = str(deps.get("database_vendor") or "").strip().lower()
    if not _SAFE_TOKEN.fullmatch(database_vendor):
        parser.error("the configured database vendor is unavailable or unsafe")
    runtime_environment = _runtime_environment_identity()
    prompt_version = str(deps.get("prompt_version") or "unknown")
    try:
        runtime_source_provenance = _runtime_source_provenance()
        runtime_source_sha256 = str(runtime_source_provenance["sha256"])
        runtime_source_manifest = list(runtime_source_provenance["files"])
    except ValueError as exc:
        parser.error(str(exc))
    git_worktree_identity = _git_worktree_identity()

    def current_fixture_state_hmac_sha256() -> str:
        return str(
            deps["fixture_state_fingerprint"](
                secret=fingerprint_secret,
                student_id=args.student_id,
                academic_year=args.academic_year,
                term=args.term,
            )
        )

    try:
        fixture_state_hmac_sha256 = current_fixture_state_hmac_sha256()
    except Exception as exc:  # noqa: BLE001 - persist no database error detail
        parser.error(f"fixture-state fingerprint failed: {_error_category(exc)}")

    if args.resume:
        if not args.output.exists():
            parser.error("--resume requires an existing output artifact")
        try:
            artifact = _load_resume(
                args.output,
                corpus=corpus,
                selected=selected,
                student_ref=student_ref,
                backend=backend,
                region=region,
                model=model,
                model_thinking_enabled=model_thinking_enabled,
                provider_config_hmac_sha256=provider_config_hmac_sha256,
                adviser_runtime_config_hmac_sha256=(adviser_runtime_config_hmac_sha256),
                database_vendor=database_vendor,
                runtime_environment=runtime_environment,
                prompt_version=prompt_version,
                runtime_source_sha256=runtime_source_sha256,
                runtime_source_manifest=runtime_source_manifest,
                fixture_state_hmac_sha256=fixture_state_hmac_sha256,
                limits=limits,
                academic_year=args.academic_year,
                term=args.term,
                student_id=args.student_id,
                student_name=student_name,
                artifact_hmac_secret=fingerprint_secret,
            )
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if args.output.exists():
            parser.error("output already exists; use --resume or choose a new path")
        artifact = _new_artifact(
            corpus,
            selected,
            student_ref=student_ref,
            backend=backend,
            region=region,
            model=model,
            model_thinking_enabled=model_thinking_enabled,
            provider_config_hmac_sha256=provider_config_hmac_sha256,
            adviser_runtime_config_hmac_sha256=adviser_runtime_config_hmac_sha256,
            database_vendor=database_vendor,
            runtime_environment=runtime_environment,
            prompt_version=prompt_version,
            runtime_source_sha256=runtime_source_sha256,
            runtime_source_manifest=runtime_source_manifest,
            fixture_state_hmac_sha256=fixture_state_hmac_sha256,
            limits=limits,
            academic_year=args.academic_year,
            term=args.term,
            git_worktree_identity=git_worktree_identity,
            artifact_hmac_secret=fingerprint_secret,
        )

    prior_usage = (artifact.get("run") or {}).get("usage") or {}
    try:
        prior_wall_seconds = float(prior_usage.get("active_wall_seconds") or 0.0)
    except (TypeError, ValueError, OverflowError):
        parser.error("resume artifact wall-time counter is invalid")
    if not 0.0 <= prior_wall_seconds <= limits.max_wall_seconds:
        parser.error("resume artifact wall-time counter is outside the run limits")
    budget = BudgetState(
        limits=limits,
        provider_calls=_non_negative_int(prior_usage.get("provider_calls")),
        provider_responses=_non_negative_int(prior_usage.get("provider_responses")),
        committed_token_ceiling=_non_negative_int(prior_usage.get("committed_token_ceiling")),
        prompt_tokens=_non_negative_int(prior_usage.get("prompt_tokens")),
        completion_tokens=_non_negative_int(prior_usage.get("completion_tokens")),
        total_tokens=_non_negative_int(prior_usage.get("total_tokens")),
        prior_wall_seconds=prior_wall_seconds,
    )

    def save(*, finalized: bool = False) -> None:
        _checkpoint(
            artifact,
            args.output,
            budget,
            student_id=args.student_id,
            student_name=student_name,
            artifact_hmac_secret=fingerprint_secret,
            finalized=finalized,
        )

    client = BudgetedLLMClient(inner, budget, checkpoint=save)
    principal = deps["AdvisorPrincipal"](role=deps["ROLE_STUDENT"], student_id=args.student_id)
    completed_ids = {
        str(row.get("case_id") or "")
        for row in artifact.get("rows") or []
        if isinstance(row, Mapping)
    }
    save()
    for number, case in enumerate(selected, 1):
        if case.case_id in completed_ids:
            continue
        if budget.provider_calls >= limits.max_provider_calls:
            budget.stopped_for = "provider-call budget reached before the next case"
            break
        if budget.wall_seconds >= limits.max_wall_seconds:
            budget.stopped_for = "active wall-time budget reached before the next case"
            break

        artifact["run"]["current_case_id"] = case.case_id
        save()
        calls_before = budget.provider_calls
        ceiling_before = budget.committed_token_ceiling
        started = time.monotonic()
        try:
            runtime_kwargs = {
                "question": case.question,
                "principal": principal,
                "academic_year": args.academic_year,
                "term": args.term,
                "history": None,
                "prior_presentation": None,
                "model": model,
                "llm_client": client,
                "channel_profile": "",
            }
            with deps["override_settings"](
                STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS=min(
                    int(getattr(settings, "STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS", 900)),
                    limits.max_tokens_per_call,
                ),
                STUDENT_ADVISOR_V2_MAX_TOKENS=min(
                    int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOKENS", 1800)),
                    limits.max_tokens_per_call,
                ),
                STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS=min(
                    float(getattr(settings, "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS", 75)),
                    limits.timeout_seconds,
                ),
            ):
                result = deps["answer"](**runtime_kwargs)
            row = result_row(
                case,
                result,
                student_id=args.student_id,
                student_name=student_name,
                latency_ms=int((time.monotonic() - started) * 1000),
                call_delta=budget.provider_calls - calls_before,
                token_ceiling_delta=budget.committed_token_ceiling - ceiling_before,
            )
            if budget.stopped_for:
                row["status"] = "budget_stopped"
                row["provider_error"] = "BudgetStop"
        except BudgetStop:
            row = error_row(
                case,
                status="budget_stopped",
                error_category="BudgetStop",
                student_id=args.student_id,
                student_name=student_name,
                latency_ms=int((time.monotonic() - started) * 1000),
                call_delta=budget.provider_calls - calls_before,
                token_ceiling_delta=budget.committed_token_ceiling - ceiling_before,
            )
        except Exception as exc:  # noqa: BLE001 - report only the bounded class name
            row = error_row(
                case,
                status="error",
                error_category=_error_category(exc),
                student_id=args.student_id,
                student_name=student_name,
                latency_ms=int((time.monotonic() - started) * 1000),
                call_delta=budget.provider_calls - calls_before,
                token_ceiling_delta=budget.committed_token_ceiling - ceiling_before,
            )
        artifact["rows"].append(row)
        artifact["run"]["current_case_id"] = ""
        save()
        print(
            f"[{number}/{len(selected)}] {case.case_id} {row['status']} "
            f"calls={budget.provider_calls}/{limits.max_provider_calls}",
            flush=True,
        )
        if row["status"] == "budget_stopped" or budget.stopped_for:
            break

    recorded = {str(row.get("case_id") or "") for row in artifact["rows"]}
    cases_complete = all(case.case_id in recorded for case in selected) and all(
        row.get("status") == "completed" for row in artifact["rows"]
    )
    try:
        fixture_state_end_hmac_sha256 = current_fixture_state_hmac_sha256()
        fixture_state_stable = fixture_state_end_hmac_sha256 == fixture_state_hmac_sha256
    except Exception:  # noqa: BLE001 - persist no database error detail
        fixture_state_end_hmac_sha256 = ""
        fixture_state_stable = False
    try:
        runtime_source_end_provenance = _runtime_source_provenance()
        runtime_source_end_sha256 = str(runtime_source_end_provenance["sha256"])
        runtime_source_end_manifest = list(runtime_source_end_provenance["files"])
        runtime_source_stable = runtime_source_end_sha256 == runtime_source_sha256
    except ValueError:
        runtime_source_end_sha256 = ""
        runtime_source_end_manifest = []
        runtime_source_stable = False
    artifact["run"]["fixture_state_end_hmac_sha256"] = fixture_state_end_hmac_sha256
    artifact["run"]["fixture_state_stable"] = fixture_state_stable
    artifact["run"]["runtime_source_end_sha256"] = runtime_source_end_sha256
    artifact["run"]["runtime_source_end_manifest"] = runtime_source_end_manifest
    artifact["run"]["runtime_source_stable"] = runtime_source_stable
    provenance_stable = fixture_state_stable and runtime_source_stable
    complete = cases_complete and provenance_stable
    if not provenance_stable:
        artifact["run"]["state"] = "state_drift"
        budget.stopped_for = "runtime source or fixture state changed during collection"
    else:
        artifact["run"]["state"] = "complete" if cases_complete else "stopped"
    if not cases_complete and provenance_stable and not budget.stopped_for:
        budget.stopped_for = "one or more cases did not complete"
    save(finalized=True)
    print(f"saved {len(artifact['rows'])}/{len(selected)} rows; state={artifact['run']['state']}")
    readiness_go = ((artifact.get("summary") or {}).get("readiness") or {}).get("status") == "GO"
    return 0 if complete and readiness_go else 1


if __name__ == "__main__":
    raise SystemExit(main())
