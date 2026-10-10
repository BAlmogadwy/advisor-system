"""
core/services/timetable_local_search_v2.py
Aggressive diagnostic-driven local search for timetable improvement.

Generates moves across ALL sections (not just hotspots), supports
multi-pass iteration, pairwise swaps, and best-improvement selection.
"""

from __future__ import annotations

import logging

from django.conf import settings

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
from core.services.timetable_constraints import (
    move_exceeds_instructor_daily_cap,
    move_introduces_instructor_clash,
    move_introduces_same_course_overlap,
)
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_optimisation_policy import (
    evaluation_is_better,
    instructor_focus_courses,
    schedule_metrics_of,
)
from core.services.timetable_pr4_instructor import (
    get_instructor_daily_cap,
    is_instructor_clash_enabled,
    is_instructor_daily_cap_enabled,
)
from core.services.timetable_room_repair import (
    PatternNotInCatalog,
    apply_move_to_grid,
    rollback_move,
    try_repair_rooms_locally,
)

logger = logging.getLogger(__name__)


def _pattern_preserves_contact_time(
    section: SectionState, pattern: CanonicalPattern | None
) -> bool:
    """A move changes the slot, while retaining family and each meeting duration."""
    return (
        pattern is not None
        and (
            not (policy := get_course_meeting_policy(section.course_code))
            or policy.accepts(pattern.meetings)
        )
        and pattern.pattern_family == section.pattern_family
        and sorted(meeting.end_min - meeting.start_min for meeting in pattern.meetings)
        == sorted(meeting.end_min - meeting.start_min for meeting in section.meetings)
    )


def generate_all_repattern_moves(
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    priority_sections: set[str] | None = None,
    locked_section_ids: set[str] | None = None,
) -> list[TimetableMove]:
    """Generate repattern moves for ALL sections (or priority subset).

    Each move tries an alternative time pattern from the same family.
    """
    moves: list[TimetableMove] = []
    targets = priority_sections if priority_sections else set(sections_by_id.keys())
    locked = (locked_section_ids or set()) | {
        sid
        for sid, section in sections_by_id.items()
        if section.is_online or getattr(section, "delivery_is_online", False)
    }

    for sec_id in sorted(targets):
        if sec_id in locked:
            continue
        sec = sections_by_id.get(sec_id)
        if not sec:
            continue
        family_patterns = pattern_catalog.get(sec.pattern_family, [])
        for alt in family_patterns:
            if alt.pattern_id != sec.pattern_id and _pattern_preserves_contact_time(sec, alt):
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
    locked_section_ids: set[str] | None = None,
) -> list[TimetableMove]:
    """Generate pairwise swap moves between hotspot sections and others.

    Swaps the time pattern of a hotspot section with another section
    from a different course in the same pattern family.
    """
    moves: list[TimetableMove] = []
    hotspot_set = set(hotspot_courses[:10])
    locked = (locked_section_ids or set()) | {
        sid
        for sid, section in sections_by_id.items()
        if section.is_online or getattr(section, "delivery_is_online", False)
    }

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
        # Match apply_move_to_grid's first matching catalog entry. Imported
        # placements can carry a known family with noncanonical contact times.
        patterns_by_id: dict[str, CanonicalPattern] = {}
        for pattern in family_pats:
            patterns_by_id.setdefault(pattern.pattern_id, pattern)

        for i, sid_a in enumerate(sec_ids):
            if sid_a in locked:
                continue
            sec_a = sections_by_id[sid_a]
            for sid_b in sec_ids[i + 1 :]:
                if sid_b in locked:
                    continue
                sec_b = sections_by_id[sid_b]
                if sec_a.course_code not in hotspot_set and sec_b.course_code not in hotspot_set:
                    continue
                if sec_a.course_code == sec_b.course_code:
                    continue
                if sec_a.pattern_id == sec_b.pattern_id:
                    continue
                if not (
                    _pattern_preserves_contact_time(sec_a, patterns_by_id.get(sec_b.pattern_id))
                    and _pattern_preserves_contact_time(sec_b, patterns_by_id.get(sec_a.pattern_id))
                ):
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
    locked_section_ids: set[str] | None = None,
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
) -> TimetableEvaluationResult:
    """Multi-pass local search with aggressive move generation.

    Strategy:
      Pass 1: Repattern hotspot + reserve-heavy sections (focused)
      Pass 2: Pairwise swaps between hotspot and non-hotspot sections
      Pass 3: Repattern ALL sections (exhaustive)

    For small scenarios (<= 80 sections): best-improvement (try ALL moves,
    pick the best). For large scenarios: first-improvement (accept the
    first improving move immediately) — 5-10x faster.
    """
    import time as _time

    current_best = best_candidate
    current_score = best_candidate.lexicographic_score
    total_improvements = 0

    # Large scenarios: switch to first-improvement to avoid multi-minute
    # iterations. 80 sections is the threshold (~45 courses × 2 sections).
    use_first_improvement = len(sections_by_id) > 80
    locked = (locked_section_ids or set()) | {
        sid
        for sid, section in sections_by_id.items()
        if section.is_online or getattr(section, "delivery_is_online", False)
    }

    # Time budget: stop after 3 minutes regardless of iteration count.
    # Prevents the server from freezing on large scenarios.
    time_budget_seconds = max(
        0.0, float(getattr(settings, "TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS", 180.0))
    )
    deadline = _time.monotonic() + time_budget_seconds

    if use_first_improvement:
        logger.info(
            "Large scenario (%d sections) — using first-improvement strategy (%.0fs budget)",
            len(sections_by_id),
            time_budget_seconds,
        )

    for iteration in range(max_iterations):
        # Time budget check
        if _time.monotonic() >= deadline:
            logger.info(
                "Time budget exhausted at iteration %d, stopping",
                iteration,
            )
            break

        # Build priority set from current diagnostics
        hotspot_courses = current_best.hotspot_courses
        instructor_courses = instructor_focus_courses(
            sections_by_id.values(), section_instructor_ids
        )
        reserve_heavy = [sid for sid, _ratio in current_best.reserve_heavy_sections]

        priority_sections = set(reserve_heavy)
        for course_code in set(hotspot_courses[:10]) | set(instructor_courses):
            for sec in sections_by_id.values():
                if sec.course_code == course_code:
                    priority_sections.add(sec.section_id)

        # Generate moves in phases: focused first, then broader
        all_moves: list[TimetableMove] = []

        # Phase 1: Repattern priority sections (focused — most improvements come from here)
        all_moves.extend(
            generate_all_repattern_moves(
                sections_by_id,
                pattern_catalog,
                priority_sections,
                locked_section_ids=locked,
            )
        )
        # Phase 2: Pairwise swaps — helps when two courses block each other;
        # exchanging their patterns can unblock both simultaneously
        all_moves.extend(
            generate_swap_moves(
                sections_by_id,
                pattern_catalog,
                hotspot_courses,
                locked_section_ids=locked,
            )
        )
        if instructor_courses:
            all_moves.extend(
                generate_swap_moves(
                    sections_by_id,
                    pattern_catalog,
                    instructor_courses,
                    locked_section_ids=locked,
                )
            )
        # Best-improvement strategy: try ALL moves before committing.
        # This avoids greedy traps where the first improving move leads
        # to a worse neighbourhood than a different improving move.
        best_move_result: TimetableEvaluationResult | None = None
        best_move_score = current_score
        best_move_snapshot = None
        moves_tried = 0
        # Pin ceilings to the board at the start of this iteration, so trial
        # alternatives do not accidentally use one another's day distribution.
        day_ceilings = schedule_metrics_of(current_best)

        broad_moves_added = iteration >= 5
        if broad_moves_added:
            all_moves.extend(
                generate_all_repattern_moves(
                    sections_by_id, pattern_catalog, None, locked_section_ids=locked
                )
            )
        seen: set[TimetableMove] = set()
        move_index = 0
        while move_index < len(all_moves) or not broad_moves_added:
            # Check inside the neighbourhood as a single scan can contain
            # thousands of evaluations. Each completed trial has been rolled
            # back; an already discovered winner is still committed below.
            if _time.monotonic() >= deadline:
                logger.info("Time budget exhausted during move scan at iteration %d", iteration)
                break
            if move_index >= len(all_moves):
                if best_move_result is not None:
                    break
                # A stalled focused neighbourhood is not a local optimum.
                # Expand now, including when the focused set supplied no moves.
                all_moves.extend(
                    generate_all_repattern_moves(
                        sections_by_id, pattern_catalog, None, locked_section_ids=locked
                    )
                )
                broad_moves_added = True
                continue
            move = all_moves[move_index]
            move_index += 1
            if move in seen:
                continue
            seen.add(move)
            try:
                snapshot = apply_move_to_grid(move, sections_by_id, pattern_catalog)
            except PatternNotInCatalog:
                # Move generator proposed a pattern the canonical catalog
                # didn't enumerate (can happen when the adaptive placer
                # lands on a combination outside the enumerated space).
                # Skip this move and carry on rather than aborting the
                # whole optimiser run.
                continue

            # Moved section id(s) — the delta-scoped structural gates below only
            # reject violations THIS move introduces, so a pre-existing violation
            # elsewhere on the board can't block an unrelated improving move.
            _moved = {move.section_id_a}
            if move.section_id_b:
                _moved.add(move.section_id_b)

            # Same-course hard-reject: two sections of the same course cannot
            # overlap (the same instructor teaches all sections). Delta-scoped.
            if move_introduces_same_course_overlap(sections_by_id, _moved):
                _rollback(snapshot, sections_by_id, room_occupancies)
                continue

            # Instructor daily-session cap hard-reject (lectures + labs): reject
            # only when a moved section lands on an (instructor, day) cell that
            # now exceeds the cap.
            if (
                section_instructor_ids
                and is_instructor_daily_cap_enabled()
                and move_exceeds_instructor_daily_cap(
                    sections_by_id, section_instructor_ids, get_instructor_daily_cap(), _moved
                )
            ):
                _rollback(snapshot, sections_by_id, room_occupancies)
                continue

            # Instructor-clash hard-reject: an instructor can't teach two sections
            # whose times overlap (interval), for a clash this move introduces.
            if (
                section_instructor_ids
                and is_instructor_clash_enabled()
                and move_introduces_instructor_clash(sections_by_id, section_instructor_ids, _moved)
            ):
                _rollback(snapshot, sections_by_id, room_occupancies)
                continue

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
                section_instructor_ids=section_instructor_ids,
                course_tiers=course_tiers,
            )
            moves_tried += 1

            if evaluation_is_better(
                test_result,
                best_move_result or current_best,
                day_ceilings=day_ceilings,
                feasibility_ceilings=current_best,
            ):
                best_move_result = test_result
                best_move_score = test_result.lexicographic_score
                best_move_snapshot = (move, snapshot)

                # First-improvement: accept immediately, skip remaining moves
                if use_first_improvement:
                    _rollback(snapshot, sections_by_id, room_occupancies)
                    break

            # Rollback this move to try the next
            _rollback(snapshot, sections_by_id, room_occupancies)

        if best_move_result is not None and best_move_snapshot is not None:
            # Re-apply the winning move
            winning_move, _ = best_move_snapshot
            winning_snapshot = apply_move_to_grid(winning_move, sections_by_id, pattern_catalog)
            if rooms_by_id is not None and room_occupancies is not None:
                room_ok = try_repair_rooms_locally(
                    winning_snapshot,
                    sections_by_id,
                    rooms_by_id,
                    room_occupancies,
                    course_room_requirements or {},
                )
                if not room_ok:
                    _rollback(winning_snapshot, sections_by_id, room_occupancies)
                    break

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
