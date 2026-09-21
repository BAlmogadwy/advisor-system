"""Schema-constrained semantic planning for Student Advisor V2.1.

The planner is deliberately a *meta tool*.  The model is offered exactly one
function, ``submit_student_turn_plan``, and must use it to return a bounded list
of read-only evidence requests.  Nothing in this module executes a capability.

Provider-side function schemas improve generation quality, but they are not an
authorization or validation boundary.  This module therefore reparses the raw
JSON returned by the provider, rejects ambiguous JSON, validates every nested
argument against the exact advertised capability schema, and only then exposes
ordinary :class:`ToolCallRequest` objects to the adviser runtime.
"""

from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.services.llm_backend import LLMInvalidResponse, ToolCallRequest, ToolChatResult
from core.services.student_advisor_v21_policy import SEMANTIC_POLICY_IDS, SemanticPolicyId

TURN_PLAN_TOOL_NAME = "submit_student_turn_plan"
MAX_RAW_PLAN_ARGUMENT_CHARS = 64_000

# Closed server-owned telemetry vocabulary for explicit current-turn constraints.
# These are schema field paths only: they cannot carry a course, section, number,
# rejected argument, or provider-authored explanation into a repair prompt/audit.
EXPLICIT_CONSTRAINT_FIELD_PATHS: frozenset[str] = frozenset(
    {
        "clarification_kind",
        "my_progress.priority_limit",
        "build_timetable_proposal.mode",
        "build_timetable_proposal.max_credits",
        "build_timetable_proposal.target_credits",
        "build_timetable_proposal.must_take_courses",
        "build_timetable_proposal.pinned_sections",
        "recommend_feasible_course_addition.additional_credit_hours",
        "recommend_feasible_course_addition.max_credits",
        "recommend_feasible_course_addition.pinned_sections",
        "rank_current_course_drop_impact.max_credits",
        "improve_current_timetable.max_credits",
    }
)


class TurnPlanDecision(str, Enum):
    """The four valid control decisions of semantic planning."""

    EXECUTE = "execute"
    CLARIFY = "clarify"
    DIRECT = "direct"
    UNSUPPORTED = "unsupported"


class ClarificationKind(str, Enum):
    """Closed server-rendered reason for a clarification decision."""

    NONE = "none"
    TIMETABLE_LOAD = "timetable_load"
    TIMETABLE_PREFERENCE = "timetable_preference"
    COURSE_OR_SECTION_IDENTITY = "course_or_section_identity"
    TERM_OR_CHOICE = "term_or_choice"
    GENERIC = "generic"


class StudentRequestOutcome(str, Enum):
    """A closed vocabulary for deliverables requested by the student.

    These values describe *what the student asked the adviser to deliver*, not
    which facts a capability happens to return.  A single turn may request
    several outcomes.  Keeping that distinction explicit lets the runtime
    verify semantic coverage independently from tool-argument validation.
    """

    COURSE_CATALOGUE = "course_catalogue"
    COURSE_ELIGIBILITY = "course_eligibility"
    PREREQUISITE_INFORMATION = "prerequisite_information"
    AVAILABLE_COURSES = "available_courses"
    COURSE_PRIORITY = "course_priority"
    COURSE_RECOMMENDATION = "course_recommendation"
    COURSE_ADDITION = "course_addition"
    COURSE_DROP_IMPACT = "course_drop_impact"
    DEGREE_PROGRESS = "degree_progress"
    DEGREE_PLAN = "degree_plan"
    CURRENT_TIMETABLE = "current_timetable"
    TIMETABLE_REVIEW = "timetable_review"
    TIMETABLE_BUILD = "timetable_build"
    TIMETABLE_FEASIBILITY = "timetable_feasibility"
    COURSE_COMPARISON = "course_comparison"
    COURSE_REPLACEMENT = "course_replacement"
    GRADUATION_FORECAST = "graduation_forecast"
    GRADUATION_IMPACT = "graduation_impact"
    CREDIT_LOAD_COMPARISON = "credit_load_comparison"
    POLICY_RULE = "policy_rule"
    ACADEMIC_ADVISER = "academic_adviser"
    PRIOR_RESULT = "prior_result"
    REGISTRATION_ACTION = "registration_action"
    GENERAL_CONVERSATION = "general_conversation"
    UNSUPPORTED_REQUEST = "unsupported_request"


UNSUPPORTED_REQUEST_OUTCOMES: frozenset[StudentRequestOutcome] = frozenset(
    {
        StudentRequestOutcome.REGISTRATION_ACTION,
        StudentRequestOutcome.CREDIT_LOAD_COMPARISON,
        StudentRequestOutcome.UNSUPPORTED_REQUEST,
    }
)

# These deliverables have no evidence capability of their own, but the server
# can render their read-only limitation alongside a separately supported
# analysis.  A plan containing only these outcomes must still be UNSUPPORTED;
# EXECUTE is valid only when at least one evidence-backed outcome is also
# present.
SERVER_OWNED_EXECUTE_OUTCOMES: frozenset[StudentRequestOutcome] = frozenset(
    {
        StudentRequestOutcome.REGISTRATION_ACTION,
        StudentRequestOutcome.CREDIT_LOAD_COMPARISON,
    }
)

EVIDENCE_BACKED_REQUEST_OUTCOMES: frozenset[StudentRequestOutcome] = frozenset(
    set(StudentRequestOutcome)
    - {
        StudentRequestOutcome.REGISTRATION_ACTION,
        StudentRequestOutcome.CREDIT_LOAD_COMPARISON,
        StudentRequestOutcome.GENERAL_CONVERSATION,
        StudentRequestOutcome.UNSUPPORTED_REQUEST,
    }
)

_REQUEST_OUTCOME_DESCRIPTIONS: dict[StudentRequestOutcome, str] = {
    StudentRequestOutcome.COURSE_CATALOGUE: "course identity or catalogue details",
    StudentRequestOutcome.COURSE_ELIGIBILITY: (
        "the overall reason a student cannot take a named course (for example ليه ما أقدر "
        "أنزل DS491? / why can't I take DS491?); do not also list prerequisite_information "
        "merely because the eligibility evidence includes missing prerequisites, but explicitly "
        "asking 'can I take X or am I still missing a prerequisite?' requires both outcomes"
    ),
    StudentRequestOutcome.PREREQUISITE_INFORMATION: (
        "the prerequisite, missing requirement, or unlock chain itself (for example وش ناقصني "
        "عشان أقدر أسجل DS491? / what am I missing before DS491?); personalized missing-state "
        "questions require why_course_locked, while course_prerequisites is catalogue-only; do "
        "not also list course_eligibility unless the student separately asks whether they are "
        "eligible. An exact-code 'requirements/prerequisite of X' catalogue question uses "
        "course_prerequisites directly, never lookup_course. course_prerequisites does not "
        "expose corequisites; a standalone corequisite question is unsupported_request with "
        "decision=unsupported and no evidence calls"
    ),
    StudentRequestOutcome.AVAILABLE_COURSES: (
        "the prerequisite-ready courses currently open to the student; 'best available courses' "
        "or 'important courses I can register but have not taken' asks for this list plus "
        "course_priority, while choosing one course to add is course_addition. A plain request "
        "for eligible courses absent from the current timetable, with no positive best/important/"
        "priority criterion, is available_courses only via my_progress"
    ),
    StudentRequestOutcome.COURSE_PRIORITY: (
        "ranking remaining, open, or current courses by verified prerequisite-chain academic "
        "importance; 'best available courses' also asks for available_courses, priority is not "
        "itself graduation impact, choosing one feasible addition is course_addition, and an "
        "explicit priority criterion in a fresh/from-scratch build remains a separate "
        "course_priority deliverable alongside timetable_build"
    ),
    StudentRequestOutcome.COURSE_RECOMMENDATION: (
        "the system's next-term course recommendations without a timetable-fit decision"
    ),
    StudentRequestOutcome.COURSE_ADDITION: (
        "selecting one feasible course to add against the current timetable and the student's "
        "stated fit, unlock, or graduation criterion; the criterion is owned by this outcome, "
        "not duplicated as available_courses, course_priority, or graduation_impact unless a "
        "separate deliverable is explicitly requested. A generic one-course choice with no "
        "criterion uses objective=balanced. Best course(s) to add alongside an exact pin is "
        "still this balanced addition compound with pinned_sections, not a best-timetable "
        "clarification"
    ),
    StudentRequestOutcome.COURSE_DROP_IMPACT: (
        "the overall impact or ranking of one or more recorded courses to drop, including a "
        "generic 'what happens if I withdraw?' or a prerequisite-continuity criterion; the "
        "drop compound owns that criterion without incidental prerequisite outcomes. An explicit "
        "'will dropping DS332 delay graduation?' question is graduation_impact alone, not "
        "course_drop_impact; choosing the least-delay drop among several named current courses "
        "remains course_drop_impact, not the singleton graduation_impact exception"
    ),
    StudentRequestOutcome.DEGREE_PROGRESS: "completed and remaining degree progress",
    StudentRequestOutcome.DEGREE_PLAN: "the student's degree-plan sequence by term",
    StudentRequestOutcome.CURRENT_TIMETABLE: "the student's recorded current timetable",
    StudentRequestOutcome.TIMETABLE_REVIEW: (
        "evaluating or improving the recorded current timetable against the student's stated "
        "graduation, academic-priority, load, or section-quality criterion"
    ),
    StudentRequestOutcome.TIMETABLE_BUILD: (
        "constructing clash-checked timetable alternatives; 'build a full timetable around "
        "DS341-M2 without conflicts' is only timetable_build because clash checking is part of "
        "the build, but a fresh/from-scratch build that explicitly asks to prioritize courses "
        "that prevent graduation delay also requires the separate course_priority deliverable. "
        "An explicit from-scratch build is executable without a supplied course/load list, and "
        "a generic build with an explicit maximum is executable in from_scratch mode without "
        "another clarification. A 'light' build without an exact or maximum credit-hour bound "
        "must clarify with "
        "clarification_kind=timetable_load; a request to name one option as 'best' must clarify "
        "with clarification_kind=timetable_preference because the builder returns neutral "
        "alternatives, not a certified ranking. Ask only for supported constraints such as "
        "exact/maximum credits and required or pinned courses/sections. A new/full/build-"
        "the-rest timetable around one or more explicit hard pins uses from_scratch so every "
        "non-pinned section may vary. around_current requires explicit whole-current/baseline "
        "retention or add-around wording. timetable_preference applies when best modifies the "
        "timetable or neutral alternatives, not when best modifies course(s) to add"
    ),
    StudentRequestOutcome.TIMETABLE_FEASIBILITY: (
        "a standalone fit or conflict decision when no timetable construction is requested"
    ),
    StudentRequestOutcome.COURSE_COMPARISON: "comparing specified courses",
    StudentRequestOutcome.COURSE_REPLACEMENT: "evaluating a feasible course replacement",
    StudentRequestOutcome.GRADUATION_FORECAST: "forecasting remaining terms or completion",
    StudentRequestOutcome.GRADUATION_IMPACT: (
        "graduation effect of a concrete add, remove, replacement, non-passage, or timetable "
        "change scenario; do not add prerequisite_information unless dependency effects are "
        "separately requested, and let an owning add/drop compound provide this criterion. "
        "'Will dropping DS332 delay graduation?' is graduation_impact alone via the drop "
        "compound, without course_drop_impact"
    ),
    StudentRequestOutcome.CREDIT_LOAD_COMPARISON: (
        "comparing alternative credit loads or finding a minimum load that preserves "
        "graduation timing; this analysis is not currently supported"
    ),
    StudentRequestOutcome.POLICY_RULE: "an applicable academic rule or policy",
    StudentRequestOutcome.ACADEMIC_ADVISER: "the student's assigned adviser information",
    StudentRequestOutcome.PRIOR_RESULT: "re-presenting a prior verified adviser result",
    StudentRequestOutcome.REGISTRATION_ACTION: "performing a registration-system mutation",
    StudentRequestOutcome.GENERAL_CONVERSATION: "a greeting or harmless non-academic exchange",
    StudentRequestOutcome.UNSUPPORTED_REQUEST: (
        "a requested deliverable outside all capabilities, including a standalone corequisite "
        "question because the prerequisite catalogue exposes no corequisite relationship"
    ),
}


class TurnPlanValidationError(LLMInvalidResponse):
    """The provider returned a turn-plan envelope that failed validation."""

    def __init__(
        self,
        message: str,
        *,
        provider_turns: Sequence[ToolChatResult] = (),
    ) -> None:
        super().__init__(message)
        # Provider turns are retained only in memory so the runtime can account
        # usage after a final rejected plan.  They are never copied into repair
        # prompts, user responses, or durable audit metadata.
        self.provider_turns = tuple(provider_turns)

    def retain_provider_turns(
        self,
        provider_turns: Sequence[ToolChatResult],
    ) -> None:
        """Attach bounded provider metadata before this error leaves planning."""

        self.provider_turns = tuple(provider_turns)


class TurnPlanProvenanceError(TurnPlanValidationError):
    """A schema-valid capability argument was not grounded in a trusted source."""


class TurnPlanSchemaError(ValueError):
    """The application supplied an invalid advertised capability contract."""


@dataclass(frozen=True)
class PlannedCapabilityCall:
    """One validated, read-only evidence request selected by the model."""

    capability: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class StudentTurnPlan:
    """A validated semantic plan, independent of provider response metadata."""

    decision: TurnPlanDecision
    evidence_requests: tuple[PlannedCapabilityCall, ...]
    clarification_kind: ClarificationKind = ClarificationKind.NONE
    clarification_question: str = ""
    requested_outcomes: tuple[StudentRequestOutcome, ...] = ()

    @property
    def requires_evidence(self) -> bool:
        return self.decision is TurnPlanDecision.EXECUTE


@dataclass(frozen=True)
class TurnPlanningResult:
    """The validated plan plus the provider turn used for usage/audit accounting."""

    plan: StudentTurnPlan
    provider_turn: ToolChatResult
    provider_turns: tuple[ToolChatResult, ...] = ()


class ArgumentProvenanceMode(str, Enum):
    """Supported server-side evidence rules for one scalar argument path."""

    EXACT = "exact"
    STRUCTURED_EXACT = "structured_exact"
    IDENTIFIER = "identifier"
    TEXT_SPAN = "text_span"
    SEMANTIC_CHOICE = "semantic_choice"


@dataclass(frozen=True)
class ArgumentProvenanceRule:
    """One allow rule for scalar values at a capability argument path.

    Paths begin with the concrete capability name.  ``*`` may be used after
    that name and matches one array index, for example
    ``("course_choice_comparison", "course_codes", "*")``.

    ``SEMANTIC_CHOICE`` is an explicit escape hatch for control values that the
    model is meant to choose (such as an enum describing an objective).  It
    must never be used for student-supplied entities or numerical constraints.
    """

    path: tuple[str, ...]
    mode: ArgumentProvenanceMode
    allowed_values: tuple[Any, ...] = ()
    source_texts: tuple[str, ...] = ()

    @classmethod
    def exact(cls, path: Sequence[str], *allowed_values: Any) -> ArgumentProvenanceRule:
        """Allow JSON scalar values that exactly equal a trusted value."""

        return cls(
            path=tuple(path),
            mode=ArgumentProvenanceMode.EXACT,
            allowed_values=tuple(copy.deepcopy(allowed_values)),
        )

    @classmethod
    def structured_exact(
        cls,
        path: Sequence[str],
        *allowed_values: dict[str, Any] | list[Any],
    ) -> ArgumentProvenanceRule:
        """Allow an exact trusted object/list, preserving relationships within it."""

        return cls(
            path=tuple(path),
            mode=ArgumentProvenanceMode.STRUCTURED_EXACT,
            allowed_values=tuple(copy.deepcopy(allowed_values)),
        )

    @classmethod
    def identifier(
        cls,
        path: Sequence[str],
        *allowed_values: str,
    ) -> ArgumentProvenanceRule:
        """Allow trusted identifiers despite harmless case/spacing/hyphen differences."""

        return cls(
            path=tuple(path),
            mode=ArgumentProvenanceMode.IDENTIFIER,
            allowed_values=tuple(allowed_values),
        )

    @classmethod
    def text_span(cls, path: Sequence[str], *source_texts: str) -> ArgumentProvenanceRule:
        """Require a string value to occur literally in trusted normalized text."""

        return cls(
            path=tuple(path),
            mode=ArgumentProvenanceMode.TEXT_SPAN,
            source_texts=tuple(source_texts),
        )

    @classmethod
    def semantic_choice(cls, path: Sequence[str]) -> ArgumentProvenanceRule:
        """Explicitly allow a schema-valid value to be selected semantically."""

        return cls(path=tuple(path), mode=ArgumentProvenanceMode.SEMANTIC_CHOICE)


@dataclass(frozen=True)
class ArgumentProvenanceContract:
    """Fail-closed provenance policy for capability arguments in one turn.

    The caller is responsible for populating rules only from trusted turn
    inputs: user-authored text, verified structured artifacts, authenticated
    student context, or server configuration.  Planner/model output and the
    full course catalogue are not trusted provenance sources.
    """

    rules: tuple[ArgumentProvenanceRule, ...]

    @classmethod
    def from_rules(
        cls,
        *rules: ArgumentProvenanceRule,
    ) -> ArgumentProvenanceContract:
        return cls(rules=tuple(rules))


def _positive_max_calls(max_calls: int) -> int:
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ValueError("max_calls must be a positive integer")
    return max_calls


def _capability_contracts(
    advertised_tools: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return name -> parameter schema for the exact surface shown to the model."""

    contracts: dict[str, dict[str, Any]] = {}
    for schema in advertised_tools:
        if not isinstance(schema, Mapping):
            raise TurnPlanSchemaError("Every advertised tool schema must be an object.")
        if schema.get("type") != "function":
            raise TurnPlanSchemaError("Every advertised tool must have type=function.")
        function = schema.get("function")
        if not isinstance(function, Mapping):
            raise TurnPlanSchemaError("Every advertised tool must contain a function object.")
        name = str(function.get("name") or "").strip()
        if not name:
            raise TurnPlanSchemaError("Every advertised function must have a non-empty name.")
        if name == TURN_PLAN_TOOL_NAME:
            raise TurnPlanSchemaError("The meta planning tool cannot be an evidence capability.")
        if name in contracts:
            raise TurnPlanSchemaError("Advertised capability names must be unique.")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
            raise TurnPlanSchemaError(
                f"Advertised capability {name!r} must have an object parameter schema."
            )
        contracts[name] = copy.deepcopy(dict(parameters))
    if not contracts:
        raise TurnPlanSchemaError("At least one evidence capability must be advertised.")
    return contracts


def build_turn_plan_tool_schema(
    advertised_tools: Sequence[Mapping[str, Any]],
    *,
    max_calls: int,
) -> dict[str, Any]:
    """Build the sole function schema offered during semantic planning.

    Each nested evidence request is discriminated by capability name, so the
    provider sees the selected capability's exact argument schema.  The same
    contracts are independently enforced locally by
    :func:`parse_turn_plan_result`; provider-side schema handling is never an
    authorization boundary.
    """

    bounded_max = _positive_max_calls(max_calls)
    contracts = _capability_contracts(advertised_tools)
    descriptions = {
        str((schema.get("function") or {}).get("name") or "").strip(): str(
            (schema.get("function") or {}).get("description") or ""
        ).strip()
        for schema in advertised_tools
    }
    catalogue = [
        {
            "capability": name,
            "description": descriptions.get(name, ""),
        }
        for name in contracts
    ]
    catalogue_json = json.dumps(
        catalogue,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    outcome_catalogue_json = json.dumps(
        {
            outcome.value: _REQUEST_OUTCOME_DESCRIPTIONS[outcome]
            for outcome in StudentRequestOutcome
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    unsupported_outcome_values = ", ".join(
        sorted(outcome.value for outcome in UNSUPPORTED_REQUEST_OUTCOMES)
    )
    return {
        "type": "function",
        "function": {
            "name": TURN_PLAN_TOOL_NAME,
            "description": (
                "Submit one semantic evidence plan for the latest student turn. "
                "Three exact boundaries are mandatory: personalized 'what am I missing before "
                "DS491?' means prerequisite_information via why_course_locked, never "
                "catalogue-only course_prerequisites; 'will dropping DS332 delay graduation?' "
                "means graduation_impact alone via rank_current_course_drop_impact with "
                "objective=least_graduation_delay and course_codes=[DS332], never "
                "course_drop_impact; and a fresh/from-scratch timetable build that explicitly "
                "prioritizes courses to prevent graduation delay means both timetable_build + "
                "course_priority and both build_timetable_proposal(mode=from_scratch, preserving "
                "every explicit cap/pin) + my_progress. "
                "Also mandatory: 'can I take DS491 or am I still missing a prerequisite?' means "
                "both course_eligibility + prerequisite_information via one why_course_locked "
                "call; an exact-code catalogue 'requirements/prerequisite of DS491' question "
                "means course_prerequisites, never lookup_course; an explicit from-scratch "
                "timetable request executes without asking for a candidate/load list; an explicit "
                "maximum-credit timetable request executes with that max_credits value and "
                "mode=from_scratch when there is no explicit current/retain/around wording; "
                "a request to name a best timetable clarifies with clarification_kind="
                "timetable_preference because timetable alternatives are neutral and no "
                "certified ranking is supported; a light timetable without a numeric "
                "bound clarifies with clarification_kind=timetable_load; and an ambiguous pin "
                "that lacks a unique course/section identity clarifies with "
                "clarification_kind=course_or_section_identity. "
                "The student's remaining pass-before/prerequisite chain for one exact course is "
                "prerequisite_information via why_course_locked; one priority-qualified extra "
                "course is course_addition via recommend_feasible_course_addition(objective="
                "unlock_impact); one extra course's effect on the graduation date is "
                "graduation_impact via the same compound with objective=faster_graduation; and "
                "the exact personalized fastest-graduation best-timetable question is "
                "timetable_review via improve_current_timetable(objective=faster_graduation, "
                "credit_load_policy=preserve, allow_course_replacements=true), not a generic "
                "best-timetable clarification. "
                "A standalone corequisite question is unsupported_request with decision="
                "unsupported and no evidence calls; a generic choice of one course when no "
                "fit, priority, unlock, or graduation criterion is stated is course_addition "
                "via recommend_feasible_course_addition(objective=balanced); a plain eligible-"
                "but-not-current-timetable list without a positive importance/ranking criterion "
                "is available_courses only via my_progress; and best course(s) to add alongside "
                "an exact pin remains course_addition via the balanced addition compound with "
                "that pin, never timetable_preference. "
                "'important courses I can register but have not taken' means course_priority + "
                "available_courses via my_progress; and selecting the least-delay drop among "
                "several named current courses means course_drop_impact via "
                "rank_current_course_drop_impact, not the singleton graduation_impact label. "
                "requested_outcomes MUST list every distinct deliverable the student asks "
                "for, using the closed outcome vocabulary; classify requested deliverables, "
                "not incidental facts that a capability may return. Graduation, priority, "
                "or timetable-fit wording that only defines a compound decision criterion "
                "belongs in that capability's typed objective and is not automatically an "
                "additional requested outcome. A compound add, drop, or current-timetable "
                "decision must use its owning compound capability rather than adjacent raw "
                "progress or timetable facts, and incidental fields or checks inside that "
                "compound must not become redundant outcomes. "
                "Use execute when verified academic evidence is needed, clarify only "
                "when an essential value is genuinely missing, and direct only for a "
                "general-conversation response that needs no academic fact. Use unsupported "
                "when every requested deliverable is outside the advertised capabilities, "
                "including a request that only asks to perform a registration action or "
                "compare alternative hypothetical credit loads. When a request combines "
                "supported analysis with a registration action and/or credit-load comparison, "
                "use execute for the supported analysis, include registration_action and/or "
                "credit_load_comparison in requested_outcomes, and request only the evidence "
                "needed for the supported analysis; the server renders each read-only "
                "limitation boundary. "
                "For execute, evidence_requests MUST be non-empty, clarification_kind MUST be "
                "none, and clarification_question MUST be the empty string. For clarify, "
                "evidence_requests MUST be empty, clarification_kind MUST identify the missing "
                "input using the closed enum, and clarification_question MUST contain a concise "
                "question for provider planning quality only; the server never renders that "
                "provider-authored question. For direct, evidence_requests MUST be empty, "
                "clarification_kind MUST be none, clarification_question MUST be the empty "
                "string, and the "
                "only requested outcome MUST be general_conversation. For unsupported, "
                "evidence_requests and clarification_question MUST both be empty, "
                "clarification_kind MUST be none, and "
                "requested_outcomes MUST contain only one or more of: "
                + unsupported_outcome_values
                + ". general_conversation is valid only with direct. "
                "unsupported_request is valid only with unsupported. registration_action and "
                "credit_load_comparison are valid with unsupported when the request contains "
                "only typed unsupported deliverables, or with execute when evidence-backed "
                "analysis is also requested. Never invent capability arguments. "
                "The exact request-outcome vocabulary is: "
                + outcome_catalogue_json
                + ". The exact available capability contracts are: "
                + catalogue_json
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": [decision.value for decision in TurnPlanDecision],
                    },
                    "requested_outcomes": {
                        "type": "array",
                        "description": (
                            "Every distinct student-requested deliverable, in request order; "
                            "never capability side effects or incidental evidence fields."
                        ),
                        "items": {
                            "type": "string",
                            "enum": [outcome.value for outcome in StudentRequestOutcome],
                        },
                        "minItems": 1,
                        "maxItems": len(StudentRequestOutcome),
                        "uniqueItems": True,
                    },
                    "evidence_requests": {
                        "type": "array",
                        "maxItems": bounded_max,
                        "items": {
                            "type": "object",
                            "properties": {
                                "capability": {
                                    "type": "string",
                                    "enum": list(contracts),
                                },
                                "arguments": {
                                    "type": "object",
                                    "description": (
                                        "Arguments matching the selected capability's exact "
                                        "schema in the matching oneOf branch."
                                    ),
                                },
                            },
                            "required": ["capability", "arguments"],
                            "additionalProperties": False,
                            "oneOf": [
                                {
                                    "properties": {
                                        "capability": {"type": "string", "enum": [name]},
                                        "arguments": copy.deepcopy(parameters),
                                    },
                                    "required": ["capability", "arguments"],
                                }
                                for name, parameters in contracts.items()
                            ],
                        },
                    },
                    "clarification_question": {
                        "type": "string",
                        "description": (
                            "A concise question only when decision is clarify; otherwise this "
                            "MUST be the empty string."
                        ),
                    },
                    "clarification_kind": {
                        "type": "string",
                        "enum": [kind.value for kind in ClarificationKind],
                        "description": (
                            "Closed server-rendered clarification reason. Use none for every "
                            "non-clarify decision. A clarify decision MUST use a non-none value: "
                            "timetable_load for a missing exact or maximum credit-hour bound; "
                            "timetable_preference when a requested best timetable cannot be "
                            "certified from the supported build constraints; "
                            "course_or_section_identity for an ambiguous course or section; "
                            "term_or_choice for a missing term or choice; otherwise generic."
                        ),
                    },
                },
                "required": [
                    "decision",
                    "requested_outcomes",
                    "evidence_requests",
                    "clarification_kind",
                    "clarification_question",
                ],
                "additionalProperties": False,
            },
        },
    }


def _provider_compatible_turn_plan_tool_schema(
    strict_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the strict plan schema onto the provider's accepted subset.

    Alibaba's OpenAI-compatible chat endpoint rejects ``uniqueItems`` with
    ``invalid_parameter_error`` even though it is valid JSON Schema. The
    canonical schema keeps that constraint, and :func:`parse_turn_plan_result`
    validates every provider result against it locally. Only the copy sent to
    the provider omits the unsupported hint.
    """

    projected = copy.deepcopy(dict(strict_schema))
    requested_outcomes = projected["function"]["parameters"]["properties"]["requested_outcomes"]
    if requested_outcomes.pop("uniqueItems", None) is not True:
        raise AssertionError("strict turn-plan schema lost its unique-outcome constraint")
    return projected


_DROP_REPAIR_POLICIES: dict[SemanticPolicyId, tuple[str, str]] = {
    SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY: (
        "graduation_impact",
        "least_graduation_delay",
    ),
    SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT: ("course_drop_impact", "balanced"),
    SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY: (
        "course_drop_impact",
        "prerequisite_continuity",
    ),
    SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION: (
        "course_drop_impact",
        "least_graduation_delay",
    ),
}

_SINGLE_CALL_REPAIR_POLICIES: dict[
    SemanticPolicyId,
    tuple[str, str, tuple[tuple[str, Any], ...]],
] = {
    SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY: (
        "available_courses",
        "my_progress",
        (),
    ),
    SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION: (
        "course_addition",
        "recommend_feasible_course_addition",
        (("objective", "timetable_fit"),),
    ),
    SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP: (
        "course_replacement",
        "graduation_progress",
        (
            ("planning_baseline_kind", "recommended_current_term"),
            ("search_better_replacements", True),
        ),
    ),
}


def _repair_focused_provider_schema(
    provider_schema: Mapping[str, Any],
    policy_ids: Sequence[SemanticPolicyId],
) -> dict[str, Any]:
    """Narrow only the provider hint for one validated repair policy."""

    focused = copy.deepcopy(dict(provider_schema))
    policies = tuple(dict.fromkeys(policy_ids))
    if len(policies) != 1:
        return focused
    policy = policies[0]
    parameters = focused["function"]["parameters"]
    properties = parameters["properties"]
    requests = properties["evidence_requests"]
    item_schema = requests["items"]

    if policy in _SINGLE_CALL_REPAIR_POLICIES:
        outcome, capability, argument_items = _SINGLE_CALL_REPAIR_POLICIES[policy]
        expected_arguments = dict(argument_items)
        focused["function"]["description"] = (
            f"Repair only the validated closed policy {policy.value}. "
            f"{_SEMANTIC_POLICY_REPAIR_MAPPINGS[policy]}."
        )
        properties["decision"]["enum"] = ["execute"]
        properties["requested_outcomes"]["items"]["enum"] = [outcome]
        properties["requested_outcomes"]["minItems"] = 1
        properties["requested_outcomes"]["maxItems"] = 1
        requests["minItems"] = requests["maxItems"] = 1
        branches = [
            branch
            for branch in item_schema["oneOf"]
            if branch["properties"]["capability"]["enum"] == [capability]
        ]
        if len(branches) != 1:
            raise AssertionError("single-call repair capability branch is missing")
        branch = branches[0]
        arguments = branch["properties"]["arguments"]
        if not set(expected_arguments) <= set(arguments["properties"]):
            raise AssertionError("single-call repair argument schema is missing")
        arguments["properties"] = {key: arguments["properties"][key] for key in expected_arguments}
        arguments["required"] = list(expected_arguments)
        arguments["additionalProperties"] = False
        for key, value in expected_arguments.items():
            arguments["properties"][key]["enum"] = [value]
            arguments["properties"][key]["description"] = (
                f"REQUIRED exact value: {json.dumps(value, ensure_ascii=False)}."
            )
        item_schema["properties"]["capability"]["enum"] = [capability]
        item_schema["oneOf"] = branches
        properties["clarification_kind"]["enum"] = ["none"]
        properties["clarification_question"]["enum"] = [""]
        return focused

    if policy in _DROP_REPAIR_POLICIES:
        outcome, objective = _DROP_REPAIR_POLICIES[policy]
        focused["function"]["description"] = (
            f"Repair only the validated closed policy {policy.value}. "
            f"{_SEMANTIC_POLICY_REPAIR_MAPPINGS[policy]}."
        )
        properties["decision"]["enum"] = ["execute"]
        properties["requested_outcomes"]["items"]["enum"] = [outcome]
        properties["requested_outcomes"]["minItems"] = 1
        properties["requested_outcomes"]["maxItems"] = 1
        requests["minItems"] = requests["maxItems"] = 1
        branches = [
            branch
            for branch in item_schema["oneOf"]
            if branch["properties"]["capability"]["enum"] == ["rank_current_course_drop_impact"]
        ]
        if len(branches) != 1:
            raise AssertionError("drop repair capability branch is missing")
        branch = branches[0]
        arguments = branch["properties"]["arguments"]
        arguments["properties"] = {
            key: value
            for key, value in arguments["properties"].items()
            if key in {"course_codes", "objective"}
        }
        arguments["required"] = ["course_codes", "objective"]
        arguments["additionalProperties"] = False
        arguments["properties"]["objective"]["enum"] = [objective]
        arguments["properties"]["objective"]["description"] = (
            f"REQUIRED exact objective: {objective}."
        )
        course_codes = arguments["properties"]["course_codes"]
        course_codes["description"] = (
            "REQUIRED course codes copied exactly, in order, from the original question."
        )
        if policy in {
            SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
            SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY,
        }:
            course_codes["minItems"] = course_codes["maxItems"] = 1
        elif policy is SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT:
            course_codes["minItems"] = 1
            course_codes["maxItems"] = 2
        else:
            course_codes["minItems"] = course_codes["maxItems"] = 3
        item_schema["properties"]["capability"]["enum"] = ["rank_current_course_drop_impact"]
        item_schema["oneOf"] = branches
        properties["clarification_kind"]["enum"] = ["none"]
        properties["clarification_question"]["enum"] = [""]
        return focused

    if policy is SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD:
        focused["function"]["description"] = (
            "Repair only the validated closed policy "
            f"{policy.value}. {_SEMANTIC_POLICY_REPAIR_MAPPINGS[policy]}. "
            "The two evidence requests must be ordered exactly: "
            "build_timetable_proposal first, my_progress second."
        )
        properties["decision"]["enum"] = ["execute"]
        properties["requested_outcomes"]["items"]["enum"] = [
            "timetable_build",
            "course_priority",
        ]
        properties["requested_outcomes"]["minItems"] = 2
        properties["requested_outcomes"]["maxItems"] = 2
        requests["minItems"] = requests["maxItems"] = 2
        allowed = {"build_timetable_proposal", "my_progress"}
        branches = [
            branch
            for branch in item_schema["oneOf"]
            if branch["properties"]["capability"]["enum"][0] in allowed
        ]
        if len(branches) != 2:
            raise AssertionError("composite repair capability branches are missing")
        branches.sort(
            key=lambda branch: 0
            if branch["properties"]["capability"]["enum"] == ["build_timetable_proposal"]
            else 1
        )
        for branch in branches:
            capability = branch["properties"]["capability"]["enum"][0]
            arguments = branch["properties"]["arguments"]
            if capability == "build_timetable_proposal":
                required = {"mode", "max_credits", "must_take_courses", "pinned_sections"}
                arguments["properties"] = {
                    key: value for key, value in arguments["properties"].items() if key in required
                }
                arguments["required"] = [
                    "mode",
                    "max_credits",
                    "must_take_courses",
                    "pinned_sections",
                ]
                arguments["properties"]["mode"]["enum"] = ["from_scratch"]
                arguments["properties"]["mode"]["description"] = (
                    "REQUIRED exact mode: from_scratch."
                )
                arguments["properties"]["max_credits"]["description"] = (
                    "REQUIRED explicit maximum credits copied from the original question."
                )
                for key in ("must_take_courses", "pinned_sections"):
                    arguments["properties"][key]["minItems"] = 1
                    arguments["properties"][key]["maxItems"] = 1
                    arguments["properties"][key]["description"] = (
                        "REQUIRED exact singleton copied from the original question."
                    )
                arguments["additionalProperties"] = False
            else:
                arguments["properties"] = {}
                arguments["required"] = []
                arguments["additionalProperties"] = False
        item_schema["properties"]["capability"]["enum"] = [
            "build_timetable_proposal",
            "my_progress",
        ]
        item_schema["oneOf"] = branches
        requests["description"] = (
            "REQUIRED exact order: build_timetable_proposal first, my_progress second."
        )
        properties["clarification_kind"]["enum"] = ["none"]
        properties["clarification_question"]["enum"] = [""]
    return focused


class _DuplicateJSONKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise _DuplicateJSONKey("duplicate JSON object key")
        parsed[key] = value
    return parsed


def _finite_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _reject_json_constant(_token: str) -> Any:
    raise ValueError("non-standard JSON constant")


def _strict_json_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise TurnPlanValidationError("Turn plan arguments must contain a JSON object.")
    if len(raw) > MAX_RAW_PLAN_ARGUMENT_CHARS:
        raise TurnPlanValidationError("Turn plan arguments exceed the accepted size.")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as exc:
        raise TurnPlanValidationError("Turn plan arguments are not strict JSON.") from exc
    if not isinstance(parsed, dict):
        raise TurnPlanValidationError("Turn plan arguments must be a JSON object.")
    return parsed


def _schema_error(message: str) -> TurnPlanSchemaError:
    return TurnPlanSchemaError(f"Invalid capability schema: {message}")


def _value_error(path: str, message: str) -> TurnPlanValidationError:
    return TurnPlanValidationError(f"Turn plan value at {path} {message}.")


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise _schema_error(f"unsupported JSON type {expected!r}")


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality that does not treat ``true`` as the number ``1``."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


_PROVENANCE_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def normalise_provenance_text(value: str) -> str:
    """Return a stable comparison form for a trusted or planned text span."""

    if not isinstance(value, str):
        raise TypeError("provenance text must be a string")
    normalized = unicodedata.normalize("NFKC", value).translate(_PROVENANCE_DIGIT_TRANSLATION)
    return " ".join(normalized.casefold().split())


def normalise_provenance_identifier(value: str) -> str:
    """Normalize case, digits, whitespace, hyphens, and underscores in an identifier."""

    normalized = normalise_provenance_text(value)
    return re.sub(r"[\s_-]+", "", normalized)


def _provenance_schema_error(message: str) -> TurnPlanSchemaError:
    return TurnPlanSchemaError(f"Invalid argument provenance contract: {message}")


def _is_json_scalar(value: Any) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _is_finite_json_value(value: Any) -> bool:
    if _is_json_scalar(value):
        return True
    if isinstance(value, list):
        return all(_is_finite_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_finite_json_value(item) for key, item in value.items()
        )
    return False


def _validated_provenance_rules(
    contract: ArgumentProvenanceContract,
) -> tuple[ArgumentProvenanceRule, ...]:
    if not isinstance(contract, ArgumentProvenanceContract):
        raise _provenance_schema_error("contract must be ArgumentProvenanceContract")

    validated: list[ArgumentProvenanceRule] = []
    for rule in contract.rules:
        if not isinstance(rule, ArgumentProvenanceRule):
            raise _provenance_schema_error("every rule must be ArgumentProvenanceRule")
        path = tuple(rule.path)
        if not path or any(not isinstance(segment, str) or not segment for segment in path):
            raise _provenance_schema_error("every rule path must contain non-empty strings")
        if path[0] == "*":
            raise _provenance_schema_error("a rule path must name a concrete capability")

        if rule.mode is ArgumentProvenanceMode.EXACT:
            if not rule.allowed_values:
                raise _provenance_schema_error("an exact rule requires allowed values")
            if any(not _is_json_scalar(value) for value in rule.allowed_values):
                raise _provenance_schema_error("exact allowed values must be finite JSON scalars")
            if rule.source_texts:
                raise _provenance_schema_error("an exact rule cannot contain source texts")
        elif rule.mode is ArgumentProvenanceMode.STRUCTURED_EXACT:
            if not rule.allowed_values or any(
                not isinstance(value, dict | list) or not _is_finite_json_value(value)
                for value in rule.allowed_values
            ):
                raise _provenance_schema_error(
                    "a structured-exact rule requires finite JSON object/list values"
                )
            if rule.source_texts:
                raise _provenance_schema_error(
                    "a structured-exact rule cannot contain source texts"
                )
        elif rule.mode is ArgumentProvenanceMode.IDENTIFIER:
            if not rule.allowed_values or any(
                not isinstance(value, str) or not normalise_provenance_identifier(value)
                for value in rule.allowed_values
            ):
                raise _provenance_schema_error(
                    "an identifier rule requires non-empty string allowed values"
                )
            if rule.source_texts:
                raise _provenance_schema_error("an identifier rule cannot contain source texts")
        elif rule.mode is ArgumentProvenanceMode.TEXT_SPAN:
            if rule.allowed_values:
                raise _provenance_schema_error("a text-span rule cannot contain allowed values")
            if not rule.source_texts or any(
                not isinstance(source, str) for source in rule.source_texts
            ):
                raise _provenance_schema_error("a text-span rule requires string source texts")
            if not any(normalise_provenance_text(source) for source in rule.source_texts):
                raise _provenance_schema_error("a text-span rule requires non-empty source text")
        elif rule.mode is ArgumentProvenanceMode.SEMANTIC_CHOICE:
            if rule.allowed_values or rule.source_texts:
                raise _provenance_schema_error(
                    "a semantic-choice rule cannot contain provenance values"
                )
        else:
            raise _provenance_schema_error("rule mode is unsupported")
        validated.append(rule)
    return tuple(validated)


def _validate_argument_provenance_value(
    value: Any,
    *,
    path: tuple[str | int, ...],
    rules: tuple[ArgumentProvenanceRule, ...],
) -> None:
    structured = tuple(
        rule
        for rule in rules
        if rule.mode is ArgumentProvenanceMode.STRUCTURED_EXACT
        and _provenance_path_matches(tuple(rule.path), path)
    )
    if structured:
        if any(
            any(_json_equal(value, allowed) for allowed in rule.allowed_values)
            for rule in structured
        ):
            return
        raise TurnPlanProvenanceError(
            f"Turn plan value at {_provenance_path_label(path)} is not supported "
            "by trusted turn sources."
        )

    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TurnPlanProvenanceError("Capability arguments must use string object keys.")
            _validate_argument_provenance_value(nested, path=(*path, key), rules=rules)
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_argument_provenance_value(nested, path=(*path, index), rules=rules)
        return
    if not _is_json_scalar(value):
        raise TurnPlanProvenanceError("Capability arguments must contain JSON values.")

    matching = tuple(
        rule
        for rule in rules
        if rule.mode is not ArgumentProvenanceMode.STRUCTURED_EXACT
        and _provenance_path_matches(tuple(rule.path), path)
    )
    label = _provenance_path_label(path)
    if not matching:
        raise TurnPlanProvenanceError(
            f"Turn plan value at {label} has no approved provenance rule."
        )
    if not any(_rule_supports_scalar(rule, value) for rule in matching):
        raise TurnPlanProvenanceError(
            f"Turn plan value at {label} is not supported by trusted turn sources."
        )


def _provenance_path_matches(
    pattern: tuple[str, ...],
    actual: tuple[str | int, ...],
) -> bool:
    if len(pattern) != len(actual):
        return False
    return all(
        (expected == "*" and isinstance(observed, int)) or expected == observed
        for expected, observed in zip(pattern, actual, strict=True)
    )


def _provenance_path_label(path: tuple[str | int, ...]) -> str:
    capability, *segments = path
    label = f"$.{capability}.arguments"
    for segment in segments:
        label += f"[{segment}]" if isinstance(segment, int) else f".{segment}"
    return label


def _rule_supports_scalar(rule: ArgumentProvenanceRule, value: Any) -> bool:
    if rule.mode is ArgumentProvenanceMode.SEMANTIC_CHOICE:
        return True
    if rule.mode is ArgumentProvenanceMode.EXACT:
        return any(_json_equal(value, allowed) for allowed in rule.allowed_values)
    if rule.mode is ArgumentProvenanceMode.IDENTIFIER:
        if not isinstance(value, str):
            return False
        candidate = normalise_provenance_identifier(value)
        return bool(candidate) and any(
            candidate == normalise_provenance_identifier(str(allowed))
            for allowed in rule.allowed_values
        )
    if rule.mode is ArgumentProvenanceMode.TEXT_SPAN:
        if not isinstance(value, str):
            return False
        candidate = normalise_provenance_text(value)
        return bool(candidate) and any(
            candidate in normalise_provenance_text(source) for source in rule.source_texts
        )
    return False


def validate_capability_argument_provenance(
    capability: str,
    arguments: Mapping[str, Any],
    *,
    contract: ArgumentProvenanceContract,
) -> dict[str, Any]:
    """Fail closed unless every scalar argument has trusted provenance.

    This check is intentionally orthogonal to JSON-schema validation.  Callers
    must first use :func:`validate_capability_arguments`, then apply this
    function before crossing the capability execution boundary.
    """

    if not isinstance(capability, str) or not capability.strip():
        raise TurnPlanProvenanceError("Capability provenance requires a capability name.")
    if not isinstance(arguments, Mapping):
        raise TurnPlanProvenanceError("Capability provenance requires an argument object.")
    rules = _validated_provenance_rules(contract)
    copied = copy.deepcopy(dict(arguments))
    _validate_argument_provenance_value(copied, path=(capability,), rules=rules)
    return copied


def validate_plan_argument_provenance(
    plan: StudentTurnPlan,
    *,
    contract: ArgumentProvenanceContract,
) -> StudentTurnPlan:
    """Validate and defensively copy every evidence request in a turn plan."""

    if not isinstance(plan, StudentTurnPlan):
        raise TurnPlanProvenanceError("Argument provenance requires a StudentTurnPlan.")
    requests = tuple(
        PlannedCapabilityCall(
            capability=request.capability,
            arguments=validate_capability_argument_provenance(
                request.capability,
                request.arguments,
                contract=contract,
            ),
        )
        for request in plan.evidence_requests
    )
    return StudentTurnPlan(
        decision=plan.decision,
        evidence_requests=requests,
        clarification_kind=plan.clarification_kind,
        clarification_question=plan.clarification_question,
        requested_outcomes=tuple(plan.requested_outcomes),
    )


def _validate_json_schema(value: Any, schema: Any, *, path: str) -> None:
    """Validate the JSON-Schema subset used by advisor capability contracts."""

    if schema is True:
        return
    if schema is False:
        raise _value_error(path, "is forbidden by its capability schema")
    if not isinstance(schema, Mapping):
        raise _schema_error("a nested schema must be an object or boolean")

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or not one_of:
            raise _schema_error("oneOf must be a non-empty schema array")
        matched = 0
        for branch in one_of:
            try:
                _validate_json_schema(value, branch, path=path)
            except TurnPlanValidationError:
                continue
            matched += 1
        if matched != 1:
            # The planner meta-schema uses a singleton capability enum as its
            # tag. If
            # that branch exists, rerun it so bounded repair receives the exact
            # argument path instead of an opaque oneOf failure.
            if matched == 0 and isinstance(value, dict):
                capability = value.get("capability")
                tagged = [
                    branch
                    for branch in one_of
                    if isinstance(branch, Mapping)
                    and isinstance(branch.get("properties"), Mapping)
                    and isinstance(branch["properties"].get("capability"), Mapping)
                    and branch["properties"]["capability"].get("enum") == [capability]
                ]
                if len(tagged) == 1:
                    tagged_properties = tagged[0]["properties"]
                    if "arguments" in value and "arguments" in tagged_properties:
                        _validate_json_schema(
                            value["arguments"],
                            tagged_properties["arguments"],
                            path=f"$.{capability}.arguments",
                        )
                    _validate_json_schema(value, tagged[0], path=path)
            raise _value_error(path, "must match exactly one oneOf branch")

    raw_type = schema.get("type")
    expected_types: list[str] = []
    if raw_type is not None:
        if isinstance(raw_type, str):
            expected_types = [raw_type]
        elif (
            isinstance(raw_type, list)
            and raw_type
            and all(isinstance(item, str) for item in raw_type)
        ):
            expected_types = list(raw_type)
        else:
            raise _schema_error("type must be a string or non-empty string list")
        if not any(_matches_json_type(value, expected) for expected in expected_types):
            raise _value_error(path, "has the wrong JSON type")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list):
            raise _schema_error("enum must be an array")
        if not any(_json_equal(value, item) for item in enum):
            raise _value_error(path, "is not one of the allowed values")

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _value_error(path, "does not match the required constant")

    if isinstance(value, dict):
        raw_properties = schema.get("properties", {})
        if not isinstance(raw_properties, Mapping):
            raise _schema_error("properties must be an object")
        raw_required = schema.get("required", [])
        if not isinstance(raw_required, list) or not all(
            isinstance(item, str) for item in raw_required
        ):
            raise _schema_error("required must be a string array")
        missing = [name for name in raw_required if name not in value]
        if missing:
            raise _value_error(path, "is missing required properties")

        additional = schema.get("additionalProperties", True)
        extras = [name for name in value if name not in raw_properties]
        if extras and additional is False:
            raise _value_error(path, "contains unexpected properties")
        if extras and additional is not True and additional is not False:
            for name in extras:
                _validate_json_schema(value[name], additional, path=f"{path}.*")

        for name, property_schema in raw_properties.items():
            if name in value:
                _validate_json_schema(value[name], property_schema, path=f"{path}.{name}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if minimum_items is not None:
            if isinstance(minimum_items, bool) or not isinstance(minimum_items, int):
                raise _schema_error("minItems must be an integer")
            if len(value) < minimum_items:
                raise _value_error(path, "contains too few items")
        if maximum_items is not None:
            if isinstance(maximum_items, bool) or not isinstance(maximum_items, int):
                raise _schema_error("maxItems must be an integer")
            if len(value) > maximum_items:
                raise _value_error(path, "contains too many items")
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(_json_equal(item, prior) for prior in value[:index]):
                    raise _value_error(path, "contains duplicate items")
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                _validate_json_schema(item, items, path=f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if minimum_length is not None:
            if isinstance(minimum_length, bool) or not isinstance(minimum_length, int):
                raise _schema_error("minLength must be an integer")
            if len(value) < minimum_length:
                raise _value_error(path, "is shorter than allowed")
        if maximum_length is not None:
            if isinstance(maximum_length, bool) or not isinstance(maximum_length, int):
                raise _schema_error("maxLength must be an integer")
            if len(value) > maximum_length:
                raise _value_error(path, "is longer than allowed")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise _schema_error("pattern must be a string")
            try:
                matched = re.search(pattern, value) is not None
            except re.error as exc:
                raise _schema_error("pattern is not a valid regular expression") from exc
            if not matched:
                raise _value_error(path, "does not match the required format")

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None:
            if isinstance(minimum, bool) or not isinstance(minimum, int | float):
                raise _schema_error("minimum must be numeric")
            if value < minimum:
                raise _value_error(path, "is below the allowed minimum")
        if maximum is not None:
            if isinstance(maximum, bool) or not isinstance(maximum, int | float):
                raise _schema_error("maximum must be numeric")
            if value > maximum:
                raise _value_error(path, "is above the allowed maximum")


def validate_capability_arguments(
    capability: str,
    arguments: Mapping[str, Any],
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and defensively copy arguments for one advertised capability."""

    contracts = _capability_contracts(advertised_tools)
    if capability not in contracts:
        raise TurnPlanValidationError("Turn plan requested an unavailable capability.")
    if not isinstance(arguments, Mapping):
        raise TurnPlanValidationError("Turn plan capability arguments must be an object.")
    copied = copy.deepcopy(dict(arguments))
    _validate_json_schema(copied, contracts[capability], path=f"$.{capability}.arguments")
    return copied


def parse_turn_plan_result(
    result: ToolChatResult,
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
    max_calls: int,
) -> StudentTurnPlan:
    """Parse and validate one forced ``submit_student_turn_plan`` response."""

    bounded_max = _positive_max_calls(max_calls)
    tools = tuple(advertised_tools)
    contracts = _capability_contracts(tools)
    calls = tuple(result.tool_calls)
    if str(result.content or "").strip():
        raise TurnPlanValidationError("Turn planner returned prose outside the plan envelope.")
    if len(calls) != 1:
        raise TurnPlanValidationError("Turn planner must return exactly one plan function call.")
    plan_call = calls[0]
    if plan_call.name != TURN_PLAN_TOOL_NAME:
        raise TurnPlanValidationError("Turn planner returned the wrong function call.")

    raw_plan = _strict_json_object(plan_call.raw_arguments)
    plan_schema = build_turn_plan_tool_schema(tools, max_calls=bounded_max)["function"][
        "parameters"
    ]
    _validate_json_schema(raw_plan, plan_schema, path="$")

    decision = TurnPlanDecision(raw_plan["decision"])
    clarification_kind = ClarificationKind(raw_plan["clarification_kind"])
    clarification = raw_plan["clarification_question"].strip()
    requested_outcomes = tuple(
        StudentRequestOutcome(value) for value in raw_plan["requested_outcomes"]
    )
    raw_requests = raw_plan["evidence_requests"]
    planned: list[PlannedCapabilityCall] = []
    seen_requests: set[str] = set()
    for raw_request in raw_requests:
        capability = raw_request["capability"]
        arguments = copy.deepcopy(raw_request["arguments"])
        request_key = json.dumps(
            [capability, arguments],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if request_key in seen_requests:
            raise TurnPlanValidationError(
                "Turn plan must not contain an exact duplicate evidence request."
            )
        seen_requests.add(request_key)
        _validate_json_schema(
            arguments,
            contracts[capability],
            path=f"$.{capability}.arguments",
        )
        planned.append(PlannedCapabilityCall(capability=capability, arguments=arguments))

    outcome_set = frozenset(requested_outcomes)
    general_conversation = StudentRequestOutcome.GENERAL_CONVERSATION
    has_unsupported_outcome = bool(outcome_set & UNSUPPORTED_REQUEST_OUTCOMES)

    if decision is TurnPlanDecision.EXECUTE:
        if not planned:
            raise TurnPlanValidationError("An execute plan must request evidence.")
        if clarification:
            raise TurnPlanValidationError("An execute plan cannot also ask a clarification.")
        if clarification_kind is not ClarificationKind.NONE:
            raise TurnPlanValidationError(
                "A non-clarification plan must use clarification_kind=none."
            )
        execute_outcomes = EVIDENCE_BACKED_REQUEST_OUTCOMES | SERVER_OWNED_EXECUTE_OUTCOMES
        if not outcome_set <= execute_outcomes or not (
            outcome_set & EVIDENCE_BACKED_REQUEST_OUTCOMES
        ):
            raise TurnPlanValidationError(
                "An execute plan must contain an evidence-backed academic outcome and may "
                "combine it only with the server-owned registration-action and credit-load "
                "comparison boundaries."
            )
    elif decision is TurnPlanDecision.CLARIFY:
        if planned:
            raise TurnPlanValidationError("A clarification plan cannot request evidence.")
        if not clarification:
            raise TurnPlanValidationError("A clarification plan must contain a question.")
        if clarification_kind is ClarificationKind.NONE:
            raise TurnPlanValidationError(
                "A clarification plan must identify a non-none clarification kind."
            )
        if not outcome_set <= EVIDENCE_BACKED_REQUEST_OUTCOMES:
            raise TurnPlanValidationError(
                "A clarification plan must identify a supported academic outcome."
            )
    elif decision is TurnPlanDecision.DIRECT:
        if planned:
            raise TurnPlanValidationError("A direct plan cannot request evidence.")
        if clarification:
            raise TurnPlanValidationError("A direct plan cannot ask a clarification.")
        if clarification_kind is not ClarificationKind.NONE:
            raise TurnPlanValidationError(
                "A non-clarification plan must use clarification_kind=none."
            )
        if outcome_set != {general_conversation}:
            raise TurnPlanValidationError(
                "A direct plan must request only the general-conversation outcome."
            )
    else:
        if planned:
            raise TurnPlanValidationError("An unsupported plan cannot request evidence.")
        if clarification:
            raise TurnPlanValidationError("An unsupported plan cannot ask a clarification.")
        if clarification_kind is not ClarificationKind.NONE:
            raise TurnPlanValidationError(
                "A non-clarification plan must use clarification_kind=none."
            )
        if (
            general_conversation in outcome_set
            or not has_unsupported_outcome
            or not outcome_set <= UNSUPPORTED_REQUEST_OUTCOMES
        ):
            raise TurnPlanValidationError(
                "An unsupported plan may contain only unsupported request outcomes."
            )

    return StudentTurnPlan(
        decision=decision,
        evidence_requests=tuple(planned),
        clarification_kind=clarification_kind,
        clarification_question=clarification,
        requested_outcomes=requested_outcomes,
    )


_COVERAGE_REPAIR_REASONS = frozenset(
    {
        "requested_outcome_uncovered",
        "unnecessary_capability",
        "requested_entity_uncovered",
        "invalid_control_combination",
        "evidence_missing",
    }
)
_REPAIR_COURSE_CODE = re.compile(r"[A-Z]{2,8}[0-9]{2,4}[A-Z]?\Z")


def _closed_repair_details(
    repair_reason: str,
    repair_details: Mapping[str, Sequence[str]] | None,
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Validate and serialize only closed, non-sensitive coverage diagnostics."""

    if not repair_details:
        return ()
    allowed_keys_by_reason = {
        "outcome_coverage_failed": frozenset(
            {
                "coverage_reason",
                "redundant_capabilities",
                "uncovered_outcomes",
                "uncovered_course_codes",
                "policy_ids",
            }
        ),
        "constraint_coverage_failed": frozenset({"missing_field_paths", "policy_ids"}),
        "argument_provenance_failed": frozenset({"policy_ids"}),
        "plan_validation_failed": frozenset({"policy_ids"}),
        "semantic_policy_failed": frozenset({"policy_ids"}),
    }
    allowed_keys = allowed_keys_by_reason.get(repair_reason)
    if allowed_keys is None:
        raise ValueError("repair_details are supported only for closed coverage repair categories")
    unknown = set(repair_details) - allowed_keys
    if unknown:
        raise ValueError("repair_details contains an unsupported closed field")

    capabilities = frozenset(_capability_contracts(advertised_tools))
    outcomes = frozenset(item.value for item in StudentRequestOutcome)
    validators: dict[str, frozenset[str] | None] = {
        "coverage_reason": _COVERAGE_REPAIR_REASONS,
        "redundant_capabilities": capabilities,
        "uncovered_outcomes": outcomes,
        "uncovered_course_codes": None,
        "missing_field_paths": EXPLICIT_CONSTRAINT_FIELD_PATHS,
        "policy_ids": SEMANTIC_POLICY_IDS,
    }
    serialized: list[str] = []
    ordered_keys = {
        "constraint_coverage_failed": ("missing_field_paths", "policy_ids"),
        "argument_provenance_failed": ("policy_ids",),
        "plan_validation_failed": ("policy_ids",),
        "semantic_policy_failed": ("policy_ids",),
    }.get(
        repair_reason,
        (
            "coverage_reason",
            "redundant_capabilities",
            "uncovered_outcomes",
            "uncovered_course_codes",
            "policy_ids",
        ),
    )
    for key in ordered_keys:
        raw_values = repair_details.get(key, ())
        if isinstance(raw_values, str) or not isinstance(raw_values, Sequence):
            raise ValueError("repair_details values must be bounded string lists")
        values = tuple(dict.fromkeys(str(value or "").strip() for value in raw_values))
        if any(not value for value in values) or len(values) > 20:
            raise ValueError("repair_details values must be non-empty and bounded")
        if key == "coverage_reason" and len(values) > 1:
            raise ValueError("repair_details may contain only one coverage reason")
        allowed_values = validators[key]
        if allowed_values is not None and any(value not in allowed_values for value in values):
            raise ValueError("repair_details contains a value outside its closed vocabulary")
        if key == "uncovered_course_codes" and any(
            not _REPAIR_COURSE_CODE.fullmatch(value) for value in values
        ):
            raise ValueError("repair_details contains an invalid course code")
        if key == "missing_field_paths" and any(
            value != "clarification_kind" and value.split(".", 1)[0] not in capabilities
            for value in values
        ):
            raise ValueError(
                "repair_details contains a constraint path for an unavailable capability"
            )
        if values:
            serialized.append(f"{key}={','.join(values)}")
    return tuple(serialized)


_SEMANTIC_POLICY_REPAIR_MAPPINGS: dict[SemanticPolicyId, str] = {
    SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED: "decision=unsupported; requested_outcomes exactly [unsupported_request]; no evidence calls",
    SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED: "decision=execute; requested_outcomes exactly [course_addition]; exactly one recommend_feasible_course_addition call with arguments exactly {objective: balanced}",
    SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY: "decision=execute; requested_outcomes exactly [available_courses]; exactly one my_progress call with empty arguments",
    SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED: "decision=execute; requested_outcomes exactly [course_addition]; exactly one recommend_feasible_course_addition call with objective=balanced and every exact pin from the original question",
    SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS: "decision=execute; requested_outcomes exactly [prerequisite_information]; exactly one why_course_locked call with only the exact single course_code from the original question",
    SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK: "decision=execute; requested_outcomes exactly [course_addition]; exactly one recommend_feasible_course_addition call with arguments exactly {objective: unlock_impact}",
    SemanticPolicyId.FASTEST_GRADUATION_TIMETABLE_REVIEW: "decision=execute; requested_outcomes exactly [timetable_review]; exactly one improve_current_timetable call with arguments exactly {objective: faster_graduation, credit_load_policy: preserve, allow_course_replacements: true}",
    SemanticPolicyId.ONE_COURSE_GRADUATION_IMPACT: "decision=execute; requested_outcomes exactly [graduation_impact]; exactly one recommend_feasible_course_addition call with arguments exactly {objective: faster_graduation}",
    SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION: "decision=clarify; requested_outcomes exactly [timetable_build]; clarification_kind=timetable_preference; one concise nonempty clarification_question; no evidence calls",
    SemanticPolicyId.MOST_DELAYING_COURSE_PRIORITY: "for the most-delaying-course request, decision=execute; requested_outcomes exactly [course_priority]; exactly one my_progress call with empty arguments",
    SemanticPolicyId.REGISTRATION_SHORTFALL_COURSE_PRIORITY: "for the registration-shortfall request, decision=execute; requested_outcomes exactly [course_priority]; exactly one my_progress call with empty arguments",
    SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY: "decision=execute; requested_outcomes exactly [graduation_impact]; exactly one rank_current_course_drop_impact call whose arguments contain only objective=least_graduation_delay and the exact singleton course_codes from the original question; prohibit max_credits and every other argument",
    SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT: "decision=execute; requested_outcomes exactly [course_drop_impact]; exactly one rank_current_course_drop_impact call whose arguments contain only objective=balanced and exact ordered course_codes from the original question; prohibit max_credits and every other argument",
    SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY: "decision=execute; requested_outcomes exactly [course_drop_impact]; exactly one rank_current_course_drop_impact call whose arguments contain only objective=prerequisite_continuity and the exact singleton course_codes from the original question; prohibit max_credits and every other argument",
    SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION: "decision=execute; requested_outcomes exactly [course_drop_impact]; exactly one rank_current_course_drop_impact call whose arguments contain only objective=least_graduation_delay and exact ordered course_codes from the original question; prohibit max_credits and every other argument",
    SemanticPolicyId.PINNED_SECTION_EVERY_OPTION_BUILD: "decision=execute; requested_outcomes exactly [timetable_build]; exactly one build_timetable_proposal call whose arguments contain only mode=from_scratch, exact singleton must_take_courses, and exact singleton pinned_sections from the original question",
    SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD: "decision=execute; requested_outcomes exactly [timetable_build, course_priority]; exactly two calls in order: build_timetable_proposal then my_progress; build arguments contain only mode=from_scratch, explicit max_credits, exact singleton must_take_courses, and exact singleton pinned_sections from the original question; prohibit target_credits, course_codes, and every other argument; my_progress arguments are empty",
    SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION: "decision=execute; requested_outcomes exactly [course_addition]; exactly one recommend_feasible_course_addition call with arguments exactly {objective: timetable_fit}",
    SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP: "decision=execute; requested_outcomes exactly [course_replacement]; exactly one graduation_progress call with arguments exactly {planning_baseline_kind: recommended_current_term, search_better_replacements: true}",
}
if set(_SEMANTIC_POLICY_REPAIR_MAPPINGS) != set(SemanticPolicyId):
    raise RuntimeError("semantic policy repair mappings must cover the closed enum exactly")


_PLAN_REPAIR_INSTRUCTIONS = {
    "": "",
    "plan_validation_failed": (
        "The server rejected the previous function call because it did not match the exact "
        "turn-plan schema. Regenerate from the original student question, use only properties "
        "from the selected capability's exact oneOf branch, and omit inferred values."
    ),
    "argument_provenance_failed": (
        "The server rejected the previous plan because at least one capability argument was "
        "not grounded in the student's trusted turn sources. Regenerate from the original "
        "student question, preserve only explicit student constraints, and omit every "
        "ungrounded argument."
    ),
    "outcome_coverage_failed": (
        "The server rejected the previous plan because its capability set did not exactly and "
        "minimally cover the typed requested outcomes. Regenerate from the original student "
        "question, cover every requested deliverable, and omit unrelated or redundant "
        "capabilities."
    ),
    "constraint_coverage_failed": (
        "The server rejected the previous plan because a selected capability omitted an "
        "explicit constraint from the current student turn. Regenerate from the original "
        "student question and copy every explicit constraint into its owning selected "
        "capability field exactly. Do not invent a constraint and do not select an extra "
        "capability merely because a field path is listed."
    ),
    "semantic_policy_failed": (
        "The server rejected the previous plan because it did not match a closed, "
        "high-confidence semantic request policy. Regenerate from the original student "
        "question without copying or discussing the rejected plan."
    ),
}


def build_plan_repair_message(
    repair_reason: str,
    repair_details: Mapping[str, Sequence[str]] | None,
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """Build one bounded server-authored repair turn without rejected plan values."""

    if repair_reason not in _PLAN_REPAIR_INSTRUCTIONS:
        raise ValueError("repair_reason is not a supported closed failure category")
    closed_repair_details = _closed_repair_details(
        repair_reason,
        repair_details,
        advertised_tools=advertised_tools,
    )
    if not repair_reason:
        return None
    details_suffix = (
        " Closed coverage details: " + "; ".join(closed_repair_details) + "."
        if closed_repair_details
        else ""
    )
    validated_policy_ids: tuple[SemanticPolicyId, ...] = ()
    if any(detail.startswith("policy_ids=") for detail in closed_repair_details):
        validated_policy_ids = tuple(
            dict.fromkeys(
                SemanticPolicyId(str(policy_id).strip())
                for policy_id in (repair_details or {}).get("policy_ids", ())
            )
        )
    policy_suffix = "".join(
        " Validated closed policy requirement for "
        f"{policy.value}: {_SEMANTIC_POLICY_REPAIR_MAPPINGS[policy]}."
        for policy in validated_policy_ids
    )
    return {
        "role": "user",
        "content": (
            _PLAN_REPAIR_INSTRUCTIONS[repair_reason]
            + details_suffix
            + policy_suffix
            + " Return only the submit_student_turn_plan function call."
        ),
    }


def plan_student_turn(
    llm_client: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    advertised_tools: Sequence[Mapping[str, Any]],
    max_calls: int,
    model: str | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    deadline_monotonic: float | None = None,
    max_attempts: int = 2,
    repair_reason: str = "",
    repair_details: Mapping[str, Sequence[str]] | None = None,
    schema_repair_policy_ids: Sequence[str] = (),
) -> TurnPlanningResult:
    """Obtain one forced, schema-constrained semantic plan from the LLM client.

    One bounded regeneration is allowed when the provider's first nested plan
    fails the server schema.  The rejected assistant payload is never copied
    into the next request; only a short server-authored error path is supplied.
    """

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be an integer")
    if not 1 <= max_attempts <= 2:
        raise ValueError("max_attempts must be between 1 and 2")
    repair_message = build_plan_repair_message(
        repair_reason,
        repair_details,
        advertised_tools=advertised_tools,
    )
    if isinstance(schema_repair_policy_ids, str):
        raise ValueError("schema_repair_policy_ids must be a bounded string sequence")
    schema_policy_ids = tuple(schema_repair_policy_ids)
    schema_repair = build_plan_repair_message(
        "plan_validation_failed",
        {"policy_ids": schema_policy_ids} if schema_policy_ids else {},
        advertised_tools=advertised_tools,
    )
    if schema_repair is None:  # pragma: no cover - closed non-empty reason
        raise AssertionError("schema repair instruction is missing")

    tools = tuple(advertised_tools)
    planner_tool = _provider_compatible_turn_plan_tool_schema(
        build_turn_plan_tool_schema(tools, max_calls=max_calls)
    )
    kwargs: dict[str, Any] = {
        "temperature": 0.0,
        "tool_choice": "required",
    }
    if model is not None:
        kwargs["model"] = model
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    if deadline_monotonic is not None:
        kwargs["deadline_monotonic"] = deadline_monotonic

    planner_messages = [copy.deepcopy(dict(message)) for message in messages]
    if repair_message is not None:
        planner_messages.append(repair_message)
    external_repair_policy_ids = tuple(
        dict.fromkeys(
            SemanticPolicyId(str(policy_id).strip())
            for policy_id in (repair_details or {}).get("policy_ids", ())
        )
    )
    schema_repair_policy_values = tuple(
        dict.fromkeys(SemanticPolicyId(str(policy_id).strip()) for policy_id in schema_policy_ids)
    )
    provider_turns: list[ToolChatResult] = []
    for attempt in range(max_attempts):
        focused_policy_ids = (
            external_repair_policy_ids
            if repair_message is not None
            else schema_repair_policy_values
            if attempt > 0
            else ()
        )
        call_kwargs = {
            **kwargs,
            "tools": [
                _repair_focused_provider_schema(planner_tool, focused_policy_ids)
                if focused_policy_ids
                else copy.deepcopy(planner_tool)
            ],
        }
        provider_turn = llm_client.chat_with_tools(
            copy.deepcopy(planner_messages),
            **call_kwargs,
        )
        provider_turns.append(provider_turn)
        try:
            plan = parse_turn_plan_result(
                provider_turn,
                advertised_tools=tools,
                max_calls=max_calls,
            )
        except TurnPlanValidationError as exc:
            if attempt + 1 >= max_attempts:
                exc.retain_provider_turns(provider_turns)
                raise
            planner_messages.append(schema_repair)
            continue
        return TurnPlanningResult(
            plan=plan,
            provider_turn=provider_turn,
            provider_turns=tuple(provider_turns),
        )
    raise AssertionError("bounded turn planning loop exhausted unexpectedly")


def synthesize_tool_calls(
    plan: StudentTurnPlan,
    *,
    call_id_prefix: str = "v21_evidence",
) -> tuple[ToolCallRequest, ...]:
    """Convert a validated execute plan into ordinary capability tool calls."""

    if plan.decision is not TurnPlanDecision.EXECUTE:
        return ()
    prefix = str(call_id_prefix or "v21_evidence").strip() or "v21_evidence"
    calls: list[ToolCallRequest] = []
    for index, request in enumerate(plan.evidence_requests, start=1):
        arguments = copy.deepcopy(request.arguments)
        raw_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        calls.append(
            ToolCallRequest(
                id=f"{prefix}_{index}",
                name=request.capability,
                arguments=arguments,
                raw_arguments=raw_arguments,
            )
        )
    return tuple(calls)


def synthesize_tool_chat_result(
    plan: StudentTurnPlan,
    *,
    model: str,
    usage: Mapping[str, Any] | None = None,
    model_revision: str = "",
    call_id_prefix: str = "v21_evidence",
) -> ToolChatResult:
    """Build the tool-turn shape consumed by V2's existing execution loop."""

    calls = synthesize_tool_calls(plan, call_id_prefix=call_id_prefix)
    if not calls:
        raise ValueError("Only an execute plan can be synthesized as a tool chat result.")
    assistant_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": call.raw_arguments,
            },
        }
        for call in calls
    ]
    return ToolChatResult(
        content="",
        tool_calls=calls,
        model=str(model or ""),
        usage=copy.deepcopy(dict(usage or {})),
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": assistant_calls,
        },
        model_revision=str(model_revision or ""),
    )


__all__ = [
    "EXPLICIT_CONSTRAINT_FIELD_PATHS",
    "MAX_RAW_PLAN_ARGUMENT_CHARS",
    "TURN_PLAN_TOOL_NAME",
    "ArgumentProvenanceContract",
    "ArgumentProvenanceMode",
    "ArgumentProvenanceRule",
    "EVIDENCE_BACKED_REQUEST_OUTCOMES",
    "PlannedCapabilityCall",
    "SERVER_OWNED_EXECUTE_OUTCOMES",
    "StudentRequestOutcome",
    "StudentTurnPlan",
    "TurnPlanDecision",
    "TurnPlanProvenanceError",
    "TurnPlanSchemaError",
    "TurnPlanValidationError",
    "TurnPlanningResult",
    "UNSUPPORTED_REQUEST_OUTCOMES",
    "build_turn_plan_tool_schema",
    "build_plan_repair_message",
    "normalise_provenance_identifier",
    "normalise_provenance_text",
    "parse_turn_plan_result",
    "plan_student_turn",
    "synthesize_tool_calls",
    "synthesize_tool_chat_result",
    "validate_capability_argument_provenance",
    "validate_capability_arguments",
    "validate_plan_argument_provenance",
]
