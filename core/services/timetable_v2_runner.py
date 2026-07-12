"""WS-E — reusable, safety-gated runner for the V2 optimiser.

Extracted from the timetable workspace view so the V2 pipeline — with its
snapshot → run → student/operational-regression → rollback safety gate — can run
either synchronously from a request OR asynchronously via the planner job
runner, without duplicating the gate. The gate lives here, beside the persist,
so a regression is always rolled back wherever the optimiser runs (closing the
audit's P1: a sync run that exceeds gunicorn's timeout is SIGKILLed before the
view-level rollback can fire).
"""

from __future__ import annotations

import logging
import traceback

from django.db import transaction

from core.models import SectionPlacement
from core.services.timetable_workspace import compute_scenario_safety_summary

logger = logging.getLogger(__name__)

_PLACEMENT_SNAPSHOT_FIELDS = (
    "id",
    "board_id",
    "term_section_id",
    "day",
    "start_time",
    "end_time",
    "room",
    "is_locked",
    "created_at",
    "updated_at",
)


def snapshot_scenario_placements(scenario_id: int) -> list[dict[str, object]]:
    """Capture current placements so an unsafe optimiser run can be restored."""
    return list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .order_by("id")
        .values(*_PLACEMENT_SNAPSHOT_FIELDS)
    )


def restore_scenario_placements(
    scenario_id: int,
    snapshot: list[dict[str, object]],
) -> None:
    """Restore a placement snapshot inside one transaction."""
    with transaction.atomic():
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).delete()
        SectionPlacement.objects.bulk_create(
            [SectionPlacement(**row) for row in snapshot],
            batch_size=500,
        )


def _optimiser_safety_metric(summary: dict[str, object], metric: str) -> int:
    same_board = summary.get("same_board_conflicts") or {}
    if metric == "same_board_overlaps" and isinstance(same_board, dict):
        return int(same_board.get("overlaps") or 0)
    if metric == "same_board_instructors" and isinstance(same_board, dict):
        return int(same_board.get("instructors") or 0)
    if metric == "same_board_rooms" and isinstance(same_board, dict):
        return int(same_board.get("rooms") or 0)
    return int(summary.get(metric) or 0)


def _score_metric(score: object, index: int) -> int | None:
    if not isinstance(score, list) or len(score) <= index:
        return None
    try:
        return int(score[index])
    except (TypeError, ValueError):
        return None


def optimiser_student_outcome_regression(result: dict[str, object]) -> dict[str, object]:
    """Block regressions in the actual student solver objective."""
    checks = [
        (0, "tier_a_unresolved", "Tier-A unresolved students"),
        (1, "unresolved_students", "Unresolved students"),
        (2, "unassigned_courses", "Unassigned courses"),
        (3, "time_clashes", "Student time clashes"),
    ]
    regressions = []
    before = result.get("baseline_score")
    after = result.get("final_score")
    for index, metric, label in checks:
        before_value = _score_metric(before, index)
        after_value = _score_metric(after, index)
        if before_value is None or after_value is None:
            continue
        if after_value > before_value:
            regressions.append(
                {
                    "metric": metric,
                    "label": label,
                    "before": before_value,
                    "after": after_value,
                    "delta": after_value - before_value,
                }
            )
    return {"blocked": bool(regressions), "regressions": regressions}


def optimiser_safety_regression(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    """Block hard operational regressions, not board-level tradeoffs."""
    checks = [
        ("same_board_overlaps", "Same-board time overlaps"),
        ("same_board_instructors", "Same-board instructor clashes"),
        ("same_board_rooms", "Same-board room clashes"),
    ]
    regressions = []
    for metric, label in checks:
        before_value = _optimiser_safety_metric(before, metric)
        after_value = _optimiser_safety_metric(after, metric)
        if after_value > before_value:
            regressions.append(
                {
                    "metric": metric,
                    "label": label,
                    "before": before_value,
                    "after": after_value,
                    "delta": after_value - before_value,
                }
            )
    return {"blocked": bool(regressions), "regressions": regressions}


def attach_optimiser_safety_metrics(
    result: dict[str, object],
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    """Attach secondary board metrics consumed by the split-workspace UI."""
    before_pairs = _optimiser_safety_metric(before, "cross_board_conflicts")
    after_pairs = _optimiser_safety_metric(after, "cross_board_conflicts")
    before_affected = _optimiser_safety_metric(before, "cross_board_affected_students")
    after_affected = _optimiser_safety_metric(after, "cross_board_affected_students")
    before_incidences = _optimiser_safety_metric(before, "cross_board_student_conflict_incidences")
    after_incidences = _optimiser_safety_metric(after, "cross_board_student_conflict_incidences")

    result["cross_board_before"] = before_pairs
    result["cross_board_after"] = after_pairs
    result["cross_board_delta"] = before_pairs - after_pairs
    result["cross_board_affected_students_before"] = before_affected
    result["cross_board_affected_students_after"] = after_affected
    result["cross_board_affected_students_delta"] = before_affected - after_affected
    result["cross_board_student_conflict_incidences_before"] = before_incidences
    result["cross_board_student_conflict_incidences_after"] = after_incidences
    result["cross_board_student_conflict_incidences_delta"] = before_incidences - after_incidences


def run_v2_optimisation_guarded(
    scenario_id: int,
    *,
    mode: str = "current",
    max_iterations: int = 50,
    run_chain: bool = True,
    run_cpsat: bool = True,
    cpsat_limit: float = 60.0,
    strategies: list[str] | None = None,
    max_chain_iterations: int = 10,
) -> dict:
    """Run the V2 optimiser behind the snapshot → regression → rollback gate.

    ``mode`` is ``"full"`` (regenerate from scratch) or ``"current"`` (improve
    the existing board). Returns the optimiser result dict augmented with
    ``safety_blocked`` / ``safety_regression`` / cross-board metrics. On an
    internal optimiser error the snapshot is restored and ``{"error": ...}`` is
    returned. The snapshot/rollback runs wherever this is called — request
    thread or async worker — so a worker SIGKILL can no longer leave the DB in a
    half-optimised state without a rollback path.
    """
    placement_snapshot = snapshot_scenario_placements(scenario_id)
    safety_before = compute_scenario_safety_summary(scenario_id)

    try:
        if mode == "full":
            from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

            result = optimise_scenario_timetable_v2(
                scenario_id=scenario_id,
                strategies=strategies,
                run_local_search=True,
                max_search_iterations=max_iterations,
                run_chain_search=run_chain,
                run_cpsat_polish=run_cpsat,
                cpsat_time_limit=cpsat_limit,
            )
        else:
            from core.services.timetable_optimizer_v2 import optimise_current_timetable

            result = optimise_current_timetable(
                scenario_id=scenario_id,
                max_search_iterations=max_iterations,
                run_chain_search=run_chain,
                max_chain_iterations=max_chain_iterations,
                run_cpsat_polish=run_cpsat,
                cpsat_time_limit=cpsat_limit,
            )
    except Exception:
        restore_scenario_placements(scenario_id, placement_snapshot)
        logger.exception("V2 optimiser failed for scenario %d", scenario_id)
        return {"error": f"Optimiser error: {traceback.format_exc(limit=3)}"}

    if "error" in result:
        return result

    safety_after = compute_scenario_safety_summary(scenario_id)
    student_regression = optimiser_student_outcome_regression(result)
    safety_regression = optimiser_safety_regression(safety_before, safety_after)
    blocking_regressions = list(student_regression["regressions"]) + list(
        safety_regression["regressions"]
    )
    # A from-scratch build (the scenario had no placements before this run, e.g.
    # the deferred-generate path) cannot "regress" — there was nothing to
    # protect, and any board is strictly better than an empty one. Only the
    # rollback gate, designed to protect an EXISTING board, applies when a prior
    # board existed; otherwise discarding the build leaves the scenario empty.
    if blocking_regressions and placement_snapshot:
        candidate_final_score = result.get("final_score")
        restore_scenario_placements(scenario_id, placement_snapshot)
        safety_after = compute_scenario_safety_summary(scenario_id)
        result["safety_blocked"] = True
        result["safety_regression"] = {
            "blocked": True,
            "regressions": blocking_regressions,
        }
        result["candidate_final_score"] = candidate_final_score
        result["persist_result"] = {
            "action": "rolled_back_safety_regression",
            "reason": "Optimiser candidate worsened the student outcome or hard operational constraints.",
            "regressions": blocking_regressions,
        }
        baseline_score = result.get("baseline_score")
        if isinstance(baseline_score, list):
            result["final_score"] = baseline_score
            if len(baseline_score) > 1:
                result["unresolved_students"] = baseline_score[1]
        logger.warning(
            "V2 optimiser result rolled back for scenario %d: %s",
            scenario_id,
            blocking_regressions,
        )
    else:
        result["safety_blocked"] = False
        result["safety_regression"] = {"blocked": False, "regressions": []}

    attach_optimiser_safety_metrics(result, safety_before, safety_after)
    return result
