"""Section move optimisation service facade.

This is the public name for the selected-section slot evaluation workflow.
It delegates to the audited timetable repair services because those already own
candidate generation, student reassignment solving, approval, apply, rollback,
and evidence reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.services.timetable_repair import (
    analyse_timetable_repair,
    apply_approved_repair_candidate,
    approve_repair_candidate,
    repair_candidate_detail,
    repair_run_detail,
    repair_run_report,
    rollback_repair_run,
)


@dataclass(frozen=True)
class SectionMoveOptimisationEngine:
    """Facade for section-slot move optimisation with actual student outcomes."""

    name: str = "SectionMoveOptimisationEngine"

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": "section_move_candidate_slots",
            "primary_metric": "actual_unresolved_students",
            "components": [
                "CandidateMoveGenerator",
                "HardFeasibilityChecker",
                "AffectedComponentBuilder",
                "StudentProfileCompressor",
                "RepairOptimiser",
                "ObjectiveManager",
                "ImpactScorer",
                "ExplanationAndAuditEngine",
            ],
        }

    def analyse_section_move(
        self,
        *,
        placement_id: int,
        blocked_student_ids: list[int] | None = None,
        blocked_requests: list[dict[str, Any]] | None = None,
        mode: str = "conservative",
        move_scope: str = "single_session",
        requested_by=None,
        limits: dict[str, int] | None = None,
        active_plan_filter: str | None = None,
    ) -> dict[str, Any]:
        detail = analyse_timetable_repair(
            placement_id=placement_id,
            blocked_student_ids=blocked_student_ids,
            blocked_requests=blocked_requests,
            mode=mode,
            move_scope=move_scope,
            requested_by=requested_by,
            limits=limits,
            active_plan_filter=active_plan_filter,
        )
        detail.setdefault("engine", self.metadata())
        return detail

    def run_detail(self, run_id: UUID | str) -> dict[str, Any]:
        detail = repair_run_detail(run_id)
        detail.setdefault("engine", self.metadata())
        return detail

    def candidate_detail(self, run_id: UUID | str, candidate_id: str) -> dict[str, Any]:
        detail = repair_candidate_detail(run_id, candidate_id)
        detail.setdefault("engine", self.metadata())
        return detail

    def report(self, run_id: UUID | str, *, candidate_id: str | None = None) -> dict[str, Any]:
        report = repair_run_report(run_id, candidate_id=candidate_id)
        report.setdefault("engine", self.metadata())
        return report

    def approve_candidate(
        self,
        run_id: UUID | str,
        candidate_id: str,
        *,
        decided_by=None,
        notes: str = "",
    ) -> dict[str, Any]:
        detail = approve_repair_candidate(
            run_id,
            candidate_id,
            decided_by=decided_by,
            notes=notes,
        )
        detail.setdefault("engine", self.metadata())
        return detail

    def apply_candidate(
        self,
        run_id: UUID | str,
        candidate_id: str,
        *,
        decided_by=None,
    ) -> dict[str, Any]:
        detail = apply_approved_repair_candidate(
            run_id,
            candidate_id,
            decided_by=decided_by,
        )
        detail.setdefault("engine", self.metadata())
        return detail

    def rollback_run(self, run_id: UUID | str, *, decided_by=None) -> dict[str, Any]:
        detail = rollback_repair_run(run_id, decided_by=decided_by)
        detail.setdefault("engine", self.metadata())
        return detail


section_move_optimisation_engine = SectionMoveOptimisationEngine()
