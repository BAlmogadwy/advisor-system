from __future__ import annotations

from collections.abc import Iterable

from django.db.models import Q, QuerySet

from core.models import Student, StudentTermSection, TermSection, TermSectionProgram


def normalize_section_program(value: object) -> str:
    """Return the canonical programme code stored on section memberships."""
    program = str(value or "").strip().upper()
    if len(program) > 32:
        raise ValueError("Programme code cannot exceed 32 characters")
    return program


def filter_sections_for_program(
    queryset: QuerySet,
    program: object,
    *,
    include_unassigned: bool = False,
) -> QuerySet:
    """Filter sections using exact normalized programme membership.

    Unassigned sections fail closed by default. Staff-only diagnostic callers
    may opt into them explicitly with ``include_unassigned=True``.
    """
    normalized = normalize_section_program(program)
    if not normalized:
        return (
            queryset.filter(program_links__isnull=True) if include_unassigned else queryset.none()
        )

    membership = Q(program_links__program=normalized)
    if include_unassigned:
        membership |= Q(program_links__isnull=True)
    return queryset.filter(membership).distinct()


def filter_sections_for_delivery_board(
    queryset: QuerySet,
    *,
    scenario_id: object,
    program: object,
) -> QuerySet:
    """Return sections that may be placed on one programme delivery board.

    Scenario-owned sections remain private to their own scenario. Global
    current-snapshot sections require an explicit membership in the board's
    programme; an incomplete board programme therefore exposes no globals.
    """
    normalized = normalize_section_program(program)
    allowed = Q(scenario_id=scenario_id)
    if normalized:
        allowed |= Q(
            scenario__isnull=True,
            program_links__program=normalized,
        )
    return queryset.filter(allowed).distinct()


def section_is_available_to_delivery_board(
    section: object,
    *,
    scenario_id: object,
    program: object,
) -> bool:
    """Validate board eligibility again at the placement mutation boundary."""
    section_scenario_id = getattr(section, "scenario_id", None)
    if section_scenario_id is not None:
        return str(section_scenario_id) == str(scenario_id)

    normalized = normalize_section_program(program)
    if not normalized:
        return False
    program_links = getattr(section, "program_links", None)
    return bool(program_links and program_links.filter(program=normalized).exists())


def reconcile_observed_section_programs(term_section_ids: Iterable[int]) -> None:
    """Rebuild observed programme links for the affected global sections.

    Imported/manual links are authoritative and remain untouched. Observed
    links mirror the students currently linked through ``StudentTermSection``;
    missing Student rows and blank programmes do not create guessed ownership.
    """
    requested_ids = {int(section_id) for section_id in term_section_ids}
    if not requested_ids:
        return

    global_ids = set(
        TermSection.objects.filter(
            id__in=requested_ids,
            scenario__isnull=True,
        ).values_list("id", flat=True)
    )
    if not global_ids:
        return

    registrations = list(
        StudentTermSection.objects.filter(term_section_id__in=global_ids).values_list(
            "term_section_id",
            "student_id",
        )
    )
    student_ids = {student_id for _section_id, student_id in registrations}
    program_by_student: dict[int, str] = {}
    for student_id, program in Student.objects.filter(student_id__in=student_ids).values_list(
        "student_id",
        "program",
    ):
        normalized = normalize_section_program(program)
        if normalized:
            program_by_student[int(student_id)] = normalized

    desired: dict[int, set[str]] = {section_id: set() for section_id in global_ids}
    for section_id, student_id in registrations:
        program = program_by_student.get(int(student_id), "")
        if program:
            desired[int(section_id)].add(program)

    for section_id in global_ids:
        wanted = desired[section_id]
        observed = TermSectionProgram.objects.filter(
            term_section_id=section_id,
            assignment_source="observed",
        )
        if wanted:
            observed.exclude(program__in=wanted).delete()
        else:
            observed.delete()

        existing = set(
            TermSectionProgram.objects.filter(
                term_section_id=section_id,
                program__in=wanted,
            ).values_list("program", flat=True)
        )
        TermSectionProgram.objects.bulk_create(
            [
                TermSectionProgram(
                    term_section_id=section_id,
                    program=program,
                    assignment_source="observed",
                )
                for program in sorted(wanted - existing)
            ],
            ignore_conflicts=True,
        )
