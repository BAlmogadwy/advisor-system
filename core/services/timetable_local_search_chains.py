"""
core/services/timetable_local_search_chains.py
Diagnostic-driven chain-2 local search for cross-board improvements.

Finds coordinated 2-section moves that single-move search cannot discover:
  "Move section A to pattern X AND section B to pattern Y"

Only generates chains from diagnostic signals — hotspot courses and their
overlap partners — to keep the neighbourhood tractable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import product

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
from core.services.timetable_room_repair import apply_move_to_grid, rollback_move

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainMove:
    """A coordinated 2-section move applied atomically."""

    move_a: TimetableMove
    move_b: TimetableMove


def _find_overlap_partners(
    hotspot_courses: list[str],
    student_profiles: dict[str, StudentProfile],
    top_n: int = 5,
) -> dict[str, list[str]]:
    """For each hotspot course, find the courses that share the most students.

    Overlap partners are courses that share students with a hotspot.
    If CS211 is a hotspot and MATH203 shares 15 students with it,
    moving MATH203 to a different pattern might free a slot for CS211.
    We only try the top-N partners to keep the neighbourhood tractable
    (without this limit, chain-2 combinations explode combinatorially).

    Returns {hotspot_code: [partner_code_1, partner_code_2, ...]}.
    """
    # Build course → student_ids mapping
    course_students: dict[str, set[str]] = {}
    for profile in student_profiles.values():
        for code in profile.recommended_courses:
            course_students.setdefault(code, set()).add(profile.student_id)

    partners: dict[str, list[str]] = {}
    hotspot_set = set(hotspot_courses)
    all_courses = list(course_students.keys())

    for hc in hotspot_courses:
        hc_students = course_students.get(hc, set())
        if not hc_students:
            continue
        overlaps: list[tuple[str, int]] = []
        for other in all_courses:
            if other == hc or other in hotspot_set:
                continue
            shared = len(hc_students & course_students.get(other, set()))
            if shared > 0:
                overlaps.append((other, shared))
        overlaps.sort(key=lambda x: -x[1])
        partners[hc] = [code for code, _ in overlaps[:top_n]]

    return partners


def generate_chain_2_moves(
    hotspot_courses: list[str],
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    max_alternatives_per_section: int = 5,
) -> list[ChainMove]:
    """Generate chain-2 moves: hotspot section + overlap partner section.

    Pruning strategy:
      - Only hotspot courses (top 5) as move_a
      - Only their top overlap partners as move_b
      - Only top N alternative patterns per section (by pattern diversity)
    """
    chains: list[ChainMove] = []
    partners = _find_overlap_partners(hotspot_courses[:5], student_profiles, top_n=5)

    if not partners:
        return chains

    # Group sections by course
    sections_by_course: dict[str, list[str]] = {}
    for sec_id, sec in sections_by_id.items():
        sections_by_course.setdefault(sec.course_code, []).append(sec_id)

    for hotspot_code, partner_codes in partners.items():
        hotspot_sec_ids = sections_by_course.get(hotspot_code, [])
        if not hotspot_sec_ids:
            continue

        for partner_code in partner_codes:
            partner_sec_ids = sections_by_course.get(partner_code, [])
            if not partner_sec_ids:
                continue

            # Get alternative patterns for hotspot sections
            for h_sid in hotspot_sec_ids:
                h_sec = sections_by_id[h_sid]
                h_alts = [
                    p
                    for p in pattern_catalog.get(h_sec.pattern_family, [])
                    if p.pattern_id != h_sec.pattern_id
                ][:max_alternatives_per_section]

                if not h_alts:
                    continue

                # Get alternative patterns for partner sections
                for p_sid in partner_sec_ids:
                    p_sec = sections_by_id[p_sid]
                    p_alts = [
                        p
                        for p in pattern_catalog.get(p_sec.pattern_family, [])
                        if p.pattern_id != p_sec.pattern_id
                    ][:max_alternatives_per_section]

                    if not p_alts:
                        continue

                    # Generate all combinations
                    for h_alt, p_alt in product(h_alts, p_alts):
                        chains.append(
                            ChainMove(
                                move_a=TimetableMove(
                                    move_type="repattern",
                                    section_id_a=h_sid,
                                    from_pattern_id_a=h_sec.pattern_id,
                                    to_pattern_id_a=h_alt.pattern_id,
                                ),
                                move_b=TimetableMove(
                                    move_type="repattern",
                                    section_id_a=p_sid,
                                    from_pattern_id_a=p_sec.pattern_id,
                                    to_pattern_id_a=p_alt.pattern_id,
                                ),
                            )
                        )

    logger.info(
        "Generated %d chain-2 moves from %d hotspot courses",
        len(chains),
        len(partners),
    )
    return chains


def _apply_chain(
    chain: ChainMove,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
):
    """Apply both moves of a chain. Returns (snapshot_a, snapshot_b)."""
    snap_a = apply_move_to_grid(chain.move_a, sections_by_id, pattern_catalog)
    snap_b = apply_move_to_grid(chain.move_b, sections_by_id, pattern_catalog)
    return snap_a, snap_b


def _rollback_chain(
    snap_a,
    snap_b,
    sections_by_id: dict[str, SectionState],
    room_occupancies: dict[str, RoomOccupancy] | None,
):
    """Rollback both moves in reverse order."""
    if room_occupancies is not None:
        rollback_move(snap_b, sections_by_id, room_occupancies)
        rollback_move(snap_a, sections_by_id, room_occupancies)
    else:
        for snap in (snap_b, snap_a):
            for s in snap.snapshots:
                sec = sections_by_id[s.section_id]
                sec.pattern_id = s.old_pattern_id
                sec.meetings = list(s.old_meetings)
                sec.assigned_room_id = s.old_room_id


def chain_local_search(
    best_candidate: TimetableEvaluationResult,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    rooms_by_id: dict[str, RoomProfile] | None = None,
    room_occupancies: dict[str, RoomOccupancy] | None = None,
    course_room_requirements: dict[str, str] | None = None,
    max_iterations: int = 10,
) -> TimetableEvaluationResult:
    """Chain-2 local search — runs AFTER single-move search exhausts.

    For each iteration:
      1. Generate chain-2 moves from current diagnostics
      2. Evaluate each chain (apply both, evaluate, rollback both)
      3. Keep the best improving chain
      4. Repeat until no improvement or max_iterations
    """
    current_best = best_candidate
    current_score = best_candidate.lexicographic_score
    total_improvements = 0

    for iteration in range(max_iterations):
        hotspot_courses = current_best.hotspot_courses
        if not hotspot_courses:
            logger.info("Chain search: no hotspot courses at iteration %d", iteration)
            break

        chains = generate_chain_2_moves(
            hotspot_courses,
            sections_by_id,
            pattern_catalog,
            student_profiles,
        )

        if not chains:
            logger.info("Chain search: no chain moves available at iteration %d", iteration)
            break

        # Best-improvement: try all chains
        best_chain_result: TimetableEvaluationResult | None = None
        best_chain_score = current_score
        best_chain: ChainMove | None = None
        chains_tried = 0

        for chain in chains:
            snap_a, snap_b = _apply_chain(chain, sections_by_id, pattern_catalog)

            # Evaluate
            test_result = evaluate_generated_timetable_candidate(
                candidate_id="chain_test",
                generated_sections=list(sections_by_id.values()),
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
            )
            chains_tried += 1

            if test_result.lexicographic_score < best_chain_score:
                best_chain_result = test_result
                best_chain_score = test_result.lexicographic_score
                best_chain = chain

            # Rollback
            _rollback_chain(snap_a, snap_b, sections_by_id, room_occupancies)

        if best_chain is not None and best_chain_result is not None:
            # Re-apply winning chain permanently
            _apply_chain(best_chain, sections_by_id, pattern_catalog)
            current_best = best_chain_result
            current_score = best_chain_score
            total_improvements += 1
            logger.info(
                "Chain iteration %d: improved %s -> %s (%d chains tried)",
                iteration,
                best_candidate.lexicographic_score,
                current_score,
                chains_tried,
            )
        else:
            logger.info(
                "Chain iteration %d: no improvement (%d chains tried)",
                iteration,
                chains_tried,
            )
            break

    logger.info("Chain search complete: %d improvements", total_improvements)
    return current_best
