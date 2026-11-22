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


def availability_repair_credit(
    result: dict, before: dict | None = None
) -> dict[str, dict[str, int]]:
    """What the teaching-time repair spent in this run, per teacher (owner,
    2026-11-22): ``{"extra_days": {id: n}, "idle_minutes": {id: m}}``, positive
    whole numbers only, empty when it spent nothing or ran not at all. The final
    gates credit it to the teacher's baseline.

    With ``before`` (the run's saved board) a teacher's credit is cut to what the
    board right after the hard repairs (``instructor_schedule_after_hard_repairs``,
    when the optimiser recorded it) still stands above ``before``: a day an earlier
    stage saved him and the repair then spent back is owed nothing, so it cannot
    cover a later stage's rise. With ``before`` but no such record the credit is
    empty (the optimiser records it whenever the repair spent)."""
    spent = (result.get("instructor_availability_repair") or {}).get("spent") or {}
    credit: dict[str, dict[str, int]] = {"extra_days": {}, "idle_minutes": {}}
    for name in credit:
        for teacher, value in (spent.get(name) or {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                credit[name][str(teacher)] = value
    repaired = result.get("instructor_schedule_after_hard_repairs")
    if before is not None and not isinstance(repaired, dict):
        # A spend without the record of the board it left is never credited
        # unclamped: nothing shows how far above the saved board it stands.
        return {"extra_days": {}, "idle_minutes": {}}
    if before is not None and isinstance(repaired, dict):
        for name, key in (
            ("extra_days", "working_days_by_instructor"),
            ("idle_minutes", "idle_minutes_by_instructor"),
        ):
            then, saved = repaired.get(key) or {}, before.get(key) or {}
            for teacher in list(credit[name]):
                owed = int(then.get(teacher, 0)) - int(saved.get(teacher, 0))
                if owed <= 0:
                    del credit[name][teacher]
                else:
                    credit[name][teacher] = min(credit[name][teacher], owed)
    return credit


def _availability_credit_used(before: dict, after: dict, credit: dict) -> list[dict]:
    """The part of each teacher's rise from ``before`` to ``after`` that the
    repair's credit covers, one row per teacher and metric. A fifth working day
    is never covered."""
    from core.services.timetable_optimisation_policy import MAX_WORKING_DAYS

    rows: list[dict] = []
    for name, key, metric in (
        ("extra_days", "working_days_by_instructor", "instructor_working_days"),
        ("idle_minutes", "idle_minutes_by_instructor", "instructor_idle_minutes"),
    ):
        old, new = before.get(key) or {}, after.get(key) or {}
        for teacher, granted in sorted(credit[name].items()):
            was, now = int(old.get(teacher, 0)), int(new.get(teacher, 0))
            used = min(granted, now - was)
            if name == "extra_days":
                used = min(used, max(MAX_WORKING_DAYS, was) - was)
            if used > 0:
                rows.append(
                    {
                        "metric": metric,
                        "instructor_id": teacher,
                        "before": was,
                        "after": now,
                        "credit": granted,
                        "used": used,
                        "reason": "availability_repair_spend",
                    }
                )
    return rows


def optimiser_instructor_day_regression(before: dict, after: dict, result: dict) -> dict:
    """Protect each instructor's initial week, including post-search passes.

    A mandatory cap/clash repair may need another day. Its measured intermediate
    board supplies that instructor's explicit exception ceiling; optional later
    compaction cannot increase it further. What the teaching-time repair spent
    (``availability_repair_credit``) is credited to that teacher's baseline: a rise
    no larger than the credit, and never a fifth day, is not a regression.
    """
    from core.services.timetable_optimisation_policy import (
        MAX_WORKING_DAYS,
        instructor_day_regressions,
    )

    credit_days = availability_repair_credit(result, before)["extra_days"]
    regressions = instructor_day_regressions(before, after)
    repair_metrics = result.get("instructor_schedule_after_hard_repairs") or {}
    repair_days = repair_metrics.get("working_days_by_instructor") or {}
    pre_pass = result.get("instructor_schedule_before_passes") or before
    pre_days = pre_pass.get("working_days_by_instructor") or {}
    blocked, exceptions = [], []
    for regression in regressions:
        iid = regression["instructor_id"]
        if regression["delta"] <= credit_days.get(iid, 0) and (
            regression["after"] <= MAX_WORKING_DAYS or regression["before"] > MAX_WORKING_DAYS
        ):
            continue  # the teaching-time repair's own spend (credited)
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
    as repair exceptions, even when the final aggregate idle improved. The one
    credit: idle minutes the teaching-time repair spent
    (``availability_repair_credit``) are added to that teacher's baseline (and,
    for the total, to the sum of the credits); a larger rise is judged as before.
    """
    from core.services.timetable_optimisation_policy import instructor_idle_regressions

    credit_idle = availability_repair_credit(result, before)["idle_minutes"]
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
            covered = (
                credit_idle.get(str(instructor_id), 0)
                if instructor_id is not None
                else sum(credit_idle.values())
            )
            if 0 < delta <= covered:
                continue  # the teaching-time repair's own spend (credited)
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


def day_off_audit(before: dict, after: dict) -> dict:
    """Generate's R1 report (owner default: reported, never blocking): who has
    no day off on the final board but had one on the saved board."""
    from core.services.timetable_optimisation_policy import MAX_WORKING_DAYS

    before_days = {str(i): d for i, d in (before.get("working_days_by_instructor") or {}).items()}
    after_days = {str(i): d for i, d in (after.get("working_days_by_instructor") or {}).items()}
    return {
        "blocking": False,
        "fifth_day_added": [
            {"instructor_id": i, "days_before": before_days.get(i, 0), "days_after": days}
            for i, days in sorted(after_days.items())
            if days > MAX_WORKING_DAYS >= before_days.get(i, 0)
        ],
        "five_day_before": sum(1 for d in before_days.values() if d > MAX_WORKING_DAYS),
        "five_day_after": sum(1 for d in after_days.values() if d > MAX_WORKING_DAYS),
    }


def _optimiser_safety_metric(summary: dict[str, object], metric: str) -> int:
    same_board = summary.get("same_board_conflicts") or {}
    if metric == "same_board_overlaps" and isinstance(same_board, dict):
        # The hard rule only: two sections of one course at once. Cross-course
        # overlaps are counted from course DEMAND, so reshuffling sections trades
        # them freely even when every student keeps a clash-free choice; their
        # real cost is what the student-outcome gate measures. (Before the
        # overlap matrix was keyed by planner identity it was empty, so this gate
        # has only ever seen same-course overlaps.)
        return int(same_board.get("same_course_overlaps") or 0)
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
        ("same_board_overlaps", "Same-board time overlaps (same course)"),
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


def restore_baseline_instructor_availability(result: dict) -> None:
    """After a whole-board rollback the teaching-time side band is the baseline
    board's count, or absent when no baseline count exists (fix plan D-J): the
    post-run count described the board the rollback discarded."""
    quality = result.get("quality_score")
    if not isinstance(quality, dict):
        return
    baseline = result.get("baseline_instructor_availability")
    if isinstance(baseline, dict):
        quality["instructor_availability"] = dict(baseline)
    else:
        quality.pop("instructor_availability", None)


#: A read of the persisted board's teaching-time violations that failed: neither
#: a clean board nor a broken one, so no gate may pass or block on it.
_UNREAD = object()


def _availability_pairs(scenario_id: int):
    """Blocked meetings per ``(section id, teacher id)`` on the persisted board
    under the map in force; None while no rule binds a teacher of the scenario
    (the flag off, or no rule: nothing is read and no gate row or key exists);
    ``_UNREAD`` when the report could not be read (``checked`` False: an empty
    count would look like a clean board)."""
    from core.services.timetable_instructor_availability import availability_for

    if not availability_for(scenario_id):
        return None
    from core.services.timetable_instructor_availability_report import (
        pair_counts,
        scenario_instructor_availability_report,
    )

    report = scenario_instructor_availability_report(scenario_id)
    if not report.get("checked"):
        return _UNREAD
    return pair_counts(report)


#: Why a teacher cannot take a section on this layout: he is busy at its time.
_BUSY_REASONS = ("clash", "daily_cap")


def _rule_attributed_tbd(saved, final, repair: dict | None) -> list[str]:
    """Sections left "to be decided" by this run because of a teaching-time rule
    (B2-5, H12): newly TBD after the run, and either released by the rule and not
    re-picked (reason ``availability``) or charged before the run to a teacher the
    availability repair moved and who is now busy at its time (clash, daily cap).
    Attributed per section, never by comparing two estimators; any other TBD
    rise still blocks."""
    from core.services.section_teacher_assignment import AVAILABILITY_REASON, STATUS_TBD

    moved = set((repair or {}).get("moved_instructors") or ())
    out = []
    for slot in final.slots:
        key = slot.key
        if final.status.get(key) != STATUS_TBD or saved.status.get(key) == STATUS_TBD:
            continue
        reason = final.tbd_reason.get(key)
        if reason == AVAILABILITY_REASON or (
            reason in _BUSY_REASONS and saved.teacher.get(key) in moved
        ):
            out.append(key)
    return sorted(out)


class _RunTeachers:
    """One teacher per section through the guarded runner (Phase 1 spec 3.3).

    Optimise Current: the map in force is read before anything is written
    (C0), the names are brought in line with it before the snapshot (C2), the
    run starts from a bounded re-pick that can never cost a teacher a day or
    idle the rules forbid (C5), and ends with a re-pick that is adopted only if
    it is no worse than the map it replaces (C7). Every phase runs pinned.
    With the day-off policy a re-pick between the search and the post passes
    (S1) may change the map in force (``in_force``); C7 starts from it.
    """

    def __init__(self, scenario_id: int) -> None:
        self.scenario_id = scenario_id
        self.saved = self.start = self.final = self.in_force = None
        self.report: dict = {"enabled": True}
        # The run-level stack S1 pins its map on, and the strict bounds and gate
        # S1 falls back to when the policy lacks evidence.
        self.stack = None
        self.fallback: tuple = (None, None)

    def pin(self, assignment):
        from core.services.section_teacher_assignment import pinned_assignment

        return pinned_assignment(self.scenario_id, assignment)

    def pre_step(self):
        """C0: the map the required-section retry charges. Pure."""
        from core.services.section_teacher_assignment import load_assignment

        return self.pin(load_assignment(self.scenario_id, release_unteachable=True))

    def save(self, *, rename: bool) -> None:
        """C2: the saved map; its names are written before the snapshot takes them."""
        from django.db import transaction

        from core.services.section_teacher_assignment import (
            load_assignment,
            stage_report,
            write_names,
        )

        self.saved = load_assignment(self.scenario_id, release_unteachable=True)
        if rename:
            with transaction.atomic():
                write_names(self.scenario_id, self.saved)
        self.report["saved"] = stage_report(self.saved)

    def start_run(self, bounds):
        """C5: the bounded run-start re-pick, persisted and pinned for the run."""
        from core.services.section_teacher_assignment import (
            assignment_scope,
            closure_handover,
            persist_and_read,
            propose_layout_assignment,
            stage_report,
            write_names,
        )

        with assignment_scope(self.scenario_id):  # one read of the stored rows
            proposed = propose_layout_assignment(
                self.scenario_id,
                current=self.saved,
                keep_violations_of=self.saved,
                bounds=bounds,
            )
            self.start, persisted = persist_and_read(self.scenario_id, proposed)
        write_names(self.scenario_id, self.start)
        self.report["run_start"] = stage_report(
            self.start,
            solver=proposed,
            persist=persisted,
            changed_sections=sorted(
                key
                for key, teacher in self.start.teacher.items()
                if teacher != self.saved.teacher.get(key)
            ),
            # Only when the persisted closure gave a section a teacher the re-pick had
            # not: the week the bounds judged is then not the one that was stored.
            **(
                {"closure_handover": handed}
                if (handed := closure_handover(proposed, self.start))
                else {}
            ),
        )
        return self.pin(self.start)

    def swap(self, stage: str, *, policy, score) -> dict | None:
        """S1: a re-pick on the board the search left, judged by the day-off
        ``policy`` (strict ``fallback`` without its evidence). The map it stores
        is pinned on the run's stack and stays in force; returns its V2 map
        when it changed who teaches what, else None."""
        from core.services.timetable_optimizer_v2 import _final_teacher_recompute

        bounds, gate = self.fallback
        stored, changed, report = _final_teacher_recompute(
            self.scenario_id, self.current(), bounds=bounds, gate=gate, policy=policy, score=score
        )
        self.report[stage] = report
        self.in_force = stored
        self.stack.enter_context(self.pin(stored))
        return stored.v2_map() if changed else None

    def current(self):
        """The map in force: S1's when it ran, else the run start's."""
        return self.in_force if self.in_force is not None else self.start

    def finish(self, *, bounds, gate, policy=None, score=None) -> bool:
        """C7: the final re-pick on the optimised board, from the map in force."""
        from core.services.timetable_optimizer_v2 import _final_teacher_recompute

        self.final, changed, final = _final_teacher_recompute(
            self.scenario_id,
            self.current(),
            bounds=bounds,
            gate=gate,
            **({"policy": policy, "score": score} if policy is not None else {}),
        )
        self.report["final"] = final
        return changed

    def read_final(self) -> None:
        """G6: Generate chose (and reported) its own maps; read the one it
        persisted, once."""
        from core.services.section_teacher_assignment import load_assignment

        self.final = load_assignment(self.scenario_id, release_unteachable=True)

    def universe(self) -> frozenset:
        return frozenset(self.saved.universe) | frozenset(self.final.universe)


def _name_taken_over_sections(row: dict, saved, final) -> dict:
    """A blocking row about a teacher's week names the sections the final map gave
    that teacher and the saved map did not (a re-pick's, or the persisted closure's
    hand-over), so the registrar sees which section cost the week. Rows about no
    teacher, and teachers who took over nothing, are left as they are."""
    ids = {str(i) for i in (row.get("instructor_ids") or [row.get("instructor_id")]) if i}
    taken = sorted(
        key
        for key, teacher in final.teacher.items()
        if teacher is not None and str(teacher) in ids and saved.teacher.get(key) != teacher
    )
    return {**row, "sections_taken_over": taken} if taken else row


def _week_gate(scenario_id: int, instructor_before: dict, result: dict, teachers: _RunTeachers):
    """C7 with priority on: a final re-pick is adopted only if, measured on the
    final board with padded metrics, no teacher's days or idle regress against
    the run start — not even as a mandatory-repair exception."""
    from core.services.section_teacher_assignment import pad_schedule_metrics
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    states = build_section_states_for_scenario(scenario_id)

    def passes(candidate) -> bool:
        universe = frozenset(teachers.saved.universe) | frozenset(candidate.universe)
        before = pad_schedule_metrics(instructor_before, universe)
        after = pad_schedule_metrics(
            instructor_schedule_metrics(states, candidate.v2_map()), universe
        )
        days = optimiser_instructor_day_regression(before, after, result)
        idle = optimiser_instructor_idle_regression(before, after, result)
        return not (
            days["regressions"]
            or days["repair_exceptions"]
            or idle["regressions"]
            or idle["repair_exceptions"]
        )

    return passes


def _refresh_after_teacher_recompute(result: dict, scenario_id: int, final) -> None:
    """Re-score the persisted board under the map C7 kept (it changed)."""
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_student_profiles_for_scenario,
        refresh_final_score_after_teacher_recompute,
    )

    refresh_final_score_after_teacher_recompute(
        result,
        scenario_id,
        build_student_profiles_for_scenario(scenario_id),
        build_course_rigidity_for_scenario(scenario_id),
        final.v2_map(),
        build_course_tier_map_for_scenario(scenario_id) if is_tiered_objective_enabled() else None,
        changed=True,
    )


def _score_restored_board(result: dict, scenario_id: int, teachers: _RunTeachers) -> None:
    """C9: a rolled-back run reports its baseline, which was judged under the
    run-start map; the restore brought the saved map back, so when C5 had
    changed who teaches what the restored board is re-judged under it."""
    if teachers.start is None or teachers.start.v2_map() == teachers.saved.v2_map():
        return
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    if not profiles or not sections:
        return
    restored = evaluate_generated_timetable_candidate(
        candidate_id="restored",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=build_course_rigidity_for_scenario(scenario_id),
        section_instructor_ids=teachers.saved.v2_map(),
        course_tiers=build_course_tier_map_for_scenario(scenario_id)
        if is_tiered_objective_enabled()
        else None,
    )
    result["final_score"] = list(restored.lexicographic_score)


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
    """Run the V2 optimiser behind the snapshot → regression → rollback gate
    (:func:`_run_v2_optimisation_guarded`), inside one teaching-time rule scope:
    the pre-step reinstatement, every phase and both pipelines read the rules
    once (``TIMETABLE_INSTRUCTOR_AVAILABILITY``; nothing loads while it is off).
    """
    from core.services.timetable_instructor_availability import availability_scope
    from core.services.timetable_optimisation_policy import (
        is_instructor_priority_enabled,
        saved_week_scope,
        week_closure_scope,
    )

    # Optimise Current with the priority on promises every teacher's saved week
    # whenever the day-off policy is not the judge: the final gate below then
    # blocks ANY rise in a teacher's days or idle, so the week gate refuses such
    # a rise to every optional move. The teaching-time repair is the one stage that
    # may still spend a day or idle there (owner, 2026-11-22), only when no
    # allowed cell exists; the final gates then credit what it spent
    # (``availability_repair_credit``). The scope covers the policy off AND the
    # policy on without a usable anchor (the strict gates stay in charge then). A
    # day-off scope with complete evidence answers first inside
    # ``week_move_breaks_gate``, so the scope changes nothing there. Generate and
    # the priority off never open it.
    preserve_saved_week = mode == "current" and is_instructor_priority_enabled()
    # Optimise Current also answers for the teacher the closure will give a section
    # that has none, once its stages have moved the board (``WeekClosureGuard``).
    options: dict = {
        "mode": mode,
        "max_iterations": max_iterations,
        "run_chain": run_chain,
        "run_cpsat": run_cpsat,
        "cpsat_limit": cpsat_limit,
        "strategies": strategies,
        "max_chain_iterations": max_chain_iterations,
        "checkpoint": checkpoint,
    }
    with (
        availability_scope(scenario_id),
        saved_week_scope(preserve_saved_week),
        week_closure_scope(mode == "current"),
    ):
        capture: dict = {}
        first = _run_v2_optimisation_guarded(scenario_id, **options, _capture=capture)
        if mode == "current" and _search_dropped_after_run_start(first, capture):
            return _second_try(scenario_id, first, capture, options)
        return first


def _search_dropped_after_run_start(result: dict, capture: dict) -> bool:
    """The week-closure guard dropped the search AND the run-start re-pick (C5) had
    changed who teaches what: the one case a second try can differ from the first run."""
    teachers = capture.get("teachers")
    return bool(
        teachers is not None
        and teachers.start is not None
        and teachers.start.v2_map() != teachers.saved.v2_map()
        and any(d.get("stage") == "search" for d in result.get("week_closure_dropped") or [])
    )


def _attempt_kept(result: dict) -> bool:
    return (
        "error" not in result
        and (result.get("persist_result") or {}).get("action") != "rolled_back_safety_regression"
    )


def _attempt_score(result: dict) -> list | None:
    """The score the attempt reached (a rolled-back attempt's is its candidate's)."""
    blocked = (result.get("persist_result") or {}).get("action") == "rolled_back_safety_regression"
    score = result.get("candidate_final_score") if blocked else result.get("final_score")
    return list(score) if score else None


def _attempt_evaluation(scenario_id: int, teachers: _RunTeachers):
    """The persisted board judged under the map the attempt ended with (None: no evidence)."""
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    if not profiles or not sections or teachers.final is None:
        return None
    return evaluate_generated_timetable_candidate(
        candidate_id="second_try",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=build_course_rigidity_for_scenario(scenario_id),
        section_instructor_ids=teachers.final.v2_map(),
        course_tiers=build_course_tier_map_for_scenario(scenario_id)
        if is_tiered_objective_enabled()
        else None,
    )


def _second_is_better(second_eval, first_eval, second: dict) -> bool:
    """The project's own stage relation (``stage_is_better``: the day-off policy's
    ``core`` when the run had one, else ``evaluation_is_better``), second against first."""
    from contextlib import nullcontext

    from core.services.timetable_optimisation_policy import (
        DayOffPolicy,
        day_off_scope,
        stage_is_better,
    )

    if second_eval is None or first_eval is None:
        return False
    policy = DayOffPolicy.from_dict(second.get("day_off_policy"))
    with day_off_scope(policy) if policy is not None else nullcontext():
        return stage_is_better(second_eval, first_eval)


def _second_try(scenario_id: int, first: dict, capture: dict, options: dict) -> dict:
    """The week-closure guard dropped the search because the run-start re-pick left a
    section without a teacher: run the whole pipeline once more under the SAVED map (C5
    not applied) and keep the better outcome. The second run is a complete run of its own
    (its own snapshot, C7 and final gates; the same budgets), so "blocked" and "error" are
    its gates' words. It is kept when the first was blocked, or when ``stage_is_better``
    prefers it to the first's persisted board; otherwise the first attempt's board and
    result stand, restored from a snapshot taken before the second began."""
    checkpoint = options["checkpoint"]
    if checkpoint:
        checkpoint()
    first_kept = _attempt_kept(first)
    first_eval = _attempt_evaluation(scenario_id, capture["teachers"]) if first_kept else None
    first_snapshot = snapshot_scenario(scenario_id) if first_kept else None
    if first_kept:
        restore_scenario(scenario_id, capture["snapshot"])  # the board the run began with
    second_capture: dict = {}
    restored = False
    try:
        second = _run_v2_optimisation_guarded(
            scenario_id, **options, _skip_run_start=True, _capture=second_capture
        )
        if "error" in second:
            outcome = "error"
        elif not _attempt_kept(second):
            outcome = "blocked"
        elif not first_kept:
            outcome = "kept"
        elif _second_is_better(
            _attempt_evaluation(scenario_id, second_capture["teachers"]), first_eval, second
        ):
            outcome = "kept"
        else:
            outcome = "not_better"
    except Exception:
        # Anything the second run lets escape must not cost the first attempt's board; a
        # cancellation is the hook's to raise (as in the run itself), and propagates.
        if checkpoint:
            checkpoint()
        logger.exception("Second try failed for scenario %d", scenario_id)
        restore_scenario(scenario_id, first_snapshot if first_kept else capture["snapshot"])
        restored = True
        second, outcome = {"error": "second try raised"}, "error"
    keep_second = outcome == "kept"
    if not keep_second and first_kept and not restored:
        restore_scenario(scenario_id, first_snapshot)
    kept = second if keep_second else first
    kept["second_try"] = {
        "reason": "search_dropped_by_week_closure_after_run_start_repick",
        "kept": "second" if keep_second else "first",
        "second_outcome": outcome,
        "first_score": _attempt_score(first),
        "second_score": _attempt_score(second),
    }
    return kept


def _run_v2_optimisation_guarded(
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
    _skip_run_start: bool = False,
    _capture: dict | None = None,
) -> dict:
    """Run the V2 optimiser behind the snapshot → regression → rollback gate.

    ``_skip_run_start`` and ``_capture`` belong to the second try
    (:func:`_second_try`): the run keeps the saved teacher map instead of the run-start
    re-pick's, and hands back the snapshot it began from and its teacher state.

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
    from contextlib import ExitStack, nullcontext

    from core.services.timetable_flags import (
        is_one_teacher_active,
        is_required_section_guarantee_enabled,
    )
    from core.services.timetable_required_sections import (
        course_labels_by_key,
        describe_short_courses,
        find_missing_required_sections,
        reinstate_missing_required_sections,
        sections_short_by_course,
    )

    # One teacher per section: every phase below charges one pinned map.
    teachers = _RunTeachers(scenario_id) if is_one_teacher_active() else None
    guarantee_on = is_required_section_guarantee_enabled()
    pre_repair: dict | None = None
    missing_before: dict[str, int] = {}
    has_existing_board = None
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
            pre_context = nullcontext()
            if teachers is not None:
                try:  # C0: read before anything is written
                    pre_context = teachers.pre_step()
                except Exception:
                    logger.exception("Teacher map unreadable for scenario %d", scenario_id)
                    return {"error": f"Teacher map error: {traceback.format_exc(limit=3)}"}
            with pre_context:
                pre_repair = reinstate_missing_required_sections(scenario_id)
            if teachers is not None:
                # The retry placed under the pinned map, which no re-pick followed:
                # the stored rows of the sections it released a teacher from are
                # replaced now, so the snapshot (and a rollback to it) holds a map
                # that matches the board (RR-1). No rows, no work.
                from core.services.section_teacher_assignment import resolve_released_sections

                resolve_released_sections(
                    scenario_id,
                    (pre_repair.get("instructor_availability") or {}).get("released", []),
                )
            missing_before = sections_short_by_course(pre_repair["after_missing"])
        elif mode != "current":
            missing_before = sections_short_by_course(find_missing_required_sections(scenario_id))
    if teachers is not None:
        if has_existing_board is None:
            from core.models import SectionPlacement

            has_existing_board = SectionPlacement.objects.filter(
                board__scenario_id=scenario_id
            ).exists()
        teachers.save(rename=mode == "current" and has_existing_board)  # C2

    board_snapshot = snapshot_scenario(scenario_id)
    if _capture is not None:
        _capture.update(snapshot=board_snapshot, teachers=teachers)
    from core.services.timetable_optimisation_policy import (
        instructor_schedule_metrics,
        is_day_off_policy_active,
        is_instructor_priority_enabled,
    )
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )

    # C4: the baseline is measured under the saved map.
    with teachers.pin(teachers.saved) if teachers is not None else nullcontext():
        safety_before = compute_scenario_safety_summary(scenario_id)
        instructor_map = (
            build_section_instructor_map_for_scenario(scenario_id)
            if is_instructor_priority_enabled()
            else None
        )
        instructor_before = instructor_schedule_metrics(
            build_section_states_for_scenario(scenario_id) if instructor_map else [], instructor_map
        )
        # Teaching-time rules (B2-6): the violations each (section, teacher) pair
        # holds at the snapshot, under the saved map. None unless a rule binds a
        # teacher of the scenario: no read, no key, no gate row.
        availability_before = _availability_pairs(scenario_id)
    teacher_bounds = None
    if teachers is not None and is_instructor_priority_enabled():
        from core.services.section_teacher_assignment import teacher_bounds as _bounds

        teacher_bounds = _bounds(instructor_before)
    # Day-off policy (R1-R5): Optimise Current judges every stage, the teacher
    # re-picks and the final board against the saved board under the saved
    # map — the run's start A. None (strict gates) when the flag is off or the
    # measurement is incomplete.
    day_off_anchor = None
    if mode == "current" and is_day_off_policy_active():
        from core.services.timetable_optimisation_policy import DayOffAnchor

        day_off_anchor = DayOffAnchor.from_metrics(
            instructor_before,
            padded=teachers is not None,
            universe=teachers.saved.universe if teachers is not None else (),
        )
        if day_off_anchor is not None and teacher_bounds is not None:
            # C5 under the policy: the day trade's idle allowance holds for a
            # first day off only, so a spare day saved at C5 costs no idle.
            teacher_bounds = day_off_anchor.start_bounds()

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

            with ExitStack() as run_stack:
                policy_kwargs: dict = {}
                if teachers is not None:
                    # C5, after the snapshot so rollback and cancel return the saved
                    # map: a re-pick no teacher's week can lose by, pinned for the run.
                    if _skip_run_start and has_existing_board:
                        # The second try: the saved map stays in force, nothing is re-picked.
                        from core.services.section_teacher_assignment import stage_report

                        teachers.start = teachers.saved
                        teachers.report["run_start"] = stage_report(
                            teachers.saved, changed_sections=[], skipped="second_try"
                        )
                    run_stack.enter_context(
                        teachers.start_run(teacher_bounds)
                        if has_existing_board and not _skip_run_start
                        else teachers.pin(teachers.saved)
                    )
                    if day_off_anchor is not None and teachers.start is not None:
                        # S1 pins the map it stores on this run's stack.
                        teachers.stack = run_stack
                        teachers.fallback = (
                            teacher_bounds,
                            _week_gate(scenario_id, instructor_before, {}, teachers),
                        )
                        policy_kwargs["teacher_swap"] = teachers.swap
                if day_off_anchor is not None:
                    policy_kwargs["day_off_anchor"] = day_off_anchor
                result = optimise_current_timetable(
                    scenario_id=scenario_id,
                    max_search_iterations=max_iterations,
                    run_chain_search=run_chain,
                    max_chain_iterations=max_chain_iterations,
                    run_cpsat_polish=run_cpsat,
                    cpsat_time_limit=cpsat_limit,
                    checkpoint=checkpoint,
                    **policy_kwargs,
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

    # The policy the run was judged by, re-read from the result: only one that
    # starts from this runner's own anchor governs C7 and the final gate; a
    # missing or different one leaves the strict gates (a superset) in charge.
    run_policy = None
    if day_off_anchor is not None:
        from core.services.timetable_optimisation_policy import DayOffPolicy

        carried = DayOffPolicy.from_dict(result.get("day_off_policy"))
        if carried is not None and carried.anchor == day_off_anchor:
            run_policy = carried
        else:
            result["day_off_policy_fallback"] = "missing" if carried is None else "anchor_mismatch"

    if teachers is not None and mode == "current" and teachers.start is not None:
        # C7: the final re-pick on the optimised board, in its own guard — a
        # failure here restores the snapshot like any optimiser failure.
        try:
            changed = teachers.finish(
                bounds=teacher_bounds,
                gate=_week_gate(scenario_id, instructor_before, result, teachers)
                if is_instructor_priority_enabled()
                else None,
                policy=run_policy,
                score=result.get("final_score"),
            )
            if changed:
                _refresh_after_teacher_recompute(result, scenario_id, teachers.final)
        except Exception:
            if checkpoint:
                checkpoint()
            restore_scenario(scenario_id, board_snapshot)
            logger.exception("Final teacher recompute failed for scenario %d", scenario_id)
            return {"error": f"Teacher recompute error: {traceback.format_exc(limit=3)}"}
    elif teachers is not None:
        teachers.read_final()

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

    # C8: the after side is measured under the final map; both sides cover the
    # union of the teachers either map knows, so a teacher who lost every
    # section reads 0 rather than as missing evidence.
    with teachers.pin(teachers.final) if teachers is not None else nullcontext():
        safety_after = compute_scenario_safety_summary(scenario_id)
        if mode == "full" and is_instructor_priority_enabled():
            # A first Generate may create section identities absent before the run.
            instructor_map = build_section_instructor_map_for_scenario(scenario_id)
        if teachers is not None and instructor_map is not None:
            instructor_map = teachers.final.v2_map()
        instructor_after = instructor_schedule_metrics(
            build_section_states_for_scenario(scenario_id) if instructor_map else [], instructor_map
        )
        availability_after = (
            _availability_pairs(scenario_id) if availability_before is not None else None
        )
    if teachers is not None and is_instructor_priority_enabled():
        from core.services.section_teacher_assignment import pad_schedule_metrics

        instructor_before = pad_schedule_metrics(instructor_before, teachers.universe())
        instructor_after = pad_schedule_metrics(instructor_after, teachers.universe())
    day_off_report = None
    # What the teaching-time repair spent (owner, 2026-11-22), credited to the
    # teachers' baselines in whichever final gate judges the run; empty (and every
    # result unchanged) when it spent nothing.
    repair_credit = (
        availability_repair_credit(result, instructor_before) if mode == "current" else None
    )
    # The part of that credit still owed: a teacher's credit never exceeds how far
    # the final board is above the saved one (an earlier stage may have saved him a
    # day the repair then spent back), and a fifth day is never covered.
    credit_used = (
        _availability_credit_used(instructor_before, instructor_after, repair_credit)
        if repair_credit and (repair_credit["extra_days"] or repair_credit["idle_minutes"])
        else []
    )
    if mode == "current" and run_policy is not None:
        # R1-R5 against the saved board: every breach blocks, a repair's
        # included (the owner's default) except what the teaching-time repair
        # spent and the final board still owes, which is added to that teacher's
        # baseline (a fifth day never).
        # R2 reads the optimiser's own board
        # (like-for-like, as the student gate does): a mandatory repair's
        # student cost does not void a spare day the search paid for.
        owed = {"extra_days": {}, "idle_minutes": {}}
        for row in credit_used:
            name = "extra_days" if row["metric"] == "instructor_working_days" else "idle_minutes"
            owed[name][row["instructor_id"]] = row["used"]
        judge = (
            run_policy.with_credit(owed["extra_days"], owed["idle_minutes"])
            if credit_used
            else run_policy
        )
        rows, day_off_report = judge.final_check(
            instructor_after,
            result.get("score_before_instructor_passes") or result.get("final_score") or (),
        )
        if judge is not run_policy:
            # The teachers' days_before / idle_before below include this credit.
            day_off_report["availability_repair_credit"] = owed
        instructor_regression = {
            "blocked": bool(rows),
            "regressions": rows,
            "repair_exceptions": [],
            "trade_exceptions": [],
        }
    else:
        instructor_regression = (
            optimiser_instructor_day_regression(instructor_before, instructor_after, result)
            if mode == "current"
            else {"blocked": False, "regressions": [], "repair_exceptions": []}
        )
    if mode == "current" and run_policy is None:
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
    if credit_used:
        # The part of the credit the teachers' rises used (computed on the saved
        # board, so it reads the same whichever gate judged the run).
        result["instructor_schedule_metric"]["availability_repair_exceptions"] = credit_used
    if day_off_report is not None:
        result["instructor_schedule_metric"]["day_off_policy"] = day_off_report
    if mode == "full" and isinstance(result.get("day_off_policy"), dict):
        # Generate is judged against its own winning candidate; R1 against the
        # saved board is reported here and does not block (owner default).
        result["day_off_policy"]["audit"] = day_off_audit(instructor_before, instructor_after)
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
    if teachers is not None:
        # Days and idle count only the sections that have a teacher, so the
        # sections left without one sit next to them — and in Optimise Current a
        # rise beyond what D10 forces is blocking (the re-picks cannot raise it;
        # this is the net that proves it).
        from core.services.section_teacher_assignment import coverage_of

        coverage_before = coverage_of(teachers.saved)
        coverage_after = coverage_of(teachers.final)
        result["instructor_schedule_metric"]["coverage"] = {
            "before": coverage_before,
            "after": coverage_after,
        }
        # Teaching-time rules (B2-5): a section TBD after the run BECAUSE of a rule
        # is listed and does not count against the floor; only while a rule binds.
        rule_tbd: list[str] = []
        if availability_before is not None and mode == "current":
            rule_tbd = _rule_attributed_tbd(
                teachers.saved, teachers.final, result.get("instructor_availability_repair")
            )
            result["instructor_schedule_metric"]["coverage"]["availability_attributed_tbd"] = (
                rule_tbd
            )
        if (
            mode == "current"
            and coverage_after["tbd_beyond_d10"] - len(rule_tbd) > coverage_before["tbd_beyond_d10"]
        ):
            blocking_regressions.append(
                {
                    "metric": "teacher_coverage",
                    "label": "Sections left without a teacher",
                    "before": coverage_before["tbd_beyond_d10"],
                    "after": coverage_after["tbd_beyond_d10"],
                    "delta": coverage_after["tbd_beyond_d10"] - coverage_before["tbd_beyond_d10"],
                }
            )
    if availability_before is _UNREAD or availability_after is _UNREAD:
        # A failed read (before or after) is not a clean board: the comparison is
        # skipped, nothing is blocked or passed on it, and the result says so.
        result["instructor_availability_gate"] = {"checked": False}
    elif availability_before is not None and availability_after is not None:
        # B2-6: the backstop for "an automatic stage created a violation". A pair
        # holding MORE blocked meetings on the final board (final map) than at the
        # snapshot (saved map) blocks, unless a stage already reported it as one
        # it could not avoid (the repair's locked_blocked / unavoidable, a placer's
        # ranked_last).
        from core.services.timetable_instructor_availability_report import excused_pairs

        repair = result.get("instructor_availability_repair") or {}
        reinstated = [
            ((source or {}).get("instructor_availability") or {}).get("ranked_last", [])
            for source in (result.get("required_section_reinstatement"), pre_repair)
        ]
        excused = excused_pairs(
            repair.get("locked_blocked", []), repair.get("unavoidable", []), *reinstated
        )
        grown = sorted(
            pair
            for pair, count in availability_after.items()
            if count > availability_before.get(pair, 0) and pair not in excused
        )
        if grown:
            blocking_regressions.append(
                {
                    "metric": "instructor_availability_violations",
                    "label": "Instructor teaching-time rules broken by this run",
                    "before": sum(availability_before.values()),
                    "after": sum(availability_after.values()),
                    "delta": sum(availability_after.values()) - sum(availability_before.values()),
                    "sections": [section for section, _teacher in grown],
                    "instructor_ids": sorted({teacher for _section, teacher in grown}),
                }
            )
    # Required-section guarantee — the rollback gate. This run's own work
    # (full mode's re-placement, current mode's cap/clash repair unplacing a
    # meeting, etc.) must never leave a required section with FEWER placed
    # sections than it had right before this run started (after the pre-step
    # above). Compared per course IDENTITY (course_key), not as one total, so
    # dropping one course while placing an unrelated one can't cancel out and
    # hide the loss — and not per display code, or CS112 PROGRAMMING II could be
    # dropped unseen while CS112 PROGRAMMING I was already short. A course that
    # was already short before this run (and could not be repaired) is
    # reported, not blocked, unless the run made it shorter still — see
    # core.services.timetable_required_sections module docstring.
    if guarantee_on:
        after_rows = find_missing_required_sections(scenario_id)
        missing_after = sections_short_by_course(after_rows)
        newly_missing = sorted(
            key for key, short in missing_after.items() if short > missing_before.get(key, 0)
        )
        if newly_missing:
            labels = course_labels_by_key(after_rows)
            blocking_regressions.append(
                {
                    "metric": "required_sections_missing",
                    "label": "Required sections dropped",
                    # Totals of sections short, so this row reads "1 -> 2" like
                    # every other regression the result chip renders. The
                    # per-identity counts behind them ({course_key: sections
                    # short}) keep "one short -> two short" on one course visible.
                    "before": sum(missing_before.values()),
                    "after": sum(missing_after.values()),
                    "before_by_course": dict(sorted(missing_before.items())),
                    "after_by_course": dict(sorted(missing_after.items())),
                    "sections": newly_missing,
                    "course_labels": [labels[key] for key in newly_missing],
                }
            )
    # A from-scratch build (the scenario had no placements before this run, e.g.
    # the deferred-generate path) cannot "regress" — there was nothing to
    # protect, and any board is strictly better than an empty one. Only the
    # rollback gate, designed to protect an EXISTING board, applies when a prior
    # board existed; otherwise discarding the build leaves the scenario empty.
    if blocking_regressions and not board_snapshot.is_empty:
        if teachers is not None:
            blocking_regressions = [
                _name_taken_over_sections(row, teachers.saved, teachers.final)
                for row in blocking_regressions
            ]
        candidate_final_score = result.get("final_score")
        restore_scenario(scenario_id, board_snapshot)
        if teachers is not None:
            # C9: the restore brought back the saved rows and names, so the
            # saved map is in force again; the remaining counts read it.
            teachers.final = teachers.saved
            instructor_map = teachers.saved.v2_map()
            result["instructor_schedule_metric"]["coverage"]["after"] = result[
                "instructor_schedule_metric"
            ]["coverage"]["before"]
        with teachers.pin(teachers.saved) if teachers is not None else nullcontext():
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
                    from core.services.timetable_instructor_cap_repair import _physical_states
                    from core.services.timetable_pr4_instructor import (
                        count_instructor_daily_overloads,
                        get_instructor_daily_cap,
                    )

                    # The counter looks sections up by id (a bare list crashed
                    # this path whenever instructors were linked), and counts a
                    # shared section's board copies once, as the repair does.
                    states = _physical_states(build_section_states_for_scenario(scenario_id))
                    remaining = count_instructor_daily_overloads(
                        {state.section_id: state for state in states},
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
        if isinstance(result.get("instructor_availability_repair"), dict):
            availability_repair = result["instructor_availability_repair"]
            result["candidate_instructor_availability_repair"] = availability_repair
            with teachers.pin(teachers.saved) if teachers is not None else nullcontext():
                restored_pairs = _availability_pairs(scenario_id)
            result["instructor_availability_repair"] = {
                "enabled": availability_repair.get("enabled", False),
                "rolled_back": True,
                "rollback_reason": "whole_board_rollback",
                "detected": availability_repair.get("detected", []),
                "repaired": [],
                "unplaced": [],
                "locked_blocked": [],
                "unavoidable": [],
                # Re-measured on the restored board under the saved map; a failed
                # read is unmeasured, never a count of zero.
                "remaining_violations": (
                    None if restored_pairs is _UNREAD else sum((restored_pairs or {}).values())
                ),
            }
            if restored_pairs is _UNREAD:
                result["instructor_availability_repair"]["remaining_unmeasured"] = True
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
        # The restored board spent nothing: no credit was used.
        result["instructor_schedule_metric"].pop("availability_repair_exceptions", None)
        if isinstance(result.get("quality_score"), dict):
            result["quality_score"]["instructor_schedule"] = instructor_before
        restore_baseline_student_gap(result)
        restore_baseline_instructor_availability(result)
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
        if teachers is not None:
            _score_restored_board(result, scenario_id, teachers)
        logger.warning(
            "V2 optimiser result rolled back for scenario %d: %s",
            scenario_id,
            blocking_regressions,
        )
    else:
        result["safety_blocked"] = False
        result["safety_regression"] = {"blocked": False, "regressions": []}
    if teachers is not None:
        from core.services.section_teacher_assignment import coverage_of

        result["section_teachers"] = {
            **(result.get("section_teachers") or {}),
            **teachers.report,
            "coverage": coverage_of(teachers.final),
        }

    # Regular-only gap option (absent when off): surface both numbers at the top
    # level of the result so the owner can see the visitors' gap the objective
    # no longer counts. Existing fields keep their meaning.
    attach_student_gap_fields(result)

    attach_optimiser_safety_metrics(result, safety_before, safety_after)

    if availability_before is not None:
        # B2-7: the authoritative count, read from the PERSISTED board under the
        # map in force once the rollback decision is made (a NEW key: the
        # existing ``instructor_availability`` of board results is untouched).
        from core.services.timetable_instructor_availability_report import (
            attachable,
            scenario_instructor_availability_report,
        )

        with teachers.pin(teachers.final) if teachers is not None else nullcontext():
            result["instructor_availability_report"] = attachable(
                scenario_instructor_availability_report(scenario_id)
            )

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
            describe_short_courses(final_report["missing"]),
        )

    return result
