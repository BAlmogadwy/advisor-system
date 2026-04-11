from __future__ import annotations

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


def generate_tier_1_moves(
    hotspot_courses: list[str],
    reserve_heavy_sections: list[str],
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
) -> list[TimetableMove]:
    moves: list[TimetableMove] = []
    priority_sections = set(reserve_heavy_sections)
    for course_code in hotspot_courses[:5]:
        for sec in sections_by_id.values():
            if sec.course_code == course_code:
                priority_sections.add(sec.section_id)
    for sec_id in sorted(priority_sections):
        sec = sections_by_id[sec_id]
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


def diagnostic_driven_local_search(
    best_candidate: TimetableEvaluationResult,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    rooms_by_id: dict[str, RoomProfile] | None = None,
    room_occupancies: dict[str, RoomOccupancy] | None = None,
    course_room_requirements: dict[str, str] | None = None,
    max_iterations: int = 25,
) -> TimetableEvaluationResult:
    current_best = best_candidate
    current_score = best_candidate.lexicographic_score
    for _iteration in range(max_iterations):
        hotspot_courses = current_best.hotspot_courses
        reserve_heavy = [sid for sid, _ratio in current_best.reserve_heavy_sections]
        neighborhood = generate_tier_1_moves(
            hotspot_courses, reserve_heavy, sections_by_id, pattern_catalog
        )
        improved = False
        for move in neighborhood:
            snapshot = apply_move_to_grid(move, sections_by_id, pattern_catalog)
            if rooms_by_id is not None and room_occupancies is not None:
                room_ok = try_repair_rooms_locally(
                    snapshot,
                    sections_by_id,
                    rooms_by_id,
                    room_occupancies,
                    course_room_requirements or {},
                )
                if not room_ok:
                    rollback_move(snapshot, sections_by_id, room_occupancies)
                    continue
            test_result = evaluate_generated_timetable_candidate(
                candidate_id="local_search_test",
                generated_sections=list(sections_by_id.values()),
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
            )
            if test_result.lexicographic_score < current_score:
                current_best = test_result
                current_score = test_result.lexicographic_score
                improved = True
                break
            if room_occupancies is not None:
                rollback_move(snapshot, sections_by_id, room_occupancies)
            else:
                # roomless rollback: restore patterns only
                for snap in snapshot.snapshots:
                    sec = sections_by_id[snap.section_id]
                    sec.pattern_id = snap.old_pattern_id
                    sec.meetings = list(snap.old_meetings)
                    sec.assigned_room_id = snap.old_room_id
        if not improved:
            break
    return current_best
