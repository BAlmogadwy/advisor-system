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
import time
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
from core.services.timetable_autoplace import WEEKDAYS
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_decision_trace import DecisionTrace
from core.services.timetable_room_repair import apply_move_to_grid, rollback_move
from core.services.timetable_solver_codes import CHAIN_ROTATED, is_stage_trace_enabled
from core.services.timetable_stage_telemetry import (
    is_stage_telemetry_enabled,
    record_stage_iterations,
    record_stage_ms,
)

logger = logging.getLogger(__name__)


def _fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _meeting_signature(sec: SectionState) -> tuple[tuple[int, int, int], ...]:
    return tuple((m.day, m.start_min, m.end_min) for m in sec.meetings)


def _section_code_key(sec: SectionState) -> str:
    label = sec.section_id
    prefix = f"{sec.course_code}_"
    if label.startswith(prefix):
        label = label[len(prefix) :]
    return f"{sec.course_code}|{label}"


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
    locked_section_ids: set[str] | None = None,
) -> list[ChainMove]:
    """Generate chain-2 moves: hotspot section + overlap partner section.

    Pruning strategy:
      - Only hotspot courses (top 5) as move_a
      - Only their top overlap partners as move_b
      - Only top N alternative patterns per section (by pattern diversity)
    """
    chains: list[ChainMove] = []
    partners = _find_overlap_partners(hotspot_courses[:5], student_profiles, top_n=5)
    locked = locked_section_ids or set()

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
                if h_sid in locked:
                    continue
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
                    if p_sid in locked:
                        continue
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
    decision_trace_out: dict[str, dict] | None = None,
    stage_telemetry: dict[str, dict[str, int]] | None = None,
    locked_section_ids: set[str] | None = None,
    chain_time_limit_seconds: float | None = None,
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

    # PR6 commit 6 — stage-boundary timing. iterations = outer-loop
    # iterations attempted (matches the natural loop counter, not the
    # accepted-chain count). The timer wraps the entire chain pass,
    # which is the observable work from the caller's perspective.
    _telemetry_on = stage_telemetry is not None and is_stage_telemetry_enabled()
    _chain_t0 = time.perf_counter() if _telemetry_on else 0.0
    iterations_attempted = 0

    # Wall-clock budget, independent of telemetry. Each chain move re-evaluates
    # the full student-assignment objective and the neighbourhood is
    # combinatorial, so without a deadline a large scenario runs many minutes.
    # current_best is only ever an accepted, strictly-improving board, so an
    # early stop always returns the best-so-far — never worse than the pre-chain
    # input (and the subsequent CP-SAT polish can still recover truncated tail
    # gains).
    _chain_deadline = (
        time.perf_counter() + chain_time_limit_seconds
        if chain_time_limit_seconds and chain_time_limit_seconds > 0
        else None
    )

    for iteration in range(max_iterations):
        if _chain_deadline is not None and time.perf_counter() > _chain_deadline:
            logger.info("Chain search: time budget reached before iteration %d", iteration)
            break
        iterations_attempted = iteration + 1
        hotspot_courses = current_best.hotspot_courses
        if not hotspot_courses:
            logger.info("Chain search: no hotspot courses at iteration %d", iteration)
            break

        chains = generate_chain_2_moves(
            hotspot_courses,
            sections_by_id,
            pattern_catalog,
            student_profiles,
            locked_section_ids=locked_section_ids,
        )

        if not chains:
            logger.info("Chain search: no chain moves available at iteration %d", iteration)
            break

        # Best-improvement: try all chains
        best_chain_result: TimetableEvaluationResult | None = None
        best_chain_score = current_score
        best_chain: ChainMove | None = None
        chains_tried = 0
        _timed_out = False

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

            # Mid-iteration deadline: stop scanning this (possibly large)
            # neighbourhood, but still let the accept-best-chain block below
            # bank the best improving chain found in the partial scan.
            if _chain_deadline is not None and time.perf_counter() > _chain_deadline:
                _timed_out = True
                break

        if best_chain is not None and best_chain_result is not None:
            # Snapshot pre-apply meeting signatures for trace emission —
            # at this point state is post-rollback (i.e. pre-chain).
            pre_sigs: dict[str, tuple[tuple[int, int, int], ...]] = {}
            if decision_trace_out is not None and is_stage_trace_enabled():
                for mv in (best_chain.move_a, best_chain.move_b):
                    sid = mv.section_id_a
                    if sid in sections_by_id:
                        pre_sigs[sid] = _meeting_signature(sections_by_id[sid])

            # Re-apply winning chain permanently
            _apply_chain(best_chain, sections_by_id, pattern_catalog)
            current_best = best_chain_result
            current_score = best_chain_score
            total_improvements += 1

            # PR5 commit 5 — emit CHAIN_ROTATED trace entries per section
            # whose chosen meetings shifted. Only accepted chains (no
            # rejected-chain telemetry); last-changer-wins by key in the
            # caller's overlay.
            if decision_trace_out is not None and is_stage_trace_enabled():
                chain_id = f"chain_{iteration}"
                for mv in (best_chain.move_a, best_chain.move_b):
                    sid = mv.section_id_a
                    if sid not in sections_by_id:
                        continue
                    sec = sections_by_id[sid]
                    before = pre_sigs.get(sid, ())
                    after = _meeting_signature(sec)
                    if before == after:
                        continue
                    anchor_before = before[0] if before else (0, 0, 0)
                    anchor_after = after[0]
                    before_day = WEEKDAYS[anchor_before[0]] if before else ""
                    after_day = WEEKDAYS[anchor_after[0]]
                    previous_slot = (
                        f"{before_day} {_fmt_hhmm(anchor_before[1])}-{_fmt_hhmm(anchor_before[2])}"
                        if before
                        else ""
                    )
                    new_slot = (
                        f"{after_day} {_fmt_hhmm(anchor_after[1])}-{_fmt_hhmm(anchor_after[2])}"
                    )
                    section_code = _section_code_key(sec)
                    entry = DecisionTrace(
                        section_code=section_code,
                        course_code=sec.course_code,
                        chosen_day=after_day,
                        chosen_start_time=_fmt_hhmm(anchor_after[1]),
                        chosen_end_time=_fmt_hhmm(anchor_after[2]),
                        chosen_room="",
                        alternatives=(),
                        stage_origin="chain",
                        stage_context={
                            "code": CHAIN_ROTATED,
                            "chain_length": 2,
                            "chain_id": chain_id,
                            "previous_slot": previous_slot,
                            "new_slot": new_slot,
                        },
                    )
                    decision_trace_out[section_code] = entry.to_dict()
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

        if _timed_out:
            logger.info("Chain search: time budget reached mid-iteration %d", iteration)
            break

    logger.info("Chain search complete: %d improvements", total_improvements)

    if _telemetry_on:
        record_stage_ms(stage_telemetry, "chain", int((time.perf_counter() - _chain_t0) * 1000))
        record_stage_iterations(stage_telemetry, "chain", iterations_attempted)

    return current_best
