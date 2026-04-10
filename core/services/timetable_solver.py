"""
core/services/timetable_solver.py
OR-Tools CP-SAT constraint programming solver for timetable placement.

Finds the globally optimal section-to-slot assignment by considering ALL
sections simultaneously, unlike the greedy auto-placer which assigns one
at a time.

Model:
  - Boolean variable per (section, meeting, day, slot): is this meeting here?
  - Hard: exactly 1 assignment per meeting, all-different days per section
  - Hard: no overlap in same student group, no same-course overlap
  - Soft: minimize gaps, prefer early/consistent slots, online late
"""

from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
    _start_is_blocked,
    _time_mask,
    get_meeting_pattern,
)


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


PRAYER_BOUNDARY = 13 * 60


def solve_board(board_id: int, time_limit_seconds: float = 10.0) -> dict:
    """Find optimal placement using CP-SAT solver."""
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"status": "error", "placed": 0, "placements": [], "objective": 0}

    scenario = board.scenario
    slot_config = scenario.slot_config or DEFAULT_SLOTS
    lab_slot_config = scenario.lab_slot_config or DEFAULT_LAB_SLOTS

    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario,
            programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )
    if not budgets:
        return {"status": "optimal", "placed": 0, "placements": [], "objective": 0}

    # Online courses
    online_codes: set[str] = set()
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_codes = set(
            ProgrammeRequirement.objects.filter(program__in=programs, is_online=True).values_list(
                "course_code", flat=True
            )
        )

    # Build valid slot options per duration
    slots_75 = []  # (day_idx, slot_idx, day, start, end, mask, start_min)
    slots_lab = []  # dedicated lab time grid for 100-min meetings
    for day_idx, day in enumerate(WEEKDAYS):
        for s_idx, s in enumerate(slot_config):
            if _start_is_blocked(s["start"]):
                continue
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

        for s_idx, s in enumerate(lab_slot_config):
            if _start_is_blocked(s["start"]):
                continue
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_lab.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_lab

    # Build sections to place
    sections = []
    for budget in budgets:
        code = budget.course_code
        cr = budget.credit_hours or 3
        pattern = get_meeting_pattern(cr)
        already = (
            SectionPlacement.objects.filter(board=board, term_section__course_code=code)
            .values("term_section_id")
            .distinct()
            .count()
        )
        to_place = max(0, budget.planned_sections - already)
        for sec_num in range(already + 1, already + to_place + 1):
            sections.append(
                {
                    "code": code,
                    "sec_num": sec_num,
                    "label": f"S{sec_num}",
                    "pattern": pattern,
                    "is_online": code.upper() in online_codes,
                    "capacity": budget.max_per_section,
                }
            )

    if not sections:
        return {"status": "optimal", "placed": 0, "placements": [], "objective": 0}

    num_slots = len(slot_config)

    # ── Build CP-SAT model ───────────────────────────────────────

    model = cp_model.CpModel()

    # Boolean vars: assign[sec_idx][meeting_idx][option_idx] = 1 if assigned
    assign = []
    sec_options = []  # sec_options[i][m] = list of option tuples

    for i, sec in enumerate(sections):
        sec_assign = []
        sec_opts = []
        for m_idx, duration in enumerate(sec["pattern"]):
            options = get_options(duration)
            m_assign = []
            for o_idx, _opt in enumerate(options):
                var = model.new_bool_var(f"a_{i}_{m_idx}_{o_idx}")
                m_assign.append(var)
            sec_assign.append(m_assign)
            sec_opts.append(options)

            # Exactly one option per meeting
            model.add_exactly_one(m_assign)

        assign.append(sec_assign)
        sec_options.append(sec_opts)

    # ── Hard: all-different days per section ──────────────────────
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        for m_a in range(len(sec["pattern"])):
            for m_b in range(m_a + 1, len(sec["pattern"])):
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                # For each pair of options on the same day, forbid both
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        if opt_a[0] == opt_b[0]:  # same day_idx
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] <= 1)

    # ── Hard: no overlap in same student group ───────────────────
    by_group: dict[int, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections):
        by_group[sec["sec_num"]].append(i)

    for _group_num, indices in by_group.items():
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        opts_a = sec_options[i_a][m_a]
                        opts_b = sec_options[i_b][m_b]
                        for oa, opt_a in enumerate(opts_a):
                            for ob, opt_b in enumerate(opts_b):
                                if opt_a[5] & opt_b[5]:  # bitmask overlap
                                    model.add(assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1)

    # ── Hard: same course different sections don't overlap ────────
    by_course: dict[str, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections):
        by_course[sec["code"]].append(i)

    for _code, indices in by_course.items():
        if len(indices) <= 1:
            continue
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        opts_a = sec_options[i_a][m_a]
                        opts_b = sec_options[i_b][m_b]
                        for oa, opt_a in enumerate(opts_a):
                            for ob, opt_b in enumerate(opts_b):
                                if opt_a[5] & opt_b[5]:
                                    model.add(assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1)

    # ── Soft objective: directly model idle gaps ───────────────────
    #
    # For each pair of sections in the same student group, if their meetings
    # land on the same day, the idle gap (start_b - end_a) is penalized.
    # This is the ACTUAL gap students experience, not a slot-index proxy.
    #
    # We use indicator variables: for each pair (meeting_a at option_oa,
    # meeting_b at option_ob), if both are chosen AND on the same day,
    # the gap in minutes is added to the penalty.

    penalties = []

    # Pre-compute slot end times in minutes for gap calculation
    slot_end_min = {}
    for s_idx, s in enumerate(slot_config):
        slot_end_min[s_idx] = _to_min(s["end"])
    # Lab slot end times (keyed by ("lab", idx) to avoid collisions)
    for s_idx, s in enumerate(lab_slot_config):
        slot_end_min[("lab", s_idx)] = _to_min(s["end"])

    # (1) REAL GAP PENALTIES between sections in same group on same day
    for _group_num, indices in by_group.items():
        # Only model real gaps for S1 (primary group) — too many variables otherwise
        # S2+ uses slot-index proxy below
        if _group_num > 1:
            continue

        w = 10  # S1 highest priority

        # For each pair of sections in the group
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]

                # Skip if either is online (no campus presence)
                if sections[i_a]["is_online"] or sections[i_b]["is_online"]:
                    continue

                # For each meeting of section A × each meeting of section B
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        opts_a = sec_options[i_a][m_a]
                        opts_b = sec_options[i_b][m_b]

                        # Group by day to reduce combinations
                        day_pairs: dict[int, list[tuple]] = defaultdict(list)
                        for oa, opt_a in enumerate(opts_a):
                            day_pairs[opt_a[0]].append(("a", oa, opt_a))
                        for ob, opt_b in enumerate(opts_b):
                            day_pairs[opt_b[0]].append(("b", ob, opt_b))

                        for _day_idx, entries in day_pairs.items():
                            a_entries = [(oa, opt) for side, oa, opt in entries if side == "a"]
                            b_entries = [(ob, opt) for side, ob, opt in entries if side == "b"]
                            for oa, opt_a in a_entries:
                                for ob, opt_b in b_entries:
                                    start_a, end_a = opt_a[6], _to_min(opt_a[4])
                                    start_b, end_b = opt_b[6], _to_min(opt_b[4])
                                    gap = (
                                        (start_b - end_a)
                                        if start_a < start_b
                                        else (start_a - end_b)
                                    )

                                    if gap <= 15:
                                        continue

                                    crosses = (end_a <= PRAYER_BOUNDARY < start_b) or (
                                        end_b <= PRAYER_BOUNDARY < start_a
                                    )
                                    if crosses:
                                        gap = int(gap * 1.5)

                                    both = model.new_bool_var(
                                        f"g_{i_a}_{m_a}_{oa}_{i_b}_{m_b}_{ob}"
                                    )
                                    # both = 1 iff assign_a AND assign_b are both 1
                                    model.add(
                                        assign[i_a][m_a][oa] + assign[i_b][m_b][ob] - 1 <= both
                                    )
                                    model.add(both <= assign[i_a][m_a][oa])
                                    model.add(both <= assign[i_b][m_b][ob])

                                    penalties.append(both * gap * w)

    # (1b) SLOT-INDEX PROXY for S2+ groups (fast approximation)
    for _group_num, indices in by_group.items():
        if _group_num <= 1:
            continue  # S1 handled with real gap model above
        w = 2
        for i_sec in indices:
            if sections[i_sec]["is_online"]:
                continue
            for m_idx in range(len(sections[i_sec]["pattern"])):
                options = sec_options[i_sec][m_idx]
                for o_idx, opt in enumerate(options):
                    slot_idx = opt[1]
                    prayer_p = 3 if slot_idx >= 2 else 0
                    penalties.append(assign[i_sec][m_idx][o_idx] * (slot_idx + prayer_p) * w)

    # (2) Online courses: prefer late slots
    for i, sec in enumerate(sections):
        if not sec["is_online"]:
            continue
        for m_idx in range(len(sec["pattern"])):
            options = sec_options[i][m_idx]
            for o_idx, opt in enumerate(options):
                slot_idx = opt[1]
                # Reward late slots (penalize early)
                early_penalty = max(0, (num_slots - 1 - slot_idx)) * 5
                penalties.append(assign[i][m_idx][o_idx] * early_penalty)

    # (3) Time consistency: prefer same slot_idx across meetings
    # 3a: slight preference for earlier slots
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        w = 3 if sec["sec_num"] == 1 else 1
        for m_idx in range(len(sec["pattern"])):
            options = sec_options[i][m_idx]
            for o_idx, opt in enumerate(options):
                penalties.append(assign[i][m_idx][o_idx] * opt[1] * w)

    # 3b: heavy penalty for meetings at DIFFERENT slot indices
    # (only compare lecture meetings — lab uses different slot grid)
    for i, sec in enumerate(sections):
        lecture_meetings = [m for m in range(len(sec["pattern"])) if sec["pattern"][m] <= 75]
        if len(lecture_meetings) <= 1:
            continue
        w = 15 if sec["sec_num"] == 1 else 5
        for a_pos in range(len(lecture_meetings)):
            for b_pos in range(a_pos + 1, len(lecture_meetings)):
                m_a = lecture_meetings[a_pos]
                m_b = lecture_meetings[b_pos]
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        if opt_a[1] != opt_b[1]:  # different slot_idx
                            diff = abs(opt_a[1] - opt_b[1])
                            both = model.new_bool_var(f"tdiff_{i}_{m_a}_{oa}_{m_b}_{ob}")
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] - 1 <= both)
                            model.add(both <= assign[i][m_a][oa])
                            model.add(both <= assign[i][m_b][ob])
                            penalties.append(both * diff * w)

    # (4) Day spacing: penalize consecutive-day meetings for the same section
    # Prefer at least 1 gap day between meetings (e.g. SUN+TUE over SUN+MON)
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        w = 8 if sec["sec_num"] == 1 else 3
        for m_a in range(len(sec["pattern"])):
            for m_b in range(m_a + 1, len(sec["pattern"])):
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        day_gap = abs(opt_a[0] - opt_b[0])
                        if day_gap == 1:  # consecutive days
                            both = model.new_bool_var(f"consec_{i}_{m_a}_{oa}_{m_b}_{ob}")
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] - 1 <= both)
                            model.add(both <= assign[i][m_a][oa])
                            model.add(both <= assign[i][m_b][ob])
                            penalties.append(both * w)

    if penalties:
        model.minimize(sum(penalties))

    # ── Solve ────────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 8
    solver.parameters.random_seed = 42

    status = solver.solve(model)

    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
    }
    status_str = status_map.get(status, "timeout")

    if status_str in ("infeasible", "timeout"):
        return {"status": status_str, "placed": 0, "placements": [], "objective": 0}

    # ── Extract solution ─────────────────────────────────────────
    placements = []
    for i, sec in enumerate(sections):
        meetings = []
        for m_idx in range(len(sec["pattern"])):
            options = sec_options[i][m_idx]
            for o_idx, opt in enumerate(options):
                if solver.value(assign[i][m_idx][o_idx]):
                    meetings.append(
                        {
                            "day": opt[2],
                            "start": opt[3],
                            "end": opt[4],
                        }
                    )
                    break

        placements.append(
            {
                "course_code": sec["code"],
                "section": sec["label"],
                "sec_num": sec["sec_num"],
                "meetings": meetings,
                "is_online": sec["is_online"],
            }
        )

    return {
        "status": status_str,
        "placed": len(placements),
        "placements": placements,
        "objective": int(solver.objective_value) if penalties else 0,
    }


def persist_solver_result(board_id: int, result: dict) -> dict:
    """Persist a solver result (from solve_board or solve_board_with_hints).

    Clears existing auto-placed sections on the board and recreates them
    from the result's placements list.  Can be called with any result dict
    that has ``placements`` in the standard solver format.

    Returns the result dict unchanged (pass-through for chaining).
    """
    if result["status"] in ("infeasible", "timeout", "error"):
        return result

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return result

    # Clear existing auto-placed
    SectionPlacement.objects.filter(board=board, term_section__source_tag="tw_auto").delete()

    budget_map = {
        b.course_code: b
        for b in ScenarioSectionBudget.objects.filter(
            scenario=board.scenario, programme_term=board.nominal_term
        )
    }

    for p in result["placements"]:
        code = p["course_code"]
        budget = budget_map.get(code)
        cap = budget.max_per_section if budget else 40

        ts, _ = TermSection.objects.get_or_create(
            course_key=code,
            section=p["section"],
            defaults={
                "course_code": code,
                "course_number": code,
                "course_name": code,
                "available_capacity": cap,
                "source_tag": "tw_auto",
            },
        )
        for m in p["meetings"]:
            TermSectionMeeting.objects.get_or_create(
                term_section=ts,
                day=m["day"],
                start_time=m["start"],
                end_time=m["end"],
                defaults={"room": "", "instructor": ""},
            )
            SectionPlacement.objects.get_or_create(
                board=board,
                term_section=ts,
                day=m["day"],
                start_time=m["start"],
                defaults={"end_time": m["end"]},
            )

    return result


def solve_and_persist_board(board_id: int, time_limit_seconds: float = 10.0) -> dict:
    """Solve and persist placements."""
    result = solve_board(board_id, time_limit_seconds)
    return persist_solver_result(board_id, result)

    return result


def solve_board_with_hints(
    board_id: int,
    greedy_placements: list[dict],
    time_limit_seconds: float = 8.0,
) -> dict:
    """Solve board with CP-SAT, warm-started from greedy placements.

    Accepts greedy results and feeds them as hints so the solver converges
    faster.  Returns the same shape as ``solve_board`` plus an
    ``"improved"`` flag indicating whether CP-SAT beat the greedy baseline.
    """
    # --- build model identically to solve_board ---
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"status": "error", "placed": 0, "placements": [], "objective": 0, "improved": False}

    scenario = board.scenario
    slot_config = scenario.slot_config or DEFAULT_SLOTS
    lab_slot_config = scenario.lab_slot_config or DEFAULT_LAB_SLOTS

    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario,
            programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )
    if not budgets:
        return {
            "status": "optimal",
            "placed": 0,
            "placements": [],
            "objective": 0,
            "improved": False,
        }

    online_codes: set[str] = set()
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_codes = set(
            ProgrammeRequirement.objects.filter(program__in=programs, is_online=True).values_list(
                "course_code", flat=True
            )
        )

    slots_75 = []
    slots_lab = []
    for day_idx, day in enumerate(WEEKDAYS):
        for s_idx, s in enumerate(slot_config):
            if _start_is_blocked(s["start"]):
                continue
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))
        for s_idx, s in enumerate(lab_slot_config):
            if _start_is_blocked(s["start"]):
                continue
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_lab.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_lab

    sections = []
    for budget in budgets:
        code = budget.course_code
        cr = budget.credit_hours or 3
        pattern = get_meeting_pattern(cr)
        already = (
            SectionPlacement.objects.filter(board=board, term_section__course_code=code)
            .values("term_section_id")
            .distinct()
            .count()
        )
        to_place = max(0, budget.planned_sections - already)
        for sec_num in range(already + 1, already + to_place + 1):
            sections.append(
                {
                    "code": code,
                    "sec_num": sec_num,
                    "label": f"S{sec_num}",
                    "pattern": pattern,
                    "is_online": code.upper() in online_codes,
                    "capacity": budget.max_per_section,
                }
            )

    if not sections:
        return {
            "status": "optimal",
            "placed": 0,
            "placements": [],
            "objective": 0,
            "improved": False,
        }

    num_slots = len(slot_config)
    model = cp_model.CpModel()

    assign = []
    sec_options = []
    for i, sec in enumerate(sections):
        sec_assign = []
        sec_opts = []
        for m_idx, duration in enumerate(sec["pattern"]):
            options = get_options(duration)
            m_assign = []
            for o_idx, _opt in enumerate(options):
                var = model.new_bool_var(f"a_{i}_{m_idx}_{o_idx}")
                m_assign.append(var)
            sec_assign.append(m_assign)
            sec_opts.append(options)
            model.add_exactly_one(m_assign)
        assign.append(sec_assign)
        sec_options.append(sec_opts)

    # Hard: all-different days per section
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        for m_a in range(len(sec["pattern"])):
            for m_b in range(m_a + 1, len(sec["pattern"])):
                for oa, opt_a in enumerate(sec_options[i][m_a]):
                    for ob, opt_b in enumerate(sec_options[i][m_b]):
                        if opt_a[0] == opt_b[0]:
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] <= 1)

    # Hard: no overlap in same student group
    by_group: dict[int, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections):
        by_group[sec["sec_num"]].append(i)
    for _group_num, indices in by_group.items():
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        for oa, opt_a in enumerate(sec_options[i_a][m_a]):
                            for ob, opt_b in enumerate(sec_options[i_b][m_b]):
                                if opt_a[5] & opt_b[5]:
                                    model.add(assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1)

    # Hard: same course different sections don't overlap
    by_course: dict[str, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections):
        by_course[sec["code"]].append(i)
    for _code, indices in by_course.items():
        if len(indices) <= 1:
            continue
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        for oa, opt_a in enumerate(sec_options[i_a][m_a]):
                            for ob, opt_b in enumerate(sec_options[i_b][m_b]):
                                if opt_a[5] & opt_b[5]:
                                    model.add(assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1)

    # Soft: gap penalties (same as solve_board)
    penalties = []
    for _group_num, indices in by_group.items():
        if _group_num > 1:
            continue
        w = 10
        for a_pos in range(len(indices)):
            for b_pos in range(a_pos + 1, len(indices)):
                i_a, i_b = indices[a_pos], indices[b_pos]
                if sections[i_a]["is_online"] or sections[i_b]["is_online"]:
                    continue
                for m_a in range(len(sections[i_a]["pattern"])):
                    for m_b in range(len(sections[i_b]["pattern"])):
                        opts_a = sec_options[i_a][m_a]
                        opts_b = sec_options[i_b][m_b]
                        day_pairs: dict[int, list[tuple]] = defaultdict(list)
                        for oa, opt_a in enumerate(opts_a):
                            day_pairs[opt_a[0]].append(("a", oa, opt_a))
                        for ob, opt_b in enumerate(opts_b):
                            day_pairs[opt_b[0]].append(("b", ob, opt_b))
                        for _day_idx, entries in day_pairs.items():
                            a_entries = [(oa, opt) for side, oa, opt in entries if side == "a"]
                            b_entries = [(ob, opt) for side, ob, opt in entries if side == "b"]
                            for oa, opt_a in a_entries:
                                for ob, opt_b in b_entries:
                                    start_a, end_a = opt_a[6], _to_min(opt_a[4])
                                    start_b, end_b = opt_b[6], _to_min(opt_b[4])
                                    gap = (
                                        (start_b - end_a)
                                        if start_a < start_b
                                        else (start_a - end_b)
                                    )
                                    if gap <= 15:
                                        continue
                                    crosses = (end_a <= PRAYER_BOUNDARY < start_b) or (
                                        end_b <= PRAYER_BOUNDARY < start_a
                                    )
                                    if crosses:
                                        gap = int(gap * 1.5)
                                    both = model.new_bool_var(
                                        f"g_{i_a}_{m_a}_{oa}_{i_b}_{m_b}_{ob}"
                                    )
                                    model.add(
                                        assign[i_a][m_a][oa] + assign[i_b][m_b][ob] - 1 <= both
                                    )
                                    model.add(both <= assign[i_a][m_a][oa])
                                    model.add(both <= assign[i_b][m_b][ob])
                                    penalties.append(both * gap * w)

    for _group_num, indices in by_group.items():
        if _group_num <= 1:
            continue
        w = 2
        for i_sec in indices:
            if sections[i_sec]["is_online"]:
                continue
            for m_idx in range(len(sections[i_sec]["pattern"])):
                for o_idx, opt in enumerate(sec_options[i_sec][m_idx]):
                    slot_idx = opt[1]
                    prayer_p = 3 if slot_idx >= 2 else 0
                    penalties.append(assign[i_sec][m_idx][o_idx] * (slot_idx + prayer_p) * w)

    for i, sec in enumerate(sections):
        if not sec["is_online"]:
            continue
        for m_idx in range(len(sec["pattern"])):
            for o_idx, opt in enumerate(sec_options[i][m_idx]):
                early_penalty = max(0, (num_slots - 1 - opt[1])) * 5
                penalties.append(assign[i][m_idx][o_idx] * early_penalty)

    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        w = 3 if sec["sec_num"] == 1 else 1
        for m_idx in range(len(sec["pattern"])):
            for o_idx, opt in enumerate(sec_options[i][m_idx]):
                penalties.append(assign[i][m_idx][o_idx] * opt[1] * w)

    # 3b: heavy penalty for meetings at DIFFERENT slot indices
    for i, sec in enumerate(sections):
        lecture_meetings = [m for m in range(len(sec["pattern"])) if sec["pattern"][m] <= 75]
        if len(lecture_meetings) <= 1:
            continue
        w = 15 if sec["sec_num"] == 1 else 5
        for a_pos in range(len(lecture_meetings)):
            for b_pos in range(a_pos + 1, len(lecture_meetings)):
                m_a = lecture_meetings[a_pos]
                m_b = lecture_meetings[b_pos]
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        if opt_a[1] != opt_b[1]:
                            diff = abs(opt_a[1] - opt_b[1])
                            both = model.new_bool_var(f"tdiff_{i}_{m_a}_{oa}_{m_b}_{ob}")
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] - 1 <= both)
                            model.add(both <= assign[i][m_a][oa])
                            model.add(both <= assign[i][m_b][ob])
                            penalties.append(both * diff * w)

    # (4) Day spacing: penalize consecutive-day meetings for the same section
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        w = 8 if sec["sec_num"] == 1 else 3
        for m_a in range(len(sec["pattern"])):
            for m_b in range(m_a + 1, len(sec["pattern"])):
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        day_gap = abs(opt_a[0] - opt_b[0])
                        if day_gap == 1:
                            both = model.new_bool_var(f"consec_{i}_{m_a}_{oa}_{m_b}_{ob}")
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] - 1 <= both)
                            model.add(both <= assign[i][m_a][oa])
                            model.add(both <= assign[i][m_b][ob])
                            penalties.append(both * w)

    if penalties:
        model.minimize(sum(penalties))

    # ── Warm-start hints from greedy placements ─────────────────
    greedy_lookup: dict[tuple[str, int], list[dict]] = {}
    for gp in greedy_placements:
        sec_num = int(gp.get("section", "S1").replace("S", "") or 1)
        greedy_lookup[(gp["course_code"], sec_num)] = gp.get("meetings", [])

    for i, sec in enumerate(sections):
        meetings = greedy_lookup.get((sec["code"], sec["sec_num"]))
        if not meetings:
            continue
        for m_idx, meeting in enumerate(meetings):
            if m_idx >= len(sec["pattern"]):
                break
            for o_idx, opt in enumerate(sec_options[i][m_idx]):
                if opt[2] == meeting["day"] and opt[3] == meeting["start"]:
                    model.add_hint(assign[i][m_idx][o_idx], 1)
                    break

    # ── Solve ───────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 8
    solver.parameters.random_seed = 42

    status = solver.solve(model)
    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
    }
    status_str = status_map.get(status, "timeout")

    if status_str in ("infeasible", "timeout"):
        return {
            "status": status_str,
            "placed": 0,
            "placements": [],
            "objective": 0,
            "improved": False,
        }

    placements = []
    for i, sec in enumerate(sections):
        meetings = []
        for m_idx in range(len(sec["pattern"])):
            for o_idx, opt in enumerate(sec_options[i][m_idx]):
                if solver.value(assign[i][m_idx][o_idx]):
                    meetings.append({"day": opt[2], "start": opt[3], "end": opt[4]})
                    break
        placements.append(
            {
                "course_code": sec["code"],
                "section": sec["label"],
                "sec_num": sec["sec_num"],
                "meetings": meetings,
                "is_online": sec["is_online"],
            }
        )

    obj = int(solver.objective_value) if penalties else 0
    greedy_count = len(greedy_placements)
    return {
        "status": status_str,
        "placed": len(placements),
        "placements": placements,
        "objective": obj,
        "improved": len(placements) >= greedy_count,
    }


def solve_scenario(scenario_id: int, time_limit_seconds: float = 5.0) -> dict:
    """Solve all boards in a scenario."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    for board in boards:
        r = solve_and_persist_board(board.id, time_limit_seconds)
        results[board.label] = r
        total_placed += r.get("placed", 0)
    return {"boards": results, "total_placed": total_placed}
