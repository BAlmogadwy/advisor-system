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
from collections.abc import Callable

from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario
from core.services.timetable_workspace import compute_scenario_safety_summary

logger = logging.getLogger(__name__)


def optimiser_instructor_day_regression(before: dict, after: dict, result: dict) -> dict:
    """Protect each instructor's initial week, including post-search passes.

    A mandatory cap/clash repair may need another day. Its measured intermediate
    board supplies that instructor's explicit exception ceiling; optional later
    compaction cannot increase it further.
    """
    from core.services.timetable_optimisation_policy import instructor_day_regressions

    regressions = instructor_day_regressions(before, after)
    repair_metrics = result.get("instructor_schedule_after_hard_repairs") or {}
    repair_days = repair_metrics.get("working_days_by_instructor") or {}
    pre_pass = result.get("instructor_schedule_before_passes") or before
    pre_days = pre_pass.get("working_days_by_instructor") or {}
    blocked, exceptions = [], []
    for regression in regressions:
        iid = regression["instructor_id"]
        if int(pre_days.get(iid, regression["before"])) <= regression["before"] and regression[
            "after"
        ] <= int(repair_days.get(iid, regression["before"])):
            exceptions.append({**regression, "reason": "mandatory_instructor_constraint_repair"})
        else:
            blocked.append(regression)
    return {"blocked": bool(blocked), "regressions": blocked, "repair_exceptions": exceptions}


def _day_trade_idle_allowance(before: dict, after: dict) -> tuple[dict[str, int], int]:
    """Idle minutes each instructor may gain for the campus days they SAVED.

    Empty unless the bounded instructor-day trade is on, so default behaviour is
    the strict per-instructor idle ceiling. Returns ``({instructor_id: minutes},
    total_minutes)``.
    """
    from core.services.timetable_pr4_instructor import (
        get_instructor_day_trade_config,
        is_instructor_day_trade_active,
    )

    cfg = get_instructor_day_trade_config()
    if not is_instructor_day_trade_active() or cfg["idle_allowance"] <= 0:
        return {}, 0
    before_days = before.get("working_days_by_instructor") or {}
    after_days = after.get("working_days_by_instructor") or {}
    per = {
        str(i): cfg["idle_allowance"] * max(0, int(d) - int(after_days.get(str(i), d)))
        for i, d in before_days.items()
    }
    return per, sum(per.values())


def optimiser_instructor_idle_regression(before: dict, after: dict, result: dict) -> dict:
    """Only a measured mandatory hard repair may increase instructor idle.

    Optional search/compaction regressions and missing evidence never qualify
    as repair exceptions, even when the final aggregate idle improved.
    """
    from core.services.timetable_optimisation_policy import instructor_idle_regressions

    pre_pass = result.get("instructor_schedule_before_passes") or before
    repaired = result.get("instructor_schedule_after_hard_repairs") or {}

    def measured(metrics, instructor_id):
        value = (
            (metrics.get("idle_minutes_by_instructor") or {}).get(instructor_id)
            if instructor_id is not None
            else metrics.get("idle_minutes")
        )
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    trade_allowance, trade_total = _day_trade_idle_allowance(before, after)
    blocked, exceptions, traded = [], [], []
    for regression in instructor_idle_regressions(before, after):
        instructor_id = regression.get("instructor_id")
        before_repair = measured(pre_pass, instructor_id)
        after_repair = measured(repaired, instructor_id)
        delta = regression.get("delta")
        if isinstance(delta, int) and not regression["metric"].endswith("_evidence"):
            allowed = (
                trade_allowance.get(str(instructor_id), 0)
                if instructor_id is not None
                else trade_total
            )
            if 0 < delta <= allowed:
                # A saved campus day was paid for in idle minutes, inside the
                # configured allowance (bounded instructor-day trade).
                traded.append({**regression, "reason": "instructor_day_saved_for_idle"})
                continue
        if (
            not regression["metric"].endswith("_evidence")
            and before_repair is not None
            and after_repair is not None
            and before_repair <= regression["before"]
            and regression["after"] <= after_repair
        ):
            exceptions.append({**regression, "reason": "mandatory_instructor_constraint_repair"})
        else:
            blocked.append(regression)
    return {
        "blocked": bool(blocked),
        "regressions": blocked,
        "repair_exceptions": exceptions,
        "trade_exceptions": traded,
    }


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


def attach_student_gap_fields(result: dict) -> None:
    """Surface the regular/visitor gap split at the top level of an optimise result.

    Present only under ``TIMETABLE_GAP_REGULAR_STUDENTS_ONLY`` (the evaluator then
    puts ``student_gap`` in ``quality_score``); absent otherwise, so every existing
    field and the OFF payload are unchanged.
    """
    student_gap = (result.get("quality_score") or {}).get("student_gap")
    if isinstance(student_gap, dict):
        result["gap_regular_students_only"] = True
        result["gap_minutes_regular"] = student_gap["gap_minutes_regular"]
        result["gap_minutes_visitors"] = student_gap["gap_minutes_visitors"]
        result["gap_minutes_all"] = student_gap["gap_minutes_all"]


def restore_baseline_student_gap(result: dict) -> None:
    """After a whole-board rollback the reported gap split is the baseline board's."""
    if isinstance(result.get("quality_score"), dict) and isinstance(
        result.get("baseline_student_gap"), dict
    ):
        result["quality_score"]["student_gap"] = dict(result["baseline_student_gap"])


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
    checkpoint: Callable[[], None] | None = None,
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
    # Required-section guarantee — pre-step. ``optimise_current_timetable``
    # reads the board "as is" and never calls the placer (by design: it must
    # not regenerate a manually-adjusted board), so a section already missing
    # before this run started would otherwise never be retried. Patch it up
    # BEFORE the snapshot, so the reinstated board becomes the baseline this
    # run's rollback protects and ``optimise_current_timetable`` optimises
    # on top of a complete board. Full mode does its own reinstatement
    # internally, at the very end of its own pipeline (it already overwrites
    # the whole board, so there is nothing useful to pre-repair here).
    from core.services.timetable_flags import is_required_section_guarantee_enabled
    from core.services.timetable_required_sections import (
        find_missing_required_sections,
        reinstate_missing_required_sections,
    )

    guarantee_on = is_required_section_guarantee_enabled()
    pre_repair: dict | None = None
    missing_before: set[str] = set()
    if guarantee_on:
        # A scenario with NO placements at all is "nothing built yet", not
        # "something was dropped" — every budgeted course looks "missing"
        # before the first build, and pre-repairing here would silently turn
        # a current-mode call into a full build, overriding
        # ``optimise_current_timetable``'s own intentional
        # "No placements found — nothing to optimise" response. Mirrors the
        # ``board_snapshot.is_empty`` guard further down for the same reason.
        from core.models import SectionPlacement

        has_existing_board = SectionPlacement.objects.filter(
            board__scenario_id=scenario_id
        ).exists()
        if mode == "current" and has_existing_board:
            pre_repair = reinstate_missing_required_sections(scenario_id)
            missing_before = {row["course_code"] for row in pre_repair["after_missing"]}
        elif mode != "current":
            missing_before = {
                row["course_code"] for row in find_missing_required_sections(scenario_id)
            }

    board_snapshot = snapshot_scenario(scenario_id)
    safety_before = compute_scenario_safety_summary(scenario_id)
    from core.services.timetable_optimisation_policy import (
        instructor_schedule_metrics,
        is_instructor_priority_enabled,
    )
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    instructor_map = (
        build_section_instructor_map_for_scenario(scenario_id)
        if is_instructor_priority_enabled()
        else None
    )
    instructor_before = instructor_schedule_metrics(
        build_section_states_for_scenario(scenario_id) if instructor_map else [], instructor_map
    )

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
                checkpoint=checkpoint,
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
                checkpoint=checkpoint,
            )
    except Exception:
        # An async cooperative signal belongs to the job's complete baseline
        # restore, rather than optimiser failure reporting. Re-checking also
        # gives an accepted cancellation priority over a simultaneous failure.
        if checkpoint:
            checkpoint()
        restore_scenario(scenario_id, board_snapshot)
        logger.exception("V2 optimiser failed for scenario %d", scenario_id)
        return {"error": f"Optimiser error: {traceback.format_exc(limit=3)}"}

    if "error" in result:
        # Full generation uses placement rows as temporary candidate storage.
        # A returned failure is just as destructive as a raised failure unless
        # the original board is restored before leaving this runner.
        restore_scenario(scenario_id, board_snapshot)
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
    if mode == "full" and is_instructor_priority_enabled():
        # A first Generate may create section identities absent before the run.
        instructor_map = build_section_instructor_map_for_scenario(scenario_id)
    instructor_after = instructor_schedule_metrics(
        build_section_states_for_scenario(scenario_id) if instructor_map else [], instructor_map
    )
    instructor_regression = (
        optimiser_instructor_day_regression(instructor_before, instructor_after, result)
        if mode == "current"
        else {"blocked": False, "regressions": [], "repair_exceptions": []}
    )
    if mode == "current":
        idle_regression = optimiser_instructor_idle_regression(
            instructor_before, instructor_after, result
        )
        instructor_regression["regressions"].extend(idle_regression["regressions"])
        instructor_regression["repair_exceptions"].extend(idle_regression["repair_exceptions"])
        instructor_regression["trade_exceptions"] = idle_regression["trade_exceptions"]
        if is_instructor_priority_enabled():
            # Optimise Current promises to preserve the saved instructor week.
            # A necessary repair is still a candidate, not permission to exceed
            # that baseline. Keep the invalid original board visibly blocked if
            # no repair satisfying these ceilings can be found.
            instructor_regression["regressions"].extend(
                {**regression, "reason": "instructor_baseline_must_be_preserved"}
                for regression in instructor_regression["repair_exceptions"]
            )
            instructor_regression["repair_exceptions"] = []
        instructor_regression["blocked"] = bool(instructor_regression["regressions"])
    result["instructor_schedule_metric"] = {
        "before": instructor_before,
        "after": instructor_after,
        "working_days_saved": instructor_before["working_days"] - instructor_after["working_days"],
        "idle_minutes_saved": instructor_before["idle_minutes"] - instructor_after["idle_minutes"],
        "repair_exceptions": instructor_regression["repair_exceptions"],
        "trade_exceptions": instructor_regression.get("trade_exceptions", []),
    }
    student_regression = optimiser_student_outcome_regression(result)
    # LIKE-FOR-LIKE, mirroring the student gate above: judge the OPTIMISER's own
    # board, captured BEFORE the mandatory instructor cap/clash repairs mutated it.
    # Those repairs enforce hard rules and "the cap WINS against students", so they
    # may accept a warning-level cross-course student overlap as the least-harm way
    # to relocate an over-cap session. Gating on the POST-repair safety summary
    # would let that accepted overlap trip a full rollback — which would REINSTATE
    # the cap violation the repair just cleared (and, on the improve branch, also
    # discard the optimiser's real gains). ``safety_before_instructor_passes`` is
    # set by the optimiser only when a pass ran; absent ⇒ no pass ⇒ fall back to
    # safety_after, so flag-off behaviour is unchanged.
    safety_after_for_gate = result.get("safety_before_instructor_passes") or safety_after
    safety_regression = optimiser_safety_regression(safety_before, safety_after_for_gate)
    blocking_regressions = (
        list(student_regression["regressions"])
        + list(safety_regression["regressions"])
        + list(instructor_regression["regressions"])
    )
    # Required-section guarantee — the rollback gate. This run's own work
    # (full mode's re-placement, current mode's cap/clash repair unplacing a
    # meeting, etc.) must never leave a required section with FEWER placed
    # sections than it had right before this run started (after the pre-step
    # above). Compared as a set of course codes, not a count, so dropping one
    # course while placing an unrelated one can't cancel out and hide the
    # loss. A course that was already missing before this run (and could not
    # be repaired) is reported, not blocked — see
    # core.services.timetable_required_sections module docstring.
    if guarantee_on:
        missing_after = {row["course_code"] for row in find_missing_required_sections(scenario_id)}
        newly_missing = sorted(missing_after - missing_before)
        if newly_missing:
            blocking_regressions.append(
                {
                    "metric": "required_sections_missing",
                    "label": "Required sections dropped",
                    "before": sorted(missing_before),
                    "after": sorted(missing_after),
                    "sections": newly_missing,
                }
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
        result["candidate_instructor_schedule"] = instructor_after
        for key, remaining_key in (
            ("instructor_cap_repair", "remaining_violations"),
            ("instructor_clash_repair", "remaining_clashes"),
        ):
            if isinstance(result.get(key), dict):
                repair = result[key]
                result[f"candidate_{key}"] = repair
                if remaining_key == "remaining_clashes":
                    remaining = int(safety_after.get("instructor_clashes_scenario") or 0)
                else:
                    from core.services.timetable_pr4_instructor import (
                        count_instructor_daily_overloads,
                        get_instructor_daily_cap,
                    )

                    # The counter looks sections up by id; a bare list crashed
                    # this rollback path whenever instructors were linked.
                    remaining = count_instructor_daily_overloads(
                        {
                            state.section_id: state
                            for state in build_section_states_for_scenario(scenario_id)
                        },
                        instructor_map or {},
                        get_instructor_daily_cap(),
                    )
                result[key] = {
                    "enabled": repair.get("enabled", False),
                    "rolled_back": True,
                    "rollback_reason": "whole_board_rollback",
                    "detected": repair.get("detected", []),
                    "repaired": [],
                    "unplaced": [],
                    "locked_blocked": [],
                    remaining_key: remaining,
                    "student_score_before": None,
                    "student_score_after": None,
                }
        if isinstance(result.get("instructor_compaction"), dict):
            compaction = result["instructor_compaction"]
            result["candidate_instructor_compaction"] = compaction
            result["instructor_compaction"] = {
                "enabled": compaction.get("enabled", False),
                "rolled_back": True,
                "rollback_reason": "whole_board_rollback",
                "relocations": [],
                "instructor_impact": {},
                "student_impact": {},
                "search": {"moves_accepted": 0},
            }
        if isinstance(result.get("secondary_recovery"), dict):
            recovery = result["secondary_recovery"]
            result["candidate_secondary_recovery"] = recovery
            # The whole-board rollback discards even a locally accepted pass.
            # Its search diagnostics remain under candidate_* for inspection.
            result["secondary_recovery"] = {
                "applied": False,
                "stop_reason": "whole_board_rollback",
                "accepted_moves": [],
            }
        result["instructor_schedule_metric"].update(
            after=instructor_before,
            working_days_saved=0,
            idle_minutes_saved=0,
            repair_exceptions=[],
        )
        if isinstance(result.get("quality_score"), dict):
            result["quality_score"]["instructor_schedule"] = instructor_before
        restore_baseline_student_gap(result)
        result["persist_result"] = {
            "action": "rolled_back_safety_regression",
            "reason": (
                "Optimiser candidate worsened student outcomes, instructor days/gaps "
                "or hard operational constraints."
            ),
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

    # Regular-only gap option (absent when off): surface both numbers at the top
    # level of the result so the owner can see the visitors' gap the objective
    # no longer counts. Existing fields keep their meaning.
    attach_student_gap_fields(result)

    attach_optimiser_safety_metrics(result, safety_before, safety_after)

    # Required-section guarantee — final reporting. Computed on the FINAL
    # persisted state (after a rollback restored the board, if one fired
    # above), and always attached regardless of the flag, so a drop stays
    # visible in the job's own result even with active repair turned off.
    from core.services.timetable_required_sections import summarise_required_sections

    final_report = summarise_required_sections(scenario_id)
    result["required_sections"] = {
        **final_report,
        "guarantee_enabled": guarantee_on,
        "pre_repair": pre_repair,
    }
    if final_report["status"] == "incomplete":
        logger.error(
            "Required sections still missing after scenario %d run: %s",
            scenario_id,
            [row["course_code"] for row in final_report["missing"]],
        )

    return result
