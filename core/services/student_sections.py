from __future__ import annotations

from django.db.models import Q

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.section_programmes import (
    normalize_section_program,
    reconcile_observed_section_programs,
)
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


class UnknownStudentGender(Exception):
    """Raised when a student's cohort cannot be resolved and must not be guessed."""


def gender_section_filter(gender: str) -> Q:
    """Build a Q() keeping only sections a ``gender`` student may take.

    Keeps the student's own gender sections PLUS any ungendered section (open to
    all). An unknown/blank gender returns an all-pass Q() so callers never
    accidentally hide every section. Used by both the planner's section catalog
    (display) and the build (scheduling) so they can never disagree.

    CAUTION — the all-pass branch is only safe when NO student was named. If a
    student WAS named and their gender could not be resolved, all-pass shows the
    other cohort's sections. Every real global section is gendered (415 F, 303 M,
    zero ungendered), so that failure is total rather than partial. Callers acting
    on behalf of a specific student must use :func:`student_gender_strict`, which
    refuses instead of guessing.
    """
    g = (gender or "").strip().upper()
    if g not in ("M", "F"):
        return Q()
    gendered = Q(section__istartswith="M") | Q(section__istartswith="F")
    return Q(section__istartswith=g) | ~gendered


def section_is_available_to_student(section: object, *, student_id: int | str) -> bool:
    """Whether this student's cohort may take this section.

    THE canonical per-section answer, and the one every surface must call — the
    planner, the chat hand-off, and whatever comes next. `gender_section_filter`
    answers the same question for a queryset; this answers it for a row, and the
    two deliberately share `section_gender` so they cannot drift apart.

    The rule that must not be re-implemented anywhere else: a section's cohort is
    the leading letter of its label, and a label with neither M nor F is open to
    everyone. Spelling that out at a call site would mean a change to section
    coding silently splitting the surfaces apart.

    Refuses rather than guesses for an unresolvable student, for the same reason
    `student_gender_strict` does: every real section is gendered, so an all-pass
    fallback is not a partial failure but a complete one.
    """
    required = student_gender_strict(student_id)
    label = getattr(section, "section", "") or ""
    allowed = section_gender(label)
    if allowed and allowed != required:
        return False

    # Scenario-owned sections belong to the planner scenario. Programme links
    # describe only the shared, current global section snapshot.
    if getattr(section, "scenario_id", None) is not None:
        return True

    student_program = (
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
    )
    program = normalize_section_program(student_program)
    if not program:
        # A student-scoped request must never degrade to an all-programme
        # catalogue merely because the profile is incomplete.
        return False

    # Known-programme students fail closed: an unassigned global section must
    # not leak into their choices merely because its course code is familiar.
    program_links = getattr(section, "program_links", None)
    return bool(program_links and program_links.filter(program=program).exists())


def student_gender_strict(student_id: int | str) -> str:
    """Return the student's cohort, or raise rather than fall back to all-pass.

    ``student_gender`` returns "" for a student with no ``Student`` row — and 722 of
    the 3,807 ids in ``StudentTermSection`` are exactly that. Feeding that "" into
    ``gender_section_filter`` produces an all-pass filter, so a student whose record
    is missing would be shown the other cohort's sections.

    Use this wherever the query is on behalf of a NAMED student. Keep plain
    ``student_gender`` only for staff browsing with no student in scope, where
    all-pass is the intended behaviour.
    """
    g = student_gender(student_id)
    if g not in ("M", "F"):
        raise UnknownStudentGender(
            f"Cannot resolve the cohort (M/F) for student {student_id}. Refusing to "
            "query sections, because an unresolved cohort would return the other "
            "cohort's sections rather than none."
        )
    return g


def _section_course_key(term_section: TermSection) -> str:
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


def get_student_term_registration_summary(
    student_id: int | str,
    academic_year: str,
    term: str,
    *,
    baseline_rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Registered credit hours for THE TERM ASKED FOR, and how confident that is.

    Distinct from `virtual_advisor._current_term_registrations`, which deliberately
    reports the student's LATEST stored term — its docstring says so: "the chat's
    configured term is the term being planned FOR and may differ from the term
    being studied". That is right for adviser chat and wrong for a dashboard tile
    labelled «الفصل المحدد في النظام», which asserts the configured term. Calling
    the private helper there produced a real mismatch: before registration opens,
    the configured term is 1448/1, the latest stored term is 1447/2, and the card
    showed 1447/2's hours under 1448/1's label.

    NO ROWS IS NOT ZERO. With this schema, an empty baseline can mean the student
    registered nothing, or registration has not opened, or the import has not run,
    or section mappings are incomplete — and there is no term-level completeness
    marker to tell them apart. `known=False` lets the screen show «—», which is
    true in all four cases, rather than a zero that is only true in one.

    Credits are counted ONCE PER COURSE. The baseline is one row per MEETING, so
    summing rows would multiply a 3-hour course by its three weekly sessions.
    """
    # A caller that has already applied a student-facing visibility rule (for
    # example the cohort filter on the home timetable) can pass those exact rows.
    # The number and the visible timetable must never be derived from different
    # section sets. ``None`` means "load them"; an explicit empty list remains
    # honest no-evidence, not a request to fetch again.
    rows = (
        get_student_term_baseline(student_id, str(academic_year), str(term))
        if baseline_rows is None
        else baseline_rows
    )
    if not rows:
        return {
            "value": None,
            "known": False,
            "course_count": 0,
            "source": "no_term_registration_evidence",
            "academic_year": str(academic_year),
            "term": str(term),
        }

    credits_by_course: dict[str, int] = {}
    for row in rows:
        code = str(row.get("course_key") or row.get("course_code") or "").strip().upper()
        if not code:
            continue
        raw_credits = row.get("credits")
        credits = int(raw_credits) if isinstance(raw_credits, int | float | str) else 0
        credits_by_course.setdefault(code, credits)

    return {
        "value": sum(credits_by_course.values()),
        "known": True,
        "course_count": len(credits_by_course),
        "source": "timetable_sections",
        "academic_year": str(academic_year),
        "term": str(term),
    }


def replace_student_term_sections(
    student_id: int | str,
    academic_year: str,
    term: str,
    term_section_ids: list[int],
    source: str = "manual",
    *,
    replace_all_global: bool = False,
) -> dict[str, int]:
    from django.db import transaction

    with transaction.atomic():
        current_rows = StudentTermSection.objects.filter(
            student_id=student_id,
            term_section__scenario__isnull=True,
        )
        if not replace_all_global:
            # Planner calls remain term-scoped: planning another term must not
            # erase the student's actual current registration snapshot.
            current_rows = current_rows.filter(
                academic_year=str(academic_year),
                term=str(term),
            )
        affected_section_ids = set(current_rows.values_list("term_section_id", flat=True))
        affected_section_ids.update(int(section_id) for section_id in term_section_ids)
        current_rows.delete()

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
        reconcile_observed_section_programs(affected_section_ids)

    return {"inserted": len(term_section_ids)}


def clear_student_section_snapshot(student_id: int | str) -> dict[str, int]:
    """Clear one student's current registration snapshot, not shared sections.

    The timetable service supplies no year/term metadata when a verified student
    has no summer timetable. Student-facing readers treat the latest link as
    current, so retaining any old links would falsely present a prior schedule
    as today's registration. Shared ``TermSection`` rows, meetings, and any
    scenario-owned student assignments remain.
    """
    from django.db import transaction

    with transaction.atomic():
        rows = StudentTermSection.objects.filter(
            student_id=student_id,
            term_section__scenario__isnull=True,
        )
        affected_section_ids = set(rows.values_list("term_section_id", flat=True))
        deleted = rows.delete()[0]
        reconcile_observed_section_programs(affected_section_ids)
    return {"deleted": deleted}
