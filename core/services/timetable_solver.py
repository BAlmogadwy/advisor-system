"""
core/services/timetable_solver.py
OR-Tools CP-SAT constraint programming solver for timetable placement.

Finds the globally optimal section-to-slot assignment by considering ALL
sections simultaneously, unlike the greedy auto-placer which assigns one
at a time.

Model:
  - Boolean variable per (section, meeting, day, slot): is this meeting here?
  - Hard: exactly 1 assignment per meeting, all-different days per section
  - Hard: no same-course overlap (instructor double-booking)
  - Soft: cross-course student overlap weighted by shared_student_count
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
    _time_mask,
    get_meeting_pattern,
)
from core.services.timetable_same_course import (
    SAME_COURSE_DIFFERENT_DAY_PENALTY,
    SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY,
    SAME_COURSE_OVERLAP_PENALTY,
    is_back_to_back_gap,
    same_day_gap_penalty,
)


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


PRAYER_BOUNDARY = 13 * 60


def _option_end_min(option: tuple) -> int:
    return option[7] if len(option) > 7 else _to_min(option[4])


def _same_course_option_penalty(option_a: tuple, option_b: tuple) -> int:
    if option_a[0] != option_b[0]:
        return SAME_COURSE_DIFFERENT_DAY_PENALTY
    start_a, end_a = option_a[6], _option_end_min(option_a)
    start_b, end_b = option_b[6], _option_end_min(option_b)
    if start_a < end_b and start_b < end_a:
        return SAME_COURSE_OVERLAP_PENALTY
    gap = start_b - end_a if end_a <= start_b else start_a - end_b
    return 0 if is_back_to_back_gap(gap) else same_day_gap_penalty(gap)


def _add_same_course_adjacency_penalties(
    model: cp_model.CpModel,
    penalties: list,
    assign: list,
    sec_options: list,
    sections: list[dict],
) -> None:
    by_course: dict[str, list[int]] = defaultdict(list)
    for idx, sec in enumerate(sections):
        by_course[sec["code"]].append(idx)

    for _code, indices in by_course.items():
        if len(indices) < 2:
            continue
        pair_has_adjacent_term = []
        for pos_a in range(len(indices)):
            for pos_b in range(pos_a + 1, len(indices)):
                i_a, i_b = indices[pos_a], indices[pos_b]
                adjacent_terms = []
                for m_a in range(len(sec_options[i_a])):
                    for m_b in range(len(sec_options[i_b])):
                        for o_a, opt_a in enumerate(sec_options[i_a][m_a]):
                            for o_b, opt_b in enumerate(sec_options[i_b][m_b]):
                                weight = _same_course_option_penalty(opt_a, opt_b)
                                both = model.new_bool_var(
                                    f"same_course_{i_a}_{m_a}_{o_a}_{i_b}_{m_b}_{o_b}"
                                )
                                model.add(assign[i_a][m_a][o_a] + assign[i_b][m_b][o_b] - 1 <= both)
                                model.add(both <= assign[i_a][m_a][o_a])
                                model.add(both <= assign[i_b][m_b][o_b])
                                if weight:
                                    penalties.append(both * weight)
                                else:
                                    adjacent_terms.append(both)
                if adjacent_terms:
                    pair_adjacent = model.new_bool_var(f"same_course_pair_adjacent_{i_a}_{i_b}")
                    model.add_max_equality(pair_adjacent, adjacent_terms)
                    pair_has_adjacent_term.append(pair_adjacent)

        if pair_has_adjacent_term:
            has_required_pair = model.new_bool_var(f"same_course_required_pair_{_code}")
            model.add_max_equality(has_required_pair, pair_has_adjacent_term)
            penalties.append((1 - has_required_pair) * SAME_COURSE_MISSING_ADJACENT_PAIR_PENALTY)


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
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

        for s_idx, s in enumerate(lab_slot_config):
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_lab.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_lab

    # Build sections to place
    sections = []
    for budget in budgets:
        code = budget.course_key or budget.course_code
        display_code = budget.course_code
        cr = budget.credit_hours or 3
        pattern = get_meeting_pattern(cr)
        already = (
            SectionPlacement.objects.filter(board=board, term_section__course_key=code)
            .values("term_section_id")
            .distinct()
            .count()
        )
        to_place = max(0, budget.planned_sections - already)
        for sec_num in range(already + 1, already + to_place + 1):
            sections.append(
                {
                    "code": code,
                    "display_code": display_code,
                    "course_name": budget.course_name or display_code,
                    "sec_num": sec_num,
                    "label": f"S{sec_num}",
                    "pattern": pattern,
                    "is_online": display_code.upper() in online_codes,
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

    # ── Build overlap matrix (used for soft penalties, NOT hard constraints) ──
    from core.services.timetable_overlap import (
        build_overlap_matrix as _bom,
    )
    from core.services.timetable_overlap import (
        shared_student_count as _ssc,
    )

    board_courses = {sec["code"] for sec in sections}
    overlap_matrix = _bom(scenario.id, board_courses)

    # NOTE: Student overlap between different courses is SOFT, not hard.
    # Only same-course overlap (below) and instructor overlap are hard.
    # The soft student-overlap penalty is added in the objective section.

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
    _add_same_course_adjacency_penalties(model, penalties, assign, sec_options, sections)

    # Pre-compute slot end times in minutes for gap calculation
    slot_end_min = {}
    for s_idx, s in enumerate(slot_config):
        slot_end_min[s_idx] = _to_min(s["end"])
    # Lab slot end times (keyed by ("lab", idx) to avoid collisions)
    for s_idx, s in enumerate(lab_slot_config):
        slot_end_min[("lab", s_idx)] = _to_min(s["end"])

    # (0b) SOFT student-overlap penalty: penalize time conflicts proportional to shared students
    student_overlap_penalties = []
    for a_pos in range(len(sections)):
        for b_pos in range(a_pos + 1, len(sections)):
            code_a, code_b = sections[a_pos]["code"], sections[b_pos]["code"]
            if code_a == code_b:
                continue
            shared = _ssc(overlap_matrix, code_a, code_b)
            if shared == 0:
                continue
            # Penalize time overlap proportional to shared students
            w_overlap = shared * 5  # strong but not infinite
            for m_a in range(len(sections[a_pos]["pattern"])):
                for m_b in range(len(sections[b_pos]["pattern"])):
                    opts_a = sec_options[a_pos][m_a]
                    opts_b = sec_options[b_pos][m_b]
                    for oa, opt_a in enumerate(opts_a):
                        for ob, opt_b in enumerate(opts_b):
                            if opt_a[5] & opt_b[5]:
                                both = model.new_bool_var(
                                    f"so_{a_pos}_{m_a}_{oa}_{b_pos}_{m_b}_{ob}"
                                )
                                model.add(
                                    assign[a_pos][m_a][oa] + assign[b_pos][m_b][ob] - 1 <= both
                                )
                                model.add(both <= assign[a_pos][m_a][oa])
                                model.add(both <= assign[b_pos][m_b][ob])
                                student_overlap_penalties.append(both * w_overlap)

    penalties.extend(student_overlap_penalties)

    # (1) REAL GAP PENALTIES between section pairs sharing students
    for a_pos in range(len(sections)):
        for b_pos in range(a_pos + 1, len(sections)):
            code_a, code_b = sections[a_pos]["code"], sections[b_pos]["code"]
            if code_a == code_b:
                continue
            shared = _ssc(overlap_matrix, code_a, code_b)
            if shared == 0:
                continue
            if sections[a_pos]["is_online"] or sections[b_pos]["is_online"]:
                continue

            w = min(shared, 30)  # weight by shared students, capped

            for m_a in range(len(sections[a_pos]["pattern"])):
                for m_b in range(len(sections[b_pos]["pattern"])):
                    opts_a = sec_options[a_pos][m_a]
                    opts_b = sec_options[b_pos][m_b]

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
                                gap = (start_b - end_a) if start_a < start_b else (start_a - end_b)

                                if gap <= 15:
                                    continue

                                crosses = (end_a <= PRAYER_BOUNDARY < start_b) or (
                                    end_b <= PRAYER_BOUNDARY < start_a
                                )
                                if crosses:
                                    gap = int(gap * 1.5)

                                both = model.new_bool_var(
                                    f"g_{a_pos}_{m_a}_{oa}_{b_pos}_{m_b}_{ob}"
                                )
                                model.add(
                                    assign[a_pos][m_a][oa] + assign[b_pos][m_b][ob] - 1 <= both
                                )
                                model.add(both <= assign[a_pos][m_a][oa])
                                model.add(both <= assign[b_pos][m_b][ob])

                                penalties.append(both * gap * w)

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
        w = 2  # uniform (no S1 priority)
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
        w = 10  # uniform (no S1 priority)
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
        w = 5  # uniform (no S1 priority)
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
                "display_code": sec["display_code"],
                "course_name": sec.get("course_name", sec["display_code"]),
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

    # Clear existing auto-placed sections AND their meetings
    auto_placements = SectionPlacement.objects.filter(
        board=board, term_section__source_tag="tw_auto"
    )
    ts_ids = set(auto_placements.values_list("term_section_id", flat=True))
    auto_placements.delete()
    for ts_id in ts_ids:
        TermSectionMeeting.objects.filter(term_section_id=ts_id).delete()

    scenario = board.scenario

    budget_map = {
        (b.course_key or b.course_code): b
        for b in ScenarioSectionBudget.objects.filter(
            scenario=scenario, programme_term=board.nominal_term
        )
    }

    for p in result["placements"]:
        code = p["course_code"]
        display_code = p.get("display_code", code)
        budget = budget_map.get(code)
        cap = budget.max_per_section if budget else 40

        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=code,
            section=p["section"],
            defaults={
                "course_code": display_code,
                "course_number": display_code,
                "course_name": p.get("course_name")
                or (budget.course_name if budget else display_code),
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
    """Solve and persist placements, then assign rooms."""
    result = solve_board(board_id, time_limit_seconds)
    persist_solver_result(board_id, result)
    from core.services.timetable_rooming import assign_rooms_to_board

    assign_rooms_to_board(board_id)
    return result

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
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))
        for s_idx, s in enumerate(lab_slot_config):
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_min(s["start"])
            slots_lab.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_lab

    sections = []
    for budget in budgets:
        code = budget.course_key or budget.course_code
        display_code = budget.course_code
        cr = budget.credit_hours or 3
        pattern = get_meeting_pattern(cr)
        already = (
            SectionPlacement.objects.filter(board=board, term_section__course_key=code)
            .values("term_section_id")
            .distinct()
            .count()
        )
        to_place = max(0, budget.planned_sections - already)
        for sec_num in range(already + 1, already + to_place + 1):
            sections.append(
                {
                    "code": code,
                    "display_code": display_code,
                    "course_name": budget.course_name or display_code,
                    "sec_num": sec_num,
                    "label": f"S{sec_num}",
                    "pattern": pattern,
                    "is_online": display_code.upper() in online_codes,
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

    # Build overlap matrix for soft penalties (NOT hard constraints)
    from core.services.timetable_overlap import (
        build_overlap_matrix as _bom,
    )
    from core.services.timetable_overlap import (
        shared_student_count as _ssc,
    )

    board_courses_h = {sec["code"] for sec in sections}
    overlap_matrix_h = _bom(scenario.id, board_courses_h)
    # Student overlap is SOFT — added in the objective section below

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

    # (0b) SOFT student-overlap time penalty (same as solve_board)
    penalties = []
    _add_same_course_adjacency_penalties(model, penalties, assign, sec_options, sections)
    for a_pos in range(len(sections)):
        for b_pos in range(a_pos + 1, len(sections)):
            code_a, code_b = sections[a_pos]["code"], sections[b_pos]["code"]
            if code_a == code_b:
                continue
            shared = _ssc(overlap_matrix_h, code_a, code_b)
            if shared == 0:
                continue
            w_overlap = shared * 5
            for m_a in range(len(sections[a_pos]["pattern"])):
                for m_b in range(len(sections[b_pos]["pattern"])):
                    opts_a = sec_options[a_pos][m_a]
                    opts_b = sec_options[b_pos][m_b]
                    for oa, opt_a in enumerate(opts_a):
                        for ob, opt_b in enumerate(opts_b):
                            if opt_a[5] & opt_b[5]:
                                both = model.new_bool_var(
                                    f"so_{a_pos}_{m_a}_{oa}_{b_pos}_{m_b}_{ob}"
                                )
                                model.add(
                                    assign[a_pos][m_a][oa] + assign[b_pos][m_b][ob] - 1 <= both
                                )
                                model.add(both <= assign[a_pos][m_a][oa])
                                model.add(both <= assign[b_pos][m_b][ob])
                                penalties.append(both * w_overlap)

    # Soft: gap penalties weighted by real student overlap
    for a_pos in range(len(sections)):
        for b_pos in range(a_pos + 1, len(sections)):
            code_a, code_b = sections[a_pos]["code"], sections[b_pos]["code"]
            if code_a == code_b:
                continue
            shared = _ssc(overlap_matrix_h, code_a, code_b)
            if shared == 0:
                continue
            if sections[a_pos]["is_online"] or sections[b_pos]["is_online"]:
                continue
            w = min(shared, 30)
            for m_a in range(len(sections[a_pos]["pattern"])):
                for m_b in range(len(sections[b_pos]["pattern"])):
                    opts_a = sec_options[a_pos][m_a]
                    opts_b = sec_options[b_pos][m_b]
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
                                gap = (start_b - end_a) if start_a < start_b else (start_a - end_b)
                                if gap <= 15:
                                    continue
                                crosses = (end_a <= PRAYER_BOUNDARY < start_b) or (
                                    end_b <= PRAYER_BOUNDARY < start_a
                                )
                                if crosses:
                                    gap = int(gap * 1.5)
                                both = model.new_bool_var(
                                    f"g_{a_pos}_{m_a}_{oa}_{b_pos}_{m_b}_{ob}"
                                )
                                model.add(
                                    assign[a_pos][m_a][oa] + assign[b_pos][m_b][ob] - 1 <= both
                                )
                                model.add(both <= assign[a_pos][m_a][oa])
                                model.add(both <= assign[b_pos][m_b][ob])
                                penalties.append(both * gap * w)

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
        w = 2  # uniform (no S1 priority)
        for m_idx in range(len(sec["pattern"])):
            for o_idx, opt in enumerate(sec_options[i][m_idx]):
                penalties.append(assign[i][m_idx][o_idx] * opt[1] * w)

    # 3b: heavy penalty for meetings at DIFFERENT slot indices
    for i, sec in enumerate(sections):
        lecture_meetings = [m for m in range(len(sec["pattern"])) if sec["pattern"][m] <= 75]
        if len(lecture_meetings) <= 1:
            continue
        w = 10  # uniform (no S1 priority)
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
        w = 5  # uniform (no S1 priority)
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
                "display_code": sec["display_code"],
                "course_name": sec.get("course_name", sec["display_code"]),
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
        "placed_vs_greedy": len(placements) - greedy_count,
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
