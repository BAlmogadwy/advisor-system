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

from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario
from core.services.timetable_workspace import compute_scenario_safety_summary

logger = logging.getLogger(__name__)


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
    """Block regressions in the actual student solver objective.

    The hard block occupies positions 0-3 in BOTH objective layouts, so the gate
    guards the same feasibility terms either way — but the position *meanings*
    (hence the labels shown on a rollback) differ, so the check table is chosen
    by layout. Under the tiered objective the soft tier (T3 / Tier-2 within
    tolerance, position 5) is intentionally NOT gated: the policy deprioritises
    those enrolments, so trading them for T1/T2/gap gains must not be rolled back.

    LIKE-FOR-LIKE: the gate judges the OPTIMISER's own decision, so it compares
    the pre-instructor-pass score against ``baseline_score`` (also pre-pass).
    ``final_score`` now describes the PERSISTED board — it includes the cost of
    the mandatory cap/clash repairs, which the optimiser did not choose and must
    not be vetoed for. Gating on it would let a required clash repair roll the
    whole run back and REINSTATE the very double-booking it just cleared, which
    inverts the documented "the cap WINS against students" rule. When no
    instructor pass ran the two are identical, so this is a no-op.
    """
    from core.services.timetable_student_assignment import is_tiered_score

    before = result.get("baseline_score")
    after = result.get("score_before_instructor_passes") or result.get("final_score")
    tiered = is_tiered_score(tuple(after)) if isinstance(after, list) else False
    if tiered:
        checks = [
            (0, "highrisk_unresolved", "High-risk unresolved (T1/T2)"),
            (1, "time_clashes", "Student time clashes"),
            (2, "t1_unresolved", "Tier-1 unassigned (core)"),
            (3, "t2_over_tolerance", "Tier-2 over tolerance"),
        ]
    else:
        checks = [
            (0, "tier_a_unresolved", "Tier-A unresolved students"),
            (1, "unresolved_students", "Unresolved students"),
            (2, "unassigned_courses", "Unassigned courses"),
            (3, "time_clashes", "Student time clashes"),
        ]
    regressions = []
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
        # Scenario-wide, so it also catches an instructor double-booked ACROSS
        # boards — which the same_board_* row above cannot see (it is summed per
        # board). Instructor clash is a hard rule, so introducing one vetoes the
        # run even when student outcomes improved.
        ("instructor_clashes_scenario", "Instructor double-bookings (all boards)"),
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
    board_snapshot = snapshot_scenario(scenario_id)
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
        restore_scenario(scenario_id, board_snapshot)
        logger.exception("V2 optimiser failed for scenario %d", scenario_id)
        return {"error": f"Optimiser error: {traceback.format_exc(limit=3)}"}

    if "error" in result:
        return result

    # Surface the soft-gap budget so the frontend can recover the pure student
    # gap from the blended tiered student-cost term (score[4] = real_gap +
    # budget * soft). 0 when the tiered objective is off (score[4] is pure gap).
    from core.services.timetable_flags import (
        get_tiered_soft_gap_budget,
        is_tiered_objective_enabled,
    )

    result["tiered_soft_gap_budget"] = (
        get_tiered_soft_gap_budget() if is_tiered_objective_enabled() else 0
    )

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
    if blocking_regressions and not board_snapshot.is_empty:
        candidate_final_score = result.get("final_score")
        restore_scenario(scenario_id, board_snapshot)
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
            from core.services.timetable_student_assignment import is_tiered_score

            result["final_score"] = baseline_score
            if is_tiered_score(tuple(baseline_score)):
                # The tiered tuple carries no total-unresolved-students headcount
                # (its position 1 is the clash count). Restore the baseline count
                # captured at optimise time instead of misreading the tuple.
                if "baseline_unresolved_students" in result:
                    result["unresolved_students"] = result["baseline_unresolved_students"]
            elif len(baseline_score) > 1:
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
