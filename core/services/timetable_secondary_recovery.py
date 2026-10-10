"""Bounded seat/room recovery inside the already achieved instructor schedule.

This pass owns no persistence. It returns copied section states only after a
candidate preserves hard student outcomes, instructor days/idle and the other
secondary outcome. Its room oracle must evaluate physical meetings, not one
room per section. Seating uses the canonical evaluator so reported gains survive
normal reconstruction of student assignments.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from core.services.timetable_assignment_models import (
    CanonicalPattern,
    SectionState,
    StudentProfile,
    TimetableEvaluationResult,
    TimetableMove,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_constraints import (
    move_exceeds_instructor_daily_cap,
    move_introduces_instructor_clash,
    move_introduces_same_course_overlap,
)
from core.services.timetable_exact_rooming import ExactRoomingError
from core.services.timetable_local_search_v2 import (
    generate_all_repattern_moves,
    generate_swap_moves,
)
from core.services.timetable_optimisation_policy import (
    instructor_schedule_metrics,
    instructor_schedule_regressions,
    schedule_metrics_of,
)
from core.services.timetable_pr4_instructor import (
    get_instructor_daily_cap,
    is_instructor_daily_cap_enabled,
)
from core.services.timetable_room_repair import PatternNotInCatalog, apply_move_to_grid
from core.services.timetable_student_assignment import is_tiered_score

RoomEvaluator = Callable[[list[SectionState]], dict[str, Any]]


def soft_unresolved_count(evaluation: TimetableEvaluationResult) -> int:
    """Count the actual soft tier, keeping legacy unassigned-course semantics."""
    score = evaluation.lexicographic_score
    return int(score[5] if is_tiered_score(score) else score[2])


def _student_safety_ok(
    candidate: TimetableEvaluationResult, incumbent: TimetableEvaluationResult
) -> bool:
    old, new = incumbent.lexicographic_score, candidate.lexicographic_score
    return (
        len(old) == len(new)
        and all(a <= b for a, b in zip(new[:4], old[:4], strict=True))
        and candidate.instructor_overload_count <= incumbent.instructor_overload_count
    )


def _target_sections(
    evaluation: TimetableEvaluationResult,
    sections: dict[str, SectionState],
    room_result: dict[str, Any],
    course_tiers: dict[str, str] | None,
) -> tuple[set[str], Counter[str]]:
    """Include unresolved courses AND their students' currently seated blockers."""
    pressure: Counter[str] = Counter()
    targets = _recoverable_room_targets(room_result, sections)
    for state in evaluation.assignment_states.values():
        unresolved = [
            course
            for course in state.unresolved_courses
            if not course_tiers or course_tiers.get(course, "T1") in {"T2", "T3"}
        ]
        if not unresolved:
            continue
        pressure.update(unresolved)
        targets.update(state.section_ids)
    targets.update(sid for sid, section in sections.items() if section.course_code in pressure)
    return targets, pressure


def _recoverable_room_targets(
    room_result: dict[str, Any],
    sections: dict[str, SectionState] | None = None,
) -> set[str]:
    if "recoverable_section_ids" in room_result:
        return set(room_result["recoverable_section_ids"])
    failures = room_result.get("failures")
    if failures:
        # Moving a meeting cannot create a larger lab or change a locked room.
        # Focus the time search on shortages caused by simultaneous demand.
        targets = {
            failure["section_id"]
            for failure in failures
            if failure.get("reason") == "ROOM_OCCUPIED"
        }
        for failure in failures:
            if failure.get("reason") == "ROOM_OCCUPIED":
                targets.update(failure.get("occupied_blocker_section_ids") or ())
        if sections:
            # A generic oracle may only name the stranded section. Include
            # overlapping blockers conservatively; the production oracle also
            # filters these by their strict compatible room domains.
            failed_meetings = [
                meeting for sid in targets if sid in sections for meeting in sections[sid].meetings
            ]
            targets.update(
                sid
                for sid, section in sections.items()
                if any(
                    a.day == b.day and a.start_min < b.end_min and b.start_min < a.end_min
                    for a in section.meetings
                    for b in failed_meetings
                )
            )
        return targets
    return set(room_result.get("unassigned_section_ids") or [])


def recover_secondary_quality(
    *,
    current_eval: TimetableEvaluationResult,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    room_evaluator: RoomEvaluator,
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
    locked_section_ids: set[str] | None = None,
    time_limit_seconds: float = 45.0,
    max_iterations: int = 12,
    max_evaluations: int = 3000,
    first_improvement: bool = True,
    max_polish_iterations: int = 4,
    room_shortage_target: int | None = None,
) -> dict[str, Any]:
    """Return a Pareto improvement in soft seats and physical room shortages.

    Input sections and the database remain unchanged. Every committed move
    ratchets both secondary counts and each teacher's days/idle. The deadline is
    checked between trials; callers must also bound their room oracle's solve.
    A prior-run room target prioritizes congestion recovery until that floor is
    reached; it never relaxes teacher, seating or room nonregression guards.
    """
    started = time.monotonic()
    deadline = started + max(0.0, time_limit_seconds)
    sections = deepcopy(sections_by_id)
    current = current_eval
    room_current = room_evaluator(list(sections.values()))
    baseline_room = int(room_current["unassigned_count"])
    baseline_soft = soft_unresolved_count(current)
    baseline_teachers = instructor_schedule_metrics(sections.values(), section_instructor_ids)
    locked = set(locked_section_ids or ()) | {
        sid for sid, section in sections.items() if section.delivery_is_online or section.is_online
    }
    evaluations = structural_rejections = instructor_rejections = room_evaluations = 0
    unrelated_room_trials_skipped = 0
    accepted: list[dict[str, Any]] = []
    stop_reason = "iteration_limit"
    visited = {tuple((sid, s.pattern_id) for sid, s in sorted(sections.items()))}
    phase = "recovery"
    recovery_iterations = polish_iterations = 0
    iteration = 0
    while True:
        soft_before = soft_unresolved_count(current)
        rooms_before = int(room_current["unassigned_count"])
        if time.monotonic() >= deadline or evaluations >= max_evaluations:
            stop_reason = "time_limit" if time.monotonic() >= deadline else "evaluation_limit"
            break
        if phase == "recovery" and (
            recovery_iterations >= max_iterations or (soft_before == 0 and rooms_before == 0)
        ):
            phase = "polish"
        if phase == "polish" and polish_iterations >= max_polish_iterations:
            stop_reason = "polish_iteration_limit"
            break
        teacher_ceiling = instructor_schedule_metrics(sections.values(), section_instructor_ids)
        targets, pressure = _target_sections(current, sections, room_current, course_tiers)
        if phase == "polish":
            for state in current.assignment_states.values():
                if state.total_gap_minutes > 0:
                    targets.update(state.section_ids)
                    pressure.update(
                        {
                            sections[sid].course_code: state.total_gap_minutes
                            for sid in state.section_ids
                            if sid in sections
                        }
                    )
        if not targets:
            if phase == "recovery":
                phase = "polish"
                continue
            stop_reason = "no_polish_targets"
            break
        iteration += 1
        if phase == "recovery":
            recovery_iterations += 1
        else:
            polish_iterations += 1
        moves = generate_all_repattern_moves(sections, pattern_catalog, targets, locked)
        room_targets = _recoverable_room_targets(room_current, sections)
        hotspot_courses = list(
            dict.fromkeys(
                [sections[sid].course_code for sid in sorted(room_targets) if sid in sections]
                + [course for course, _count in pressure.most_common()]
            )
        )
        moves += generate_swap_moves(sections, pattern_catalog, hotspot_courses, locked)

        def move_priority(
            move: TimetableMove,
            targets_for_rooms: set[str] = room_targets,
            course_pressure: Counter[str] = pressure,
            prioritize_rooms: bool = (
                room_shortage_target is not None and rooms_before > room_shortage_target
            ),
        ) -> tuple:
            ids = {move.section_id_a}
            if move.section_id_b:
                ids.add(move.section_id_b)
            return (
                not bool(ids & targets_for_rooms) if prioritize_rooms else False,
                bool(any((section_instructor_ids or {}).get(sid) for sid in ids)),
                not bool(ids & targets_for_rooms),
                -sum(course_pressure[sections[sid].course_code] for sid in ids),
                move.move_type == "swap",
                move.section_id_a,
            )

        best: tuple | None = None
        seen: set[TimetableMove] = set()
        for move in sorted(moves, key=move_priority):
            if time.monotonic() >= deadline or evaluations >= max_evaluations:
                stop_reason = "time_limit" if time.monotonic() >= deadline else "evaluation_limit"
                break
            if move in seen:
                continue
            seen.add(move)
            try:
                snapshot = apply_move_to_grid(move, sections, pattern_catalog)
            except PatternNotInCatalog:
                continue
            try:
                moved = {move.section_id_a}
                if move.section_id_b:
                    moved.add(move.section_id_b)
                signature = tuple((sid, s.pattern_id) for sid, s in sorted(sections.items()))
                if signature in visited:
                    continue
                if (
                    move_introduces_same_course_overlap(sections, moved)
                    or (
                        section_instructor_ids
                        and move_introduces_instructor_clash(
                            sections, section_instructor_ids, moved
                        )
                    )
                    or (
                        section_instructor_ids
                        and is_instructor_daily_cap_enabled()
                        and move_exceeds_instructor_daily_cap(
                            sections, section_instructor_ids, get_instructor_daily_cap(), moved
                        )
                    )
                ):
                    structural_rejections += 1
                    continue
                teachers = instructor_schedule_metrics(sections.values(), section_instructor_ids)
                if instructor_schedule_regressions(
                    teacher_ceiling, teachers
                ) or instructor_schedule_regressions(baseline_teachers, teachers):
                    instructor_rejections += 1
                    continue
                candidate = evaluate_generated_timetable_candidate(
                    f"secondary_{iteration}_{evaluations}",
                    list(sections.values()),
                    student_profiles,
                    course_rigidity,
                    section_instructor_ids,
                    course_tiers,
                )
                evaluations += 1
                candidate_soft = soft_unresolved_count(candidate)
                if not _student_safety_ok(candidate, current) or candidate_soft > soft_before:
                    continue
                student_improvement = candidate.lexicographic_score < current.lexicographic_score
                if candidate_soft == soft_before and not (
                    phase == "polish" and student_improvement
                ):
                    if rooms_before == 0 or not (moved & room_targets):
                        unrelated_room_trials_skipped += 1
                        continue
                try:
                    room_candidate = room_evaluator(list(sections.values()))
                except ExactRoomingError:
                    structural_rejections += 1
                    continue
                room_evaluations += 1
                candidate_rooms = int(room_candidate["unassigned_count"])
                same_counts = candidate_soft == soft_before and candidate_rooms == rooms_before
                if candidate_rooms > rooms_before or (
                    same_counts and not (phase == "polish" and student_improvement)
                ):
                    continue
                key = (candidate_soft, candidate_rooms, candidate.lexicographic_score)
                if best is None or key < best[0]:
                    best = (key, move, candidate, room_candidate, signature)
                    if first_improvement:
                        break
            finally:
                for previous in snapshot.snapshots:
                    section = sections[previous.section_id]
                    section.meetings = list(previous.old_meetings)
                    section.pattern_id = previous.old_pattern_id
                    section.assigned_room_id = previous.old_room_id
        if best is None:
            if stop_reason in {"time_limit", "evaluation_limit"}:
                break
            if phase == "recovery":
                phase = "polish"
                continue
            stop_reason = "no_safe_polish_improvement"
            break
        _key, move, current, room_current, signature = best
        apply_move_to_grid(move, sections, pattern_catalog)
        visited.add(signature)
        accepted.append(
            {
                "section_id": move.section_id_a,
                "partner_section_id": move.section_id_b,
                "move_type": move.move_type,
                "soft_before": soft_before,
                "soft_after": soft_unresolved_count(current),
                "rooms_before": rooms_before,
                "rooms_after": int(room_current["unassigned_count"]),
                "phase": phase,
            }
        )

    report = {
        "applied": bool(accepted),
        "accepted_moves": accepted,
        "soft_unresolved_before": baseline_soft,
        "soft_unresolved_after": soft_unresolved_count(current),
        "room_shortages_before": baseline_room,
        "room_shortages_after": int(room_current["unassigned_count"]),
        "room_shortage_target": room_shortage_target,
        "instructor_before": baseline_teachers,
        "instructor_after": schedule_metrics_of(current),
        "score_before": list(current_eval.lexicographic_score),
        "score_after": list(current.lexicographic_score),
        "student_gap_minutes_before": sum(
            state.total_gap_minutes for state in current_eval.assignment_states.values()
        ),
        "student_gap_minutes_after": sum(
            state.total_gap_minutes for state in current.assignment_states.values()
        ),
        "evaluations": evaluations,
        "room_evaluations": room_evaluations,
        "unrelated_room_trials_skipped": unrelated_room_trials_skipped,
        "recovery_iterations": recovery_iterations,
        "polish_iterations": polish_iterations,
        "structural_rejections": structural_rejections,
        "instructor_rejections": instructor_rejections,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stop_reason": stop_reason,
    }
    return {
        "eval": current,
        "improved_sections": list(sections.values()),
        "room_evaluation": room_current,
        "report": report,
    }
