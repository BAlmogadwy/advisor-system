"""
core/services/elective_resolver.py
Post-scrape elective placeholder resolver.

The university plan system only marks elective placeholders (IS1, FE2,
GSE1, etc.) as "passed" after the grade is posted. During the semester,
the placeholder stays "not_taken" even though the student IS studying a
real course that fulfills it.

This resolver cross-references each student's current timetable against
their unfulfilled placeholders and updates the StudentCourse status to
"studying" when a match is found.

Run this AFTER scraping student plans and timetables:
    scrape plans → scrape timetables → resolve_elective_placeholders()

To revert, simply re-scrape the student plans (they'll overwrite status).
"""

from __future__ import annotations

import logging
from collections import defaultdict

from core.models import (
    ElectiveTermMapping,
    ProgrammeRequirement,
    StudentCourse,
    StudentTermSection,
)
from core.services.reporting import get_student_ids

logger = logging.getLogger(__name__)

# Placeholder prefixes — any course code matching these patterns is
# an elective placeholder, not a real course.
PROGRAM_ELECTIVE_PREFIXES = (
    "AI1",
    "AI2",
    "AI3",
    "CS1",
    "CS2",
    "CS3",
    "IS1",
    "IS2",
    "IS3",
    "DS1",
    "DS2",
    "DS3",
    "COE1",
    "COE2",
    "COE3",
    "COE4",
    "CYB1",
    "CYB2",
    "CYB3",
    "CYB4",
)
FREE_ELECTIVE_CODES = ("FE1", "FE2")
UNIVERSITY_ELECTIVE_CODES = ("GSE1", "GSE2", "GSE3")

ALL_PLACEHOLDER_CODES = (
    set(PROGRAM_ELECTIVE_PREFIXES) | set(FREE_ELECTIVE_CODES) | set(UNIVERSITY_ELECTIVE_CODES)
)


def _classify_placeholder(code: str) -> str | None:
    """Return the placeholder type or None if not a placeholder."""
    if code in FREE_ELECTIVE_CODES:
        return "free_elective"
    if code in UNIVERSITY_ELECTIVE_CODES:
        return "university_elective"
    if code in PROGRAM_ELECTIVE_PREFIXES:
        return "program_elective"
    return None


def _get_plan_course_codes(program: str) -> set[str]:
    """All course codes in a programme plan (both real and placeholders)."""
    return set(
        ProgrammeRequirement.objects.filter(program=program).values_list("course_code", flat=True)
    )


def _get_unfulfilled_placeholders(
    student_id: int,
    program: str,
) -> list[tuple[str, str, int]]:
    """Return unfulfilled placeholders sorted by term (ascending).

    Returns list of (code, placeholder_type, programme_term).
    Only returns placeholders that are 'not_taken' in StudentCourse.
    """
    # Get all placeholders in this plan
    placeholders = []
    for pr in ProgrammeRequirement.objects.filter(program=program).order_by("programme_term"):
        ptype = _classify_placeholder(pr.course_code)
        if ptype is None:
            continue
        placeholders.append((pr.course_code, ptype, pr.programme_term))

    if not placeholders:
        return []

    # Check which are not_taken (or missing from StudentCourse entirely)
    taken_statuses = {}
    for sc in StudentCourse.objects.filter(
        student_id=student_id,
        course__course_code__in=[p[0] for p in placeholders],
    ).select_related("course"):
        taken_statuses[sc.course.course_code] = sc.status

    unfulfilled = []
    for code, ptype, term in placeholders:
        status = taken_statuses.get(code)
        if status in (None, "not_taken"):
            unfulfilled.append((code, ptype, term))

    return unfulfilled


def _get_timetable_courses(student_id: int) -> set[str]:
    """Return course codes from the student's current timetable."""
    return set(
        StudentTermSection.objects.filter(student_id=student_id)
        .select_related("term_section")
        .values_list("term_section__course_key", flat=True)
    )


def _get_elective_mapped_courses(program: str) -> dict[str, set[str]]:
    """Return {placeholder_code: {elective_course_codes}} from ElectiveTermMapping."""
    result: dict[str, set[str]] = defaultdict(set)
    for m in ElectiveTermMapping.objects.filter(programme=program).select_related("elective"):
        result[m.placeholder_code].add(m.elective.course_code)
    return dict(result)


def resolve_elective_placeholders(
    program: str,
    section: str | None = None,
    student_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict:
    """Resolve elective placeholders by cross-referencing timetables.

    For each student:
      1. Find unfulfilled placeholders (IS1, FE2, GSE1, etc.)
      2. Find courses in their timetable NOT in their plan
      3. Match timetable courses to placeholders by type
      4. Update StudentCourse status to "studying"

    Matching rules:
      - Program electives (IS1, CS1): matched via ElectiveTermMapping
      - Free electives (FE1, FE2): any timetable course not in plan
      - University electives (GSE1): any timetable course not in plan
      - Fill in ascending term order (lowest unfulfilled first)

    Parameters
    ----------
    program : str
        Programme code (e.g. "IS").
    section : str | None
        Optional section filter ("M" or "F").
    student_ids : list[int] | None
        Specific students to process. If None, processes all in program/section.
    dry_run : bool
        If True, don't update DB — just report what would change.

    Returns
    -------
    dict with keys: total_students, resolved_count, updates (list of dicts)
    """
    if student_ids is None:
        student_ids = get_student_ids(program=program, section=section)

    # Regular plan courses (excluding placeholders) — anything in the
    # timetable NOT in this set must be fulfilling a placeholder.
    plan_codes = _get_plan_course_codes(program)
    regular_plan_codes = plan_codes - ALL_PLACEHOLDER_CODES

    updates: list[dict] = []
    resolved_count = 0

    for sid in student_ids:
        unfulfilled = _get_unfulfilled_placeholders(sid, program)
        if not unfulfilled:
            continue

        timetable_codes = _get_timetable_courses(sid)
        if not timetable_codes:
            continue

        # Any course in the timetable that isn't a regular plan course
        # MUST be fulfilling one of the elective placeholders. The
        # university already controls registration — if the student is
        # studying it, it's valid for their plan.
        extra_courses = timetable_codes - regular_plan_codes

        if not extra_courses:
            continue

        # Only exclude PASSED courses — studying courses are exactly
        # what we want to match to placeholders.
        passed_only = set(
            StudentCourse.objects.filter(
                student_id=sid,
                status="passed",
            )
            .select_related("course")
            .values_list("course__course_code", flat=True)
        )

        # Track which extra courses have been assigned this round
        assigned_courses: set[str] = set()
        student_updates: list[dict] = []

        # Fill placeholders in ascending term order, matching by type:
        # - Program electives (IS1, CS1): only courses from the same
        #   department (IS*, CS*, etc.)
        # - Free electives (FE1, FE2): any course NOT from the programme
        #   department (education, arts, other faculties)
        # - University electives (GSE1): general university courses (GS*)
        #   or any remaining unmatched course
        dept_prefix = program.rstrip("2")  # IS2 -> IS, CS2 -> CS, AI2 -> AI

        for placeholder_code, ptype, term in unfulfilled:
            available = extra_courses - assigned_courses - passed_only

            if not available:
                break

            if ptype == "program_elective":
                # Only courses from the same department
                candidates = {c for c in available if c.startswith(dept_prefix)}
            elif ptype == "free_elective":
                # Courses NOT from this department and NOT general (GS*)
                candidates = {
                    c for c in available if not c.startswith(dept_prefix) and not c.startswith("GS")
                }
            else:  # university_elective
                # General courses (GS*) first, then anything remaining
                candidates = {c for c in available if c.startswith("GS")}
                if not candidates:
                    # Fallback: any remaining course
                    candidates = available

            if not candidates:
                continue

            # Pick the first available candidate (alphabetical for consistency)
            pick = sorted(candidates)[0]
            assigned_courses.add(pick)

            student_updates.append(
                {
                    "student_id": sid,
                    "placeholder": placeholder_code,
                    "placeholder_type": ptype,
                    "term": term,
                    "resolved_with": pick,
                }
            )

        # Apply updates
        if student_updates and not dry_run:
            for upd in student_updates:
                # Update the placeholder's StudentCourse status to "studying"
                StudentCourse.objects.filter(
                    student_id=sid,
                    course__course_code=upd["placeholder"],
                    status="not_taken",
                ).update(status="studying")

        if student_updates:
            resolved_count += 1
            updates.extend(student_updates)

    action = "DRY RUN" if dry_run else "APPLIED"
    logger.info(
        "Elective resolver [%s] %s: %d students resolved, %d updates",
        program,
        action,
        resolved_count,
        len(updates),
    )

    return {
        "program": program,
        "total_students": len(student_ids),
        "resolved_count": resolved_count,
        "total_updates": len(updates),
        "dry_run": dry_run,
        "updates": updates,
    }
