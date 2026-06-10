from __future__ import annotations

from django.db.models import Q

from core.models import ProgrammeRequirement, Student, StudentCourse, StudentTermSection
from core.services.student_helpers import normalize_code

# Sections are gender-segregated and labelled with a leading gender tag, e.g.
# "M7", "F3" (first character is the cohort gender). A student (Student.section
# is "M" or "F") may only see/take sections of their own gender. Labels without
# an M/F prefix are treated as ungendered and shown to everyone.


def section_gender(section_label: str) -> str:
    """Return 'M'/'F' for a gendered section label (e.g. 'M7'/'F3'), else ''."""
    s = (section_label or "").strip().upper()
    return s[0] if s[:1] in ("M", "F") else ""


def student_gender(student_id: int | str) -> str:
    """Return the student's cohort gender ('M'/'F') from Student.section, else ''."""
    try:
        sid = int(student_id)
    except (TypeError, ValueError):
        return ""
    sec = Student.objects.filter(student_id=sid).values_list("section", flat=True).first()
    g = (sec or "").strip().upper()
    return g if g in ("M", "F") else ""


def gender_section_filter(gender: str) -> Q:
    """Build a Q() keeping only sections a ``gender`` student may take.

    Keeps the student's own gender sections PLUS any ungendered section (open to
    all). An unknown/blank gender returns an all-pass Q() so callers never
    accidentally hide every section. Used by both the planner's section catalog
    (display) and the build (scheduling) so they can never disagree.
    """
    g = (gender or "").strip().upper()
    if g not in ("M", "F"):
        return Q()
    gendered = Q(section__istartswith="M") | Q(section__istartswith="F")
    return Q(section__istartswith=g) | ~gendered


def _section_course_key(term_section) -> str:
    key = normalize_code(getattr(term_section, "course_key", "") or "")
    if key:
        return key
    code = normalize_code(getattr(term_section, "course_code", "") or "")
    number = normalize_code(getattr(term_section, "course_number", "") or "")
    if code and number and number != code:
        return normalize_code(f"{code}{number}")
    return code or number


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
            academic_year=str(academic_year),
            term=str(term),
            term_section__scenario__isnull=True,
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
        course_key_norm = _section_course_key(ts)
        credits = credit_map.get(course_key_norm, 0)

        meetings_list = sorted(ts.meetings.all(), key=lambda m: (m.day, m.start_time))
        if meetings_list:
            for m in meetings_list:
                out.append(
                    {
                        "course_code": course_key_norm,
                        "course_key": course_key_norm,
                        "course_name": ts.course_name or "",
                        "course_number": "",
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
                    "course_code": course_key_norm,
                    "course_key": course_key_norm,
                    "course_name": ts.course_name or "",
                    "course_number": "",
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


def append_unmapped_studying_courses(
    student_id: int | str,
    baseline: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Keep registered-course totals honest when section mappings are partial."""
    student_program = (
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
    )

    credit_map: dict[str, int] = {}
    if student_program:
        for code, credits in ProgrammeRequirement.objects.filter(
            program__iexact=student_program,
        ).values_list("course_code", "credit_hours"):
            norm = normalize_code(code)
            if norm:
                credit_map[norm] = credits or 0

    seen_codes: set[str] = set()
    for row in baseline:
        code = normalize_code(row.get("course_key") or row.get("course_code") or "")
        if code:
            seen_codes.add(code)

    out = list(baseline)
    studying_rows = (
        StudentCourse.objects.filter(
            student_id=student_id,
            status__iexact="studying",
        )
        .select_related("course")
        .order_by("course__course_code")
    )
    for sc in studying_rows:
        course = sc.course
        code = normalize_code(course.course_code)
        if not code or code in seen_codes:
            continue
        credits = credit_map.get(code, course.credit_hours or 0)
        out.append(
            {
                "course_code": course.course_code or code,
                "course_key": code,
                "course_name": course.description or "",
                "course_number": "",
                "section": "",
                "registered_count": None,
                "credits": int(credits or 0),
                "day": "",
                "start_time": "",
                "end_time": "",
                "room": "",
                "instructor": "",
                "term_section_id": None,
                "source": "fallback_studying",
            }
        )
        seen_codes.add(code)

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
        StudentTermSection.objects.filter(
            student_id=student_id,
            academic_year=str(academic_year),
            term=str(term),
        ).delete()

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
