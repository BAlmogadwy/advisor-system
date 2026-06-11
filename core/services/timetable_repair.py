"""Read-only registration repair analysis foundation.

This module is intentionally conservative.  It creates an audited repair run,
captures the current timetable/registration snapshot, bounds the affected
component, and evaluates candidate section moves against hard feasibility.
It does not apply timetable moves or student registration changes.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations, product
from time import perf_counter
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

try:
    from ortools.sat.python import cp_model
except Exception:  # pragma: no cover - exercised only when optional dependency is absent.
    cp_model = None

try:
    from ortools.graph.python import min_cost_flow
except Exception:  # pragma: no cover - exercised only when optional dependency is absent.
    min_cost_flow = None

from core.models import (
    DeliveryBoard,
    Prerequisite,
    Room,
    SectionPlacement,
    Student,
    StudentTermSection,
    TermSection,
    TimetableRepairApproval,
    TimetableRepairCandidate,
    TimetableRepairCandidateMetric,
    TimetableRepairGlobalPlan,
    TimetableRepairGlobalPlanItem,
    TimetableRepairRejectedCandidate,
    TimetableRepairRun,
    TimetableRepairSnapshot,
    TimetableRepairSolverLog,
    TimetableRepairStudentChange,
    TimetableScenario,
)
from core.services.section_move_optimisation_components import (
    AffectedComponentBuilder,
    CandidateMoveGenerator,
    ExplanationAndAuditEngine,
    HardFeasibilityChecker,
    ImpactScorer,
    ObjectiveManager,
    RepairOptimiser,
    SectionMoveOptimisationComponents,
    StudentProfileCompressor,
)
from core.services.timetable_demand import load_scenario_course_demands
from core.services.timetable_move_outcome import preview_placement_student_outcome_candidates
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_repair_domain import (
    build_current_evaluator_assignment_baseline,
    build_repair_domain_snapshot,
    build_repair_solver_problem_input,
)
from core.services.timetable_repair_eligibility import (
    build_repair_eligibility_context_for_section_ids,
    classify_repair_student_policy,
    eligible_repair_section_ids,
    repair_eligibility_summary,
    repair_section_id_ineligibility_reasons,
)
from core.services.timetable_validation import get_prayer_windows, validate_candidate
from core.services.timetable_workspace import (
    preview_placement_room_candidates,
    preview_placement_slot_candidates,
    validate_placement,
)

DEFAULT_LIMITS = {
    "max_depth": 3,
    "max_students": 300,
    "max_courses": 20,
    "max_sections": 120,
    "max_candidates": 20,
    "max_variables": 20_000,
    "max_conflict_edges": 50_000,
    "max_profile_patterns": 2_000,
    "max_lns_students": 80,
    "max_lns_variables": 8_000,
    "max_lns_iterations": 12,
    "max_solver_seconds": 5,
    "max_total_solver_seconds": 45,
    "max_candidate_workers": 2,
    "max_batch_opportunities": 5,
}

EVALUATOR_BASELINE_SOURCE = "repair_evaluator_baseline"
REPAIR_CACHE_VERSION = "repair-cache-evaluator-baseline-v9"
REPAIR_SOLVER_VERSION = "repair-solver-cpsat-flow-adaptive-lns-v3"
REPAIR_CONSTRAINT_VERSION = "repair-constraints-eligibility-capacity-conflict-v2"
REPAIR_OBJECTIVE_VERSION = "repair-objective-requested-quality-v3"
REPAIR_API_CONTRACT_VERSION = "repair-api-contract-v1"
REPAIR_GLOBAL_PLAN_VERSION = "repair-global-plan-v1"

MOVE_SCOPE_SINGLE_SESSION = "single_session"
MOVE_SCOPE_ALL_SESSIONS = "all_sessions"
MOVE_SCOPE_LECTURES_ONLY = "lectures_only"
MOVE_SCOPE_CHOICES = {
    MOVE_SCOPE_SINGLE_SESSION,
    MOVE_SCOPE_ALL_SESSIONS,
    MOVE_SCOPE_LECTURES_ONLY,
}
MOVE_SCOPE_LABELS = {
    MOVE_SCOPE_SINGLE_SESSION: "Selected session only",
    MOVE_SCOPE_ALL_SESSIONS: "All sessions including lab",
    MOVE_SCOPE_LECTURES_ONLY: "Lectures only",
}
REPAIR_WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]
REPAIR_DAY_INDEX = {day: idx for idx, day in enumerate(REPAIR_WEEKDAYS)}


def _section_move_optimisation_components() -> SectionMoveOptimisationComponents:
    """Return blueprint-shaped component boundaries for the repair pipeline."""

    return SectionMoveOptimisationComponents(
        candidate_move_generator=CandidateMoveGenerator(
            prepare_rows=_prepare_repair_candidate_rows,
        ),
        hard_feasibility_checker=HardFeasibilityChecker(
            check_candidate_section_move=hard_feasibility_rejections,
        ),
        affected_component_builder=AffectedComponentBuilder(
            build_component=build_affected_component,
        ),
        student_profile_compressor=StudentProfileCompressor(
            compression_summary=_student_profile_compression_summary,
        ),
        repair_optimiser=RepairOptimiser(
            evaluate_drafts=_evaluate_repair_candidate_drafts,
        ),
        objective_manager=ObjectiveManager(),
        impact_scorer=ImpactScorer(
            ranking_diagnostics=_repair_candidate_ranking_diagnostics,
        ),
        explanation_and_audit_engine=ExplanationAndAuditEngine(
            persist_candidate_drafts=_persist_repair_candidate_drafts,
        ),
    )


def _repair_mode_policy(mode: str) -> dict[str, Any]:
    """Return the auditable solver policy for a repair mode."""

    mode = _normalise_mode(mode)
    common = {
        "mode": mode,
        "existing_course_policy": "hard_protected_no_loss",
        "tier_1": "existing courses are hard-protected",
        "approval_policy": "approval_allowed_after_no_loss_exact_solution",
        "analysis_only": False,
        "max_solver_seconds_multiplier": 1.0,
    }
    if mode == TimetableRepairRun.MODE_BALANCED:
        return {
            **common,
            "strategy": "balanced_lexicographic_cp_sat",
            "tier_2": "maximize blocked target-course recovery",
            "tier_3": "maximize all requested-course recovery at the target-course optimum",
            "tier_4": "minimize existing section changes at the previous optima",
            "tier_5": "minimize students moved at the previous optima",
            "tier_6": "minimize cascade disruption through the movement and section-change stages",
            "tier_7": "preserve spare capacity as a final timetable-quality preference",
            "stages": [
                {"name": "maximize_blocked_recovery", "sense": "max", "expr": "served"},
                {"name": "maximize_requested_course_recovery", "sense": "max", "expr": "requested"},
                {"name": "minimize_section_changes", "sense": "min", "expr": "changed"},
                {"name": "minimize_moved_students", "sense": "min", "expr": "moved"},
                {"name": "minimize_timetable_quality_penalty", "sense": "min", "expr": "quality"},
            ],
            "mode_summary": "Balanced mode maximizes recovery, then prefers fewer total section changes and better spare-capacity quality.",
            "max_solver_seconds_multiplier": 1.5,
        }
    if mode == TimetableRepairRun.MODE_SIMULATION:
        return {
            **common,
            "strategy": "simulation_lexicographic_cp_sat",
            "tier_2": "maximize blocked target-course recovery",
            "tier_3": "maximize all requested-course recovery at the target-course optimum",
            "tier_4": "minimize existing section changes at the previous optima",
            "tier_5": "minimize students moved at the previous optima",
            "tier_6": "minimize cascade disruption through the movement and section-change stages",
            "tier_7": "preserve spare capacity as a final timetable-quality preference",
            "stages": [
                {"name": "maximize_blocked_recovery", "sense": "max", "expr": "served"},
                {"name": "maximize_requested_course_recovery", "sense": "max", "expr": "requested"},
                {"name": "minimize_section_changes", "sense": "min", "expr": "changed"},
                {"name": "minimize_moved_students", "sense": "min", "expr": "moved"},
                {"name": "minimize_timetable_quality_penalty", "sense": "min", "expr": "quality"},
            ],
            "approval_policy": "analysis_only_not_applicable",
            "analysis_only": True,
            "mode_summary": "Simulation mode evaluates repair options and requested-course recovery without allowing approval or apply.",
            "max_solver_seconds_multiplier": 2.0,
        }
    return {
        **common,
        "strategy": "staged_lexicographic_cp_sat",
        "tier_2": "maximize blocked target-course recovery",
        "tier_3": "maximize all requested-course recovery at the target-course optimum",
        "tier_4": "minimize students moved at the previous optima",
        "tier_5": "minimize existing section changes at the previous optima",
        "tier_6": "minimize cascade disruption through the movement and section-change stages",
        "tier_7": "preserve spare capacity as a final timetable-quality preference",
        "stages": [
            {"name": "maximize_blocked_recovery", "sense": "max", "expr": "served"},
            {"name": "maximize_requested_course_recovery", "sense": "max", "expr": "requested"},
            {"name": "minimize_moved_students", "sense": "min", "expr": "moved"},
            {"name": "minimize_section_changes", "sense": "min", "expr": "changed"},
            {"name": "minimize_timetable_quality_penalty", "sense": "min", "expr": "quality"},
        ],
        "mode_summary": "Conservative mode maximizes recovery while keeping disruption to the smallest group and preserving spare capacity as a final preference.",
    }


class TimetableRepairOperationError(ValueError):
    """Structured error for approval/apply/rollback operations."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "REPAIR_OPERATION_ERROR",
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}


def _normalise_plan_filter(value: object | None) -> str:
    text = str(value or "").replace("\u00a0", " ").strip().upper()
    return "" if text in {"", "ALL"} else text


def _repair_planning_scope(
    placement: SectionPlacement,
    active_plan_filter: object | None,
) -> dict[str, Any]:
    """Return scoped same-board overlap rules for filtered planning panes."""

    active = _normalise_plan_filter(active_plan_filter)
    scope: dict[str, Any] = {
        "active_plan_filter": active or "ALL",
        "filter_applied": False,
        "ignored_overlap_count": 0,
        "ignore_overlap_term_section_ids": [],
        "visible_term_section_count": 0,
    }
    if not active:
        return scope

    try:
        from core.services.timetable_plan_lens import SHARED_OWNER, build_scenario_plan_lens

        lens = build_scenario_plan_lens(placement.board.scenario_id)
    except Exception as exc:
        scope["reason"] = "plan_lens_unavailable"
        scope["error"] = str(exc)
        return scope

    plans = {str(plan or "").strip().upper() for plan in lens.get("plans", [])}
    if active != SHARED_OWNER and active not in plans:
        scope["reason"] = "unknown_plan_filter"
        scope["available_plans"] = sorted(plans)
        return scope

    visible_ids: set[int] = set()
    ignored_ids: set[int] = set()
    sections = lens.get("sections") or {}
    for section_id, row in sections.items():
        try:
            term_section_id = int(section_id)
        except (TypeError, ValueError):
            continue
        owner = str(row.get("owner") or "").strip().upper()
        filter_plans = {
            str(plan or "").strip().upper()
            for plan in (row.get("filter_plans") or [])
            if str(plan or "").strip()
        }
        if active == SHARED_OWNER:
            is_visible = bool(row.get("shared")) or owner == SHARED_OWNER
        else:
            is_visible = active in filter_plans
        if term_section_id == placement.term_section_id:
            is_visible = True
        if is_visible:
            visible_ids.add(term_section_id)
        else:
            ignored_ids.add(term_section_id)

    scope.update(
        {
            "filter_applied": True,
            "ignored_overlap_count": len(ignored_ids),
            "ignore_overlap_term_section_ids": sorted(ignored_ids),
            "visible_term_section_count": len(visible_ids),
        }
    )
    return scope


def analyse_timetable_repair(
    *,
    placement_id: int,
    blocked_student_ids: list[int] | None = None,
    blocked_requests: list[dict[str, Any]] | None = None,
    mode: str = TimetableRepairRun.MODE_CONSERVATIVE,
    move_scope: str = MOVE_SCOPE_SINGLE_SESSION,
    requested_by=None,
    limits: dict[str, int] | None = None,
    active_plan_filter: str | None = None,
) -> dict[str, Any]:
    """Create and complete a read-only repair analysis run."""

    active_limits = _normalise_limits(limits)
    blocked_ids = _normalise_student_ids(blocked_student_ids or [])
    placement = SectionPlacement.objects.select_related(
        "board__scenario",
        "term_section",
    ).get(id=placement_id)
    scenario = placement.board.scenario
    mode = _normalise_mode(mode)
    move_scope = _normalise_move_scope(move_scope)
    planning_scope = _repair_planning_scope(placement, active_plan_filter)
    target_course = placement.term_section.course_key or placement.term_section.course_code
    blocked_demand = build_blocked_demand_snapshot(
        scenario_id=scenario.id,
        target_course=target_course,
        explicit_student_ids=blocked_ids,
        explicit_requests=blocked_requests or [],
        limit=active_limits["max_students"],
    )
    cache_fingerprint = _repair_state_fingerprint(scenario.id, blocked_ids)
    request_payload = {
        "placement_id": placement_id,
        "blocked_student_ids": blocked_ids,
        "blocked_demand": blocked_demand,
        "mode": mode,
        "move_scope": move_scope,
        "active_plan_filter": planning_scope["active_plan_filter"],
        "planning_scope": planning_scope,
        "limits": active_limits,
        "cache": {
            "version": REPAIR_CACHE_VERSION,
            "fingerprint": cache_fingerprint,
        },
    }

    cached_run = _find_cached_repair_run(
        scenario_id=scenario.id,
        placement_id=placement_id,
        target_section_id=placement.term_section_id,
        mode=mode,
        request_payload=request_payload,
    )
    if cached_run is not None:
        detail = repair_run_detail(cached_run.id)
        detail["cache"] = {
            "hit": True,
            "run_id": str(cached_run.id),
            "version": REPAIR_CACHE_VERSION,
            "fingerprint": cache_fingerprint,
        }
        return detail

    run = TimetableRepairRun.objects.create(
        scenario=scenario,
        target_placement=placement,
        target_section=placement.term_section,
        requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        mode=mode,
        status=TimetableRepairRun.STATUS_RUNNING,
        solver_version=REPAIR_SOLVER_VERSION,
        constraint_version=REPAIR_CONSTRAINT_VERSION,
        objective_version=REPAIR_OBJECTIVE_VERSION,
        request_payload=request_payload,
    )
    _log(run, "info", "repair_analysis_started", request_payload)

    try:
        blueprint = _section_move_optimisation_components()
        before_snapshot = build_assignment_snapshot(scenario.id)
        component = blueprint.affected_component_builder.build(
            scenario.id,
            placement.term_section,
            blocked_student_ids=blocked_demand.get("active_student_ids") or blocked_ids,
            limits=active_limits,
        )
        component["blocked_demand"] = blocked_demand

        with transaction.atomic():
            run.before_snapshot = before_snapshot
            run.save(update_fields=["before_snapshot"])
            TimetableRepairSnapshot.objects.bulk_create(
                [
                    TimetableRepairSnapshot(
                        run=run,
                        kind=TimetableRepairSnapshot.KIND_BEFORE,
                        payload_json=before_snapshot,
                    ),
                    TimetableRepairSnapshot(
                        run=run,
                        kind=TimetableRepairSnapshot.KIND_COMPONENT,
                        payload_json=component,
                    ),
                ]
            )

        candidate_payloads = evaluate_repair_candidates(
            run,
            placement,
            component=component,
            limits=active_limits,
            planning_scope=planning_scope,
            move_scope=move_scope,
        )
        summary = build_repair_summary(
            run=run,
            placement=placement,
            before_snapshot=before_snapshot,
            component=component,
            candidate_payloads=candidate_payloads,
        )
        summary["cache"] = {
            "enabled": True,
            "hit": False,
            "version": REPAIR_CACHE_VERSION,
            "fingerprint": cache_fingerprint,
        }
        with transaction.atomic():
            run.status = TimetableRepairRun.STATUS_COMPLETED
            run.completed_at = timezone.now()
            run.summary_json = summary
            run.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "summary_json",
                ]
            )
            TimetableRepairApproval.objects.create(
                run=run,
                requested_by=requested_by
                if getattr(requested_by, "is_authenticated", False)
                else None,
                status=TimetableRepairApproval.STATUS_PENDING,
                notes="Read-only analysis created. Applying changes is disabled in this phase.",
            )
            _log(run, "info", "repair_analysis_completed", summary)
    except Exception as exc:
        run.status = TimetableRepairRun.STATUS_FAILED
        run.completed_at = timezone.now()
        run.error_message = str(exc)
        run.save(update_fields=["status", "completed_at", "error_message"])
        _log(run, "error", "repair_analysis_failed", {"error": str(exc)})
        raise

    detail = repair_run_detail(run.id)
    detail["cache"] = {
        "hit": False,
        "run_id": str(run.id),
        "version": REPAIR_CACHE_VERSION,
        "fingerprint": cache_fingerprint,
    }
    return detail


def simulate_timetable_repair_scope(
    *,
    scenario_id: int,
    program: str = "",
    nominal_term: int | None = None,
    course_keys: list[str] | None = None,
    requested_by=None,
    limits: dict[str, int] | None = None,
    max_placements: int = 8,
) -> dict[str, Any]:
    """Run analysis-only repair scans across a bounded scenario/programme scope."""

    scenario = TimetableScenario.objects.get(id=int(scenario_id))
    active_limits = _normalise_limits(
        {
            "max_depth": 4,
            "max_candidates": 8,
            "max_solver_seconds": 8,
            "max_total_solver_seconds": 90,
            **(limits or {}),
        }
    )
    max_placements = max(1, min(int(max_placements or 8), 25))
    program = str(program or "").strip()
    requested_course_filter = {
        str(course).strip() for course in (course_keys or []) if str(course).strip()
    }

    boards = DeliveryBoard.objects.filter(scenario=scenario)
    if program:
        boards = boards.filter(program=program)
    if nominal_term is not None:
        boards = boards.filter(nominal_term=int(nominal_term))
    board_ids = list(boards.values_list("id", flat=True))

    placements_qs = (
        SectionPlacement.objects.filter(board_id__in=board_ids, is_locked=False)
        .select_related("board", "term_section")
        .order_by("term_section__course_key", "term_section__section", "day", "start_time", "id")
    )
    if requested_course_filter:
        placements_qs = placements_qs.filter(term_section__course_key__in=requested_course_filter)
    placements = list(placements_qs)
    placement_course_keys = {
        str(placement.term_section.course_key or placement.term_section.course_code or "").strip()
        for placement in placements
        if str(
            placement.term_section.course_key or placement.term_section.course_code or ""
        ).strip()
    }
    if requested_course_filter:
        placement_course_keys &= requested_course_filter

    blocked_by_course, unresolved_scope = _actual_unresolved_students_by_course(
        scenario_id=scenario.id,
        course_keys=placement_course_keys,
    )
    placement_targets = _simulation_placement_targets(
        placements=placements,
        blocked_by_course=blocked_by_course,
        max_placements=max_placements,
        max_students=active_limits["max_students"],
    )

    run_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    scan_deadline = perf_counter() + max(1, int(active_limits["max_total_solver_seconds"]))
    for target in placement_targets:
        remaining_seconds = scan_deadline - perf_counter()
        if remaining_seconds <= 0:
            error_rows.append(
                {
                    **target,
                    "error": "global_repair_scan_time_budget_exhausted",
                    "message": "Skipped because the global unresolved-student scan reached its total time budget.",
                }
            )
            continue
        target_limits = {
            **active_limits,
            "max_total_solver_seconds": max(1, int(remaining_seconds)),
        }
        try:
            detail = analyse_timetable_repair(
                placement_id=int(target["placement_id"]),
                blocked_student_ids=list(target["blocked_student_ids"]),
                mode=TimetableRepairRun.MODE_SIMULATION,
                requested_by=requested_by,
                limits=target_limits,
            )
        except Exception as exc:  # Keep scope simulation useful when one target is malformed.
            error_rows.append({**target, "error": str(exc)[:500]})
            continue
        run_rows.append(_simulation_run_row(target, detail))

    opportunity_rows = sorted(
        [row for row in run_rows if row.get("best_candidate")],
        key=_simulation_opportunity_rank_key,
    )
    domain_payload = _simulation_domain_payload(
        scenario_id=scenario.id,
        placements=placements,
        blocked_by_course=blocked_by_course,
        active_limits=active_limits,
    )
    aggregate = _simulation_aggregate_summary(
        run_rows=run_rows,
        error_rows=error_rows,
        placement_targets=placement_targets,
        blocked_by_course=blocked_by_course,
        placement_course_keys=placement_course_keys,
    )
    batch_plan = _simulation_batch_plan(
        opportunity_rows=opportunity_rows,
        max_opportunities=active_limits["max_batch_opportunities"],
    )
    return {
        "simulation_version": "repair-scope-simulation-v1",
        "api_contract": _simulation_api_contract(),
        "analysis_only": True,
        "scenario": {
            "id": scenario.id,
            "academic_year": scenario.academic_year,
            "term": scenario.term,
            "name": scenario.name,
            "status": scenario.status,
        },
        "scope": {
            "program": program,
            "nominal_term": nominal_term,
            "course_keys": sorted(requested_course_filter),
            "board_count": len(board_ids),
            "placement_count": len(placements),
            "selected_target_count": len(placement_targets),
            "max_placements": max_placements,
            "limits": active_limits,
        },
        "governance": _simulation_governance_payload(
            max_placements=max_placements,
            limits=active_limits,
        ),
        "domain": domain_payload,
        "aggregate": aggregate,
        "batch_plan": batch_plan,
        "demand": {
            "source": "current_assignment_unresolved",
            "actual_unresolved_student_count": int(
                unresolved_scope.get("actual_unresolved_student_count") or 0
            ),
            "actual_unresolved_request_count": int(
                unresolved_scope.get("actual_unresolved_request_count") or 0
            ),
            "evaluation": unresolved_scope,
            "course_count": len(placement_course_keys),
            "active_blocked_course_count": sum(
                1 for students in blocked_by_course.values() if students
            ),
            "total_unserved_requests": sum(
                len(students) for students in blocked_by_course.values()
            ),
            "total_unresolved_requests": sum(
                len(students) for students in blocked_by_course.values()
            ),
            "courses": [
                {
                    "course_key": course,
                    "unserved_student_count": len(blocked_by_course.get(course, [])),
                    "unresolved_student_count": len(blocked_by_course.get(course, [])),
                    "selected_for_scan": any(
                        row["course_key"] == course for row in placement_targets
                    ),
                }
                for course in sorted(placement_course_keys)
            ],
        },
        "selected_targets": placement_targets,
        "runs": run_rows,
        "errors": error_rows,
        "best_opportunities": opportunity_rows[:10],
        "notes": [
            "Scope simulation creates analysis-only repair runs in simulation mode.",
            "No candidate from this scan can be approved or applied directly.",
            "Targets are bounded by actual unresolved students from the current assignment evaluator and max_placements.",
            "Aggregate recovery is a planning signal, not a guaranteed combined apply plan.",
            "Batch plan selects non-overlapping zero-harm opportunities for a safer programme-level review queue.",
        ],
    }


def create_global_repair_plan(
    *,
    scenario_id: int,
    program: str = "",
    nominal_term: int | None = None,
    course_keys: list[str] | None = None,
    mode: str = TimetableRepairRun.MODE_CONSERVATIVE,
    requested_by=None,
    limits: dict[str, int] | None = None,
    max_placements: int = 8,
    notes: str = "",
) -> dict[str, Any]:
    """Create a durable programme/level repair plan from fresh applyable runs."""

    scenario = TimetableScenario.objects.get(id=int(scenario_id))
    mode = _normalise_mode(mode)
    if mode == TimetableRepairRun.MODE_SIMULATION:
        mode = TimetableRepairRun.MODE_CONSERVATIVE
    active_limits = _normalise_limits(limits)
    max_placements = max(1, min(int(max_placements or 8), 25))
    course_filter = [str(course).strip() for course in (course_keys or []) if str(course).strip()]
    program = str(program or "").strip()

    request_payload = {
        "version": REPAIR_GLOBAL_PLAN_VERSION,
        "scenario_id": scenario.id,
        "program": program,
        "nominal_term": int(nominal_term) if nominal_term not in {None, ""} else None,
        "course_keys": course_filter,
        "mode": mode,
        "limits": active_limits,
        "max_placements": max_placements,
        "objective": "minimize_unresolved_students",
    }
    request_signature = _json_payload_signature(request_payload)

    simulation = simulate_timetable_repair_scope(
        scenario_id=scenario.id,
        program=program,
        nominal_term=request_payload["nominal_term"],
        course_keys=course_filter,
        requested_by=requested_by,
        limits=active_limits,
        max_placements=max_placements,
    )
    selected = list((simulation.get("batch_plan") or {}).get("selected") or [])

    plan = TimetableRepairGlobalPlan.objects.create(
        scenario=scenario,
        requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        status=TimetableRepairGlobalPlan.STATUS_DRAFT,
        scope_program=program,
        scope_nominal_term=request_payload["nominal_term"],
        mode=mode,
        request_signature=request_signature,
        request_payload=request_payload,
        simulation_json=simulation,
        summary_json={
            "version": REPAIR_GLOBAL_PLAN_VERSION,
            "status": "building",
            "primary_objective": "minimize_unresolved_students",
        },
        notes=notes,
    )

    ready_items: list[TimetableRepairGlobalPlanItem] = []
    skipped_rows: list[dict[str, Any]] = []
    for sequence, opportunity in enumerate(selected, start=1):
        try:
            item = _create_global_plan_item_from_opportunity(
                plan=plan,
                opportunity=opportunity,
                sequence=sequence,
                mode=mode,
                requested_by=requested_by,
                limits=active_limits,
                existing_items=ready_items,
            )
        except TimetableRepairOperationError as exc:
            skipped_rows.append(
                {
                    "sequence": sequence,
                    "course_key": opportunity.get("course_key") or "",
                    "placement_id": opportunity.get("placement_id"),
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            )
            continue
        if item.status == TimetableRepairGlobalPlanItem.STATUS_READY:
            ready_items.append(item)
        else:
            skipped_rows.append(
                {
                    "sequence": sequence,
                    "course_key": item.course_key,
                    "placement_id": item.placement_id,
                    "code": item.status.upper(),
                    "message": item.notes,
                    "details": item.metrics_json,
                }
            )

    plan.status = (
        TimetableRepairGlobalPlan.STATUS_DRAFT
        if ready_items
        else TimetableRepairGlobalPlan.STATUS_EMPTY
    )
    plan.summary_json = _global_plan_summary(
        plan=plan,
        simulation=simulation,
        skipped_rows=skipped_rows,
    )
    plan.save(update_fields=["status", "summary_json"])
    return global_repair_plan_detail(plan.id)


def global_repair_plan_detail(plan_id: UUID | str) -> dict[str, Any]:
    plan = (
        TimetableRepairGlobalPlan.objects.select_related("scenario", "requested_by", "decided_by")
        .prefetch_related("items__repair_run", "items__candidate", "items__placement")
        .get(id=plan_id)
    )
    return serialize_global_repair_plan(plan, include_items=True)


def approve_global_repair_plan(
    plan_id: UUID | str,
    *,
    decided_by=None,
    notes: str = "",
) -> dict[str, Any]:
    """Approve every ready candidate in a global repair plan."""

    with transaction.atomic():
        plan = (
            TimetableRepairGlobalPlan.objects.select_for_update()
            .select_related("scenario")
            .get(id=plan_id)
        )
        _ensure_global_plan_mutable(plan)
        if plan.status not in {
            TimetableRepairGlobalPlan.STATUS_DRAFT,
            TimetableRepairGlobalPlan.STATUS_APPROVED,
        }:
            raise TimetableRepairOperationError(
                "Global repair plan is not in a draft state",
                code="REPAIR_GLOBAL_PLAN_NOT_DRAFT",
                status=409,
                details={"status": plan.status},
            )
        items = list(
            plan.items.select_for_update()
            .filter(
                status__in=[
                    TimetableRepairGlobalPlanItem.STATUS_READY,
                    TimetableRepairGlobalPlanItem.STATUS_APPROVED,
                ]
            )
            .select_related("repair_run", "candidate")
            .order_by("sequence", "id")
        )
        if not items:
            raise TimetableRepairOperationError(
                "Global repair plan has no ready repair items",
                code="REPAIR_GLOBAL_PLAN_EMPTY",
                status=409,
            )
        for item in items:
            if item.status != TimetableRepairGlobalPlanItem.STATUS_APPROVED:
                approve_repair_candidate(
                    item.repair_run_id,
                    item.candidate.candidate_id,
                    decided_by=decided_by,
                    notes=notes or f"Approved by global repair plan {plan.id}.",
                )
                item.status = TimetableRepairGlobalPlanItem.STATUS_APPROVED
                item.notes = "Approved through global repair plan."
                item.save(update_fields=["status", "notes"])
        plan.status = TimetableRepairGlobalPlan.STATUS_APPROVED
        plan.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
        plan.decided_at = timezone.now()
        plan.notes = notes or plan.notes
        plan.summary_json = _global_plan_summary(plan=plan)
        plan.save(update_fields=["status", "decided_by", "decided_at", "notes", "summary_json"])
    return global_repair_plan_detail(plan_id)


def apply_global_repair_plan(
    plan_id: UUID | str,
    *,
    decided_by=None,
) -> dict[str, Any]:
    """Apply a previously approved global repair plan as one guarded batch."""

    try:
        with transaction.atomic():
            plan = (
                TimetableRepairGlobalPlan.objects.select_for_update()
                .select_related("scenario")
                .get(id=plan_id)
            )
            _ensure_global_plan_mutable(plan)
            if plan.status != TimetableRepairGlobalPlan.STATUS_APPROVED:
                raise TimetableRepairOperationError(
                    "Global repair plan must be approved before apply",
                    code="REPAIR_GLOBAL_PLAN_NOT_APPROVED",
                    status=409,
                    details={"status": plan.status},
                )
            items = list(
                plan.items.select_for_update()
                .filter(status=TimetableRepairGlobalPlanItem.STATUS_APPROVED)
                .select_related("repair_run", "candidate")
                .order_by("sequence", "id")
            )
            if not items:
                raise TimetableRepairOperationError(
                    "Global repair plan has no approved repair items",
                    code="REPAIR_GLOBAL_PLAN_EMPTY",
                    status=409,
                )
            for item in items:
                apply_approved_repair_candidate(
                    item.repair_run_id,
                    item.candidate.candidate_id,
                    decided_by=decided_by,
                )
                item.status = TimetableRepairGlobalPlanItem.STATUS_APPLIED
                item.notes = "Applied through global repair plan."
                item.save(update_fields=["status", "notes"])
            plan.status = TimetableRepairGlobalPlan.STATUS_APPLIED
            plan.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
            plan.applied_at = timezone.now()
            plan.summary_json = _global_plan_summary(plan=plan)
            plan.save(update_fields=["status", "decided_by", "applied_at", "summary_json"])
    except TimetableRepairOperationError:
        _mark_global_plan_failed(plan_id)
        raise
    return global_repair_plan_detail(plan_id)


def rollback_global_repair_plan(
    plan_id: UUID | str,
    *,
    decided_by=None,
) -> dict[str, Any]:
    """Rollback applied global-plan items in reverse sequence order."""

    try:
        with transaction.atomic():
            plan = (
                TimetableRepairGlobalPlan.objects.select_for_update()
                .select_related("scenario")
                .get(id=plan_id)
            )
            _ensure_global_plan_mutable(plan)
            if plan.status != TimetableRepairGlobalPlan.STATUS_APPLIED:
                raise TimetableRepairOperationError(
                    "Global repair plan is not applied",
                    code="REPAIR_GLOBAL_PLAN_NOT_APPLIED",
                    status=409,
                    details={"status": plan.status},
                )
            items = list(
                plan.items.select_for_update()
                .filter(status=TimetableRepairGlobalPlanItem.STATUS_APPLIED)
                .select_related("repair_run")
                .order_by("-sequence", "-id")
            )
            if not items:
                raise TimetableRepairOperationError(
                    "Global repair plan has no applied repair items",
                    code="REPAIR_GLOBAL_PLAN_EMPTY",
                    status=409,
                )
            for item in items:
                rollback_repair_run(item.repair_run_id, decided_by=decided_by)
                item.status = TimetableRepairGlobalPlanItem.STATUS_ROLLED_BACK
                item.notes = "Rolled back through global repair plan."
                item.save(update_fields=["status", "notes"])
            plan.status = TimetableRepairGlobalPlan.STATUS_ROLLED_BACK
            plan.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
            plan.rolled_back_at = timezone.now()
            plan.summary_json = _global_plan_summary(plan=plan)
            plan.save(update_fields=["status", "decided_by", "rolled_back_at", "summary_json"])
    except TimetableRepairOperationError:
        _mark_global_plan_failed(plan_id)
        raise
    return global_repair_plan_detail(plan_id)


def _create_global_plan_item_from_opportunity(
    *,
    plan: TimetableRepairGlobalPlan,
    opportunity: dict[str, Any],
    sequence: int,
    mode: str,
    requested_by,
    limits: dict[str, int],
    existing_items: list[TimetableRepairGlobalPlanItem],
) -> TimetableRepairGlobalPlanItem:
    next_action = opportunity.get("next_action") or {}
    payload = next_action.get("targeted_analysis_payload") or {}
    placement_id = int(payload.get("placement_id") or opportunity.get("placement_id") or 0)
    if placement_id <= 0:
        raise TimetableRepairOperationError(
            "Global plan opportunity is missing placement_id",
            code="REPAIR_GLOBAL_PLAN_INVALID_OPPORTUNITY",
            status=409,
            details=opportunity,
        )
    blocked_student_ids = (
        payload.get("blocked_student_ids") or opportunity.get("blocked_student_ids") or []
    )
    detail = analyse_timetable_repair(
        placement_id=placement_id,
        blocked_student_ids=blocked_student_ids,
        mode=mode,
        requested_by=requested_by,
        limits=limits,
    )
    candidate_payload = _global_plan_best_candidate_payload(detail)
    if candidate_payload is None:
        raise TimetableRepairOperationError(
            "Fresh targeted analysis did not produce an applyable recovery candidate",
            code="REPAIR_GLOBAL_PLAN_NO_APPLYABLE_CANDIDATE",
            status=409,
            details={"run_id": (detail.get("run") or {}).get("id"), "placement_id": placement_id},
        )

    run_id = (detail.get("run") or {}).get("id")
    candidate = TimetableRepairCandidate.objects.select_related(
        "run",
        "run__target_section",
        "run__target_placement",
    ).get(
        run_id=run_id,
        candidate_id=candidate_payload["candidate_id"],
    )
    metrics = _global_plan_candidate_metrics(candidate_payload)
    impact = _global_plan_candidate_impact(candidate)
    conflict = _global_plan_destination_conflict(
        candidate, [item.candidate for item in existing_items]
    )
    status = TimetableRepairGlobalPlanItem.STATUS_READY
    notes = "Ready for coordinated approval and apply."
    if conflict:
        status = TimetableRepairGlobalPlanItem.STATUS_SKIPPED
        notes = conflict["message"]
        metrics = {**metrics, "skip_reason": conflict}

    return TimetableRepairGlobalPlanItem.objects.create(
        plan=plan,
        sequence=sequence,
        repair_run=candidate.run,
        candidate=candidate,
        placement=candidate.run.target_placement,
        course_key=str(getattr(candidate.run.target_section, "course_key", "") or ""),
        status=status,
        metrics_json=metrics,
        impact_json=impact,
        notes=notes,
    )


def _global_plan_best_candidate_payload(detail: dict[str, Any]) -> dict[str, Any] | None:
    candidates = list(detail.get("candidates") or [])
    applyable: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = candidate.get("decision") or {}
        preflight = candidate.get("preflight") or {}
        metrics = (candidate.get("metrics") or {}).get("exact_repair") or {}
        if not decision.get("approve_allowed"):
            continue
        if not preflight.get("approve_ready"):
            continue
        if int(metrics.get("existing_lost") or 0) > 0:
            continue
        if int(metrics.get("blocked_recovered") or 0) <= 0:
            continue
        applyable.append(candidate)
    if not applyable:
        return None
    applyable.sort(key=_global_plan_candidate_rank_key)
    return applyable[0]


def _global_plan_candidate_rank_key(candidate: dict[str, Any]) -> tuple:
    exact = (candidate.get("metrics") or {}).get("exact_repair") or {}
    return (
        int(exact.get("existing_lost") or 0),
        int(exact.get("unresolved_blocked") or 0),
        -int(exact.get("blocked_recovered") or 0),
        -int(exact.get("requested_courses_recovered") or 0),
        int(exact.get("students_moved") or 0),
        int(exact.get("section_changes") or 0),
        int((exact.get("timetable_quality") or {}).get("penalty") or 0),
        int(candidate.get("score_rank") or 9999),
        str(candidate.get("candidate_id") or ""),
    )


def _global_plan_candidate_metrics(candidate_payload: dict[str, Any]) -> dict[str, Any]:
    exact = (candidate_payload.get("metrics") or {}).get("exact_repair") or {}
    return {
        "primary_objective": "minimize_unresolved_students",
        "candidate_id": candidate_payload.get("candidate_id", ""),
        "blocked_recovered": int(exact.get("blocked_recovered") or 0),
        "unresolved_blocked": int(exact.get("unresolved_blocked") or 0),
        "requested_courses_recovered": int(exact.get("requested_courses_recovered") or 0),
        "unresolved_requested_courses": int(exact.get("unresolved_requested_courses") or 0),
        "existing_lost": int(exact.get("existing_lost") or 0),
        "students_moved": int(exact.get("students_moved") or 0),
        "section_changes": int(exact.get("section_changes") or 0),
        "quality_penalty": int((exact.get("timetable_quality") or {}).get("penalty") or 0),
        "solver_status": exact.get("solver_status", ""),
        "solver_strategy": exact.get("solver_strategy", ""),
    }


def _global_plan_candidate_impact(candidate: TimetableRepairCandidate) -> dict[str, Any]:
    target_course = str(
        getattr(candidate.run.target_section, "course_key", "")
        or getattr(candidate.run.target_section, "course_code", "")
        or ""
    )
    changes = list(candidate.student_changes.order_by("student_id", "course_key", "id"))
    affected_student_ids = sorted(
        {
            int(change.student_id)
            for change in changes
            if change.change_type
            in {
                TimetableRepairStudentChange.CHANGE_MOVED,
                TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
                TimetableRepairStudentChange.CHANGE_LOST,
            }
        }
    )
    target_recovered_student_ids = sorted(
        {
            int(change.student_id)
            for change in changes
            if change.change_type == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED
            and str(change.course_key or "") == target_course
        }
    )
    section_ids: set[int] = set()
    for change in changes:
        for value in (change.before_section_id, change.after_section_id):
            parsed = _parse_section_id(value)
            if parsed:
                section_ids.add(parsed)
    if candidate.run.target_section_id:
        section_ids.add(int(candidate.run.target_section_id))
    return {
        "target_course": target_course,
        "affected_student_ids": affected_student_ids,
        "affected_student_count": len(affected_student_ids),
        "target_recovered_student_ids": target_recovered_student_ids,
        "target_recovered_student_count": len(target_recovered_student_ids),
        "section_ids": sorted(section_ids),
        "course_keys": sorted(
            {str(change.course_key or "") for change in changes if change.course_key}
        ),
        "change_count": len(changes),
    }


def _global_plan_destination_conflict(
    candidate: TimetableRepairCandidate,
    selected_candidates: list[TimetableRepairCandidate],
) -> dict[str, Any] | None:
    candidate_slot = _candidate_destination_slot(candidate)
    if not candidate_slot["room"]:
        return None
    for other in selected_candidates:
        other_slot = _candidate_destination_slot(other)
        if not other_slot["room"]:
            continue
        if (
            candidate_slot["room"] != other_slot["room"]
            or candidate_slot["day"] != other_slot["day"]
        ):
            continue
        if _intervals_overlap(
            candidate_slot["start_minutes"],
            candidate_slot["end_minutes"],
            other_slot["start_minutes"],
            other_slot["end_minutes"],
        ):
            return {
                "code": "DESTINATION_ROOM_OVERLAP",
                "message": "Candidate destination overlaps another selected global-plan room/time.",
                "candidate_id": candidate.candidate_id,
                "other_candidate_id": other.candidate_id,
                "room": candidate_slot["room"],
                "day": candidate_slot["day"],
            }
    return None


def _candidate_destination_slot(candidate: TimetableRepairCandidate) -> dict[str, Any]:
    return {
        "day": str(candidate.day or ""),
        "room": str(candidate.room or ""),
        "start_minutes": _parse_time_minutes(candidate.start_time),
        "end_minutes": _parse_time_minutes(candidate.end_time),
    }


def _parse_time_minutes(value: str) -> int:
    try:
        hour, minute = str(value or "").split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 0


def _intervals_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _global_plan_summary(
    *,
    plan: TimetableRepairGlobalPlan,
    simulation: dict[str, Any] | None = None,
    skipped_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    simulation_payload = simulation or plan.simulation_json or {}
    simulation_demand = simulation_payload.get("demand") or {}
    items = list(plan.items.all().order_by("sequence", "id"))
    status_counts = Counter(item.status for item in items)
    ready_statuses = {
        TimetableRepairGlobalPlanItem.STATUS_READY,
        TimetableRepairGlobalPlanItem.STATUS_APPROVED,
        TimetableRepairGlobalPlanItem.STATUS_APPLIED,
    }
    active_items = [item for item in items if item.status in ready_statuses]
    metrics = [item.metrics_json or {} for item in active_items]
    recovered_student_ids = sorted(
        {
            int(student_id)
            for item in active_items
            for student_id in (item.impact_json or {}).get("target_recovered_student_ids", [])
        }
    )
    skipped = (
        skipped_rows
        if skipped_rows is not None
        else [
            {
                "sequence": item.sequence,
                "course_key": item.course_key,
                "placement_id": item.placement_id,
                "code": "SKIPPED",
                "message": item.notes,
                "details": item.metrics_json,
            }
            for item in items
            if item.status == TimetableRepairGlobalPlanItem.STATUS_SKIPPED
        ]
    )
    scenario_unresolved_students = int(
        simulation_demand.get("actual_unresolved_student_count") or 0
    )
    scenario_unresolved_requests = int(
        simulation_demand.get("actual_unresolved_request_count") or 0
    )
    scenario_unresolved_after_plan = (
        max(0, scenario_unresolved_students - len(recovered_student_ids))
        if scenario_unresolved_students
        else 0
    )
    return {
        "version": REPAIR_GLOBAL_PLAN_VERSION,
        "status": plan.status,
        "primary_objective": "minimize_unresolved_students",
        "objective_order": [
            "protect_existing_registrations",
            "minimize_unresolved_students",
            "maximize_recovered_blocked_students",
            "maximize_requested_course_recovery",
            "minimize_student_movement",
        ],
        "item_count": len(items),
        "active_item_count": len(active_items),
        "status_counts": dict(sorted(status_counts.items())),
        "estimated_totals": {
            "scenario_unresolved_students_before": scenario_unresolved_students,
            "scenario_unresolved_students_after_plan": scenario_unresolved_after_plan,
            "scenario_unresolved_requests_before": scenario_unresolved_requests,
            "distinct_target_students_recovered": len(recovered_student_ids),
            "blocked_recovered": sum(int(row.get("blocked_recovered") or 0) for row in metrics),
            "unresolved_blocked": sum(int(row.get("unresolved_blocked") or 0) for row in metrics),
            "requested_courses_recovered": sum(
                int(row.get("requested_courses_recovered") or 0) for row in metrics
            ),
            "unresolved_requested_courses": sum(
                int(row.get("unresolved_requested_courses") or 0) for row in metrics
            ),
            "existing_lost": sum(int(row.get("existing_lost") or 0) for row in metrics),
            "students_moved": sum(int(row.get("students_moved") or 0) for row in metrics),
            "section_changes": sum(int(row.get("section_changes") or 0) for row in metrics),
        },
        "recovered_student_ids": recovered_student_ids[:250],
        "recovered_student_ids_truncated": len(recovered_student_ids) > 250,
        "simulation": {
            "selected_count": (simulation_payload.get("batch_plan") or {}).get("selected_count", 0),
            "scanned_run_count": (simulation_payload.get("aggregate") or {}).get(
                "scanned_run_count", 0
            ),
            "analysis_only_source": True,
        },
        "scenario_unresolved": {
            "source": simulation_demand.get("source") or "current_assignment_unresolved",
            "student_count": scenario_unresolved_students,
            "request_count": scenario_unresolved_requests,
            "reason_counts": (simulation_demand.get("evaluation") or {}).get("reason_counts") or {},
            "hotspot_courses": (simulation_demand.get("evaluation") or {}).get("hotspot_courses")
            or [],
            "capacity_pressure_courses": (
                (simulation_demand.get("evaluation") or {}).get("capacity_pressure_courses") or []
            ),
        },
        "skipped": skipped[:25],
        "skipped_truncated": len(skipped) > 25,
        "governance": {
            "approval_required": True,
            "apply_requires_approved_plan": True,
            "apply_uses_existing_candidate_preflight": True,
            "rollback_available_after_apply": plan.status
            == TimetableRepairGlobalPlan.STATUS_APPLIED,
            "cross_board_is_not_primary_objective": True,
        },
    }


def serialize_global_repair_plan(
    plan: TimetableRepairGlobalPlan,
    *,
    include_items: bool = False,
) -> dict[str, Any]:
    payload = {
        "global_plan_version": REPAIR_GLOBAL_PLAN_VERSION,
        "api_contract": _global_plan_api_contract(plan),
        "plan": {
            "id": str(plan.id),
            "scenario_id": plan.scenario_id,
            "scenario_name": plan.scenario.name if plan.scenario_id else "",
            "status": plan.status,
            "mode": plan.mode,
            "scope_program": plan.scope_program,
            "scope_nominal_term": plan.scope_nominal_term,
            "requested_by": getattr(plan.requested_by, "username", "")
            if plan.requested_by_id
            else "",
            "decided_by": getattr(plan.decided_by, "username", "") if plan.decided_by_id else "",
            "created_at": plan.created_at.isoformat() if plan.created_at else "",
            "decided_at": plan.decided_at.isoformat() if plan.decided_at else "",
            "applied_at": plan.applied_at.isoformat() if plan.applied_at else "",
            "rolled_back_at": plan.rolled_back_at.isoformat() if plan.rolled_back_at else "",
            "request_signature": plan.request_signature,
            "request_payload": plan.request_payload,
            "notes": plan.notes,
        },
        "summary": plan.summary_json or {},
    }
    if include_items:
        payload["items"] = [
            serialize_global_repair_plan_item(item)
            for item in plan.items.select_related("repair_run", "candidate", "placement").order_by(
                "sequence", "id"
            )
        ]
    return payload


def serialize_global_repair_plan_item(item: TimetableRepairGlobalPlanItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "sequence": item.sequence,
        "status": item.status,
        "run_id": str(item.repair_run_id),
        "candidate_id": item.candidate.candidate_id,
        "placement_id": item.placement_id,
        "course_key": item.course_key,
        "metrics": item.metrics_json,
        "impact": item.impact_json,
        "notes": item.notes,
        "links": {
            "run_detail": f"/ops/tw/repair/runs/{item.repair_run_id}/",
            "run_report": f"/ops/tw/repair/runs/{item.repair_run_id}/report/",
            "candidate_detail": f"/ops/tw/repair/runs/{item.repair_run_id}/candidates/{item.candidate.candidate_id}/",
        },
    }


def _global_plan_api_contract(plan: TimetableRepairGlobalPlan | None = None) -> dict[str, Any]:
    plan_id = str(getattr(plan, "id", "") or "")
    return {
        "version": REPAIR_GLOBAL_PLAN_VERSION,
        "student_identifier_policy": "student_ids_only",
        "primary_objective": "minimize_unresolved_students",
        "endpoint_templates": {
            "create": "/ops/tw/repair/global-plans/",
            "detail": "/ops/tw/repair/global-plans/{plan_id}/",
            "approve": "/ops/tw/repair/global-plans/{plan_id}/approve/",
            "apply": "/ops/tw/repair/global-plans/{plan_id}/apply/",
            "rollback": "/ops/tw/repair/global-plans/{plan_id}/rollback/",
        },
        "endpoints": {
            "detail": f"/ops/tw/repair/global-plans/{plan_id}/" if plan_id else "",
            "approve": f"/ops/tw/repair/global-plans/{plan_id}/approve/" if plan_id else "",
            "apply": f"/ops/tw/repair/global-plans/{plan_id}/apply/" if plan_id else "",
            "rollback": f"/ops/tw/repair/global-plans/{plan_id}/rollback/" if plan_id else "",
        },
        "governance": {
            "simulation_source_is_analysis_only": True,
            "plan_items_are_fresh_applyable_repair_runs": True,
            "approval_required_before_apply": True,
        },
    }


def _ensure_global_plan_mutable(plan: TimetableRepairGlobalPlan) -> None:
    if plan.scenario.status == "published":
        raise TimetableRepairOperationError(
            "Cannot modify a published scenario",
            code="SCENARIO_PUBLISHED",
            status=400,
        )


def _mark_global_plan_failed(plan_id: UUID | str) -> None:
    TimetableRepairGlobalPlan.objects.filter(id=plan_id).update(
        status=TimetableRepairGlobalPlan.STATUS_FAILED,
    )


def _json_payload_signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _simulation_governance_payload(
    *,
    max_placements: int,
    limits: dict[str, int],
) -> dict[str, Any]:
    return {
        "mode": TimetableRepairRun.MODE_SIMULATION,
        "analysis_only": True,
        "apply_allowed": False,
        "approval_allowed": False,
        "target_selection_policy": (
            "rank_actual_unresolved_students_by_course_then_course_section"
        ),
        "bounded_scan": {
            "max_placements": int(max_placements),
            "max_students_per_target": int(limits.get("max_students") or 0),
            "max_candidates_per_target": int(limits.get("max_candidates") or 0),
            "max_batch_opportunities": int(limits.get("max_batch_opportunities") or 0),
            "max_total_solver_seconds": int(limits.get("max_total_solver_seconds") or 0),
        },
        "operator_action": (
            "Use the best opportunity as evidence, then run a targeted repair analysis before approval/apply."
        ),
    }


def _simulation_api_contract() -> dict[str, Any]:
    return {
        "version": "repair-simulation-api-contract-v1",
        "student_identifier_policy": "student_ids_only",
        "mutation_policy": "simulation_is_read_only_and_never_applies_changes",
        "batch_plan_policy": "selected_opportunities_are_review_queue_items_not_direct_apply_actions",
        "endpoint_templates": {
            "simulate": "/ops/tw/repair/simulate/",
            "job_submit": "/ops/tw/repair/jobs/",
            "job_result": "/ops/tw/repair/jobs/{job_id}/result/",
            "targeted_analysis": "/ops/tw/repair/analyse/",
            "run_detail": "/ops/tw/repair/runs/{run_id}/",
            "run_report": "/ops/tw/repair/runs/{run_id}/report/",
            "candidate_detail": "/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/",
        },
    }


def _simulation_domain_payload(
    *,
    scenario_id: int,
    placements: list[SectionPlacement],
    blocked_by_course: dict[str, list[int]],
    active_limits: dict[str, int],
) -> dict[str, Any]:
    section_ids = {int(placement.term_section_id) for placement in placements}
    course_keys = {
        str(placement.term_section.course_key or placement.term_section.course_code or "").strip()
        for placement in placements
        if str(
            placement.term_section.course_key or placement.term_section.course_code or ""
        ).strip()
    }
    student_ids = {
        int(student_id)
        for student_ids_for_course in blocked_by_course.values()
        for student_id in student_ids_for_course
    }
    max_items = max(25, int(active_limits.get("max_students") or DEFAULT_LIMITS["max_students"]))
    payload = build_repair_domain_snapshot(
        scenario_id,
        student_ids=student_ids,
        course_keys=course_keys,
        section_ids=section_ids,
    ).to_audit_payload(max_index_items=max_items)
    return {
        "version": payload["version"],
        "counts": payload["counts"],
        "indexes": payload["indexes"],
        "index_truncated": payload["index_truncated"],
        "rows_truncated": payload["rows_truncated"],
    }


def _simulation_aggregate_summary(
    *,
    run_rows: list[dict[str, Any]],
    error_rows: list[dict[str, Any]],
    placement_targets: list[dict[str, Any]],
    blocked_by_course: dict[str, list[int]],
    placement_course_keys: set[str],
) -> dict[str, Any]:
    selected_courses = {str(row.get("course_key") or "") for row in placement_targets}
    unresolved_by_selected_courses = sum(
        len(students)
        for course, students in blocked_by_course.items()
        if course in selected_courses
    )
    unresolved_by_unselected_courses = sum(
        len(students)
        for course, students in blocked_by_course.items()
        if course not in selected_courses
    )
    best_metrics = [
        ((row.get("best_candidate") or {}).get("metrics") or {})
        for row in run_rows
        if row.get("best_candidate")
    ]
    return {
        "scanned_run_count": len(run_rows),
        "errored_target_count": len(error_rows),
        "selected_target_count": len(placement_targets),
        "selected_course_count": len(selected_courses),
        "placement_course_count": len(placement_course_keys),
        "unserved_requests_in_selected_courses": unresolved_by_selected_courses,
        "unserved_requests_outside_selected_targets": unresolved_by_unselected_courses,
        "unresolved_requests_in_selected_courses": unresolved_by_selected_courses,
        "unresolved_requests_outside_selected_targets": unresolved_by_unselected_courses,
        "blocked_recovered_best_sum": sum(
            int(metrics.get("blocked_recovered") or 0) for metrics in best_metrics
        ),
        "existing_lost_best_sum": sum(
            int(metrics.get("existing_lost") or 0) for metrics in best_metrics
        ),
        "students_moved_best_sum": sum(
            int(metrics.get("students_moved") or 0) for metrics in best_metrics
        ),
        "section_changes_best_sum": sum(
            int(metrics.get("section_changes") or 0) for metrics in best_metrics
        ),
        "zero_harm_opportunity_count": sum(
            1
            for metrics in best_metrics
            if int(metrics.get("blocked_recovered") or 0) > 0
            and int(metrics.get("existing_lost") or 0) == 0
        ),
        "coverage_notes": [
            "Counts are summed per target and can double-count students across overlapping opportunities.",
            "Selected targets are bounded by max_placements and the total scan time budget; unselected unresolved demand remains for later scans.",
        ],
    }


def _simulation_batch_plan(
    *,
    opportunity_rows: list[dict[str, Any]],
    max_opportunities: int,
) -> dict[str, Any]:
    """Select a conservative non-overlapping set of simulation opportunities."""

    skipped: list[dict[str, Any]] = []
    max_opportunities = max(1, int(max_opportunities or DEFAULT_LIMITS["max_batch_opportunities"]))
    eligible_rows: list[dict[str, Any]] = []
    for index, row in enumerate(opportunity_rows):
        best = row.get("best_candidate") or {}
        metrics = best.get("metrics") or {}
        impact = best.get("impact") or {}
        if int(metrics.get("blocked_recovered") or 0) <= 0:
            skipped.append(
                _simulation_batch_skip(
                    row, "NO_RECOVERY", "Best opportunity does not recover blocked students."
                )
            )
            continue
        if int(metrics.get("existing_lost") or 0) > 0:
            skipped.append(
                _simulation_batch_skip(
                    row,
                    "EXISTING_LOSS",
                    "Best opportunity would lose at least one existing registration.",
                    details={"existing_lost": int(metrics.get("existing_lost") or 0)},
                )
            )
            continue
        eligible_rows.append(
            {
                "index": index,
                "row": row,
                "metrics": metrics,
                "affected_student_ids": {
                    int(sid) for sid in impact.get("affected_student_ids") or []
                },
                "section_ids": {int(section_id) for section_id in impact.get("section_ids") or []},
                "course_keys": {str(row.get("course_key") or "")}
                if row.get("course_key")
                else set(),
            }
        )

    selection_result = _simulation_batch_exact_selection(
        eligible_rows=eligible_rows,
        max_opportunities=max_opportunities,
    )
    if selection_result is None:
        selection_result = _simulation_batch_greedy_selection(
            eligible_rows=eligible_rows,
            max_opportunities=max_opportunities,
        )

    selected_indices = set(selection_result["selected_indices"])
    selected_rows = [
        item["row"] for item in eligible_rows if int(item["index"]) in selected_indices
    ]
    selected = [_simulation_batch_selection(row) for row in selected_rows]
    selected_students = {
        int(sid)
        for item in eligible_rows
        if int(item["index"]) in selected_indices
        for sid in item["affected_student_ids"]
    }
    selected_sections = {
        int(section_id)
        for item in eligible_rows
        if int(item["index"]) in selected_indices
        for section_id in item["section_ids"]
    }
    selected_courses = {
        str(course)
        for item in eligible_rows
        if int(item["index"]) in selected_indices
        for course in item["course_keys"]
    }
    for item in eligible_rows:
        if int(item["index"]) in selected_indices:
            continue
        skipped.append(
            _simulation_batch_nonselection_skip(
                item["row"],
                selected_student_ids=selected_students,
                selected_section_ids=selected_sections,
                selected_course_keys=selected_courses,
                batch_limit_reached=len(selected) >= max_opportunities,
            )
        )

    selected_metrics = [row["metrics"] for row in selected]
    return {
        "version": "repair-simulation-batch-plan-v1",
        "analysis_only": True,
        "selection_policy": selection_result["selection_policy"],
        "optimizer": selection_result["optimizer"],
        "max_opportunities": max_opportunities,
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "selected": selected,
        "skipped": skipped[:25],
        "skipped_truncated": len(skipped) > 25,
        "admin_summary": _simulation_batch_admin_summary(
            selected=selected,
            skipped=skipped,
            optimizer=selection_result["optimizer"],
        ),
        "action_queue": _simulation_batch_action_queue(selected),
        "estimated_totals": {
            "blocked_recovered": sum(
                int(row.get("blocked_recovered") or 0) for row in selected_metrics
            ),
            "requested_courses_recovered": sum(
                int(row.get("requested_courses_recovered") or 0) for row in selected_metrics
            ),
            "existing_lost": sum(int(row.get("existing_lost") or 0) for row in selected_metrics),
            "students_moved": sum(int(row.get("students_moved") or 0) for row in selected_metrics),
            "section_changes": sum(
                int(row.get("section_changes") or 0) for row in selected_metrics
            ),
        },
        "overlap_guards": {
            "student_overlap": True,
            "section_overlap": True,
            "course_overlap": True,
            "zero_harm_only": True,
        },
        "notes": [
            "This is an exact opportunity-level batch plan, not a single combined student-reallocation proof.",
            "Each selected row must still be converted into a fresh targeted analysis before approval/apply.",
        ],
    }


def _simulation_batch_exact_selection(
    *,
    eligible_rows: list[dict[str, Any]],
    max_opportunities: int,
) -> dict[str, Any] | None:
    if cp_model is None:
        return None
    if not eligible_rows:
        return {
            "selected_indices": [],
            "selection_policy": "exact_cp_sat_zero_harm_non_overlapping_students_sections_courses",
            "optimizer": {
                "enabled": True,
                "used": True,
                "solver_status": "not_needed",
                "candidate_count": 0,
                "variable_count": 0,
                "constraint_count": 0,
            },
        }

    model = cp_model.CpModel()
    y = {
        int(item["index"]): model.NewBoolVar(f"batch_select_{int(item['index'])}")
        for item in eligible_rows
    }
    model.Add(sum(y.values()) <= max_opportunities)
    constraint_count = 1
    for attr in ("affected_student_ids", "section_ids", "course_keys"):
        grouped: dict[Any, list[Any]] = defaultdict(list)
        for item in eligible_rows:
            for value in item[attr]:
                grouped[value].append(y[int(item["index"])])
        for variables in grouped.values():
            if len(variables) > 1:
                model.AddAtMostOne(variables)
                constraint_count += 1

    objective_terms = []
    for item in eligible_rows:
        metrics = item["metrics"]
        score = (
            int(metrics.get("blocked_recovered") or 0) * 1_000_000
            + int(metrics.get("requested_courses_recovered") or 0) * 100_000
            - int(metrics.get("students_moved") or 0) * 1_000
            - int(metrics.get("section_changes") or 0) * 100
            - int(metrics.get("quality_penalty") or 0)
            - int(item["index"])
        )
        objective_terms.append(score * y[int(item["index"])])
    model.Maximize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2.0
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 23
    started = perf_counter()
    status = solver.Solve(model)
    runtime_ms = int((perf_counter() - started) * 1000)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None
    selected_indices = [int(index) for index, var in y.items() if solver.BooleanValue(var)]
    return {
        "selected_indices": selected_indices,
        "selection_policy": "exact_cp_sat_zero_harm_non_overlapping_students_sections_courses",
        "optimizer": {
            "enabled": True,
            "used": True,
            "fallback_used": False,
            "solver_status": _cp_sat_status_name(status),
            "candidate_count": len(eligible_rows),
            "variable_count": len(y),
            "constraint_count": constraint_count,
            "objective_value": int(solver.ObjectiveValue()),
            "runtime_ms": runtime_ms,
            "proof_status": "proven_optimal" if status == cp_model.OPTIMAL else "feasible",
        },
    }


def _simulation_batch_greedy_selection(
    *,
    eligible_rows: list[dict[str, Any]],
    max_opportunities: int,
) -> dict[str, Any]:
    selected: list[int] = []
    used_students: set[int] = set()
    used_sections: set[int] = set()
    used_courses: set[str] = set()
    for item in eligible_rows:
        if len(selected) >= max_opportunities:
            break
        if item["affected_student_ids"] & used_students:
            continue
        if item["section_ids"] & used_sections:
            continue
        if item["course_keys"] & used_courses:
            continue
        selected.append(int(item["index"]))
        used_students.update(item["affected_student_ids"])
        used_sections.update(item["section_ids"])
        used_courses.update(item["course_keys"])
    return {
        "selected_indices": selected,
        "selection_policy": "greedy_ranked_zero_harm_non_overlapping_students_sections_courses",
        "optimizer": {
            "enabled": cp_model is not None,
            "used": False,
            "fallback_used": True,
            "solver_status": "solver_unavailable_or_not_feasible",
            "candidate_count": len(eligible_rows),
            "variable_count": 0,
            "constraint_count": 0,
        },
    }


def _simulation_batch_nonselection_skip(
    row: dict[str, Any],
    *,
    selected_student_ids: set[int],
    selected_section_ids: set[int],
    selected_course_keys: set[str],
    batch_limit_reached: bool,
) -> dict[str, Any]:
    best = row.get("best_candidate") or {}
    impact = best.get("impact") or {}
    course = str(row.get("course_key") or "")
    overlap_students = sorted(
        {int(sid) for sid in impact.get("affected_student_ids") or []} & selected_student_ids
    )
    overlap_sections = sorted(
        {int(section_id) for section_id in impact.get("section_ids") or []} & selected_section_ids
    )
    overlap_courses = sorted({course} & selected_course_keys) if course else []
    if overlap_courses:
        return _simulation_batch_skip(
            row,
            "COURSE_OVERLAP",
            "Another selected opportunity already covers this course.",
            details={"course_keys": overlap_courses},
        )
    if overlap_students:
        return _simulation_batch_skip(
            row,
            "STUDENT_OVERLAP",
            "Opportunity touches students already selected in the batch plan.",
            details={"student_ids": overlap_students[:25]},
        )
    if overlap_sections:
        return _simulation_batch_skip(
            row,
            "SECTION_OVERLAP",
            "Opportunity touches sections already selected in the batch plan.",
            details={"term_section_ids": overlap_sections[:25]},
        )
    if batch_limit_reached:
        return _simulation_batch_skip(
            row, "BATCH_LIMIT_REACHED", "Batch opportunity limit reached."
        )
    return _simulation_batch_skip(
        row,
        "OPTIMIZER_PRIORITY",
        "Exact batch optimiser preferred a different non-overlapping opportunity set.",
    )


def _simulation_batch_selection(row: dict[str, Any]) -> dict[str, Any]:
    best = row.get("best_candidate") or {}
    metrics = best.get("metrics") or {}
    impact = best.get("impact") or {}
    run_id = str(row.get("run_id") or "")
    candidate_id = str(best.get("candidate_id") or "")
    placement_id = int(row.get("placement_id") or 0)
    blocked_student_ids = [int(sid) for sid in row.get("blocked_student_ids") or []]
    return {
        "run_id": run_id,
        "placement_id": placement_id,
        "term_section_id": int(row.get("term_section_id") or 0),
        "course_key": str(row.get("course_key") or ""),
        "section": str(row.get("section") or ""),
        "candidate_id": candidate_id,
        "placement": best.get("placement") or {},
        "metrics": metrics,
        "impact": {
            "affected_student_count": len(impact.get("affected_student_ids") or []),
            "target_recovered_student_count": len(impact.get("target_recovered_student_ids") or []),
            "moved_student_count": len(impact.get("moved_student_ids") or []),
            "touched_section_count": len(impact.get("section_ids") or []),
            "touched_course_count": len(impact.get("course_keys") or []),
        },
        "next_action": {
            "kind": "run_fresh_targeted_analysis_before_approval",
            "targeted_analysis_payload": {
                "placement_id": placement_id,
                "blocked_student_ids": blocked_student_ids,
                "mode": TimetableRepairRun.MODE_CONSERVATIVE,
            },
            "review_payload": {
                "run_id": run_id,
                "candidate_id": candidate_id,
            },
        },
        "links": {
            "run_detail": f"/ops/tw/repair/runs/{run_id}/" if run_id else "",
            "run_report": f"/ops/tw/repair/runs/{run_id}/report/" if run_id else "",
            "candidate_detail": (
                f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/"
                if run_id and candidate_id
                else ""
            ),
            "targeted_analysis": "/ops/tw/repair/analyse/",
        },
    }


def _simulation_batch_admin_summary(
    *,
    selected: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    optimizer: dict[str, Any],
) -> dict[str, Any]:
    selected_metrics = [row.get("metrics") or {} for row in selected]
    skip_counts = Counter(str(row.get("code") or "UNKNOWN") for row in skipped)
    return {
        "headline": (
            f"{len(selected)} coordinated repair opportunit"
            f"{'y' if len(selected) == 1 else 'ies'} selected for review"
        ),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "estimated_blocked_recovered": sum(
            int(row.get("blocked_recovered") or 0) for row in selected_metrics
        ),
        "estimated_existing_lost": sum(
            int(row.get("existing_lost") or 0) for row in selected_metrics
        ),
        "skip_reason_counts": dict(sorted(skip_counts.items())),
        "optimizer_status": {
            "used": bool(optimizer.get("used")),
            "solver_status": str(optimizer.get("solver_status") or ""),
            "proof_status": str(optimizer.get("proof_status") or ""),
        },
        "operator_guidance": [
            "Review selected opportunities from top to bottom.",
            "Run a fresh targeted analysis for each selected placement before approval.",
            "Apply remains disabled from simulation output.",
        ],
    }


def _simulation_batch_action_queue(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        next_action = row.get("next_action") or {}
        links = row.get("links") or {}
        queue.append(
            {
                "sequence": index,
                "course_key": row.get("course_key") or "",
                "section": row.get("section") or "",
                "placement_id": row.get("placement_id"),
                "candidate_id": row.get("candidate_id") or "",
                "action": "run_fresh_targeted_analysis",
                "payload": next_action.get("targeted_analysis_payload") or {},
                "links": {
                    "targeted_analysis": links.get("targeted_analysis") or "",
                    "candidate_detail": links.get("candidate_detail") or "",
                    "run_report": links.get("run_report") or "",
                },
            }
        )
    return queue


def _simulation_batch_skip(
    row: dict[str, Any],
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    best = row.get("best_candidate") or {}
    return {
        "run_id": row.get("run_id") or "",
        "placement_id": int(row.get("placement_id") or 0),
        "course_key": str(row.get("course_key") or ""),
        "section": str(row.get("section") or ""),
        "candidate_id": str(best.get("candidate_id") or ""),
        "code": code,
        "message": message,
        "details": details or {},
    }


def _actual_unresolved_students_by_course(
    *,
    scenario_id: int,
    course_keys: set[str],
) -> tuple[dict[str, list[int]], dict[str, Any]]:
    """Return unresolved students per course from the real assignment evaluator.

    The global repair planner is measured by students who remain unresolved
    after the current timetable is solved in memory. Do not use raw demand
    minus persisted ``StudentTermSection`` rows here: those rows can lag behind
    the current timetable solver and can greatly overstate the repair target.
    """

    requested_filter = {str(course).strip() for course in course_keys if str(course).strip()}
    summary: dict[str, Any] = {
        "source": "current_assignment_evaluator",
        "evaluation_available": False,
        "requested_course_filter_count": len(requested_filter),
        "actual_unresolved_student_count": 0,
        "actual_unresolved_request_count": 0,
        "courses": [],
        "reason_counts": {},
        "hotspot_courses": [],
        "capacity_pressure_courses": [],
    }
    if not requested_filter:
        return {}, summary

    try:
        from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
        from core.services.timetable_optimizer_v2 import (
            build_course_rigidity_for_scenario,
            build_section_states_for_scenario,
            build_student_profiles_for_scenario,
        )

        student_profiles = build_student_profiles_for_scenario(scenario_id)
        sections = build_section_states_for_scenario(scenario_id)
        if not student_profiles or not sections:
            summary["notes"] = [
                "No current assignment evaluation was available for this scenario.",
            ]
            return {}, summary

        evaluation = evaluate_generated_timetable_candidate(
            candidate_id="current_global_repair_scope",
            generated_sections=sections,
            student_profiles=student_profiles,
            course_rigidity=build_course_rigidity_for_scenario(scenario_id),
        )
    except Exception as exc:
        summary["error"] = str(exc)[:500]
        summary["notes"] = [
            "Current unresolved-student evaluation failed; global repair target selection was skipped.",
        ]
        return {}, summary

    unresolved_by_course: dict[str, set[int]] = defaultdict(set)
    reason_counts: Counter[str] = Counter()
    course_reason_counts: dict[str, Counter[str]] = defaultdict(Counter)
    unresolved_student_ids: set[int] = set()
    for student_id, state in evaluation.assignment_states.items():
        unresolved_courses = state.unresolved_courses or {}
        if not unresolved_courses:
            continue
        try:
            numeric_student_id = int(student_id)
        except (TypeError, ValueError):
            continue
        matched_any_course = False
        for course_key, reason in unresolved_courses.items():
            course_key = str(course_key or "").strip()
            if not course_key or course_key not in requested_filter:
                continue
            reason_code = str(getattr(reason, "reason", "") or "unknown")
            unresolved_by_course[course_key].add(numeric_student_id)
            reason_counts[reason_code] += 1
            course_reason_counts[course_key][reason_code] += 1
            matched_any_course = True
        if matched_any_course:
            unresolved_student_ids.add(numeric_student_id)

    rows = []
    for course_key in sorted(unresolved_by_course):
        students = sorted(unresolved_by_course[course_key])
        rows.append(
            {
                "course_key": course_key,
                "unresolved_student_count": len(students),
                "reason_counts": dict(sorted(course_reason_counts[course_key].items())),
            }
        )

    summary.update(
        {
            "evaluation_available": True,
            "actual_unresolved_student_count": len(unresolved_student_ids),
            "actual_unresolved_request_count": sum(
                len(students) for students in unresolved_by_course.values()
            ),
            "courses": rows,
            "reason_counts": dict(sorted(reason_counts.items())),
            "hotspot_courses": list(evaluation.hotspot_courses[:10]),
            "capacity_pressure_courses": list(evaluation.capacity_pressure_courses[:10]),
            "lexicographic_score": list(evaluation.lexicographic_score),
        }
    )
    return {course: sorted(students) for course, students in unresolved_by_course.items()}, summary


def _unserved_requested_students_by_course(
    *,
    scenario_id: int,
    course_keys: set[str],
) -> dict[str, list[int]]:
    if not course_keys:
        return {}
    requested: dict[str, set[int]] = defaultdict(set)
    for demand in load_scenario_course_demands(scenario_id, course_keys=course_keys):
        course = str(demand.course_key or "").strip()
        if course:
            requested[course].add(int(demand.student_id))

    served_pairs = set(
        (int(student_id), str(course_key))
        for student_id, course_key in StudentTermSection.objects.filter(
            term_section__scenario_id=scenario_id,
            term_section__course_key__in=course_keys,
        ).values_list("student_id", "term_section__course_key")
    )
    return {
        course: sorted(
            student_id
            for student_id in student_ids
            if (int(student_id), course) not in served_pairs
        )
        for course, student_ids in requested.items()
    }


def _simulation_placement_targets(
    *,
    placements: list[SectionPlacement],
    blocked_by_course: dict[str, list[int]],
    max_placements: int,
    max_students: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for placement in placements:
        course = str(
            placement.term_section.course_key or placement.term_section.course_code or ""
        ).strip()
        blocked_students = list(blocked_by_course.get(course) or [])
        if not blocked_students:
            continue
        rows.append(
            {
                "placement_id": placement.id,
                "term_section_id": placement.term_section_id,
                "course_key": course,
                "course_code": placement.term_section.course_code,
                "section": placement.term_section.section,
                "board_id": placement.board_id,
                "board_label": placement.board.label,
                "program": placement.board.program,
                "nominal_term": placement.board.nominal_term,
                "current": {
                    "day": placement.day,
                    "start_time": placement.start_time,
                    "end_time": placement.end_time,
                    "room": placement.room or "",
                },
                "blocked_student_count": len(blocked_students),
                "blocked_student_ids": blocked_students[:max_students],
                "blocked_student_ids_truncated": len(blocked_students) > max_students,
            }
        )

    def row_key(row):
        return (
            -int(row["blocked_student_count"]),
            str(row["course_key"]),
            str(row["section"]),
            int(row["placement_id"]),
        )

    rows.sort(key=row_key)
    rows_by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_course[str(row["course_key"])].append(row)

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    ordered_courses = sorted(
        rows_by_course,
        key=lambda course: (
            -max(int(row["blocked_student_count"]) for row in rows_by_course[course]),
            course,
        ),
    )
    for course in ordered_courses:
        if len(selected) >= max_placements:
            break
        row = rows_by_course[course][0]
        selected.append({**row, "target_selection_round": "first_unresolved_course_pass"})
        selected_ids.add(int(row["placement_id"]))

    if len(selected) < max_placements:
        for row in rows:
            if len(selected) >= max_placements:
                break
            if int(row["placement_id"]) in selected_ids:
                continue
            selected.append({**row, "target_selection_round": "additional_section_pass"})
            selected_ids.add(int(row["placement_id"]))

    return selected


def _simulation_run_row(target: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    candidates = detail.get("candidates") or []
    best = next((candidate for candidate in candidates if candidate.get("score_rank") == 1), None)
    if best is None and candidates:
        best = candidates[0]
    exact = ((best or {}).get("metrics") or {}).get("exact_repair") or {}
    exact_solved = bool(exact.get("enabled")) and str(exact.get("solver_status") or "") in {
        "optimal",
        "feasible",
    }
    target_blocked_count = int(target.get("blocked_student_count") or 0)
    return {
        "run_id": (detail.get("run") or {}).get("id", ""),
        "cache": detail.get("cache") or {},
        "placement_id": target["placement_id"],
        "term_section_id": target["term_section_id"],
        "course_key": target["course_key"],
        "section": target["section"],
        "blocked_student_count": target["blocked_student_count"],
        "blocked_student_ids": list(target.get("blocked_student_ids") or []),
        "summary": detail.get("summary") or {},
        "best_candidate": {
            "candidate_id": (best or {}).get("candidate_id", ""),
            "status": (best or {}).get("status", ""),
            "solver_status": (best or {}).get("solver_status", ""),
            "score_rank": (best or {}).get("score_rank"),
            "placement": {
                "day": (best or {}).get("day", ""),
                "start_time": (best or {}).get("start_time", ""),
                "end_time": (best or {}).get("end_time", ""),
                "room": (best or {}).get("room", ""),
            },
            "metrics": {
                "existing_lost": int(exact.get("existing_lost") or 0) if exact_solved else 0,
                "blocked_recovered": int(exact.get("blocked_recovered") or 0)
                if exact_solved
                else 0,
                "requested_courses_recovered": (
                    int(exact.get("requested_courses_recovered") or 0) if exact_solved else 0
                ),
                "unresolved_blocked": (
                    int(exact.get("unresolved_blocked") or 0)
                    if exact_solved
                    else target_blocked_count
                ),
                "students_moved": int(exact.get("students_moved") or 0) if exact_solved else 0,
                "section_changes": int(exact.get("section_changes") or 0) if exact_solved else 0,
                "quality_penalty": (
                    int((exact.get("timetable_quality") or {}).get("penalty") or 0)
                    if exact_solved
                    else 0
                ),
                "solver_strategy": exact.get("solver_strategy") or "",
                "exact_solved": exact_solved,
            },
            "impact": _simulation_best_candidate_impact(
                target=target,
                best_candidate=best or {},
                detail=detail,
            ),
        }
        if best
        else None,
    }


def _simulation_best_candidate_impact(
    *,
    target: dict[str, Any],
    best_candidate: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = str(best_candidate.get("candidate_id") or "")
    changes = [
        row
        for row in detail.get("student_changes") or []
        if str(row.get("candidate_id") or "") == candidate_id
    ]
    affected_student_ids = sorted(
        {int(row.get("student_id")) for row in changes if row.get("student_id") is not None}
    )
    moved_student_ids = sorted(
        {
            int(row.get("student_id"))
            for row in changes
            if row.get("student_id") is not None
            and row.get("change_type")
            in {
                TimetableRepairStudentChange.CHANGE_MOVED,
                TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
                TimetableRepairStudentChange.CHANGE_LOST,
            }
        }
    )
    target_recovered_student_ids = sorted(
        {
            int(row.get("student_id"))
            for row in changes
            if row.get("student_id") is not None
            and row.get("change_type") == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED
            and str(row.get("course_key") or "") == str(target.get("course_key") or "")
        }
    )
    section_ids = (
        {int(target.get("term_section_id") or 0)} if target.get("term_section_id") else set()
    )
    for row in changes:
        for field in ("before_section_id", "after_section_id"):
            try:
                value = int(str(row.get(field) or "").strip())
            except (TypeError, ValueError):
                continue
            if value:
                section_ids.add(value)
    course_keys = sorted(
        {str(row.get("course_key") or "") for row in changes if row.get("course_key")}
    )
    return {
        "affected_student_ids": affected_student_ids,
        "moved_student_ids": moved_student_ids,
        "target_recovered_student_ids": target_recovered_student_ids,
        "section_ids": sorted(section_ids),
        "course_keys": course_keys,
        "change_count": len(changes),
    }


def _simulation_opportunity_rank_key(row: dict[str, Any]) -> tuple:
    metrics = (row.get("best_candidate") or {}).get("metrics") or {}
    return (
        int(metrics.get("existing_lost") or 0),
        int(metrics.get("unresolved_blocked") or 0),
        -int(metrics.get("blocked_recovered") or 0),
        -int(metrics.get("requested_courses_recovered") or 0),
        int(metrics.get("students_moved") or 0),
        int(metrics.get("section_changes") or 0),
        int(metrics.get("quality_penalty") or 0),
        str(row.get("course_key") or ""),
    )


def repair_run_detail(run_id: UUID | str) -> dict[str, Any]:
    """Return a stable JSON-serialisable repair run detail payload."""

    run = TimetableRepairRun.objects.select_related(
        "scenario",
        "target_placement",
        "target_section",
        "requested_by",
    ).get(id=run_id)
    candidates = list(run.candidates.order_by("score_rank", "created_at", "id"))
    rejected = list(run.rejected_candidates.order_by("created_at", "id"))
    student_changes = list(
        TimetableRepairStudentChange.objects.filter(candidate__run=run)
        .select_related("candidate")
        .order_by("candidate__score_rank", "candidate__candidate_id", "student_id", "course_key")[
            :500
        ]
    )
    approvals = _repair_approval_rows(run)
    audit_logs, audit_logs_truncated = _repair_audit_log_rows(run)
    run_freshness = _repair_run_freshness(run)
    return {
        "api_contract": _repair_api_contract(run),
        "run": serialize_repair_run(run),
        "summary": run.summary_json or {},
        "run_freshness": run_freshness,
        "candidates": [serialize_candidate(candidate, run=run) for candidate in candidates],
        "rejected_candidates": [
            {
                "candidate_key": row.candidate_key,
                "day": row.day,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "room": row.room,
                "reasons": row.reasons_json,
            }
            for row in rejected
        ],
        "snapshots": [
            {
                "kind": snap.kind,
                "created_at": snap.created_at.isoformat() if snap.created_at else "",
                "payload": snap.payload_json,
            }
            for snap in run.snapshots.order_by("created_at", "id")
        ],
        "student_changes": [serialize_student_change(change) for change in student_changes],
        "student_changes_truncated": (
            TimetableRepairStudentChange.objects.filter(candidate__run=run).count()
            > len(student_changes)
        ),
        "approvals": approvals,
        "audit_logs": audit_logs,
        "audit_logs_truncated": audit_logs_truncated,
        "audit_timeline": _repair_audit_timeline(
            approvals=approvals,
            audit_logs=audit_logs,
        ),
        "apply_enabled": _run_has_approved_candidate(run),
        "rollback_preflight": _rollback_readiness(run),
    }


def repair_run_report(run_id: UUID | str, *, candidate_id: str | None = None) -> dict[str, Any]:
    """Return an admin-facing evidence package for a repair run or one candidate."""

    detail = repair_run_detail(run_id)
    run = detail["run"]
    summary = detail.get("summary") or {}
    candidates = detail.get("candidates") or []
    selected_candidate_id = candidate_id or summary.get("best_candidate_id") or ""
    selected_candidate = None
    if selected_candidate_id:
        selected_candidate = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("candidate_id") or "") == str(selected_candidate_id)
            ),
            None,
        )
        if selected_candidate is None:
            raise TimetableRepairOperationError(
                "Repair candidate not found",
                code="NOT_FOUND",
                status=404,
                details={"candidate_id": selected_candidate_id},
            )

    candidate_scope = str(selected_candidate_id or "all")
    changes = [
        _report_student_change(change)
        for change in detail.get("student_changes") or []
        if not selected_candidate_id
        or str(change.get("candidate_id") or "") == str(selected_candidate_id)
    ]
    request_payload = run.get("request_payload") or {}
    return {
        "report_version": "repair-report-v1",
        "api_contract": _repair_api_contract(run, candidate_id=selected_candidate_id or None),
        "generated_at": timezone.now().isoformat(),
        "scope": {
            "run_id": run["id"],
            "candidate_id": candidate_scope,
            "student_identifier_policy": "student_ids_only",
        },
        "run": {
            "id": run["id"],
            "scenario_id": run["scenario_id"],
            "scenario_name": run["scenario_name"],
            "mode": run["mode"],
            "status": run["status"],
            "requested_by": run["requested_by"],
            "requested_at": run["requested_at"],
            "completed_at": run["completed_at"],
            "versions": {
                "solver": run["solver_version"],
                "constraints": run["constraint_version"],
                "objective": run["objective_version"],
                "cache": (summary.get("versions") or {}).get("cache", REPAIR_CACHE_VERSION),
            },
        },
        "target": summary.get("target") or {},
        "run_freshness": detail.get("run_freshness") or {},
        "request": {
            "blocked_student_ids": request_payload.get("blocked_student_ids") or [],
            "limits": request_payload.get("limits") or {},
            "cache": summary.get("cache") or request_payload.get("cache") or {},
        },
        "summary": {
            "candidate_count": summary.get("candidate_count", 0),
            "feasible_candidate_count": summary.get("feasible_candidate_count", 0),
            "rejected_candidate_count": summary.get("rejected_candidate_count", 0),
            "not_solved_candidate_count": summary.get("not_solved_candidate_count", 0),
            "best_candidate_id": summary.get("best_candidate_id", ""),
            "student_solver": summary.get("student_solver") or {},
            "candidate_evaluation": summary.get("candidate_evaluation") or {},
            "assignment_snapshot_available": summary.get("assignment_snapshot_available", False),
            "component_counts": summary.get("component_counts") or {},
            "component_truncated": summary.get("component_truncated", False),
            "blocked_demand": summary.get("blocked_demand")
            or request_payload.get("blocked_demand")
            or {},
        },
        "selected_candidate": _report_candidate(selected_candidate) if selected_candidate else None,
        "candidates": [_report_candidate(candidate) for candidate in candidates],
        "student_changes": changes,
        "student_changes_truncated": detail.get("student_changes_truncated", False),
        "rejected_candidates": detail.get("rejected_candidates") or [],
        "approvals": detail.get("approvals") or [],
        "audit": {
            "timeline": detail.get("audit_timeline") or [],
            "logs": detail.get("audit_logs") or [],
            "logs_truncated": detail.get("audit_logs_truncated", False),
        },
        "snapshot_inventory": _report_snapshot_inventory(detail.get("snapshots") or []),
        "safety": {
            "approval_required": True,
            "apply_enabled": detail.get("apply_enabled", False),
            "rollback_available": bool(
                (summary.get("application") or {}).get("status") == "applied"
            ),
            "rollback_preflight": detail.get("rollback_preflight") or {},
            "run_freshness": detail.get("run_freshness") or {},
            "automatic_apply": False,
        },
    }


def repair_candidate_detail(
    run_id: UUID | str,
    candidate_identifier: int | str,
    *,
    student_change_limit: int = 1000,
) -> dict[str, Any]:
    """Return a direct evidence package for one repair candidate."""

    run = TimetableRepairRun.objects.select_related(
        "scenario",
        "target_placement",
        "target_section",
        "requested_by",
    ).get(id=run_id)
    candidate = _candidate_for_run(run, candidate_identifier)
    candidate_payload = serialize_candidate(candidate, run=run)
    summary = run.summary_json or {}

    limit = max(1, min(int(student_change_limit or 1000), 5000))
    changes_qs = candidate.student_changes.select_related("candidate").order_by(
        "student_id",
        "course_key",
        "id",
    )
    student_changes = list(changes_qs[:limit])
    student_change_total = int(candidate_payload.get("student_change_count") or 0)
    rejected_rows = list(
        TimetableRepairRejectedCandidate.objects.filter(run=run)
        .filter(Q(candidate=candidate) | Q(candidate_key=candidate.candidate_id))
        .order_by("created_at", "id")
    )
    approvals = [
        row
        for row in _repair_approval_rows(run)
        if str(row.get("candidate_id") or "") == candidate.candidate_id
    ]
    audit_logs, audit_logs_truncated = _repair_candidate_audit_log_rows(candidate)
    run_freshness = _repair_run_freshness(run)
    snapshots = [
        {
            "kind": snap.kind,
            "created_at": snap.created_at.isoformat() if snap.created_at else "",
            "payload": snap.payload_json,
        }
        for snap in run.snapshots.order_by("created_at", "id")
    ]

    return {
        "candidate_detail_version": "repair-candidate-detail-v1",
        "api_contract": _repair_api_contract(run, candidate_id=candidate.candidate_id),
        "generated_at": timezone.now().isoformat(),
        "scope": {
            "run_id": str(run.id),
            "candidate_id": candidate.candidate_id,
            "student_identifier_policy": "student_ids_only",
            "student_change_limit": limit,
        },
        "run": serialize_repair_run(run),
        "target": summary.get("target") or {},
        "run_freshness": run_freshness,
        "candidate": candidate_payload,
        "report_candidate": _report_candidate(candidate_payload),
        "student_changes": [serialize_student_change(change) for change in student_changes],
        "student_changes_total": student_change_total,
        "student_changes_truncated": student_change_total > len(student_changes),
        "student_change_type_counts": dict(
            Counter(candidate.student_changes.values_list("change_type", flat=True))
        ),
        "rejected_candidate": [
            {
                "candidate_key": row.candidate_key,
                "day": row.day,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "room": row.room,
                "reasons": row.reasons_json,
            }
            for row in rejected_rows
        ],
        "approvals": approvals,
        "audit": {
            "timeline": _repair_audit_timeline(
                approvals=approvals,
                audit_logs=audit_logs,
            ),
            "logs": audit_logs,
            "logs_truncated": audit_logs_truncated,
        },
        "snapshot_inventory": _report_snapshot_inventory(snapshots),
        "safety": {
            "approval_required": True,
            "automatic_apply": False,
            "apply_enabled": bool((candidate_payload.get("decision") or {}).get("apply_allowed")),
            "decision": candidate_payload.get("decision") or {},
            "preflight": candidate_payload.get("preflight") or {},
            "run_freshness": run_freshness,
        },
    }


def _report_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    existing = candidate.get("admin_summary")
    if isinstance(existing, dict) and str(existing.get("candidate_id") or "") == str(
        candidate.get("candidate_id") or ""
    ):
        return existing
    exact = (candidate.get("metrics") or {}).get("exact_repair") or {}
    return {
        "candidate_id": candidate.get("candidate_id", ""),
        "placement": {
            "day": candidate.get("day", ""),
            "start_time": candidate.get("start_time", ""),
            "end_time": candidate.get("end_time", ""),
            "room": candidate.get("room", ""),
        },
        "status": candidate.get("status", ""),
        "solver_status": candidate.get("solver_status", ""),
        "score_rank": candidate.get("score_rank"),
        "metrics": {
            "blocked_recovered": exact.get("blocked_recovered", 0),
            "requested_courses_recovered": exact.get("requested_courses_recovered", 0),
            "additional_requested_courses_recovered": exact.get(
                "additional_requested_courses_recovered",
                0,
            ),
            "existing_lost": exact.get("existing_lost", 0),
            "students_moved": exact.get("students_moved", 0),
            "section_changes": exact.get("section_changes", 0),
            "unresolved_blocked": exact.get("unresolved_blocked", 0),
            "unresolved_requested_courses": exact.get("unresolved_requested_courses", 0),
            "quality_penalty": (exact.get("timetable_quality") or {}).get("penalty", 0),
            "runtime_ms": exact.get("runtime_ms", 0),
            "solver_strategy": exact.get("solver_strategy", ""),
            "solver_status": exact.get("solver_status", ""),
        },
        "evidence": {
            "rejection_reasons": candidate.get("rejection_reasons") or [],
            "unresolved_diagnostics": exact.get("unresolved_diagnostics") or {},
            "eligibility_policy": exact.get("eligibility_policy") or {},
            "cascade": exact.get("cascade") or {},
            "objective_trace": (exact.get("objective") or {}).get("trace") or [],
            "conflict_policy": exact.get("conflict_policy") or {},
            "profile_compression": exact.get("profile_compression") or {},
            "min_cost_flow": exact.get("min_cost_flow") or {},
            "large_neighbourhood": exact.get("large_neighbourhood") or {},
        },
        "decision": candidate.get("decision") or {},
        "preflight": candidate.get("preflight") or {},
        "ranking": (candidate.get("metrics") or {}).get("ranking") or {},
        "evaluation": (candidate.get("metrics") or {}).get("evaluation") or {},
    }


def _repair_api_contract(
    run: TimetableRepairRun | dict[str, Any],
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    run_id = str(getattr(run, "id", "") or (run.get("id", "") if isinstance(run, dict) else ""))
    candidate_id = str(candidate_id or "").strip()
    endpoints = {
        "run_detail": f"/ops/tw/repair/runs/{run_id}/",
        "run_report": f"/ops/tw/repair/runs/{run_id}/report/",
        "rollback": f"/ops/tw/repair/runs/{run_id}/rollback/",
    }
    if candidate_id:
        endpoints.update(
            {
                "candidate_detail": f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/",
                "candidate_approve": f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/approve/",
                "candidate_apply": f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/",
            }
        )
    return {
        "version": REPAIR_API_CONTRACT_VERSION,
        "student_identifier_policy": "student_ids_only",
        "analysis_mutation_policy": "analysis_is_read_only_until_explicit_apply",
        "candidate_decision_policy": "approve_then_apply_with_current_state_preflight",
        "rollback_policy": "rollback_requires_applied_snapshot_and_current_state_preflight",
        "endpoint_templates": {
            "run_detail": "/ops/tw/repair/runs/{run_id}/",
            "run_report": "/ops/tw/repair/runs/{run_id}/report/",
            "candidate_detail": "/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/",
            "candidate_approve": "/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/approve/",
            "candidate_apply": "/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/",
            "rollback": "/ops/tw/repair/runs/{run_id}/rollback/",
        },
        "endpoints": endpoints,
    }


def _report_student_change(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": change.get("candidate_id", ""),
        "student_id": change.get("student_id"),
        "course_key": change.get("course_key", ""),
        "before_section_id": change.get("before_section_id", ""),
        "after_section_id": change.get("after_section_id", ""),
        "change_type": change.get("change_type", ""),
        "reason": (change.get("details") or {}).get("unresolved_reason") or {},
    }


def _report_snapshot_inventory(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payload = snapshot.get("payload") or {}
        rows.append(
            {
                "kind": snapshot.get("kind", ""),
                "created_at": snapshot.get("created_at", ""),
                "counts": payload.get("counts") or {},
                "truncated": payload.get("truncated", False),
                "has_payload": bool(payload),
            }
        )
    return rows


def _find_cached_repair_run(
    *,
    scenario_id: int,
    placement_id: int,
    target_section_id: int,
    mode: str,
    request_payload: dict[str, Any],
) -> TimetableRepairRun | None:
    """Return a completed equivalent run when the scenario fingerprint is unchanged."""

    disqualifying_approvals = [
        TimetableRepairApproval.STATUS_APPROVED,
        TimetableRepairApproval.STATUS_APPLIED,
        TimetableRepairApproval.STATUS_ROLLED_BACK,
    ]
    candidates = (
        TimetableRepairRun.objects.filter(
            scenario_id=scenario_id,
            target_placement_id=placement_id,
            target_section_id=target_section_id,
            mode=mode,
            status=TimetableRepairRun.STATUS_COMPLETED,
        )
        .exclude(approvals__status__in=disqualifying_approvals)
        .order_by("-completed_at", "-requested_at")[:20]
    )
    for run in candidates:
        payload = run.request_payload or {}
        if payload == request_payload:
            return run
    return None


def _repair_run_freshness(run: TimetableRepairRun) -> dict[str, Any]:
    """Compare a completed analysis with the current scenario state."""

    checked_at = timezone.now().isoformat()
    request_payload = run.request_payload or {}
    requested_cache = request_payload.get("cache") if isinstance(request_payload, dict) else {}
    requested_cache = requested_cache if isinstance(requested_cache, dict) else {}
    analysis_fingerprint = str(requested_cache.get("fingerprint") or "")
    blocked_ids = _normalise_student_ids(request_payload.get("blocked_student_ids") or [])
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[dict[str, Any]] = []

    def add_check(
        name: str, status: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                "details": details or {},
            }
        )

    def add_block(code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        blocking_reasons.append(
            {
                "code": code,
                "message": message,
                "details": details or {},
            }
        )

    if run.status == TimetableRepairRun.STATUS_COMPLETED:
        add_check("analysis_completed", "passed", "Repair analysis completed successfully.")
    else:
        add_check(
            "analysis_completed",
            "failed",
            f"Repair analysis status is {run.status}.",
        )
        add_block(
            "REPAIR_RUN_NOT_COMPLETED",
            "Only completed repair analyses can be trusted for approval.",
            details={"run_status": run.status},
        )

    current_fingerprint = ""
    fingerprint_matches = False
    if not analysis_fingerprint:
        add_check(
            "scenario_fingerprint",
            "failed",
            "No analysis fingerprint was stored for this repair run.",
        )
        add_block(
            "REPAIR_RUN_FINGERPRINT_MISSING",
            "Run a fresh analysis so the recommendation can be tied to a scenario fingerprint.",
        )
    else:
        try:
            current_fingerprint = _repair_state_fingerprint(run.scenario_id, blocked_ids)
            fingerprint_matches = current_fingerprint == analysis_fingerprint
        except Exception as exc:  # pragma: no cover - defensive for broken scenario references.
            add_check(
                "scenario_fingerprint",
                "failed",
                "Current scenario fingerprint could not be calculated.",
                {"error": str(exc)[:200]},
            )
            add_block(
                "REPAIR_RUN_FINGERPRINT_ERROR",
                "Run a fresh analysis; current scenario fingerprint could not be calculated.",
                details={"error": str(exc)[:200]},
            )
        else:
            if fingerprint_matches:
                add_check(
                    "scenario_fingerprint",
                    "passed",
                    "Current timetable, assignments, rooms and eligibility inputs match the analysis snapshot.",
                )
            else:
                add_check(
                    "scenario_fingerprint",
                    "failed",
                    "Current timetable, assignments, rooms or eligibility inputs changed after the analysis.",
                    {
                        "analysis_fingerprint": analysis_fingerprint,
                        "current_fingerprint": current_fingerprint,
                    },
                )
                add_block(
                    "REPAIR_RUN_STALE",
                    "Current scenario state changed after this analysis; run a fresh repair analysis before approval.",
                    details={
                        "analysis_fingerprint": analysis_fingerprint,
                        "current_fingerprint": current_fingerprint,
                    },
                )

    approval_state = _repair_run_latest_approval_state(run)
    if approval_state in {
        TimetableRepairApproval.STATUS_APPLIED,
        TimetableRepairApproval.STATUS_ROLLED_BACK,
    }:
        add_check(
            "repair_lifecycle",
            "informational",
            f"Repair lifecycle is already {approval_state}.",
            {"approval_state": approval_state},
        )
    else:
        add_check(
            "repair_lifecycle",
            "passed",
            "No applied or rolled-back repair blocks this analysis from being reviewed.",
            {"approval_state": approval_state or "none"},
        )

    if approval_state == TimetableRepairApproval.STATUS_APPLIED:
        status = "applied"
        message = "The repair candidate has already been applied; use rollback readiness for the operational state."
    elif approval_state == TimetableRepairApproval.STATUS_ROLLED_BACK:
        status = "rolled_back"
        message = (
            "The applied repair has been rolled back; run a fresh analysis for new recommendations."
        )
    elif blocking_reasons:
        status = (
            "stale"
            if any(row["code"] == "REPAIR_RUN_STALE" for row in blocking_reasons)
            else "blocked"
        )
        message = "Run a fresh repair analysis before trusting or applying this recommendation."
    else:
        status = "fresh"
        message = "This repair run still matches the current scenario inputs."

    recommendation_current = (
        status == "fresh"
        and run.status == TimetableRepairRun.STATUS_COMPLETED
        and fingerprint_matches
    )
    return {
        "status": status,
        "checked_at": checked_at,
        "message": message,
        "recommendation_current": recommendation_current,
        "requires_rerun": status in {"stale", "blocked", "rolled_back"},
        "fingerprint_matches_analysis": fingerprint_matches,
        "analysis_fingerprint": analysis_fingerprint,
        "current_fingerprint": current_fingerprint,
        "cache_version": requested_cache.get("version") or "",
        "approval_state": approval_state or "none",
        "checks": checks,
        "blocking_reasons": blocking_reasons,
    }


def _repair_run_latest_approval_state(run: TimetableRepairRun) -> str:
    row = (
        run.approvals.order_by("-decided_at", "-created_at", "-id")
        .values_list("status", flat=True)
        .first()
    )
    return str(row or "")


def _repair_state_fingerprint(scenario_id: int, blocked_ids: list[int]) -> str:
    """Fingerprint timetable, demand, assignment, room and eligibility inputs."""

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("board")
        .order_by("id")
        .values(
            "id",
            "board_id",
            "board__program",
            "board__nominal_term",
            "term_section_id",
            "day",
            "start_time",
            "end_time",
            "room",
            "is_locked",
        )
    )
    term_sections = list(
        TermSection.objects.filter(scenario_id=scenario_id)
        .order_by("id")
        .values(
            "id",
            "course_key",
            "course_code",
            "course_name",
            "section",
            "available_capacity",
            "registered_count",
            "source_tag",
        )
    )
    assignments = list(
        StudentTermSection.objects.filter(term_section__scenario_id=scenario_id)
        .order_by("student_id", "term_section_id", "id")
        .values("student_id", "term_section_id", "source")
    )
    scenario_requests = [
        {
            "student_id": demand.student_id,
            "course_key": demand.course_key,
            "course_code": demand.course_code,
            "primary_term": demand.primary_term,
            "is_cross_term": demand.is_cross_term,
            "status": demand.status,
            "priority": demand.priority,
            "reason_blocked": demand.reason_blocked,
            "source": demand.source,
        }
        for demand in load_scenario_course_demands(scenario_id)
    ]
    student_ids = {int(row["student_id"]) for row in assignments}
    student_ids.update(int(row["student_id"]) for row in scenario_requests)
    student_ids.update(int(sid) for sid in blocked_ids)
    students = list(
        Student.objects.filter(student_id__in=student_ids)
        .order_by("student_id")
        .values(
            "student_id",
            "program",
            "section",
            "status",
            "total_earned_credits",
            "current_registered_credits",
        )
    )
    programs = sorted(
        {str(row.get("program") or "").strip() for row in students if row.get("program")}
    )
    prerequisites = list(
        Prerequisite.objects.filter(program__in=programs)
        .order_by("program", "course_code", "prerequisite_course_code")
        .values("program", "course_code", "prerequisite_course_code")
    )
    rooms = list(
        Room.objects.all()
        .order_by("room_code", "section")
        .values("room_code", "section", "room_type", "capacity", "department")
    )
    payload = {
        "version": REPAIR_CACHE_VERSION,
        "scenario_id": scenario_id,
        "blocked_ids": sorted(int(sid) for sid in blocked_ids),
        "placements": placements,
        "term_sections": term_sections,
        "assignments": assignments,
        "scenario_requests": scenario_requests,
        "students": students,
        "prerequisites": prerequisites,
        "rooms": rooms,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approve_repair_candidate(
    run_id: UUID | str,
    candidate_identifier: int | str,
    *,
    decided_by=None,
    notes: str = "",
) -> dict[str, Any]:
    """Approve a solved candidate while keeping actual writes separate."""

    with transaction.atomic():
        run = (
            TimetableRepairRun.objects.select_for_update().select_related("scenario").get(id=run_id)
        )
        candidate = _candidate_for_run(run, candidate_identifier, lock=True)
        _ensure_scenario_mutable(run)
        _ensure_candidate_applicable(candidate)

        if (
            TimetableRepairApproval.objects.select_for_update()
            .filter(
                run=run,
                status=TimetableRepairApproval.STATUS_APPLIED,
            )
            .exists()
        ):
            raise TimetableRepairOperationError(
                "This repair run has already been applied",
                code="REPAIR_ALREADY_APPLIED",
                status=409,
            )

        _validate_candidate_write_preconditions(
            run,
            candidate,
            lock_changes=True,
            materialize_evaluator_baseline=False,
        )

        TimetableRepairApproval.objects.select_for_update().filter(
            run=run,
            status=TimetableRepairApproval.STATUS_APPROVED,
        ).exclude(candidate=candidate).update(
            status=TimetableRepairApproval.STATUS_REJECTED,
            decided_by=decided_by if getattr(decided_by, "is_authenticated", False) else None,
            decided_at=timezone.now(),
            notes="Superseded by another approved repair candidate.",
        )
        approval = (
            TimetableRepairApproval.objects.select_for_update()
            .filter(run=run, candidate=candidate)
            .order_by("-created_at", "-id")
            .first()
        )
        if approval is None:
            approval = TimetableRepairApproval.objects.create(
                run=run,
                candidate=candidate,
                requested_by=run.requested_by,
            )
        approval.status = TimetableRepairApproval.STATUS_APPROVED
        approval.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
        approval.decided_at = timezone.now()
        approval.notes = notes or "Candidate approved for controlled repair application."
        approval.save(update_fields=["candidate", "status", "decided_by", "decided_at", "notes"])
        _log(
            run,
            "info",
            "repair_candidate_approved",
            {"candidate_id": candidate.candidate_id},
            candidate=candidate,
        )
    return repair_run_detail(run_id)


def apply_approved_repair_candidate(
    run_id: UUID | str,
    candidate_identifier: int | str,
    *,
    decided_by=None,
) -> dict[str, Any]:
    """Apply one approved repair candidate in a guarded transaction."""

    with transaction.atomic():
        run = (
            TimetableRepairRun.objects.select_for_update()
            .select_related("scenario", "target_placement")
            .get(id=run_id)
        )
        candidate = _candidate_for_run(run, candidate_identifier, lock=True)
        _ensure_scenario_mutable(run)
        _ensure_candidate_applicable(candidate)

        approval = (
            TimetableRepairApproval.objects.select_for_update()
            .filter(
                run=run,
                candidate=candidate,
                status=TimetableRepairApproval.STATUS_APPROVED,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if approval is None:
            raise TimetableRepairOperationError(
                "Candidate must be approved before it can be applied",
                code="REPAIR_NOT_APPROVED",
                status=409,
            )

        placement, changes = _validate_candidate_write_preconditions(
            run,
            candidate,
            lock_changes=True,
            materialize_evaluator_baseline=True,
        )
        source_tag = _repair_source_tag(run, candidate)

        placements_by_id = _placements_for_candidate_moves(candidate, lock=True)
        moves = _candidate_move_set(candidate, placement=placement)
        old_placements: list[dict[str, Any]] = []
        new_placements: list[dict[str, Any]] = []
        for move in moves:
            scoped_placement = placements_by_id.get(int(move.get("placement_id") or 0))
            if scoped_placement is None:
                continue
            old_placements.append(
                {
                    "placement_id": scoped_placement.id,
                    "day": scoped_placement.day,
                    "start_time": scoped_placement.start_time,
                    "end_time": scoped_placement.end_time,
                    "room": scoped_placement.room or "",
                }
            )
            scoped_placement.day = str(move.get("day") or "")
            scoped_placement.start_time = str(move.get("start") or "")
            scoped_placement.end_time = str(move.get("end") or "")
            scoped_placement.room = str(move.get("room") or "")
            scoped_placement.save(
                update_fields=["day", "start_time", "end_time", "room", "updated_at"]
            )
            new_placements.append(
                {
                    "placement_id": scoped_placement.id,
                    "day": scoped_placement.day,
                    "start_time": scoped_placement.start_time,
                    "end_time": scoped_placement.end_time,
                    "room": scoped_placement.room or "",
                }
            )

        applied_counts = _apply_student_changes(run, candidate, changes, source_tag)
        after_snapshot = build_assignment_snapshot(run.scenario_id)
        TimetableRepairSnapshot.objects.create(
            run=run,
            kind=TimetableRepairSnapshot.KIND_AFTER,
            payload_json={
                "stage": "applied",
                "candidate_id": candidate.candidate_id,
                "snapshot": after_snapshot,
            },
        )

        approval.status = TimetableRepairApproval.STATUS_APPLIED
        approval.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
        approval.decided_at = timezone.now()
        approval.notes = "Repair candidate applied."
        approval.save(update_fields=["status", "decided_by", "decided_at", "notes"])

        summary = dict(run.summary_json or {})
        summary["application"] = {
            "status": "applied",
            "candidate_id": candidate.candidate_id,
            "applied_at": timezone.now().isoformat(),
            "placement": {
                "from": old_placements[0] if old_placements else {},
                "to": new_placements[0] if new_placements else {},
            },
            "placements": {
                "from": old_placements,
                "to": new_placements,
                "move_scope": _candidate_move_scope_payload(candidate),
            },
            "student_changes": applied_counts,
            "rollback_available": True,
        }
        run.summary_json = summary
        run.save(update_fields=["summary_json"])
        _log(
            run,
            "info",
            "repair_candidate_applied",
            summary["application"],
            candidate=candidate,
        )
    return repair_run_detail(run_id)


def rollback_repair_run(
    run_id: UUID | str,
    *,
    decided_by=None,
) -> dict[str, Any]:
    """Rollback the applied candidate for a repair run."""

    with transaction.atomic():
        run = (
            TimetableRepairRun.objects.select_for_update()
            .select_related("scenario", "target_placement")
            .get(id=run_id)
        )
        _ensure_scenario_mutable(run)
        approval = (
            TimetableRepairApproval.objects.select_for_update()
            .filter(run=run, status=TimetableRepairApproval.STATUS_APPLIED)
            .select_related("candidate")
            .order_by("-decided_at", "-created_at", "-id")
            .first()
        )
        if approval is None or approval.candidate is None:
            raise TimetableRepairOperationError(
                "No applied repair candidate is available to rollback",
                code="REPAIR_NOT_APPLIED",
                status=409,
            )
        candidate = approval.candidate
        placements_by_id = _placements_for_candidate_moves(candidate, lock=True)
        for move in _candidate_move_set(candidate):
            scoped_placement = placements_by_id.get(int(move.get("placement_id") or 0))
            if scoped_placement is not None:
                _assert_placement_matches_candidate_move(move, scoped_placement)

        source_tag = _repair_source_tag(run, candidate)
        changes = list(candidate.student_changes.select_for_update().order_by("-id"))
        rolled_back_counts = _rollback_student_changes(run, candidate, changes, source_tag)

        restored_placements: list[dict[str, Any]] = []
        for scoped_placement in placements_by_id.values():
            before_placement = _before_placement_row(run, scoped_placement.id)
            scoped_placement.day = before_placement["day"]
            scoped_placement.start_time = before_placement["start_time"]
            scoped_placement.end_time = before_placement["end_time"]
            scoped_placement.room = before_placement.get("room", "") or ""
            scoped_placement.save(
                update_fields=["day", "start_time", "end_time", "room", "updated_at"]
            )
            restored_placements.append(
                {
                    "placement_id": scoped_placement.id,
                    "day": scoped_placement.day,
                    "start_time": scoped_placement.start_time,
                    "end_time": scoped_placement.end_time,
                    "room": scoped_placement.room or "",
                }
            )

        after_snapshot = build_assignment_snapshot(run.scenario_id)
        TimetableRepairSnapshot.objects.create(
            run=run,
            kind=TimetableRepairSnapshot.KIND_AFTER,
            payload_json={
                "stage": "rolled_back",
                "candidate_id": candidate.candidate_id,
                "snapshot": after_snapshot,
            },
        )

        approval.status = TimetableRepairApproval.STATUS_ROLLED_BACK
        approval.decided_by = decided_by if getattr(decided_by, "is_authenticated", False) else None
        approval.decided_at = timezone.now()
        approval.notes = "Repair candidate rolled back."
        approval.save(update_fields=["status", "decided_by", "decided_at", "notes"])

        summary = dict(run.summary_json or {})
        summary["rollback"] = {
            "status": "rolled_back",
            "candidate_id": candidate.candidate_id,
            "rolled_back_at": timezone.now().isoformat(),
            "student_changes": rolled_back_counts,
            "placement_restored": restored_placements[0] if restored_placements else {},
            "placements_restored": restored_placements,
        }
        run.summary_json = summary
        run.save(update_fields=["summary_json"])
        _log(
            run,
            "info",
            "repair_candidate_rolled_back",
            summary["rollback"],
            candidate=candidate,
        )
    return repair_run_detail(run_id)


def build_assignment_snapshot(scenario_id: int) -> dict[str, Any]:
    """Capture exact current assignments and placement state for audit."""

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("board", "term_section")
        .order_by("board__display_order", "term_section__course_key", "term_section__section")
    )
    assignments = list(
        StudentTermSection.objects.filter(term_section__scenario_id=scenario_id)
        .select_related("term_section")
        .order_by("student_id", "term_section__course_key", "term_section__section")
    )
    return {
        "scenario_id": scenario_id,
        "captured_at": timezone.now().isoformat(),
        "placement_count": len(placements),
        "assignment_count": len(assignments),
        "exact_assignment_source_available": bool(assignments),
        "placements": [
            {
                "placement_id": placement.id,
                "board_id": placement.board_id,
                "board_label": placement.board.label,
                "term_section_id": placement.term_section_id,
                "course_key": placement.term_section.course_key,
                "course_code": placement.term_section.course_code,
                "section": placement.term_section.section,
                "day": placement.day,
                "start_time": placement.start_time,
                "end_time": placement.end_time,
                "room": placement.room or "",
                "is_locked": placement.is_locked,
            }
            for placement in placements
        ],
        "assignments": [
            {
                "student_id": assignment.student_id,
                "term_section_id": assignment.term_section_id,
                "course_key": assignment.term_section.course_key,
                "course_code": assignment.term_section.course_code,
                "section": assignment.term_section.section,
                "source": assignment.source,
            }
            for assignment in assignments
        ],
    }


def build_blocked_demand_snapshot(
    *,
    scenario_id: int,
    target_course: str,
    explicit_student_ids: list[int] | None = None,
    explicit_requests: list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build an auditable target-course recovery demand set.

    Explicit rows win when the caller is analysing a specific blocked list.
    Otherwise the canonical scenario course-request table supplies demand.
    """

    target_course = str(target_course or "").strip()
    max_rows = max(1, int(limit or DEFAULT_LIMITS["max_students"]))
    explicit_ids = _normalise_student_ids(explicit_student_ids or [])
    raw_requests = explicit_requests or []
    request_rows: dict[tuple[int, str], dict[str, Any]] = {}
    ignored_rows: list[dict[str, Any]] = []

    def add_request(
        *,
        student_id: int,
        course_key: str,
        source: str,
        reason: str = "",
        priority: str = "",
        status: str = "blocked",
        raw: dict[str, Any] | None = None,
    ) -> None:
        course_key = str(course_key or target_course).strip()
        if not student_id or not course_key:
            return
        row = {
            "student_id": int(student_id),
            "course_key": course_key,
            "status": str(status or "blocked"),
            "priority": str(priority or "normal"),
            "reason": str(reason or ""),
            "source": source,
        }
        if course_key != target_course:
            ignored_rows.append({**row, "ignored_reason": "non_target_course"})
            return
        key = (int(student_id), course_key)
        if key in request_rows and source == "explicit_student_id":
            return
        request_rows[key] = {**row, **({"raw": raw} if raw else {})}

    for raw in raw_requests:
        if not isinstance(raw, dict):
            continue
        try:
            student_id = int(raw.get("student_id"))
        except (TypeError, ValueError):
            continue
        add_request(
            student_id=student_id,
            course_key=str(raw.get("course_key") or raw.get("course_code") or target_course),
            source=str(raw.get("source") or "explicit_request"),
            reason=str(raw.get("reason") or raw.get("reason_blocked") or "manual_request"),
            priority=str(raw.get("priority") or "normal"),
            status=str(raw.get("status") or "blocked"),
            raw={
                key: raw.get(key)
                for key in ["request_id", "status", "priority", "reason", "reason_blocked"]
                if key in raw
            },
        )

    for student_id in explicit_ids:
        add_request(
            student_id=int(student_id),
            course_key=target_course,
            source="explicit_student_id",
            reason="manual_blocked_student_list",
            priority="normal",
        )

    inferred_ids: list[int] = []
    if not request_rows:
        target_section_ids = set(
            TermSection.objects.filter(
                scenario_id=scenario_id, course_key=target_course
            ).values_list("id", flat=True)
        )
        already_assigned = set(
            StudentTermSection.objects.filter(
                term_section_id__in=target_section_ids,
            ).values_list("student_id", flat=True)
        )
        for student_id in _students_requesting_course(scenario_id, target_course, max_rows):
            if int(student_id) in already_assigned:
                continue
            inferred_ids.append(int(student_id))
            add_request(
                student_id=int(student_id),
                course_key=target_course,
                source="scenario_course_demand",
                reason="scenario_request_rows_target_course",
                priority="normal",
            )
            if len(request_rows) >= max_rows:
                break

    student_ids = [student_id for student_id, _course in request_rows]
    students = {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=student_ids).values(
            "student_id",
            "program",
            "section",
            "status",
            "total_earned_credits",
            "current_registered_credits",
        )
    }
    scenario_demand_student_ids = {
        demand.student_id
        for demand in load_scenario_course_demands(
            scenario_id,
            course_keys=[target_course],
        )
        if demand.student_id in student_ids
    }
    current_courses_by_student: dict[int, set[str]] = defaultdict(set)
    for student_id, course_key in StudentTermSection.objects.filter(
        student_id__in=student_ids,
        term_section__scenario_id=scenario_id,
    ).values_list("student_id", "term_section__course_key"):
        current_courses_by_student[int(student_id)].add(str(course_key))

    rows: list[dict[str, Any]] = []
    source_counts = Counter()
    priority_counts = Counter()
    already_registered_count = 0
    missing_student_count = 0
    for row in sorted(
        request_rows.values(), key=lambda item: (item["student_id"], item["course_key"])
    ):
        student = students.get(int(row["student_id"]), {})
        already_registered = target_course in current_courses_by_student.get(
            int(row["student_id"]), set()
        )
        if already_registered:
            already_registered_count += 1
        if not student:
            missing_student_count += 1
        source_counts[row["source"]] += 1
        priority_counts[row["priority"]] += 1
        priority_group, graduation_priority, protected, protection_reason = (
            classify_repair_student_policy(
                status=student.get("status") or "",
                total_earned_credits=int(student.get("total_earned_credits") or 0),
                current_registered_credits=int(student.get("current_registered_credits") or 0),
            )
        )
        rows.append(
            {
                **row,
                "program": student.get("program") or "",
                "section": student.get("section") or "",
                "priority_group": priority_group,
                "graduation_priority": graduation_priority,
                "protected": protected,
                "protection_reason": protection_reason,
                "in_scenario_demand": int(row["student_id"]) in scenario_demand_student_ids,
                "already_registered_target": already_registered,
                "current_course_count": len(
                    current_courses_by_student.get(int(row["student_id"]), set())
                ),
            }
        )

    active_rows = [row for row in rows if not row["already_registered_target"]]
    return {
        "version": "blocked-demand-v1",
        "target_course_key": target_course,
        "source": "explicit_or_scenario_demand",
        "explicit_student_count": len(explicit_ids),
        "explicit_request_count": len(raw_requests),
        "inferred_request_count": len(inferred_ids),
        "active_request_count": len(active_rows),
        "already_registered_count": already_registered_count,
        "missing_student_count": missing_student_count,
        "ignored_request_count": len(ignored_rows),
        "active_student_ids": [int(row["student_id"]) for row in active_rows],
        "source_counts": dict(source_counts),
        "priority_counts": dict(priority_counts),
        "rows": rows[:max_rows],
        "rows_truncated": len(rows) > max_rows,
        "ignored_requests": ignored_rows[:20],
        "notes": [
            "Explicit blocked requests are preferred when supplied.",
            "Scenario course request rows are used as the recovery target when no explicit blocked requests are supplied.",
            "Students already registered in the target course are retained as evidence but excluded from active recovery demand.",
        ],
    }


def build_affected_component(
    scenario_id: int,
    target_section: TermSection,
    *,
    blocked_student_ids: list[int] | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a bounded student/course/section neighbourhood for repair."""

    active_limits = _normalise_limits(limits)
    target_course = target_section.course_key or target_section.course_code
    max_depth = active_limits["max_depth"]
    max_students = active_limits["max_students"]
    max_courses = active_limits["max_courses"]
    max_sections = active_limits["max_sections"]

    blocked_seed_ids: set[int] = set(blocked_student_ids or [])
    student_ids: set[int] = set(blocked_seed_ids)
    course_keys: set[str] = {target_course}
    section_ids: set[int] = set()
    truncated_reasons: list[str] = []
    depth_trace: list[dict[str, Any]] = []

    target_sections = list(
        TermSection.objects.filter(scenario_id=scenario_id, course_key=target_course)
        .order_by("section")
        .values_list("id", flat=True)
    )
    section_ids.update(target_sections)
    target_course_student_ids = set(
        StudentTermSection.objects.filter(term_section_id__in=target_sections).values_list(
            "student_id",
            flat=True,
        )
    )
    student_ids.update(target_course_student_ids)

    inferred = _students_requesting_course(scenario_id, target_course, max_students)
    demand_by_student: dict[int, list[str]] = defaultdict(list)
    for demand in load_scenario_course_demands(scenario_id):
        demand_by_student[int(demand.student_id)].append(str(demand.course_key))
    inferred_requester_ids: set[int] = set()
    for sid in inferred:
        if len(student_ids) >= max_students:
            truncated_reasons.append("max_students")
            break
        student_ids.add(sid)
        inferred_requester_ids.add(sid)

    frontier = deque((sid, 0) for sid in sorted(student_ids))
    seen_students = set(student_ids)
    while frontier:
        sid, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        before_courses = set(course_keys)
        before_sections = set(section_ids)
        before_students = set(seen_students)
        assigned = list(
            StudentTermSection.objects.filter(
                student_id=sid,
                term_section__scenario_id=scenario_id,
            )
            .select_related("term_section")
            .values(
                "student_id",
                "term_section_id",
                "term_section__course_key",
            )
        )
        for row in assigned:
            course_keys.add(row["term_section__course_key"])
            section_ids.add(row["term_section_id"])

        for course in demand_by_student.get(int(sid), []):
            if course:
                course_keys.add(str(course))

        if len(course_keys) > max_courses:
            truncated_reasons.append("max_courses")
            course_keys = set(sorted(course_keys)[:max_courses])
        sections_for_courses = list(
            TermSection.objects.filter(
                scenario_id=scenario_id,
                course_key__in=course_keys,
            ).values_list("id", flat=True)
        )
        section_ids.update(sections_for_courses)
        if len(section_ids) > max_sections:
            truncated_reasons.append("max_sections")
            section_ids = set(sorted(section_ids)[:max_sections])

        neighbours = list(
            StudentTermSection.objects.filter(term_section_id__in=section_ids)
            .values_list("student_id", flat=True)
            .distinct()
        )
        for neighbour in neighbours:
            if neighbour in seen_students:
                continue
            if len(seen_students) >= max_students:
                truncated_reasons.append("max_students")
                break
            seen_students.add(neighbour)
            student_ids.add(neighbour)
            frontier.append((neighbour, depth + 1))
        depth_trace.append(
            {
                "depth": depth,
                "expanded_student_id": sid,
                "assigned_courses_seen": len(assigned),
                "courses_added": len(course_keys - before_courses),
                "sections_added": len(section_ids - before_sections),
                "students_added": len(seen_students - before_students),
                "totals": {
                    "students": len(seen_students),
                    "courses": len(course_keys),
                    "sections": len(section_ids),
                },
            }
        )

    student_rows = _student_component_rows(student_ids, scenario_id, target_course)
    sections = list(
        TermSection.objects.filter(id__in=section_ids)
        .order_by("course_key", "section")
        .values("id", "course_key", "course_code", "course_name", "section")
    )
    domain_snapshot = build_repair_domain_snapshot(
        scenario_id,
        student_ids=student_ids,
        course_keys=course_keys,
        section_ids=section_ids,
    ).to_audit_payload(max_index_items=active_limits["max_students"])
    return {
        "scenario_id": scenario_id,
        "target_course_key": target_course,
        "target_section_id": target_section.id,
        "limits": active_limits,
        "truncated": bool(truncated_reasons),
        "truncated_reasons": sorted(set(truncated_reasons)),
        "students": student_rows,
        "courses": sorted(course_keys),
        "sections": list(sections),
        "domain": domain_snapshot,
        "counts": {
            "students": len(student_rows),
            "courses": len(course_keys),
            "sections": len(sections),
            "profiles": _profile_count(student_rows),
            "domain_students": int((domain_snapshot.get("counts") or {}).get("students") or 0),
            "domain_requests": int((domain_snapshot.get("counts") or {}).get("requests") or 0),
            "domain_assignments": int(
                (domain_snapshot.get("counts") or {}).get("assignments") or 0
            ),
        },
        "expansion": {
            "seed_counts": {
                "blocked_students": len(blocked_seed_ids),
                "target_course_sections": len(target_sections),
                "target_course_current_students": len(target_course_student_ids),
                "target_course_requesters": len(inferred_requester_ids),
            },
            "max_depth": max_depth,
            "depth_trace": depth_trace[:100],
            "depth_trace_truncated": len(depth_trace) > 100,
        },
        "locked": _component_locked_summary(scenario_id, section_ids),
        "notes": [
            "Exact current section assignments are taken from StudentTermSection when available.",
            "Scenario course request rows seed requested-course demand when no exact current assignment exists.",
        ],
    }


def evaluate_repair_candidates(
    run: TimetableRepairRun,
    placement: SectionPlacement,
    *,
    component: dict[str, Any],
    limits: dict[str, int],
    planning_scope: dict[str, Any] | None = None,
    move_scope: str = MOVE_SCOPE_SINGLE_SESSION,
) -> list[dict[str, Any]]:
    """Generate, hard-filter, and solve read-only candidate repairs.

    Candidate solving is intentionally kept in memory.  Database rows are
    written once the batch has been ranked so audit persistence is separate
    from solver working state and future candidate workers can stay isolated.
    """

    evaluation_started = perf_counter()
    blueprint = _section_move_optimisation_components()
    max_candidates = limits["max_candidates"]
    preparation_started = perf_counter()
    prepared_rows = blueprint.candidate_move_generator.generate(
        placement,
        limits=limits,
        planning_scope=planning_scope,
        move_scope=move_scope,
    )
    preparation_runtime_ms = int((perf_counter() - preparation_started) * 1000)
    outcome_started = perf_counter()
    outcome_rows = _student_outcome_rows(
        placement.id,
        [row["source_row"] for row in prepared_rows],
    )
    outcome_runtime_ms = int((perf_counter() - outcome_started) * 1000)
    selected_rows = _select_repair_candidate_rows(
        prepared_rows,
        outcome_rows=outcome_rows,
        max_candidates=max_candidates,
    )
    evaluation_budget = _repair_evaluation_budget(
        limits=limits,
        selected_candidate_count=len(selected_rows),
    )
    worker_plan = _repair_candidate_worker_plan(
        limits,
        selected_candidate_count=len(selected_rows),
    )
    drafts = blueprint.repair_optimiser.solve_candidate_drafts(
        run=run,
        placement=placement,
        component=component,
        limits=limits,
        prepared_rows=selected_rows,
        prepared_candidate_count=len(prepared_rows),
        selected_candidate_count=len(selected_rows),
        preparation_runtime_ms=preparation_runtime_ms,
        outcome_runtime_ms=outcome_runtime_ms,
        outcome_rows=outcome_rows,
        evaluation_started=evaluation_started,
        worker_plan=worker_plan,
    )

    rank_by_key = blueprint.objective_manager.rank_by_candidate_id(drafts)
    total_runtime_ms = int((perf_counter() - evaluation_started) * 1000)
    blueprint.impact_scorer.attach_evaluation_metrics(
        drafts,
        rank_by_key=rank_by_key,
        evaluation_budget=evaluation_budget,
        total_runtime_ms=total_runtime_ms,
    )

    persisted_payloads = blueprint.explanation_and_audit_engine.persist_candidates(run, drafts)
    return sorted(
        persisted_payloads,
        key=lambda p: (p.get("score_rank") is None, p.get("score_rank") or 9999),
    )


def _evaluate_repair_candidate_drafts(
    *,
    run: TimetableRepairRun,
    placement: SectionPlacement,
    component: dict[str, Any],
    limits: dict[str, int],
    prepared_rows: list[dict[str, Any]],
    prepared_candidate_count: int,
    selected_candidate_count: int,
    preparation_runtime_ms: int,
    outcome_runtime_ms: int,
    outcome_rows: dict[tuple[str, str], dict[str, Any]],
    evaluation_started: float,
    worker_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    if not worker_plan.get("enabled"):
        return [
            _evaluate_repair_candidate_draft(
                run=run,
                placement=placement,
                component=component,
                limits=limits,
                prepared=prepared,
                candidate_index=idx,
                prepared_candidate_count=prepared_candidate_count,
                selected_candidate_count=selected_candidate_count,
                preparation_runtime_ms=preparation_runtime_ms,
                outcome_runtime_ms=outcome_runtime_ms,
                outcome=outcome_rows.get((prepared["day"], prepared["start"]), {}),
                evaluation_started=evaluation_started,
                worker_plan=worker_plan,
            )
            for idx, prepared in enumerate(prepared_rows, start=1)
        ]

    futures = {}
    drafts: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=int(worker_plan["worker_count"]),
        thread_name_prefix="repair-candidate",
    ) as executor:
        for idx, prepared in enumerate(prepared_rows, start=1):
            futures[
                executor.submit(
                    _evaluate_repair_candidate_draft_worker,
                    run_id=str(run.id),
                    placement_id=int(placement.id),
                    component=component,
                    limits=limits,
                    prepared=prepared,
                    candidate_index=idx,
                    prepared_candidate_count=prepared_candidate_count,
                    selected_candidate_count=selected_candidate_count,
                    preparation_runtime_ms=preparation_runtime_ms,
                    outcome_runtime_ms=outcome_runtime_ms,
                    outcome=outcome_rows.get((prepared["day"], prepared["start"]), {}),
                    evaluation_started=evaluation_started,
                    worker_plan=worker_plan,
                )
            ] = idx
        for future in as_completed(futures):
            drafts.append(future.result())
    return sorted(drafts, key=lambda draft: int(draft.get("_candidate_index") or 0))


def _evaluate_repair_candidate_draft_worker(**kwargs: Any) -> dict[str, Any]:
    close_old_connections()
    try:
        run = TimetableRepairRun.objects.get(id=kwargs.pop("run_id"))
        placement = SectionPlacement.objects.select_related(
            "board__scenario",
            "term_section",
        ).get(id=kwargs.pop("placement_id"))
        return _evaluate_repair_candidate_draft(
            run=run,
            placement=placement,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - one candidate must not poison the whole batch.
        return _candidate_worker_failure_draft(error=exc, **kwargs)
    finally:
        close_old_connections()


def _evaluate_repair_candidate_draft(
    *,
    run: TimetableRepairRun,
    placement: SectionPlacement,
    component: dict[str, Any],
    limits: dict[str, int],
    prepared: dict[str, Any],
    candidate_index: int,
    prepared_candidate_count: int,
    selected_candidate_count: int,
    preparation_runtime_ms: int,
    outcome_runtime_ms: int,
    outcome: dict[str, Any],
    evaluation_started: float,
    worker_plan: dict[str, Any],
) -> dict[str, Any]:
    candidate_started = perf_counter()
    candidate = _initial_repair_candidate_draft(
        prepared=prepared,
        candidate_index=candidate_index,
        prepared_candidate_count=prepared_candidate_count,
        selected_candidate_count=selected_candidate_count,
        preparation_runtime_ms=preparation_runtime_ms,
        outcome_runtime_ms=outcome_runtime_ms,
        outcome=outcome,
        evaluation_started=evaluation_started,
        worker_plan=worker_plan,
    )
    budget_state = (candidate["metrics"]["evaluation"] or {}).get("budget") or {}
    solver_skipped_by_budget = bool(budget_state.get("exhausted")) and not bool(
        candidate.get("rejection_reasons")
    )
    try:
        if solver_skipped_by_budget:
            _mark_candidate_not_solved(
                run,
                candidate,
                status="budget_exhausted",
                reason="Evaluation budget was exhausted before this candidate could be solved",
                started=candidate_started,
                details={"budget": budget_state},
            )
        elif not candidate.get("rejection_reasons"):
            solve_conservative_student_repair(
                run,
                candidate,
                placement,
                component=component,
                limits=limits,
            )
    except Exception as exc:  # noqa: BLE001 - preserve the run and mark the candidate.
        _mark_candidate_not_solved(
            run,
            candidate,
            status="candidate_solver_error",
            reason=f"{type(exc).__name__}: {exc}",
            started=candidate_started,
            details={"error_type": type(exc).__name__},
        )
    _attach_candidate_evaluation_runtime(
        candidate,
        candidate_runtime_ms=int((perf_counter() - candidate_started) * 1000),
    )
    candidate["_rank_key"] = _repair_candidate_rank_key(candidate)
    return candidate


def _initial_repair_candidate_draft(
    *,
    prepared: dict[str, Any],
    candidate_index: int,
    prepared_candidate_count: int,
    selected_candidate_count: int,
    preparation_runtime_ms: int,
    outcome_runtime_ms: int,
    outcome: dict[str, Any],
    evaluation_started: float,
    worker_plan: dict[str, Any],
) -> dict[str, Any]:
    row = prepared["source_row"]
    candidate_key = f"cand_{candidate_index:03d}"
    day = str(prepared["day"])
    start = str(prepared["start"])
    selected_room = str(prepared.get("selected_room") or "")
    rejections = prepared.get("rejections") or []
    budget_state = _repair_evaluation_budget_state(
        evaluation_started,
        limits=worker_plan["limits"],
        selected_candidate_count=selected_candidate_count,
    )
    solver_skipped_by_budget = bool(budget_state["exhausted"]) and not bool(rejections)
    metrics = {
        "quick": {
            "critical_count": row.get("critical_count", 0),
            "warning_count": row.get("warning_count", 0),
            "student_affected_count": row.get("student_affected_count", 0),
            "impact_score": row.get("impact_score", 0),
            "badge": row.get("badge", ""),
        },
        "student_outcome": outcome.get("student_outcome") or {},
        "student_outcome_source": (
            "current_assignability_evaluator" if outcome else "not_available"
        ),
        "room": prepared["room_payload"],
        "move_scope": prepared.get("move_scope_payload") or {},
        "generation": prepared["generation"],
        "evaluation": {
            "candidate_index": candidate_index,
            "candidate_key": candidate_key,
            "source_index": prepared["source_index"],
            "source_candidate_count": prepared["generation"].get("source_candidate_count", 0),
            "prepared_candidate_count": prepared_candidate_count,
            "selected_candidate_count": selected_candidate_count,
            "scan_limit": prepared["generation"].get("scan_limit", 0),
            "preparation_runtime_ms": preparation_runtime_ms,
            "student_outcome_runtime_ms": outcome_runtime_ms,
            "candidate_loop_mode": "in_memory_then_audited_bulk_persist",
            "parallelism": _candidate_parallelism_metrics(worker_plan),
            "budget": budget_state,
            "hard_rejected": bool(rejections),
            "solver_invoked": not bool(rejections) and not solver_skipped_by_budget,
            "solver_skipped_reason": (
                "evaluation_budget_exhausted" if solver_skipped_by_budget else ""
            ),
        },
    }
    status = (
        TimetableRepairCandidate.STATUS_REJECTED
        if rejections
        else TimetableRepairCandidate.STATUS_FEASIBLE
    )
    return {
        "_candidate_index": candidate_index,
        "candidate_id": candidate_key,
        "day": day,
        "start_time": start,
        "end_time": str(prepared["end"]),
        "room": selected_room,
        "status": status,
        "solver_status": "not_run_readonly_phase",
        "score_rank": None,
        "metrics": metrics,
        "explanation": _candidate_explanation(row, selected_room, rejections, outcome),
        "rejection_reasons": rejections,
        "student_changes": [],
        "solver_logs": [],
    }


def _candidate_worker_failure_draft(
    *,
    error: Exception,
    prepared: dict[str, Any],
    candidate_index: int,
    prepared_candidate_count: int,
    selected_candidate_count: int,
    preparation_runtime_ms: int,
    outcome_runtime_ms: int,
    outcome: dict[str, Any],
    evaluation_started: float,
    worker_plan: dict[str, Any],
    **_unused: Any,
) -> dict[str, Any]:
    started = perf_counter()
    candidate = _initial_repair_candidate_draft(
        prepared=prepared,
        candidate_index=candidate_index,
        prepared_candidate_count=prepared_candidate_count,
        selected_candidate_count=selected_candidate_count,
        preparation_runtime_ms=preparation_runtime_ms,
        outcome_runtime_ms=outcome_runtime_ms,
        outcome=outcome,
        evaluation_started=evaluation_started,
        worker_plan=worker_plan,
    )
    _mark_candidate_not_solved(
        None,
        candidate,
        status="candidate_worker_error",
        reason=f"{type(error).__name__}: {error}",
        started=started,
        details={"error_type": type(error).__name__},
    )
    _attach_candidate_evaluation_runtime(
        candidate,
        candidate_runtime_ms=int((perf_counter() - started) * 1000),
    )
    candidate["_rank_key"] = _repair_candidate_rank_key(candidate)
    return candidate


def _persist_repair_candidate_drafts(
    run: TimetableRepairRun,
    drafts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist in-memory candidate evaluation results as audited DB rows."""

    if not drafts:
        return []

    with transaction.atomic():
        TimetableRepairCandidate.objects.bulk_create(
            [
                TimetableRepairCandidate(
                    run=run,
                    candidate_id=str(draft["candidate_id"]),
                    day=str(draft["day"]),
                    start_time=str(draft["start_time"]),
                    end_time=str(draft["end_time"]),
                    room=str(draft.get("room") or ""),
                    status=str(draft["status"]),
                    solver_status=str(draft.get("solver_status") or "not_run")[:32],
                    score_rank=draft.get("score_rank"),
                    metrics_json=draft.get("metrics") or {},
                    explanation_json=draft.get("explanation") or {},
                    rejection_reasons=draft.get("rejection_reasons") or [],
                )
                for draft in drafts
            ]
        )
        candidates = {
            candidate.candidate_id: candidate
            for candidate in TimetableRepairCandidate.objects.filter(run=run)
        }
        metric_objs: list[TimetableRepairCandidateMetric] = []
        for draft in drafts:
            candidate = candidates.get(str(draft["candidate_id"]))
            if candidate is None:
                continue
            metric_objs.extend(
                _candidate_metric_row_objects(
                    candidate,
                    draft.get("metrics") or {},
                )
            )
        TimetableRepairCandidateMetric.objects.bulk_create(metric_objs, batch_size=1000)
        TimetableRepairRejectedCandidate.objects.bulk_create(
            [
                TimetableRepairRejectedCandidate(
                    run=run,
                    candidate=candidates.get(str(draft["candidate_id"])),
                    candidate_key=str(draft["candidate_id"]),
                    day=str(draft["day"]),
                    start_time=str(draft["start_time"]),
                    end_time=str(draft["end_time"]),
                    room=str(draft.get("room") or ""),
                    reasons_json=draft.get("rejection_reasons") or [],
                )
                for draft in drafts
                if draft.get("rejection_reasons")
            ]
        )
        change_objs: list[TimetableRepairStudentChange] = []
        for draft in drafts:
            candidate = candidates.get(str(draft["candidate_id"]))
            if candidate is None:
                continue
            for change in draft.get("student_changes") or []:
                change_objs.append(
                    TimetableRepairStudentChange(
                        candidate=candidate,
                        student_id=int(change["student_id"]),
                        course_key=str(change.get("course_key") or ""),
                        before_section_id=str(change.get("before_section_id") or ""),
                        after_section_id=str(change.get("after_section_id") or ""),
                        change_type=str(change.get("change_type") or ""),
                        details_json=change.get("details_json") or {},
                    )
                )
        TimetableRepairStudentChange.objects.bulk_create(change_objs, batch_size=1000)
        TimetableRepairSolverLog.objects.bulk_create(
            [
                TimetableRepairSolverLog(
                    run=run,
                    candidate=candidates.get(str(draft["candidate_id"])),
                    level=str(log.get("level") or "info")[:16],
                    message=str(log.get("message") or ""),
                    payload_json=log.get("payload") or {},
                )
                for draft in drafts
                for log in draft.get("solver_logs") or []
            ]
        )

    persisted = [
        serialize_candidate(candidates[str(draft["candidate_id"])], run=run)
        for draft in drafts
        if str(draft["candidate_id"]) in candidates
    ]
    return sorted(
        persisted,
        key=lambda p: (p.get("score_rank") is None, p.get("score_rank") or 9999),
    )


def _candidate_metric_row_objects(
    candidate: TimetableRepairCandidate,
    metrics: dict[str, Any],
    *,
    max_rows: int = 256,
) -> list[TimetableRepairCandidateMetric]:
    """Convert candidate metric JSON into bounded normalized scalar rows."""
    rows: list[TimetableRepairCandidateMetric] = []
    seen_keys: set[str] = set()
    for metric_key, value in _iter_candidate_metric_values(metrics):
        if len(rows) >= max_rows:
            break
        metric_key = metric_key[:160]
        if metric_key in seen_keys:
            continue
        seen_keys.add(metric_key)
        category = metric_key.split(".", 1)[0] if "." in metric_key else "summary"
        value_number = None
        value_text = ""
        if isinstance(value, bool):
            value_text = "true" if value else "false"
        elif isinstance(value, int | float):
            value_number = float(value)
        elif isinstance(value, str):
            value_text = value[:1000]
        else:
            continue
        rows.append(
            TimetableRepairCandidateMetric(
                candidate=candidate,
                metric_key=metric_key,
                category=category[:64],
                value_number=value_number,
                value_text=value_text,
                value_json=value,
            )
        )
    return rows


def _iter_candidate_metric_values(
    value: Any,
    *,
    prefix: str = "",
    depth: int = 0,
    max_depth: int = 5,
) -> list[tuple[str, Any]]:
    """Flatten metric dictionaries into stable scalar path keys."""
    if depth > max_depth:
        return []
    if isinstance(value, dict):
        rows: list[tuple[str, Any]] = []
        for key in sorted(value):
            child_key = _metric_path_part(key)
            if not child_key:
                continue
            next_prefix = f"{prefix}.{child_key}" if prefix else child_key
            rows.extend(
                _iter_candidate_metric_values(
                    value[key],
                    prefix=next_prefix,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )
        return rows
    if isinstance(value, list):
        if all(isinstance(item, str | int | float | bool) for item in value[:20]):
            return [(f"{prefix}.count", len(value))] if prefix else []
        return []
    if prefix and isinstance(value, str | int | float | bool):
        return [(prefix, value)]
    return []


def _metric_path_part(value: object) -> str:
    raw = str(value or "").strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")


def solve_conservative_student_repair(
    run: TimetableRepairRun,
    candidate: dict[str, Any],
    placement: SectionPlacement,
    *,
    component: dict[str, Any],
    limits: dict[str, int],
) -> dict[str, Any]:
    """Run a read-only CP-SAT student reallocation model for one candidate.

    Conservative mode treats every existing StudentTermSection course as a hard
    promise: the student must keep that course, either in the same section or a
    conflict-free alternative section of the same course.
    """

    started = perf_counter()

    if cp_model is None:
        return _mark_candidate_not_solved(
            run,
            candidate,
            status="solver_unavailable",
            reason="OR-Tools CP-SAT is not installed",
            started=started,
        )

    scenario_id = int(component.get("scenario_id") or placement.board.scenario_id)
    target_course = str(
        component.get("target_course_key")
        or placement.term_section.course_key
        or placement.term_section.course_code
    )
    blocked_ids = _blocked_student_ids_for_run(run, component)
    blocked_set = set(blocked_ids)
    component_student_ids = {
        int(row["student_id"])
        for row in component.get("students", [])
        if row.get("student_id") is not None
    }
    student_ids = sorted(component_student_ids | set(blocked_ids))
    if not student_ids:
        return _mark_candidate_not_solved(
            run,
            candidate,
            status="data_unavailable",
            reason="No affected students were available for exact repair",
            started=started,
        )

    course_keys = {str(course) for course in component.get("courses", []) if course}
    course_keys.add(target_course)
    component_section_ids = {
        int(row["id"]) for row in component.get("sections", []) if row.get("id") is not None
    }
    problem_input = build_repair_solver_problem_input(
        scenario_id,
        target_course_key=target_course,
        student_ids=student_ids,
        blocked_student_ids=blocked_ids,
        course_keys=course_keys,
        section_ids=component_section_ids,
    )

    if (
        not problem_input.exact_assignment_source_available
        and problem_input.assignment_source != "current_evaluator_assignment"
    ):
        return _mark_candidate_not_solved(
            run,
            candidate,
            status="data_unavailable",
            reason=(
                "No exact StudentTermSection rows or whole-scenario evaluator assignment "
                "baseline exist for this scenario"
            ),
            started=started,
            details={"solver_domain": problem_input.to_audit_payload()},
        )

    current_by_student_course = problem_input.current_by_student_course
    affected_current_by_section = Counter(problem_input.affected_current_by_section)
    duplicates = list(problem_input.duplicate_current_assignments)
    if duplicates:
        return _mark_candidate_not_solved(
            run,
            candidate,
            status="data_invalid",
            reason="Duplicate current section assignments exist for at least one student/course",
            started=started,
            details={
                "duplicates": duplicates[:20],
                "solver_domain": problem_input.to_audit_payload(),
            },
        )

    requested_courses_by_student = problem_input.requested_courses_by_student
    section_by_id = problem_input.section_by_id
    sections_by_course = problem_input.sections_by_course
    eligibility_context = build_repair_eligibility_context_for_section_ids(
        scenario_id=scenario_id,
        student_ids=student_ids,
        section_ids=list(problem_input.section_ids),
    )

    missing_current_options = list(problem_input.missing_current_options)
    if missing_current_options:
        return _mark_candidate_not_solved(
            run,
            candidate,
            status="data_invalid",
            reason="Current section assignments were not available as solver options",
            started=started,
            details={
                "missing_current_options": missing_current_options[:20],
                "solver_domain": problem_input.to_audit_payload(),
            },
        )

    model = cp_model.CpModel()
    x: dict[tuple[int, str, int], Any] = {}
    by_student_vars: dict[int, list[tuple[str, int, Any]]] = defaultdict(list)
    by_section_vars: dict[int, list[Any]] = defaultdict(list)

    def section_var(student_id: int, course_key: str, section_id: int):
        section_id = int(section_id)
        key = (student_id, course_key, section_id)
        if key not in x:
            var = model.NewBoolVar(f"x_s{student_id}_c{_safe_var_name(course_key)}_t{section_id}")
            x[key] = var
            by_student_vars[student_id].append((course_key, section_id, var))
            by_section_vars[section_id].append(var)
        return x[key]

    required_pairs: list[tuple[int, str, int]] = []
    served_by_student: dict[int, Any] = {}
    requested_served_by_student_course: dict[tuple[int, str], Any] = {}
    optional_requested_pairs: set[tuple[int, str]] = set()
    target_options_by_blocked_student: dict[int, list[int]] = {}
    target_ineligible_by_blocked_student: dict[int, list[dict[str, Any]]] = {}
    option_ids_by_student_course: dict[tuple[int, str], list[int]] = {}
    for sid in student_ids:
        current_courses = current_by_student_course.get(sid, {})
        for course, current_section_id in sorted(current_courses.items()):
            option_ids = eligible_repair_section_ids(
                eligibility_context,
                student_id=sid,
                course_key=course,
                section_ids=sections_by_course.get(course, []),
                current_section_id=current_section_id,
            )
            option_vars = [section_var(sid, course, section_id) for section_id in option_ids]
            if not option_vars:
                return _mark_candidate_not_solved(
                    run,
                    candidate,
                    status="data_invalid",
                    reason=f"No section options exist for required course {course}",
                    started=started,
                    details={"student_id": sid, "course_key": course},
                )
            model.Add(sum(option_vars) == 1)
            required_pairs.append((sid, course, current_section_id))
            option_ids_by_student_course[(sid, course)] = list(option_ids)

        for course in _optional_requested_courses_for_student(
            student_id=sid,
            current_courses=current_courses,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_set=blocked_set,
        ):
            course_section_ids = sections_by_course.get(course, [])
            course_option_ids = eligible_repair_section_ids(
                eligibility_context,
                student_id=sid,
                course_key=course,
                section_ids=course_section_ids,
                is_new_course=True,
            )
            option_ids_by_student_course[(sid, course)] = list(course_option_ids)
            if course == target_course and sid in blocked_set:
                target_options_by_blocked_student[sid] = list(course_option_ids)
                target_ineligible_by_blocked_student[sid] = _blocked_target_ineligibility(
                    eligibility_context,
                    student_id=sid,
                    course_key=target_course,
                    section_ids=course_section_ids,
                )
            served = model.NewBoolVar(f"served_s{sid}_{_safe_var_name(course)}")
            requested_served_by_student_course[(sid, course)] = served
            optional_requested_pairs.add((sid, course))
            if course == target_course and sid in blocked_set:
                served_by_student[sid] = served
            if course_option_ids:
                option_vars = [
                    section_var(sid, course, section_id) for section_id in course_option_ids
                ]
                model.Add(sum(option_vars) == served)
            else:
                model.Add(served == 0)

    variable_count = len(x) + len(required_pairs) + len(requested_served_by_student_course)
    profile_compression = _student_profile_compression_summary(
        student_ids=student_ids,
        eligibility_context=eligibility_context,
        current_by_student_course=current_by_student_course,
        option_ids_by_student_course=option_ids_by_student_course,
        requested_courses_by_student=requested_courses_by_student,
        target_course=target_course,
        blocked_ids=blocked_ids,
        variable_count=variable_count,
    )
    section_ids = set(section_by_id)
    total_current_by_section = Counter(problem_input.total_current_by_section)
    capacity_by_section: dict[int, int] = {}
    fixed_occupancy_by_section: dict[int, int] = {}
    for section_id, section in section_by_id.items():
        total_current = int(total_current_by_section.get(section_id, 0))
        affected_current = int(affected_current_by_section.get(section_id, 0))
        fixed_occupancy = max(0, total_current - affected_current)
        capacity = _section_capacity(section, total_current)
        capacity_by_section[section_id] = capacity
        fixed_occupancy_by_section[section_id] = fixed_occupancy
        section_vars = by_section_vars.get(section_id, [])
        if section_vars:
            model.Add(sum(section_vars) + fixed_occupancy <= capacity)
    section_meetings = _candidate_section_meetings(
        scenario_id,
        placement,
        candidate,
        section_ids=section_ids,
    )
    section_quality_components_by_id = _section_quality_components(
        section_ids=section_ids,
        capacity_by_section=capacity_by_section,
        fixed_occupancy_by_section=fixed_occupancy_by_section,
        section_meetings=section_meetings,
    )
    section_quality_cost_by_id = {
        section_id: int(row.get("total") or 0)
        for section_id, row in section_quality_components_by_id.items()
    }
    assignment_quality_cost_by_key = _assignment_quality_costs(
        student_ids=student_ids,
        option_ids_by_student_course=option_ids_by_student_course,
        current_by_student_course=current_by_student_course,
        section_meetings=section_meetings,
        section_quality_cost_by_id=section_quality_cost_by_id,
    )
    mode_policy = _repair_mode_policy(run.mode)
    warm_start_summary = {
        "enabled": False,
        "used": False,
        "reason": "not_applicable_to_selected_solver",
    }
    flow_solution = _solve_min_cost_flow_repair_if_simple(
        mode_policy=mode_policy,
        student_ids=student_ids,
        current_by_student_course=current_by_student_course,
        option_ids_by_student_course=option_ids_by_student_course,
        requested_courses_by_student=requested_courses_by_student,
        target_course=target_course,
        blocked_ids=blocked_ids,
        capacity_by_section=capacity_by_section,
        fixed_occupancy_by_section=fixed_occupancy_by_section,
        section_quality_cost_by_id=section_quality_cost_by_id,
        assignment_quality_cost_by_key=assignment_quality_cost_by_key,
    )
    if flow_solution:
        chosen = flow_solution["chosen"]
        status_name = flow_solution["solver_status"]
        objective_trace = flow_solution["objective_trace"]
        conflict_policy = flow_solution["conflict_policy"]
        conflict_edges = int(conflict_policy["logical_conflict_edges"])
        solver_variable_count = int(flow_solution["variables"])
        solver_strategy = "min_cost_flow"
        profile_solver = {}
        min_cost_flow_summary = flow_solution["min_cost_flow"]
        large_neighbourhood_summary = {
            "enabled": False,
            "used": False,
            "reason": "min_cost_flow_used",
        }
        profile_compression["solver_used"] = False
        profile_compression["solver_strategy"] = solver_strategy
        runtime_ms = int((perf_counter() - started) * 1000)
    else:
        lns_solution = None
        profile_solution = _solve_profile_repair_if_beneficial(
            limits=limits,
            mode_policy=mode_policy,
            student_ids=student_ids,
            eligibility_context=eligibility_context,
            current_by_student_course=current_by_student_course,
            option_ids_by_student_course=option_ids_by_student_course,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_ids=blocked_ids,
            section_meetings=section_meetings,
            capacity_by_section=capacity_by_section,
            fixed_occupancy_by_section=fixed_occupancy_by_section,
            section_quality_cost_by_id=section_quality_cost_by_id,
            assignment_quality_cost_by_key=assignment_quality_cost_by_key,
            profile_compression=profile_compression,
        )
        if profile_solution:
            chosen = profile_solution["chosen"]
            status_name = profile_solution["solver_status"]
            objective_trace = profile_solution["objective_trace"]
            conflict_policy = profile_solution["conflict_policy"]
            conflict_edges = int(conflict_policy["logical_conflict_edges"])
            solver_variable_count = int(profile_solution["variables"])
            solver_strategy = "profile_pattern_cp_sat"
            profile_solver = profile_solution["profile_solver"]
            warm_start_summary = profile_solution.get("warm_start") or warm_start_summary
            min_cost_flow_summary = {"enabled": False, "reason": "profile_solver_used"}
            large_neighbourhood_summary = {
                "enabled": False,
                "used": False,
                "reason": "profile_solver_used",
            }
            profile_compression["solver_used"] = True
            profile_compression["solver_strategy"] = solver_strategy
            runtime_ms = int((perf_counter() - started) * 1000)
        else:
            profile_compression["solver_used"] = False
            profile_compression["solver_strategy"] = "student_level_cp_sat"
            min_cost_flow_summary = {
                "enabled": min_cost_flow is not None,
                "used": False,
                "reason": "not_simple_one_course_case",
            }
            large_neighbourhood_summary = {
                "enabled": True,
                "used": False,
                "reason": "not_needed",
            }
            if variable_count > int(limits.get("max_variables", DEFAULT_LIMITS["max_variables"])):
                lns_solution = _solve_large_neighbourhood_repair_if_large(
                    limits=limits,
                    mode_policy=mode_policy,
                    student_ids=student_ids,
                    current_by_student_course=current_by_student_course,
                    option_ids_by_student_course=option_ids_by_student_course,
                    requested_courses_by_student=requested_courses_by_student,
                    target_course=target_course,
                    blocked_ids=blocked_ids,
                    section_meetings=section_meetings,
                    capacity_by_section=capacity_by_section,
                    fixed_occupancy_by_section=fixed_occupancy_by_section,
                    section_quality_cost_by_id=section_quality_cost_by_id,
                    assignment_quality_cost_by_key=assignment_quality_cost_by_key,
                )
                if lns_solution and not lns_solution.get("not_solved"):
                    chosen = lns_solution["chosen"]
                    status_name = lns_solution["solver_status"]
                    objective_trace = lns_solution["objective_trace"]
                    conflict_policy = lns_solution["conflict_policy"]
                    conflict_edges = int(conflict_policy["logical_conflict_edges"])
                    solver_variable_count = int(lns_solution["variables"])
                    solver_strategy = "large_neighbourhood_cp_sat"
                    profile_solver = {}
                    warm_start_summary = lns_solution.get("warm_start") or warm_start_summary
                    large_neighbourhood_summary = lns_solution["large_neighbourhood"]
                    runtime_ms = int((perf_counter() - started) * 1000)
                else:
                    large_neighbourhood_summary = (lns_solution or {}).get(
                        "large_neighbourhood"
                    ) or {
                        "enabled": True,
                        "used": False,
                        "reason": "no_feasible_bounded_neighbourhood",
                    }
                    return _mark_candidate_not_solved(
                        run,
                        candidate,
                        status="too_large",
                        reason="Affected component exceeded the interactive solver variable limit",
                        started=started,
                        details={
                            "variable_count": variable_count,
                            "max_variables": limits.get(
                                "max_variables", DEFAULT_LIMITS["max_variables"]
                            ),
                            "profile_compression": profile_compression,
                            "min_cost_flow": min_cost_flow_summary,
                            "large_neighbourhood": large_neighbourhood_summary,
                        },
                    )

            if not lns_solution:
                conflict_policy = _add_student_time_conflict_constraints(
                    model,
                    by_student_vars=by_student_vars,
                    section_meetings=section_meetings,
                    limits=limits,
                )
                conflict_edges = int(conflict_policy["logical_conflict_edges"])
                if conflict_policy["too_large"]:
                    return _mark_candidate_not_solved(
                        run,
                        candidate,
                        status="too_large",
                        reason="Affected component exceeded the interactive conflict-edge limit",
                        started=started,
                        details={
                            "variables": len(x),
                            "conflict_edges": conflict_edges,
                            "max_conflict_edges": conflict_policy["max_conflict_edges"],
                            "conflict_policy": conflict_policy,
                            "profile_compression": profile_compression,
                            "min_cost_flow": min_cost_flow_summary,
                            "large_neighbourhood": large_neighbourhood_summary,
                        },
                    )

        if not profile_solution and not lns_solution:
            changed_by_student: dict[int, list[Any]] = defaultdict(list)
            changed_vars: list[Any] = []
            for sid, course, current_section_id in required_pairs:
                current_var = x.get((sid, course, current_section_id))
                if current_var is None:
                    return _mark_candidate_not_solved(
                        run,
                        candidate,
                        status="data_invalid",
                        reason="Current assignment variable was not created",
                        started=started,
                        details={
                            "student_id": sid,
                            "course_key": course,
                            "section_id": current_section_id,
                        },
                    )
                changed = model.NewBoolVar(
                    f"changed_s{sid}_c{_safe_var_name(course)}_from{current_section_id}"
                )
                model.Add(changed + current_var == 1)
                changed_by_student[sid].append(changed)
                changed_vars.append(changed)

            moved_vars: list[Any] = []
            for sid, student_changes in changed_by_student.items():
                moved = model.NewBoolVar(f"moved_s{sid}")
                for changed in student_changes:
                    model.Add(moved >= changed)
                model.Add(moved <= sum(student_changes))
                moved_vars.append(moved)

            served_expr = sum(served_by_student.values()) if served_by_student else 0
            requested_expr = (
                sum(requested_served_by_student_course.values())
                if requested_served_by_student_course
                else 0
            )
            moved_expr = sum(moved_vars) if moved_vars else 0
            changed_expr = sum(changed_vars) if changed_vars else 0
            quality_expr = _assignment_quality_expr(x, assignment_quality_cost_by_key)
            warm_start_summary = _add_current_assignment_solver_hints(
                model,
                x=x,
                current_by_student_course=current_by_student_course,
                served_by_student=served_by_student,
                requested_served_by_student_course=requested_served_by_student_course,
            )
            solver, status, objective_trace = _solve_lexicographic_repair(
                model,
                limits=limits,
                policy=mode_policy,
                served_expr=served_expr,
                requested_expr=requested_expr,
                moved_expr=moved_expr,
                changed_expr=changed_expr,
                quality_expr=quality_expr,
            )
            status_name = _cp_sat_status_name(status)
            runtime_ms = int((perf_counter() - started) * 1000)

            if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
                return _mark_candidate_not_solved(
                    run,
                    candidate,
                    status=status_name,
                    reason=f"No {mode_policy['mode']} no-loss assignment was found for this candidate",
                    started=started,
                    details={
                        "variables": len(x),
                        "conflict_edges": conflict_edges,
                        "conflict_policy": conflict_policy,
                        "runtime_ms": runtime_ms,
                        "mode_policy": mode_policy,
                        "objective_trace": objective_trace,
                        "solver_domain": problem_input.to_audit_payload(),
                    },
                )

            chosen: dict[tuple[int, str], int] = {}
            for (sid, course, section_id), var in x.items():
                if solver.BooleanValue(var):
                    chosen[(sid, course)] = section_id
            solver_variable_count = len(x)
            solver_strategy = "student_level_cp_sat"
            profile_solver = {}
    chosen_count_by_section: Counter[int] = Counter(chosen.values())

    change_rows: list[dict[str, Any]] = []
    moved_student_ids: set[int] = set()
    unchanged_count = 0
    moved_section_count = 0
    existing_lost = 0
    for sid, course_map in sorted(current_by_student_course.items()):
        for course, before_section_id in sorted(course_map.items()):
            after_section_id = chosen.get((sid, course))
            if after_section_id is None:
                change_type = TimetableRepairStudentChange.CHANGE_LOST
                existing_lost += 1
            elif after_section_id == before_section_id:
                change_type = TimetableRepairStudentChange.CHANGE_UNCHANGED
                unchanged_count += 1
            else:
                change_type = TimetableRepairStudentChange.CHANGE_MOVED
                moved_section_count += 1
                moved_student_ids.add(sid)
            change_rows.append(
                {
                    "student_id": sid,
                    "course_key": course,
                    "before_section_id": str(before_section_id),
                    "after_section_id": str(after_section_id or ""),
                    "change_type": change_type,
                    "details_json": {
                        "before": _section_summary(section_by_id.get(before_section_id)),
                        "after": _section_summary(section_by_id.get(after_section_id))
                        if after_section_id
                        else None,
                        "policy": mode_policy["existing_course_policy"],
                        "eligibility_policy": "program_section_lock_and_protection_checked",
                    },
                }
            )

    blocked_newly_registered = 0
    requested_courses_recovered = 0
    additional_requested_courses_recovered = 0
    unresolved_blocked = 0
    unresolved_requested_courses = 0
    unresolved_diagnostics: dict[int, dict[str, Any]] = {}
    for sid, course in sorted(optional_requested_pairs):
        after_section_id = chosen.get((sid, course))
        is_target_blocked = (
            sid in blocked_set
            and course == target_course
            and target_course not in current_by_student_course.get(sid, {})
        )
        if after_section_id:
            requested_courses_recovered += 1
            if is_target_blocked:
                blocked_newly_registered += 1
            else:
                additional_requested_courses_recovered += 1
            change_type = TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED
            unresolved_reason = None
        else:
            unresolved_requested_courses += 1
            change_type = TimetableRepairStudentChange.CHANGE_UNRESOLVED
            if is_target_blocked:
                unresolved_blocked += 1
                unresolved_reason = _diagnose_unresolved_blocked_student(
                    student_id=sid,
                    target_course=target_course,
                    section_by_id=section_by_id,
                    target_option_ids=target_options_by_blocked_student.get(sid, []),
                    ineligible_sections=target_ineligible_by_blocked_student.get(sid, []),
                    chosen=chosen,
                    section_meetings=section_meetings,
                    capacity_by_section=capacity_by_section,
                    fixed_occupancy_by_section=fixed_occupancy_by_section,
                    chosen_count_by_section=chosen_count_by_section,
                )
                unresolved_diagnostics[sid] = unresolved_reason
            else:
                unresolved_reason = {
                    "reason": "requested_course_not_selected",
                    "course_key": course,
                    "eligible_option_count": len(
                        option_ids_by_student_course.get((sid, course), [])
                    ),
                    "policy": "lower_priority_requested_course_recovery",
                }
        change_rows.append(
            {
                "student_id": sid,
                "course_key": course,
                "before_section_id": "",
                "after_section_id": str(after_section_id or ""),
                "change_type": change_type,
                "details_json": {
                    "after": _section_summary(section_by_id.get(after_section_id))
                    if after_section_id
                    else None,
                    "policy": "blocked_target_course_recovery"
                    if is_target_blocked
                    else "requested_course_recovery",
                    "mode_policy": mode_policy["mode"],
                    "eligibility_policy": "program_section_prerequisite_and_protection_checked",
                    "objective_role": "target_blocked_recovery"
                    if is_target_blocked
                    else "requested_course_recovery",
                    "unresolved_reason": unresolved_reason,
                },
            }
        )

    candidate["student_changes"] = change_rows
    eligibility_summary = repair_eligibility_summary(eligibility_context)
    unresolved_summary = _unresolved_diagnostic_summary(unresolved_diagnostics)
    cascade_summary = _cascade_repair_summary(change_rows, target_course=target_course)
    solver_budget_summary = _solver_budget_summary(
        limits=limits,
        mode_policy=mode_policy,
        objective_trace=objective_trace,
        runtime_ms=runtime_ms,
    )
    quality_breakdown = _quality_penalty_breakdown(
        chosen,
        assignment_quality_cost_by_key=assignment_quality_cost_by_key,
        section_quality_cost_by_id=section_quality_cost_by_id,
        section_quality_components_by_id=section_quality_components_by_id,
    )
    exact_metrics = {
        "enabled": True,
        "solver_status": status_name,
        "mode": run.mode,
        "existing_lost": existing_lost,
        "blocked_recovered": blocked_newly_registered,
        "requested_courses_recovered": requested_courses_recovered,
        "additional_requested_courses_recovered": additional_requested_courses_recovered,
        "unresolved_requested_courses": unresolved_requested_courses,
        "unresolved_blocked": unresolved_blocked,
        "students_moved": len(moved_student_ids),
        "existing_section_changes": moved_section_count,
        "section_changes": moved_section_count + requested_courses_recovered,
        "unchanged_existing_assignments": unchanged_count,
        "protected_existing_assignments": len(required_pairs),
        "optional_requested_courses": len(optional_requested_pairs),
        "student_change_rows": len(change_rows),
        "variables": solver_variable_count,
        "student_level_variables": len(x),
        "solver_strategy": solver_strategy,
        "conflict_edges": conflict_edges,
        "conflict_policy": conflict_policy,
        "profile_compression": profile_compression,
        "profile_solver": profile_solver,
        "min_cost_flow": min_cost_flow_summary,
        "large_neighbourhood": large_neighbourhood_summary,
        "warm_start": warm_start_summary,
        "timetable_quality": {
            "policy": "final-tier spare-capacity weak-slot day-balance preferences",
            "penalty": quality_breakdown["total"],
            "components": quality_breakdown,
            "section_cost_count": len(section_quality_cost_by_id),
        },
        "solver_budget": solver_budget_summary,
        "solver_domain": problem_input.to_audit_payload(),
        "capacity_policy": "max(available_capacity, registered_count, exact_current_occupancy)",
        "eligibility_policy": eligibility_summary,
        "unresolved_diagnostics": unresolved_summary,
        "cascade": cascade_summary,
        "runtime_ms": runtime_ms,
        "mode_policy": mode_policy,
        "objective": {
            "tier_1": mode_policy["tier_1"],
            "tier_2": mode_policy["tier_2"],
            "tier_3": mode_policy["tier_3"],
            "tier_4": mode_policy["tier_4"],
            "tier_5": mode_policy.get("tier_5", ""),
            "tier_6": mode_policy.get("tier_6", ""),
            "tier_7": mode_policy.get("tier_7", ""),
            "strategy": mode_policy["strategy"],
            "stage_order": [stage["name"] for stage in mode_policy["stages"]],
            "trace": objective_trace,
        },
    }
    metrics = dict(candidate.get("metrics") or {})
    metrics["exact_repair"] = exact_metrics
    explanation = dict(candidate.get("explanation") or {})
    explanation["student_solver_status"] = status_name
    explanation["student_solver_summary"] = {
        "mode": mode_policy["mode"],
        "mode_summary": mode_policy["mode_summary"],
        "analysis_only": mode_policy["analysis_only"],
        "blocked_recovered": blocked_newly_registered,
        "requested_courses_recovered": requested_courses_recovered,
        "additional_requested_courses_recovered": additional_requested_courses_recovered,
        "existing_lost": existing_lost,
        "students_moved": len(moved_student_ids),
        "unresolved_blocked": unresolved_blocked,
        "unresolved_requested_courses": unresolved_requested_courses,
        "eligibility_rejected_options": eligibility_summary["rejected_option_count"],
        "unresolved_reasons": unresolved_summary["reason_counts"],
        "cascade": {
            "requires_multi_course_cascade": cascade_summary["requires_multi_course_cascade"],
            "touched_course_count": cascade_summary["touched_course_count"],
            "multi_course_student_count": cascade_summary["multi_course_student_count"],
        },
    }
    candidate["metrics"] = metrics
    candidate["explanation"] = explanation
    candidate["solver_status"] = status_name
    candidate["status"] = (
        TimetableRepairCandidate.STATUS_FEASIBLE
        if existing_lost == 0
        else TimetableRepairCandidate.STATUS_NOT_SOLVED
    )
    _candidate_draft_log(
        candidate,
        "info",
        "repair_candidate_student_solver_completed",
        exact_metrics,
    )
    return exact_metrics


def hard_feasibility_rejections(
    placement: SectionPlacement,
    *,
    day: str,
    start_time: str,
    end_time: str,
    room: str,
    room_reasons: list[dict[str, Any]],
    planning_scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Reject impossible candidates before any future student solver."""

    reasons: list[dict[str, Any]] = []
    if placement.is_locked:
        reasons.append({"code": "TARGET_PLACEMENT_LOCKED", "message": "Target placement is locked"})
    if room_reasons:
        reasons.extend(room_reasons)

    for reason in validate_candidate(
        {
            "day": day,
            "start_time": start_time,
            "end_time": end_time,
            "room": room,
            "course_code": placement.term_section.course_code,
        },
        {
            "prayer_windows": get_prayer_windows(),
            "locked_cells": _locked_cells_for_scenario(placement.board.scenario_id, placement.id),
        },
    ):
        reasons.append(reason.to_dict())

    validation = validate_placement(
        board_id=placement.board_id,
        day=day,
        start_time=start_time,
        end_time=end_time,
        room=room,
        term_section_id=placement.term_section_id,
        exclude_placement_id=placement.id,
        ignore_overlap_term_section_ids=set(
            (planning_scope or {}).get("ignore_overlap_term_section_ids") or []
        ),
    )
    if validation.get("critical_count", 0):
        reasons.append(
            {
                "code": "TIME_OR_INSTRUCTOR_CONFLICT",
                "message": f"{validation['critical_count']} critical timetable issue(s)",
                "details": {
                    "overlaps": validation.get("overlaps", []),
                    "instructor_clashes": validation.get("instructor_clashes", []),
                },
            }
        )
    return reasons


def build_repair_summary(
    *,
    run: TimetableRepairRun,
    placement: SectionPlacement,
    before_snapshot: dict[str, Any],
    component: dict[str, Any],
    candidate_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    feasible = [
        row
        for row in candidate_payloads
        if row["status"] == TimetableRepairCandidate.STATUS_FEASIBLE
    ]
    rejected = [
        row
        for row in candidate_payloads
        if row["status"] == TimetableRepairCandidate.STATUS_REJECTED
    ]
    not_solved = [
        row
        for row in candidate_payloads
        if row["status"] == TimetableRepairCandidate.STATUS_NOT_SOLVED
    ]
    best = feasible[0] if feasible else None
    exact_rows = [
        (row.get("metrics") or {}).get("exact_repair") or {}
        for row in candidate_payloads
        if (row.get("metrics") or {}).get("exact_repair")
    ]
    solver_status_counts = Counter(row.get("solver_status", "unknown") for row in exact_rows)
    solver_strategy_counts = Counter(row.get("solver_strategy", "unknown") for row in exact_rows)
    best_exact = (best.get("metrics") or {}).get("exact_repair") if best else None
    candidate_evaluation = _candidate_evaluation_summary(candidate_payloads, best)
    return {
        "run_id": str(run.id),
        "mode": run.mode,
        "versions": {
            "solver": run.solver_version,
            "constraints": run.constraint_version,
            "objective": run.objective_version,
            "cache": REPAIR_CACHE_VERSION,
        },
        "target": {
            "placement_id": placement.id,
            "term_section_id": placement.term_section_id,
            "course_key": placement.term_section.course_key,
            "course_code": placement.term_section.course_code,
            "section": placement.term_section.section,
            "current": {
                "day": placement.day,
                "start_time": placement.start_time,
                "end_time": placement.end_time,
                "room": placement.room or "",
            },
        },
        "candidate_count": len(candidate_payloads),
        "feasible_candidate_count": len(feasible),
        "rejected_candidate_count": len(rejected),
        "not_solved_candidate_count": len(not_solved),
        "best_candidate_id": best["candidate_id"] if best else "",
        "assignment_snapshot_available": bool(
            before_snapshot.get("exact_assignment_source_available")
        ),
        "component_counts": component.get("counts", {}),
        "component_truncated": component.get("truncated", False),
        "blocked_demand": component.get("blocked_demand") or {},
        "student_solver": {
            "exact_cp_sat_reallocation": (
                "enabled_readonly" if cp_model is not None else "solver_unavailable"
            ),
            "apply_enabled": False,
            "approval_required": True,
            "solver_status_counts": dict(solver_status_counts),
            "solver_strategy_counts": dict(solver_strategy_counts),
            "proposed_student_change_count": sum(
                int(row.get("student_change_count") or 0) for row in candidate_payloads
            ),
            "best_candidate_metrics": best_exact or {},
        },
        "candidate_evaluation": candidate_evaluation,
        "next_phase": [
            "Promote the current spare-capacity quality tier into richer day-balance and weak-slot preferences.",
            "Use the global repair plan workflow for programme/level unresolved-student recovery batches.",
        ],
    }


def serialize_repair_run(run: TimetableRepairRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "scenario_id": run.scenario_id,
        "scenario_name": run.scenario.name if run.scenario_id else "",
        "target_placement_id": run.target_placement_id,
        "target_section_id": run.target_section_id,
        "mode": run.mode,
        "status": run.status,
        "requested_by": getattr(run.requested_by, "username", "") if run.requested_by_id else "",
        "requested_at": run.requested_at.isoformat() if run.requested_at else "",
        "completed_at": run.completed_at.isoformat() if run.completed_at else "",
        "solver_version": run.solver_version,
        "constraint_version": run.constraint_version,
        "objective_version": run.objective_version,
        "request_payload": run.request_payload,
        "error_message": run.error_message,
    }


def serialize_candidate(
    candidate: TimetableRepairCandidate,
    *,
    run: TimetableRepairRun | None = None,
) -> dict[str, Any]:
    student_change_count = candidate.student_changes.count()
    run = run or candidate.run
    decision = _candidate_decision_gate(
        candidate,
        student_change_count=student_change_count,
    )
    payload = {
        "id": candidate.id,
        "candidate_id": candidate.candidate_id,
        "day": candidate.day,
        "start_time": candidate.start_time,
        "end_time": candidate.end_time,
        "room": candidate.room,
        "status": candidate.status,
        "solver_status": candidate.solver_status,
        "score_rank": candidate.score_rank,
        "metrics": candidate.metrics_json,
        "metric_rows": serialize_candidate_metric_rows(candidate),
        "explanation": candidate.explanation_json,
        "rejection_reasons": candidate.rejection_reasons,
        "student_change_count": student_change_count,
        "decision": decision,
        "preflight": _candidate_current_state_preflight(
            run,
            candidate,
            student_change_count=student_change_count,
            decision=decision,
        ),
    }
    payload["admin_summary"] = _report_candidate(payload)
    return payload


def serialize_candidate_metric_rows(candidate: TimetableRepairCandidate) -> list[dict[str, Any]]:
    return [
        {
            "metric_key": row.metric_key,
            "category": row.category,
            "value_number": row.value_number,
            "value_text": row.value_text,
            "value": row.value_json,
        }
        for row in candidate.metric_rows.all().order_by("metric_key")
    ]


def serialize_student_change(change: TimetableRepairStudentChange) -> dict[str, Any]:
    return {
        "candidate_id": change.candidate.candidate_id,
        "student_id": change.student_id,
        "course_key": change.course_key,
        "before_section_id": change.before_section_id,
        "after_section_id": change.after_section_id,
        "change_type": change.change_type,
        "details": change.details_json,
    }


def _repair_approval_rows(run: TimetableRepairRun) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row.candidate.candidate_id if row.candidate_id else "",
            "status": row.status,
            "notes": row.notes,
            "requested_by": getattr(row.requested_by, "username", "")
            if row.requested_by_id
            else "",
            "decided_by": getattr(row.decided_by, "username", "") if row.decided_by_id else "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "decided_at": row.decided_at.isoformat() if row.decided_at else "",
        }
        for row in run.approvals.select_related("candidate", "requested_by", "decided_by").order_by(
            "created_at", "id"
        )
    ]


def _repair_audit_log_rows(
    run: TimetableRepairRun,
    *,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], bool]:
    rows = list(
        run.solver_logs.select_related("candidate").order_by("created_at", "id")[: limit + 1]
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    return (
        [
            {
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "level": row.level,
                "message": row.message,
                "candidate_id": row.candidate.candidate_id if row.candidate_id else "",
                "payload": _compact_audit_payload(row.payload_json or {}),
            }
            for row in rows
        ],
        truncated,
    )


def _repair_candidate_audit_log_rows(
    candidate: TimetableRepairCandidate,
    *,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], bool]:
    rows = list(
        candidate.solver_logs.select_related("candidate").order_by("created_at", "id")[: limit + 1]
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    return (
        [
            {
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "level": row.level,
                "message": row.message,
                "candidate_id": candidate.candidate_id,
                "payload": _compact_audit_payload(row.payload_json or {}),
            }
            for row in rows
        ],
        truncated,
    )


def _compact_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep audit payloads useful in UI/report responses without huge nested blobs."""

    if not isinstance(payload, dict):
        return {}
    allowed_keys = {
        "candidate_id",
        "blocked_recovered",
        "existing_lost",
        "students_moved",
        "unresolved_blocked",
        "solver_status",
        "solver_strategy",
        "status",
        "mode",
        "error",
        "runtime_ms",
        "candidate_count",
        "feasible_candidate_count",
        "rejected_candidate_count",
        "not_solved_candidate_count",
        "application",
        "rollback",
        "student_changes",
        "solver_budget",
        "warm_start",
        "blocked_demand",
    }
    compact = {key: payload[key] for key in allowed_keys if key in payload}
    if "student_solver" in payload and isinstance(payload["student_solver"], dict):
        solver = payload["student_solver"]
        compact["student_solver"] = {
            key: solver.get(key)
            for key in [
                "exact_cp_sat_reallocation",
                "solver_status_counts",
                "solver_strategy_counts",
                "proposed_student_change_count",
            ]
            if key in solver
        }
    if "candidate_evaluation" in payload and isinstance(payload["candidate_evaluation"], dict):
        evaluation = payload["candidate_evaluation"]
        compact["candidate_evaluation"] = {
            key: evaluation.get(key)
            for key in ["mode", "budget", "solver_invoked_count", "candidate_runtime_ms"]
            if key in evaluation
        }
    if "target" in payload and isinstance(payload["target"], dict):
        target = payload["target"]
        compact["target"] = {
            key: target.get(key)
            for key in ["placement_id", "term_section_id", "course_key", "course_code", "section"]
            if key in target
        }
    return compact


def _repair_audit_timeline(
    *,
    approvals: list[dict[str, Any]],
    audit_logs: list[dict[str, Any]],
    limit: int = 80,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for log in audit_logs:
        rows.append(
            {
                "type": "log",
                "created_at": log.get("created_at", ""),
                "level": log.get("level", ""),
                "event": log.get("message", ""),
                "candidate_id": log.get("candidate_id", ""),
                "actor": "",
                "summary": _audit_event_summary(log.get("message", ""), log.get("payload") or {}),
            }
        )
    for approval in approvals:
        status = approval.get("status", "")
        event_time = approval.get("decided_at") or approval.get("created_at") or ""
        rows.append(
            {
                "type": "approval",
                "created_at": event_time,
                "level": "info",
                "event": f"repair_approval_{status}",
                "candidate_id": approval.get("candidate_id", ""),
                "actor": approval.get("decided_by") or approval.get("requested_by") or "",
                "summary": approval.get("notes") or f"Approval status: {status}",
            }
        )
    rows.sort(key=lambda row: (row.get("created_at") or "", row.get("event") or ""))
    return rows[-limit:]


def _audit_event_summary(message: str, payload: dict[str, Any]) -> str:
    if message == "repair_candidate_student_solver_completed":
        return (
            f"{payload.get('solver_strategy') or 'solver'} "
            f"{payload.get('solver_status') or ''}; "
            f"recovered {payload.get('blocked_recovered', 0)}, "
            f"requested {payload.get('requested_courses_recovered', 0)}, "
            f"moved {payload.get('students_moved', 0)}"
        ).strip()
    if message == "repair_candidate_student_solver_not_solved":
        if payload.get("solver_status") == "budget_exhausted":
            return "Skipped after the interactive evaluation budget was exhausted"
        return str(payload.get("reason") or payload.get("status") or "Not solved")
    if message == "repair_analysis_completed":
        return (
            f"{payload.get('feasible_candidate_count', 0)} feasible / "
            f"{payload.get('candidate_count', 0)} candidates"
        )
    if message == "repair_candidate_applied":
        return f"Applied {payload.get('candidate_id', '')}; rollback available"
    if message == "repair_candidate_rolled_back":
        return f"Rolled back {payload.get('candidate_id', '')}"
    if message == "repair_candidate_approved":
        return f"Approved {payload.get('candidate_id', '')}"
    if message == "repair_analysis_started":
        return f"Mode {payload.get('mode', '')}; target placement {payload.get('placement_id', '')}"
    if message == "repair_analysis_failed":
        return str(payload.get("error") or "Analysis failed")
    return str(message or "")


def _candidate_evaluation_summary(
    candidate_payloads: list[dict[str, Any]],
    best: dict[str, Any] | None,
) -> dict[str, Any]:
    evaluation_rows = [
        (payload.get("metrics") or {}).get("evaluation") or {} for payload in candidate_payloads
    ]
    ranking_rows = [
        (payload.get("metrics") or {}).get("ranking") or {} for payload in candidate_payloads
    ]
    exact_rows = [
        (payload.get("metrics") or {}).get("exact_repair") or {} for payload in candidate_payloads
    ]
    budget_rows = [row.get("budget") or {} for row in evaluation_rows]
    parallel_rows = [row.get("parallelism") or {} for row in evaluation_rows]
    best_ranking = (best.get("metrics") or {}).get("ranking") if best else {}
    parallel_enabled = any(bool(row.get("enabled")) for row in parallel_rows)
    return {
        "mode": "in_memory_then_audited_bulk_persist",
        "parallelism": {
            "enabled": parallel_enabled,
            "strategy": next(
                (str(row.get("strategy") or "") for row in parallel_rows if row.get("strategy")),
                "",
            ),
            "dispatch": next(
                (str(row.get("dispatch") or "") for row in parallel_rows if row.get("dispatch")),
                "",
            ),
            "worker_count": max(
                (int(row.get("worker_count") or 1) for row in parallel_rows),
                default=1,
            ),
            "requested_worker_count": max(
                (int(row.get("requested_worker_count") or 1) for row in parallel_rows),
                default=1,
            ),
            "database_write_policy": next(
                (
                    str(row.get("database_write_policy") or "")
                    for row in parallel_rows
                    if row.get("database_write_policy")
                ),
                "",
            ),
            "budget_policy": next(
                (
                    str(row.get("budget_policy") or "")
                    for row in parallel_rows
                    if row.get("budget_policy")
                ),
                "",
            ),
            "reason": next(
                (str(row.get("reason") or "") for row in parallel_rows if row.get("reason")),
                "",
            ),
        },
        "source_candidate_count": max(
            (int(row.get("source_candidate_count") or 0) for row in evaluation_rows),
            default=0,
        ),
        "scan_limit": max(
            (int(row.get("scan_limit") or 0) for row in evaluation_rows),
            default=0,
        ),
        "prepared_candidate_count": max(
            (int(row.get("prepared_candidate_count") or 0) for row in evaluation_rows),
            default=0,
        ),
        "selected_candidate_count": len(candidate_payloads),
        "hard_rejected_count": sum(1 for row in evaluation_rows if row.get("hard_rejected")),
        "solver_invoked_count": sum(1 for row in evaluation_rows if row.get("solver_invoked")),
        "candidate_runtime_ms": sum(
            int(row.get("candidate_runtime_ms") or 0) for row in evaluation_rows
        ),
        "solver_runtime_ms": sum(int(row.get("runtime_ms") or 0) for row in exact_rows),
        "preparation_runtime_ms": max(
            (int(row.get("preparation_runtime_ms") or 0) for row in evaluation_rows),
            default=0,
        ),
        "student_outcome_runtime_ms": max(
            (int(row.get("student_outcome_runtime_ms") or 0) for row in evaluation_rows),
            default=0,
        ),
        "budget": {
            "enabled": True,
            "policy": "bounded_interactive_candidate_evaluation",
            "limit_seconds": max(
                (int(row.get("limit_seconds") or 0) for row in budget_rows),
                default=0,
            ),
            "limit_ms": max(
                (int(row.get("limit_ms") or 0) for row in budget_rows),
                default=0,
            ),
            "selected_candidate_count": max(
                (int(row.get("selected_candidate_count") or 0) for row in budget_rows),
                default=len(candidate_payloads),
            ),
            "estimated_seconds_per_candidate": max(
                (float(row.get("estimated_seconds_per_candidate") or 0) for row in budget_rows),
                default=0,
            ),
            "exhausted_candidate_count": sum(1 for row in budget_rows if row.get("exhausted")),
            "budget_skipped_solver_count": sum(
                1
                for row in evaluation_rows
                if row.get("solver_skipped_reason") == "evaluation_budget_exhausted"
            ),
        },
        "total_evaluation_runtime_ms": max(
            (int(row.get("total_evaluation_runtime_ms") or 0) for row in evaluation_rows),
            default=0,
        ),
        "ranked_candidate_count": sum(1 for row in ranking_rows if row.get("score_rank")),
        "ranking_strategy": "lexicographic_protect_recover_requested_minimize_disruption_quality",
        "best_candidate_id": best.get("candidate_id", "") if best else "",
        "best_candidate_reason": (best_ranking or {}).get("primary_reason", ""),
    }


def _run_has_approved_candidate(run: TimetableRepairRun) -> bool:
    return TimetableRepairApproval.objects.filter(
        run=run,
        status=TimetableRepairApproval.STATUS_APPROVED,
    ).exists()


def _candidate_for_run(
    run: TimetableRepairRun,
    candidate_identifier: int | str,
    *,
    lock: bool = False,
) -> TimetableRepairCandidate:
    qs = TimetableRepairCandidate.objects.filter(run=run)
    if lock:
        qs = qs.select_for_update()
    identifier = str(candidate_identifier).strip()
    lookup = Q(candidate_id=identifier)
    if identifier.isdigit():
        lookup |= Q(id=int(identifier))
    try:
        return qs.get(lookup)
    except TimetableRepairCandidate.DoesNotExist as exc:
        raise TimetableRepairOperationError(
            "Repair candidate not found",
            code="REPAIR_CANDIDATE_NOT_FOUND",
            status=404,
        ) from exc


def _ensure_scenario_mutable(run: TimetableRepairRun) -> None:
    if run.scenario.status == "published":
        raise TimetableRepairOperationError(
            "Cannot modify a published scenario",
            code="SCENARIO_PUBLISHED",
            status=400,
        )


def _ensure_candidate_applicable(candidate: TimetableRepairCandidate) -> None:
    if candidate.run.mode == TimetableRepairRun.MODE_SIMULATION:
        raise TimetableRepairOperationError(
            "Simulation repair runs are analysis-only and cannot be approved or applied",
            code="REPAIR_SIMULATION_ONLY",
            status=409,
            details={"mode": candidate.run.mode},
        )
    if candidate.status != TimetableRepairCandidate.STATUS_FEASIBLE:
        raise TimetableRepairOperationError(
            "Only feasible solved candidates can be approved or applied",
            code="REPAIR_CANDIDATE_NOT_FEASIBLE",
            status=409,
            details={
                "candidate_status": candidate.status,
                "solver_status": candidate.solver_status,
            },
        )
    if candidate.solver_status not in {"optimal", "feasible"}:
        raise TimetableRepairOperationError(
            "Candidate has no exact solved student repair proposal",
            code="REPAIR_CANDIDATE_NOT_SOLVED",
            status=409,
            details={"solver_status": candidate.solver_status},
        )
    exact = (candidate.metrics_json or {}).get("exact_repair") or {}
    if int(exact.get("existing_lost") or 0):
        raise TimetableRepairOperationError(
            "Candidate would lose existing registrations and cannot be applied",
            code="REPAIR_EXISTING_LOSS_BLOCKED",
            status=409,
            details=exact,
        )
    if not candidate.student_changes.exists():
        raise TimetableRepairOperationError(
            "Candidate has no audited student changes to apply",
            code="REPAIR_NO_STUDENT_CHANGES",
            status=409,
        )


def _validate_candidate_write_preconditions(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
    *,
    lock_changes: bool = False,
    materialize_evaluator_baseline: bool = False,
) -> tuple[SectionPlacement, list[TimetableRepairStudentChange]]:
    """Validate the current scenario still matches the audited repair proposal."""

    placements_by_id = _placements_for_candidate_moves(candidate, lock=True)
    placement = placements_by_id.get(int(run.target_placement_id)) or _target_placement_for_update(
        run
    )
    for scoped_placement in placements_by_id.values():
        _assert_placement_matches_before_snapshot(run, scoped_placement)
    planning_scope = (run.request_payload or {}).get("planning_scope") or {}
    rejections = _hard_feasibility_rejections_for_move_set(
        placements_by_id=placements_by_id,
        moves=_candidate_move_set(candidate, placement=placement),
        planning_scope=planning_scope,
    )
    rejections.extend(
        _move_scope_self_overlap_rejections(_candidate_move_set(candidate, placement=placement))
    )
    if rejections:
        raise TimetableRepairOperationError(
            "Candidate is no longer feasible; run a fresh analysis",
            code="REPAIR_CANDIDATE_STALE",
            status=409,
            details={"rejections": rejections},
        )

    changes_qs = candidate.student_changes
    if lock_changes:
        changes_qs = changes_qs.select_for_update()
    changes = list(changes_qs.order_by("id"))
    if materialize_evaluator_baseline:
        _materialize_evaluator_baseline_for_apply(run, candidate)
    _assert_student_changes_match_current_state(run, candidate, changes)
    return placement, changes


def _candidate_decision_gate(
    candidate: TimetableRepairCandidate,
    *,
    student_change_count: int | None = None,
) -> dict[str, Any]:
    """Return a static admin decision gate for UI/reporting.

    Current-state freshness is still validated transactionally by approve/apply.
    """

    exact = (candidate.metrics_json or {}).get("exact_repair") or {}
    approval = candidate.approvals.order_by("-decided_at", "-created_at", "-id").first()
    approval_status = approval.status if approval else ""
    change_count = (
        int(student_change_count)
        if student_change_count is not None
        else candidate.student_changes.count()
    )
    blocked_reasons: list[dict[str, str]] = []
    cautions: list[dict[str, str]] = []

    def block(code: str, message: str) -> None:
        blocked_reasons.append({"code": code, "message": message})

    def caution(code: str, message: str) -> None:
        cautions.append({"code": code, "message": message})

    if candidate.run.mode == TimetableRepairRun.MODE_SIMULATION:
        block("REPAIR_SIMULATION_ONLY", "Simulation runs are analysis-only.")
    if candidate.status != TimetableRepairCandidate.STATUS_FEASIBLE:
        block("REPAIR_CANDIDATE_NOT_FEASIBLE", "Only feasible candidates can be approved.")
    if candidate.solver_status not in {"optimal", "feasible"}:
        block(
            "REPAIR_CANDIDATE_NOT_SOLVED",
            "The student repair solver did not produce an applicable solution.",
        )
    if int(exact.get("existing_lost") or 0):
        block("REPAIR_EXISTING_LOSS_BLOCKED", "The candidate would lose existing registrations.")
    if change_count <= 0:
        block("REPAIR_NO_STUDENT_CHANGES", "No audited student changes are available to apply.")
    if approval_status == TimetableRepairApproval.STATUS_APPLIED:
        block("REPAIR_ALREADY_APPLIED", "This candidate has already been applied.")
    if approval_status == TimetableRepairApproval.STATUS_ROLLED_BACK:
        block("REPAIR_ALREADY_ROLLED_BACK", "This candidate was already rolled back.")

    if int(exact.get("unresolved_blocked") or 0):
        caution("REPAIR_UNRESOLVED_REMAIN", "Some blocked students remain unresolved.")
    if int(exact.get("students_moved") or 0):
        caution(
            "REPAIR_MOVES_EXISTING_STUDENTS", "Existing students will be moved between sections."
        )
    if (exact.get("cascade") or {}).get("requires_multi_course_cascade"):
        caution("REPAIR_MULTI_COURSE_CASCADE", "The solution requires a multi-course cascade.")

    approve_allowed = not blocked_reasons and approval_status not in {
        TimetableRepairApproval.STATUS_APPROVED,
        TimetableRepairApproval.STATUS_APPLIED,
        TimetableRepairApproval.STATUS_ROLLED_BACK,
    }
    apply_allowed = (
        not blocked_reasons and approval_status == TimetableRepairApproval.STATUS_APPROVED
    )
    risk_level = "blocked" if blocked_reasons else "caution" if cautions else "safe"
    return {
        "approval_status": approval_status or "none",
        "approve_allowed": approve_allowed,
        "apply_allowed": apply_allowed,
        "risk_level": risk_level,
        "blocked_reasons": blocked_reasons,
        "cautions": cautions,
        "preflight": {
            "approval_runs_current_state_validation": True,
            "apply_revalidates_current_state": True,
            "rollback_requires_repair_owned_assignments": True,
        },
    }


def _candidate_current_state_preflight(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
    *,
    student_change_count: int | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only freshness check for UI/reporting before approve/apply."""

    decision = decision or _candidate_decision_gate(
        candidate,
        student_change_count=student_change_count,
    )
    actionable = bool(decision.get("approve_allowed") or decision.get("apply_allowed"))
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[dict[str, Any]] = []

    def add_check(
        name: str, status: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                "details": details or {},
            }
        )

    def add_block(exc: TimetableRepairOperationError, check_name: str) -> None:
        add_check(check_name, "failed", exc.message, exc.details)
        blocking_reasons.append(
            {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        )

    if not actionable:
        return {
            "status": "not_applicable",
            "checked_at": timezone.now().isoformat(),
            "current_state_valid": False,
            "approve_ready": False,
            "apply_ready": False,
            "checks": [],
            "blocking_reasons": list(decision.get("blocked_reasons") or []),
            "skipped_reason": "candidate_is_not_actionable",
        }

    try:
        placements_by_id = _placements_for_candidate_moves(candidate, lock=False)
        placement = placements_by_id.get(
            int(run.target_placement_id)
        ) or _target_placement_for_preflight(run)
        for scoped_placement in placements_by_id.values():
            _assert_placement_matches_before_snapshot(run, scoped_placement)
        add_check(
            "target_placement_snapshot",
            "passed",
            "Scoped placement snapshot still matches the analysis snapshot.",
            {"placement_count": len(placements_by_id) or 1},
        )
    except TimetableRepairOperationError as exc:
        add_block(exc, "target_placement_snapshot")
        placement = None
        placements_by_id = {}

    if placement is not None:
        planning_scope = (run.request_payload or {}).get("planning_scope") or {}
        moves = _candidate_move_set(candidate, placement=placement)
        rejections = _hard_feasibility_rejections_for_move_set(
            placements_by_id=placements_by_id,
            moves=moves,
            planning_scope=planning_scope,
        )
        rejections.extend(_move_scope_self_overlap_rejections(moves))
        if rejections:
            exc = TimetableRepairOperationError(
                "Candidate is no longer feasible; run a fresh analysis",
                code="REPAIR_CANDIDATE_STALE",
                status=409,
                details={"rejections": rejections},
            )
            add_block(exc, "hard_feasibility")
        else:
            add_check(
                "hard_feasibility",
                "passed",
                "Room, instructor, lock and timetable feasibility still pass for the selected scope.",
            )

    try:
        changes = list(candidate.student_changes.order_by("id"))
        _assert_student_changes_match_current_state(run, candidate, changes)
        add_check(
            "student_assignments",
            "passed",
            "Current student assignments still match the audited proposal.",
            {"student_change_count": len(changes)},
        )
    except TimetableRepairOperationError as exc:
        add_block(exc, "student_assignments")

    current_state_valid = not blocking_reasons
    return {
        "status": "fresh" if current_state_valid else "stale",
        "checked_at": timezone.now().isoformat(),
        "current_state_valid": current_state_valid,
        "approve_ready": bool(decision.get("approve_allowed")) and current_state_valid,
        "apply_ready": bool(decision.get("apply_allowed")) and current_state_valid,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "skipped_reason": "",
    }


def _rollback_readiness(run: TimetableRepairRun) -> dict[str, Any]:
    """Read-only rollback readiness check that mirrors rollback write guards."""

    approval = (
        TimetableRepairApproval.objects.filter(
            run=run,
            status=TimetableRepairApproval.STATUS_APPLIED,
        )
        .select_related("candidate")
        .order_by("-decided_at", "-created_at", "-id")
        .first()
    )
    if approval is None or approval.candidate is None:
        return {
            "status": "not_applicable",
            "checked_at": timezone.now().isoformat(),
            "rollback_ready": False,
            "candidate_id": "",
            "checks": [],
            "blocking_reasons": [],
            "skipped_reason": "no_applied_repair_candidate",
        }

    candidate = approval.candidate
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[dict[str, Any]] = []
    counts = Counter()

    def add_check(
        name: str, status: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                "details": details or {},
            }
        )

    def add_block(
        code: str,
        message: str,
        *,
        check_name: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        add_check(check_name, "failed", message, details)
        blocking_reasons.append(
            {
                "code": code,
                "message": message,
                "details": details or {},
            }
        )

    if run.scenario.status == "published":
        add_block(
            "SCENARIO_PUBLISHED",
            "Cannot rollback a published scenario",
            check_name="scenario_mutable",
        )
    else:
        add_check("scenario_mutable", "passed", "Scenario is still mutable.")

    try:
        placements_by_id = _placements_for_candidate_moves(candidate, lock=False)
        for move in _candidate_move_set(candidate):
            placement = placements_by_id.get(int(move.get("placement_id") or 0))
            if placement is None:
                continue
            _assert_placement_matches_candidate_move(move, placement)
        add_check(
            "target_placement_applied",
            "passed",
            "Scoped placements still match the applied repair candidate.",
            {"placement_count": len(placements_by_id) or 1},
        )
        before_rows = [
            _before_placement_row(run, placement_id) for placement_id in sorted(placements_by_id)
        ]
        add_check(
            "before_snapshot_available",
            "passed",
            "Original placement snapshots are available for rollback.",
            {"restore_to": before_rows},
        )
    except TimetableRepairOperationError as exc:
        add_block(
            exc.code,
            exc.message,
            check_name="target_placement_applied",
            details=exc.details,
        )

    source_tag = _repair_source_tag(run, candidate)
    assignment_block_count = len(blocking_reasons)
    for change in candidate.student_changes.order_by("-id"):
        if change.change_type == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED:
            after_id = _parse_section_id(change.after_section_id)
            if not after_id:
                add_block(
                    "REPAIR_ROLLBACK_CHANGE_INVALID",
                    "Rollback change is missing an applied section",
                    check_name="repair_owned_assignments",
                    details=serialize_student_change(change),
                )
                continue
            row = StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section_id=after_id,
                term_section__scenario_id=run.scenario_id,
            ).first()
            if row is None:
                add_block(
                    "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    "Applied student assignment is missing; manual review is required",
                    check_name="repair_owned_assignments",
                    details=serialize_student_change(change),
                )
                continue
            if row.source != source_tag:
                add_block(
                    "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    "Applied student assignment was modified after repair; manual review is required",
                    check_name="repair_owned_assignments",
                    details={"expected_source": source_tag, "current_source": row.source},
                )
                continue
            counts["newly_registered_removed"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_MOVED:
            before_id = _parse_section_id(change.before_section_id)
            after_id = _parse_section_id(change.after_section_id)
            if not before_id or not after_id:
                add_block(
                    "REPAIR_ROLLBACK_CHANGE_INVALID",
                    "Rollback move is missing a before or after section",
                    check_name="repair_owned_assignments",
                    details=serialize_student_change(change),
                )
                continue
            row = StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section_id=after_id,
                term_section__scenario_id=run.scenario_id,
            ).first()
            if row is None:
                add_block(
                    "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    "Applied student assignment is missing; manual review is required",
                    check_name="repair_owned_assignments",
                    details=serialize_student_change(change),
                )
                continue
            if row.source != source_tag:
                add_block(
                    "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    "Applied student assignment was modified after repair; manual review is required",
                    check_name="repair_owned_assignments",
                    details={"expected_source": source_tag, "current_source": row.source},
                )
                continue
            if StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section_id=before_id,
                term_section__scenario_id=run.scenario_id,
            ).exists():
                add_block(
                    "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    "Rollback destination already exists; manual review is required",
                    check_name="repair_owned_assignments",
                    details=serialize_student_change(change),
                )
                continue
            counts["moved_section_restored"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNCHANGED:
            counts["unchanged"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNRESOLVED:
            counts["unresolved"] += 1

    if len(blocking_reasons) == assignment_block_count:
        add_check(
            "repair_owned_assignments",
            "passed",
            "Repair-owned student assignment changes are still rollback-safe.",
            {"student_changes": dict(counts)},
        )

    rollback_ready = not blocking_reasons
    return {
        "status": "ready" if rollback_ready else "blocked",
        "checked_at": timezone.now().isoformat(),
        "rollback_ready": rollback_ready,
        "candidate_id": candidate.candidate_id,
        "applied_at": approval.decided_at.isoformat() if approval.decided_at else "",
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "student_changes": dict(counts),
        "skipped_reason": "",
    }


def _target_placement_for_preflight(run: TimetableRepairRun) -> SectionPlacement:
    if not run.target_placement_id:
        raise TimetableRepairOperationError(
            "Repair run has no target placement",
            code="REPAIR_TARGET_PLACEMENT_MISSING",
            status=409,
        )
    try:
        return SectionPlacement.objects.select_related("board__scenario", "term_section").get(
            id=run.target_placement_id
        )
    except SectionPlacement.DoesNotExist as exc:
        raise TimetableRepairOperationError(
            "Target placement no longer exists",
            code="REPAIR_TARGET_PLACEMENT_MISSING",
            status=409,
        ) from exc


def _target_placement_for_update(run: TimetableRepairRun) -> SectionPlacement:
    if not run.target_placement_id:
        raise TimetableRepairOperationError(
            "Repair run has no target placement",
            code="REPAIR_TARGET_PLACEMENT_MISSING",
            status=409,
        )
    try:
        return (
            SectionPlacement.objects.select_for_update()
            .select_related("board__scenario", "term_section")
            .get(id=run.target_placement_id)
        )
    except SectionPlacement.DoesNotExist as exc:
        raise TimetableRepairOperationError(
            "Target placement no longer exists",
            code="REPAIR_TARGET_PLACEMENT_MISSING",
            status=409,
        ) from exc


def _placements_for_candidate_moves(
    candidate: TimetableRepairCandidate,
    *,
    lock: bool = False,
) -> dict[int, SectionPlacement]:
    moves = _candidate_move_set(candidate)
    placement_ids = sorted(
        {int(move["placement_id"]) for move in moves if move.get("placement_id")}
    )
    if not placement_ids:
        return {}
    queryset = SectionPlacement.objects.select_related("board__scenario", "term_section")
    if lock:
        queryset = queryset.select_for_update()
    placements = {row.id: row for row in queryset.filter(id__in=placement_ids)}
    missing = [placement_id for placement_id in placement_ids if placement_id not in placements]
    if missing:
        raise TimetableRepairOperationError(
            "One or more scoped placements no longer exist",
            code="REPAIR_TARGET_PLACEMENT_MISSING",
            status=409,
            details={"missing_placement_ids": missing},
        )
    return placements


def _before_placement_row(run: TimetableRepairRun, placement_id: int) -> dict[str, Any]:
    for row in (run.before_snapshot or {}).get("placements", []):
        if int(row.get("placement_id") or 0) == int(placement_id):
            return row
    raise TimetableRepairOperationError(
        "Before snapshot does not contain the target placement",
        code="REPAIR_SNAPSHOT_INCOMPLETE",
        status=409,
    )


def _assert_placement_matches_before_snapshot(
    run: TimetableRepairRun,
    placement: SectionPlacement,
) -> None:
    before = _before_placement_row(run, placement.id)
    current = {
        "term_section_id": placement.term_section_id,
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room or "",
    }
    expected = {
        "term_section_id": before.get("term_section_id"),
        "day": before.get("day"),
        "start_time": before.get("start_time"),
        "end_time": before.get("end_time"),
        "room": before.get("room") or "",
    }
    if current != expected:
        raise TimetableRepairOperationError(
            "Target placement changed after analysis; run a fresh repair analysis",
            code="REPAIR_STALE_PLACEMENT",
            status=409,
            details={"expected": expected, "current": current},
        )


def _assert_placement_matches_candidate(
    candidate: TimetableRepairCandidate,
    placement: SectionPlacement,
) -> None:
    move = next(
        (
            row
            for row in _candidate_move_set(candidate, placement=placement)
            if int(row.get("placement_id") or 0) == int(placement.id)
        ),
        None,
    )
    if move is not None:
        _assert_placement_matches_candidate_move(move, placement)
        return
    current = {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room or "",
    }
    expected = {
        "day": candidate.day,
        "start_time": candidate.start_time,
        "end_time": candidate.end_time,
        "room": candidate.room or "",
    }
    if current != expected:
        raise TimetableRepairOperationError(
            "Target placement changed after repair was applied; manual review is required",
            code="REPAIR_ROLLBACK_STALE_PLACEMENT",
            status=409,
            details={"expected": expected, "current": current},
        )


def _assert_placement_matches_candidate_move(
    move: dict[str, Any],
    placement: SectionPlacement,
) -> None:
    current = {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room or "",
    }
    expected = {
        "day": str(move.get("day") or ""),
        "start_time": str(move.get("start") or ""),
        "end_time": str(move.get("end") or ""),
        "room": str(move.get("room") or ""),
    }
    if current != expected:
        raise TimetableRepairOperationError(
            "Scoped placement changed after repair was applied; manual review is required",
            code="REPAIR_ROLLBACK_STALE_PLACEMENT",
            status=409,
            details={
                "placement_id": placement.id,
                "expected": expected,
                "current": current,
            },
        )


def _candidate_uses_evaluator_assignment(candidate: TimetableRepairCandidate) -> bool:
    exact = (candidate.metrics_json or {}).get("exact_repair") or {}
    solver_domain = exact.get("solver_domain") or {}
    return str(solver_domain.get("assignment_source") or "") == "current_evaluator_assignment"


def _persistent_assignment_count(scenario_id: int) -> int:
    return StudentTermSection.objects.filter(term_section__scenario_id=scenario_id).count()


def _materialize_evaluator_baseline_for_apply(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
) -> dict[str, Any]:
    """Persist the evaluator baseline before applying a generated-scenario repair.

    Generated timetable scenarios can have no ``StudentTermSection`` rows yet.
    The solver still has a real whole-scenario assignment baseline from the
    timetable evaluator.  Before an apply write, materialize that baseline so
    the audited move/new-registration changes update durable rows and rollback
    has something concrete to restore.
    """

    if not _candidate_uses_evaluator_assignment(candidate):
        return {"materialized": False, "reason": "candidate_used_persistent_assignment_source"}
    existing_count = _persistent_assignment_count(run.scenario_id)
    if existing_count:
        return {
            "materialized": False,
            "reason": "persistent_assignments_already_exist",
            "existing_assignment_count": existing_count,
        }

    demand_student_ids = {
        int(row.student_id)
        for row in load_scenario_course_demands(run.scenario_id)
        if row.student_id is not None
    }
    if not demand_student_ids:
        raise TimetableRepairOperationError(
            "Cannot materialize evaluator assignment baseline without scenario demand students",
            code="REPAIR_BASELINE_UNAVAILABLE",
            status=409,
        )
    baseline = build_current_evaluator_assignment_baseline(
        run.scenario_id,
        selected_students=demand_student_ids,
    )
    summary = baseline.get("summary") or {}
    current_by_student_course = baseline.get("current_by_student_course") or {}
    if not summary.get("available") or not current_by_student_course:
        raise TimetableRepairOperationError(
            "Current evaluator assignment baseline is unavailable for apply",
            code="REPAIR_BASELINE_UNAVAILABLE",
            status=409,
            details=summary,
        )

    now = timezone.now().isoformat()
    objects: list[StudentTermSection] = []
    for student_id, courses in sorted(current_by_student_course.items()):
        for term_section_id in sorted(set(int(section_id) for section_id in courses.values())):
            objects.append(
                StudentTermSection(
                    student_id=int(student_id),
                    academic_year=str(run.scenario.academic_year),
                    term=str(run.scenario.term),
                    term_section_id=term_section_id,
                    source=EVALUATOR_BASELINE_SOURCE,
                    created_at=now,
                    updated_at=now,
                )
            )
    StudentTermSection.objects.bulk_create(objects, batch_size=1000, ignore_conflicts=True)
    materialized_count = _persistent_assignment_count(run.scenario_id)
    payload = {
        "materialized": True,
        "source": EVALUATOR_BASELINE_SOURCE,
        "created_assignment_count": len(objects),
        "persistent_assignment_count": materialized_count,
        "summary": summary,
    }
    _log(run, "info", "repair_evaluator_baseline_materialized", payload, candidate=candidate)
    return payload


def _assert_student_changes_match_current_state(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
    changes: list[TimetableRepairStudentChange],
) -> None:
    invalid = [
        change
        for change in changes
        if change.change_type == TimetableRepairStudentChange.CHANGE_LOST
    ]
    if invalid:
        raise TimetableRepairOperationError(
            "Candidate includes lost-course changes and cannot be applied",
            code="REPAIR_EXISTING_LOSS_BLOCKED",
            status=409,
        )
    scenario_id = run.scenario_id
    if _candidate_uses_evaluator_assignment(candidate) and not _persistent_assignment_count(
        scenario_id
    ):
        return
    for change in changes:
        if change.change_type in {
            TimetableRepairStudentChange.CHANGE_UNCHANGED,
            TimetableRepairStudentChange.CHANGE_MOVED,
        }:
            before_id = _parse_section_id(change.before_section_id)
            if not before_id:
                raise TimetableRepairOperationError(
                    "Existing-course change is missing a before section",
                    code="REPAIR_CHANGE_INVALID",
                    status=409,
                    details=serialize_student_change(change),
                )
            if not StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section_id=before_id,
                term_section__scenario_id=scenario_id,
            ).exists():
                raise TimetableRepairOperationError(
                    "Student assignment changed after analysis; run a fresh repair analysis",
                    code="REPAIR_STALE_STUDENT_ASSIGNMENT",
                    status=409,
                    details=serialize_student_change(change),
                )
            if change.change_type == TimetableRepairStudentChange.CHANGE_MOVED:
                after_id = _parse_section_id(change.after_section_id)
                if not after_id:
                    raise TimetableRepairOperationError(
                        "Moved-section change is missing an after section",
                        code="REPAIR_CHANGE_INVALID",
                        status=409,
                        details=serialize_student_change(change),
                    )
                if StudentTermSection.objects.filter(
                    student_id=change.student_id,
                    term_section_id=after_id,
                    term_section__scenario_id=scenario_id,
                ).exists():
                    raise TimetableRepairOperationError(
                        "Destination assignment already exists for a proposed move",
                        code="REPAIR_STALE_STUDENT_ASSIGNMENT",
                        status=409,
                        details=serialize_student_change(change),
                    )
        elif change.change_type == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED:
            after_id = _parse_section_id(change.after_section_id)
            if not after_id:
                raise TimetableRepairOperationError(
                    "New registration change is missing an after section",
                    code="REPAIR_CHANGE_INVALID",
                    status=409,
                    details=serialize_student_change(change),
                )
            if StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section__scenario_id=scenario_id,
                term_section__course_key=change.course_key,
            ).exists():
                raise TimetableRepairOperationError(
                    "Student already has this course after analysis; run a fresh repair analysis",
                    code="REPAIR_STALE_STUDENT_ASSIGNMENT",
                    status=409,
                    details=serialize_student_change(change),
                )


def _apply_student_changes(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
    changes: list[TimetableRepairStudentChange],
    source_tag: str,
) -> dict[str, int]:
    counts = Counter()
    for change in changes:
        if change.change_type == TimetableRepairStudentChange.CHANGE_MOVED:
            before_id = _parse_section_id(change.before_section_id)
            after_id = _parse_section_id(change.after_section_id)
            row = StudentTermSection.objects.select_for_update().get(
                student_id=change.student_id,
                term_section_id=before_id,
                term_section__scenario_id=run.scenario_id,
            )
            details = dict(change.details_json or {})
            details["applied"] = {
                "academic_year": row.academic_year,
                "term": row.term,
                "previous_source": row.source,
                "source": source_tag,
            }
            row.term_section_id = int(after_id)
            row.source = source_tag
            row.save(update_fields=["term_section", "source", "updated_at"])
            change.details_json = details
            change.save(update_fields=["details_json"])
            counts["moved_section"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED:
            after_id = _parse_section_id(change.after_section_id)
            StudentTermSection.objects.create(
                student_id=change.student_id,
                academic_year=str(run.scenario.academic_year),
                term=str(run.scenario.term),
                term_section_id=int(after_id),
                source=source_tag,
            )
            details = dict(change.details_json or {})
            details["applied"] = {
                "academic_year": str(run.scenario.academic_year),
                "term": str(run.scenario.term),
                "source": source_tag,
            }
            change.details_json = details
            change.save(update_fields=["details_json"])
            counts["newly_registered"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNCHANGED:
            counts["unchanged"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNRESOLVED:
            counts["unresolved"] += 1
    return dict(counts)


def _rollback_student_changes(
    run: TimetableRepairRun,
    candidate: TimetableRepairCandidate,
    changes: list[TimetableRepairStudentChange],
    source_tag: str,
) -> dict[str, int]:
    counts = Counter()
    for change in changes:
        if change.change_type == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED:
            after_id = _parse_section_id(change.after_section_id)
            row = _repair_owned_assignment(run, change.student_id, after_id, source_tag)
            row.delete()
            counts["newly_registered_removed"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_MOVED:
            before_id = _parse_section_id(change.before_section_id)
            after_id = _parse_section_id(change.after_section_id)
            row = _repair_owned_assignment(run, change.student_id, after_id, source_tag)
            if StudentTermSection.objects.filter(
                student_id=change.student_id,
                term_section_id=before_id,
                term_section__scenario_id=run.scenario_id,
            ).exists():
                raise TimetableRepairOperationError(
                    "Rollback destination already exists; manual review is required",
                    code="REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
                    status=409,
                    details=serialize_student_change(change),
                )
            applied = (change.details_json or {}).get("applied") or {}
            row.term_section_id = int(before_id)
            row.source = str(applied.get("previous_source") or "manual")
            row.save(update_fields=["term_section", "source", "updated_at"])
            counts["moved_section_restored"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNCHANGED:
            counts["unchanged"] += 1
        elif change.change_type == TimetableRepairStudentChange.CHANGE_UNRESOLVED:
            counts["unresolved"] += 1
    return dict(counts)


def _repair_owned_assignment(
    run: TimetableRepairRun,
    student_id: int,
    term_section_id: int | None,
    source_tag: str,
) -> StudentTermSection:
    if not term_section_id:
        raise TimetableRepairOperationError(
            "Rollback change is missing an applied section",
            code="REPAIR_ROLLBACK_CHANGE_INVALID",
            status=409,
        )
    try:
        row = StudentTermSection.objects.select_for_update().get(
            student_id=student_id,
            term_section_id=term_section_id,
            term_section__scenario_id=run.scenario_id,
        )
    except StudentTermSection.DoesNotExist as exc:
        raise TimetableRepairOperationError(
            "Applied student assignment is missing; manual review is required",
            code="REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
            status=409,
        ) from exc
    if row.source != source_tag:
        raise TimetableRepairOperationError(
            "Applied student assignment was modified after repair; manual review is required",
            code="REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT",
            status=409,
            details={"expected_source": source_tag, "current_source": row.source},
        )
    return row


def _repair_source_tag(run: TimetableRepairRun, candidate: TimetableRepairCandidate) -> str:
    return f"timetable_repair:{run.id}:{candidate.candidate_id}"


def _parse_section_id(value: str) -> int | None:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _student_outcome_rows(
    placement_id: int, quick_rows: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        preview = preview_placement_student_outcome_candidates(
            placement_id,
            candidate_moves=quick_rows,
        )
    except Exception:
        return {}
    rows = {}
    for row in preview.get("candidates", []):
        rows[(str(row.get("day") or ""), str(row.get("start") or ""))] = row
    return rows


def _format_time_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _placement_duration_minutes(placement: SectionPlacement) -> int:
    return max(
        0,
        _parse_time_minutes(placement.end_time) - _parse_time_minutes(placement.start_time),
    )


def _placement_meeting_kind(placement: SectionPlacement) -> str:
    return "lab" if _placement_duration_minutes(placement) > 80 else "lect"


def _placements_for_move_scope(
    placement: SectionPlacement,
    move_scope: str,
) -> tuple[list[SectionPlacement], list[SectionPlacement]]:
    move_scope = _normalise_move_scope(move_scope)
    scoped = list(
        SectionPlacement.objects.filter(
            board_id=placement.board_id,
            term_section_id=placement.term_section_id,
        )
        .select_related("board__scenario", "term_section")
        .order_by("day", "start_time", "id")
    )
    if not scoped:
        return [placement], []

    if move_scope == MOVE_SCOPE_SINGLE_SESSION:
        included = [row for row in scoped if row.id == placement.id] or [placement]
    elif move_scope == MOVE_SCOPE_LECTURES_ONLY:
        included = [row for row in scoped if _placement_meeting_kind(row) == "lect"]
        if not included:
            included = [row for row in scoped if row.id == placement.id] or [placement]
    else:
        included = list(scoped)

    included_ids = {row.id for row in included}
    excluded = [row for row in scoped if row.id not in included_ids]
    return included, excluded


def _build_move_scope_payload(
    *,
    anchor: SectionPlacement,
    scoped_placements: list[SectionPlacement],
    excluded_placements: list[SectionPlacement],
    move_scope: str,
    day: str,
    start: str,
    end: str,
    planning_scope: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    day = str(day or "").upper()
    anchor_day_index = REPAIR_DAY_INDEX.get(str(anchor.day or "").upper())
    target_day_index = REPAIR_DAY_INDEX.get(day)
    anchor_start = _parse_time_minutes(anchor.start_time)
    target_start = _parse_time_minutes(start)
    if anchor_day_index is None or target_day_index is None:
        return (
            _empty_move_scope_payload(anchor, move_scope, scoped_placements, excluded_placements),
            [
                {
                    "code": "MOVE_SCOPE_INVALID_DAY",
                    "message": "Move scope cannot map the selected session to the target day",
                    "details": {"day": day},
                }
            ],
            {},
        )

    day_delta = target_day_index - anchor_day_index
    time_delta = target_start - anchor_start
    moves: list[dict[str, Any]] = []
    room_payloads: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    by_id = {row.id: row for row in scoped_placements}

    for row in scoped_placements:
        source_day_index = REPAIR_DAY_INDEX.get(str(row.day or "").upper())
        if source_day_index is None:
            rejections.append(
                {
                    "code": "MOVE_SCOPE_SOURCE_DAY_INVALID",
                    "message": "A session in this move scope has an invalid current day",
                    "details": {"placement_id": row.id, "day": row.day},
                }
            )
            continue
        new_day_index = source_day_index + day_delta
        if new_day_index < 0 or new_day_index >= len(REPAIR_WEEKDAYS):
            rejections.append(
                {
                    "code": "MOVE_SCOPE_COMPANION_OUT_OF_WEEK",
                    "message": "A scoped companion session would move outside the teaching week",
                    "details": {
                        "placement_id": row.id,
                        "current_day": row.day,
                        "day_delta": day_delta,
                    },
                }
            )
            continue
        source_start = _parse_time_minutes(row.start_time)
        source_end = _parse_time_minutes(row.end_time)
        new_start = source_start + time_delta
        new_end = source_end + time_delta
        if new_start < 0 or new_end > 24 * 60 or new_end <= new_start:
            rejections.append(
                {
                    "code": "MOVE_SCOPE_COMPANION_TIME_INVALID",
                    "message": "A scoped companion session would move outside a valid day time",
                    "details": {
                        "placement_id": row.id,
                        "current_start": row.start_time,
                        "current_end": row.end_time,
                        "time_delta": time_delta,
                    },
                }
            )
            continue
        move_day = REPAIR_WEEKDAYS[new_day_index]
        move_start = _format_time_minutes(new_start)
        move_end = _format_time_minutes(new_end)
        room, room_reasons, room_payload = _select_room_for_candidate(
            row,
            move_day,
            move_start,
            move_end,
            planning_scope=planning_scope,
        )
        room_payloads.append(
            {
                "placement_id": row.id,
                "room": room,
                "room_payload": room_payload,
                "room_reasons": room_reasons,
            }
        )
        moves.append(
            {
                "placement_id": row.id,
                "term_section_id": row.term_section_id,
                "day": move_day,
                "start": move_start,
                "end": move_end,
                "room": room,
                "kind": _placement_meeting_kind(row),
                "is_anchor": row.id == anchor.id,
                "before": {
                    "day": row.day,
                    "start": row.start_time,
                    "end": row.end_time,
                    "room": row.room or "",
                },
                "room_reasons": room_reasons,
            }
        )

    if moves:
        rejections.extend(
            _hard_feasibility_rejections_for_move_set(
                placements_by_id=by_id,
                moves=moves,
                planning_scope=planning_scope,
            )
        )
    rejections.extend(_move_scope_self_overlap_rejections(moves))

    payload = {
        "scope": _normalise_move_scope(move_scope),
        "label": MOVE_SCOPE_LABELS[_normalise_move_scope(move_scope)],
        "anchor_placement_id": anchor.id,
        "included_placement_ids": [row.id for row in scoped_placements],
        "excluded_placement_ids": [row.id for row in excluded_placements],
        "included_count": len(scoped_placements),
        "excluded_count": len(excluded_placements),
        "day_delta": day_delta,
        "time_delta_minutes": time_delta,
        "moves": moves,
        "summary": {
            "lecture_sessions": sum(1 for move in moves if move.get("kind") == "lect"),
            "lab_sessions": sum(1 for move in moves if move.get("kind") == "lab"),
        },
    }
    primary_room_payload = next(
        (row["room_payload"] for row in room_payloads if row["placement_id"] == anchor.id),
        room_payloads[0]["room_payload"] if room_payloads else {},
    )
    return payload, rejections, primary_room_payload


def _empty_move_scope_payload(
    anchor: SectionPlacement,
    move_scope: str,
    scoped_placements: list[SectionPlacement],
    excluded_placements: list[SectionPlacement],
) -> dict[str, Any]:
    move_scope = _normalise_move_scope(move_scope)
    return {
        "scope": move_scope,
        "label": MOVE_SCOPE_LABELS[move_scope],
        "anchor_placement_id": anchor.id,
        "included_placement_ids": [row.id for row in scoped_placements],
        "excluded_placement_ids": [row.id for row in excluded_placements],
        "included_count": len(scoped_placements),
        "excluded_count": len(excluded_placements),
        "moves": [],
    }


def _hard_feasibility_rejections_for_move_set(
    *,
    placements_by_id: dict[int, SectionPlacement],
    moves: list[dict[str, Any]],
    planning_scope: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    moving_ids = {int(move.get("placement_id") or 0) for move in moves}
    rejections: list[dict[str, Any]] = []
    for move in moves:
        placement_id = int(move.get("placement_id") or 0)
        placement = placements_by_id.get(placement_id)
        if placement is None:
            rejections.append(
                {
                    "code": "MOVE_SCOPE_PLACEMENT_MISSING",
                    "message": "A scoped placement no longer exists",
                    "details": {"placement_id": placement_id},
                }
            )
            continue
        scoped_rejections = hard_feasibility_rejections(
            placement,
            day=str(move.get("day") or ""),
            start_time=str(move.get("start") or ""),
            end_time=str(move.get("end") or ""),
            room=str(move.get("room") or ""),
            room_reasons=list(move.get("room_reasons") or []),
            planning_scope=planning_scope,
        )
        for reason in scoped_rejections:
            filtered = _filter_move_set_self_conflict_reason(reason, moving_ids)
            if filtered:
                filtered = dict(filtered)
                details = dict(filtered.get("details") or {})
                details.setdefault("placement_id", placement_id)
                filtered["details"] = details
                rejections.append(filtered)
    return rejections


def _filter_move_set_self_conflict_reason(
    reason: dict[str, Any],
    moving_ids: set[int],
) -> dict[str, Any] | None:
    if reason.get("code") != "TIME_OR_INSTRUCTOR_CONFLICT":
        return reason
    details = dict(reason.get("details") or {})

    def keep(row: dict[str, Any]) -> bool:
        try:
            return int(row.get("id") or 0) not in moving_ids
        except (TypeError, ValueError):
            return True

    overlaps = [row for row in details.get("overlaps", []) if keep(row)]
    instructors = [row for row in details.get("instructor_clashes", []) if keep(row)]
    critical_overlaps = [row for row in overlaps if row.get("severity") == "critical"]
    if not critical_overlaps and not instructors:
        return None
    return {
        **reason,
        "details": {
            **details,
            "overlaps": overlaps,
            "instructor_clashes": instructors,
        },
    }


def _move_scope_self_overlap_rejections(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rejections: list[dict[str, Any]] = []
    for left, right in combinations(moves, 2):
        if str(left.get("day") or "") != str(right.get("day") or ""):
            continue
        if not _intervals_overlap(
            _parse_time_minutes(str(left.get("start") or "")),
            _parse_time_minutes(str(left.get("end") or "")),
            _parse_time_minutes(str(right.get("start") or "")),
            _parse_time_minutes(str(right.get("end") or "")),
        ):
            continue
        rejections.append(
            {
                "code": "MOVE_SCOPE_SELF_OVERLAP",
                "message": "The scoped section sessions would overlap each other",
                "details": {
                    "left_placement_id": left.get("placement_id"),
                    "right_placement_id": right.get("placement_id"),
                    "day": left.get("day"),
                },
            }
        )
    return rejections


def _prepare_repair_candidate_rows(
    placement: SectionPlacement,
    *,
    limits: dict[str, int],
    planning_scope: dict[str, Any] | None = None,
    move_scope: str | None = None,
) -> list[dict[str, Any]]:
    """Scan beyond the visible candidate count and keep administratively useful rows."""

    move_scope = _normalise_move_scope(move_scope)
    scoped_placements, excluded_placements = _placements_for_move_scope(placement, move_scope)
    quick_preview = preview_placement_slot_candidates(placement.id)
    source_rows = list(quick_preview.get("candidates", []))
    max_candidates = int(limits.get("max_candidates", DEFAULT_LIMITS["max_candidates"]))
    scan_limit = _repair_candidate_scan_limit(max_candidates, len(source_rows))
    prepared: list[dict[str, Any]] = []

    for source_index, row in enumerate(source_rows[:scan_limit], start=1):
        day = str(row.get("day") or "")
        start = str(row.get("start") or "")
        end = str(row.get("end") or "")
        move_scope_payload, rejections, room_payload = _build_move_scope_payload(
            anchor=placement,
            scoped_placements=scoped_placements,
            excluded_placements=excluded_placements,
            move_scope=move_scope,
            day=day,
            start=start,
            end=end,
            planning_scope=planning_scope,
        )
        anchor_move = next(
            (
                move
                for move in move_scope_payload.get("moves", [])
                if int(move.get("placement_id") or 0) == int(placement.id)
            ),
            {},
        )
        selected_room = str(anchor_move.get("room") or "")
        source_row = {
            **row,
            "moves": [
                {
                    "placement_id": move.get("placement_id"),
                    "day": move.get("day"),
                    "start": move.get("start"),
                    "end": move.get("end"),
                    "room": move.get("room"),
                    "kind": move.get("kind"),
                }
                for move in move_scope_payload.get("moves", [])
            ],
            "move_scope": move_scope,
        }
        prepared.append(
            {
                "source_row": source_row,
                "source_index": source_index,
                "day": day,
                "start": start,
                "end": end,
                "selected_room": selected_room,
                "room_payload": room_payload,
                "rejections": rejections,
                "move_scope_payload": move_scope_payload,
                "generation": {
                    "source_rank": row.get("rank") or source_index,
                    "source_candidate_count": len(source_rows),
                    "scan_limit": scan_limit,
                    "hard_rejection_count": len(rejections),
                    "room_policy_clean": bool(
                        room_payload.get("policy_clean") or room_payload.get("is_online")
                    ),
                    "move_scope": {
                        "scope": move_scope,
                        "label": MOVE_SCOPE_LABELS[move_scope],
                        "included_count": int(move_scope_payload.get("included_count") or 0),
                        "excluded_count": int(move_scope_payload.get("excluded_count") or 0),
                    },
                    "planning_scope": {
                        "active_plan_filter": (planning_scope or {}).get(
                            "active_plan_filter", "ALL"
                        ),
                        "filter_applied": bool((planning_scope or {}).get("filter_applied")),
                        "ignored_overlap_count": int(
                            (planning_scope or {}).get("ignored_overlap_count") or 0
                        ),
                    },
                },
            }
        )

    prepared.sort(key=_prepared_candidate_sort_key)
    return prepared


def _repair_candidate_scan_limit(max_candidates: int, available_count: int) -> int:
    if available_count <= 0:
        return 0
    desired = max(max_candidates, (max_candidates * 4), max_candidates + 10)
    return min(available_count, desired, 80)


def _select_repair_candidate_rows(
    prepared_rows: list[dict[str, Any]],
    *,
    outcome_rows: dict[tuple[str, str], dict[str, Any]],
    max_candidates: int,
) -> list[dict[str, Any]]:
    """Choose exact-solver candidates by real student outcome first.

    The slot preview can produce many administratively clean placements.  The
    exact CP-SAT repair budget is much smaller, so the preselection step must be
    driven by the same thing the timetable builder is trying to improve:
    students who remain unresolved after whole-scenario assignment.
    """

    limit = max(1, int(max_candidates or DEFAULT_LIMITS["max_candidates"]))
    selected = sorted(
        prepared_rows,
        key=lambda prepared: _prepared_candidate_student_outcome_sort_key(
            prepared,
            outcome_rows.get(
                (str(prepared.get("day") or ""), str(prepared.get("start") or "")), {}
            ),
        ),
    )[:limit]
    for selection_rank, prepared in enumerate(selected, start=1):
        generation = dict(prepared.get("generation") or {})
        generation["student_outcome_preselection"] = {
            "enabled": True,
            "selection_rank": selection_rank,
            "candidate_budget": limit,
            "strategy": "actual_unresolved_students_first",
            "outcome_available": bool(
                outcome_rows.get(
                    (str(prepared.get("day") or ""), str(prepared.get("start") or "")),
                    {},
                )
            ),
        }
        prepared["generation"] = generation
    return selected


def _prepared_candidate_student_outcome_sort_key(
    prepared: dict[str, Any],
    outcome_row: dict[str, Any],
) -> tuple:
    row = prepared["source_row"]
    rejections = prepared.get("rejections") or []
    room_payload = prepared.get("room_payload") or {}
    outcome = outcome_row.get("student_outcome") or {}
    outcome_available = bool(outcome)
    return (
        1 if rejections else 0,
        0 if outcome_available else 1,
        int(outcome.get("blocked_students_delta") or 0),
        int(outcome.get("unresolved_course_delta") or 0),
        -int(outcome.get("newly_unblocked_student_count") or 0),
        -int(outcome.get("improved_student_count") or 0),
        int(outcome.get("newly_blocked_student_count") or 0),
        int(outcome.get("worsened_student_count") or 0),
        max(0, int(outcome.get("actual_clash_delta") or 0)),
        0 if room_payload.get("policy_clean") or room_payload.get("is_online") else 1,
        len(rejections),
        int(row.get("critical_count") or 0),
        int(row.get("warning_count") or 0),
        int(row.get("student_affected_count") or 0),
        int(row.get("impact_score") or 0),
        int(row.get("rank") or prepared["source_index"]),
        prepared["day"],
        prepared["start"],
    )


def _prepared_candidate_sort_key(prepared: dict[str, Any]) -> tuple:
    row = prepared["source_row"]
    rejections = prepared["rejections"]
    room_payload = prepared["room_payload"]
    return (
        1 if rejections else 0,
        0 if room_payload.get("policy_clean") or room_payload.get("is_online") else 1,
        len(rejections),
        int(row.get("critical_count") or 0),
        int(row.get("warning_count") or 0),
        int(row.get("student_affected_count") or 0),
        int(row.get("impact_score") or 0),
        int(row.get("rank") or prepared["source_index"]),
        prepared["day"],
        prepared["start"],
    )


def _blocked_target_ineligibility(
    eligibility_context: Any,
    *,
    student_id: int,
    course_key: str,
    section_ids: list[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section_id in section_ids:
        policy = getattr(eligibility_context, "sections", {}).get(int(section_id))
        reasons = repair_section_id_ineligibility_reasons(
            eligibility_context,
            student_id=student_id,
            course_key=course_key,
            section_id=int(section_id),
            is_new_course=True,
        )
        if reasons:
            rows.append(
                {
                    "term_section_id": int(section_id),
                    "section": getattr(policy, "section_label", ""),
                    "reasons": reasons,
                }
            )
    return rows


def _diagnose_unresolved_blocked_student(
    *,
    student_id: int,
    target_course: str,
    section_by_id: dict[int, Any],
    target_option_ids: list[int],
    ineligible_sections: list[dict[str, Any]],
    chosen: dict[tuple[int, str], int],
    section_meetings: dict[int, list[dict[str, str]]],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    chosen_count_by_section: Counter[int],
) -> dict[str, Any]:
    if not target_option_ids:
        if ineligible_sections:
            return {
                "code": "NO_ELIGIBLE_TARGET_SECTION",
                "message": "No target section satisfies eligibility and protection rules",
                "ineligible_summary": _summarise_ineligible_sections(ineligible_sections),
                "ineligible_samples": ineligible_sections[:5],
            }
        return {
            "code": "NO_TARGET_SECTION_OPTIONS",
            "message": "No target course sections were available in the affected component",
        }

    option_diagnostics: list[dict[str, Any]] = []
    capacity_blocked = 0
    clash_blocked = 0
    for section_id in target_option_ids:
        remaining = _remaining_repair_capacity(
            section_id,
            capacity_by_section=capacity_by_section,
            fixed_occupancy_by_section=fixed_occupancy_by_section,
            chosen_count_by_section=chosen_count_by_section,
        )
        clash_rows = _student_target_clashes(
            student_id=student_id,
            target_course=target_course,
            target_section_id=section_id,
            chosen=chosen,
            section_by_id=section_by_id,
            section_meetings=section_meetings,
        )
        if remaining <= 0:
            capacity_blocked += 1
        if clash_rows:
            clash_blocked += 1
        option_diagnostics.append(
            {
                "term_section_id": section_id,
                "section": _section_summary(section_by_id.get(section_id)),
                "remaining_capacity_after_solution": remaining,
                "clashes": clash_rows,
            }
        )

    if capacity_blocked == len(target_option_ids):
        code = "NO_CAPACITY_AFTER_REPAIR"
        message = "All eligible target sections are full after protecting existing registrations"
    elif clash_blocked == len(target_option_ids):
        code = "TIMETABLE_CLASH_WITH_PROTECTED_COURSES"
        message = "Every eligible target section clashes with the student's protected courses"
    elif capacity_blocked + clash_blocked >= len(target_option_ids):
        code = "CAPACITY_OR_TIMETABLE_BLOCKED"
        message = "Eligible target sections are blocked by capacity or timetable clashes"
    else:
        code = "CONSERVATIVE_SOLVER_NOT_SELECTED"
        message = "The conservative solver could not include this student at the optimum"
    return {
        "code": code,
        "message": message,
        "eligible_target_section_count": len(target_option_ids),
        "option_diagnostics": option_diagnostics[:10],
    }


def _remaining_repair_capacity(
    section_id: int,
    *,
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    chosen_count_by_section: Counter[int],
) -> int:
    return (
        int(capacity_by_section.get(section_id, 0))
        - int(fixed_occupancy_by_section.get(section_id, 0))
        - int(chosen_count_by_section.get(section_id, 0))
    )


def _student_target_clashes(
    *,
    student_id: int,
    target_course: str,
    target_section_id: int,
    chosen: dict[tuple[int, str], int],
    section_by_id: dict[int, Any],
    section_meetings: dict[int, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    target_meetings = section_meetings.get(target_section_id, [])
    rows: list[dict[str, Any]] = []
    for (sid, course_key), section_id in sorted(chosen.items()):
        if sid != student_id or course_key == target_course:
            continue
        if _section_meetings_overlap(target_meetings, section_meetings.get(section_id, [])):
            rows.append(
                {
                    "course_key": course_key,
                    "term_section_id": section_id,
                    "section": _section_summary(section_by_id.get(section_id)),
                }
            )
    return rows


def _summarise_ineligible_sections(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for reason in row.get("reasons", []):
            counts[str(reason.get("code") or "UNKNOWN")] += 1
    return dict(sorted(counts.items()))


def _unresolved_diagnostic_summary(
    unresolved_diagnostics: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    counts = Counter(
        str(reason.get("code") or "UNKNOWN") for reason in unresolved_diagnostics.values()
    )
    return {
        "reason_counts": dict(sorted(counts.items())),
        "students": [
            {"student_id": sid, **reason}
            for sid, reason in sorted(unresolved_diagnostics.items())[:50]
        ],
        "students_truncated": len(unresolved_diagnostics) > 50,
    }


def _change_field(change: TimetableRepairStudentChange | dict[str, Any], name: str, default=None):
    if isinstance(change, dict):
        return change.get(name, default)
    return getattr(change, name, default)


def _cascade_repair_summary(
    change_rows: list[TimetableRepairStudentChange | dict[str, Any]],
    *,
    target_course: str,
) -> dict[str, Any]:
    actionable_types = {
        TimetableRepairStudentChange.CHANGE_MOVED,
        TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
        TimetableRepairStudentChange.CHANGE_LOST,
    }
    existing_move_rows = [
        row
        for row in change_rows
        if _change_field(row, "change_type") == TimetableRepairStudentChange.CHANGE_MOVED
    ]
    new_rows = [
        row
        for row in change_rows
        if _change_field(row, "change_type") == TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED
    ]
    actionable = [
        row for row in change_rows if _change_field(row, "change_type") in actionable_types
    ]
    by_student: dict[int, list[TimetableRepairStudentChange | dict[str, Any]]] = defaultdict(list)
    for row in actionable:
        by_student[int(_change_field(row, "student_id", 0) or 0)].append(row)

    changed_courses_by_student = {
        sid: sorted(
            {
                str(_change_field(row, "course_key") or "")
                for row in rows
                if _change_field(row, "course_key")
            }
        )
        for sid, rows in by_student.items()
    }
    multi_course_students = sorted(
        sid for sid, courses in changed_courses_by_student.items() if len(courses) > 1
    )
    touched_courses = sorted(
        {
            str(_change_field(row, "course_key") or "")
            for row in actionable
            if _change_field(row, "course_key")
        }
    )
    existing_move_courses = sorted(
        {
            str(_change_field(row, "course_key") or "")
            for row in existing_move_rows
            if _change_field(row, "course_key")
        }
    )
    max_changed_courses = max(
        (len(courses) for courses in changed_courses_by_student.values()), default=0
    )

    return {
        "target_course": target_course,
        "requires_multi_course_cascade": bool(multi_course_students),
        "touched_courses": touched_courses,
        "touched_course_count": len(touched_courses),
        "existing_move_courses": existing_move_courses,
        "existing_move_course_count": len(existing_move_courses),
        "students_with_changes": len(by_student),
        "students_with_existing_section_moves": len(
            {int(_change_field(row, "student_id", 0) or 0) for row in existing_move_rows}
        ),
        "multi_course_student_count": len(multi_course_students),
        "multi_course_student_ids": multi_course_students[:50],
        "multi_course_student_ids_truncated": len(multi_course_students) > 50,
        "max_changed_courses_per_student": max_changed_courses,
        "existing_section_move_count": len(existing_move_rows),
        "new_registration_count": len(new_rows),
        "required_change_count": len(actionable),
        "required_change_samples": [
            {
                "student_id": _change_field(row, "student_id"),
                "course_key": _change_field(row, "course_key", ""),
                "change_type": _change_field(row, "change_type", ""),
                "before_section_id": _change_field(row, "before_section_id", ""),
                "after_section_id": _change_field(row, "after_section_id", ""),
            }
            for row in actionable[:50]
        ],
    }


def _select_room_for_candidate(
    placement: SectionPlacement,
    day: str,
    start: str,
    end: str,
    *,
    planning_scope: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    if OnlineCourseLookup().is_online_course_for_board(
        placement.board,
        placement.term_section.course_code,
    ):
        return "", [], {"is_online": True, "selected_room": ""}

    preview = preview_placement_room_candidates(
        placement.id,
        day=day,
        start_time=start,
        end_time=end,
        ignore_overlap_term_section_ids=set(
            (planning_scope or {}).get("ignore_overlap_term_section_ids") or []
        ),
    )
    clean = [
        room
        for room in preview.get("candidates", [])
        if room.get("policy_clean") and room.get("available")
    ]
    if clean:
        selected = clean[0]
        return (
            str(selected["room_code"]),
            [],
            {
                "is_online": False,
                "selected_room": selected["room_code"],
                "policy_clean": True,
                "capacity": selected.get("capacity"),
                "room_type": selected.get("room_type"),
            },
        )
    scoped_rooms = [
        room
        for room in preview.get("candidates", [])
        if room.get("department_fit") and room.get("fits_gender")
    ]
    if scoped_rooms:
        selected = scoped_rooms[0]
        # Temporary experiment: do not block candidate repair only because no
        # policy-clean room exists.  For now, pick a room in the same department
        # pool and gender/section side so the student repair solver can test
        # whether reassignment improves unresolved students.
        return (
            str(selected["room_code"]),
            [],
            {
                "is_online": False,
                "selected_room": selected["room_code"],
                "policy_clean": False,
                "experimental_room_policy_relaxed": True,
                "room_scope_only": True,
                "capacity": selected.get("capacity"),
                "room_type": selected.get("room_type"),
                "department_fit": selected.get("department_fit"),
                "fits_gender": selected.get("fits_gender"),
                "available": selected.get("available"),
                "reasons": selected.get("reasons", []),
            },
        )
    best_available = next(
        (room for room in preview.get("candidates", []) if room.get("available")),
        None,
    )
    details = {
        "is_online": False,
        "selected_room": "",
        "policy_clean": False,
        "available_room_count": preview.get("summary", {}).get("available", 0),
        "clean_room_count": preview.get("summary", {}).get("clean", 0),
        "nearest_room": best_available.get("room_code") if best_available else "",
        "nearest_reasons": best_available.get("reasons", []) if best_available else [],
    }
    return (
        "",
        [
            {
                "code": "NO_POLICY_CLEAN_ROOM",
                "message": "No clean room is available",
                "details": details,
            }
        ],
        details,
    )


def _candidate_explanation(
    row: dict[str, Any],
    selected_room: str,
    rejections: list[dict[str, Any]],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": f"{row.get('day')} {row.get('start')}-{row.get('end')}",
        "selected_room": selected_room,
        "status_text": (
            "Rejected before solver"
            if rejections
            else "Hard-feasible for future conservative repair solver"
        ),
        "quick_evidence": row.get("evidence", []),
        "student_outcome_badge": outcome.get("badge", ""),
    }


def _candidate_field(candidate: TimetableRepairCandidate | dict[str, Any], name: str, default=None):
    if isinstance(candidate, dict):
        aliases = {
            "metrics_json": "metrics",
            "explanation_json": "explanation",
        }
        return candidate.get(aliases.get(name, name), default)
    return getattr(candidate, name, default)


def _candidate_draft_log(
    candidate: dict[str, Any],
    level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    candidate.setdefault("solver_logs", []).append(
        {
            "level": level,
            "message": message,
            "payload": payload,
        }
    )


def _attach_candidate_evaluation_runtime(
    candidate: dict[str, Any],
    *,
    candidate_runtime_ms: int,
) -> None:
    metrics = dict(candidate.get("metrics") or {})
    evaluation = dict(metrics.get("evaluation") or {})
    evaluation["candidate_runtime_ms"] = int(candidate_runtime_ms)
    exact = metrics.get("exact_repair") or {}
    if exact:
        evaluation["solver_runtime_ms"] = int(exact.get("runtime_ms") or 0)
        evaluation["solver_strategy"] = exact.get("solver_strategy") or ""
        evaluation["solver_status"] = exact.get("solver_status") or candidate.get("solver_status")
    else:
        evaluation["solver_runtime_ms"] = 0
        evaluation["solver_status"] = candidate.get("solver_status")
    metrics["evaluation"] = evaluation
    candidate["metrics"] = metrics


def _repair_candidate_rank_key(candidate: TimetableRepairCandidate | dict[str, Any]) -> tuple:
    return tuple(_repair_candidate_ranking_diagnostics(candidate, None)["rank_key"])


def _repair_candidate_ranking_diagnostics(
    candidate: TimetableRepairCandidate | dict[str, Any],
    score_rank: int | None,
) -> dict[str, Any]:
    metrics = _candidate_field(candidate, "metrics_json", {}) or {}
    exact = metrics.get("exact_repair") or {}
    solver_status = str(_candidate_field(candidate, "solver_status", "") or "")
    day = str(_candidate_field(candidate, "day", "") or "")
    start_time = str(_candidate_field(candidate, "start_time", "") or "")
    status = str(_candidate_field(candidate, "status", "") or "")
    if exact.get("enabled") and solver_status in {"optimal", "feasible"}:
        rank_key = (
            int(exact.get("existing_lost") or 0),
            int(exact.get("unresolved_blocked") or 0),
            -int(exact.get("blocked_recovered") or 0),
            -int(exact.get("requested_courses_recovered") or 0),
            int(exact.get("students_moved") or 0),
            int(exact.get("section_changes") or 0),
            int((exact.get("timetable_quality") or {}).get("penalty") or 0),
            day,
            start_time,
        )
        return {
            "eligible": True,
            "score_rank": score_rank,
            "rank_key": list(rank_key),
            "strategy": "exact_repair_lexicographic",
            "primary_reason": _repair_rank_primary_reason(exact),
            "criteria": [
                {
                    "name": "protect_existing",
                    "sense": "min",
                    "value": int(exact.get("existing_lost") or 0),
                },
                {
                    "name": "minimize_unresolved",
                    "sense": "min",
                    "value": int(exact.get("unresolved_blocked") or 0),
                },
                {
                    "name": "recover_blocked",
                    "sense": "max",
                    "value": int(exact.get("blocked_recovered") or 0),
                },
                {
                    "name": "recover_requested_courses",
                    "sense": "max",
                    "value": int(exact.get("requested_courses_recovered") or 0),
                },
                {
                    "name": "minimize_moved_students",
                    "sense": "min",
                    "value": int(exact.get("students_moved") or 0),
                },
                {
                    "name": "minimize_section_changes",
                    "sense": "min",
                    "value": int(exact.get("section_changes") or 0),
                },
                {
                    "name": "minimize_quality_penalty",
                    "sense": "min",
                    "value": int((exact.get("timetable_quality") or {}).get("penalty") or 0),
                },
            ],
            "tie_breakers": [
                {"name": "day", "value": day},
                {"name": "start_time", "value": start_time},
            ],
        }

    outcome = metrics.get("student_outcome") or {}
    quick = metrics.get("quick") or {}
    rank_key = (
        int(outcome.get("blocked_students_delta") or 0),
        int(outcome.get("unresolved_course_delta") or 0),
        -int(outcome.get("newly_unblocked_student_count") or 0),
        -int(outcome.get("improved_student_count") or 0),
        int(outcome.get("worsened_student_count") or 0),
        int(outcome.get("newly_blocked_student_count") or 0),
        max(0, int(outcome.get("actual_clash_delta") or 0)),
        int(quick.get("critical_count") or 0),
        int(quick.get("warning_count") or 0),
        int(quick.get("impact_score") or 0),
        day,
        start_time,
    )
    return {
        "eligible": status == TimetableRepairCandidate.STATUS_FEASIBLE,
        "score_rank": score_rank,
        "rank_key": list(rank_key),
        "strategy": "preview_fallback_lexicographic",
        "primary_reason": _repair_fallback_rank_primary_reason(candidate, exact),
        "criteria": [
            {
                "name": "minimize_unresolved_students_delta",
                "sense": "min",
                "value": int(outcome.get("blocked_students_delta") or 0),
            },
            {
                "name": "minimize_unresolved_courses_delta",
                "sense": "min",
                "value": int(outcome.get("unresolved_course_delta") or 0),
            },
            {
                "name": "maximize_newly_unblocked_students",
                "sense": "max",
                "value": int(outcome.get("newly_unblocked_student_count") or 0),
            },
            {
                "name": "maximize_improved_students",
                "sense": "max",
                "value": int(outcome.get("improved_student_count") or 0),
            },
            {
                "name": "avoid_worsened_students",
                "sense": "min",
                "value": int(outcome.get("worsened_student_count") or 0),
            },
            {
                "name": "avoid_newly_blocked",
                "sense": "min",
                "value": int(outcome.get("newly_blocked_student_count") or 0),
            },
            {
                "name": "avoid_clash_increase",
                "sense": "min",
                "value": max(0, int(outcome.get("actual_clash_delta") or 0)),
            },
            {
                "name": "quick_critical",
                "sense": "min",
                "value": int(quick.get("critical_count") or 0),
            },
            {
                "name": "quick_warning",
                "sense": "min",
                "value": int(quick.get("warning_count") or 0),
            },
            {"name": "quick_impact", "sense": "min", "value": int(quick.get("impact_score") or 0)},
        ],
        "tie_breakers": [
            {"name": "day", "value": day},
            {"name": "start_time", "value": start_time},
        ],
    }


def _repair_rank_primary_reason(exact: dict[str, Any]) -> str:
    recovered = int(exact.get("blocked_recovered") or 0)
    requested = int(exact.get("requested_courses_recovered") or 0)
    additional_requested = max(0, requested - recovered)
    lost = int(exact.get("existing_lost") or 0)
    unresolved = int(exact.get("unresolved_blocked") or 0)
    moved = int(exact.get("students_moved") or 0)
    if lost == 0 and recovered and additional_requested:
        return (
            f"Recovers {recovered} blocked student(s) plus "
            f"{additional_requested} additional requested course(s) with no existing registration loss."
        )
    if lost == 0 and recovered:
        return f"Recovers {recovered} blocked student(s) with no existing registration loss."
    if lost == 0 and unresolved == 0:
        return "Keeps existing registrations protected and leaves no blocked students unresolved."
    if moved == 0:
        return "Requires no existing-student section movement."
    return "Best lexicographic balance of protection, recovery and disruption."


def _repair_fallback_rank_primary_reason(
    candidate: TimetableRepairCandidate | dict[str, Any],
    exact: dict[str, Any],
) -> str:
    status = str(_candidate_field(candidate, "status", "") or "")
    if status == TimetableRepairCandidate.STATUS_REJECTED:
        return "Rejected before solver by hard feasibility checks."
    if status == TimetableRepairCandidate.STATUS_NOT_SOLVED:
        return str(
            exact.get("reason") or "Student repair solver did not produce an applicable solution."
        )
    return "Ranked by preview impact because no exact solved repair metrics were available."


def _mark_candidate_not_solved(
    run: TimetableRepairRun | None,
    candidate: dict[str, Any],
    *,
    status: str,
    reason: str,
    started: float,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = dict(candidate.get("metrics") or {})
    runtime_ms = int((perf_counter() - started) * 1000)
    exact_metrics = {
        "enabled": cp_model is not None,
        "solver_status": status,
        "reason": reason,
        "runtime_ms": runtime_ms,
        **(details or {}),
    }
    metrics["exact_repair"] = exact_metrics
    explanation = dict(candidate.get("explanation") or {})
    explanation["student_solver_status"] = status
    explanation["student_solver_reason"] = reason
    candidate["metrics"] = metrics
    candidate["explanation"] = explanation
    candidate["solver_status"] = status[:32]
    candidate["status"] = TimetableRepairCandidate.STATUS_NOT_SOLVED
    _candidate_draft_log(
        candidate,
        "warning",
        "repair_candidate_student_solver_not_solved",
        exact_metrics,
    )
    return exact_metrics


def _requested_courses_by_student(
    scenario_id: int,
    student_ids: list[int],
    *,
    allowed_course_keys: set[str] | None = None,
) -> dict[int, set[str]]:
    """Return bounded canonical requested courses for the affected students."""

    student_set = {int(sid) for sid in student_ids}
    if not student_set:
        return {}
    demands = load_scenario_course_demands(
        scenario_id,
        course_keys=allowed_course_keys if allowed_course_keys else None,
    )
    requested: dict[int, set[str]] = defaultdict(set)
    for demand in demands:
        sid = int(demand.student_id)
        course_key = str(demand.course_key or "").strip()
        if sid in student_set and course_key:
            requested[sid].add(course_key)
    return requested


def _optional_requested_courses_for_student(
    *,
    student_id: int,
    current_courses: dict[str, int],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_set: set[int],
) -> list[str]:
    """Courses the solver may newly add for a student without risking current courses."""

    courses = set(requested_courses_by_student.get(int(student_id), set()))
    if int(student_id) in blocked_set and target_course not in current_courses:
        courses.add(target_course)
    courses.difference_update(current_courses.keys())
    return sorted(course for course in courses if course)


def _section_quality_components(
    *,
    section_ids: set[int],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_meetings: dict[int, list[dict[str, str]]] | None = None,
) -> dict[int, dict[str, Any]]:
    """Per-section quality components that are safe as final-tier preferences."""

    components: dict[int, dict[str, Any]] = {}
    for section_id in section_ids:
        capacity = int(capacity_by_section.get(section_id, 0))
        fixed_occupancy = int(fixed_occupancy_by_section.get(section_id, 0))
        available_for_solver = max(0, capacity - fixed_occupancy)
        if available_for_solver <= 0:
            spare_capacity_penalty = 1_000
        elif available_for_solver >= 40:
            spare_capacity_penalty = 0
        else:
            spare_capacity_penalty = 40 - available_for_solver
        components[int(section_id)] = {
            "spare_capacity_penalty": spare_capacity_penalty,
            "weak_slot_penalty": 0,
            "available_for_solver": available_for_solver,
            "total": spare_capacity_penalty,
        }
    for section_id, meetings in (section_meetings or {}).items():
        weak_slot_penalty = sum(_weak_slot_penalty(meeting) for meeting in meetings)
        if int(section_id) not in components:
            components[int(section_id)] = {
                "spare_capacity_penalty": 0,
                "available_for_solver": 0,
            }
        components[int(section_id)]["weak_slot_penalty"] = weak_slot_penalty
        components[int(section_id)]["total"] = int(
            components[int(section_id)].get("spare_capacity_penalty") or 0
        ) + int(weak_slot_penalty)
    return components


def _section_quality_costs(
    *,
    section_ids: set[int],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_meetings: dict[int, list[dict[str, str]]] | None = None,
) -> dict[int, int]:
    """Small final-tier penalty that prefers healthier and less awkward slots."""

    components = _section_quality_components(
        section_ids=section_ids,
        capacity_by_section=capacity_by_section,
        fixed_occupancy_by_section=fixed_occupancy_by_section,
        section_meetings=section_meetings,
    )
    return {section_id: int(row.get("total") or 0) for section_id, row in components.items()}


def _weak_slot_penalty(meeting: dict[str, str]) -> int:
    """Soft preference against hard-to-use teaching slots."""

    try:
        start = _minutes(str(meeting.get("start_time") or ""))
    except ValueError:
        return 0
    if start < 9 * 60:
        return 6
    if start >= 16 * 60:
        return 6
    if 12 * 60 <= start < 14 * 60:
        return 2
    return 0


def _student_current_day_loads(
    *,
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    section_meetings: dict[int, list[dict[str, str]]],
) -> dict[int, Counter[str]]:
    loads: dict[int, Counter[str]] = {}
    for sid in student_ids:
        counter: Counter[str] = Counter()
        for section_id in current_by_student_course.get(int(sid), {}).values():
            for day in _section_meeting_days(int(section_id), section_meetings):
                counter[day] += 1
        loads[int(sid)] = counter
    return loads


def _section_meeting_days(
    section_id: int,
    section_meetings: dict[int, list[dict[str, str]]],
) -> set[str]:
    return {
        str(meeting.get("day") or "").strip().upper()
        for meeting in section_meetings.get(int(section_id), [])
        if str(meeting.get("day") or "").strip()
    }


def _assignment_quality_costs(
    *,
    student_ids: list[int],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    current_by_student_course: dict[int, dict[str, int]],
    section_meetings: dict[int, list[dict[str, str]]],
    section_quality_cost_by_id: dict[int, int],
) -> dict[tuple[int, str, int], int]:
    day_loads = _student_current_day_loads(
        student_ids=student_ids,
        current_by_student_course=current_by_student_course,
        section_meetings=section_meetings,
    )
    costs: dict[tuple[int, str, int], int] = {}
    for (student_id, course_key), section_ids in option_ids_by_student_course.items():
        sid = int(student_id)
        course = str(course_key)
        current_section_id = current_by_student_course.get(sid, {}).get(course)
        current_days = (
            _section_meeting_days(int(current_section_id), section_meetings)
            if current_section_id
            else set()
        )
        for section_id in section_ids:
            section_id = int(section_id)
            section_cost = int(section_quality_cost_by_id.get(section_id, 0))
            day_balance_penalty = 0
            for day in _section_meeting_days(section_id, section_meetings):
                base_load = int(day_loads.get(sid, Counter()).get(day, 0))
                if day in current_days:
                    base_load = max(0, base_load - 1)
                day_balance_penalty += max(0, base_load) * 3
            costs[(sid, course, section_id)] = section_cost + day_balance_penalty
    return costs


def _assignment_quality_penalty(
    chosen: dict[tuple[int, str], int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
) -> int:
    return sum(
        int(assignment_quality_cost_by_key.get((int(sid), str(course), int(section_id)), 0))
        for (sid, course), section_id in chosen.items()
    )


def _assignment_quality_expr(
    x: dict[tuple[int, str, int], Any],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
) -> Any:
    terms = [
        var * int(assignment_quality_cost_by_key.get((int(sid), str(course), int(section_id)), 0))
        for (sid, course, section_id), var in x.items()
    ]
    return sum(terms) if terms else 0


def _quality_penalty_breakdown(
    chosen: dict[tuple[int, str], int],
    *,
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
    section_quality_cost_by_id: dict[int, int],
    section_quality_components_by_id: dict[int, dict[str, Any]],
) -> dict[str, int]:
    section_total = 0
    spare_total = 0
    weak_slot_total = 0
    assignment_total = 0
    for (sid, course), section_id in chosen.items():
        section_id = int(section_id)
        components = section_quality_components_by_id.get(section_id, {})
        section_cost = int(section_quality_cost_by_id.get(section_id, 0))
        assignment_cost = int(
            assignment_quality_cost_by_key.get((int(sid), str(course), section_id), section_cost)
        )
        section_total += section_cost
        assignment_total += assignment_cost
        spare_total += int(components.get("spare_capacity_penalty") or 0)
        weak_slot_total += int(components.get("weak_slot_penalty") or 0)
    return {
        "total": assignment_total,
        "section_quality": section_total,
        "spare_capacity": spare_total,
        "weak_slot": weak_slot_total,
        "day_balance": max(0, assignment_total - section_total),
    }


def _solve_min_cost_flow_repair_if_simple(
    *,
    mode_policy: dict[str, Any],
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_quality_cost_by_id: dict[int, int] | None = None,
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int] | None = None,
) -> dict[str, Any] | None:
    """Use min-cost flow for single-course sectioning with capacity constraints only."""

    if min_cost_flow is None:
        return None
    blocked_set = set(blocked_ids)
    has_new_blocked = any(
        sid in blocked_set and target_course not in current_by_student_course.get(sid, {})
        for sid in student_ids
    )
    if not has_new_blocked:
        return None
    for courses in current_by_student_course.values():
        if any(course != target_course for course in courses):
            return None
    for sid in student_ids:
        optional_courses = _optional_requested_courses_for_student(
            student_id=sid,
            current_courses=current_by_student_course.get(sid, {}),
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_set=blocked_set,
        )
        if any(course != target_course for course in optional_courses):
            return None

    current_students = {
        sid: courses[target_course]
        for sid, courses in current_by_student_course.items()
        if target_course in courses
    }
    for sid in student_ids:
        if sid not in current_students and sid not in blocked_set:
            return None
    target_section_ids = _lns_target_section_ids(
        current_by_student_course=current_by_student_course,
        option_ids_by_student_course=option_ids_by_student_course,
        target_course=target_course,
    )
    if not target_section_ids:
        return None

    flow = min_cost_flow.SimpleMinCostFlow()
    source = 0
    sink = 1
    unserved_node = 2
    next_node = 3
    student_node: dict[int, int] = {}
    section_node: dict[int, int] = {}
    student_arc_by_index: dict[int, tuple[int, int | None]] = {}
    unserved_cost = 1_000_000
    moved_cost = 100

    def add_arc(tail: int, head: int, capacity: int, cost: int) -> int:
        arc = flow.add_arc_with_capacity_and_unit_cost(tail, head, capacity, cost)
        return int(arc)

    for sid in student_ids:
        student_node[sid] = next_node
        next_node += 1
    for section_id in target_section_ids:
        section_node[section_id] = next_node
        next_node += 1

    for sid in student_ids:
        add_arc(source, student_node[sid], 1, 0)
        options = option_ids_by_student_course.get((sid, target_course), [])
        current_section_id = current_students.get(sid)
        for section_id in options:
            section_id = int(section_id)
            quality_cost = int(
                (assignment_quality_cost_by_key or {}).get((int(sid), target_course, section_id), 0)
            )
            cost = (
                moved_cost if current_section_id and section_id != int(current_section_id) else 0
            ) + quality_cost
            arc = add_arc(student_node[sid], section_node[section_id], 1, cost)
            student_arc_by_index[arc] = (sid, section_id)
        if sid in blocked_set and current_section_id is None:
            arc = add_arc(student_node[sid], unserved_node, 1, unserved_cost)
            student_arc_by_index[arc] = (sid, None)

    for section_id in target_section_ids:
        remaining_capacity = int(capacity_by_section.get(section_id, 0)) - int(
            fixed_occupancy_by_section.get(section_id, 0)
        )
        if remaining_capacity > 0:
            add_arc(section_node[section_id], sink, remaining_capacity, 0)
    add_arc(unserved_node, sink, len(student_ids), 0)
    flow.set_node_supply(source, len(student_ids))
    flow.set_node_supply(sink, -len(student_ids))

    status = flow.solve()
    if status != flow.OPTIMAL:
        return None

    chosen: dict[tuple[int, str], int] = {}
    newly_served = 0
    moved_students = 0
    changed_assignments = 0
    unserved = 0
    quality_penalty = 0
    for arc in range(flow.num_arcs()):
        if flow.flow(arc) <= 0 or arc not in student_arc_by_index:
            continue
        sid, section_id = student_arc_by_index[arc]
        if section_id is None:
            unserved += 1
            continue
        chosen[(int(sid), target_course)] = int(section_id)
        quality_penalty += int(
            (assignment_quality_cost_by_key or {}).get(
                (int(sid), target_course, int(section_id)),
                int((section_quality_cost_by_id or {}).get(int(section_id), 0)),
            )
        )
        if sid in blocked_set and sid not in current_students:
            newly_served += 1
        elif int(current_students.get(sid, section_id)) != int(section_id):
            moved_students += 1
            changed_assignments += 1

    return {
        "chosen": chosen,
        "solver_status": "optimal",
        "variables": flow.num_arcs(),
        "objective_trace": [
            {
                "stage": 1,
                "name": "maximize_blocked_recovery",
                "sense": "max",
                "status": "optimal",
                "value": newly_served,
            },
            {
                "stage": 2,
                "name": "maximize_requested_course_recovery",
                "sense": "max",
                "status": "optimal",
                "value": newly_served,
            },
            {
                "stage": 3,
                "name": "minimize_section_changes",
                "sense": "min",
                "status": "optimal",
                "value": changed_assignments,
            },
            {
                "stage": 4,
                "name": "minimize_moved_students",
                "sense": "min",
                "status": "optimal",
                "value": moved_students,
            },
            {
                "stage": 5,
                "name": "minimize_timetable_quality_penalty",
                "sense": "min",
                "status": "optimal",
                "value": quality_penalty,
            },
        ],
        "conflict_policy": {
            "strategy": "single_course_min_cost_flow_no_time_conflicts",
            "too_large": False,
            "logical_conflict_edges": 0,
            "max_conflict_edges": 0,
            "at_most_one_constraints": 0,
            "pairwise_constraints": 0,
            "covered_pair_count": 0,
            "samples": [],
        },
        "min_cost_flow": {
            "enabled": True,
            "used": True,
            "strategy": "single_course_capacity_min_cost_flow",
            "mode": mode_policy["mode"],
            "student_count": len(student_ids),
            "section_count": len(target_section_ids),
            "arc_count": flow.num_arcs(),
            "optimal_cost": int(flow.optimal_cost()),
            "blocked_recovered": newly_served,
            "requested_courses_recovered": newly_served,
            "unserved": unserved,
            "moved_students": moved_students,
            "changed_assignments": changed_assignments,
            "quality_penalty": quality_penalty,
        },
    }


def _solve_large_neighbourhood_repair_if_large(
    *,
    limits: dict[str, int],
    mode_policy: dict[str, Any],
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
    section_meetings: dict[int, list[dict[str, str]]],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_quality_cost_by_id: dict[int, int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
) -> dict[str, Any] | None:
    """Solve a bounded repair neighbourhood when the full component is too large."""

    if cp_model is None:
        return None
    blocked_set = set(blocked_ids)
    student_set = set(student_ids)
    blocked_new = [
        sid
        for sid in student_ids
        if sid in blocked_set and target_course not in current_by_student_course.get(sid, {})
    ]
    if not blocked_new:
        return None
    max_lns_students = int(limits.get("max_lns_students", DEFAULT_LIMITS["max_lns_students"]))
    max_lns_variables = int(limits.get("max_lns_variables", DEFAULT_LIMITS["max_lns_variables"]))
    neighbourhoods = _build_lns_neighbourhood_specs(
        student_ids=student_ids,
        student_set=student_set,
        blocked_new=blocked_new,
        current_by_student_course=current_by_student_course,
        option_ids_by_student_course=option_ids_by_student_course,
        target_course=target_course,
        section_meetings=section_meetings,
        capacity_by_section=capacity_by_section,
        fixed_occupancy_by_section=fixed_occupancy_by_section,
        max_lns_students=max_lns_students,
    )
    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int, int, int, int, int] | None = None
    best_spec: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    max_lns_iterations = max(
        1,
        int(limits.get("max_lns_iterations", DEFAULT_LIMITS["max_lns_iterations"])),
    )
    pending = [{**spec, "origin": "base", "adaptive_generation": 0} for spec in neighbourhoods]
    seen_signatures: set[tuple[int, ...]] = set()
    stats: dict[str, dict[str, Any]] = {
        str(spec["name"]): _initial_lns_neighbourhood_stat(spec) for spec in pending
    }
    family_stats: dict[str, dict[str, Any]] = {
        _lns_neighbourhood_family(spec): _initial_lns_family_stat(_lns_neighbourhood_family(spec))
        for spec in pending
    }
    iteration = 0
    while pending and iteration < max_lns_iterations:
        pending.sort(
            key=lambda spec: _adaptive_lns_spec_sort_key(
                spec,
                stats=stats,
                family_stats=family_stats,
                best_score=best_score,
            )
        )
        spec = pending.pop(0)
        name = str(spec["name"])
        family = _lns_neighbourhood_family(spec)
        relaxed_ids = list(spec["student_ids"])
        if not relaxed_ids:
            continue
        signature = tuple(int(sid) for sid in relaxed_ids)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        iteration += 1
        result = _solve_lns_neighbourhood(
            name=name,
            reason=str(spec.get("reason") or ""),
            limits=limits,
            mode_policy=mode_policy,
            relaxed_ids=relaxed_ids,
            all_student_ids=student_ids,
            current_by_student_course=current_by_student_course,
            option_ids_by_student_course=option_ids_by_student_course,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_ids=blocked_ids,
            section_meetings=section_meetings,
            capacity_by_section=capacity_by_section,
            fixed_occupancy_by_section=fixed_occupancy_by_section,
            section_quality_cost_by_id=section_quality_cost_by_id,
            assignment_quality_cost_by_key=assignment_quality_cost_by_key,
            max_lns_variables=max_lns_variables,
        )
        attempt = result["attempt"]
        attempt["iteration"] = iteration
        attempt["origin"] = str(spec.get("origin") or "base")
        attempt["adaptive_family"] = family
        attempt["adaptive_generation"] = int(spec.get("adaptive_generation") or 0)
        attempt["adaptive_weight_before"] = round(
            float(stats.setdefault(name, _initial_lns_neighbourhood_stat(spec))["weight"]),
            3,
        )
        attempt["adaptive_selection_priority"] = round(
            float(stats[name].get("last_selection_priority") or 0.0),
            3,
        )
        attempt["adaptive_family_weight_before"] = round(
            float(family_stats.setdefault(family, _initial_lns_family_stat(family))["weight"]),
            3,
        )
        solution = result.get("solution")
        if not solution:
            reward = _adaptive_lns_reward(attempt=attempt, score=None, improved=False)
            _update_lns_neighbourhood_stat(
                stats.setdefault(name, _initial_lns_neighbourhood_stat(spec)),
                family_stats.setdefault(family, _initial_lns_family_stat(family)),
                attempt=attempt,
                score=None,
                improved=False,
                reward=reward,
            )
            attempt["adaptive_weight_after"] = round(float(stats[name]["weight"]), 3)
            attempt["adaptive_family_weight_after"] = round(
                float(family_stats[family]["weight"]), 3
            )
            attempt["adaptive_reward"] = round(float(reward), 3)
            attempts.append(attempt)
            continue
        score = (
            int(solution["large_neighbourhood"]["blocked_recovered"]),
            -int(solution["large_neighbourhood"].get("unresolved_blocked") or 0),
            int(solution["large_neighbourhood"].get("requested_courses_recovered") or 0),
            -int(solution["large_neighbourhood"]["students_moved"]),
            -int(solution["large_neighbourhood"]["section_changes"]),
            -int(solution["large_neighbourhood"].get("quality_penalty") or 0),
            -int(solution["variables"]),
        )
        improved = best_score is None or score > best_score
        attempt["score"] = list(score)
        attempt["improved_incumbent"] = improved
        reward = _adaptive_lns_reward(attempt=attempt, score=score, improved=improved)
        _update_lns_neighbourhood_stat(
            stats.setdefault(name, _initial_lns_neighbourhood_stat(spec)),
            family_stats.setdefault(family, _initial_lns_family_stat(family)),
            attempt=attempt,
            score=score,
            improved=improved,
            reward=reward,
        )
        attempt["adaptive_weight_after"] = round(float(stats[name]["weight"]), 3)
        attempt["adaptive_family_weight_after"] = round(float(family_stats[family]["weight"]), 3)
        attempt["adaptive_reward"] = round(float(reward), 3)
        attempts.append(attempt)
        if improved:
            best = solution
            best_score = score
            best_spec = spec
            pending.extend(
                _adaptive_lns_followup_specs(
                    incumbent=solution,
                    winning_spec=spec,
                    base_specs=neighbourhoods,
                    blocked_new=blocked_new,
                    student_set=student_set,
                    current_by_student_course=current_by_student_course,
                    target_course=target_course,
                    max_lns_students=max_lns_students,
                    existing_signatures=seen_signatures
                    | {tuple(int(sid) for sid in row.get("student_ids") or []) for row in pending},
                )
            )

    if best is None:
        return {
            "not_solved": True,
            "large_neighbourhood": {
                "enabled": True,
                "used": False,
                "strategy": "adaptive_large_neighbourhood_cp_sat",
                "reason": "no_feasible_bounded_neighbourhood",
                "attempts": attempts,
                "neighbourhood_count": len(neighbourhoods),
                "iteration_count": iteration,
                "max_lns_iterations": max_lns_iterations,
                "max_lns_students": max_lns_students,
                "max_lns_variables": max_lns_variables,
                "adaptive": _adaptive_lns_summary(
                    stats=stats,
                    family_stats=family_stats,
                    best_score=best_score,
                    best_spec=best_spec,
                    max_lns_iterations=max_lns_iterations,
                ),
            },
        }
    best["large_neighbourhood"]["attempts"] = attempts
    best["large_neighbourhood"]["neighbourhood_count"] = len(neighbourhoods)
    best["large_neighbourhood"]["iteration_count"] = iteration
    best["large_neighbourhood"]["max_lns_iterations"] = max_lns_iterations
    best["large_neighbourhood"]["max_lns_students"] = max_lns_students
    best["large_neighbourhood"]["max_lns_variables"] = max_lns_variables
    best["large_neighbourhood"]["strategy"] = "adaptive_large_neighbourhood_cp_sat"
    best["large_neighbourhood"]["adaptive"] = _adaptive_lns_summary(
        stats=stats,
        family_stats=family_stats,
        best_score=best_score,
        best_spec=best_spec,
        max_lns_iterations=max_lns_iterations,
    )
    return best


def _initial_lns_neighbourhood_stat(spec: dict[str, Any]) -> dict[str, Any]:
    family = _lns_neighbourhood_family(spec)
    return {
        "name": str(spec.get("name") or ""),
        "family": family,
        "origin": str(spec.get("origin") or "base"),
        "weight": 1.0,
        "attempts": 0,
        "successes": 0,
        "improvements": 0,
        "skipped": 0,
        "reward_total": 0.0,
        "average_reward": 0.0,
        "last_reward": 0.0,
        "last_selection_priority": 0.0,
        "best_score": None,
        "last_status": "",
    }


def _initial_lns_family_stat(family: str) -> dict[str, Any]:
    return {
        "family": str(family or "general"),
        "weight": 1.0,
        "attempts": 0,
        "successes": 0,
        "improvements": 0,
        "skipped": 0,
        "reward_total": 0.0,
        "average_reward": 0.0,
        "last_reward": 0.0,
    }


def _lns_neighbourhood_family(spec: dict[str, Any]) -> str:
    explicit = str(spec.get("adaptive_family") or "").strip()
    if explicit:
        return explicit
    name = str(spec.get("name") or "")
    if name.startswith("adaptive_union_"):
        return "hybrid"
    if name.startswith("adaptive_incumbent_"):
        return "incumbent"
    if name.startswith("target_course"):
        return "target_course"
    if name.startswith("capacity_pressure"):
        return "capacity_pressure"
    if name.startswith("target_conflict"):
        return "conflict_frontier"
    if name.startswith("multi_course"):
        return "multi_course"
    if name.startswith("whole_component"):
        return "whole_component"
    if name.startswith("blocked"):
        return "blocked_only"
    return "general"


def _adaptive_lns_spec_sort_key(
    spec: dict[str, Any],
    *,
    stats: dict[str, dict[str, Any]],
    family_stats: dict[str, dict[str, Any]],
    best_score: tuple[int, ...] | None,
) -> tuple[float, int, int, str]:
    name = str(spec.get("name") or "")
    family = _lns_neighbourhood_family(spec)
    stat = stats.setdefault(name, _initial_lns_neighbourhood_stat(spec))
    family_stat = family_stats.setdefault(family, _initial_lns_family_stat(family))
    ids = spec.get("student_ids") or []
    size = len(ids)
    origin_bonus = {
        "incumbent": 0.35,
        "hybrid": 0.2,
        "base": 0.0,
    }.get(str(spec.get("origin") or "base"), 0.0)
    incumbent_bonus = 0.1 if best_score is not None else 0.0
    exploration_bonus = 0.08 / (1 + int(stat.get("attempts") or 0))
    # Lower sort key wins, so negate the learned priority.
    priority = (
        0.62 * float(stat.get("weight") or 1.0)
        + 0.38 * float(family_stat.get("weight") or 1.0)
        + origin_bonus
        + incumbent_bonus
        + exploration_bonus
    )
    stat["last_selection_priority"] = round(float(priority), 3)
    return (-priority, size, int(spec.get("adaptive_generation") or 0), name)


def _update_lns_neighbourhood_stat(
    stat: dict[str, Any],
    family_stat: dict[str, Any],
    *,
    attempt: dict[str, Any],
    score: tuple[int, ...] | None,
    improved: bool,
    reward: float,
) -> None:
    for row in (stat, family_stat):
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["last_status"] = str(attempt.get("status") or "")
        row["last_reward"] = float(reward)
        row["reward_total"] = float(row.get("reward_total") or 0.0) + float(reward)
        row["average_reward"] = float(row["reward_total"]) / max(1, int(row["attempts"]))
    if score is None:
        for row in (stat, family_stat):
            row["skipped"] = int(row.get("skipped") or 0) + 1
            row["weight"] = max(0.2, float(row.get("weight") or 1.0) + float(reward))
        return
    stat["successes"] = int(stat.get("successes") or 0) + 1
    family_stat["successes"] = int(family_stat.get("successes") or 0) + 1
    current_best = stat.get("best_score")
    if current_best is None or tuple(score) > tuple(current_best):
        stat["best_score"] = list(score)
    stat["weight"] = min(12.0, max(0.2, float(stat.get("weight") or 1.0) + float(reward)))
    family_stat["weight"] = min(
        12.0,
        max(0.2, float(family_stat.get("weight") or 1.0) + float(reward) * 0.7),
    )
    if improved:
        stat["improvements"] = int(stat.get("improvements") or 0) + 1
        family_stat["improvements"] = int(family_stat.get("improvements") or 0) + 1


def _adaptive_lns_reward(
    *,
    attempt: dict[str, Any],
    score: tuple[int, ...] | None,
    improved: bool,
) -> float:
    """Reward neighbourhoods by student recovery, not board-level appearance."""

    if score is None:
        return -0.3
    blocked_recovered = int(attempt.get("blocked_recovered") or 0)
    unresolved = int(attempt.get("unresolved_blocked") or 0)
    requested_recovered = int(attempt.get("requested_courses_recovered") or 0)
    moved = int(attempt.get("students_moved") or 0)
    changes = int(attempt.get("section_changes") or 0)
    reward = (
        1.4 * blocked_recovered
        - 0.9 * unresolved
        + 0.2 * requested_recovered
        - 0.04 * moved
        - 0.02 * changes
    )
    if improved:
        reward += 1.2
    return max(-1.0, min(6.0, reward))


def _adaptive_lns_followup_specs(
    *,
    incumbent: dict[str, Any],
    winning_spec: dict[str, Any],
    base_specs: list[dict[str, Any]],
    blocked_new: list[int],
    student_set: set[int],
    current_by_student_course: dict[int, dict[str, int]],
    target_course: str,
    max_lns_students: int,
    existing_signatures: set[tuple[int, ...]],
) -> list[dict[str, Any]]:
    changed_ids = _lns_incumbent_changed_student_ids(
        incumbent=incumbent,
        current_by_student_course=current_by_student_course,
        target_course=target_course,
        blocked_new=blocked_new,
    )
    winning_ids = [int(sid) for sid in winning_spec.get("student_ids") or []]
    target_current = [
        sid for sid, courses in current_by_student_course.items() if target_course in courses
    ]
    generation = int(winning_spec.get("adaptive_generation") or 0) + 1
    candidates = [
        {
            "name": f"adaptive_incumbent_cascade_g{generation}",
            "origin": "incumbent",
            "adaptive_family": "incumbent",
            "adaptive_generation": generation,
            "reason": "incumbent changed students plus blocked target-course demand",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [*changed_ids, *winning_ids],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": f"adaptive_incumbent_target_frontier_g{generation}",
            "origin": "incumbent",
            "adaptive_family": "incumbent",
            "adaptive_generation": generation,
            "reason": "incumbent changed students plus current target-course occupants",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [*changed_ids, *target_current, *winning_ids],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
    ]
    for base in base_specs:
        base_ids = [int(sid) for sid in base.get("student_ids") or []]
        if not base_ids or tuple(base_ids) == tuple(winning_ids):
            continue
        candidates.append(
            {
                "name": f"adaptive_union_{winning_spec.get('name')}_{base.get('name')}_g{generation}",
                "origin": "hybrid",
                "adaptive_family": f"hybrid:{_lns_neighbourhood_family(base)}",
                "adaptive_generation": generation,
                "reason": f"hybrid of incumbent neighbourhood and {base.get('name')}",
                "student_ids": _cap_lns_student_ids(
                    blocked_new,
                    [*changed_ids, *winning_ids, *base_ids],
                    student_set=student_set,
                    max_lns_students=max_lns_students,
                ),
            }
        )
        if len(candidates) >= 4:
            break

    out: list[dict[str, Any]] = []
    for spec in candidates:
        signature = tuple(int(sid) for sid in spec.get("student_ids") or [])
        if not signature or signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        out.append(spec)
    return out


def _lns_incumbent_changed_student_ids(
    *,
    incumbent: dict[str, Any],
    current_by_student_course: dict[int, dict[str, int]],
    target_course: str,
    blocked_new: list[int],
) -> list[int]:
    chosen = incumbent.get("chosen") or {}
    changed: list[int] = []
    seen: set[int] = set()
    for (student_id, course_key), section_id in chosen.items():
        sid = int(student_id)
        course = str(course_key)
        current = current_by_student_course.get(sid, {}).get(course)
        is_new_target = sid in set(blocked_new) and course == target_course and current is None
        if is_new_target or (current is not None and int(current) != int(section_id)):
            if sid not in seen:
                changed.append(sid)
                seen.add(sid)
    return changed


def _adaptive_lns_summary(
    *,
    stats: dict[str, dict[str, Any]],
    family_stats: dict[str, dict[str, Any]],
    best_score: tuple[int, ...] | None,
    best_spec: dict[str, Any] | None,
    max_lns_iterations: int,
) -> dict[str, Any]:
    rows = sorted(
        stats.values(),
        key=lambda row: (
            -int(row.get("improvements") or 0),
            -int(row.get("successes") or 0),
            -float(row.get("weight") or 0),
            str(row.get("name") or ""),
        ),
    )
    return {
        "enabled": True,
        "strategy": "adaptive_family_weighted_neighbourhood_ordering",
        "max_iterations": int(max_lns_iterations),
        "best_neighbourhood": str((best_spec or {}).get("name") or ""),
        "best_family": _lns_neighbourhood_family(best_spec or {}) if best_spec else "",
        "best_score": list(best_score) if best_score is not None else None,
        "reward_policy": {
            "primary": "minimize_unresolved_students",
            "secondary": "maximize_recovered_blocked_students",
            "cross_board_role": "diagnostic_not_objective",
        },
        "family_weights": [
            {
                "family": str(row.get("family") or ""),
                "weight": round(float(row.get("weight") or 0), 3),
                "attempts": int(row.get("attempts") or 0),
                "successes": int(row.get("successes") or 0),
                "improvements": int(row.get("improvements") or 0),
                "skipped": int(row.get("skipped") or 0),
                "reward_total": round(float(row.get("reward_total") or 0), 3),
                "average_reward": round(float(row.get("average_reward") or 0), 3),
            }
            for row in sorted(
                family_stats.values(),
                key=lambda row: (
                    -float(row.get("weight") or 0),
                    -float(row.get("average_reward") or 0),
                    str(row.get("family") or ""),
                ),
            )
        ],
        "learned_neighbourhoods": [
            {
                "name": str(row.get("name") or ""),
                "family": str(row.get("family") or ""),
                "origin": str(row.get("origin") or ""),
                "weight": round(float(row.get("weight") or 0), 3),
                "attempts": int(row.get("attempts") or 0),
                "successes": int(row.get("successes") or 0),
                "improvements": int(row.get("improvements") or 0),
                "skipped": int(row.get("skipped") or 0),
                "reward_total": round(float(row.get("reward_total") or 0), 3),
                "average_reward": round(float(row.get("average_reward") or 0), 3),
                "last_reward": round(float(row.get("last_reward") or 0), 3),
                "last_selection_priority": round(float(row.get("last_selection_priority") or 0), 3),
                "best_score": row.get("best_score"),
                "last_status": str(row.get("last_status") or ""),
            }
            for row in rows[:12]
        ],
    }


def _build_lns_neighbourhood_specs(
    *,
    student_ids: list[int],
    student_set: set[int],
    blocked_new: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    target_course: str,
    section_meetings: dict[int, list[dict[str, str]]],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    max_lns_students: int,
) -> list[dict[str, Any]]:
    target_current = [
        sid for sid in student_ids if target_course in current_by_student_course.get(sid, {})
    ]
    target_section_ids = sorted(
        {
            int(section_id)
            for (sid, course), section_ids in option_ids_by_student_course.items()
            if course == target_course
            for section_id in section_ids
        }
    )
    multi_course_students = [
        sid for sid in student_ids if len(current_by_student_course.get(sid, {})) > 1
    ]
    specs = [
        {
            "name": "target_course_direct",
            "adaptive_family": "target_course",
            "reason": "blocked students plus students currently occupying the target course",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [*target_current],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": "capacity_pressure",
            "adaptive_family": "capacity_pressure",
            "reason": "students occupying target-course sections with limited remaining capacity",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                _capacity_pressure_student_ids(
                    student_ids=student_ids,
                    current_by_student_course=current_by_student_course,
                    target_section_ids=target_section_ids,
                    capacity_by_section=capacity_by_section,
                    fixed_occupancy_by_section=fixed_occupancy_by_section,
                    pressure_threshold=max(1, len(blocked_new)),
                ),
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": "target_conflict_frontier",
            "adaptive_family": "conflict_frontier",
            "reason": "students in sections that overlap candidate target-course options",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                _target_conflict_student_ids(
                    student_ids=student_ids,
                    current_by_student_course=current_by_student_course,
                    target_section_ids=target_section_ids,
                    section_meetings=section_meetings,
                ),
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": "multi_course_frontier",
            "adaptive_family": "multi_course",
            "reason": "multi-course students most likely to participate in a cascade",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [*target_current, *multi_course_students],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": "whole_component_capped",
            "adaptive_family": "whole_component",
            "reason": "largest bounded component allowed by interactive limits",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [*target_current, *student_ids],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
        {
            "name": "blocked_only",
            "adaptive_family": "blocked_only",
            "reason": "blocked students only; no existing student movement",
            "student_ids": _cap_lns_student_ids(
                blocked_new,
                [],
                student_set=student_set,
                max_lns_students=max_lns_students,
            ),
        },
    ]
    return _dedupe_lns_neighbourhood_specs(specs)


def _cap_lns_student_ids(
    blocked_new: list[int],
    candidate_ids: list[int],
    *,
    student_set: set[int],
    max_lns_students: int,
) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()
    for sid in [*blocked_new, *candidate_ids]:
        sid = int(sid)
        if sid in student_set and sid not in seen:
            ordered.append(sid)
            seen.add(sid)
        if len(ordered) >= max_lns_students:
            break
    return ordered


def _lns_target_section_ids(
    *,
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    target_course: str,
) -> list[int]:
    section_ids = {
        int(section_id)
        for (sid, course), candidate_section_ids in option_ids_by_student_course.items()
        if course == target_course
        for section_id in candidate_section_ids
    }
    section_ids.update(
        int(courses[target_course])
        for courses in current_by_student_course.values()
        if target_course in courses
    )
    return sorted(section_ids)


def _dedupe_lns_neighbourhood_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    for spec in specs:
        ids = tuple(spec.get("student_ids") or [])
        if not ids or ids in seen:
            continue
        seen.add(ids)
        out.append(spec)
    return out


def _capacity_pressure_student_ids(
    *,
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    target_section_ids: list[int],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    pressure_threshold: int,
) -> list[int]:
    target_set = set(target_section_ids)
    occupancy = Counter(
        int(section_id)
        for courses in current_by_student_course.values()
        for section_id in courses.values()
    )
    pressure_sections = {
        section_id
        for section_id in target_set
        if int(capacity_by_section.get(section_id, 0))
        - int(fixed_occupancy_by_section.get(section_id, 0))
        - int(occupancy.get(section_id, 0))
        <= pressure_threshold
    }
    return _students_in_sections(
        student_ids=student_ids,
        current_by_student_course=current_by_student_course,
        section_ids=pressure_sections,
    )


def _target_conflict_student_ids(
    *,
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    target_section_ids: list[int],
    section_meetings: dict[int, list[dict[str, str]]],
) -> list[int]:
    conflict_sections: set[int] = set()
    target_set = set(target_section_ids)
    target_meetings = [
        meeting
        for section_id in target_section_ids
        for meeting in section_meetings.get(section_id, [])
    ]
    if not target_meetings:
        return []
    for courses in current_by_student_course.values():
        for section_id in courses.values():
            section_id = int(section_id)
            if section_id in target_set:
                continue
            if _section_meetings_overlap(target_meetings, section_meetings.get(section_id, [])):
                conflict_sections.add(section_id)
    return _students_in_sections(
        student_ids=student_ids,
        current_by_student_course=current_by_student_course,
        section_ids=conflict_sections,
    )


def _students_in_sections(
    *,
    student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    section_ids: set[int],
) -> list[int]:
    if not section_ids:
        return []
    out: list[int] = []
    for sid in student_ids:
        if any(
            int(section_id) in section_ids
            for section_id in current_by_student_course.get(sid, {}).values()
        ):
            out.append(int(sid))
    return out


def _solve_lns_neighbourhood(
    *,
    name: str,
    reason: str,
    limits: dict[str, int],
    mode_policy: dict[str, Any],
    relaxed_ids: list[int],
    all_student_ids: list[int],
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
    section_meetings: dict[int, list[dict[str, str]]],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_quality_cost_by_id: dict[int, int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
    max_lns_variables: int,
) -> dict[str, Any]:
    attempt = {
        "name": name,
        "reason": reason,
        "relaxed_student_count": len(relaxed_ids),
        "status": "not_solved",
        "variables": None,
        "blocked_recovered": 0,
    }
    blocked_set = set(blocked_ids)
    relaxed_set = set(relaxed_ids)
    base_chosen = {
        (int(sid), str(course)): int(section_id)
        for sid, courses in current_by_student_course.items()
        for course, section_id in courses.items()
    }
    lns_fixed_occupancy = Counter(fixed_occupancy_by_section)
    for sid in all_student_ids:
        if sid in relaxed_set:
            continue
        for section_id in current_by_student_course.get(sid, {}).values():
            lns_fixed_occupancy[int(section_id)] += 1

    model = cp_model.CpModel()
    x: dict[tuple[int, str, int], Any] = {}
    by_student_vars: dict[int, list[tuple[str, int, Any]]] = defaultdict(list)
    by_section_vars: dict[int, list[Any]] = defaultdict(list)

    def section_var(student_id: int, course_key: str, section_id: int):
        key = (int(student_id), str(course_key), int(section_id))
        if key not in x:
            var = model.NewBoolVar(
                f"lns_{name}_s{student_id}_c{_safe_var_name(course_key)}_t{section_id}"
            )
            x[key] = var
            by_student_vars[int(student_id)].append((str(course_key), int(section_id), var))
            by_section_vars[int(section_id)].append(var)
        return x[key]

    required_pairs: list[tuple[int, str, int]] = []
    served_by_student: dict[int, Any] = {}
    requested_served_by_student_course: dict[tuple[int, str], Any] = {}
    for sid in relaxed_ids:
        current_courses = current_by_student_course.get(sid, {})
        for course, current_section_id in sorted(current_courses.items()):
            options = [
                int(section_id)
                for section_id in option_ids_by_student_course.get((sid, course), [])
            ]
            if int(current_section_id) not in options:
                attempt["status"] = "skipped"
                attempt["failure_reason"] = "current_section_missing_from_options"
                attempt["student_id"] = int(sid)
                attempt["course_key"] = str(course)
                return {"solution": None, "attempt": attempt}
            option_vars = [section_var(sid, course, section_id) for section_id in options]
            model.Add(sum(option_vars) == 1)
            required_pairs.append((int(sid), str(course), int(current_section_id)))
        for course in _optional_requested_courses_for_student(
            student_id=sid,
            current_courses=current_courses,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_set=blocked_set,
        ):
            options = [
                int(section_id)
                for section_id in option_ids_by_student_course.get((sid, course), [])
            ]
            served = model.NewBoolVar(f"lns_served_s{sid}_{_safe_var_name(course)}")
            requested_served_by_student_course[(int(sid), str(course))] = served
            if int(sid) in blocked_set and str(course) == target_course:
                served_by_student[int(sid)] = served
            if options:
                option_vars = [section_var(sid, course, section_id) for section_id in options]
                model.Add(sum(option_vars) == served)
            else:
                model.Add(served == 0)

    variable_count = len(x) + len(required_pairs) + len(requested_served_by_student_course)
    attempt["variables"] = variable_count
    if variable_count > max_lns_variables:
        attempt["status"] = "skipped"
        attempt["failure_reason"] = "variable_limit_exceeded"
        attempt["max_lns_variables"] = max_lns_variables
        return {"solution": None, "attempt": attempt}

    section_ids = set(by_section_vars) | set(lns_fixed_occupancy)
    for section_id in section_ids:
        capacity = int(capacity_by_section.get(section_id, 0))
        fixed_occupancy = int(lns_fixed_occupancy.get(section_id, 0))
        if fixed_occupancy > capacity:
            attempt["status"] = "skipped"
            attempt["failure_reason"] = "fixed_occupancy_exceeds_capacity"
            attempt["section_id"] = int(section_id)
            attempt["fixed_occupancy"] = fixed_occupancy
            attempt["capacity"] = capacity
            return {"solution": None, "attempt": attempt}
        terms = by_section_vars.get(section_id, [])
        if terms:
            model.Add(sum(terms) + fixed_occupancy <= capacity)

    conflict_policy = _add_student_time_conflict_constraints(
        model,
        by_student_vars=by_student_vars,
        section_meetings=section_meetings,
        limits=limits,
    )
    if conflict_policy["too_large"]:
        attempt["status"] = "skipped"
        attempt["failure_reason"] = "conflict_edge_limit_exceeded"
        attempt["conflict_edges"] = int(conflict_policy["logical_conflict_edges"])
        attempt["max_conflict_edges"] = int(conflict_policy["max_conflict_edges"])
        return {"solution": None, "attempt": attempt}
    conflict_policy = dict(conflict_policy)
    conflict_policy["strategy"] = "large_neighbourhood_at_most_one_groups"
    conflict_edges = int(conflict_policy["logical_conflict_edges"])

    changed_by_student: dict[int, list[Any]] = defaultdict(list)
    changed_vars: list[Any] = []
    for sid, course, current_section_id in required_pairs:
        current_var = x.get((sid, course, current_section_id))
        if current_var is None:
            attempt["status"] = "skipped"
            attempt["failure_reason"] = "current_assignment_variable_missing"
            attempt["student_id"] = int(sid)
            attempt["course_key"] = str(course)
            return {"solution": None, "attempt": attempt}
        changed = model.NewBoolVar(
            f"lns_changed_s{sid}_c{_safe_var_name(course)}_from{current_section_id}"
        )
        model.Add(changed + current_var == 1)
        changed_by_student[sid].append(changed)
        changed_vars.append(changed)

    moved_vars: list[Any] = []
    for sid, student_changes in changed_by_student.items():
        moved = model.NewBoolVar(f"lns_moved_s{sid}")
        for changed in student_changes:
            model.Add(moved >= changed)
        model.Add(moved <= sum(student_changes))
        moved_vars.append(moved)

    warm_start = _add_current_assignment_solver_hints(
        model,
        x=x,
        current_by_student_course=current_by_student_course,
        served_by_student=served_by_student,
        requested_served_by_student_course=requested_served_by_student_course,
        student_ids=relaxed_ids,
    )
    solver, status, objective_trace = _solve_lexicographic_repair(
        model,
        limits=limits,
        policy=mode_policy,
        served_expr=sum(served_by_student.values()) if served_by_student else 0,
        requested_expr=sum(requested_served_by_student_course.values())
        if requested_served_by_student_course
        else 0,
        moved_expr=sum(moved_vars) if moved_vars else 0,
        changed_expr=sum(changed_vars) if changed_vars else 0,
        quality_expr=_assignment_quality_expr(x, assignment_quality_cost_by_key),
    )
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        attempt["status"] = _cp_sat_status_name(status)
        attempt["failure_reason"] = "solver_status_not_feasible"
        attempt["objective_trace"] = objective_trace
        return {"solution": None, "attempt": attempt}

    chosen = dict(base_chosen)
    for (sid, course, section_id), var in x.items():
        if solver.BooleanValue(var):
            chosen[(sid, course)] = section_id

    moved_student_ids: set[int] = set()
    section_changes = 0
    for sid, course, current_section_id in required_pairs:
        if int(chosen.get((sid, course), current_section_id)) != int(current_section_id):
            section_changes += 1
            moved_student_ids.add(int(sid))
    blocked_recovered = sum(
        1
        for sid in blocked_ids
        if target_course not in current_by_student_course.get(sid, {})
        and chosen.get((int(sid), target_course))
    )
    blocked_target_count = sum(
        1 for sid in blocked_ids if target_course not in current_by_student_course.get(sid, {})
    )
    unresolved_blocked = max(0, blocked_target_count - blocked_recovered)
    requested_courses_recovered = sum(
        1 for key in requested_served_by_student_course if chosen.get((int(key[0]), str(key[1])))
    )
    quality_penalty = _assignment_quality_penalty(chosen, assignment_quality_cost_by_key)
    attempt["status"] = _cp_sat_status_name(status)
    attempt["blocked_recovered"] = blocked_recovered
    attempt["unresolved_blocked"] = unresolved_blocked
    attempt["requested_courses_recovered"] = requested_courses_recovered
    attempt["students_moved"] = len(moved_student_ids)
    attempt["section_changes"] = section_changes + requested_courses_recovered
    solution = {
        "chosen": chosen,
        "solver_status": _cp_sat_status_name(status),
        "objective_trace": objective_trace,
        "variables": variable_count,
        "conflict_policy": conflict_policy,
        "warm_start": warm_start,
        "large_neighbourhood": {
            "enabled": True,
            "used": True,
            "strategy": "bounded_large_neighbourhood_cp_sat",
            "neighbourhood": name,
            "neighbourhood_reason": reason,
            "relaxed_student_count": len(relaxed_ids),
            "fixed_student_count": max(0, len(all_student_ids) - len(relaxed_ids)),
            "variables": variable_count,
            "conflict_edges": conflict_edges,
            "blocked_recovered": blocked_recovered,
            "unresolved_blocked": unresolved_blocked,
            "requested_courses_recovered": requested_courses_recovered,
            "students_moved": len(moved_student_ids),
            "section_changes": section_changes + requested_courses_recovered,
            "quality_penalty": quality_penalty,
            "mode": mode_policy["mode"],
        },
    }
    return {"solution": solution, "attempt": attempt}


def _solve_profile_repair_if_beneficial(
    *,
    limits: dict[str, int],
    mode_policy: dict[str, Any],
    student_ids: list[int],
    eligibility_context: Any,
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
    section_meetings: dict[int, list[dict[str, str]]],
    capacity_by_section: dict[int, int],
    fixed_occupancy_by_section: dict[int, int],
    section_quality_cost_by_id: dict[int, int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
    profile_compression: dict[str, Any],
) -> dict[str, Any] | None:
    """Solve with profile-pattern variables when profiles materially reduce the model."""

    if int(profile_compression.get("profile_count") or 0) >= len(student_ids):
        return None

    profiles = _solver_profile_rows(
        student_ids=student_ids,
        eligibility_context=eligibility_context,
        current_by_student_course=current_by_student_course,
        option_ids_by_student_course=option_ids_by_student_course,
        requested_courses_by_student=requested_courses_by_student,
        target_course=target_course,
        blocked_ids=blocked_ids,
    )
    if len(profiles) >= len(student_ids):
        return None

    max_patterns = int(limits.get("max_profile_patterns", DEFAULT_LIMITS["max_profile_patterns"]))
    profile_patterns: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    total_patterns = 0
    rejected_conflicting_patterns = 0
    for profile in profiles:
        patterns = _enumerate_profile_patterns(
            profile,
            section_meetings=section_meetings,
            section_quality_cost_by_id=section_quality_cost_by_id,
            assignment_quality_cost_by_key=assignment_quality_cost_by_key,
            max_patterns=max_patterns,
        )
        if patterns is None or not patterns:
            return None
        profile_patterns.append((profile, patterns))
        total_patterns += len(patterns)
        rejected_conflicting_patterns += int(profile.get("rejected_conflicting_patterns") or 0)
        if total_patterns > max_patterns:
            return None

    if total_patterns >= int(profile_compression.get("student_level_variable_count") or 0):
        return None

    profile_model = cp_model.CpModel()
    pattern_vars: dict[tuple[int, int], Any] = {}
    served_terms: list[Any] = []
    requested_terms: list[Any] = []
    moved_terms: list[Any] = []
    changed_terms: list[Any] = []
    quality_terms: list[Any] = []
    capacity_terms: dict[int, list[Any]] = defaultdict(list)

    for profile_index, (profile, patterns) in enumerate(profile_patterns):
        profile_size = len(profile["student_ids"])
        profile_vars: list[Any] = []
        for pattern_index, pattern in enumerate(patterns):
            var = profile_model.NewIntVar(
                0,
                profile_size,
                f"p{profile_index}_pattern{pattern_index}",
            )
            pattern_vars[(profile_index, pattern_index)] = var
            profile_vars.append(var)
            if pattern["served"]:
                served_terms.append(var * int(pattern["served"]))
            if pattern.get("requested_served"):
                requested_terms.append(var * int(pattern["requested_served"]))
            if pattern["moved"]:
                moved_terms.append(var * int(pattern["moved"]))
            if pattern["changed_count"]:
                changed_terms.append(var * int(pattern["changed_count"]))
            if pattern.get("quality_penalty"):
                quality_terms.append(var * int(pattern["quality_penalty"]))
            for section_id, count in pattern["section_counts"].items():
                if count:
                    capacity_terms[int(section_id)].append(var * int(count))
        profile_model.Add(sum(profile_vars) == profile_size)

    for section_id, terms in capacity_terms.items():
        if not terms:
            continue
        profile_model.Add(
            sum(terms) + int(fixed_occupancy_by_section.get(section_id, 0))
            <= int(capacity_by_section.get(section_id, 0))
        )

    warm_start = _add_profile_pattern_solver_hints(
        profile_model,
        profile_patterns=profile_patterns,
        pattern_vars=pattern_vars,
    )
    solver, status, objective_trace = _solve_lexicographic_repair(
        profile_model,
        limits=limits,
        policy=mode_policy,
        served_expr=sum(served_terms) if served_terms else 0,
        requested_expr=sum(requested_terms) if requested_terms else 0,
        moved_expr=sum(moved_terms) if moved_terms else 0,
        changed_expr=sum(changed_terms) if changed_terms else 0,
        quality_expr=sum(quality_terms) if quality_terms else 0,
    )
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None

    chosen: dict[tuple[int, str], int] = {}
    for profile_index, (profile, patterns) in enumerate(profile_patterns):
        students = list(profile["student_ids"])
        offset = 0
        for pattern_index, pattern in enumerate(patterns):
            count = int(solver.Value(pattern_vars[(profile_index, pattern_index)]))
            if count <= 0:
                continue
            for student_id in students[offset : offset + count]:
                for course, section_id in pattern["assignment"].items():
                    chosen[(int(student_id), str(course))] = int(section_id)
            offset += count

    return {
        "chosen": chosen,
        "solver_status": _cp_sat_status_name(status),
        "objective_trace": objective_trace,
        "variables": len(pattern_vars),
        "conflict_policy": {
            "strategy": "profile_pattern_enumeration",
            "too_large": False,
            "logical_conflict_edges": 0,
            "max_conflict_edges": limits.get(
                "max_conflict_edges",
                DEFAULT_LIMITS["max_conflict_edges"],
            ),
            "at_most_one_constraints": 0,
            "pairwise_constraints": 0,
            "covered_pair_count": 0,
            "pattern_count": total_patterns,
            "rejected_conflicting_patterns": rejected_conflicting_patterns,
            "samples": [],
        },
        "profile_solver": {
            "enabled": True,
            "strategy": "profile_pattern_cp_sat",
            "profile_count": len(profiles),
            "pattern_count": total_patterns,
            "variables": len(pattern_vars),
            "student_count": len(student_ids),
            "status": _cp_sat_status_name(status),
        },
        "warm_start": warm_start,
    }


def _solver_profile_rows(
    *,
    student_ids: list[int],
    eligibility_context: Any,
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
) -> list[dict[str, Any]]:
    blocked_set = set(blocked_ids)
    profile_rows: dict[str, dict[str, Any]] = {}
    for sid in student_ids:
        student_policy = eligibility_context.students.get(sid)
        current_courses = current_by_student_course.get(sid, {})
        course_options = _student_course_option_signature(
            student_id=sid,
            current_courses=current_courses,
            option_ids_by_student_course=option_ids_by_student_course,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_set=blocked_set,
        )
        protected_assignments = tuple(
            sorted(
                int(section_id)
                for student_id, section_id in eligibility_context.protected_assignments
                if int(student_id) == int(sid)
            )
        )
        signature_payload = {
            "program": getattr(student_policy, "program", ""),
            "section": getattr(student_policy, "section", ""),
            "status": getattr(student_policy, "status", ""),
            "priority_group": getattr(student_policy, "priority_group", "normal"),
            "graduation_priority": bool(getattr(student_policy, "graduation_priority", False)),
            "mobility_policy": getattr(student_policy, "mobility_policy", "normal"),
            "protected": bool(getattr(student_policy, "protected", False)),
            "protection_reason": getattr(student_policy, "protection_reason", ""),
            "total_earned_credits": int(getattr(student_policy, "total_earned_credits", 0) or 0),
            "current_registered_credits": int(
                getattr(student_policy, "current_registered_credits", 0) or 0
            ),
            "blocked_target_course": sid in blocked_set and target_course not in current_courses,
            "course_options": course_options,
            "protected_assignments": protected_assignments,
        }
        signature = json.dumps(
            signature_payload, sort_keys=True, default=list, separators=(",", ":")
        )
        profile_id = hashlib.sha1(signature.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        profile = profile_rows.setdefault(
            profile_id,
            {
                "profile_id": profile_id,
                "student_ids": [],
                "representative_student_id": int(sid),
                "course_options": course_options,
                "blocked_target_course": signature_payload["blocked_target_course"],
                "rejected_conflicting_patterns": 0,
            },
        )
        profile["student_ids"].append(int(sid))

    return sorted(
        profile_rows.values(),
        key=lambda row: (-len(row["student_ids"]), row["profile_id"]),
    )


def _student_course_option_signature(
    *,
    student_id: int,
    current_courses: dict[str, int],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_set: set[int],
) -> list[dict[str, Any]]:
    course_options: list[dict[str, Any]] = []
    for course, current_section_id in sorted(current_courses.items()):
        course_options.append(
            {
                "course": course,
                "current_section_id": int(current_section_id),
                "option_ids": tuple(
                    sorted(option_ids_by_student_course.get((student_id, course), []))
                ),
                "required": True,
            }
        )
    for course in _optional_requested_courses_for_student(
        student_id=student_id,
        current_courses=current_courses,
        requested_courses_by_student=requested_courses_by_student,
        target_course=target_course,
        blocked_set=blocked_set,
    ):
        course_options.append(
            {
                "course": course,
                "current_section_id": None,
                "option_ids": tuple(
                    sorted(option_ids_by_student_course.get((student_id, course), []))
                ),
                "required": False,
                "target_blocked": student_id in blocked_set and course == target_course,
            }
        )
    return course_options


def _enumerate_profile_patterns(
    profile: dict[str, Any],
    *,
    section_meetings: dict[int, list[dict[str, str]]],
    section_quality_cost_by_id: dict[int, int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
    max_patterns: int,
) -> list[dict[str, Any]] | None:
    course_options = profile.get("course_options") or []
    optional_courses = {str(row["course"]) for row in course_options if not row.get("required")}
    target_blocked_courses = {
        str(row["course"])
        for row in course_options
        if not row.get("required") and row.get("target_blocked")
    }
    choice_rows: list[list[int | None]] = []
    product_size = 1
    for row in course_options:
        options = [int(section_id) for section_id in row.get("option_ids") or []]
        if row.get("required"):
            if not options:
                return []
            choices: list[int | None] = options
        else:
            choices = [None] + options
        product_size *= max(1, len(choices))
        if product_size > max_patterns:
            return None
        choice_rows.append(choices)

    patterns: list[dict[str, Any]] = []
    rejected_conflicts = 0
    for combo in product(*choice_rows):
        assignment: dict[str, int] = {}
        for row, section_id in zip(course_options, combo, strict=False):
            if section_id is not None:
                assignment[str(row["course"])] = int(section_id)
        if not _assignment_pattern_is_conflict_free(assignment, section_meetings):
            rejected_conflicts += 1
            continue
        changed_count = sum(
            1
            for row in course_options
            if row.get("required")
            and assignment.get(str(row["course"])) != int(row["current_section_id"])
        )
        section_counts = Counter(assignment.values())
        requested_served = sum(1 for course in optional_courses if assignment.get(course))
        quality_penalty = _profile_assignment_quality_penalty(
            profile=profile,
            assignment=assignment,
            section_quality_cost_by_id=section_quality_cost_by_id,
            assignment_quality_cost_by_key=assignment_quality_cost_by_key,
        )
        patterns.append(
            {
                "assignment": assignment,
                "section_counts": dict(section_counts),
                "served": 1
                if any(assignment.get(course) for course in target_blocked_courses)
                else 0,
                "requested_served": requested_served,
                "changed_count": changed_count,
                "moved": 1 if changed_count else 0,
                "quality_penalty": quality_penalty,
            }
        )
        if len(patterns) > max_patterns:
            return None
    profile["rejected_conflicting_patterns"] = rejected_conflicts
    return patterns


def _assignment_pattern_is_conflict_free(
    assignment: dict[str, int],
    section_meetings: dict[int, list[dict[str, str]]],
) -> bool:
    section_ids = list(assignment.values())
    for left_id, right_id in combinations(section_ids, 2):
        if _section_meetings_overlap(
            section_meetings.get(left_id, []),
            section_meetings.get(right_id, []),
        ):
            return False
    return True


def _profile_assignment_quality_penalty(
    *,
    profile: dict[str, Any],
    assignment: dict[str, int],
    section_quality_cost_by_id: dict[int, int],
    assignment_quality_cost_by_key: dict[tuple[int, str, int], int],
) -> int:
    representative_student_id = int(
        profile.get("representative_student_id") or (profile.get("student_ids") or [0])[0] or 0
    )
    total = 0
    for course, section_id in assignment.items():
        section_id = int(section_id)
        total += int(
            assignment_quality_cost_by_key.get(
                (representative_student_id, str(course), section_id),
                int(section_quality_cost_by_id.get(section_id, 0)),
            )
        )
    return total


def _student_profile_compression_summary(
    *,
    student_ids: list[int],
    eligibility_context: Any,
    current_by_student_course: dict[int, dict[str, int]],
    option_ids_by_student_course: dict[tuple[int, str], list[int]],
    requested_courses_by_student: dict[int, set[str]],
    target_course: str,
    blocked_ids: list[int],
    variable_count: int,
) -> dict[str, Any]:
    """Group students by solver-equivalent profile for audit and future compression."""

    blocked_set = set(blocked_ids)
    profile_rows: dict[str, dict[str, Any]] = {}
    student_count = len(student_ids)
    for sid in student_ids:
        student_policy = eligibility_context.students.get(sid)
        current_courses = current_by_student_course.get(sid, {})
        course_options: list[dict[str, Any]] = []
        for course, current_section_id in sorted(current_courses.items()):
            course_options.append(
                {
                    "course": course,
                    "current_section_id": int(current_section_id),
                    "option_ids": tuple(
                        sorted(option_ids_by_student_course.get((sid, course), []))
                    ),
                    "required": True,
                }
            )
        optional_requested_courses = _optional_requested_courses_for_student(
            student_id=sid,
            current_courses=current_courses,
            requested_courses_by_student=requested_courses_by_student,
            target_course=target_course,
            blocked_set=blocked_set,
        )
        for course in optional_requested_courses:
            course_options.append(
                {
                    "course": course,
                    "current_section_id": None,
                    "option_ids": tuple(
                        sorted(option_ids_by_student_course.get((sid, course), []))
                    ),
                    "required": False,
                    "target_blocked": sid in blocked_set and course == target_course,
                }
            )
        protected_assignments = tuple(
            sorted(
                int(section_id)
                for student_id, section_id in eligibility_context.protected_assignments
                if int(student_id) == int(sid)
            )
        )
        signature_payload = {
            "program": getattr(student_policy, "program", ""),
            "section": getattr(student_policy, "section", ""),
            "status": getattr(student_policy, "status", ""),
            "priority_group": getattr(student_policy, "priority_group", "normal"),
            "graduation_priority": bool(getattr(student_policy, "graduation_priority", False)),
            "mobility_policy": getattr(student_policy, "mobility_policy", "normal"),
            "protected": bool(getattr(student_policy, "protected", False)),
            "protection_reason": getattr(student_policy, "protection_reason", ""),
            "total_earned_credits": int(getattr(student_policy, "total_earned_credits", 0) or 0),
            "current_registered_credits": int(
                getattr(student_policy, "current_registered_credits", 0) or 0
            ),
            "blocked_target_course": sid in blocked_set and target_course not in current_courses,
            "course_options": course_options,
            "protected_assignments": protected_assignments,
        }
        signature = json.dumps(
            signature_payload, sort_keys=True, default=list, separators=(",", ":")
        )
        profile_id = hashlib.sha1(signature.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        option_variable_count = sum(len(row["option_ids"]) for row in course_options)
        profile = profile_rows.setdefault(
            profile_id,
            {
                "profile_id": profile_id,
                "student_ids": [],
                "student_count": 0,
                "current_course_count": len(current_courses),
                "requested_course_count": len(optional_requested_courses),
                "option_variable_count": option_variable_count,
            },
        )
        profile["student_ids"].append(int(sid))
        profile["student_count"] += 1

    profiles = sorted(
        profile_rows.values(),
        key=lambda row: (-int(row["student_count"]), row["profile_id"]),
    )
    profile_count = len(profiles)
    largest_profile_size = max((int(row["student_count"]) for row in profiles), default=0)
    estimated_profile_variable_count = sum(
        int(row.get("option_variable_count") or 0) for row in profiles
    )
    return {
        "enabled": True,
        "strategy": "solver_option_signature_v1",
        "student_count": student_count,
        "profile_count": profile_count,
        "largest_profile_size": largest_profile_size,
        "student_level_variable_count": int(variable_count),
        "estimated_profile_variable_count": int(estimated_profile_variable_count),
        "estimated_variable_reduction": max(
            0,
            int(variable_count) - int(estimated_profile_variable_count),
        ),
        "compression_ratio": round(profile_count / student_count, 4) if student_count else 0,
        "sample_profiles": [
            {
                "profile_id": row["profile_id"],
                "student_count": row["student_count"],
                "sample_student_ids": row["student_ids"][:5],
                "current_course_count": row["current_course_count"],
                "requested_course_count": row["requested_course_count"],
                "option_variable_count": row["option_variable_count"],
            }
            for row in profiles[:10]
        ],
    }


def _add_student_time_conflict_constraints(
    model: Any,
    *,
    by_student_vars: dict[int, list[tuple[str, int, Any]]],
    section_meetings: dict[int, list[dict[str, str]]],
    limits: dict[str, int],
) -> dict[str, Any]:
    """Build no-time-clash constraints with bounded, auditable complexity."""

    max_edges = int(limits.get("max_conflict_edges", DEFAULT_LIMITS["max_conflict_edges"]))
    logical_edges = 0
    at_most_one_constraints = 0
    pairwise_constraints = 0
    covered_pairs: set[tuple[Any, ...]] = set()
    samples: list[dict[str, Any]] = []

    def maybe_record_sample(student_id: int, strategy: str, left: Any, right: Any) -> None:
        if len(samples) >= 12:
            return
        samples.append(
            {
                "student_id": student_id,
                "strategy": strategy,
                "left": {"course_key": left[0], "section_id": left[1]},
                "right": {"course_key": right[0], "section_id": right[1]},
            }
        )

    def pair_key(student_id: int, left: Any, right: Any) -> tuple[Any, ...]:
        left_key = (str(left[0]), int(left[1]))
        right_key = (str(right[0]), int(right[1]))
        if right_key < left_key:
            left_key, right_key = right_key, left_key
        return (int(student_id), left_key, right_key)

    def too_large() -> dict[str, Any]:
        return {
            "strategy": "equal_slot_at_most_one_then_pairwise_overlap",
            "too_large": True,
            "logical_conflict_edges": logical_edges,
            "max_conflict_edges": max_edges,
            "at_most_one_constraints": at_most_one_constraints,
            "pairwise_constraints": pairwise_constraints,
            "covered_pair_count": len(covered_pairs),
            "samples": samples,
        }

    for student_id, rows in by_student_vars.items():
        groups: dict[tuple[str, str, str], list[tuple[str, int, Any]]] = defaultdict(list)
        for row in rows:
            _course, section_id, _var = row
            for meeting in section_meetings.get(section_id, []):
                groups[
                    (
                        str(meeting.get("day") or ""),
                        str(meeting.get("start_time") or ""),
                        str(meeting.get("end_time") or ""),
                    )
                ].append(row)
        for group_rows in groups.values():
            deduped = list(
                {
                    (course, section_id): (course, section_id, var)
                    for course, section_id, var in group_rows
                }.values()
            )
            if len(deduped) < 2:
                continue
            cross_pairs = [
                (left, right) for left, right in combinations(deduped, 2) if left[0] != right[0]
            ]
            if not cross_pairs:
                continue
            if logical_edges + len(cross_pairs) > max_edges:
                return too_large()
            model.AddAtMostOne([var for _course, _section_id, var in deduped])
            at_most_one_constraints += 1
            logical_edges += len(cross_pairs)
            for left, right in cross_pairs:
                covered_pairs.add(pair_key(student_id, left, right))
                maybe_record_sample(student_id, "at_most_one", left, right)

    for student_id, rows in by_student_vars.items():
        for left, right in combinations(rows, 2):
            left_course, left_section_id, left_var = left
            right_course, right_section_id, right_var = right
            if left_course == right_course:
                continue
            key = pair_key(student_id, left, right)
            if key in covered_pairs:
                continue
            if _section_meetings_overlap(
                section_meetings.get(left_section_id, []),
                section_meetings.get(right_section_id, []),
            ):
                if logical_edges + 1 > max_edges:
                    return too_large()
                model.Add(left_var + right_var <= 1)
                pairwise_constraints += 1
                logical_edges += 1
                maybe_record_sample(student_id, "pairwise", left, right)

    return {
        "strategy": "equal_slot_at_most_one_then_pairwise_overlap",
        "too_large": False,
        "logical_conflict_edges": logical_edges,
        "max_conflict_edges": max_edges,
        "at_most_one_constraints": at_most_one_constraints,
        "pairwise_constraints": pairwise_constraints,
        "covered_pair_count": len(covered_pairs),
        "samples": samples,
    }


def _solve_lexicographic_repair(
    model: Any,
    *,
    limits: dict[str, int],
    policy: dict[str, Any],
    served_expr: Any,
    requested_expr: Any = 0,
    moved_expr: Any = 0,
    changed_expr: Any = 0,
    quality_expr: Any = 0,
) -> tuple[Any, int, list[dict[str, Any]]]:
    """Solve the mode objective in auditable priority stages."""

    trace: list[dict[str, Any]] = []
    max_seconds = float(limits.get("max_solver_seconds", DEFAULT_LIMITS["max_solver_seconds"]))
    max_seconds *= float(policy.get("max_solver_seconds_multiplier") or 1.0)
    expr_by_name = {
        "served": served_expr,
        "requested": requested_expr,
        "moved": moved_expr,
        "changed": changed_expr,
        "quality": quality_expr,
    }
    stages = [
        (str(stage["name"]), str(stage["sense"]), expr_by_name[str(stage["expr"])])
        for stage in policy.get("stages", [])
    ]
    if not stages:
        stages = [("maximize_blocked_recovery", "max", served_expr)]
    stage_seconds = max(1.0, max_seconds / float(len(stages)))

    solver = None
    last_status = cp_model.UNKNOWN
    for index, (name, sense, expr) in enumerate(stages):
        if sense == "max":
            model.Maximize(expr)
        else:
            model.Minimize(expr)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = stage_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 17
        stage_started = perf_counter()
        status = solver.Solve(model)
        stage_runtime_ms = int((perf_counter() - stage_started) * 1000)
        last_status = status
        status_name = _cp_sat_status_name(status)
        value = (
            _cp_expr_value(solver, expr)
            if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
            else None
        )
        trace.append(
            {
                "stage": index + 1,
                "name": name,
                "sense": sense,
                "status": status_name,
                "value": value,
                "time_limit_seconds": round(stage_seconds, 3),
                "runtime_ms": stage_runtime_ms,
                "wall_time_seconds": _solver_float_metric(solver, "WallTime"),
                "branches": _solver_int_metric(solver, "NumBranches"),
                "conflicts": _solver_int_metric(solver, "NumConflicts"),
                "objective_value": _solver_float_metric(solver, "ObjectiveValue")
                if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
                else None,
                "best_objective_bound": _solver_float_metric(solver, "BestObjectiveBound")
                if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
                else None,
                "proof_status": "proven_optimal" if status == cp_model.OPTIMAL else status_name,
            }
        )
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return solver, status, trace
        if index < len(stages) - 1:
            _fix_cp_expr_value(model, expr, int(value or 0))
    return solver, last_status, trace


def _solver_budget_summary(
    *,
    limits: dict[str, int],
    mode_policy: dict[str, Any],
    objective_trace: list[dict[str, Any]],
    runtime_ms: int,
) -> dict[str, Any]:
    base_seconds = float(limits.get("max_solver_seconds", DEFAULT_LIMITS["max_solver_seconds"]))
    multiplier = float(mode_policy.get("max_solver_seconds_multiplier") or 1.0)
    total_seconds = base_seconds * multiplier
    stage_count = max(1, len(objective_trace))
    return {
        "enabled": True,
        "policy": "per_candidate_staged_cp_sat_budget",
        "base_seconds": base_seconds,
        "mode_multiplier": multiplier,
        "total_seconds": round(total_seconds, 3),
        "stage_count": stage_count,
        "stage_seconds": round(total_seconds / stage_count, 3),
        "runtime_ms": int(runtime_ms),
        "stage_runtime_ms": sum(int(row.get("runtime_ms") or 0) for row in objective_trace),
        "status_counts": dict(
            Counter(str(row.get("status") or "unknown") for row in objective_trace)
        ),
        "proof_complete": all(row.get("status") == "optimal" for row in objective_trace),
    }


def _solver_float_metric(solver: Any, method_name: str) -> float | None:
    method = getattr(solver, method_name, None)
    if not callable(method):
        return None
    try:
        return round(float(method()), 6)
    except Exception:  # pragma: no cover - metric methods are optional across versions.
        return None


def _solver_int_metric(solver: Any, method_name: str) -> int | None:
    method = getattr(solver, method_name, None)
    if not callable(method):
        return None
    try:
        return int(method())
    except Exception:  # pragma: no cover - metric methods are optional across versions.
        return None


def _cp_expr_value(solver: Any, expr: Any) -> int:
    if isinstance(expr, int):
        return int(expr)
    return int(solver.Value(expr))


def _fix_cp_expr_value(model: Any, expr: Any, value: int) -> None:
    if isinstance(expr, int):
        if int(expr) != value:
            model.AddBoolOr([])
        return
    model.Add(expr == value)


def _cp_sat_status_name(status: int) -> str:
    if cp_model is None:
        return "solver_unavailable"
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
        cp_model.UNKNOWN: "unknown",
    }.get(status, "unknown")


def _blocked_student_ids_for_run(
    run: TimetableRepairRun,
    component: dict[str, Any],
) -> list[int]:
    payload = run.request_payload or {}
    demand = payload.get("blocked_demand") if isinstance(payload, dict) else {}
    if isinstance(demand, dict):
        active = _normalise_student_ids(demand.get("active_student_ids") or [])
        if active:
            return active
    requested = _normalise_student_ids(payload.get("blocked_student_ids") or [])
    if requested:
        return requested
    component_demand = component.get("blocked_demand") if isinstance(component, dict) else {}
    if isinstance(component_demand, dict):
        active = _normalise_student_ids(component_demand.get("active_student_ids") or [])
        if active:
            return active
    return [
        int(row["student_id"])
        for row in component.get("students", [])
        if row.get("student_id") is not None and row.get("requests_target_course")
    ]


def _term_section_course_key(section: Any) -> str:
    return str(section.course_key or section.course_code or "").strip()


def _safe_var_name(value: str) -> str:
    cleaned = []
    for ch in str(value):
        cleaned.append(ch if ch.isalnum() else "_")
    return "".join(cleaned)[:80] or "course"


def _section_capacity(section: Any, total_current: int) -> int:
    candidates = [total_current]
    for value in (section.available_capacity, section.registered_count):
        try:
            if value is not None:
                candidates.append(int(value))
        except (TypeError, ValueError):
            continue
    return max(candidates) if candidates else 0


def _candidate_section_meetings(
    scenario_id: int,
    placement: SectionPlacement,
    candidate: TimetableRepairCandidate | dict[str, Any],
    *,
    section_ids: set[int],
) -> dict[int, list[dict[str, str]]]:
    meetings: dict[int, list[dict[str, str]]] = defaultdict(list)
    move_by_placement_id = {
        int(move["placement_id"]): move
        for move in _candidate_move_set(candidate, placement=placement)
        if move.get("placement_id")
    }
    rows = SectionPlacement.objects.filter(
        board__scenario_id=scenario_id,
        term_section_id__in=section_ids,
    ).select_related("term_section")
    for row in rows:
        if row.id in move_by_placement_id:
            move = move_by_placement_id[row.id]
            meetings[row.term_section_id].append(
                {
                    "day": str(move.get("day") or ""),
                    "start_time": str(move.get("start") or ""),
                    "end_time": str(move.get("end") or ""),
                    "room": str(move.get("room") or ""),
                }
            )
        else:
            meetings[row.term_section_id].append(
                {
                    "day": row.day,
                    "start_time": row.start_time,
                    "end_time": row.end_time,
                    "room": row.room or "",
                }
            )
    return meetings


def _candidate_move_scope_payload(
    candidate: TimetableRepairCandidate | dict[str, Any],
) -> dict[str, Any]:
    metrics = _candidate_field(candidate, "metrics_json", {}) or {}
    payload = metrics.get("move_scope") if isinstance(metrics, dict) else None
    return payload if isinstance(payload, dict) else {}


def _candidate_move_set(
    candidate: TimetableRepairCandidate | dict[str, Any],
    *,
    placement: SectionPlacement | None = None,
) -> list[dict[str, Any]]:
    payload = _candidate_move_scope_payload(candidate)
    moves = payload.get("moves") if isinstance(payload.get("moves"), list) else []
    normalized = [
        {
            "placement_id": int(move["placement_id"]),
            "day": str(move.get("day") or ""),
            "start": str(move.get("start") or move.get("start_time") or ""),
            "end": str(move.get("end") or move.get("end_time") or ""),
            "room": str(move.get("room") or ""),
            "kind": str(move.get("kind") or ""),
            "is_anchor": bool(move.get("is_anchor")),
        }
        for move in moves
        if isinstance(move, dict) and move.get("placement_id")
    ]
    if normalized:
        return normalized
    placement_id = placement.id if placement is not None else None
    if placement_id is None and not isinstance(candidate, dict):
        placement_id = candidate.run.target_placement_id
    if placement_id is None:
        return []
    return [
        {
            "placement_id": int(placement_id),
            "day": str(_candidate_field(candidate, "day", "") or ""),
            "start": str(_candidate_field(candidate, "start_time", "") or ""),
            "end": str(_candidate_field(candidate, "end_time", "") or ""),
            "room": str(_candidate_field(candidate, "room", "") or ""),
            "kind": "",
            "is_anchor": True,
        }
    ]


def _section_meetings_overlap(
    left: list[dict[str, str]],
    right: list[dict[str, str]],
) -> bool:
    return any(_meeting_overlap(a, b) for a in left for b in right)


def _repair_evaluation_budget(
    *,
    limits: dict[str, int],
    selected_candidate_count: int,
) -> dict[str, Any]:
    limit_seconds = int(
        limits.get(
            "max_total_solver_seconds",
            DEFAULT_LIMITS["max_total_solver_seconds"],
        )
    )
    selected_count = max(0, int(selected_candidate_count))
    return {
        "enabled": True,
        "policy": "bounded_interactive_candidate_evaluation",
        "limit_seconds": limit_seconds,
        "limit_ms": limit_seconds * 1000,
        "selected_candidate_count": selected_count,
        "estimated_seconds_per_candidate": round(
            limit_seconds / selected_count,
            3,
        )
        if selected_count
        else 0,
    }


def _repair_evaluation_budget_state(
    started: float,
    *,
    limits: dict[str, int],
    selected_candidate_count: int,
) -> dict[str, Any]:
    budget = _repair_evaluation_budget(
        limits=limits,
        selected_candidate_count=selected_candidate_count,
    )
    elapsed_ms = int((perf_counter() - started) * 1000)
    limit_ms = int(budget["limit_ms"])
    return {
        **budget,
        "elapsed_ms": elapsed_ms,
        "remaining_ms": max(0, limit_ms - elapsed_ms),
        "exhausted": elapsed_ms >= limit_ms,
    }


def _repair_candidate_worker_plan(
    limits: dict[str, int],
    *,
    selected_candidate_count: int,
) -> dict[str, Any]:
    requested = max(1, int(limits.get("max_candidate_workers", 1) or 1))
    max_workers = min(requested, max(1, selected_candidate_count), 4)
    enabled_by_setting = bool(
        getattr(settings, "TIMETABLE_REPAIR_PARALLEL_CANDIDATES_ENABLED", True)
    )
    disabled_reason = ""
    if selected_candidate_count < 2:
        disabled_reason = "single_candidate_batch"
    elif not enabled_by_setting:
        disabled_reason = "settings_disabled"
    elif _running_under_pytest():
        disabled_reason = "pytest_thread_isolation"
    elif max_workers <= 1:
        disabled_reason = "single_worker_limit"

    enabled = not disabled_reason
    return {
        "enabled": enabled,
        "strategy": (
            "thread_pool_in_memory_candidate_compute"
            if enabled
            else "serial_in_memory_candidate_compute"
        ),
        "dispatch": "ThreadPoolExecutor" if enabled else "serial",
        "worker_count": max_workers if enabled else 1,
        "requested_worker_count": requested,
        "selected_candidate_count": selected_candidate_count,
        "database_write_policy": "deferred_bulk_persist_after_ranking",
        "budget_policy": "workers_check_shared_elapsed_budget_before_solver",
        "reason": (
            "Candidate solving runs in bounded workers; audit rows are written after ranking."
            if enabled
            else disabled_reason
        ),
        "limits": limits,
    }


def _candidate_parallelism_metrics(worker_plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(worker_plan.get("enabled")),
        "strategy": worker_plan.get("strategy") or "",
        "dispatch": worker_plan.get("dispatch") or "",
        "worker_count": int(worker_plan.get("worker_count") or 1),
        "requested_worker_count": int(worker_plan.get("requested_worker_count") or 1),
        "selected_candidate_count": int(worker_plan.get("selected_candidate_count") or 0),
        "database_write_policy": worker_plan.get("database_write_policy") or "",
        "budget_policy": worker_plan.get("budget_policy") or "",
        "reason": worker_plan.get("reason") or "",
    }


def _running_under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _add_current_assignment_solver_hints(
    model: Any,
    *,
    x: dict[tuple[int, str, int], Any],
    current_by_student_course: dict[int, dict[str, int]],
    served_by_student: dict[int, Any] | None = None,
    requested_served_by_student_course: dict[tuple[int, str], Any] | None = None,
    student_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Seed CP-SAT with the current assignment so bounded repairs start stable."""

    allowed_students = {int(sid) for sid in student_ids} if student_ids is not None else None
    hint_count = 0
    current_assignment_hint_count = 0
    served_zero_hint_count = 0
    try:
        for (student_id, course_key, section_id), var in x.items():
            if allowed_students is not None and int(student_id) not in allowed_students:
                continue
            value = (
                1
                if int(current_by_student_course.get(int(student_id), {}).get(str(course_key), -1))
                == int(section_id)
                else 0
            )
            model.AddHint(var, value)
            hint_count += 1
            if value:
                current_assignment_hint_count += 1
        for student_id, var in (served_by_student or {}).items():
            if allowed_students is not None and int(student_id) not in allowed_students:
                continue
            model.AddHint(var, 0)
            hint_count += 1
            served_zero_hint_count += 1
        hinted_served_var_ids = {id(var) for var in (served_by_student or {}).values()}
        for (student_id, _course), var in (requested_served_by_student_course or {}).items():
            if id(var) in hinted_served_var_ids:
                continue
            if allowed_students is not None and int(student_id) not in allowed_students:
                continue
            model.AddHint(var, 0)
            hint_count += 1
            served_zero_hint_count += 1
    except Exception as exc:  # pragma: no cover - defensive against OR-Tools API differences.
        return {
            "enabled": True,
            "used": False,
            "strategy": "current_assignment_cp_sat_hint",
            "reason": "hint_api_unavailable_or_rejected",
            "error": str(exc)[:200],
        }
    return {
        "enabled": True,
        "used": hint_count > 0,
        "strategy": "current_assignment_cp_sat_hint",
        "hint_count": hint_count,
        "current_assignment_hint_count": current_assignment_hint_count,
        "served_zero_hint_count": served_zero_hint_count,
    }


def _add_profile_pattern_solver_hints(
    model: Any,
    *,
    profile_patterns: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    pattern_vars: dict[tuple[int, int], Any],
) -> dict[str, Any]:
    """Seed profile-pattern CP-SAT with the no-move pattern when it exists."""

    hint_count = 0
    profile_current_pattern_count = 0
    try:
        for profile_index, (profile, patterns) in enumerate(profile_patterns):
            profile_size = len(profile.get("student_ids") or [])
            current_pattern_index = None
            for pattern_index, pattern in enumerate(patterns):
                if _profile_pattern_preserves_current(profile, pattern):
                    current_pattern_index = pattern_index
                    break
            for pattern_index, _pattern in enumerate(patterns):
                value = profile_size if pattern_index == current_pattern_index else 0
                model.AddHint(pattern_vars[(profile_index, pattern_index)], value)
                hint_count += 1
            if current_pattern_index is not None:
                profile_current_pattern_count += 1
    except Exception as exc:  # pragma: no cover - defensive against OR-Tools API differences.
        return {
            "enabled": True,
            "used": False,
            "strategy": "profile_current_pattern_cp_sat_hint",
            "reason": "hint_api_unavailable_or_rejected",
            "error": str(exc)[:200],
        }
    return {
        "enabled": True,
        "used": hint_count > 0,
        "strategy": "profile_current_pattern_cp_sat_hint",
        "hint_count": hint_count,
        "profile_current_pattern_count": profile_current_pattern_count,
    }


def _profile_pattern_preserves_current(
    profile: dict[str, Any],
    pattern: dict[str, Any],
) -> bool:
    assignment = pattern.get("assignment") or {}
    for row in profile.get("course_options") or []:
        course = str(row.get("course") or "")
        if row.get("required"):
            if int(assignment.get(course, -1)) != int(row.get("current_section_id") or -1):
                return False
            continue
        if course in assignment:
            return False
    return True


def _meeting_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    if str(left.get("day") or "").strip().upper() != str(right.get("day") or "").strip().upper():
        return False
    try:
        return _minutes(left.get("start_time", "")) < _minutes(
            right.get("end_time", "")
        ) and _minutes(left.get("end_time", "")) > _minutes(right.get("start_time", ""))
    except ValueError:
        return False


def _minutes(value: str) -> int:
    raw = str(value or "").strip()
    hour, minute = raw.split(":", 1)
    h = int(hour)
    m = int(minute)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(raw)
    return h * 60 + m


def _section_summary(section: Any | None) -> dict[str, Any] | None:
    if section is None:
        return None
    return {
        "term_section_id": section.id,
        "course_key": section.course_key,
        "course_code": section.course_code,
        "section": section.section,
    }


def _locked_cells_for_scenario(scenario_id: int, exclude_placement_id: int) -> list[dict[str, str]]:
    return [
        {
            "day": row.day,
            "start_time": row.start_time,
            "room": row.room or "",
        }
        for row in SectionPlacement.objects.filter(
            board__scenario_id=scenario_id,
            is_locked=True,
        ).exclude(id=exclude_placement_id)
    ]


def _component_locked_summary(scenario_id: int, section_ids: set[int]) -> dict[str, Any]:
    rows = list(
        SectionPlacement.objects.filter(
            board__scenario_id=scenario_id,
            term_section_id__in=section_ids,
            is_locked=True,
        )
        .select_related("board", "term_section")
        .order_by("term_section__course_key", "term_section__section", "day", "start_time")
    )
    return {
        "locked_placement_count": len(rows),
        "locked_section_ids": sorted({row.term_section_id for row in rows}),
        "samples": [
            {
                "placement_id": row.id,
                "term_section_id": row.term_section_id,
                "course_key": row.term_section.course_key,
                "section": row.term_section.section,
                "board": row.board.label,
                "day": row.day,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "room": row.room or "",
            }
            for row in rows[:20]
        ],
    }


def _student_component_rows(
    student_ids: set[int],
    scenario_id: int,
    target_course: str,
) -> list[dict[str, Any]]:
    students = {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=student_ids).values(
            "student_id",
            "program",
            "section",
            "status",
            "total_earned_credits",
            "current_registered_credits",
        )
    }
    demands_by_student: dict[int, list[Any]] = defaultdict(list)
    for demand in load_scenario_course_demands(scenario_id):
        if int(demand.student_id) in student_ids:
            demands_by_student[int(demand.student_id)].append(demand)
    assigned_counts = Counter(
        StudentTermSection.objects.filter(
            student_id__in=student_ids,
            term_section__scenario_id=scenario_id,
        ).values_list("student_id", flat=True)
    )
    rows = []
    for sid in sorted(student_ids):
        student = students.get(sid, {})
        demands = demands_by_student.get(sid, [])
        requested_courses = [str(demand.course_key) for demand in demands if demand.course_key]
        priority_group, graduation_priority, protected, protection_reason = (
            classify_repair_student_policy(
                status=student.get("status") or "",
                total_earned_credits=int(student.get("total_earned_credits") or 0),
                current_registered_credits=int(student.get("current_registered_credits") or 0),
            )
        )
        rows.append(
            {
                "student_id": sid,
                "program": student.get("program") or "",
                "section": student.get("section") or "",
                "status": student.get("status") or "",
                "priority_group": priority_group,
                "graduation_priority": graduation_priority,
                "protected": protected,
                "protection_reason": protection_reason,
                "primary_term": demands[0].primary_term if demands else None,
                "recommended_course_count": len(requested_courses),
                "requests_target_course": target_course in requested_courses,
                "current_assignment_count": assigned_counts.get(sid, 0),
                "total_earned_credits": student.get("total_earned_credits"),
                "current_registered_credits": student.get("current_registered_credits"),
            }
        )
    return rows


def _profile_count(student_rows: list[dict[str, Any]]) -> int:
    signatures = {
        (
            row.get("program", ""),
            row.get("section", ""),
            row.get("status", ""),
            row.get("primary_term"),
            row.get("recommended_course_count"),
            row.get("requests_target_course"),
            row.get("current_assignment_count"),
        )
        for row in student_rows
    }
    return len(signatures)


def _students_requesting_course(scenario_id: int, course_key: str, limit: int) -> list[int]:
    out = []
    seen: set[int] = set()
    for demand in load_scenario_course_demands(scenario_id, course_keys=[course_key]):
        if demand.student_id in seen:
            continue
        seen.add(demand.student_id)
        out.append(demand.student_id)
        if len(out) >= limit:
            break
    return out


def _normalise_limits(limits: dict[str, int] | None) -> dict[str, int]:
    active = dict(DEFAULT_LIMITS)
    if limits:
        for key in active:
            try:
                value = int(limits.get(key, active[key]))
            except (TypeError, ValueError):
                continue
            if value > 0:
                active[key] = min(value, active[key] * 4)
    return active


def _normalise_student_ids(values: list[int]) -> list[int]:
    out = []
    seen = set()
    for value in values:
        try:
            sid = int(value)
        except (TypeError, ValueError):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _normalise_mode(mode: str) -> str:
    value = str(mode or TimetableRepairRun.MODE_CONSERVATIVE).strip().lower()
    allowed = {item[0] for item in TimetableRepairRun.MODE_CHOICES}
    return value if value in allowed else TimetableRepairRun.MODE_CONSERVATIVE


def _normalise_move_scope(move_scope: str | None) -> str:
    value = str(move_scope or MOVE_SCOPE_SINGLE_SESSION).strip().lower()
    return value if value in MOVE_SCOPE_CHOICES else MOVE_SCOPE_SINGLE_SESSION


def _log(
    run: TimetableRepairRun,
    level: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    candidate: TimetableRepairCandidate | None = None,
) -> None:
    TimetableRepairSolverLog.objects.create(
        run=run,
        candidate=candidate,
        level=level,
        message=message,
        payload_json=payload or {},
    )
