from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import (
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    TimetableEvaluationResult,
)
from core.services.timetable_quality import evaluate_timetable_quality


def evaluate_generated_timetable_candidate(
    candidate_id: str,
    generated_sections: list[SectionState],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
) -> TimetableEvaluationResult:
    working_sections = deepcopy(generated_sections)
    sections_by_id = ssa.build_sections_by_id(working_sections)
    sections_by_course = ssa.build_sections_by_course(sections_by_id)
    states, unresolved_ids = ssa.assign_students_to_sections(
        student_profiles,
        sections_by_id,
        sections_by_course,
        course_rigidity,
    )
    score = ssa.evaluate_assignability_lexicographic(
        states, student_profiles, sections_by_id, section_instructor_ids
    )
    quality_score = evaluate_timetable_quality(working_sections, states)
    return TimetableEvaluationResult(
        candidate_id=candidate_id,
        lexicographic_score=score,
        assignment_states=states,
        unresolved_student_ids=unresolved_ids,
        hotspot_courses=extract_hotspot_courses(states),
        capacity_pressure_courses=extract_capacity_pressure_courses(states),
        reserve_heavy_sections=extract_reserve_heavy_sections(sections_by_id),
        quality_score=quality_score,
    )


def extract_hotspot_courses(states: dict[str, StudentAssignmentState]) -> list[str]:
    course_failures: dict[str, int] = defaultdict(int)
    for state in states.values():
        for course_code, unres_reason in state.unresolved_courses.items():
            if unres_reason.reason in ("all_clash", "mixed_blockers"):
                course_failures[course_code] += 1
    return [
        course for course, _count in sorted(course_failures.items(), key=lambda x: (-x[1], x[0]))
    ]


def extract_capacity_pressure_courses(
    states: dict[str, StudentAssignmentState],
) -> list[str]:
    capacity_failures: dict[str, int] = defaultdict(int)
    for state in states.values():
        for course_code, unres_reason in state.unresolved_courses.items():
            if unres_reason.reason in ("full", "reserve_only"):
                capacity_failures[course_code] += 1
    return [
        course for course, _count in sorted(capacity_failures.items(), key=lambda x: (-x[1], x[0]))
    ]


def extract_reserve_heavy_sections(
    sections_by_id: dict[str, SectionState], threshold: float = 0.5
) -> list[tuple[str, float]]:
    heavy: list[tuple[str, float]] = []
    for sec_id, sec in sections_by_id.items():
        if sec.reserve_capacity > 0:
            ratio = sec.reserve_used() / sec.reserve_capacity
            if ratio >= threshold:
                heavy.append((sec_id, ratio))
    heavy.sort(key=lambda x: (-x[1], x[0]))
    return heavy


def rank_timetable_candidates(
    candidate_list: list[dict[str, Any]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
) -> list[TimetableEvaluationResult]:
    results: list[TimetableEvaluationResult] = []
    for candidate in candidate_list:
        results.append(
            evaluate_generated_timetable_candidate(
                candidate_id=candidate["id"],
                generated_sections=candidate["sections"],
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
                section_instructor_ids=section_instructor_ids,
            )
        )
    results.sort(
        key=lambda r: (
            r.lexicographic_score,
            int((r.quality_score or {}).get("penalty") or 0),
            r.candidate_id,
        )
    )
    return results
