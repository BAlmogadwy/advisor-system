"""Exact advisor-only optimization of a graduation forecast's starting term.

The optimized mode is intentionally isolated from the shared recommender. It
uses the recorded current-section snapshot only for the first term, requires
strict (passed/earned-only) eligibility, and delegates every later term to the
normal parity-based graduation simulator.

``TermSection`` has no year/term columns. The caller must provide the separately
configured snapshot clock and it must exactly match the forecast term. Recorded
sections are catalogue evidence, never a seat, clash-free timetable,
registration permission, or actual registration.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.models import ElectiveCourse, ProgrammeRequirement, Student, TermSection
from core.services.academic_state import AcademicStateUnavailable, build_student_academic_state
from core.services.course_identity import normalize_course_name, planner_course_key
from core.services.eligibility import evaluate_prerequisites, split_hour_prereqs
from core.services.section_snapshot_guard import section_snapshot_operation_guard
from core.services.student_graduation import (
    DEFAULT_MAX_CREDITS_PER_TERM,
    RECOMMENDED_CURRENT_TERM,
    build_graduation_report,
)
from core.services.student_helpers import (
    get_program_prerequisites,
    get_student_passed_and_studying,
    is_elective_slot,
    normalize_code,
)
from core.services.student_sections import (
    UnknownStudentGender,
    gender_section_filter,
    student_gender_strict,
)

OPTIMIZED_CURRENT_OFFERINGS = "optimized_current_offerings"

# Search is exact. If a malformed/unusually broad plan exceeds this bound, fail
# instead of publishing a heuristic result under the word "optimized".
MAX_FEASIBLE_SUBSETS = 20_000


class OptimizedGraduationUnavailable(RuntimeError):
    """Trustworthy evidence is insufficient to build the optimized scenario."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, Any] | None = None,
        status: int = 422,
    ):
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})
        self.status = int(status)


@dataclass(frozen=True)
class _OfferingAuthority:
    course_code: str
    course_name: str
    direct_programmes: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    plan_code: str
    plan_name: str
    offered_course_code: str
    offered_course_name: str
    credits: int
    programme_term: int
    requirement_type: str
    requirement_order: int
    unlock_value: int
    mapping_kind: str
    recorded_sections: tuple[str, ...]
    recorded_section_programmes: tuple[str, ...]

    def report_row(self) -> dict[str, Any]:
        mapped = self.mapping_kind == "TERM_MAPPED_ELECTIVE"
        return {
            # Simulation completes the plan requirement. For a mapped elective
            # that is the placeholder, while offered_course_code is the real
            # course the student would register in.
            "code": self.plan_code,
            "name": self.offered_course_name if mapped else self.plan_name,
            "credits": self.credits,
            "section": "",
            "requirement_type": self.requirement_type,
            "programme_term": self.programme_term,
            "elective_slot": mapped,
            "offered_course_code": self.offered_course_code,
            "offered_course_name": self.offered_course_name,
            "fulfills_plan_code": self.plan_code if mapped else "",
            "mapping_kind": self.mapping_kind,
            # Evidence only: no individual section has been selected.
            "recorded_sections": list(self.recorded_sections),
            "recorded_section_count": len(self.recorded_sections),
            "recorded_section_programmes": list(self.recorded_section_programmes),
        }


def _downstream_values(
    plan_codes: set[str], prerequisite_map: dict[str, list[str]]
) -> dict[str, int]:
    """Count transitive in-plan dependants for a deterministic tie-break."""
    reverse: dict[str, set[str]] = {code: set() for code in plan_codes}
    for raw_course, raw_prerequisites in prerequisite_map.items():
        course = normalize_code(raw_course)
        if course not in plan_codes:
            continue
        prerequisites, _required_hours = split_hour_prereqs(raw_prerequisites)
        for raw_prerequisite in prerequisites:
            prerequisite = normalize_code(raw_prerequisite)
            if prerequisite in plan_codes:
                reverse.setdefault(prerequisite, set()).add(course)

    values: dict[str, int] = {}
    for root in plan_codes:
        seen: set[str] = set()
        stack = list(reverse.get(root, set()))
        while stack:
            course = stack.pop()
            if course in seen:
                continue
            seen.add(course)
            stack.extend(reverse.get(course, set()) - seen)
        values[root] = len(seen)
    return values


def _identity_indexes(
    codes: set[str],
) -> tuple[dict[tuple[str, str], set[str]], dict[str, set[str]]]:
    """Load programme identities without assuming normalized legacy DB values."""
    by_program_code: dict[tuple[str, str], set[str]] = {}
    by_code: dict[str, set[str]] = {}

    def add(program: object, raw_code: object, raw_name: object) -> None:
        program_n = normalize_code(program)
        code = normalize_code(raw_code)
        if not program_n or code not in codes:
            return
        identity = planner_course_key(code, raw_name)
        by_program_code.setdefault((program_n, code), set()).add(identity)
        by_code.setdefault(code, set()).add(identity)

    # Normalize in Python: a legacy "cross 200" row can prove CROSS200.
    for program, code, name in ProgrammeRequirement.objects.values_list(
        "program", "course_code", "course_name"
    ):
        add(program, code, name)
    for program, code, name in ElectiveCourse.objects.values_list(
        "programme", "course_code", "course_name"
    ):
        add(program, code, name)
    return by_program_code, by_code


def _matching_recorded_sections(
    *,
    cohort: str,
    authorities: dict[str, _OfferingAuthority],
) -> tuple[dict[str, dict[str, tuple[str, ...]]], dict[str, Any]]:
    """Capture trustworthy same-cohort recorded sections across programmes."""
    codes = set(authorities)
    if not codes:
        return {}, {"fingerprint": "", "source_tags": [], "captured_section_count": 0}

    identities_by_program_code, identities_by_code = _identity_indexes(codes)
    with section_snapshot_operation_guard(blocking=False) as acquired:
        if not acquired:
            raise OptimizedGraduationUnavailable(
                "The current-section snapshot is being updated. Retry shortly.",
                code="SECTION_SNAPSHOT_BUSY",
                details={"retryable": True},
                status=503,
            )
        sections = list(
            TermSection.objects.filter(
                scenario__isnull=True,
                course_key__in=sorted(codes),
            )
            .filter(gender_section_filter(cohort))
            .prefetch_related("program_links")
            .order_by("course_key", "section", "id")
        )
        fingerprint_rows = [
            {
                "id": int(section.id),
                "course_key": normalize_code(section.course_key),
                "course_name": str(section.course_name or ""),
                "section": str(section.section or ""),
                "source_tag": str(section.source_tag or ""),
                "updated_at": str(section.updated_at or ""),
                "programmes": sorted(
                    normalize_code(link.program)
                    for link in section.program_links.all()
                    if normalize_code(link.program)
                ),
            }
            for section in sections
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_rows,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    labels_by_code: dict[str, set[str]] = {}
    programmes_by_code: dict[str, set[str]] = {}
    for section in sections:
        code = normalize_code(section.course_key)
        authority = authorities.get(code)
        if authority is None:
            continue
        expected_identity = planner_course_key(code, authority.course_name)
        raw_section_name = str(section.course_name or "").strip()
        comparable_section_name = normalize_course_name(raw_section_name)
        if comparable_section_name and re.search(r"[A-Za-z]", raw_section_name):
            # Latin/number names compare directly. Arabic scraped names normalize
            # blank and rely on the programme identity link below.
            if planner_course_key(code, raw_section_name) != expected_identity:
                continue
        elif not raw_section_name and len(identities_by_code.get(code, set())) > 1:
            # Blank evidence cannot resolve a genuinely ambiguous display code.
            continue

        linked_programmes = {
            normalize_code(link.program)
            for link in section.program_links.all()
            if normalize_code(link.program)
        }
        if not linked_programmes:
            continue
        direct_programmes = set(authority.direct_programmes)
        identity_can_cross_programmes = bool(normalize_course_name(authority.course_name))
        matching_programmes = {
            linked_programme
            for linked_programme in linked_programmes
            if linked_programme in direct_programmes
            or (
                identity_can_cross_programmes
                and expected_identity
                in identities_by_program_code.get((linked_programme, code), set())
            )
        }
        if not matching_programmes:
            continue

        label = str(section.section or "").strip().upper() or "SHARED"
        labels_by_code.setdefault(code, set()).add(label)
        programmes_by_code.setdefault(code, set()).update(matching_programmes)

    evidence = {
        code: {
            "labels": tuple(sorted(labels)),
            "programmes": tuple(sorted(programmes_by_code.get(code, set()))),
        }
        for code, labels in labels_by_code.items()
        if labels
    }
    provenance = {
        "fingerprint": fingerprint,
        "source_tags": sorted({str(section.source_tag or "") for section in sections}),
        "captured_section_count": len(sections),
    }
    return evidence, provenance


def _strictly_met(
    raw_prerequisites: Iterable[str],
    *,
    passed: set[str],
    earned_credits: int,
) -> bool:
    return evaluate_prerequisites(
        raw_prerequisites,
        passed,
        set(),
        strict_passed_only=True,
        earned_credits=earned_credits,
        registered_credits=0,
    ).met


def _feasible_states(candidates: list[_Candidate], cap: int) -> list[tuple[int, ...]]:
    """Enumerate every non-empty feasible baseline or fail at the ceiling."""
    states: list[tuple[int, ...]] = []

    def visit(
        index: int,
        chosen: tuple[int, ...],
        credits: int,
        plan_codes: frozenset[str],
        offered_codes: frozenset[str],
    ) -> None:
        if index >= len(candidates):
            if chosen:
                states.append(chosen)
                if len(states) > MAX_FEASIBLE_SUBSETS:
                    raise OptimizedGraduationUnavailable(
                        "The exact optimized-baseline search exceeds its safe limit.",
                        code="OPTIMIZATION_SEARCH_LIMIT_EXCEEDED",
                        details={"maximum_feasible_subsets": MAX_FEASIBLE_SUBSETS},
                    )
            return

        visit(index + 1, chosen, credits, plan_codes, offered_codes)
        candidate = candidates[index]
        if (
            credits + candidate.credits <= cap
            and candidate.plan_code not in plan_codes
            and candidate.offered_course_code not in offered_codes
        ):
            visit(
                index + 1,
                (*chosen, index),
                credits + candidate.credits,
                plan_codes | {candidate.plan_code},
                offered_codes | {candidate.offered_course_code},
            )

    visit(0, (), 0, frozenset(), frozenset())
    return states


def _inclusion_maximal_states(
    states: list[tuple[int, ...]], candidates: list[_Candidate], cap: int
) -> list[tuple[int, ...]]:
    """Discard baselines to which another eligible requirement can be added."""
    maximal: list[tuple[int, ...]] = []
    all_indexes = set(range(len(candidates)))
    for state in states:
        selected = set(state)
        credits = sum(candidates[index].credits for index in state)
        plan_codes = {candidates[index].plan_code for index in state}
        offered_codes = {candidates[index].offered_course_code for index in state}
        can_add = any(
            credits + candidates[index].credits <= cap
            and candidates[index].plan_code not in plan_codes
            and candidates[index].offered_course_code not in offered_codes
            for index in all_indexes - selected
        )
        if not can_add:
            maximal.append(state)
    return maximal


def _report_score(
    report: dict[str, Any],
    selected: tuple[int, ...],
    candidates: list[_Candidate],
) -> tuple[Any, ...]:
    """The exact deterministic optimization contract."""
    completed = bool(report.get("simulation_completed"))
    raw_terms = report.get("estimated_terms_including_planning_baseline")
    terms = int(raw_terms) if completed and raw_terms is not None else 10_000
    blockers = len(report.get("unresolved_requirements") or [])
    credits = sum(candidates[index].credits for index in selected)
    unlock_value = sum(candidates[index].unlock_value for index in selected)
    signature = tuple(
        (
            candidates[index].requirement_order,
            candidates[index].plan_code,
            candidates[index].offered_course_code,
        )
        for index in selected
    )
    return (
        0 if completed else 1,
        terms,
        blockers,
        -credits,
        -unlock_value,
        signature,
    )


def _relabel_report(
    report: dict[str, Any],
    *,
    optimization: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    relabeled = dict(report)
    relabeled["planning_baseline_kind"] = OPTIMIZED_CURRENT_OFFERINGS
    baseline = dict(relabeled.get("planning_baseline") or {})
    baseline["kind"] = OPTIMIZED_CURRENT_OFFERINGS
    relabeled["planning_baseline"] = baseline
    relabeled["offering_optimization"] = optimization
    relabeled["optimization"] = optimization  # compatibility alias
    relabeled["planning_baseline_provenance"] = provenance
    assumptions = list(relabeled.get("simulation_assumptions") or [])
    assumptions.insert(
        0,
        "The optimized starting term uses passed-only prerequisites and earned-only "
        "credit-hour gates against the recorded current-section snapshot.",
    )
    assumptions.insert(
        1,
        "A recorded section may come from another programme only when its course "
        "identity matches this degree plan; no section, seat, or registration is guaranteed.",
    )
    relabeled["simulation_assumptions"] = assumptions
    return relabeled


def build_optimized_current_offerings_report(
    student_id: int,
    year: int,
    term: int,
    *,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
    section_snapshot_academic_year: int | None = None,
    section_snapshot_term: int | None = None,
) -> dict[str, Any]:
    """Return the exact advisor-only optimized current-section forecast."""
    try:
        requested_pair = (int(year), int(term))
    except (TypeError, ValueError):
        raise OptimizedGraduationUnavailable(
            "The graduation planning term is invalid.", code="INVALID_GRADUATION_TERM"
        ) from None
    try:
        snapshot_pair = (int(section_snapshot_academic_year), int(section_snapshot_term))
    except (TypeError, ValueError):
        snapshot_pair = None
    if snapshot_pair != requested_pair:
        raise OptimizedGraduationUnavailable(
            "The recorded section snapshot is not verified for this graduation term.",
            code="SECTION_SNAPSHOT_TERM_MISMATCH",
            details={
                "planning_term": {"academic_year": requested_pair[0], "term": requested_pair[1]},
                "section_snapshot_term": (
                    {"academic_year": snapshot_pair[0], "term": snapshot_pair[1]}
                    if snapshot_pair
                    else None
                ),
            },
        )
    if requested_pair[1] not in {1, 2}:
        raise OptimizedGraduationUnavailable(
            "Optimized graduation planning supports main terms 1 and 2 only.",
            code="UNSUPPORTED_GRADUATION_TERM",
        )

    cap = max(1, int(max_credits_per_term))
    student = Student.objects.filter(student_id=student_id).first()
    if student is None:
        raise OptimizedGraduationUnavailable(
            "Student record was not found.", code="STUDENT_NOT_FOUND"
        )
    program = normalize_code(student.program)
    if not program:
        raise OptimizedGraduationUnavailable(
            "The student's programme is not recorded.", code="PROGRAMME_UNRESOLVED"
        )
    try:
        cohort = student_gender_strict(student_id)
    except UnknownStudentGender as exc:
        raise OptimizedGraduationUnavailable(str(exc), code="COHORT_UNRESOLVED") from exc

    requirements = list(
        ProgrammeRequirement.objects.filter(program=student.program)
        .order_by("programme_term", "course_code")
        .values(
            "course_code",
            "course_name",
            "programme_term",
            "credit_hours",
            "type",
        )
    )
    if not requirements:
        raise OptimizedGraduationUnavailable(
            "No degree-plan requirements are recorded for this student.",
            code="PROGRAMME_PLAN_UNAVAILABLE",
        )

    passed, _studying = get_student_passed_and_studying(student_id)
    passed = {normalize_code(code) for code in passed if normalize_code(code)}
    earned_credits = int(student.total_earned_credits or 0)
    prerequisite_map = get_program_prerequisites(program)
    plan_codes = {
        normalize_code(row.get("course_code"))
        for row in requirements
        if normalize_code(row.get("course_code"))
    }
    downstream = _downstream_values(plan_codes, prerequisite_map)
    requirement_by_code = {
        normalize_code(row.get("course_code")): row
        for row in requirements
        if normalize_code(row.get("course_code"))
    }
    requirement_order = {
        normalize_code(row.get("course_code")): index
        for index, row in enumerate(requirements)
        if normalize_code(row.get("course_code"))
    }
    direct_plan_codes = {
        code for code, row in requirement_by_code.items() if not is_elective_slot(row.get("type"))
    }

    authorities: dict[str, _OfferingAuthority] = {}
    direct_specs: list[tuple[str, dict[str, Any]]] = []
    for code in sorted(direct_plan_codes, key=lambda value: requirement_order[value]):
        row = requirement_by_code[code]
        if code in passed:
            continue
        direct_specs.append((code, row))
        authorities[code] = _OfferingAuthority(
            course_code=code,
            course_name=str(row.get("course_name") or ""),
            direct_programmes=(program,),
        )

    elective_specs: list[tuple[str, dict[str, Any], Any, list[str]]] = []
    try:
        academic_state = build_student_academic_state(
            student_id, str(requested_pair[0]), str(requested_pair[1])
        )
    except AcademicStateUnavailable as exc:
        raise OptimizedGraduationUnavailable(str(exc), code="ACADEMIC_STATE_UNAVAILABLE") from exc
    for plan_code, row in requirement_by_code.items():
        if not is_elective_slot(row.get("type")) or plan_code in passed:
            continue
        state_row = academic_state.course(plan_code)
        if state_row is None:
            continue
        plan_credits = max(0, int(row.get("credit_hours") or 0))
        for option in state_row.elective_options:
            actual_code = normalize_code(option.course_code)
            if (
                not actual_code
                or actual_code in passed
                or actual_code in direct_plan_codes
                or int(option.credit_hours or 0) != plan_credits
            ):
                continue
            elective_row = (
                ElectiveCourse.objects.filter(
                    programme__iexact=option.mapping_programme,
                    course_code__iexact=actual_code,
                )
                .values_list("prerequisites_csv", flat=True)
                .first()
            )
            elective_prerequisites = [
                normalize_code(value)
                for value in str(elective_row or "").split(",")
                if normalize_code(value)
            ]
            if not _strictly_met(
                prerequisite_map.get(plan_code, []),
                passed=passed,
                earned_credits=earned_credits,
            ) or not _strictly_met(
                elective_prerequisites,
                passed=passed,
                earned_credits=earned_credits,
            ):
                continue
            elective_specs.append((plan_code, row, option, elective_prerequisites))
            authority = _OfferingAuthority(
                course_code=actual_code,
                course_name=str(option.course_name or ""),
                direct_programmes=tuple(
                    sorted(
                        {
                            normalize_code(option.mapping_programme),
                            program,
                            program[:-1] if program.endswith("2") else program,
                        }
                        - {""}
                    )
                ),
            )
            existing = authorities.get(actual_code)
            if existing is None:
                authorities[actual_code] = authority
            elif planner_course_key(actual_code, existing.course_name) != planner_course_key(
                actual_code, authority.course_name
            ):
                # Conflicting mappings cannot establish one course identity.
                authorities.pop(actual_code, None)

    section_evidence, snapshot_capture = _matching_recorded_sections(
        cohort=cohort, authorities=authorities
    )

    candidates: list[_Candidate] = []
    rejection_counts = {
        "strict_eligibility": 0,
        "no_matching_recorded_section": 0,
        "over_credit_cap": 0,
    }
    for code, row in direct_specs:
        if not _strictly_met(
            prerequisite_map.get(code, []),
            passed=passed,
            earned_credits=earned_credits,
        ):
            rejection_counts["strict_eligibility"] += 1
            continue
        evidence = section_evidence.get(code)
        if evidence is None:
            rejection_counts["no_matching_recorded_section"] += 1
            continue
        credits = max(0, int(row.get("credit_hours") or 0))
        if credits > cap:
            rejection_counts["over_credit_cap"] += 1
            continue
        candidates.append(
            _Candidate(
                plan_code=code,
                plan_name=str(row.get("course_name") or ""),
                offered_course_code=code,
                offered_course_name=str(row.get("course_name") or ""),
                credits=credits,
                programme_term=int(row.get("programme_term") or 0),
                requirement_type=str(row.get("type") or ""),
                requirement_order=requirement_order[code],
                unlock_value=downstream.get(code, 0),
                mapping_kind="DIRECT_PLAN_COURSE",
                recorded_sections=evidence["labels"],
                recorded_section_programmes=evidence["programmes"],
            )
        )
    for plan_code, row, option, _elective_prerequisites in elective_specs:
        actual_code = normalize_code(option.course_code)
        evidence = section_evidence.get(actual_code)
        if evidence is None:
            rejection_counts["no_matching_recorded_section"] += 1
            continue
        credits = max(0, int(option.credit_hours or 0))
        if credits > cap:
            rejection_counts["over_credit_cap"] += 1
            continue
        candidates.append(
            _Candidate(
                plan_code=plan_code,
                plan_name=str(row.get("course_name") or ""),
                offered_course_code=actual_code,
                offered_course_name=str(option.course_name or ""),
                credits=credits,
                programme_term=int(row.get("programme_term") or 0),
                requirement_type=str(row.get("type") or ""),
                requirement_order=requirement_order[plan_code],
                unlock_value=downstream.get(plan_code, 0),
                mapping_kind="TERM_MAPPED_ELECTIVE",
                recorded_sections=evidence["labels"],
                recorded_section_programmes=evidence["programmes"],
            )
        )

    if not candidates:
        raise OptimizedGraduationUnavailable(
            "No strictly eligible degree-plan course has a trustworthy recorded "
            "section for this student's cohort.",
            code="NO_ELIGIBLE_RECORDED_SECTIONS",
            details={"cohort": cohort, "program": program},
        )
    candidates.sort(
        key=lambda candidate: (
            candidate.requirement_order,
            candidate.plan_code,
            candidate.offered_course_code,
        )
    )

    feasible_states = _feasible_states(candidates, cap)
    maximal_states = _inclusion_maximal_states(feasible_states, candidates, cap)
    best_state: tuple[int, ...] | None = None
    best_score: tuple[Any, ...] | None = None
    best_report: dict[str, Any] | None = None
    query_cache: dict[object, object] = {}
    for state in maximal_states:
        current_courses = [candidates[index].report_row() for index in state]
        candidate_report = build_graduation_report(
            student_id,
            requested_pair[0],
            requested_pair[1],
            planning_baseline_kind=RECOMMENDED_CURRENT_TERM,
            max_credits_per_term=cap,
            _current_courses_override=current_courses,
            _prerequisite_map=prerequisite_map,
            _query_cache=query_cache,
        )
        if not candidate_report:
            continue
        score = _report_score(candidate_report, state, candidates)
        if best_score is None or score < best_score:
            best_state, best_score, best_report = state, score, candidate_report

    if best_state is None or best_report is None:
        raise OptimizedGraduationUnavailable(
            "The exact optimized graduation scenario could not be evaluated.",
            code="OPTIMIZED_SCENARIO_UNAVAILABLE",
        )
    selected = [candidates[index] for index in best_state]

    optimization = {
        "mode": OPTIMIZED_CURRENT_OFFERINGS,
        "strict_passed_only": True,
        "earned_hours_only": True,
        "section_snapshot_academic_year": snapshot_pair[0],
        "section_snapshot_term": snapshot_pair[1],
        "objective": [
            "complete_forecast",
            "fewest_terms_including_baseline",
            "fewest_unresolved_requirements",
            "most_baseline_credits",
            "greatest_downstream_value",
            "canonical_plan_and_offered_course_order",
        ],
        "optimization_complete": True,
        "search_method": "exact_inclusion_maximal_subsets",
        "candidate_count": len(candidates),
        "feasible_subset_count": len(feasible_states),
        "evaluated_maximal_subset_count": len(maximal_states),
        "max_credits": cap,
        "selected_credits": sum(candidate.credits for candidate in selected),
        "selected_plan_codes": [candidate.plan_code for candidate in selected],
        "selected_course_codes": [candidate.plan_code for candidate in selected],
        "selected_offered_course_codes": [candidate.offered_course_code for candidate in selected],
        "rejection_counts": rejection_counts,
    }
    provenance = {
        "source": "recorded_current_section_snapshot",
        "section_snapshot_academic_year": snapshot_pair[0],
        "section_snapshot_term": snapshot_pair[1],
        "section_snapshot_fingerprint": snapshot_capture["fingerprint"],
        "section_source_tags": snapshot_capture["source_tags"],
        "captured_section_count": snapshot_capture["captured_section_count"],
        "cohort": cohort,
        "strict_passed_only": True,
        "earned_hours_only": True,
        "cross_programme_sections_allowed": True,
        "cross_programme_identity_rule": "planner_course_identity_match",
        "seats_guaranteed": False,
        "timetable_conflicts_checked": False,
        "registration_guaranteed": False,
    }
    return _relabel_report(best_report, optimization=optimization, provenance=provenance)


__all__ = [
    "OPTIMIZED_CURRENT_OFFERINGS",
    "OptimizedGraduationUnavailable",
    "build_optimized_current_offerings_report",
]
