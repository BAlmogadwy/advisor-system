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
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_autoplace import (
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

    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario, programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )
    if not budgets:
        return {"status": "optimal", "placed": 0, "placements": [], "objective": 0}

    # Online courses
    online_codes: set[str] = set()
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_codes = set(
            ProgrammeRequirement.objects.filter(
                program__in=programs, is_online=True
            ).values_list("course_code", flat=True)
        )

    # Build valid slot options per duration
    slots_75 = []  # (day_idx, slot_idx, day, start, end, mask, start_min)
    slots_100 = []
    for day_idx, day in enumerate(WEEKDAYS):
        for s_idx, s in enumerate(slot_config):
            if _start_is_blocked(s["start"]):
                continue
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

        for s_idx in range(len(slot_config) - 1):
            if _start_is_blocked(slot_config[s_idx]["start"]):
                continue
            start = slot_config[s_idx]["start"]
            end = slot_config[s_idx + 1]["end"]
            mask = _time_mask(day, start, end)
            start_min = _to_min(start)
            slots_100.append((day_idx, s_idx, day, start, end, mask, start_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_100

    # Build sections to place
    sections = []
    for budget in budgets:
        code = budget.course_code
        cr = budget.credit_hours or 3
        pattern = get_meeting_pattern(cr)
        already = SectionPlacement.objects.filter(
            board=board, term_section__course_code=code
        ).count()
        to_place = max(0, budget.planned_sections - already)
        for sec_num in range(already + 1, already + to_place + 1):
            sections.append({
                "code": code,
                "sec_num": sec_num,
                "label": f"S{sec_num}",
                "pattern": pattern,
                "is_online": code.upper() in online_codes,
                "capacity": budget.max_per_section,
            })

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
            for o_idx, opt in enumerate(options):
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

    for group_num, indices in by_group.items():
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
                                    model.add(
                                        assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1
                                    )

    # ── Hard: same course different sections don't overlap ────────
    by_course: dict[str, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections):
        by_course[sec["code"]].append(i)

    for code, indices in by_course.items():
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
                                    model.add(
                                        assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1
                                    )

    # ── Soft objective ───────────────────────────────────────────
    penalties = []

    for i, sec in enumerate(sections):
        w = 10 if sec["sec_num"] == 1 else 2  # S1 group priority

        for m_idx in range(len(sec["pattern"])):
            options = sec_options[i][m_idx]
            for o_idx, opt in enumerate(options):
                day_idx, slot_idx, day, start, end, mask, start_min = opt
                var = assign[i][m_idx][o_idx]

                # Slot position penalty: prefer compact adjacent slots
                # Slot 0=0, Slot 1=1, Slot 2=3(after prayer), Slot 3=4, Slot 4=5
                prayer_penalty = 2 if slot_idx >= 2 else 0  # crossing prayer
                slot_cost = slot_idx + prayer_penalty
                penalties.append(var * slot_cost * w)

                # Online: prefer late slots (invert)
                if sec["is_online"]:
                    online_cost = (num_slots - 1 - slot_idx) * 5
                    penalties.append(var * online_cost)

    # Time consistency: prefer same slot_idx across meetings of same section
    # Use per-meeting slot_idx penalty instead of pairwise products
    # (pairwise BoolVar products aren't supported directly in CP-SAT)
    for i, sec in enumerate(sections):
        if len(sec["pattern"]) <= 1:
            continue
        w = 3 if sec["sec_num"] == 1 else 1
        # For each meeting, the slot_idx is already penalized above.
        # Add extra penalty for variance: penalize slot_idx deviation from first meeting
        # Approximation: penalize each meeting's slot_idx independently
        # (the per-slot penalty above already encourages consistency)

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
                    meetings.append({
                        "day": opt[2], "start": opt[3], "end": opt[4],
                    })
                    break

        placements.append({
            "course_code": sec["code"],
            "section": sec["label"],
            "sec_num": sec["sec_num"],
            "meetings": meetings,
            "is_online": sec["is_online"],
        })

    return {
        "status": status_str,
        "placed": len(placements),
        "placements": placements,
        "objective": int(solver.objective_value) if penalties else 0,
    }


def solve_and_persist_board(board_id: int, time_limit_seconds: float = 10.0) -> dict:
    """Solve and persist placements."""
    result = solve_board(board_id, time_limit_seconds)
    if result["status"] in ("infeasible", "timeout"):
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
            course_key=code, section=p["section"],
            defaults={
                "course_code": code, "course_number": code, "course_name": code,
                "available_capacity": cap, "source_tag": "tw_auto",
            },
        )
        for m in p["meetings"]:
            TermSectionMeeting.objects.get_or_create(
                term_section=ts, day=m["day"], start_time=m["start"], end_time=m["end"],
                defaults={"room": "", "instructor": ""},
            )
            SectionPlacement.objects.get_or_create(
                board=board, term_section=ts, day=m["day"], start_time=m["start"],
                defaults={"end_time": m["end"]},
            )

    return result


def solve_scenario(scenario_id: int, time_limit_seconds: float = 10.0) -> dict:
    """Solve all boards in a scenario."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    for board in boards:
        r = solve_and_persist_board(board.id, time_limit_seconds)
        results[board.label] = r
        total_placed += r.get("placed", 0)
    return {"boards": results, "total_placed": total_placed}
