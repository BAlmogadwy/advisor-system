"""
core/services/timetable_cpsat_polisher.py
Global CP-SAT polisher for cross-board timetable optimisation.

Takes an existing timetable (after greedy + local search) and runs a
single CP-SAT pass across ALL boards simultaneously, with:
  - Cross-board student overlap penalties
  - Warm-start hints from current best
  - Same section set (no adding/removing sections)
  - Proxy objective → verified by full assignment evaluator

This is a POLISHER, not a rebuilder — it only accepts improvements
confirmed by the student-assignment evaluator.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from ortools.sat.python import cp_model

from core.models import (
    DeliveryBoard,
    ScenarioStudentMap,
)
from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentProfile,
    TimetableEvaluationResult,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_decision_trace import DecisionTrace
from core.services.timetable_solver_codes import CPSAT_IMPROVED, is_stage_trace_enabled
from core.services.timetable_stage_telemetry import (
    is_stage_telemetry_enabled,
    record_stage_iterations,
    record_stage_ms,
)
from core.services.timetable_workspace import _time_mask, _to_minutes

logger = logging.getLogger(__name__)


def build_cross_board_overlap_matrix(
    scenario_id: int,
) -> dict[tuple[str, str], int]:
    """Build a student overlap matrix across ALL courses in the scenario.

    Returns {(course_a, course_b): shared_student_count} where
    course_a < course_b lexicographically.
    """
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in ScenarioStudentMap.objects.filter(scenario_id=scenario_id):
        for code in sm.recommended_courses or []:
            course_students[code].add(sm.student_id)

    overlap: dict[tuple[str, str], int] = {}
    codes = sorted(course_students.keys())
    for i, ca in enumerate(codes):
        sa = course_students[ca]
        for cb in codes[i + 1 :]:
            shared = len(sa & course_students[cb])
            if shared > 0:
                overlap[(ca, cb)] = shared

    logger.info(
        "Cross-board overlap matrix: %d courses, %d non-zero pairs",
        len(codes),
        len(overlap),
    )
    return overlap


def _shared_count(overlap: dict[tuple[str, str], int], a: str, b: str) -> int:
    key = (min(a, b), max(a, b))
    return overlap.get(key, 0)


def polish_scenario_with_cpsat(
    scenario_id: int,
    current_sections: list[SectionState],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    current_eval: TimetableEvaluationResult,
    time_limit_seconds: float = 60.0,
    hotspot_only: bool = False,
    stage_telemetry: dict[str, dict[str, int]] | None = None,
    locked_section_ids: set[str] | None = None,
) -> dict | None:
    """Run a global CP-SAT pass as a polisher on the current best timetable.

    Parameters
    ----------
    scenario_id : int
        Scenario PK.
    current_sections : list[SectionState]
        Current best timetable sections (with meetings, pattern info).
    student_profiles : dict[str, StudentProfile]
        Student assignment profiles.
    course_rigidity : dict[str, float]
        Per-course rigidity.
    current_eval : TimetableEvaluationResult
        Current evaluation result (used for hotspot filtering).
    time_limit_seconds : float
        CP-SAT solver time budget.
    hotspot_only : bool
        If True, only include hotspot courses + their overlap partners.
        Much faster for large scenarios.

    Returns
    -------
    TimetableEvaluationResult or None
        Improved result if CP-SAT found a strictly better solution
        (verified by full evaluator). None if no improvement.
    """
    scenario = DeliveryBoard.objects.filter(scenario_id=scenario_id).first()
    if not scenario:
        return None
    scenario_obj = scenario.scenario

    slot_config = scenario_obj.slot_config or DEFAULT_SLOTS
    lab_slot_config = scenario_obj.lab_slot_config or DEFAULT_LAB_SLOTS

    # Build cross-board overlap matrix
    overlap = build_cross_board_overlap_matrix(scenario_id)

    # Determine which sections to include in the model. Locked sections
    # remain fixed; otherwise the verified score could assume a move that
    # persistence later refuses to write.
    locked = locked_section_ids or set()
    if hotspot_only:
        hotspot_set = set(current_eval.hotspot_courses[:10])
        # Add overlap partners
        for code in list(hotspot_set):
            for (ca, cb), count in overlap.items():
                if ca == code and count >= 3:
                    hotspot_set.add(cb)
                elif cb == code and count >= 3:
                    hotspot_set.add(ca)
        sections_to_polish = [
            s
            for s in current_sections
            if s.course_code in hotspot_set and s.section_id not in locked
        ]
        fixed_sections = [
            s
            for s in current_sections
            if s.course_code not in hotspot_set or s.section_id in locked
        ]
    else:
        sections_to_polish = [s for s in current_sections if s.section_id not in locked]
        fixed_sections = [s for s in current_sections if s.section_id in locked]

    if not sections_to_polish:
        logger.info("CP-SAT polisher: no sections to polish")
        return None

    logger.info(
        "CP-SAT polisher: %d sections to optimise, %d fixed, time_limit=%.0fs",
        len(sections_to_polish),
        len(fixed_sections),
        time_limit_seconds,
    )

    # Build valid slot options per duration
    slots_75: list[tuple] = []
    slots_lab: list[tuple] = []
    for day_idx, day in enumerate(WEEKDAYS):
        for s_idx, s in enumerate(slot_config):
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_minutes(s["start"])
            end_min = _to_minutes(s["end"])
            slots_75.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min, end_min))

        for s_idx, s in enumerate(lab_slot_config):
            mask = _time_mask(day, s["start"], s["end"])
            start_min = _to_minutes(s["start"])
            end_min = _to_minutes(s["end"])
            slots_lab.append((day_idx, s_idx, day, s["start"], s["end"], mask, start_min, end_min))

    def get_options(duration: int):
        return slots_75 if duration <= 75 else slots_lab

    # ── Build CP-SAT model ───────────────────────────────────────
    model = cp_model.CpModel()

    # Compute meeting durations for each section
    sec_durations: list[list[int]] = []
    for sec in sections_to_polish:
        durations = [m.end_min - m.start_min for m in sec.meetings]
        sec_durations.append(durations)

    # Boolean vars: assign[sec_idx][meeting_idx][option_idx]
    assign: list[list[list]] = []
    sec_options: list[list[list[tuple]]] = []

    for i, _sec in enumerate(sections_to_polish):
        sec_assign = []
        sec_opts = []
        for m_idx, duration in enumerate(sec_durations[i]):
            options = get_options(duration)
            m_assign = []
            for o_idx, _opt in enumerate(options):
                var = model.new_bool_var(f"a_{i}_{m_idx}_{o_idx}")
                m_assign.append(var)
            sec_assign.append(m_assign)
            sec_opts.append(list(options))

            # Exactly one option per meeting
            model.add_exactly_one(m_assign)

        assign.append(sec_assign)
        sec_options.append(sec_opts)

    # ── Hard: all-different days per section ──────────────────────
    for i, _sec in enumerate(sections_to_polish):
        if len(sec_durations[i]) <= 1:
            continue
        for m_a in range(len(sec_durations[i])):
            for m_b in range(m_a + 1, len(sec_durations[i])):
                opts_a = sec_options[i][m_a]
                opts_b = sec_options[i][m_b]
                for oa, opt_a in enumerate(opts_a):
                    for ob, opt_b in enumerate(opts_b):
                        if opt_a[0] == opt_b[0]:  # same day_idx
                            model.add(assign[i][m_a][oa] + assign[i][m_b][ob] <= 1)

    # ── Hard: no same-course section overlap ─────────────────────
    course_groups: dict[str, list[int]] = defaultdict(list)
    for i, sec in enumerate(sections_to_polish):
        course_groups[sec.course_code].append(i)

    for _code, indices in course_groups.items():
        if len(indices) < 2:
            continue
        for ia in range(len(indices)):
            for ib in range(ia + 1, len(indices)):
                i_a = indices[ia]
                i_b = indices[ib]
                for m_a in range(len(sec_durations[i_a])):
                    for m_b in range(len(sec_durations[i_b])):
                        opts_a = sec_options[i_a][m_a]
                        opts_b = sec_options[i_b][m_b]
                        for oa, opt_a in enumerate(opts_a):
                            for ob, opt_b in enumerate(opts_b):
                                if opt_a[0] == opt_b[0] and opt_a[5] & opt_b[5]:
                                    model.add(assign[i_a][m_a][oa] + assign[i_b][m_b][ob] <= 1)

    # ── Soft: cross-course student overlap penalty ───────────────
    penalties = []
    for i_a in range(len(sections_to_polish)):
        code_a = sections_to_polish[i_a].course_code
        for i_b in range(i_a + 1, len(sections_to_polish)):
            code_b = sections_to_polish[i_b].course_code
            if code_a == code_b:
                continue
            shared = _shared_count(overlap, code_a, code_b)
            if shared == 0:
                continue
            weight = shared * 5
            for m_a in range(len(sec_durations[i_a])):
                for m_b in range(len(sec_durations[i_b])):
                    opts_a = sec_options[i_a][m_a]
                    opts_b = sec_options[i_b][m_b]
                    for oa, opt_a in enumerate(opts_a):
                        for ob, opt_b in enumerate(opts_b):
                            if opt_a[0] == opt_b[0] and opt_a[5] & opt_b[5]:
                                penalties.append(
                                    (assign[i_a][m_a][oa], assign[i_b][m_b][ob], weight)
                                )

    # Also penalise overlap with FIXED sections (if hotspot_only mode)
    for fixed_sec in fixed_sections:
        for i_a in range(len(sections_to_polish)):
            code_a = sections_to_polish[i_a].course_code
            shared = _shared_count(overlap, code_a, fixed_sec.course_code)
            if shared == 0:
                continue
            weight = shared * 5
            for m_a in range(len(sec_durations[i_a])):
                opts_a = sec_options[i_a][m_a]
                for fixed_meeting in fixed_sec.meetings:
                    for oa, opt_a in enumerate(opts_a):
                        if opt_a[0] == fixed_meeting.day and opt_a[5] & fixed_meeting.mask:
                            penalties.append((assign[i_a][m_a][oa], None, weight))

    # Build penalty objective
    penalty_vars = []
    for var_a, var_b, weight in penalties:
        if var_b is not None:
            p = model.new_bool_var(f"p_{len(penalty_vars)}")
            model.add(var_a + var_b - 1 <= p)
            penalty_vars.append((p, weight))
        else:
            # Fixed section overlap — penalty if var_a is 1
            penalty_vars.append((var_a, weight))

    if penalty_vars:
        model.minimize(sum(var * w for var, w in penalty_vars))

    # ── Warm-start hints from current placements ─────────────────
    # Tell the solver where each section currently sits. This gives it
    # a feasible starting point, so even if it times out it can return
    # a solution at least as good as the current one.
    hints_set = 0
    for i, sec in enumerate(sections_to_polish):
        for m_idx, meeting in enumerate(sec.meetings):
            opts = sec_options[i][m_idx]
            for o_idx, opt in enumerate(opts):
                if opt[0] == meeting.day and opt[6] == meeting.start_min:
                    model.add_hint(assign[i][m_idx][o_idx], 1)
                    hints_set += 1
                    break

    logger.info(
        "CP-SAT model: %d sections, %d penalty terms, %d hints, solving...",
        len(sections_to_polish),
        len(penalty_vars),
        hints_set,
    )

    # ── Solve ────────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 8
    solver.parameters.random_seed = 42

    # PR6 commit 5 — stage-boundary timing. Guardrail: iterations==1 means
    # the polisher was actually invoked (solver.solve() reached), not
    # merely enabled by config. All early returns above this fence leave
    # cpsat telemetry at zero (short-circuit semantics per DoR §3).
    _telemetry_on = stage_telemetry is not None and is_stage_telemetry_enabled()
    _cpsat_t0 = time.perf_counter() if _telemetry_on else 0.0
    status = solver.solve(model)
    if _telemetry_on:
        record_stage_ms(stage_telemetry, "cpsat", int((time.perf_counter() - _cpsat_t0) * 1000))
        record_stage_iterations(stage_telemetry, "cpsat", 1)
    status_name = solver.status_name(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        logger.info("CP-SAT polisher: %s — no solution found", status_name)
        return None

    logger.info(
        "CP-SAT polisher: %s, objective=%d, wall_time=%.1fs",
        status_name,
        int(solver.objective_value),
        solver.wall_time,
    )

    # ── Extract solution → SectionState list ─────────────────────
    improved_sections: list[SectionState] = []

    for i, sec in enumerate(sections_to_polish):
        new_meetings: list[SectionMeeting] = []
        for m_idx in range(len(sec_durations[i])):
            opts = sec_options[i][m_idx]
            for o_idx, opt in enumerate(opts):
                if solver.value(assign[i][m_idx][o_idx]):
                    new_meetings.append(
                        SectionMeeting(
                            day=opt[0],
                            start_min=opt[6],
                            end_min=opt[7],
                        )
                    )
                    break

        improved_sections.append(
            SectionState(
                section_id=sec.section_id,
                course_code=sec.course_code,
                meetings=new_meetings,
                max_capacity=sec.max_capacity,
                reserve_capacity=sec.reserve_capacity,
                room_type_required=sec.room_type_required,
                demand_capacity=sec.demand_capacity,
                assigned_room_id=sec.assigned_room_id,
                pattern_family=sec.pattern_family,
                pattern_id=sec.pattern_id,
            )
        )

    # Add fixed sections back
    all_sections = improved_sections + fixed_sections

    # ── Build decision trace for changed sections ────────────────────
    decision_trace = {}
    if is_stage_trace_enabled():

        def minutes_to_hhmm(minutes: int) -> str:
            """Convert minutes to HH:MM format."""
            hours = minutes // 60
            mins = minutes % 60
            return f"{hours:02d}:{mins:02d}"

        # Compare original vs improved sections to find changes
        for i, original_sec in enumerate(sections_to_polish):
            improved_sec = improved_sections[i]

            # Compare signatures: (day, start_min, end_min) per meeting
            original_signature = tuple(
                (meeting.day, meeting.start_min, meeting.end_min)
                for meeting in original_sec.meetings
            )
            improved_signature = tuple(
                (meeting.day, meeting.start_min, meeting.end_min)
                for meeting in improved_sec.meetings
            )

            if original_signature != improved_signature:
                # Find first changed meeting
                first_changed_idx = next(
                    idx
                    for idx, (orig, new) in enumerate(
                        zip(original_signature, improved_signature, strict=False)
                    )
                    if orig != new
                )

                orig_meeting = original_sec.meetings[first_changed_idx]
                new_meeting = improved_sec.meetings[first_changed_idx]

                # Strip the course_code prefix from section_id ("CS101_S1" → "S1")
                # so section_code matches the "{course}|{label}" convention used
                # by the greedy / SA / chain / rooming traces.
                section_label = original_sec.section_id
                prefix = f"{original_sec.course_code}_"
                if section_label.startswith(prefix):
                    section_label = section_label[len(prefix) :]
                section_code = f"{original_sec.course_code}|{section_label}"

                orig_day_name = WEEKDAYS[orig_meeting.day]
                new_day_name = WEEKDAYS[new_meeting.day]

                trace_entry = DecisionTrace(
                    section_code=section_code,
                    course_code=original_sec.course_code,
                    chosen_day=new_day_name,
                    chosen_start_time=minutes_to_hhmm(new_meeting.start_min),
                    chosen_end_time=minutes_to_hhmm(new_meeting.end_min),
                    chosen_room="",
                    alternatives=(),
                    stage_origin="cpsat",
                    stage_context={
                        "code": CPSAT_IMPROVED,
                        "previous_slot": f"{orig_day_name} {minutes_to_hhmm(orig_meeting.start_min)}-{minutes_to_hhmm(orig_meeting.end_min)}",
                        "new_slot": f"{new_day_name} {minutes_to_hhmm(new_meeting.start_min)}-{minutes_to_hhmm(new_meeting.end_min)}",
                    },
                )
                decision_trace[section_code] = trace_entry.to_dict()

    # ── Verify with full evaluator ───────────────────────────────
    # CRITICAL: The CP-SAT objective is a PROXY (weighted overlap
    # penalties). It doesn't model the full student assignment with
    # capacity limits, reserve logic, and gap calculations. We must
    # verify any "improvement" with the real evaluator before accepting.
    polished_eval = evaluate_generated_timetable_candidate(
        candidate_id="cpsat_polish",
        generated_sections=all_sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
    )

    if polished_eval.lexicographic_score < current_eval.lexicographic_score:
        logger.info(
            "CP-SAT polisher IMPROVED: %s -> %s",
            current_eval.lexicographic_score,
            polished_eval.lexicographic_score,
        )
        return {
            "eval": polished_eval,
            "improved_sections": improved_sections,
            "decision_trace": decision_trace,
        }
    else:
        logger.info(
            "CP-SAT polisher: no improvement (current %s, polished %s)",
            current_eval.lexicographic_score,
            polished_eval.lexicographic_score,
        )
        return None
