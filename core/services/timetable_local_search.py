"""
core/services/timetable_local_search.py
Simulated annealing + iterative local search for timetable optimization.

This is the state-of-the-art approach used by ITC competition winners and
production university schedulers (UniTime, OptaPlanner). The algorithm:

  Phase 1: Build a feasible solution using the greedy auto-placer (0.1s)
  Phase 2: Improve it via thousands of random moves (5-10s)

Move types:
  - RELOCATE: move one meeting to a different (day, slot) on the same board
  - SWAP: exchange the time slots of two meetings from different sections

Acceptance criterion (simulated annealing):
  - Always accept improvements (lower cost)
  - Sometimes accept worse solutions with probability exp(-delta/temperature)
  - Temperature decreases over time (annealing schedule)
  - This allows escaping local optima that greedy gets stuck in

Cost function (what we minimize):
  - Overlap-weighted idle gap minutes between courses sharing students
  - Gap weight proportional to shared_student_count (capped at 30)
  - 1.5x penalty for midday break crossings
  - Online courses not counted in gaps
  - Hard constraints (same-course overlap, shared >= HARD_OVERLAP_THRESHOLD)
    = infinite cost (rejected immediately)
"""

from __future__ import annotations

import math
import random
import time
from collections import defaultdict

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
    _time_mask,
)
from core.services.timetable_decision_trace import DecisionTrace
from core.services.timetable_online import OnlineCourseLookup, normalise_course_code
from core.services.timetable_same_course import (
    has_same_course_overlap as same_course_windows_overlap,
)
from core.services.timetable_same_course import (
    make_meeting_window,
    same_course_section_spread_penalty,
)
from core.services.timetable_solver_codes import (
    SA_RELOCATE_ACCEPTED,
    is_stage_trace_enabled,
)
from core.services.timetable_stage_telemetry import (
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
    record_stage_iterations,
    record_stage_ms,
)


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


MIDDAY_BOUNDARY = 13 * 60  # 13:00


# ── Cost Function ────────────────────────────────────────────────


def _same_course_windows_by_course(schedule: dict[int, list[dict]], sections: list[dict]) -> dict:
    by_course = defaultdict(list)
    for idx, meetings in schedule.items():
        sec = sections[idx]
        code = sec["code"]
        label = sec.get("label", str(idx))
        by_course[code].append(
            [make_meeting_window(code, m["day"], m["start"], m["end"], label) for m in meetings]
        )
    return by_course


def _same_course_spread_cost(schedule: dict[int, list[dict]], sections: list[dict]) -> int:
    return same_course_section_spread_penalty(_same_course_windows_by_course(schedule, sections))


def _has_same_course_overlap(schedule: dict[int, list[dict]], sections: list[dict]) -> bool:
    return any(
        same_course_windows_overlap(meetings)
        for meetings in _same_course_windows_by_course(schedule, sections).values()
    )


def _compute_cost(
    schedule: dict[int, list[dict]],
    sections: list[dict],
    online_codes: set[str],
    overlap_matrix: dict | None = None,
) -> float:
    """Compute total weighted idle gap cost for a schedule.

    Args:
        schedule: {section_index: [{day, slot_idx, start, end, mask}, ...]}
        sections: list of section metadata dicts
        online_codes: set of online course codes
        overlap_matrix: real student-overlap matrix (optional)

    Returns:
        Total cost (lower is better). Float to allow fractional annealing.
    """
    # Collect meetings by course for overlap-weighted gap computation
    course_day_times: dict[str, dict[str, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for i, meetings in schedule.items():  # noqa: B007
        sec = sections[i]
        if normalise_course_code(sec["code"]) in online_codes:
            continue
        for m in meetings:
            s_min = _to_min(m["start"])
            e_min = _to_min(m["end"])
            course_day_times[sec["code"]][m["day"]].append((s_min, e_min))

    # Compute pairwise gap costs weighted by shared students
    from core.services.timetable_overlap import shared_student_count as _ssc

    total_cost = 0.0
    codes = list(course_day_times.keys())
    seen_pairs: set[tuple[str, str]] = set()
    for a_idx in range(len(codes)):
        for b_idx in range(a_idx, len(codes)):
            code_a, code_b = codes[a_idx], codes[b_idx]
            pk = (min(code_a, code_b), max(code_a, code_b))
            if pk in seen_pairs:
                continue
            seen_pairs.add(pk)

            if code_a == code_b:
                continue  # same course: student takes ONE section, no inter-section gap
            elif overlap_matrix:
                shared = _ssc(overlap_matrix, code_a, code_b)
                if shared == 0:
                    continue
                w = min(shared, 30) * 0.5
            else:
                w = 10.0

            for day in set(course_day_times[code_a].keys()) & set(course_day_times[code_b].keys()):
                times = sorted(course_day_times[code_a][day] + course_day_times[code_b][day])
                if len(times) < 2:
                    continue
                for j in range(len(times) - 1):
                    gap = times[j + 1][0] - times[j][1]
                    if gap <= 0:
                        continue
                    if times[j][1] <= MIDDAY_BOUNDARY < times[j + 1][0]:
                        gap *= 1.5
                    total_cost += gap * w

    total_cost += _same_course_spread_cost(schedule, sections)
    return total_cost


def _check_hard_constraints(
    schedule: dict[int, list[dict]],
    sections: list[dict],
    overlap_matrix: dict | None = None,
) -> bool:
    """Return True if all hard constraints are satisfied."""

    # 1. No HARD overlap only for same-course or very high-overlap pairs
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        SAME_COURSE_SENTINEL,
    )
    from core.services.timetable_overlap import (
        shared_student_count as _ssc_hc,
    )

    all_masks: list[tuple[str, int]] = []
    for i, meetings in schedule.items():  # noqa: B007
        sec = sections[i]
        for m in meetings:
            all_masks.append((sec["code"], m["mask"]))

    for a in range(len(all_masks)):
        for b in range(a + 1, len(all_masks)):
            if all_masks[a][0] == all_masks[b][0]:
                continue  # same course checked below
            if all_masks[a][1] & all_masks[b][1]:
                shared = (
                    _ssc_hc(overlap_matrix, all_masks[a][0], all_masks[b][0])
                    if overlap_matrix
                    else SAME_COURSE_SENTINEL
                )
                if shared >= HARD_OVERLAP_THRESHOLD:
                    return False

    # 2. Same course different sections don't overlap
    if _has_same_course_overlap(schedule, sections):
        return False

    # 3. All different days per section
    for i, meetings in schedule.items():  # noqa: B007
        days = [m["day"] for m in meetings]
        if len(days) != len(set(days)):
            return False

    return True


# ── Move Generators ──────────────────────────────────────────────


def _generate_relocate_move(
    schedule: dict[int, list[dict]],
    sections: list[dict],
    valid_options: dict[int, list[list[dict]]],
    rng: random.Random,
    locked_idx: set[int] | None = None,
) -> tuple[int, int, dict] | None:
    """Generate a random RELOCATE move: move one meeting to a different slot.

    Returns (section_idx, meeting_idx, new_option) or None if no valid move.
    A locked section is never selected as a move target (the draw is burned,
    matching the LS-v2 skip-set contract).
    """
    sec_idx = rng.randint(0, len(sections) - 1)
    if locked_idx and sec_idx in locked_idx:
        return None
    meetings = schedule[sec_idx]
    if not meetings:
        return None

    m_idx = rng.randint(0, len(meetings) - 1)
    current = meetings[m_idx]
    options = valid_options[sec_idx][m_idx]

    if len(options) <= 1:
        return None

    # Pick a random different option
    new_opt = rng.choice(options)
    attempts = 0
    while (
        new_opt["day"] == current["day"] and new_opt["start"] == current["start"] and attempts < 10
    ):
        new_opt = rng.choice(options)
        attempts += 1

    if new_opt["day"] == current["day"] and new_opt["start"] == current["start"]:
        return None

    return sec_idx, m_idx, new_opt


def _apply_move(
    schedule: dict[int, list[dict]],
    sec_idx: int,
    m_idx: int,
    new_opt: dict,
) -> dict:
    """Apply a move and return the old option (for undo)."""
    old = schedule[sec_idx][m_idx].copy()
    schedule[sec_idx][m_idx] = new_opt
    return old


def _undo_move(
    schedule: dict[int, list[dict]],
    sec_idx: int,
    m_idx: int,
    old_opt: dict,
) -> None:
    schedule[sec_idx][m_idx] = old_opt


# ── Main Algorithm ───────────────────────────────────────────────


def optimize_board(
    board_id: int,
    max_seconds: float = 8.0,
    initial_temp: float = 500.0,
    cooling_rate: float = 0.9995,
    seed: int = 42,
) -> dict:
    """Optimize a board's timetable using simulated annealing.

    Starts from the current greedy solution (already placed on the board),
    then improves it through random moves.

    Returns dict with: status, cost_before, cost_after, iterations, improvements
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"status": "error", "stage_telemetry": empty_stage_telemetry()}

    scenario = board.scenario
    slot_config = scenario.slot_config or DEFAULT_SLOTS
    lab_slot_config = scenario.lab_slot_config or DEFAULT_LAB_SLOTS

    # Build real overlap matrix
    from core.models import ScenarioSectionBudget
    from core.services.timetable_overlap import build_overlap_matrix as _bom

    board_courses = set(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario, programme_term=board.nominal_term
        ).values_list("course_code", flat=True)
    )
    overlap_matrix = _bom(scenario.id, board_courses) if board_courses else {}

    online_codes = OnlineCourseLookup().codes_for_board(board)

    # Load current placements as the initial solution
    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("term_section__course_code", "term_section__section", "day")
    )

    if not placements:
        return {
            "status": "empty",
            "cost_before": 0,
            "cost_after": 0,
            "stage_telemetry": empty_stage_telemetry(),
        }

    # Build sections list and initial schedule
    sections = []
    schedule: dict[int, list[dict]] = {}
    sec_map: dict[tuple[str, str], int] = {}  # (code, section) -> index

    for p in placements:
        key = (p.term_section.course_code, p.term_section.section)
        if key not in sec_map:
            idx = len(sections)
            sec_map[key] = idx
            # Extract sec_num from section label "S1" -> 1
            sec_label = p.term_section.section
            sec_num = (
                int(sec_label[1:]) if sec_label.startswith("S") and sec_label[1:].isdigit() else 1
            )
            sections.append(
                {
                    "code": p.term_section.course_code,
                    "sec_num": sec_num,
                    "label": sec_label,
                    "term_section_id": p.term_section_id,
                    "is_online": normalise_course_code(p.term_section.course_code) in online_codes,
                    "is_locked": False,
                }
            )
            schedule[idx] = []

        idx = sec_map[key]
        if p.is_locked:
            sections[idx]["is_locked"] = True
        # Find slot_idx for this placement (check lecture slots, then lab slots)
        slot_idx = 0
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        if duration <= 75:
            for si, s in enumerate(slot_config):
                if s["start"] == p.start_time:
                    slot_idx = si
                    break
        else:
            for si, s in enumerate(lab_slot_config):
                if s["start"] == p.start_time:
                    slot_idx = si
                    break

        schedule[idx].append(
            {
                "day": p.day,
                "slot_idx": slot_idx,
                "start": p.start_time,
                "end": p.end_time,
                "mask": _time_mask(p.day, p.start_time, p.end_time),
                "placement_id": p.id,
            }
        )

    # PR5 commit 3 — snapshot the initial (greedy) schedule before any
    # SA mutation so we can emit an SA_RELOCATE_ACCEPTED trace entry for
    # every section whose chosen meeting set differs after the polish
    # pass. Captured here under flag gate so flag-off builds have
    # zero overhead. Keyed on section-index into ``sections``; each
    # entry stores a frozen tuple of (day, start, end) per meeting for
    # deterministic post-run diffing.
    initial_schedule_snapshot: dict[int, tuple[tuple[str, str, str], ...]] = {}
    if is_stage_trace_enabled():
        for i, meetings in schedule.items():
            initial_schedule_snapshot[i] = tuple((m["day"], m["start"], m["end"]) for m in meetings)

    from core.services.timetable_validation import (
        blocked_slot_keys,
        is_lock_enforcement_enabled,
    )

    # Blocked cells are excluded from the relocate domain (always on); locked
    # sections are never offered as a relocate target (flag-gated). Sections are
    # never dropped — only gated — so dense-index keying stays aligned with the
    # schedule, snapshot, and PR5 trace diff.
    blocked_set = blocked_slot_keys(scenario.blocked_slots)
    locked_idx: set[int] = (
        {i for i, sec in enumerate(sections) if sec.get("is_locked")}
        if is_lock_enforcement_enabled()
        else set()
    )

    # Build valid options per section per meeting
    valid_options: dict[int, list[list[dict]]] = {}
    for i, _sec in enumerate(sections):
        meetings = schedule[i]
        sec_opts = []
        for m in meetings:
            # Determine duration from current meeting
            duration = _to_min(m["end"]) - _to_min(m["start"])
            options = []
            for day in WEEKDAYS:
                if duration <= 75:
                    for si, s in enumerate(slot_config):
                        if (day, s["start"]) in blocked_set:
                            continue
                        mask = _time_mask(day, s["start"], s["end"])
                        options.append(
                            {
                                "day": day,
                                "slot_idx": si,
                                "start": s["start"],
                                "end": s["end"],
                                "mask": mask,
                            }
                        )
                else:
                    # Lab (100-min): use dedicated lab slot grid
                    for si, s in enumerate(lab_slot_config):
                        if (day, s["start"]) in blocked_set:
                            continue
                        mask = _time_mask(day, s["start"], s["end"])
                        options.append(
                            {
                                "day": day,
                                "slot_idx": si,
                                "start": s["start"],
                                "end": s["end"],
                                "mask": mask,
                            }
                        )
            sec_opts.append(options)
        valid_options[i] = sec_opts

    # ── Simulated Annealing ──────────────────────────────────────

    rng = random.Random(seed)
    cost_before = _compute_cost(schedule, sections, online_codes, overlap_matrix)
    best_cost = cost_before
    best_schedule = {k: [m.copy() for m in v] for k, v in schedule.items()}
    current_cost = cost_before
    temperature = initial_temp

    iterations = 0
    improvements = 0
    start_time = time.time()

    # PR6 commit 4 — SA stage boundary fence (perf_counter for sub-ms
    # resolution; monotonic across Python stdlib contract). Instrument
    # only at the stage entry/exit, not inside the inner loop body
    # (ChatGPT commit-4 ruling: no per-iteration timing arrays).
    _telemetry_on = is_stage_telemetry_enabled()
    _sa_t0 = time.perf_counter() if _telemetry_on else 0.0

    while time.time() - start_time < max_seconds:
        iterations += 1
        temperature *= cooling_rate

        # Generate random move (locked sections are never relocated)
        move = _generate_relocate_move(schedule, sections, valid_options, rng, locked_idx)
        if move is None:
            continue

        sec_idx, m_idx, new_opt = move
        old_opt = _apply_move(schedule, sec_idx, m_idx, new_opt)

        # Check hard constraints
        if not _check_hard_constraints(schedule, sections, overlap_matrix):
            _undo_move(schedule, sec_idx, m_idx, old_opt)
            continue

        # Compute new cost
        new_cost = _compute_cost(schedule, sections, online_codes, overlap_matrix)
        delta = new_cost - current_cost

        # Accept or reject
        if delta < 0:
            # Improvement — always accept
            current_cost = new_cost
            improvements += 1
            if new_cost < best_cost:
                best_cost = new_cost
                best_schedule = {k: [m.copy() for m in v] for k, v in schedule.items()}
        elif temperature > 0.01 and rng.random() < math.exp(-delta / temperature):
            # Worse but accepted (exploration)
            current_cost = new_cost
        else:
            # Rejected — undo
            _undo_move(schedule, sec_idx, m_idx, old_opt)

    # PR6 commit 4 — close the SA stage fence. sa.iterations is the
    # count of SA attempts (not accepted moves), per ChatGPT ruling.
    _stage_telemetry = empty_stage_telemetry()
    if _telemetry_on:
        record_stage_ms(_stage_telemetry, "sa", int((time.perf_counter() - _sa_t0) * 1000))
        record_stage_iterations(_stage_telemetry, "sa", iterations)

    # Restore best found
    schedule = best_schedule

    # PR5 commit 3 — emit SA_RELOCATE_ACCEPTED trace entries for every
    # section whose chosen meeting set shifted between the initial
    # (greedy) snapshot and the best-found post-SA schedule. Flag-gated;
    # when off, ``decision_trace`` is simply absent from the result
    # dict so the return shape remains backward-compatible.
    decision_trace: dict[str, dict] = {}
    if is_stage_trace_enabled() and initial_schedule_snapshot:
        for i, meetings in schedule.items():
            final_sig = tuple((m["day"], m["start"], m["end"]) for m in meetings)
            initial_sig = initial_schedule_snapshot.get(i, ())
            if final_sig == initial_sig:
                continue
            sec = sections[i]
            section_code = f"{sec['code']}|{sec['label']}"
            # Prefer the first differing meeting to anchor the chosen
            # slot in the trace entry. Subsequent meetings of the same
            # section share the same section_code key; the chosen_*
            # fields therefore represent the first-meeting anchor, and
            # stage_context records the full before/after signatures so
            # downstream consumers can reconstruct any missed meeting.
            anchor_initial = initial_sig[0] if initial_sig else ("", "", "")
            anchor_final = final_sig[0] if final_sig else ("", "", "")
            entry = DecisionTrace(
                section_code=section_code,
                course_code=sec["code"],
                chosen_day=anchor_final[0],
                chosen_start_time=anchor_final[1],
                chosen_end_time=anchor_final[2],
                chosen_room="",  # room reassignment happens in a later pass
                alternatives=(),
                stage_origin="sa",
                stage_context={
                    "code": SA_RELOCATE_ACCEPTED,
                    "from_slot": f"{anchor_initial[0]} {anchor_initial[1]}-{anchor_initial[2]}",
                    "to_slot": f"{anchor_final[0]} {anchor_final[1]}-{anchor_final[2]}",
                    "cost_delta": int(best_cost) - int(cost_before),
                    "initial_signature": list(initial_sig),
                    "final_signature": list(final_sig),
                },
            )
            decision_trace[section_code] = entry.to_dict()

    return {
        "status": "optimized",
        "cost_before": cost_before,
        "cost_after": best_cost,
        "iterations": iterations,
        "improvements": improvements,
        "schedule": schedule,
        "sections": sections,
        "decision_trace": decision_trace,
        # PR6 commit 4 — schema-stable telemetry block; sa.* populated
        # when flag on, zero otherwise. Other stage keys stay at zero
        # (greedy.* lives on auto_place_board's payload; scenario-level
        # aggregation at commit 7 sums per-key across both sources).
        "stage_telemetry": _stage_telemetry,
    }


_SA_SNAPSHOT_FIELDS = (
    "term_section_id",
    "board_id",
    "day",
    "start_time",
    "end_time",
    "room",
    "is_locked",
)


def _snapshot_board_placements(board_id: int) -> list[dict]:
    """Capture a board's placements so an SA regression can be rolled back."""
    return list(
        SectionPlacement.objects.filter(board_id=board_id)
        .order_by("id")
        .values(*_SA_SNAPSHOT_FIELDS)
    )


def _restore_board_placements(board_id: int, snapshot: list[dict]) -> None:
    """Restore a board's placement snapshot inside one transaction."""
    from django.db import transaction

    with transaction.atomic():
        SectionPlacement.objects.filter(board_id=board_id).delete()
        SectionPlacement.objects.bulk_create(
            [SectionPlacement(**row) for row in snapshot], batch_size=500
        )


def _sa_scenario_score(scenario_id: int, profiles, rigidity, candidate_id: str):
    """Lexicographic student-assignment score of the scenario's DB placements.

    Returns the score tuple, or ``None`` when there is nothing to evaluate.
    This is the SAME objective the V2 pipeline ranks candidates by, so the SA
    gate optimises (or at least never regresses) the canonical objective rather
    than SA's private gap-cost.
    """
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    states = build_section_states_for_scenario(scenario_id)
    if not states:
        return None
    evaluation = evaluate_generated_timetable_candidate(
        candidate_id=candidate_id,
        generated_sections=states,
        student_profiles=profiles,
        course_rigidity=rigidity,
    )
    return tuple(evaluation.lexicographic_score)


def optimize_and_persist_board(board_id: int, max_seconds: float = 8.0) -> dict:
    """Optimize and update placements in the database.

    Runs simulated annealing in memory, then persists the result — but only if
    the full student-assignment evaluator confirms SA did not regress the
    student outcome versus the greedy/CP-SAT baseline (WS-B evaluator gate).
    On regression the baseline is restored. The gate is inactive when the
    scenario has no student profiles (nothing to protect).
    """
    result = optimize_board(board_id, max_seconds=max_seconds)

    if result["status"] != "optimized":
        return result

    schedule = result["schedule"]
    sections = result["sections"]

    try:
        board = DeliveryBoard.objects.get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return result

    # ── SA evaluator gate (WS-B) — capture the pre-SA baseline ──────
    scenario_id = board.scenario_id
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_student_profiles_for_scenario,
    )

    _gate_profiles = build_student_profiles_for_scenario(scenario_id)
    _gate_rigidity = build_course_rigidity_for_scenario(scenario_id) if _gate_profiles else {}
    _baseline_score = None
    _baseline_snapshot: list[dict] = []
    if _gate_profiles:
        _baseline_score = _sa_scenario_score(
            scenario_id, _gate_profiles, _gate_rigidity, "sa_baseline"
        )
        _baseline_snapshot = _snapshot_board_placements(board_id)

    # Delete all current placements + meetings for auto sections on this board.
    # When lock enforcement is on, locked sections are preserved untouched
    # (never deleted, never re-persisted) so a registrar lock survives the SA
    # polish — get_or_create recreates default to is_locked=False.
    from core.services.timetable_validation import is_lock_enforcement_enabled

    locks_on = is_lock_enforcement_enabled()
    locked_ts_ids: set[int] = set()
    if locks_on:
        locked_ts_ids = set(
            SectionPlacement.objects.filter(board=board, is_locked=True).values_list(
                "term_section_id", flat=True
            )
        )

    auto_placements = SectionPlacement.objects.filter(
        board=board, term_section__source_tag="tw_auto"
    )
    if locks_on:
        auto_placements = auto_placements.exclude(is_locked=True)
    ts_ids = set(auto_placements.values_list("term_section_id", flat=True))
    auto_placements.delete()

    # Delete old meetings for these term sections
    for ts_id in ts_ids:
        TermSectionMeeting.objects.filter(term_section_id=ts_id).delete()

    # Recreate from optimized schedule
    for i, meetings in schedule.items():  # noqa: B007
        sec = sections[i]
        ts_id = sec["term_section_id"]
        if ts_id in locked_ts_ids:
            continue
        try:
            ts = TermSection.objects.get(id=ts_id)
        except TermSection.DoesNotExist:
            continue

        for m in meetings:
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

    # Assign rooms after re-persisting (annealing may have moved sections)
    from core.services.timetable_rooming import assign_rooms_to_board

    assign_rooms_to_board(board_id, respect_locked=locks_on)

    # ── SA evaluator gate (WS-B) — roll back a regression ───────────
    # If SA strictly worsened the canonical student-assignment score, restore
    # the greedy/CP-SAT baseline so SA can never persist a worse student
    # outcome by chasing its private gap-cost.
    if _baseline_score is not None:
        _after_score = _sa_scenario_score(scenario_id, _gate_profiles, _gate_rigidity, "sa_after")
        if _after_score is not None and _after_score > _baseline_score:
            _restore_board_placements(board_id, _baseline_snapshot)
            assign_rooms_to_board(board_id, respect_locked=locks_on)
            result["sa_evaluator_rolled_back"] = True
            result["sa_baseline_score"] = list(_baseline_score)
            result["sa_regressed_score"] = list(_after_score)
        else:
            result["sa_evaluator_rolled_back"] = False

    return result


def optimize_scenario(scenario_id: int, max_seconds_per_board: float = 5.0) -> dict:
    """Optimize all boards in a scenario using simulated annealing."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_before = 0
    total_after = 0

    for board in boards:
        r = optimize_and_persist_board(board.id, max_seconds=max_seconds_per_board)
        results[board.label] = r
        if r.get("cost_before") is not None:
            total_before += r["cost_before"]
            total_after += r["cost_after"]

    return {
        "boards": results,
        "total_cost_before": total_before,
        "total_cost_after": total_after,
        "improvement_pct": ((total_before - total_after) / max(total_before, 1)) * 100,
    }
