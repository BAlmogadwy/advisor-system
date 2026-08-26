"""Closed semantic-outcome coverage contract for Student Advisor V2.1.

The turn planner chooses both the student-facing deliverables (``requested_outcomes``)
and the read-only capabilities needed to produce them.  Capability argument validation
proves that a call is well formed and grounded, but it cannot prove that the selected
call answers the student's request.  This module supplies that independent, typed
postcondition.

The mapping is intentionally conservative.  Compound decisions such as "pick one
course I can add", "which registered course is safest to drop", and "improve my
current timetable" are owned by one deterministic compound capability each.  A plan
cannot pretend to satisfy them by collecting a few adjacent facts and leaving the
cross-capability conclusion to prose.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core.services.student_advisor_v21_plan import (
    SERVER_OWNED_EXECUTE_OUTCOMES,
    UNSUPPORTED_REQUEST_OUTCOMES,
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
)

OUTCOME_CAPABILITIES: dict[StudentRequestOutcome, frozenset[str]] = {
    StudentRequestOutcome.COURSE_CATALOGUE: frozenset({"lookup_course"}),
    StudentRequestOutcome.COURSE_ELIGIBILITY: frozenset({"why_course_locked"}),
    StudentRequestOutcome.PREREQUISITE_INFORMATION: frozenset(
        {"course_prerequisites", "why_course_locked", "my_progress"}
    ),
    StudentRequestOutcome.AVAILABLE_COURSES: frozenset({"my_progress"}),
    StudentRequestOutcome.COURSE_PRIORITY: frozenset({"my_progress"}),
    StudentRequestOutcome.COURSE_RECOMMENDATION: frozenset({"recommend_courses"}),
    StudentRequestOutcome.COURSE_ADDITION: frozenset({"recommend_feasible_course_addition"}),
    StudentRequestOutcome.COURSE_DROP_IMPACT: frozenset({"rank_current_course_drop_impact"}),
    StudentRequestOutcome.DEGREE_PROGRESS: frozenset({"my_progress"}),
    StudentRequestOutcome.DEGREE_PLAN: frozenset({"my_plan_by_term"}),
    StudentRequestOutcome.CURRENT_TIMETABLE: frozenset({"my_timetable"}),
    StudentRequestOutcome.TIMETABLE_REVIEW: frozenset({"improve_current_timetable"}),
    StudentRequestOutcome.TIMETABLE_BUILD: frozenset({"build_timetable_proposal"}),
    StudentRequestOutcome.TIMETABLE_FEASIBILITY: frozenset(
        {"my_clash_free_sections", "build_timetable_proposal"}
    ),
    StudentRequestOutcome.COURSE_COMPARISON: frozenset({"course_choice_comparison"}),
    StudentRequestOutcome.COURSE_REPLACEMENT: frozenset({"feasible_course_replacements"}),
    StudentRequestOutcome.GRADUATION_FORECAST: frozenset({"graduation_progress"}),
    # A graduation-progress call owns impact only when its validated arguments
    # contain a concrete add/remove/replacement scenario.  A baseline forecast
    # cannot answer an alternative credit-load or other unsupported comparison.
    StudentRequestOutcome.GRADUATION_IMPACT: frozenset(),
    StudentRequestOutcome.CREDIT_LOAD_COMPARISON: frozenset(),
    StudentRequestOutcome.POLICY_RULE: frozenset({"policy_lookup"}),
    StudentRequestOutcome.ACADEMIC_ADVISER: frozenset({"my_advisor"}),
    StudentRequestOutcome.PRIOR_RESULT: frozenset({"present_prior_artifact"}),
    StudentRequestOutcome.REGISTRATION_ACTION: frozenset(),
    StudentRequestOutcome.GENERAL_CONVERSATION: frozenset(),
    StudentRequestOutcome.UNSUPPORTED_REQUEST: frozenset(),
}

UNSUPPORTED_OUTCOMES = UNSUPPORTED_REQUEST_OUTCOMES
DIRECT_OUTCOMES = frozenset({StudentRequestOutcome.GENERAL_CONVERSATION})


def _owners_for_outcome(
    outcome: StudentRequestOutcome,
    requested: frozenset[StudentRequestOutcome],
    evidence_requests: Iterable[PlannedCapabilityCall] = (),
) -> frozenset[str]:
    """Return direct or compound owners for one requested deliverable.

    Compound capabilities may directly own a criterion they deterministically
    compute. The requested outcome describes the student's deliverable, not an
    implementation step the planner must repeat merely to name the compound.
    A standalone graduation *forecast* still requires ``graduation_progress``;
    a concrete add/drop/replacement/improvement impact may be answered by the
    corresponding typed compound result.
    """

    owners = set(OUTCOME_CAPABILITIES.get(outcome, frozenset()))
    if outcome is StudentRequestOutcome.GRADUATION_IMPACT:
        if any(
            request.capability == "graduation_progress"
            and (
                bool(request.arguments.get("add_current_courses"))
                or bool(request.arguments.get("remove_current_courses"))
                or bool(request.arguments.get("noncompletion_current_courses"))
                or request.arguments.get("search_better_replacements") is True
            )
            for request in evidence_requests
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
        if StudentRequestOutcome.COURSE_COMPARISON in requested and any(
            request.capability == "course_choice_comparison"
            and str(request.arguments.get("objective") or "").strip().lower() == "graduation"
            for request in evidence_requests
        ):
            # The comparator builds one fair graduation scenario per named
            # candidate.  It may own graduation impact only as a criterion of
            # that explicit comparison; a standalone forecast still belongs to
            # graduation_progress or a concrete compound decision.
            owners.add("course_choice_comparison")
    if (
        outcome is StudentRequestOutcome.COURSE_PRIORITY
        and StudentRequestOutcome.COURSE_ADDITION in requested
    ):
        owners.add("recommend_feasible_course_addition")
    if (
        outcome is StudentRequestOutcome.COURSE_REPLACEMENT
        and StudentRequestOutcome.TIMETABLE_REVIEW in requested
    ):
        owners.add("improve_current_timetable")
    if outcome is StudentRequestOutcome.COURSE_REPLACEMENT and any(
        request.capability == "graduation_progress"
        and request.arguments.get("search_better_replacements") is True
        for request in evidence_requests
    ):
        # The graduation service owns a bounded academic-only replacement
        # search.  Full timetable certification remains the distinct
        # feasible_course_replacements contract.
        owners.add("graduation_progress")
    return frozenset(owners)


@dataclass(frozen=True)
class OutcomeCoverageReport:
    """Server-verifiable semantic coverage for one typed turn plan."""

    valid: bool
    requested_outcomes: tuple[StudentRequestOutcome, ...]
    covered_outcomes: tuple[StudentRequestOutcome, ...]
    uncovered_outcomes: tuple[StudentRequestOutcome, ...]
    selected_capabilities: tuple[str, ...]
    redundant_capabilities: tuple[str, ...]
    uncovered_course_codes: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "requested_outcomes": [item.value for item in self.requested_outcomes],
            "covered_outcomes": [item.value for item in self.covered_outcomes],
            "uncovered_outcomes": [item.value for item in self.uncovered_outcomes],
            "selected_capabilities": list(self.selected_capabilities),
            "redundant_capabilities": list(self.redundant_capabilities),
            "uncovered_course_codes": list(self.uncovered_course_codes),
            "reason": self.reason,
        }


def _report(
    plan: StudentTurnPlan,
    *,
    valid: bool,
    covered: Iterable[StudentRequestOutcome] = (),
    uncovered: Iterable[StudentRequestOutcome] = (),
    redundant: Iterable[str] = (),
    uncovered_course_codes: Iterable[str] = (),
    reason: str = "",
) -> OutcomeCoverageReport:
    return OutcomeCoverageReport(
        valid=valid,
        requested_outcomes=tuple(plan.requested_outcomes),
        covered_outcomes=tuple(covered),
        uncovered_outcomes=tuple(uncovered),
        selected_capabilities=tuple(request.capability for request in plan.evidence_requests),
        redundant_capabilities=tuple(redundant),
        uncovered_course_codes=tuple(uncovered_course_codes),
        reason=reason,
    )


def evaluate_outcome_coverage(
    plan: StudentTurnPlan,
    *,
    advertised_capabilities: Iterable[str] | None = None,
    explicit_course_codes: Iterable[str] = (),
) -> OutcomeCoverageReport:
    """Evaluate whether a plan minimally fulfils every requested deliverable.

    This is deliberately independent of the model prompt and capability argument
    schemas.  It is a server-owned launch boundary and must run before execution.
    """

    outcomes = tuple(plan.requested_outcomes)
    if not outcomes:
        return _report(plan, valid=False, uncovered=(), reason="outcomes_missing")
    if len(set(outcomes)) != len(outcomes):
        return _report(
            plan,
            valid=False,
            uncovered=outcomes,
            reason="duplicate_outcomes",
        )

    outcome_set = frozenset(outcomes)
    selected = tuple(request.capability for request in plan.evidence_requests)
    selected_set = frozenset(selected)
    advertised = (
        frozenset(str(name) for name in advertised_capabilities)
        if advertised_capabilities is not None
        else None
    )
    if advertised is not None and not selected_set <= advertised:
        return _report(
            plan,
            valid=False,
            uncovered=outcomes,
            redundant=sorted(selected_set - advertised),
            reason="capability_not_advertised",
        )

    if plan.decision is TurnPlanDecision.DIRECT:
        valid = outcome_set == DIRECT_OUTCOMES and not selected
        return _report(
            plan,
            valid=valid,
            covered=outcomes if valid else (),
            uncovered=() if valid else outcomes,
            reason="" if valid else "direct_outcome_mismatch",
        )

    if plan.decision is TurnPlanDecision.UNSUPPORTED:
        valid = bool(outcome_set) and outcome_set <= UNSUPPORTED_OUTCOMES and not selected
        return _report(
            plan,
            valid=valid,
            covered=outcomes if valid else (),
            uncovered=() if valid else outcomes,
            reason="" if valid else "unsupported_outcome_mismatch",
        )

    if plan.decision is TurnPlanDecision.CLARIFY:
        invalid = outcome_set & (DIRECT_OUTCOMES | UNSUPPORTED_OUTCOMES)
        valid = not invalid and not selected
        return _report(
            plan,
            valid=valid,
            covered=outcomes if valid else (),
            uncovered=() if valid else outcomes,
            reason="" if valid else "clarify_outcome_mismatch",
        )

    # EXECUTE: each requested outcome must have an owning selected capability,
    # and each selected capability must be justified by at least one requested
    # outcome.  This is both completeness and evidence-minimality.
    # These obligations are deliberately not evidence capabilities.  They are
    # covered by fixed server-authored limitation blocks, but only after the
    # parser has proved that an EXECUTE plan also contains an evidence-backed
    # outcome and a non-empty minimal capability set.
    server_covered = SERVER_OWNED_EXECUTE_OUTCOMES
    owners_by_outcome = {
        outcome: _owners_for_outcome(
            outcome,
            outcome_set,
            plan.evidence_requests,
        )
        for outcome in outcomes
    }
    covered = tuple(
        outcome
        for outcome in outcomes
        if outcome in server_covered or bool(owners_by_outcome[outcome] & selected_set)
    )
    uncovered = tuple(outcome for outcome in outcomes if outcome not in covered)
    justified = frozenset(
        capability for outcome in outcomes for capability in owners_by_outcome[outcome]
    )
    redundant_list = [capability for capability in selected if capability not in justified]
    for capability in selected:
        if capability in redundant_list:
            continue
        remaining = selected_set - {capability}
        if all(
            outcome in server_covered or bool(owners_by_outcome[outcome] & remaining)
            for outcome in outcomes
        ):
            redundant_list.append(capability)
    redundant = tuple(dict.fromkeys(redundant_list))
    for request in plan.evidence_requests:
        if request.capability == "graduation_progress":
            search = request.arguments.get("search_better_replacements") is True
            additions = {
                str(code or "").strip().replace("-", "").upper()
                for code in request.arguments.get("add_current_courses") or []
                if str(code or "").strip()
            }
            removals = {
                str(code or "").strip().replace("-", "").upper()
                for code in request.arguments.get("remove_current_courses") or []
                if str(code or "").strip()
            }
            noncompletion = {
                str(code or "").strip().replace("-", "").upper()
                for code in request.arguments.get("noncompletion_current_courses") or []
                if str(code or "").strip()
            }
            explicit_changes = bool(additions or removals or noncompletion)
            invalid_noncompletion = bool(
                noncompletion
                and (
                    request.arguments.get("planning_baseline_kind") != "registered_timetable"
                    or bool(additions)
                    or bool(removals)
                    or search
                )
            )
            if (search and explicit_changes) or invalid_noncompletion:
                return _report(
                    plan,
                    valid=False,
                    covered=covered,
                    uncovered=uncovered,
                    redundant=redundant,
                    reason="invalid_control_combination",
                )
        if request.capability != "improve_current_timetable":
            continue
        objective = str(request.arguments.get("objective") or "balanced").strip().lower()
        allow_replacements = request.arguments.get("allow_course_replacements")
        invalid_controls = (
            objective in {"faster_graduation", "academic_priority"}
            and allow_replacements is not True
        ) or (objective == "schedule_quality" and allow_replacements is not False)
        if invalid_controls:
            return _report(
                plan,
                valid=False,
                covered=covered,
                uncovered=uncovered,
                redundant=redundant,
                reason="invalid_control_combination",
            )
    target_outcomes = frozenset(
        {
            StudentRequestOutcome.COURSE_CATALOGUE,
            StudentRequestOutcome.COURSE_ELIGIBILITY,
            StudentRequestOutcome.PREREQUISITE_INFORMATION,
            StudentRequestOutcome.COURSE_ADDITION,
            StudentRequestOutcome.COURSE_DROP_IMPACT,
            StudentRequestOutcome.TIMETABLE_BUILD,
            StudentRequestOutcome.TIMETABLE_FEASIBILITY,
            StudentRequestOutcome.COURSE_COMPARISON,
            StudentRequestOutcome.COURSE_REPLACEMENT,
            StudentRequestOutcome.GRADUATION_IMPACT,
        }
    )
    required_codes = {
        str(code or "").replace("-", "").upper()
        for code in explicit_course_codes
        if str(code or "").strip()
    }
    planned_codes: set[str] = set()

    def collect_codes(value: object, *, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect_codes(child, key=str(child_key))
        elif isinstance(value, list | tuple):
            for child in value:
                collect_codes(child, key=key)
        elif key in {
            "query",
            "course_code",
            "course_codes",
            "candidate_courses",
            "must_take_courses",
            "remove_course",
            "add_course",
            "remove_current_courses",
            "noncompletion_current_courses",
            "add_current_courses",
        }:
            token = str(value or "").strip().replace("-", "").upper()
            if token:
                planned_codes.add(token)

    enforce_entity_coverage = bool(outcome_set & target_outcomes)
    if enforce_entity_coverage:
        for request in plan.evidence_requests:
            collect_codes(request.arguments)
    uncovered_codes = (
        tuple(sorted(required_codes - planned_codes)) if enforce_entity_coverage else ()
    )

    valid = not uncovered and not redundant and not uncovered_codes and bool(selected)
    reason = ""
    if uncovered:
        reason = "requested_outcome_uncovered"
    elif redundant:
        reason = "unnecessary_capability"
    elif uncovered_codes:
        reason = "requested_entity_uncovered"
    elif not selected:
        reason = "evidence_missing"
    return _report(
        plan,
        valid=valid,
        covered=covered,
        uncovered=uncovered,
        redundant=redundant,
        uncovered_course_codes=uncovered_codes,
        reason=reason,
    )


def minimise_redundant_capabilities(
    plan: StudentTurnPlan,
    *,
    advertised_capabilities: Iterable[str] | None = None,
    explicit_course_codes: Iterable[str] = (),
    report: OutcomeCoverageReport | None = None,
) -> tuple[StudentTurnPlan, OutcomeCoverageReport, tuple[str, ...]]:
    """Remove only capabilities the closed outcome contract proves redundant.

    This is not semantic routing and it never fills a missing tool or argument.
    The model has already supplied a schema-valid, provenance-valid plan.  When
    the coverage checker proves that one selected capability contributes no
    requested outcome that the remaining calls do not already cover, retaining
    it would disclose more evidence than necessary and make an otherwise valid
    turn fail closed.  Re-evaluate the reduced plan before accepting it; every
    other coverage failure remains untouched.
    """

    initial = report or evaluate_outcome_coverage(
        plan,
        advertised_capabilities=advertised_capabilities,
        explicit_course_codes=explicit_course_codes,
    )
    if initial.reason != "unnecessary_capability" or not initial.redundant_capabilities:
        return plan, initial, ()

    redundant = frozenset(initial.redundant_capabilities)
    kept_requests = tuple(
        request for request in plan.evidence_requests if request.capability not in redundant
    )
    removed = tuple(
        request.capability for request in plan.evidence_requests if request.capability in redundant
    )
    if not removed or not kept_requests:
        return plan, initial, ()

    candidate = StudentTurnPlan(
        decision=plan.decision,
        requested_outcomes=plan.requested_outcomes,
        evidence_requests=kept_requests,
        clarification_kind=plan.clarification_kind,
        clarification_question=plan.clarification_question,
    )
    candidate_report = evaluate_outcome_coverage(
        candidate,
        advertised_capabilities=advertised_capabilities,
        explicit_course_codes=explicit_course_codes,
    )
    if not candidate_report.valid:
        return plan, initial, ()
    return candidate, candidate_report, removed


__all__ = [
    "DIRECT_OUTCOMES",
    "OUTCOME_CAPABILITIES",
    "OutcomeCoverageReport",
    "UNSUPPORTED_OUTCOMES",
    "evaluate_outcome_coverage",
    "minimise_redundant_capabilities",
]
