"""
core/services/timetable_overlap.py
Real student-pair overlap utilities for the timetable planner.

Replaces the fake cohort/S1-S2 grouping with an actual course-overlap
matrix built from ScenarioStudentMap.  Used by all placement engines
(greedy, CP-SAT, local search, load-balanced) and the conflict
detection layer.

Key functions:
    build_overlap_matrix   — {(courseA, courseB): shared_student_count}
    courses_share_students — O(1) boolean check
    shared_student_count   — get the count
    course_overlap_load    — total overlap weight for a single course
    build_course_students_map — {course_code: {student_ids}}
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from itertools import combinations

from core.services.student_helpers import normalize_code

# Threshold for treating a student overlap as a hard/critical constraint.
# Pairs with >= HARD_OVERLAP_THRESHOLD shared students are hard-blocked in
# local search / load-balanced and flagged critical in the workspace.
# Pairs below this threshold are soft (penalised but not forbidden).
HARD_OVERLAP_THRESHOLD = 20

OverlapMatrix = dict[tuple[str, str], int]


def overlap_key(code_a: str, code_b: str) -> tuple[str, str]:
    """Canonical key: sorted alphabetically so (A,B) == (B,A)."""
    a, b = normalize_code(code_a), normalize_code(code_b)
    return (a, b) if a <= b else (b, a)


def build_course_students_map(
    scenario_id: int,
    course_codes: Collection[str],
) -> dict[str, set[int]]:
    """Build {course_code: {student_ids}} from ScenarioStudentMap.

    Scans ALL students in the scenario (not filtered by primary_term)
    so cross-term visitor students are included.  Only courses in
    *course_codes* are tracked.

    Parameters
    ----------
    scenario_id : int
        PK of the TimetableScenario.
    course_codes : Collection[str]
        The board's actual course codes (from ScenarioSectionBudget).
    """
    from core.models import ScenarioStudentMap

    codes_norm = {normalize_code(c) for c in course_codes if c}
    course_students: dict[str, set[int]] = defaultdict(set)

    for sm in ScenarioStudentMap.objects.filter(scenario_id=scenario_id):
        for code in sm.recommended_courses:
            nc = normalize_code(code)
            if nc in codes_norm:
                course_students[nc].add(sm.student_id)

    return dict(course_students)


def build_overlap_matrix(
    scenario_id: int,
    course_codes: Collection[str],
) -> OverlapMatrix:
    """Build pairwise student-overlap counts between courses.

    For each student, finds which of *course_codes* they need, then
    increments the overlap counter for every pair.  Includes ALL
    students in the scenario (primary + visitor/cross-term).

    Parameters
    ----------
    scenario_id : int
        PK of the TimetableScenario.
    course_codes : Collection[str]
        The board's actual course codes.

    Returns
    -------
    OverlapMatrix
        ``{(courseA, courseB): shared_student_count}`` with keys sorted
        alphabetically.  Missing keys imply 0 overlap.
    """
    from core.models import ScenarioStudentMap

    codes_norm = {normalize_code(c) for c in course_codes if c}
    matrix: dict[tuple[str, str], int] = defaultdict(int)

    for sm in ScenarioStudentMap.objects.filter(scenario_id=scenario_id):
        hits = sorted({normalize_code(c) for c in sm.recommended_courses} & codes_norm)
        for a, b in combinations(hits, 2):
            matrix[overlap_key(a, b)] += 1

    return dict(matrix)


def courses_share_students(
    matrix: OverlapMatrix,
    code_a: str,
    code_b: str,
) -> bool:
    """Check if two courses share any students (O(1) lookup)."""
    if code_a == code_b:
        return True  # same course always "shares"
    return matrix.get(overlap_key(code_a, code_b), 0) > 0


SAME_COURSE_SENTINEL = 999
"""Sentinel return value from shared_student_count when code_a == code_b."""


def shared_student_count(
    matrix: OverlapMatrix,
    code_a: str,
    code_b: str,
) -> int:
    """Get the number of shared students between two courses.

    Returns SAME_COURSE_SENTINEL (999) when code_a == code_b.  Callers
    that care about same-course should check code equality first rather
    than relying on this sentinel.
    """
    if code_a == code_b:
        return SAME_COURSE_SENTINEL
    return matrix.get(overlap_key(code_a, code_b), 0)


def course_overlap_load(
    matrix: OverlapMatrix,
    code: str,
) -> int:
    """Total overlap weight for a single course.

    Sums ``shared_student_count(code, other)`` across all other courses
    in the matrix.  Higher = more students share this course with others
    = more important to minimize gaps.
    """
    nc = normalize_code(code)
    total = 0
    for (a, b), count in matrix.items():
        if a == nc or b == nc:
            total += count
    return total
