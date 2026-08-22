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
) -> list[tuple[str, str, int | None]]:
    """Return unfulfilled placeholders sorted by term (ascending).

    Returns list of (code, placeholder_type, programme_term).
    Only returns placeholders that remain unfulfilled in StudentCourse.
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

    # Check which are not taken/failed (or missing from StudentCourse entirely).
    taken_statuses = {}
    for sc in StudentCourse.objects.filter(
        student_id=student_id,
        course__course_code__in=[p[0] for p in placeholders],
    ).select_related("course"):
        taken_statuses[sc.course.course_code] = sc.status

    unfulfilled = []
    for code, ptype, term in placeholders:
        status = taken_statuses.get(code)
        if status in (None, "not_taken", "failed"):
            unfulfilled.append((code, ptype, term))

    return unfulfilled


def _get_timetable_courses(
    student_id: int,
    *,
    academic_year: str | None = None,
    term: str | None = None,
) -> set[str]:
    """Return course codes from a verified global scraper snapshot only."""
    snapshots = (
        StudentTermSection.objects.filter(
            student_id=student_id,
            source="scraper_timetable",
            term_section__scenario__isnull=True,
        )
        .exclude(
            # Another branch's sections are not evidence of this student's study
            # here. Without this, elective resolution and academic_state disagreed
            # about whether the same student occupies an elective slot.
            term_section__section__istartswith="YM"
        )
        .exclude(term_section__section__istartswith="YF")
    )
    if academic_year is None or term is None:
        latest = (
            snapshots.order_by("-academic_year", "-term")
            .values_list("academic_year", "term")
            .first()
        )
        if latest is None:
            return set()
        academic_year, term = latest
    return set(
        snapshots.filter(
            academic_year=academic_year,
            term=term,
        )
        .select_related("term_section")
        .values_list("term_section__course_key", flat=True)
    )


def _get_elective_mapped_courses(
    program: str,
    *,
    academic_year: str,
    term: str,
) -> dict[str, set[str]]:
    """Return authoritative placeholder mappings for one verified term."""
    normalized_program = str(program or "").strip().upper()
    mapping_programs = {normalized_program}
    if normalized_program.endswith("2"):
        # Curriculum-version programme codes (CS2/DS2/AI2) share the
        # department catalogue when mappings use the base programme code.
        mapping_programs.add(normalized_program[:-1])
    try:
        term_number = int(term)
    except (TypeError, ValueError):
        return {}

    result: dict[str, set[str]] = defaultdict(set)
    mappings = ElectiveTermMapping.objects.filter(
        programme__in=mapping_programs,
        academic_year=str(academic_year),
        term=term_number,
    ).select_related("elective")
    for m in mappings:
        result[m.placeholder_code].add(m.elective.course_code)
    return dict(result)


def _reconcile_unmapped_studying_placeholders(
    student_id: int,
    program: str,
    *,
    timetable_codes: set[str],
    mapped_courses: dict[str, set[str]],
    dry_run: bool,
) -> list[dict]:
    """Undo placeholder statuses unsupported by the verified term snapshot."""
    from core.services.course_classifier import parse_course_result

    placeholder_codes = {
        code
        for code in ProgrammeRequirement.objects.filter(program=program).values_list(
            "course_code", flat=True
        )
        if _classify_placeholder(code) is not None
    }
    rows = StudentCourse.objects.filter(
        student_id=student_id,
        course__course_code__in=placeholder_codes,
        status="studying",
    ).select_related("course")

    reconciliations: list[dict] = []
    for row in rows:
        placeholder = row.course.course_code
        backed_by_timetable = placeholder in timetable_codes or bool(
            timetable_codes & mapped_courses.get(placeholder, set())
        )
        if backed_by_timetable:
            continue

        result = parse_course_result({"letter": row.grade, "marks": row.mark})
        restored_status = result["outcome"] or "not_taken"
        reconciliations.append(
            {
                "student_id": student_id,
                "placeholder": placeholder,
                "restored_status": restored_status,
            }
        )
        if not dry_run:
            row.status = restored_status
            row.save(update_fields=["status"])

    return reconciliations


def resolve_elective_placeholders(
    program: str,
    section: str | None = None,
    student_ids: list[int] | None = None,
    student_snapshots: dict[int, tuple[str, str]] | None = None,
    dry_run: bool = False,
) -> dict:
    """Resolve elective placeholders by cross-referencing timetables.

    For each student:
      1. Find unfulfilled placeholders (IS1, FE2, GSE1, etc.)
      2. Find courses in their timetable NOT in their plan
      3. Match timetable courses to placeholders by type
      4. Update StudentCourse status to "studying"

    Matching rules:
      - Every placeholder requires an exact ElectiveTermMapping for the
        verified academic year and term
      - Fill in ascending term order (lowest unfulfilled first)

    Parameters
    ----------
    program : str
        Programme code (e.g. "IS").
    section : str | None
        Optional section filter ("M" or "F").
    student_ids : list[int] | None
        Specific students to process. If None, processes all in program/section.
    student_snapshots : dict[int, tuple[str, str]] | None
        Optional exact verified scraper term per student. Students absent from
        this mapping are skipped. Batch scraping supplies this so planner or
        scenario rows can never become current-registration evidence.
    dry_run : bool
        If True, don't update DB — just report what would change.

    Returns
    -------
    dict with keys: total_students, resolved_count, updates (list of dicts)
    """
    if student_ids is None:
        student_ids = get_student_ids(program=program, section=section)

    # Timetable codes outside the regular plan remain candidates until an
    # authoritative term mapping proves which placeholder they fulfil.
    plan_codes = _get_plan_course_codes(program)
    regular_plan_codes = plan_codes - ALL_PLACEHOLDER_CODES

    updates: list[dict] = []
    reconciliations: list[dict] = []
    resolved_count = 0

    for sid in student_ids:
        if student_snapshots is not None:
            snapshot = student_snapshots.get(sid)
            if snapshot is None:
                continue
        else:
            snapshot = (
                StudentTermSection.objects.filter(
                    student_id=sid,
                    source="scraper_timetable",
                    term_section__scenario__isnull=True,
                )
                .order_by("-academic_year", "-term")
                .values_list("academic_year", "term")
                .first()
            )
            if snapshot is None:
                continue

        timetable_codes = _get_timetable_courses(
            sid,
            academic_year=snapshot[0],
            term=snapshot[1],
        )
        if not timetable_codes:
            continue
        mapped_courses = _get_elective_mapped_courses(
            program,
            academic_year=snapshot[0],
            term=snapshot[1],
        )
        reconciliations.extend(
            _reconcile_unmapped_studying_placeholders(
                sid,
                program,
                timetable_codes=timetable_codes,
                mapped_courses=mapped_courses,
                dry_run=dry_run,
            )
        )

        unfulfilled = _get_unfulfilled_placeholders(sid, program)
        if not unfulfilled:
            continue

        # Registration proves only that a course is being studied; it does not
        # prove which degree-plan requirement the course fulfils.
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

        # Fill placeholders in ascending term order. Registration proves that
        # the student is taking a course, but only the term mapping proves
        # which degree-plan placeholder that course fulfils.

        for placeholder_code, ptype, term in unfulfilled:
            available = extra_courses - assigned_courses - passed_only

            if not available:
                break

            candidates = available & mapped_courses.get(placeholder_code, set())

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
                    status__in=("not_taken", "failed"),
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
        "reconciled_count": len(reconciliations),
        "dry_run": dry_run,
        "updates": updates,
        "reconciliations": reconciliations,
    }
