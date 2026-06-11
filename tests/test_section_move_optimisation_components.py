from __future__ import annotations

from core.services.section_move_optimisation import SectionMoveOptimisationEngine
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


def test_section_move_engine_metadata_exposes_blueprint_components() -> None:
    metadata = SectionMoveOptimisationEngine().metadata()

    assert metadata["primary_metric"] == "actual_unresolved_students"
    assert metadata["components"] == [
        "CandidateMoveGenerator",
        "HardFeasibilityChecker",
        "AffectedComponentBuilder",
        "StudentProfileCompressor",
        "RepairOptimiser",
        "ObjectiveManager",
        "ImpactScorer",
        "ExplanationAndAuditEngine",
    ]


def test_blueprint_components_delegate_without_duplicating_logic() -> None:
    calls: list[str] = []

    def prepare_rows(*args, **kwargs):
        calls.append("prepare")
        return [{"status": "feasible", "_rank_key": (0,), "candidate_id": "cand_001"}]

    def check_candidate_section_move(*args, **kwargs):
        calls.append("check")
        return [{"code": "TEST"}]

    def build_component(*args, **kwargs):
        calls.append("component")
        return {"students": []}

    def evaluate_drafts(**kwargs):
        calls.append("solve")
        return kwargs["prepared_rows"]

    def ranking_diagnostics(candidate, rank):
        calls.append("score")
        return {"rank": rank, "candidate_id": candidate["candidate_id"]}

    def persist_candidate_drafts(run, drafts):
        calls.append("audit")
        return drafts

    components = SectionMoveOptimisationComponents(
        candidate_move_generator=CandidateMoveGenerator(prepare_rows=prepare_rows),
        hard_feasibility_checker=HardFeasibilityChecker(
            check_candidate_section_move=check_candidate_section_move
        ),
        affected_component_builder=AffectedComponentBuilder(build_component=build_component),
        student_profile_compressor=StudentProfileCompressor(
            compression_summary=lambda **_kwargs: {"enabled": True}
        ),
        repair_optimiser=RepairOptimiser(evaluate_drafts=evaluate_drafts),
        objective_manager=ObjectiveManager(),
        impact_scorer=ImpactScorer(ranking_diagnostics=ranking_diagnostics),
        explanation_and_audit_engine=ExplanationAndAuditEngine(
            persist_candidate_drafts=persist_candidate_drafts
        ),
    )

    prepared = components.candidate_move_generator.generate(
        object(),
        limits={"max_candidates": 1},
    )
    assert components.hard_feasibility_checker.check(
        object(),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="A101",
        room_reasons=[],
    ) == [{"code": "TEST"}]
    assert components.affected_component_builder.build(1, object()) == {"students": []}

    drafts = components.repair_optimiser.solve_candidate_drafts(prepared_rows=prepared)
    rank_by_key = components.objective_manager.rank_by_candidate_id(drafts)
    components.impact_scorer.attach_evaluation_metrics(
        drafts,
        rank_by_key=rank_by_key,
        evaluation_budget={"limit_ms": 1000},
        total_runtime_ms=5,
    )
    persisted = components.explanation_and_audit_engine.persist_candidates(object(), drafts)

    assert persisted[0]["metrics"]["ranking"]["rank"] == 1
    assert calls == ["prepare", "check", "component", "solve", "score", "audit"]
