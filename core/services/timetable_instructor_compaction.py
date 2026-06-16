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
- **Instructor objective** (lexicographic, lower=better):
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

from django.db import transaction

from core.services.timetable_assignment_models import RiskTier, SectionMeeting
from core.services.timetable_pr4_instructor import (
    get_instructor_compaction_config,
    get_instructor_daily_cap,
    is_instructor_compaction_enabled,
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

    from core.models import SectionPlacement, TermSectionMeeting, TimetableScenario
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_locked_section_ids_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_rooming import assign_rooms_to_board
    from core.services.timetable_validation import blocked_slot_keys

    cfg = get_instructor_compaction_config()
    cap = get_instructor_daily_cap()

    scenario = TimetableScenario.objects.get(id=scenario_id)
    states = build_section_states_for_scenario(scenario_id)
    if not states:
        report["note"] = "no placements"
        return report
    sbi = {s.section_id: s for s in states}
    # Empty profiles are fine: the student gates simply become vacuous and the
    # pass optimises instructor idle freely (still respecting the daily cap).
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id) if profiles else {}
    cap_map = build_section_instructor_map_for_scenario(scenario_id)
    if not cap_map:
        report["note"] = "no instructor assignments (links off?)"
        return report
    locked = build_locked_section_ids_for_scenario(scenario_id)
    tier = {sid: p.risk_tier for sid, p in profiles.items()}
    code_of = {sid: s.course_code for sid, s in sbi.items()}

    lec = [(_to_min(s["start"]), _to_min(s["end"])) for s in (scenario.slot_config or [])]
    lab = [(_to_min(s["start"]), _to_min(s["end"])) for s in (scenario.lab_slot_config or [])]
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

    placement_of: dict[tuple[str, int, int], object] = {}
    sid2board: dict[str, object] = {}
    sid2ts: dict[str, object] = {}
    for p in placements:
        sid = _sid(p)
        di = WEEKDAYS.index(p.day.upper()) if p.day.upper() in WEEKDAYS else -1
        placement_of[(sid, di, _to_min(p.start_time))] = p
        sid2board.setdefault(sid, p.board)
        sid2ts.setdefault(sid, p.term_section)

    # ── Metrics ──
    def evaluate():
        return evaluate_generated_timetable_candidate(
            "compaction", states, profiles, rigidity, section_instructor_ids=cap_map
        )

    def student_metrics(res):
        per = {sid: st.total_gap_minutes for sid, st in res.assignment_states.items()}
        total = sum(per.values())
        by_tier: dict = defaultdict(int)
        for s, g in per.items():
            by_tier[tier.get(s)] += g
        return per, total, by_tier

    def instr_metrics():
        byday: dict = defaultdict(list)
        for sid, instrs in cap_map.items():
            s = sbi.get(sid)
            if not s:
                continue
            for iid in instrs:
                for m in s.meetings:
                    byday[(iid, m.day)].append((m.start_min, m.end_min))
        largest = over90 = total = 0
        weekly: dict = defaultdict(int)
        holes: dict = {}
        for key, sess in byday.items():
            sess = sorted(set(sess))
            gaps = [g for g in (sess[i + 1][0] - sess[i][1] for i in range(len(sess) - 1)) if g > 0]
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
    orig_pos = {
        sid: [(m.day, m.start_min, m.end_min) for m in s.meetings] for sid, s in sbi.items()
    }

    gap_ceiling = base_total_gap * (1.0 + cfg["gap_budget"])

    def gates_ok(res, idle_saved, gap_added):
        sc = tuple(res.lexicographic_score)
        if sc[0:4] > base_score[0:4] or sc[5] > base_score[5]:
            return False
        per, total, by_tier = student_metrics(res)
        if total > gap_ceiling:
            return False
        if by_tier.get(RiskTier.A, 0) > base_by_tier.get(RiskTier.A, 0):
            return False
        if by_tier.get(RiskTier.B, 0) > base_by_tier.get(RiskTier.B, 0):
            return False
        for s, g in per.items():
            if g - base_per.get(s, 0) > cfg["per_student_cap"]:
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

    cur_instr = base_instr
    cur_eval = base_eval
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

    timed_out = False
    _round = 0
    for _round in range(cfg["max_rounds"]):
        if moves_accepted >= max_moves or _over_budget():
            break
        worst_days = sorted(cur_instr["holes"].items(), key=lambda kv: -kv[1][0])
        target_iids = list(dict.fromkeys(iid for (iid, _d), (h, _di) in worst_days if h > 0))[:3]
        if not target_iids:
            break
        _, cur_gap, _ = student_metrics(cur_eval)
        cur_tuple = ituple(cur_instr, cur_gap - base_total_gap)
        best = None
        for sid, midx in movable:
            iids = cap_map.get(sid, frozenset())
            if not (set(iids) & set(target_iids)):
                continue
            for gen in _NEIGHBOURHOODS:
                for day2, s2, e2 in gen(sid, midx, iids):
                    old = _apply(sid, midx, day2, s2, e2, iids)
                    sig = _signature()
                    if sig not in visited:
                        res = evaluate()
                        moves_evaluated += 1
                        im = instr_metrics()
                        _, tg, _ = student_metrics(res)
                        idle_saved = cur_instr["total"] - im["total"]
                        gap_added = tg - cur_gap
                        if gates_ok(res, idle_saved, gap_added):
                            it = ituple(im, tg - base_total_gap)
                            if it < cur_tuple and (best is None or it < best[0]):
                                best = (it, sid, midx, day2, s2, e2, res, im, sig)
                    _revert(sid, midx, old, day2, s2, e2, iids)
                    if _over_budget():
                        timed_out = True
                        break
                if timed_out:
                    break
            if timed_out:
                break
        if best is None:
            break
        _, sid, midx, day2, s2, e2, res, im, sig = best
        _apply(sid, midx, day2, s2, e2, cap_map.get(sid, frozenset()))
        visited.add(sig)
        cur_instr, cur_eval = im, res
        moves_accepted += 1

    # ── Persist accepted relocations ──
    touched_boards: set = set()
    relocations: list = []
    with transaction.atomic():
        for sid, s in sbi.items():
            for i, m in enumerate(s.meetings):
                od, os_, oe = orig_pos[sid][i]
                if (m.day, m.start_min, m.end_min) == (od, os_, oe):
                    continue
                p = placement_of.get((sid, od, os_))
                if p is None:
                    continue
                new_day, new_start, new_end = WEEKDAYS[m.day], _hhmm(m.start_min), _hhmm(m.end_min)
                TermSectionMeeting.objects.filter(
                    term_section=p.term_section, day=p.day, start_time=p.start_time
                ).update(day=new_day, start_time=new_start, end_time=new_end)
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
                    sid2ts[sid], scenario, sid2board[sid], sid2ts[sid].course_code
                )
        for board_id in touched_boards:
            assign_rooms_to_board(board_id, respect_locked=True)

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
            "protected": {
                "feasibility_before": list(base_score[0:4]),
                "feasibility_after": list(tuple(cur_eval.lexicographic_score)[0:4]),
                "reserve_before": base_score[5],
                "reserve_after": tuple(cur_eval.lexicographic_score)[5],
            },
            "student_impact": {
                "total_gap_before": base_total_gap,
                "total_gap_after": fin_total_gap,
                "total_gap_delta": fin_total_gap - base_total_gap,
                "budget_ceiling": int(gap_ceiling),
                "max_added_gap_any_student": max_add,
                "students_worsened": len(worsened),
                "students_improved": len(improved_students),
                "tierA_gap_delta": fin_by_tier.get(RiskTier.A, 0) - base_by_tier.get(RiskTier.A, 0),
                "graduating_gap_delta": fin_by_tier.get(RiskTier.B, 0)
                - base_by_tier.get(RiskTier.B, 0),
            },
            "instructor_impact": {
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
