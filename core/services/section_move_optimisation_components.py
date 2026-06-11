"""Blueprint component boundaries for section move optimisation.

The heavy implementation still lives in ``timetable_repair`` while this module
defines the professional component shape.  Each component receives the existing
implementation function as a dependency, so this refactor adds architecture
boundaries without duplicating solver logic or changing behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateMoveGenerator:
    """Generate candidate section placements for the target section."""

    prepare_rows: Callable[..., list[dict[str, Any]]]

    name: str = "CandidateMoveGenerator"

    def generate(
        self,
        placement: Any,
        *,
        limits: dict[str, int],
        planning_scope: dict[str, Any] | None = None,
        move_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.prepare_rows(
            placement,
            limits=limits,
            planning_scope=planning_scope,
            move_scope=move_scope,
        )


@dataclass(frozen=True)
class HardFeasibilityChecker:
    """Reject impossible section moves before student reassignment solving."""

    check_candidate_section_move: Callable[..., list[dict[str, Any]]]

    name: str = "HardFeasibilityChecker"

    def check(
        self,
        placement: Any,
        *,
        day: str,
        start_time: str,
        end_time: str,
        room: str,
        room_reasons: list[dict[str, Any]],
        planning_scope: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self.check_candidate_section_move(
            placement,
            day=day,
            start_time=start_time,
            end_time=end_time,
            room=room,
            room_reasons=room_reasons,
            planning_scope=planning_scope,
        )


@dataclass(frozen=True)
class AffectedComponentBuilder:
    """Build the bounded cascading student/course/section neighbourhood."""

    build_component: Callable[..., dict[str, Any]]

    name: str = "AffectedComponentBuilder"

    def build(
        self,
        scenario_id: int,
        target_section: Any,
        *,
        blocked_student_ids: list[int] | None = None,
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return self.build_component(
            scenario_id,
            target_section,
            blocked_student_ids=blocked_student_ids,
            limits=limits,
        )


@dataclass(frozen=True)
class StudentProfileCompressor:
    """Detect solver-equivalent student profiles for smaller models."""

    compression_summary: Callable[..., dict[str, Any]]

    name: str = "StudentProfileCompressor"

    def summarise(self, **kwargs: Any) -> dict[str, Any]:
        return self.compression_summary(**kwargs)


@dataclass(frozen=True)
class RepairOptimiser:
    """Solve student reassignment for generated candidate moves."""

    evaluate_drafts: Callable[..., list[dict[str, Any]]]

    name: str = "RepairOptimiser"

    def solve_candidate_drafts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.evaluate_drafts(**kwargs)


@dataclass(frozen=True)
class ObjectiveManager:
    """Apply lexicographic candidate ranking priorities."""

    name: str = "ObjectiveManager"

    def rank_feasible_drafts(self, drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            [draft for draft in drafts if draft.get("status") == "feasible"],
            key=lambda payload: payload["_rank_key"],
        )

    def rank_by_candidate_id(self, drafts: list[dict[str, Any]]) -> dict[str, int]:
        return {
            draft["candidate_id"]: idx
            for idx, draft in enumerate(self.rank_feasible_drafts(drafts), start=1)
        }


@dataclass(frozen=True)
class ImpactScorer:
    """Attach runtime, budget, and ranking diagnostics to candidate metrics."""

    ranking_diagnostics: Callable[..., dict[str, Any]]

    name: str = "ImpactScorer"

    def attach_evaluation_metrics(
        self,
        drafts: list[dict[str, Any]],
        *,
        rank_by_key: dict[str, int],
        evaluation_budget: dict[str, Any],
        total_runtime_ms: int,
    ) -> None:
        for candidate in drafts:
            rank = rank_by_key.get(candidate["candidate_id"])
            metrics = dict(candidate.get("metrics") or {})
            evaluation = dict(metrics.get("evaluation") or {})
            evaluation["total_evaluation_runtime_ms"] = total_runtime_ms
            evaluation["budget"] = {
                **evaluation_budget,
                "elapsed_ms": total_runtime_ms,
                "exhausted": total_runtime_ms > int(evaluation_budget["limit_ms"]),
            }
            metrics["evaluation"] = evaluation
            metrics["ranking"] = self.ranking_diagnostics(candidate, rank)
            candidate["score_rank"] = rank
            candidate["metrics"] = metrics


@dataclass(frozen=True)
class ExplanationAndAuditEngine:
    """Persist candidate evidence, student changes, metrics, and solver logs."""

    persist_candidate_drafts: Callable[..., list[dict[str, Any]]]

    name: str = "ExplanationAndAuditEngine"

    def persist_candidates(
        self,
        run: Any,
        drafts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self.persist_candidate_drafts(run, drafts)


@dataclass(frozen=True)
class SectionMoveOptimisationComponents:
    """Composed blueprint components used by the section move engine."""

    candidate_move_generator: CandidateMoveGenerator
    hard_feasibility_checker: HardFeasibilityChecker
    affected_component_builder: AffectedComponentBuilder
    student_profile_compressor: StudentProfileCompressor
    repair_optimiser: RepairOptimiser
    objective_manager: ObjectiveManager
    impact_scorer: ImpactScorer
    explanation_and_audit_engine: ExplanationAndAuditEngine

    def metadata(self) -> dict[str, list[str]]:
        return {
            "components": [
                self.candidate_move_generator.name,
                self.hard_feasibility_checker.name,
                self.affected_component_builder.name,
                self.student_profile_compressor.name,
                self.repair_optimiser.name,
                self.objective_manager.name,
                self.impact_scorer.name,
                self.explanation_and_audit_engine.name,
            ]
        }
