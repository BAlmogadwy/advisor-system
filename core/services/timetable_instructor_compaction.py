"""Post-build instructor-day compaction pass.

Shrinks each instructor's WITHIN-DAY idle gaps by relocating their sessions in
TIME — it never changes who teaches what (one instructor per course is fixed by
policy). Runs AFTER the daily-cap repair and treats the cap as a hard gate.

Validated on scenario 627 (rolled-back replay): total instructor idle -40%, all
instructors improved / none worsened, feasibility + reserve unchanged, and total
student gap actually fell — because compacting an instructor's day tends to
compact the students who take those classes too.

Design (agreed with the user + an external review):
- **Worst-day-first** hill-climb. Each round targets the instructor-days with the
  largest within-day hole and tries to shrink them.
- **Instructor priority** protects each instructor's campus-day ceiling and
  strictly improves (total campus working days, total true idle). The shared
  selection policy compares feasible alternatives with the current iteration's
  hard ceilings frozen. With instructor priority disabled, the legacy objective
  remains (lexicographic, lower=better):
  (largest_within_day_hole, #instr-days_with_hole>90min, worst_weekly_idle,
   total_idle, added_student_gap). Attacks the visible "one bad day" first.
- **Layered student guards** (hard, vs the pre-pass baseline): feasibility
  (unresolved tier-A / total unresolved / unassigned / clashes) and reserve never
  worsen; total student gap-minutes ≤ baseline·(1+budget); tier-A AND graduating
  (tier-B) added gap ≤ 0; per-student added gap ≤ a ceiling. A **trade alert**
  rejects any move whose student-gap cost isn't repaid by enough instructor
  saving (ratio guard) — the safety net for scenarios where the win/win
  alignment does not hold.
- **Modular neighbourhood**: relocation today; ``swap`` (and later chain/LNS) can
  be added to ``_NEIGHBOURHOODS`` without touching the evaluator or guards.
- **Oscillation guards**: max rounds, accepted-move cap, no-revisit set, and the
  strict-improvement rule.

Flag-gated (``is_instructor_compaction_enabled``); no-op when off.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from django.db import transaction

from core.services.course_identity import course_display_code
from core.services.timetable_assignment_models import RiskTier, SectionMeeting
from core.services.timetable_board_persistence import external_shared_section_ids
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_optimisation_policy import (
    MAX_WORKING_DAYS,
    current_day_off_policy,
    evaluation_is_better,
    evaluation_order_key,
    instructor_schedule_metrics,
    instructor_schedule_regressions,
    is_instructor_priority_enabled,
)
from core.services.timetable_pr4_instructor import (
    get_instructor_compaction_config,
    get_instructor_daily_cap,
    get_instructor_day_trade_config,
    is_instructor_compaction_enabled,
    is_instructor_day_trade_active,
)

logger = logging.getLogger(__name__)

WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _interval_busy(slotset, day2: int, s2: int, e2: int) -> bool:
    """True if ``[s2, e2)`` on ``day2`` overlaps any interval in ``slotset``.

    Occupancy is stored as ``(day, start_min, end_min)`` so the guard tests true
    INTERVAL overlap, not start-time equality. The lecture grid (e.g. 10:30-11:45)
    and lab grid (09:00-10:40) interleave, so two sessions can overlap in time at
    different start minutes — a start-only guard misses that and lets a relocation
    create a ``same_board_overlaps`` the safety gate rolls back wholesale.
    """
    return any(d == day2 and s2 < oe and e2 > os for (d, os, oe) in slotset)


def compact_instructor_schedules(scenario_id: int) -> dict:
    """Compact instructor days for a scenario. Persists accepted relocations.
    Returns a full before/after audit report. No-op when the flag is off."""
    report: dict = {"enabled": False}
    if not is_instructor_compaction_enabled():
        return report
    report["enabled"] = True

    from core.services.timetable_fixed_pattern_search import improve_fixed_course_patterns

    report["weekly_pattern_search"] = improve_fixed_course_patterns(scenario_id)

    from core.models import SectionPlacement, TermSectionMeeting, TimetableScenario
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_autoplace import placeable_slots
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import (
        is_gap_regular_students_only_enabled,
        is_tiered_objective_enabled,
    )
    from core.services.timetable_online import OnlineCourseLookup
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_locked_section_ids_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_rooming import assign_rooms_to_board
    from core.services.timetable_student_assignment import (
        TI_SOFT,
        is_tiered_score,
        reserve_used_of,
    )
    from core.services.timetable_validation import blocked_slot_keys

    cfg = get_instructor_compaction_config()
    cap = get_instructor_daily_cap()
    instructor_priority = is_instructor_priority_enabled()
    # Bounded student-gap-for-instructor-day trade. Only meaningful under the
    # instructor-first policy (days are a ranked quantity there); default OFF.
    trade = get_instructor_day_trade_config()
    trade_on = is_instructor_day_trade_active()
    # The run's day-off policy (R1-R5), when the optimiser runs this pass inside
    # its scope: moves are judged against the run's start (see ``_assess``).
    day_off = current_day_off_policy()
    room_stats: dict = {"rejected": 0, "over_floor": {}}
    if trade_on:
        # Report-only; absent while the rule is off so the report matches master.
        report["trade_rule"] = {**trade, "enabled": True, "room_gate_unavailable": None}

    scenario = TimetableScenario.objects.get(id=scenario_id)
    states = build_section_states_for_scenario(scenario_id)
    if not states:
        report["note"] = "no placements"
        return report
    # Ordinary shared sections have a placement copy on each board. Search
    # physical meetings once; duplicate copies are not extra teaching sessions.
    for state in states:
        state.meetings = list({(m.day, m.start_min, m.end_min): m for m in state.meetings}.values())
    sbi = {s.section_id: s for s in states}
    # Empty profiles are fine: the student gates simply become vacuous and the
    # pass optimises instructor idle freely (still respecting the daily cap).
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id) if profiles else {}
    cap_map = build_section_instructor_map_for_scenario(scenario_id)
    course_tiers = (
        build_course_tier_map_for_scenario(scenario_id) if is_tiered_objective_enabled() else None
    )
    if not cap_map:
        report["note"] = "no instructor assignments (links off?)"
        return report
    locked = build_locked_section_ids_for_scenario(scenario_id)
    # Classic relocation has no modality-aware domains. Keep remote teaching
    # fixed until it can preserve online delivery explicitly.
    locked.update(s.section_id for s in states if s.delivery_is_online or s.is_online)
    tier = {sid: p.risk_tier for sid, p in profiles.items()}
    code_of = {sid: s.course_code for sid, s in sbi.items()}

    lec = [(_to_min(s["start"]), _to_min(s["end"])) for s in placeable_slots(scenario.slot_config)]
    lab = [
        (_to_min(s["start"]), _to_min(s["end"])) for s in placeable_slots(scenario.lab_slot_config)
    ]
    blocked = {(d.upper(), _to_min(st)) for (d, st) in blocked_slot_keys(scenario.blocked_slots)}

    # ── DB ↔ in-memory mapping for persistence ──
    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("term_section", "board")
    )

    def _sid(p) -> str:
        ts = p.term_section
        return f"{ts.course_key or ts.course_code}_{ts.section}"

    placement_of: dict[tuple[str, int, int, int], list] = defaultdict(list)
    board_signatures: dict[str, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    sid2board: dict[str, object] = {}
    sid2ts: dict[str, object] = {}
    for p in placements:
        sid = _sid(p)
        di = WEEKDAYS.index(p.day.upper()) if p.day.upper() in WEEKDAYS else -1
        signature = (di, _to_min(p.start_time), _to_min(p.end_time))
        placement_of[(sid, *signature)].append(p)
        board_signatures[sid][p.board_id].add(signature)
        sid2board.setdefault(sid, p.board)
        sid2ts.setdefault(sid, p.term_section)
    external_ids = external_shared_section_ids(scenario_id)
    report["externally_owned_sections_frozen"] = sorted(external_ids)
    locked.update(_sid(p) for p in placements if p.term_section_id in external_ids)
    # Existing divergent copies cannot be mapped to one physical schedule.
    # Leave the entire section fixed until an explicit shared-section repair.
    locked.update(
        sid
        for sid, copies in board_signatures.items()
        if len({frozenset(signature) for signature in copies.values()}) > 1
    )

    # Capture the board at this optional pass's entry, AFTER mandatory repairs.
    # Its persistence savepoint must never roll those repairs back with it.
    online_lookup = OnlineCourseLookup()

    def physical_unassigned(rows):
        return sum(
            1
            for p in rows
            if (not (p.room or "").strip() or p.room.strip().upper() == "UNASSIGNED")
            and not get_course_meeting_policy(
                p.term_section.course_code, p.term_section.course_key, p.term_section.course_number
            )
            and not online_lookup.is_online_course_for_board(
                p.board,
                course_display_code(
                    p.term_section.course_key,
                    p.term_section.course_code,
                    p.term_section.course_number,
                ),
            )
        )

    rooms_before = physical_unassigned(placements)

    # A lock freezes times and meeting assignments, not rooms: rooming may
    # legitimately re-room a locked meeting (and stamp the row's updated_at).
    def locked_snapshot(
        term_section_ids: set[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[Any]]:
        rows = [
            {k: v for k, v in row.items() if k not in ("room", "updated_at")}
            for row in SectionPlacement.objects.filter(
                board__scenario_id=scenario_id, is_locked=True
            )
            .order_by("pk")
            .values()
        ]
        ts_ids = (
            {row["term_section_id"] for row in rows}
            if term_section_ids is None
            else term_section_ids
        )
        meetings = [
            {k: v for k, v in row.items() if k != "room"}
            for row in TermSectionMeeting.objects.filter(term_section_id__in=ts_ids)
            .order_by("pk")
            .values()
        ]
        return rows, meetings, ts_ids

    locked_before, locked_meetings_before, locked_ts_ids = locked_snapshot()

    # ── Metrics ──
    def evaluate():
        return evaluate_generated_timetable_candidate(
            "compaction",
            states,
            profiles,
            rigidity,
            section_instructor_ids=cap_map,
            course_tiers=course_tiers,
        )

    # TIMETABLE_GAP_REGULAR_STUDENTS_ONLY: visitors (cross-term students) leave
    # the AGGREGATE gap total only - the global budget ceiling, the day-trade total
    # and the gap_added trade ratio. Per-student caps and the tier A/B totals still
    # cover every student, so a visitor keeps the same protection.
    regular_gap_only = is_gap_regular_students_only_enabled()
    visitor_ids = (
        {sid for sid, p in profiles.items() if p.is_visitor} if regular_gap_only else set()
    )

    def student_metrics(res):
        per = {sid: st.total_gap_minutes for sid, st in res.assignment_states.items()}
        total = sum(g for sid, g in per.items() if sid not in visitor_ids)
        by_tier: dict = defaultdict(int)
        for s, g in per.items():
            by_tier[tier.get(s)] += g
        return per, total, by_tier

    def instr_metrics():
        byday: dict = defaultdict(list)
        for sid, instrs in cap_map.items():
            s = sbi.get(sid)
            if not s or (instructor_priority and (s.delivery_is_online or s.is_online)):
                continue
            for iid in instrs:
                for m in s.meetings:
                    byday[(iid, m.day)].append((m.start_min, m.end_min))
        largest = over90 = total = 0
        weekly: dict = defaultdict(int)
        holes: dict = {}
        for key, sess in byday.items():
            sess = sorted(set(sess))
            if instructor_priority:
                gaps = []
                end = 0
                for start, finish in sess:
                    if end and start > end:
                        gaps.append(start - end)
                    end = max(end, finish)
            else:
                gaps = [
                    g for g in (sess[i + 1][0] - sess[i][1] for i in range(len(sess) - 1)) if g > 0
                ]
            di = sum(gaps)
            hole = max(gaps) if gaps else 0
            total += di
            weekly[key[0]] += di
            largest = max(largest, hole)
            holes[key] = (hole, di)
            if hole > 90:
                over90 += 1
        return {
            "largest": largest,
            "over90": over90,
            "worst_weekly": max(weekly.values()) if weekly else 0,
            "total": total,
            "holes": holes,
            "weekly": dict(weekly),
        }

    # ── Occupancy (kept in sync during search) ──
    instr_slots: dict = defaultdict(set)
    course_slots: dict = defaultdict(set)
    section_days: dict = defaultdict(set)
    for sid, s in sbi.items():
        for m in s.meetings:
            section_days[sid].add(m.day)
            course_slots[code_of[sid]].add((m.day, m.start_min, m.end_min))
            for iid in cap_map.get(sid, frozenset()):
                instr_slots[iid].add((m.day, m.start_min, m.end_min))

    def _relocation_moves(sid, midx, iids):
        """Neighbourhood generator: feasible (day2, s2, e2) targets for one
        session, honouring all-diff-days, blocked slots, instructor clash,
        same-course separation and the daily cap. (Swap/chain go here later.)"""
        meeting = sbi[sid].meetings[midx]
        if get_course_meeting_policy(sbi[sid].course_code):
            return  # Already searched as a complete section, never as one day.
        dur = meeting.end_min - meeting.start_min
        slots = lab if dur > 75 else lec
        others = section_days[sid] - {meeting.day}
        course = code_of[sid]
        for day2 in range(len(WEEKDAYS)):
            same_day = day2 == meeting.day
            if not same_day and day2 in others:
                continue
            if not same_day and any(
                sum(1 for (d, _s, _e) in instr_slots[iid] if d == day2) >= cap for iid in iids
            ):
                continue
            for s2, e2 in slots:
                # Relocation changes time, never the weekly contact minutes.
                if e2 - s2 != dur:
                    continue
                if same_day and s2 == meeting.start_min:
                    continue
                if (WEEKDAYS[day2], s2) in blocked:
                    continue
                if any(_interval_busy(instr_slots[iid], day2, s2, e2) for iid in iids):
                    continue
                if _interval_busy(course_slots[course], day2, s2, e2):
                    continue
                yield day2, s2, e2

    _NEIGHBOURHOODS = [_relocation_moves]  # swap/chain/LNS slot in here

    def _apply(sid, midx, day2, s2, e2, iids):
        old = sbi[sid].meetings[midx]
        sbi[sid].meetings[midx] = SectionMeeting(day2, s2, e2)
        section_days[sid].discard(old.day)
        section_days[sid].add(day2)
        course_slots[code_of[sid]].discard((old.day, old.start_min, old.end_min))
        course_slots[code_of[sid]].add((day2, s2, e2))
        for iid in iids:
            instr_slots[iid].discard((old.day, old.start_min, old.end_min))
            instr_slots[iid].add((day2, s2, e2))
        return old

    def _revert(sid, midx, old, day2, s2, e2, iids):
        course_slots[code_of[sid]].discard((day2, s2, e2))
        course_slots[code_of[sid]].add((old.day, old.start_min, old.end_min))
        section_days[sid].discard(day2)
        section_days[sid].add(old.day)
        for iid in iids:
            instr_slots[iid].discard((day2, s2, e2))
            instr_slots[iid].add((old.day, old.start_min, old.end_min))
        sbi[sid].meetings[midx] = old

    # ── Baseline ──
    base_eval = evaluate()
    base_score = tuple(base_eval.lexicographic_score)
    base_per, base_total_gap, base_by_tier = student_metrics(base_eval)
    base_instr = instr_metrics()
    base_schedule = instructor_schedule_metrics(states, cap_map)
    orig_pos = {
        sid: [(m.day, m.start_min, m.end_min) for m in s.meetings] for sid, s in sbi.items()
    }

    gap_ceiling = base_total_gap * (1.0 + cfg["gap_budget"])

    # The accepted board so far, refreshed every round: student gates are measured
    # against the PRE-PASS baseline, but a day-saving trade may have lifted a
    # student above that baseline, and an ordinary later move must not be blamed
    # for it — it is only barred from making the student WORSE than they now are.
    cur_ctx: dict = {"per": base_per, "total": base_total_gap, "by_tier": base_by_tier}

    def gates_ok(res, idle_saved, gap_added, day_saving=False):
        sc = tuple(res.lexicographic_score)
        # Positions 0-3 are the hard feasibility block in BOTH layouts (legacy:
        # tier_a/unres/unassigned/clash; tiered: high-risk/clash/T1/T2-over), so
        # each must be non-worsening independently: fewer high-risk unresolved
        # students must not compensate for more total unresolved or clashes.
        # Reserve is idx 5 legacy /
        # idx 6 tiered — reserve_used_of resolves it. In the tiered layout idx 5
        # is soft_unresolved (T3 + T2-within-tolerance): guard it too so
        # relocating instructor sessions can never strand a soft-tier student
        # that the legacy [5] slot used to protect via reserve.
        if any(sc[i] > base_score[i] for i in range(4)):
            return False
        reserve_slack = trade["reserve_allowance"] if (trade_on and day_saving) else 0
        if reserve_used_of(sc) > reserve_used_of(base_score) + reserve_slack:
            return False
        if is_tiered_score(sc) and sc[TI_SOFT] > base_score[TI_SOFT]:
            return False
        per, total, by_tier = student_metrics(res)
        if trade_on and day_saving:
            # Bounded trade: this move saves an instructor campus day, so it may
            # cost students up to the configured totals. High-risk (A) and
            # graduating (B) students get their own, normally tighter, ceiling.
            if total - base_total_gap > trade["total_gap"]:
                return False
            for s, g in per.items():
                limit = (
                    trade["high_risk_per_student"]
                    if tier.get(s) in (RiskTier.A, RiskTier.B)
                    else trade["per_student"]
                )
                if g - base_per.get(s, 0) > limit and g > cur_ctx["per"].get(s, 0):
                    return False
            return True
        if total > max(gap_ceiling, cur_ctx["total"]):
            return False
        if by_tier.get(RiskTier.A, 0) > max(
            base_by_tier.get(RiskTier.A, 0), cur_ctx["by_tier"].get(RiskTier.A, 0)
        ):
            return False
        if by_tier.get(RiskTier.B, 0) > max(
            base_by_tier.get(RiskTier.B, 0), cur_ctx["by_tier"].get(RiskTier.B, 0)
        ):
            return False
        for s, g in per.items():
            if g - base_per.get(s, 0) > cfg["per_student_cap"] and g > cur_ctx["per"].get(s, 0):
                return False
        # Trade alert: if this move costs student spread, require enough payoff.
        if gap_added > 0 and idle_saved < cfg["trade_ratio"] * gap_added:
            return False
        return True

    def ituple(im, added_gap):
        return (im["largest"], im["over90"], im["worst_weekly"], im["total"], added_gap)

    movable = [
        (sid, mi)
        for sid in sbi
        for mi in range(len(sbi[sid].meetings))
        if cap_map.get(sid) and sid not in locked
    ]
    movable_instructors = {iid for sid, _mi in movable for iid in cap_map.get(sid, ())}
    movable_set = set(movable)

    # Exact room evaluator for day-saving trades (read-only; see _assess). It fails
    # CLOSED: without it no trade is accepted (ordinary compaction is unaffected),
    # and the reason is reported under ``trade_rule.room_gate_unavailable``.
    room_gate = None
    if trade_on:
        from core.services.timetable_exact_rooming import ExactRoomingError
        from core.services.timetable_recovery_rooming import build_recovery_room_evaluator

        try:
            # The baseline is solved ONCE, with a long budget, so its unroomed count
            # is normally PROVEN optimal; that proven count is the floor. If even
            # the long budget cannot prove it, the floor is the baseline's count
            # under the SAME short-budget method the candidates use, so a candidate
            # is never held to a stricter standard than the board it replaces.
            _baseline_eval = build_recovery_room_evaluator(
                scenario_id, time_limit_seconds=trade["room_baseline_seconds"]
            )
            _room_eval = build_recovery_room_evaluator(
                scenario_id, time_limit_seconds=trade["room_candidate_seconds"]
            )
            _baseline = _baseline_eval(states)
            _proven = bool(_baseline.get("count_proven_optimal"))
            _floor = (
                int(_baseline["unassigned_count"])
                if _proven
                else int(_room_eval(states)["unassigned_count"])
            )
            room_gate = {"evaluate": _room_eval, "baseline_floor": _floor}
            report["trade_rule"]["room_baseline"] = {
                "unassigned": int(_baseline["unassigned_count"]),
                "proven": _proven,
                "floor": _floor,
                "floor_source": "proven_long_budget" if _proven else "candidate_method",
            }
        except ExactRoomingError as exc:
            report["trade_rule"]["room_gate_unavailable"] = str(exc) or type(exc).__name__

    cur_instr = base_instr
    cur_eval = base_eval
    cur_schedule = base_schedule
    visited: set = set()
    moves_evaluated = moves_accepted = 0
    max_moves = max(20, len(movable) * 4)

    def _signature():
        return frozenset((sid, m.day, m.start_min) for sid, s in sbi.items() for m in s.meetings)

    visited.add(_signature())

    # Wall-clock budget — worst-day-first front-loads the biggest wins, so when
    # time runs out we stop and keep what we found (safe for a sync UI rebuild).
    t0 = time.monotonic()
    budget = cfg["time_budget"]

    def _over_budget() -> bool:
        return budget > 0 and (time.monotonic() - t0) > budget

    def _assess(best):
        """Evaluate the board AS CURRENTLY APPLIED; return ``(it, res, im, sig, rank)``
        when it is an acceptable improvement over the round's ``best`` candidate
        (or the accepted board), else None. A move that saves an instructor campus day may, when
        the bounded trade is on, spend students/idle within its configured
        limits; every other move keeps the original gates unchanged.

        With the run's day-off policy (R5): only a FIRST day off (an instructor
        going from 5 working days to 4 or fewer) may spend the trade's student
        budgets; a second or third day off must not cost students at all; no
        instructor's days may rise; and a move is judged by the policy against
        the run's start, so one instructor's idle may rise when the total falls
        (R4), and the trade's idle allowance holds for a first day off only."""
        nonlocal moves_evaluated
        sig = _signature()
        if sig in visited:
            return None
        res = evaluate()
        moves_evaluated += 1
        im = instr_metrics()
        per_after, tg, _ = student_metrics(res)
        idle_saved = cur_instr["total"] - im["total"]
        gap_added = tg - cur_gap
        # Legacy (priority off) never reads the schedule metrics; tolerate an
        # evaluation that carries none (the gates below reject it first).
        trial_schedule = (
            (getattr(res, "quality_score", None) or {}).get("instructor_schedule") or {}
            if instructor_priority
            else {}
        )
        day_saved = cur_schedule["working_days"] - int(
            trial_schedule.get("working_days", cur_schedule["working_days"])
        )
        schedule_ceiling = cur_schedule
        if trade_on and day_saved > 0 and trade["idle_allowance"] > 0:
            # The merged day may cost a little idle; allow it only for the
            # instructors whose day count actually fell.
            allowed = dict(cur_schedule["idle_minutes_by_instructor"])
            for iid, old_days in cur_schedule["working_days_by_instructor"].items():
                new_days = trial_schedule["working_days_by_instructor"].get(iid, old_days)
                if new_days < old_days:
                    allowed[iid] = (
                        allowed.get(iid, 0) + (old_days - new_days) * trade["idle_allowance"]
                    )
            schedule_ceiling = {
                **cur_schedule,
                "idle_minutes_by_instructor": allowed,
                "idle_minutes": sum(allowed.values()),
            }
        day_saving = day_saved > 0
        if day_off is not None:
            cur_days = cur_schedule["working_days_by_instructor"]
            trial_days = trial_schedule.get("working_days_by_instructor") or {}
            teachers = set(cur_days) | set(trial_days)
            before = {i: int(cur_days.get(i, 0)) for i in teachers}
            after = {i: int(trial_days.get(i, 0)) for i in teachers}
            if any(after[i] > before[i] for i in teachers):
                return None  # after the search no instructor's days rise
            first_day_off = any(before[i] > MAX_WORKING_DAYS >= after[i] for i in teachers)
            if (
                not first_day_off
                and any(after[i] < before[i] for i in teachers)
                and tuple(res.lexicographic_score) > tuple(cur_eval.lexicographic_score)
            ):
                return None  # R5: a second or third day off never costs students
            day_saving = first_day_off  # R5: the student budgets buy first days off only
        if not gates_ok(res, idle_saved, gap_added, day_saving):
            return None
        if trade_on and day_saved > 0:
            # A traded move relocates whole sessions; keep it only if the room
            # evaluator leaves no more meetings unroomed than the baseline floor
            # (see ``room_gate``). This is a pre-filter, not the safety net: the
            # persist step re-counts the placements that really have no room after
            # real rooming and rolls the WHOLE pass back on any increase over
            # ``rooms_before`` (``candidate_rooms > rooms_before`` below), so an
            # optimistic evaluator costs the pass, never a worse persisted board.
            if room_gate is None:
                room_stats["rejected"] += 1
                return None
            try:
                over_floor = (
                    room_gate["evaluate"](states)["unassigned_count"] - room_gate["baseline_floor"]
                )
            except ExactRoomingError:
                over_floor = None
            if over_floor is None or over_floor > 0:
                room_stats["rejected"] += 1
                if over_floor is not None:
                    key = str(over_floor)
                    room_stats["over_floor"][key] = room_stats["over_floor"].get(key, 0) + 1
                return None
        it = ituple(im, tg - base_total_gap)
        rank = None
        if day_off is not None:
            # The policy's step (prelude of evaluation_is_better) against the
            # board in hand replaces the strict per-instructor ceilings, and the
            # move must buy an instructor gain (days, then idle). Candidates are
            # ranked by the policy's key with the student tail as tie-breaks, the
            # same shape as the trade rank below.
            improves = evaluation_is_better(res, cur_eval, feasibility_ceilings=cur_eval) and (
                trial_schedule["working_days"],
                trial_schedule["idle_minutes"],
            ) < (cur_schedule["working_days"], cur_schedule["idle_minutes"])
            if improves:
                max_added = max(
                    (g - cur_ctx["per"].get(sid_, 0) for sid_, g in per_after.items()), default=0
                )
                policy_key = day_off.key(res)
                rank = (*policy_key[:6], gap_added, max_added, *policy_key[6:])
                improves = best is None or rank < best[5]
        elif instructor_priority and trade_on:
            # Accept only a strict improvement on the board in hand, then rank the
            # acceptable candidates: fewest working days, least instructor idle,
            # and WITHIN equal idle the least student cost - lower total added gap,
            # then lower worst per-student added gap - so the student tail is as
            # short as the trade allows. Ties fall through to the policy key.
            improves = (
                evaluation_is_better(
                    res,
                    cur_eval,
                    day_ceilings=schedule_ceiling,
                    feasibility_ceilings=cur_eval,
                )
                and (not instructor_schedule_regressions(schedule_ceiling, trial_schedule))
                and (trial_schedule["working_days"], trial_schedule["idle_minutes"])
                < (cur_schedule["working_days"], cur_schedule["idle_minutes"])
            )
            if improves:
                max_added = max(
                    (g - cur_ctx["per"].get(sid_, 0) for sid_, g in per_after.items()), default=0
                )
                policy_key = evaluation_order_key(res)
                rank = (*policy_key[:6], gap_added, max_added, *policy_key[6:])
                improves = best is None or rank < best[5]
        elif instructor_priority:
            improves = evaluation_is_better(
                res,
                best[2] if best is not None else cur_eval,
                day_ceilings=schedule_ceiling,
                feasibility_ceilings=cur_eval,
            )
            improves = (
                improves
                and (not instructor_schedule_regressions(schedule_ceiling, trial_schedule))
                and (trial_schedule["working_days"], trial_schedule["idle_minutes"])
                < (cur_schedule["working_days"], cur_schedule["idle_minutes"])
            )
        else:
            improves = it < cur_tuple and (best is None or it < best[0])
        return (it, res, im, sig, rank) if improves else None

    def _vacate_leaves(iid, day):
        """Joint relocations that EMPTY ``day`` for instructor ``iid`` by moving
        each of that day's sessions onto days the instructor already works.
        One session at a time cannot do this when the day holds two or more
        (the first move leaves the day open and gains nothing), so the sessions
        are moved together. Yields lists of ``(sid, midx, day2, s2, e2)``."""
        on_day = [
            (sid, mi)
            for sid, st in sbi.items()
            if iid in cap_map.get(sid, ()) and not (st.delivery_is_online or st.is_online)
            for mi, m in enumerate(st.meetings)
            if m.day == day
        ]
        if not on_day or len(on_day) > trade["vacate_max_sessions"]:
            return
        if any((sid, mi) not in movable_set for sid, mi in on_day):
            return  # a locked / fixed-pattern / online session pins the day
        other_days = {
            m.day
            for sid, st in sbi.items()
            if iid in cap_map.get(sid, ()) and not (st.delivery_is_online or st.is_online)
            for m in st.meetings
            if m.day != day
        }

        def walk(index, chosen):
            if index == len(on_day):
                yield list(chosen)
                return
            sid, mi = on_day[index]
            iids = cap_map.get(sid, frozenset())
            for day2, s2, e2 in list(_relocation_moves(sid, mi, iids)):
                if day2 not in other_days:
                    continue
                old = _apply(sid, mi, day2, s2, e2, iids)
                chosen.append((sid, mi, day2, s2, e2))
                try:
                    yield from walk(index + 1, chosen)
                finally:
                    chosen.pop()
                    _revert(sid, mi, old, day2, s2, e2, iids)

        yield from walk(0, [])

    timed_out = False
    _round = 0
    for _round in range(cfg["max_rounds"]):
        if moves_accepted >= max_moves or _over_budget():
            break
        worst_days = sorted(cur_instr["holes"].items(), key=lambda kv: -kv[1][0])
        target_iids = list(dict.fromkeys(iid for (iid, _d), (h, _di) in worst_days if h > 0))[:3]
        if instructor_priority:
            days_by_instructor = cur_schedule["working_days_by_instructor"]
            target_iids = sorted(
                movable_instructors,
                key=lambda iid: (
                    -days_by_instructor.get(str(iid), 0),
                    -cur_instr["weekly"].get(iid, 0),
                    iid,
                ),
            )
            target_iids = [
                iid
                for iid in target_iids
                if days_by_instructor.get(str(iid), 0) > 1 or cur_instr["weekly"].get(iid, 0) > 0
            ]
        if not target_iids:
            break
        cur_per, cur_gap, cur_by_tier = student_metrics(cur_eval)
        cur_ctx.update(per=cur_per, total=cur_gap, by_tier=cur_by_tier)
        cur_tuple = ituple(cur_instr, cur_gap - base_total_gap)
        best = None
        # Try three priority instructors first. If that group has no feasible
        # gain, broaden to the next group instead of falsely declaring a local
        # optimum because the busiest teachers cannot move.
        target_groups = {iid: index // 3 for index, iid in enumerate(target_iids)}

        def move_group(move, groups=target_groups):
            return min(
                (groups[iid] for iid in cap_map.get(move[0], ()) if iid in groups),
                default=len(groups),
            )

        active_group = 0
        for sid, midx in sorted(movable, key=move_group):
            iids = cap_map.get(sid, frozenset())
            if not (set(iids) & set(target_iids)):
                continue
            group = move_group((sid, midx))
            if instructor_priority and group != active_group and best is not None:
                break
            active_group = group
            for gen in _NEIGHBOURHOODS:
                for day2, s2, e2 in gen(sid, midx, iids):
                    old = _apply(sid, midx, day2, s2, e2, iids)
                    found = _assess(best)
                    if found is not None:
                        best = (
                            found[0],
                            [(sid, midx, day2, s2, e2)],
                            found[1],
                            found[2],
                            found[3],
                            found[4],
                        )
                    _revert(sid, midx, old, day2, s2, e2, iids)
                    if _over_budget():
                        timed_out = True
                        break
                if timed_out:
                    break
            if timed_out:
                break

        # Day-vacating joint moves. Single relocations cannot save a day when the
        # instructor teaches two sessions on it, so when nothing better turned up
        # try emptying such a day outright. Candidates are ranked by the cheap
        # instructor-only cost first; only the best few reach the (expensive)
        # student evaluation.
        if best is None and trade_on and trade["vacate_days"] and not timed_out:
            by_cost: list = []
            before_weekly = dict(cur_instr["weekly"])
            before_days_of: dict = defaultdict(int)
            for i_, _d in cur_instr["holes"]:
                before_days_of[i_] += 1
            for iid in target_iids:
                for day in sorted({d for (i_, d) in cur_instr["holes"] if i_ == iid}):
                    leaves = []
                    for leaf in _vacate_leaves(iid, day):
                        # _vacate_leaves leaves the moves APPLIED while yielding,
                        # so the cheap instructor-only metrics read the trial board.
                        im2 = instr_metrics()
                        if sum(1 for (i_, _d) in im2["holes"] if i_ == iid) < before_days_of[iid]:
                            added = im2["weekly"].get(iid, 0) - before_weekly.get(iid, 0)
                            leaves.append((added, im2["total"] - cur_instr["total"], leaf))
                        if _over_budget():
                            timed_out = True
                            break
                    leaves.sort(key=lambda row: (row[0], row[1], row[2]))
                    by_cost.extend(leaves[: trade["vacate_top_n"]])
                    if timed_out:
                        break
                if timed_out:
                    break
            by_cost.sort(key=lambda row: (row[0], row[1], row[2]))
            for _added, _tot, leaf in by_cost:
                if timed_out or _over_budget():
                    timed_out = True
                    break
                applied = []
                iid_sets = []
                for sid, mi, day2, s2, e2 in leaf:
                    iset = cap_map.get(sid, frozenset())
                    applied.append((sid, mi, day2, s2, e2, _apply(sid, mi, day2, s2, e2, iset)))
                    iid_sets.append(iset)
                found = _assess(best)
                if found is not None:
                    best = (found[0], list(leaf), found[1], found[2], found[3], found[4])
                for (sid, mi, day2, s2, e2, old), iset in zip(
                    reversed(applied), reversed(iid_sets), strict=True
                ):
                    _revert(sid, mi, old, day2, s2, e2, iset)
                if _over_budget():
                    timed_out = True
                    break
        if best is None:
            break
        _, chosen_moves, res, im, sig, _rank = best
        for sid, midx, day2, s2, e2 in chosen_moves:
            _apply(sid, midx, day2, s2, e2, cap_map.get(sid, frozenset()))
        visited.add(sig)
        cur_instr, cur_eval = im, res
        cur_schedule = instructor_schedule_metrics(states, cap_map)
        moves_accepted += 1

    if trade_on:
        report["trade_rule"]["room_gate_rejected"] = room_stats["rejected"]
        # How far over the floor the rejected candidates were: {"1": n, "2": m, ...}.
        report["trade_rule"]["room_gate_rejected_over_floor"] = room_stats["over_floor"]

    # ── Persist accepted relocations ──
    touched_boards: set = set()
    relocations: list = []
    rollback_reason = None
    candidate_rooms = rooms_before
    candidate_moves_accepted = moves_accepted
    candidate_instructor_impact = {
        "working_days": cur_schedule["working_days"],
        "working_days_by_instructor": cur_schedule["working_days_by_instructor"],
        "total_idle": cur_instr["total"],
        "largest_hole": cur_instr["largest"],
    }
    candidate_total_gap = student_metrics(cur_eval)[1]
    candidate_locked_preserved = True
    moved_shared_group = False
    shared_room_error = None
    with transaction.atomic():
        for sid, s in sbi.items():
            for i, m in enumerate(s.meetings):
                od, os_, oe = orig_pos[sid][i]
                if (m.day, m.start_min, m.end_min) == (od, os_, oe):
                    continue
                copies = placement_of.get((sid, od, os_, oe))
                if not copies:
                    continue
                moved_shared_group |= len(copies) > 1
                new_day, new_start, new_end = WEEKDAYS[m.day], _hhmm(m.start_min), _hhmm(m.end_min)
                TermSectionMeeting.objects.filter(
                    term_section=copies[0].term_section,
                    day=WEEKDAYS[od],
                    start_time=_hhmm(os_),
                    end_time=_hhmm(oe),
                ).update(day=new_day, start_time=new_start, end_time=new_end)
                for p in copies:
                    p.day, p.start_time, p.end_time, p.room = new_day, new_start, new_end, ""
                    p.save(update_fields=["day", "start_time", "end_time", "room"])
                    touched_boards.add(p.board_id)
                relocations.append(
                    {
                        "section": f"{code_of[sid]} {sid.split('_')[-1]}",
                        "from": f"{WEEKDAYS[od]} {_hhmm(os_)}",
                        "to": f"{new_day} {new_start}",
                    }
                )
        for sid in {s for s, _i in movable}:
            if sid2board.get(sid) and sid2board[sid].id in touched_boards:
                apply_primary_instructor(
                    sid2ts[sid],
                    scenario,
                    sid2board[sid],
                    course_display_code(
                        sid2ts[sid].course_key, sid2ts[sid].course_code, sid2ts[sid].course_number
                    ),
                )
        for board_id in touched_boards:
            assign_rooms_to_board(board_id, respect_locked=True)

        if moved_shared_group:
            # Per-board rooming sees another copy as competing occupancy.
            # Resolve the physical scenario once, then fan that exact room
            # assignment to all copies and canonical meetings in this savepoint.
            from core.services.timetable_exact_rooming import ExactRoomingError
            from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
            from core.services.timetable_secondary_recovery_runner import (
                _persist_exact_room_result,
                _sync_recovered_meetings,
            )

            try:
                # With the trade ON, re-room with the same long budget the baseline got,
                # so a trade the gate judged equal is not undone by a weaker persist solve
                # that leaves more meetings unroomed (and rolls the whole pass back). With
                # it OFF the solve keeps its default budget, exactly as before.
                room_evaluator = build_recovery_room_evaluator(
                    scenario_id,
                    **({"time_limit_seconds": trade["room_baseline_seconds"]} if trade_on else {}),
                )
                room_result = room_evaluator(states)
                physical_rows = list(
                    SectionPlacement.objects.filter(board__scenario_id=scenario_id).values()
                )
                physical_meetings = list(
                    TermSectionMeeting.objects.filter(
                        term_section__scenario_id=scenario_id
                    ).values()
                )
                _persist_exact_room_result(scenario_id, room_result, physical_rows)
                _sync_recovered_meetings(
                    physical_rows, physical_meetings, room_result, scenario_id=scenario_id
                )
            except ExactRoomingError as exc:
                shared_room_error = str(exc)

        if touched_boards:
            candidate_rows = list(
                SectionPlacement.objects.filter(board__scenario_id=scenario_id)
                .exclude(day="")
                .select_related("board", "term_section")
            )
            candidate_rooms = physical_unassigned(candidate_rows)
            locked_after, locked_meetings_after, _ = locked_snapshot(locked_ts_ids)
            candidate_locked_preserved = (
                locked_after == locked_before and locked_meetings_after == locked_meetings_before
            )
            reasons = []
            if shared_room_error:
                reasons.append(f"shared physical room assignment unavailable: {shared_room_error}")
            if candidate_rooms > rooms_before:
                reasons.append(
                    f"physical meetings without rooms increased from {rooms_before} to {candidate_rooms}"
                )
            if not candidate_locked_preserved:
                reasons.append("a locked placement or meeting assignment changed")
            if reasons:
                rollback_reason = "; ".join(reasons)
                # Roll back ONLY compaction's writes, including rooming,
                # instructor metadata and audit timestamps, preserving the
                # repaired input board exactly.
                transaction.set_rollback(True)

    candidate_relocations = list(relocations)
    if rollback_reason:
        moves_accepted = 0
        relocations = []
        cur_eval, cur_instr, cur_schedule = base_eval, base_instr, base_schedule

    # ── Audit report ──
    fin_per, fin_total_gap, fin_by_tier = student_metrics(cur_eval)
    max_add = max((fin_per.get(s, 0) - base_per.get(s, 0) for s in base_per), default=0)
    worsened = [s for s in base_per if fin_per.get(s, 0) > base_per.get(s, 0)]
    improved_students = [s for s in base_per if fin_per.get(s, 0) < base_per.get(s, 0)]
    bw, fw = defaultdict(int), defaultdict(int)
    for (iid, _d), (_h, di) in base_instr["holes"].items():
        bw[iid] += di
    for (iid, _d), (_h, di) in cur_instr["holes"].items():
        fw[iid] += di
    report.update(
        {
            "neighbourhood_version": "relocation-v1",
            "rolled_back": bool(rollback_reason),
            "rollback_reason": rollback_reason,
            "room_safety": {
                "physical_unassigned_before": rooms_before,
                "physical_unassigned_candidate": candidate_rooms,
                "physical_unassigned_after": rooms_before if rollback_reason else candidate_rooms,
                "candidate_locked_assignments_preserved": candidate_locked_preserved,
                "locked_assignments_preserved": True,
            },
            "candidate_instructor_impact": candidate_instructor_impact,
            "candidate_student_total_gap": candidate_total_gap,
            "candidate_relocations": candidate_relocations,
            "protected": {
                "feasibility_before": list(base_score[0:4]),
                "feasibility_after": list(tuple(cur_eval.lexicographic_score)[0:4]),
                "reserve_before": reserve_used_of(base_score),
                "reserve_after": reserve_used_of(tuple(cur_eval.lexicographic_score)),
            },
            "student_impact": {
                "total_gap_before": base_total_gap,
                "total_gap_after": fin_total_gap,
                "total_gap_delta": fin_total_gap - base_total_gap,
                **(
                    {
                        "gap_regular_students_only": True,
                        "total_gap_all_before": sum(base_per.values()),
                        "total_gap_all_after": sum(fin_per.values()),
                    }
                    if regular_gap_only
                    else {}
                ),
                "budget_ceiling": int(gap_ceiling),
                "max_added_gap_any_student": max_add,
                "students_worsened": len(worsened),
                "students_improved": len(improved_students),
                "tierA_gap_delta": fin_by_tier.get(RiskTier.A, 0) - base_by_tier.get(RiskTier.A, 0),
                "graduating_gap_delta": fin_by_tier.get(RiskTier.B, 0)
                - base_by_tier.get(RiskTier.B, 0),
            },
            "instructor_impact": {
                "working_days_before": base_schedule["working_days"],
                "working_days_after": cur_schedule["working_days"],
                "working_days_by_instructor_before": base_schedule["working_days_by_instructor"],
                "working_days_by_instructor_after": cur_schedule["working_days_by_instructor"],
                "idle_minutes_by_instructor_before": base_schedule["idle_minutes_by_instructor"],
                "idle_minutes_by_instructor_after": cur_schedule["idle_minutes_by_instructor"],
                "total_idle_before": base_instr["total"],
                "total_idle_after": cur_instr["total"],
                "total_idle_saved": base_instr["total"] - cur_instr["total"],
                "largest_hole_before": base_instr["largest"],
                "largest_hole_after": cur_instr["largest"],
                "over90_before": base_instr["over90"],
                "over90_after": cur_instr["over90"],
                "worst_weekly_before": base_instr["worst_weekly"],
                "worst_weekly_after": cur_instr["worst_weekly"],
                "instructors_improved": sum(1 for i in bw if fw.get(i, 0) < bw[i]),
                "instructors_worsened": sum(1 for i in bw if fw.get(i, 0) > bw[i]),
            },
            "search": {
                "moves_evaluated": moves_evaluated,
                "moves_accepted": moves_accepted,
                "candidate_moves_accepted": candidate_moves_accepted,
                "rounds_used": _round + 1,
                "residual_largest_hole": cur_instr["largest"],
                "timed_out": timed_out,
                "elapsed_seconds": round(time.monotonic() - t0, 1),
            },
            "relocations": relocations,
        }
    )
    logger.info(
        "Instructor compaction (scenario %d): %d moves, idle %d->%d min, student gap %d->%d",
        scenario_id,
        moves_accepted,
        base_instr["total"],
        cur_instr["total"],
        base_total_gap,
        fin_total_gap,
    )
    return report
