from __future__ import annotations

from core.models import ProgrammeRequirement, Student, StudentTermSection
from core.services.student_helpers import normalize_code


def ensure_student_section_schema() -> None:
    # Schema is managed by Django migrations.
    # Keep this function as a compatibility no-op for existing call sites.
    return


def get_student_term_baseline(
    student_id: int | str, academic_year: str, term: str
) -> list[dict[str, object]]:
    sts_qs = (
        StudentTermSection.objects.filter(
            student_id=student_id,
        )
        .select_related("term_section")
        .prefetch_related("term_section__meetings")
    )

    # Get student's program for credit lookup
    student_program = (
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
    )

    # Build a credit lookup from programme_requirements
    credit_map: dict[str, int] = {}
    if student_program:
        for code, credits in ProgrammeRequirement.objects.filter(
            program__iexact=student_program,
        ).values_list("course_code", "credit_hours"):
            norm = normalize_code(code)
            if norm:
                credit_map[norm] = credits or 0

    out: list[dict[str, object]] = []
    for sts in sts_qs.order_by(
        "term_section__course_code",
        "term_section__course_number",
        "term_section__section",
    ):
        ts = sts.term_section
        course_key_norm = normalize_code(f"{ts.course_code or ''}{ts.course_number or ''}")
        credits = credit_map.get(course_key_norm, 0)

        meetings = ts.meetings.order_by("day", "start_time")
        if meetings.exists():
            for m in meetings:
                out.append(
                    {
                        "course_code": f"{(ts.course_code or '')}{(ts.course_number or '')}".replace(
                            " ", ""
                        ),
                        "course_name": ts.course_name or "",
                        "course_number": ts.course_number or "",
                        "section": ts.section or "",
                        "registered_count": ts.registered_count
                        if ts.registered_count is not None
                        else None,
                        "credits": credits,
                        "day": m.day or "",
                        "start_time": m.start_time or "",
                        "end_time": m.end_time or "",
                        "room": m.room or "",
                        "instructor": m.instructor or "",
                        "term_section_id": ts.id,
                        "source": sts.source or "mapped",
                    }
                )
        else:
            out.append(
                {
                    "course_code": f"{(ts.course_code or '')}{(ts.course_number or '')}".replace(
                        " ", ""
                    ),
                    "course_name": ts.course_name or "",
                    "course_number": ts.course_number or "",
                    "section": ts.section or "",
                    "registered_count": ts.registered_count
                    if ts.registered_count is not None
                    else None,
                    "credits": credits,
                    "day": "",
                    "start_time": "",
                    "end_time": "",
                    "room": "",
                    "instructor": "",
                    "term_section_id": ts.id,
                    "source": sts.source or "mapped",
                }
            )
    return out


def replace_student_term_sections(
    student_id: int | str,
    academic_year: str,
    term: str,
    term_section_ids: list[int],
    source: str = "manual",
) -> dict[str, int]:
    from django.db import transaction

    with transaction.atomic():
        StudentTermSection.objects.filter(student_id=student_id).delete()

        objs = [
            StudentTermSection(
                student_id=int(student_id),
                academic_year=str(academic_year),
                term=str(term),
                term_section_id=int(sid),
                source=source,
            )
            for sid in term_section_ids
        ]
        StudentTermSection.objects.bulk_create(objs, ignore_conflicts=True)

    return {"inserted": len(term_section_ids)}
