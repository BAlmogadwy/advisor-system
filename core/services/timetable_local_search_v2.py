"""
core/services/timetable_local_search_v2.py
Aggressive diagnostic-driven local search for timetable improvement.

Generates moves across ALL sections (not just hotspots), supports
multi-pass iteration, pairwise swaps, and best-improvement selection.
"""

from __future__ import annotations

import logging

from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RoomOccupancy,
    RoomProfile,
    SectionState,
    StudentProfile,
    TimetableEvaluationResult,
    TimetableMove,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_room_repair import (
    apply_move_to_grid,
    rollback_move,
    try_repair_rooms_locally,
)

logger = logging.getLogger(__name__)


def generate_all_repattern_moves(
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    priority_sections: set[str] | None = None,
) -> list[TimetableMove]:
    """Generate repattern moves for ALL sections (or priority subset).

    Each move tries an alternative time pattern from the same family.
    """
    moves: list[TimetableMove] = []
    targets = priority_sections if priority_sections else set(sections_by_id.keys())

    for sec_id in sorted(targets):
        sec = sections_by_id.get(sec_id)
        if not sec:
            continue
        family_patterns = pattern_catalog.get(sec.pattern_family, [])
        for alt in family_patterns:
            if alt.pattern_id != sec.pattern_id:
                moves.append(
                    TimetableMove(
                        move_type="repattern",
                        section_id_a=sec_id,
                        from_pattern_id_a=sec.pattern_id,
                        to_pattern_id_a=alt.pattern_id,
                    )
                )
    return moves


def generate_swap_moves(
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    hotspot_courses: list[str],
) -> list[TimetableMove]:
    """Generate pairwise swap moves between hotspot sections and others.

    Swaps the time pattern of a hotspot section with another section
    from a different course in the same pattern family.
    """
    moves: list[TimetableMove] = []
    hotspot_set = set(hotspot_courses[:10])

    # Group sections by pattern_family
    family_sections: dict[str, list[str]] = {}
    for sec_id, sec in sections_by_id.items():
        if sec.pattern_family:
            family_sections.setdefault(sec.pattern_family, []).append(sec_id)

    for family, sec_ids in family_sections.items():
        if len(sec_ids) < 2:
            continue
        family_pats = pattern_catalog.get(family, [])
        if len(family_pats) < 2:
            continue

        for i, sid_a in enumerate(sec_ids):
            sec_a = sections_by_id[sid_a]
            if sec_a.course_code not in hotspot_set:
                continue
            for sid_b in sec_ids[i + 1 :]:
                sec_b = sections_by_id[sid_b]
                if sec_a.course_code == sec_b.course_code:
                    continue
                if sec_a.pattern_id == sec_b.pattern_id:
                    continue
                # Swap: A gets B's pattern, B gets A's pattern
                moves.append(
                    TimetableMove(
                        move_type="swap",
                        section_id_a=sid_a,
                        from_pattern_id_a=sec_a.pattern_id,
                        to_pattern_id_a=sec_b.pattern_id,
                        section_id_b=sid_b,
                        from_pattern_id_b=sec_b.pattern_id,
                        to_pattern_id_b=sec_a.pattern_id,
                    )
                )
    return moves


def _rollback(
    snapshot,
    sections_by_id: dict[str, SectionState],
    room_occupancies: dict[str, RoomOccupancy] | None,
) -> None:
    """Rollback a move, handling both room-aware and roomless cases."""
    if room_occupancies is not None:
        rollback_move(snapshot, sections_by_id, room_occupancies)
    else:
        for snap in snapshot.snapshots:
            sec = sections_by_id[snap.section_id]
            sec.pattern_id = snap.old_pattern_id
            sec.meetings = list(snap.old_meetings)
            sec.assigned_room_id = snap.old_room_id


def diagnostic_driven_local_search(
    best_candidate: TimetableEvaluationResult,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    rooms_by_id: dict[str, RoomProfile] | None = None,
    room_occupancies: dict[str, RoomOccupancy] | None = None,
    course_room_requirements: dict[str, str] | None = None,
    max_iterations: int = 50,
) -> TimetableEvaluationResult:
    """Multi-pass local search with aggressive move generation.

    Strategy:
      Pass 1: Repattern hotspot + reserve-heavy sections (focused)
      Pass 2: Pairwise swaps between hotspot and non-hotspot sections
      Pass 3: Repattern ALL sections (exhaustive)

    Uses best-improvement: evaluates ALL moves in the neighborhood,
    picks the single best improving move, then regenerates.
    """
    current_best = best_candidate
    current_score = best_candidate.lexicographic_score
    total_improvements = 0

    for iteration in range(max_iterations):
        # Build priority set from current diagnostics
        hotspot_courses = current_best.hotspot_courses
        reserve_heavy = [sid for sid, _ratio in current_best.reserve_heavy_sections]

        priority_sections = set(reserve_heavy)
        for course_code in hotspot_courses[:10]:
            for sec in sections_by_id.values():
                if sec.course_code == course_code:
                    priority_sections.add(sec.section_id)

        # Generate moves in phases: focused first, then broader
        all_moves: list[TimetableMove] = []

        # Phase 1: Repattern priority sections (focused — most improvements come from here)
        all_moves.extend(
            generate_all_repattern_moves(sections_by_id, pattern_catalog, priority_sections)
        )
        # Phase 2: Pairwise swaps — helps when two courses block each other;
        # exchanging their patterns can unblock both simultaneously
        all_moves.extend(generate_swap_moves(sections_by_id, pattern_catalog, hotspot_courses))
        # Phase 3 (after iteration 5): Exhaustive — try ALL sections.
        # Expensive but catches improvements the focused phases miss.
        if iteration >= 5:
            all_moves.extend(generate_all_repattern_moves(sections_by_id, pattern_catalog, None))

        if not all_moves:
            logger.info("No moves available at iteration %d, stopping", iteration)
            break

        # Best-improvement strategy: try ALL moves before committing.
        # This avoids greedy traps where the first improving move leads
        # to a worse neighbourhood than a different improving move.
        best_move_result: TimetableEvaluationResult | None = None
        best_move_score = current_score
        best_move_snapshot = None
        moves_tried = 0

        for move in all_moves:
            snapshot = apply_move_to_grid(move, sections_by_id, pattern_catalog)

            # Room feasibility check
            if rooms_by_id is not None and room_occupancies is not None:
                room_ok = try_repair_rooms_locally(
                    snapshot,
                    sections_by_id,
                    rooms_by_id,
                    room_occupancies,
                    course_room_requirements or {},
                )
                if not room_ok:
                    _rollback(snapshot, sections_by_id, room_occupancies)
                    continue

            # Evaluate
            test_result = evaluate_generated_timetable_candidate(
                candidate_id="ls_test",
                generated_sections=list(sections_by_id.values()),
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
            )
            moves_tried += 1

            if test_result.lexicographic_score < best_move_score:
                # This is the best so far — but rollback for now,
                # we'll re-apply the winner after trying all moves
                if best_move_snapshot is not None:
                    pass  # previous best was already rolled back
                best_move_result = test_result
                best_move_score = test_result.lexicographic_score
                best_move_snapshot = (move, snapshot)

            # Rollback this move to try the next
            _rollback(snapshot, sections_by_id, room_occupancies)

        if best_move_result is not None and best_move_snapshot is not None:
            # Re-apply the winning move
            winning_move, _ = best_move_snapshot
            apply_move_to_grid(winning_move, sections_by_id, pattern_catalog)
            if rooms_by_id is not None and room_occupancies is not None:
                try_repair_rooms_locally(
                    _,
                    sections_by_id,
                    rooms_by_id,
                    room_occupancies,
                    course_room_requirements or {},
                )

            current_best = best_move_result
            current_score = best_move_score
            total_improvements += 1
            logger.info(
                "Iteration %d: improved score %s → %s (%d moves tried)",
                iteration,
                best_candidate.lexicographic_score,
                current_score,
                moves_tried,
            )
        else:
            logger.info(
                "Iteration %d: no improvement found (%d moves tried), stopping",
                iteration,
                moves_tried,
            )
            break

    logger.info(
        "Local search complete: %d improvements in %d iterations",
        total_improvements,
        min(iteration + 1, max_iterations) if "iteration" in dir() else 0,
    )
    return current_best
