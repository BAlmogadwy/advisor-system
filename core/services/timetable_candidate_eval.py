from __future__ import annotations

from collections import defaultdict
from copy import copy
from typing import Any

from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import (
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    TimetableEvaluationResult,
)
from core.services.timetable_flags import (
    is_gap_regular_students_only_enabled,
    is_tiered_objective_enabled,
)
from core.services.timetable_optimisation_policy import (
    evaluation_order_key,
    instructor_schedule_metrics,
)
from core.services.timetable_pr4_instructor import (
    count_instructor_daily_overloads,
    get_instructor_daily_cap,
    is_instructor_daily_cap_enabled,
)
from core.services.timetable_quality import evaluate_timetable_quality


def _working_copy(sections: list[SectionState]) -> list[SectionState]:
    """An independent copy of ``sections`` that seating can mutate freely.

    Seating only ever changes a section's ``current_enrollment`` and
    ``enrolled_student_ids``; everything else is an immutable scalar or a frozen
    ``SectionMeeting``. So copy those two (plus the meetings *list*, so even an
    in-place list edit stays isolated) and share the immutable meetings, instead
    of paying ``deepcopy`` to rebuild every meeting on every evaluation.
    """
    clones: list[SectionState] = []
    for section in sections:
        clone = copy(section)
        clone.meetings = list(section.meetings)
        clone.enrolled_student_ids = set(section.enrolled_student_ids)
        clones.append(clone)
    return clones


def evaluate_generated_timetable_candidate(
    candidate_id: str,
    generated_sections: list[SectionState],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
) -> TimetableEvaluationResult:
    working_sections = _working_copy(generated_sections)
    sections_by_id = ssa.build_sections_by_id(working_sections)
    sections_by_course = ssa.build_sections_by_course(sections_by_id)
    grouping: dict[str, Any] = {}
    # Fresh per evaluation, like the working copy: the grouping pass's change
    # to each section's reserve use, which the reserve readers below subtract.
    reserve_shift: dict[str, int] = {}
    states, unresolved_ids = ssa.assign_students_to_sections(
        student_profiles,
        sections_by_id,
        sections_by_course,
        course_rigidity,
        course_tiers=course_tiers if is_tiered_objective_enabled() else None,
        grouping_report=grouping,
        reserve_shift=reserve_shift,
    )
    score = ssa.evaluate_assignability_lexicographic(
        states,
        student_profiles,
        sections_by_id,
        section_instructor_ids,
        course_tiers,
        reserve_shift=reserve_shift,
    )
    quality_score = evaluate_timetable_quality(working_sections, states)
    if grouping:
        # Side-band only, present only when a section is marked "mainly for
        # programme": what the grouping did and what it cost in student gap.
        # Never part of ``score``; without marks the dict is exactly as before.
        quality_score["programme_grouping"] = grouping
    if is_gap_regular_students_only_enabled():
        # Side-band, ON only: both numbers, so visitors' gap stays visible even
        # though the objective no longer counts it. Absent when the flag is off.
        quality_score["student_gap"] = {
            "regular_only": True,
            **ssa.student_gap_split(states, student_profiles),
        }
    quality_score["instructor_schedule"] = instructor_schedule_metrics(
        working_sections, section_instructor_ids
    )
    # Side-band only — never part of ``score``. Reports how many instructor-day
    # sessions exceed the cap (0 when compliant / flag off / no instructor map),
    # so the structural cap can be observed without disturbing the tuple.
    overload_count = 0
    if section_instructor_ids and is_instructor_daily_cap_enabled():
        overload_count = count_instructor_daily_overloads(
            sections_by_id, section_instructor_ids, get_instructor_daily_cap()
        )
    return TimetableEvaluationResult(
        candidate_id=candidate_id,
        lexicographic_score=score,
        assignment_states=states,
        unresolved_student_ids=unresolved_ids,
        hotspot_courses=extract_hotspot_courses(states),
        capacity_pressure_courses=extract_capacity_pressure_courses(states),
        reserve_heavy_sections=extract_reserve_heavy_sections(
            sections_by_id, reserve_shift=reserve_shift
        ),
        quality_score=quality_score,
        instructor_overload_count=overload_count,
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
    sections_by_id: dict[str, SectionState],
    threshold: float = 0.5,
    reserve_shift: dict[str, int] | None = None,
) -> list[tuple[str, float]]:
    """Sections whose reserve use is at least ``threshold`` of their reserve.

    ``reserve_shift`` is the grouping pass's per-section change in reserve use
    (``assign_students_to_sections``), subtracted: a marked section that fills
    its reserve by re-sectioning within the course is not capacity pressure, so
    it is neither listed nor made a search priority because of that.
    """
    shift = reserve_shift or {}
    heavy: list[tuple[str, float]] = []
    for sec_id, sec in sections_by_id.items():
        if sec.reserve_capacity > 0:
            ratio = (sec.reserve_used() - shift.get(sec_id, 0)) / sec.reserve_capacity
            if ratio >= threshold:
                heavy.append((sec_id, ratio))
    heavy.sort(key=lambda x: (-x[1], x[0]))
    return heavy


def rank_timetable_candidates(
    candidate_list: list[dict[str, Any]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
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
                course_tiers=course_tiers,
            )
        )
    results.sort(
        key=lambda r: (
            evaluation_order_key(r),
            int((r.quality_score or {}).get("penalty") or 0),
            r.candidate_id,
        )
    )
    return results
