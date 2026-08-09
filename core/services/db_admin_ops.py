import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.db.models import Count, Q, QuerySet

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.section_programmes import reconcile_observed_section_programs

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REQ_CSV = BASE_DIR / "data" / "department_courses.csv"
DEFAULT_PRE_CSV = BASE_DIR / "data" / "department_prerequisites.csv"
BACKUP_DIR = BASE_DIR / "runtime" / "db_backups"


SECTION_SNAPSHOT_TOKEN_SALT = "core.db-admin.section-snapshot.v1"
SECTION_SNAPSHOT_TOKEN_MAX_AGE_SECONDS = 10 * 60
_SECTION_SNAPSHOT_CLEAR_LOCK = threading.Lock()


class _SectionStudentLinkRow(TypedDict):
    id: int
    term_section_id: int
    student_id: int


def _is_sqlite() -> bool:
    """Return True if the default database engine is SQLite."""
    engine = settings.DATABASES["default"].get("ENGINE", "")
    return "sqlite" in engine.lower()


def create_backup_snapshot() -> dict[str, Any]:
    if not _is_sqlite():
        # PostgreSQL backups are managed by the hosting provider (e.g. Render).
        return {
            "ok": True,
            "skipped": True,
            "message": "Database backups are managed by the hosting provider. File-level snapshots are only available for SQLite.",
        }
    db_path = Path(str(settings.DATABASES["default"]["NAME"]))
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database file not found: {db_path}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = BACKUP_DIR / f"db_{ts}_{uuid.uuid4().hex[:8]}.sqlite3"

    # Copy through SQLite's online backup API. A raw copy of only the main
    # database file is not a valid snapshot while committed pages remain in a
    # WAL file (for example when an active reader prevents a checkpoint).
    source_db: sqlite3.Connection | None = None
    destination_db: sqlite3.Connection | None = None
    backup_complete = False
    try:
        source_db = sqlite3.connect(str(db_path), timeout=30.0)
        source_db.execute("PRAGMA query_only = ON")
        destination_db = sqlite3.connect(str(backup_path), timeout=30.0)
        source_db.backup(destination_db)
        backup_complete = True
    finally:
        if destination_db is not None:
            destination_db.close()
        if source_db is not None:
            source_db.close()
        if not backup_complete:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "ok": True,
        "backup_file": backup_path.name,  # filename only, no full path
        "size_bytes": int(backup_path.stat().st_size),
    }


def run_integrity_checks() -> dict[str, Any]:
    if _is_sqlite():
        with connection.cursor() as cur:
            cur.execute("PRAGMA integrity_check")
            pragma_row = cur.fetchone()
            integrity_result = str(pragma_row[0]) if pragma_row else "unknown"
    else:
        # PostgreSQL: no PRAGMA equivalent; managed databases handle integrity.
        integrity_result = "ok (managed database)"

    # Orphan student_courses (student_id not in students table)
    orphan_student_courses = StudentCourse.objects.filter(
        ~Q(student_id__in=Student.objects.values_list("student_id", flat=True))
    ).count()

    # Duplicate prerequisite triplets
    from django.db.models import Count

    duplicate_prereq_triplets = (
        Prerequisite.objects.values("program", "course_code", "prerequisite_course_code")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
        .count()
    )

    invalid_credit_rows = ProgrammeRequirement.objects.filter(
        Q(credit_hours__isnull=True) | Q(credit_hours__lte=0)
    ).count()

    invalid_term_rows = (
        ProgrammeRequirement.objects.exclude(
            programme_term__range=(1, 10),
        )
        .filter(
            Q(programme_term__isnull=False),
        )
        .count()
        + ProgrammeRequirement.objects.filter(programme_term__isnull=True).count()
    )

    return {
        "ok": True,
        "integrity_check": integrity_result,
        "orphan_student_courses": orphan_student_courses,
        "duplicate_prerequisite_triplets": duplicate_prereq_triplets,
        "invalid_credit_rows": invalid_credit_rows,
        "invalid_programme_term_rows": invalid_term_rows,
        "advice": {
            "orphan_student_courses": "Delete orphan rows or re-insert missing students.",
            "duplicate_prerequisite_triplets": "Deduplicate prerequisites table for exact triplets.",
            "invalid_credit_rows": "Fix source catalog rows with non-positive credit hours.",
            "invalid_programme_term_rows": "Fix programme_term outside 1..10 range.",
        },
    }


def preview_delete_students(
    program: str | None = None, section: str | None = None
) -> dict[str, Any]:
    qs = Student.objects.all()
    if program:
        qs = qs.filter(program=program)
    if section:
        qs = qs.filter(section=section)
    students_count = qs.count()

    sc_qs = StudentCourse.objects.all()
    if program and section:
        sc_qs = sc_qs.filter(student__program=program, student__section=section)
    elif program:
        sc_qs = sc_qs.filter(student__program=program)
    elif section:
        sc_qs = sc_qs.filter(student__section=section)
    student_courses_count = sc_qs.count()

    student_ids = qs.values_list("student_id", flat=True)
    student_term_sections = StudentTermSection.objects.filter(student_id__in=student_ids)
    student_term_sections_count = student_term_sections.count()
    affected_term_sections_count = (
        student_term_sections.values("term_section_id").distinct().count()
    )

    return {
        "students_count": students_count,
        "student_courses_count": student_courses_count,
        "student_term_sections_count": student_term_sections_count,
        "affected_term_sections_count": affected_term_sections_count,
        "program": program,
        "section": section,
    }


def delete_students(program: str | None = None, section: str | None = None) -> dict[str, Any]:
    preview = preview_delete_students(program=program, section=section)
    backup = create_backup_snapshot()

    with transaction.atomic():
        qs = Student.objects.all()
        if program:
            qs = qs.filter(program=program)
        if section:
            qs = qs.filter(section=section)
        student_ids = list(qs.values_list("student_id", flat=True))

        timetable_links = StudentTermSection.objects.filter(student_id__in=student_ids)
        affected_term_section_ids = set(timetable_links.values_list("term_section_id", flat=True))
        student_term_sections_deleted = timetable_links.delete()[0]
        student_courses_deleted = StudentCourse.objects.filter(student_id__in=student_ids).delete()[
            0
        ]
        qs.delete()
        reconcile_observed_section_programs(affected_term_section_ids)

    return {
        "ok": True,
        "backup": backup,
        **preview,
        "deleted_students": len(student_ids),
        "deleted_student_courses": student_courses_deleted,
        "deleted_student_term_sections": student_term_sections_deleted,
    }


def preview_delete_program_catalog(program: str) -> dict[str, Any]:
    requirements_count = ProgrammeRequirement.objects.filter(program=program).count()
    prerequisites_count = Prerequisite.objects.filter(program=program).count()
    return {
        "program": program,
        "requirements_count": requirements_count,
        "prerequisites_count": prerequisites_count,
    }


def delete_program_catalog(program: str) -> dict[str, Any]:
    preview = preview_delete_program_catalog(program)
    backup = create_backup_snapshot()

    with transaction.atomic():
        Prerequisite.objects.filter(program=program).delete()
        ProgrammeRequirement.objects.filter(program=program).delete()

    return {"ok": True, "backup": backup, **preview}


def import_program_plan(
    program: str, csv_text: str, replace_existing: bool = False
) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"course_code", "programme_term", "credit_hours"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("CSV must include headers: course_code, programme_term, credit_hours")

    has_max_capacity = "max_capacity" in (reader.fieldnames or [])

    rows: list[dict[str, Any]] = []
    for row in reader:
        code = str(row.get("course_code", "")).strip().upper().replace(" ", "")
        if not code:
            continue
        pterm = int(str(row.get("programme_term", "0")).strip())
        credits = int(str(row.get("credit_hours", "0")).strip())
        ctype = str(row.get("type", "CORE")).strip() or "CORE"
        entry: dict[str, Any] = {
            "program": program,
            "course_code": code,
            "type": ctype,
            "programme_term": pterm,
            "credit_hours": credits,
        }
        if has_max_capacity:
            raw_cap = str(row.get("max_capacity", "")).strip()
            entry["max_capacity"] = int(raw_cap) if raw_cap else None
        rows.append(entry)

    if not rows:
        raise ValueError("CSV contains no valid rows")

    backup: dict[str, Any] | None = None
    if replace_existing:
        backup = create_backup_snapshot()

    inserted = 0
    with transaction.atomic():
        if replace_existing:
            ProgrammeRequirement.objects.filter(program=program).delete()

        for r in rows:
            defaults: dict[str, Any] = {
                "type": r["type"],
                "programme_term": r["programme_term"],
                "credit_hours": r["credit_hours"],
            }
            if "max_capacity" in r:
                defaults["max_capacity"] = r["max_capacity"]
            ProgrammeRequirement.objects.update_or_create(
                program=r["program"],
                course_code=r["course_code"],
                defaults=defaults,
            )
            inserted += 1

    return {
        "ok": True,
        "program": program,
        "rows_processed": len(rows),
        "rows_upserted": inserted,
        "replace_existing": replace_existing,
        "backup": backup,
    }


def legacy_load_department_files_exact(
    requirements_csv_path: str | None = None,
    prerequisites_csv_path: str | None = None,
) -> dict[str, Any]:
    req_path = Path(requirements_csv_path) if requirements_csv_path else DEFAULT_REQ_CSV
    pre_path = Path(prerequisites_csv_path) if prerequisites_csv_path else DEFAULT_PRE_CSV

    if not req_path.exists():
        raise ValueError(f"requirements csv not found: {req_path}")
    if not pre_path.exists():
        raise ValueError(f"prerequisites csv not found: {pre_path}")

    req_count = 0
    pre_count = 0

    with transaction.atomic():
        with req_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"program", "course_code", "type", "programme_term", "credit_hours"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    "requirements csv must include: program, course_code, type, programme_term, credit_hours"
                )

            for row in reader:
                ProgrammeRequirement.objects.update_or_create(
                    program=str(row["program"]).strip(),
                    course_code=str(row["course_code"]).replace(" ", "").upper(),
                    defaults={
                        "type": str(row["type"]).strip(),
                        "programme_term": int(str(row["programme_term"]).strip()),
                        "credit_hours": int(str(row["credit_hours"]).strip()),
                    },
                )
                req_count += 1

        with pre_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"program", "course_code", "prerequisite_course_code"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    "prerequisites csv must include: program, course_code, prerequisite_course_code"
                )

            for row in reader:
                Prerequisite.objects.create(
                    program=str(row["program"]).strip(),
                    course_code=str(row["course_code"]).replace(" ", "").upper(),
                    prerequisite_course_code=str(row["prerequisite_course_code"])
                    .replace(" ", "")
                    .upper(),
                )
                pre_count += 1

    return {
        "ok": True,
        "mode": "legacy_exact",
        "requirements_csv": str(req_path),
        "prerequisites_csv": str(pre_path),
        "requirements_loaded": req_count,
        "prerequisites_loaded": pre_count,
    }


def preview_oracle_plan(
    program: str,
    encoding: str = "windows-1256",
    filepath: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Parse an Oracle study-plan export and return a preview (no DB writes).

    Returns a dict with ``preview_rows`` (flat list ready for editable table),
    ``metadata``, ``summary``, ``warnings``, and existing DB row counts.
    """
    from core.services.oracle_plan_parser import map_course_type, parse_oracle_plan

    parsed = parse_oracle_plan(filepath, encoding=encoding, content=content)

    # Flatten courses into a list the frontend can render as editable rows.
    preview_rows: list[dict[str, Any]] = []
    for _level_key, level_data in parsed["levels"].items():
        for course in level_data["courses"]:
            delivery = course.get("delivery", "")
            preview_rows.append(
                {
                    "code": course["code"],
                    "code_ar": course.get("code_ar", ""),
                    "en_name": course["en_name"],
                    "ar_name": course.get("ar_name", ""),
                    "credits": course["credits"],
                    "level_number": course["level_number"],
                    "level_en": course["level_en"],
                    "level_ar": course.get("level_ar", ""),
                    "type": map_course_type(course.get("course_type", "")),
                    "prereqs_str": ", ".join(course.get("prereqs", [])),
                    "delivery": delivery,
                    "is_online": 1 if "إلكتروني" in delivery else 0,
                }
            )

    # Existing DB row counts for this program (helps the user decide).
    existing_requirements = ProgrammeRequirement.objects.filter(program=program).count()
    existing_prerequisites = Prerequisite.objects.filter(program=program).count()

    return {
        "ok": True,
        "metadata": parsed["metadata"],
        "summary": parsed["summary"],
        "warnings": parsed["warnings"],
        "preview_rows": preview_rows,
        "existing_db": {
            "requirements": existing_requirements,
            "prerequisites": existing_prerequisites,
        },
    }


def import_oracle_plan_from_rows(
    program: str,
    rows: list[dict[str, Any]],
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Insert user-edited Oracle plan rows into the database.

    *rows* is a list of dicts coming from the editable frontend table::

        {code, en_name, credits, level_number, type, prereqs_str}

    ``prereqs_str`` is a comma-separated string of prerequisite course codes.
    ``en_name`` is stored both in the global Course catalogue and on the
    ProgrammeRequirement row so same-code courses can keep plan-specific names.

    Returns counts: ``{requirements_upserted, prerequisites_inserted, courses_upserted}``.
    """
    if not rows:
        raise ValueError("No rows to import")

    backup = create_backup_snapshot()

    requirements_upserted = 0
    prerequisites_inserted = 0
    courses_upserted = 0

    with transaction.atomic():
        if replace_existing:
            Prerequisite.objects.filter(program=program).delete()
            ProgrammeRequirement.objects.filter(program=program).delete()

        for row in rows:
            code = str(row.get("code", "")).strip().upper().replace(" ", "")
            if not code:
                continue

            credits = int(str(row.get("credits", 0)).strip() or 0)
            level_number = int(str(row.get("level_number", 0)).strip() or 0)
            course_type = str(row.get("type", "Mandatory")).strip() or "Mandatory"
            en_name = str(row.get("en_name", row.get("course_name", ""))).strip()
            is_online = bool(int(str(row.get("is_online", 0)).strip() or 0))

            # Upsert ProgrammeRequirement
            ProgrammeRequirement.objects.update_or_create(
                program=program,
                course_code=code,
                defaults={
                    "type": course_type,
                    "programme_term": level_number,
                    "credit_hours": credits,
                    "is_online": is_online,
                    "course_name": en_name,
                },
            )
            requirements_upserted += 1

            # Prerequisites — delete existing for this course then re-insert.
            if not replace_existing:
                Prerequisite.objects.filter(program=program, course_code=code).delete()

            prereqs_str = str(row.get("prereqs_str", "")).strip()
            if prereqs_str:
                for p in prereqs_str.split(","):
                    p = p.strip().upper().replace(" ", "")
                    if p:
                        Prerequisite.objects.create(
                            program=program,
                            course_code=code,
                            prerequisite_course_code=p,
                        )
                        prerequisites_inserted += 1

            # Upsert Course metadata
            Course.objects.update_or_create(
                course_code=code,
                defaults={
                    "description": en_name,
                    "credit_hours": credits,
                },
            )
            courses_upserted += 1

    return {
        "ok": True,
        "program": program,
        "replace_existing": replace_existing,
        "requirements_upserted": requirements_upserted,
        "prerequisites_inserted": prerequisites_inserted,
        "courses_upserted": courses_upserted,
        "backup": backup,
    }


def list_external_courses() -> dict[str, Any]:
    """List all external (non-plan) courses with student counts."""
    from django.db.models import Count

    courses = (
        Course.objects.filter(is_external=True)
        .annotate(
            student_count=Count("student_courses", filter=Q(student_courses__status="studying"))
        )
        .order_by("course_code")
        .values(
            "course_id", "course_code", "department", "description", "credit_hours", "student_count"
        )
    )
    items = list(courses)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
    }


def delete_external_courses(course_ids: list[int] | None = None) -> dict[str, Any]:
    """Delete external courses and their associated records.

    If *course_ids* is ``None``, deletes ALL external courses.
    Otherwise deletes only the specified ones.
    """
    backup = create_backup_snapshot()

    qs = Course.objects.filter(is_external=True)
    if course_ids is not None:
        qs = qs.filter(course_id__in=course_ids)

    course_pks = list(qs.values_list("course_id", flat=True))
    course_codes = list(qs.values_list("course_code", flat=True))

    with transaction.atomic():
        # Delete student_courses referencing these external courses
        sc_deleted = StudentCourse.objects.filter(course_id__in=course_pks).delete()[0]

        # Delete term_section_meetings and student_term_sections for external term_sections
        ext_ts_ids = list(
            TermSection.objects.filter(
                scenario__isnull=True,  # external sections are always global
                course_key__in=course_codes,
                source_tag="external",
            ).values_list("id", flat=True)
        )
        sts_deleted = (
            StudentTermSection.objects.filter(term_section_id__in=ext_ts_ids).delete()[0]
            if ext_ts_ids
            else 0
        )
        tsm_deleted = (
            TermSectionMeeting.objects.filter(term_section_id__in=ext_ts_ids).delete()[0]
            if ext_ts_ids
            else 0
        )
        ts_deleted = TermSection.objects.filter(id__in=ext_ts_ids).delete()[0] if ext_ts_ids else 0

        # Delete the external courses themselves
        courses_deleted = qs.delete()[0]

    return {
        "ok": True,
        "backup": backup,
        "courses_deleted": courses_deleted,
        "student_courses_deleted": sc_deleted,
        "term_sections_deleted": ts_deleted,
        "term_section_meetings_deleted": tsm_deleted,
        "student_term_sections_deleted": sts_deleted,
    }


# ── Elective Catalogue ─────────────────────────────────────────────


def import_elective_catalogue(
    programme: str,
    rows: list[dict],
) -> dict:
    """Import or update the elective course catalogue for a programme.

    Each row must have at minimum ``course_code`` and ``course_name``.
    Optional keys: ``prerequisites`` (comma-separated), ``category``,
    ``credit_hours``.

    Returns a summary dict with counts of created and updated records.
    """
    programme = programme.strip().upper()
    created = 0
    updated = 0

    for row in rows:
        code = str(row.get("course_code", "")).strip().upper().replace(" ", "")
        name = str(row.get("course_name", "")).strip()
        if not code or not name:
            continue

        prereqs = str(row.get("prerequisites", "")).strip()
        category = str(row.get("category", "")).strip().upper()
        credit_hours = int(row.get("credit_hours", 3) or 3)

        _obj, was_created = ElectiveCourse.objects.update_or_create(
            programme=programme,
            course_code=code,
            defaults={
                "course_name": name,
                "category": category or programme,
                "credit_hours": credit_hours,
                "prerequisites_csv": prereqs,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return {
        "ok": True,
        "programme": programme,
        "created": created,
        "updated": updated,
        "total": created + updated,
    }


def set_elective_term_mapping(
    academic_year: str,
    term: int,
    programme: str,
    mappings: list[dict],
) -> dict:
    """Set elective-to-placeholder mappings for a specific term.

    Replaces all existing mappings for the given (year, term, programme).
    Each mapping dict must have ``placeholder_code`` and ``course_code``.

    Returns a summary dict.
    """
    programme = programme.strip().upper()
    academic_year = str(academic_year).strip()

    # Clear existing mappings for this term/programme
    deleted, _ = ElectiveTermMapping.objects.filter(
        academic_year=academic_year,
        term=term,
        programme=programme,
    ).delete()

    created = 0
    errors: list[str] = []

    for m in mappings:
        placeholder = str(m.get("placeholder_code", "")).strip().upper().replace(" ", "")
        course_code = str(m.get("course_code", "")).strip().upper().replace(" ", "")
        if not placeholder or not course_code:
            continue

        try:
            elective = ElectiveCourse.objects.get(programme=programme, course_code=course_code)
        except ElectiveCourse.DoesNotExist:
            errors.append(f"{course_code} not found in {programme} catalogue")
            continue

        ElectiveTermMapping.objects.create(
            academic_year=academic_year,
            term=term,
            programme=programme,
            placeholder_code=placeholder,
            elective=elective,
        )
        created += 1

    return {
        "ok": True,
        "programme": programme,
        "academic_year": academic_year,
        "term": term,
        "cleared": deleted,
        "created": created,
        "errors": errors,
    }


# -- Current section snapshot maintenance ------------------------------------


class SectionSnapshotClearError(ValueError):
    """A safe, user-facing failure from the section snapshot workflow."""

    code = "section_snapshot_error"
    status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class SectionSnapshotPreviewStale(SectionSnapshotClearError):
    code = "section_snapshot_preview_stale"
    status_code = 409


class SectionSnapshotBusy(SectionSnapshotClearError):
    code = "section_snapshot_busy"
    status_code = 409


def _normalise_section_snapshot_scope(
    *,
    program: object,
    gender: object,
    all_programs: object,
) -> dict[str, Any]:
    if not isinstance(all_programs, bool):
        raise SectionSnapshotClearError("all_programs must be a boolean")

    normalised_program = str(program or "").strip().upper()
    normalised_gender = str(gender or "ALL").strip().upper()
    if normalised_gender not in {"ALL", "M", "F"}:
        raise SectionSnapshotClearError("gender must be one of ALL, M, or F")
    if all_programs and normalised_program:
        raise SectionSnapshotClearError("program must be empty when all_programs is true")
    if not all_programs and not normalised_program:
        raise SectionSnapshotClearError("program is required when all_programs is false")

    return {
        "program": normalised_program,
        "gender": normalised_gender,
        "all_programs": all_programs,
    }


def _section_snapshot_candidates(scope: dict[str, Any]) -> QuerySet[TermSection]:
    qs = TermSection.objects.filter(scenario__isnull=True)
    gender = scope["gender"]
    if gender != "ALL":
        qs = qs.filter(section__istartswith=gender)
    return qs


def _section_snapshot_fingerprint(scope: dict[str, Any]) -> str:
    """Hash every row that can change the meaning or impact of *scope*.

    The hash deliberately covers all gender-matching global sections, not only
    the selected programme. Assigning an unowned section to the selected
    programme therefore invalidates an older preview instead of letting the
    destructive request proceed with an outdated target set.
    """

    candidate_qs = _section_snapshot_candidates(scope)
    candidate_ids = list(candidate_qs.order_by("id").values_list("id", flat=True))

    sections = list(
        candidate_qs.order_by("id").values_list(
            "id",
            "course_key",
            "course_code",
            "course_number",
            "section",
            "source_tag",
            "available_capacity",
            "registered_count",
            "updated_at",
        )
    )
    memberships = list(
        TermSectionProgram.objects.filter(term_section_id__in=candidate_ids)
        .order_by("id")
        .values_list("id", "term_section_id", "program", "assignment_source")
    )
    meetings = list(
        TermSectionMeeting.objects.filter(term_section_id__in=candidate_ids)
        .order_by("id")
        .values_list(
            "id",
            "term_section_id",
            "day",
            "start_time",
            "end_time",
            "building",
            "floor_wing",
            "room",
            "instructor",
            "updated_at",
        )
    )
    student_links = list(
        StudentTermSection.objects.filter(term_section_id__in=candidate_ids)
        .order_by("id")
        .values_list(
            "id",
            "term_section_id",
            "student_id",
            "academic_year",
            "term",
            "source",
            "updated_at",
        )
    )
    linked_student_ids = sorted({int(row[2]) for row in student_links})
    student_programs = list(
        Student.objects.filter(student_id__in=linked_student_ids)
        .order_by("student_id")
        .values_list("student_id", "program")
    )

    # These rows are not deleted by this operation, but their presence prevents
    # a physical section row from being deleted so planner workspaces survive.
    placements = list(
        candidate_qs.filter(placements__isnull=False)
        .order_by("id", "placements__id")
        .values_list("id", "placements__id")
    )
    visibility = list(
        candidate_qs.filter(board_visibility__isnull=False)
        .order_by("id", "board_visibility__id")
        .values_list("id", "board_visibility__id")
    )

    serialisable = {
        "scope": scope,
        "sections": sections,
        "memberships": memberships,
        "meetings": meetings,
        "student_links": student_links,
        "student_programs": student_programs,
        "placements": placements,
        "visibility": visibility,
    }
    raw = json.dumps(
        serialisable,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _build_section_snapshot_preview(scope: dict[str, Any]) -> dict[str, Any]:
    fingerprint_before = _section_snapshot_fingerprint(scope)
    candidate_qs = _section_snapshot_candidates(scope)

    unassigned_qs = candidate_qs.annotate(program_count=Count("program_links")).filter(
        program_count=0
    )
    unassigned_samples = list(
        unassigned_qs.order_by("course_key", "section", "id").values(
            "id", "course_key", "section", "source_tag"
        )[:10]
    )
    unassigned_count = unassigned_qs.count()

    if scope["all_programs"]:
        selected_qs = candidate_qs
    else:
        selected_qs = candidate_qs.filter(program_links__program=scope["program"]).distinct()
    selected_ids = list(selected_qs.order_by("id").values_list("id", flat=True))

    membership_rows = list(
        TermSectionProgram.objects.filter(term_section_id__in=selected_ids)
        .order_by("term_section_id", "program", "id")
        .values("id", "term_section_id", "program", "assignment_source")
    )
    student_link_rows: list[_SectionStudentLinkRow] = list(
        StudentTermSection.objects.filter(term_section_id__in=selected_ids)
        .order_by("term_section_id", "student_id", "id")
        .values("id", "term_section_id", "student_id")
    )

    programs_by_section: dict[int, list[str]] = {section_id: [] for section_id in selected_ids}
    for membership_row in membership_rows:
        programs_by_section[int(membership_row["term_section_id"])].append(
            str(membership_row["program"])
        )

    links_by_section: dict[int, list[_SectionStudentLinkRow]] = {
        section_id: [] for section_id in selected_ids
    }
    for student_link_row in student_link_rows:
        links_by_section[int(student_link_row["term_section_id"])].append(student_link_row)

    if scope["all_programs"]:
        affected_link_ids = {int(row["id"]) for row in student_link_rows}
        physical_delete_ids = set(selected_ids)
        membership_delete_ids = {int(row["id"]) for row in membership_rows}
        shared_section_ids: set[int] = set()
    else:
        # ``Student.program`` is legacy free text, while section memberships are
        # canonical upper-case codes. Compare the linked students using the same
        # trim + case normalization so values such as ``" ai "`` cannot survive
        # an AI-scoped clear and make the section look spuriously shared.
        linked_student_ids = {int(row["student_id"]) for row in student_link_rows}
        target_student_ids = {
            int(student_id)
            for student_id, raw_program in Student.objects.filter(
                student_id__in=linked_student_ids
            ).values_list("student_id", "program")
            if str(raw_program or "").strip().upper() == scope["program"]
        }
        affected_link_ids = {
            int(row["id"])
            for row in student_link_rows
            if int(row["student_id"]) in target_student_ids
        }
        membership_delete_ids = {
            int(row["id"]) for row in membership_rows if str(row["program"]) == scope["program"]
        }

        physical_delete_ids = set()
        shared_section_ids = set()
        for section_id in selected_ids:
            other_programs = [
                program
                for program in programs_by_section[section_id]
                if program != scope["program"]
            ]
            remaining_links = [
                row
                for row in links_by_section[section_id]
                if int(row["id"]) not in affected_link_ids
            ]
            if other_programs or remaining_links:
                shared_section_ids.add(section_id)
            else:
                physical_delete_ids.add(section_id)

    # Global sections can be attached to a scenario's delivery board. Retain
    # those physical rows rather than cascading into planner workspaces.
    protected_section_ids = set(
        selected_qs.filter(Q(placements__isnull=False) | Q(board_visibility__isnull=False))
        .distinct()
        .values_list("id", flat=True)
    )
    physical_delete_ids -= protected_section_ids
    retained_section_ids = set(selected_ids) - physical_delete_ids

    meetings_to_delete = TermSectionMeeting.objects.filter(
        term_section_id__in=physical_delete_ids
    ).count()

    if scope["all_programs"]:
        affected_student_rows = student_link_rows
    else:
        affected_student_rows = [
            row for row in student_link_rows if int(row["id"]) in affected_link_ids
        ]
    distinct_students = len({int(row["student_id"]) for row in affected_student_rows})

    source_rows = (
        selected_qs.values("source_tag").annotate(count=Count("id")).order_by("source_tag")
    )
    source_breakdown = {str(row["source_tag"] or "other"): int(row["count"]) for row in source_rows}

    meeting_counts = {
        int(row["term_section_id"]): int(row["count"])
        for row in TermSectionMeeting.objects.filter(term_section_id__in=selected_ids)
        .values("term_section_id")
        .annotate(count=Count("id"))
    }
    samples: list[dict[str, Any]] = []
    for section in selected_qs.order_by("course_key", "section", "id")[:25]:
        section_id = int(section.id)
        section_link_rows = links_by_section.get(section_id, [])
        samples.append(
            {
                "id": section_id,
                "course_key": section.course_key,
                "course_name": section.course_name,
                "section": section.section,
                "source_tag": section.source_tag,
                "programs": programs_by_section.get(section_id, []),
                "meetings_count": meeting_counts.get(section_id, 0),
                "student_links_count": len(section_link_rows),
                "student_links_affected": sum(
                    1 for row in section_link_rows if int(row["id"]) in affected_link_ids
                ),
                "action": "delete" if section_id in physical_delete_ids else "retain",
                "retained_reason": (
                    "planner_reference"
                    if section_id in protected_section_ids
                    else ("shared" if section_id in shared_section_ids else "")
                ),
            }
        )

    warnings: list[dict[str, Any]] = []
    if unassigned_count:
        warnings.append(
            {
                "code": "unassigned_sections",
                "count": unassigned_count,
                "included": bool(scope["all_programs"]),
                "message": (
                    f"{unassigned_count} unassigned section(s) are included in this clear."
                    if scope["all_programs"]
                    else f"{unassigned_count} unassigned section(s) cannot be matched to this programme and will be retained."
                ),
            }
        )
    if shared_section_ids:
        warnings.append(
            {
                "code": "shared_sections_retained",
                "count": len(shared_section_ids),
                "message": "Shared section rows and their meetings will be retained for other programmes or students.",
            }
        )
    if protected_section_ids:
        warnings.append(
            {
                "code": "planner_sections_retained",
                "count": len(protected_section_ids),
                "message": "Sections referenced by planner workspaces will be retained to protect scenario data.",
            }
        )

    fingerprint_after = _section_snapshot_fingerprint(scope)
    if fingerprint_before != fingerprint_after:
        raise SectionSnapshotPreviewStale(
            "Section data changed while the preview was being built. Generate the preview again."
        )

    counts = {
        "affected_sections": len(selected_ids),
        "physical_sections_to_delete": len(physical_delete_ids),
        "retained_sections": len(retained_section_ids),
        "shared_sections_retained": len(shared_section_ids),
        "planner_sections_retained": len(protected_section_ids),
        "program_memberships_to_delete": len(membership_delete_ids),
        "meetings_to_delete": meetings_to_delete,
        "student_links_to_delete": len(affected_link_ids),
        "distinct_students_affected": distinct_students,
    }
    return {
        "ok": True,
        "scope": scope,
        "counts": counts,
        # Flat aliases keep the browser contract simple and explicit.
        "sections_count": counts["affected_sections"],
        "physical_sections_count": counts["physical_sections_to_delete"],
        "retained_sections_count": counts["retained_sections"],
        "shared_sections_count": counts["shared_sections_retained"],
        "shared_retained_count": counts["shared_sections_retained"],
        "protected_sections_count": counts["planner_sections_retained"],
        "memberships_count": counts["program_memberships_to_delete"],
        "meetings_count": counts["meetings_to_delete"],
        "student_links_count": counts["student_links_to_delete"],
        "students_count": counts["distinct_students_affected"],
        "source_breakdown": source_breakdown,
        "samples": samples,
        "warnings": warnings,
        "unassigned": {
            "count": unassigned_count,
            "included": bool(scope["all_programs"]),
            "samples": unassigned_samples,
        },
        "_selected_ids": selected_ids,
        "_physical_delete_ids": sorted(physical_delete_ids),
        "_membership_delete_ids": sorted(membership_delete_ids),
        "_affected_link_ids": sorted(affected_link_ids),
        "_fingerprint": fingerprint_after,
    }


def _public_section_snapshot_preview(preview: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in preview.items() if not key.startswith("_")}


def preview_clear_section_snapshot(
    *,
    program: object,
    gender: object,
    all_programs: object,
    user_id: object,
) -> dict[str, Any]:
    """Preview an exact, current-snapshot clear and issue a signed token."""

    _raise_if_scraper_running()
    scope = _normalise_section_snapshot_scope(
        program=program,
        gender=gender,
        all_programs=all_programs,
    )
    preview = _build_section_snapshot_preview(scope)
    token_payload = {
        "version": 1,
        "purpose": "clear-current-section-snapshot",
        "user_id": str(user_id),
        "scope": scope,
        "fingerprint": preview["_fingerprint"],
        "affected_sections_count": preview["sections_count"],
    }
    token = signing.dumps(
        token_payload,
        salt=SECTION_SNAPSHOT_TOKEN_SALT,
        compress=True,
    )
    result = _public_section_snapshot_preview(preview)
    result.update(
        {
            "preview_token": token,
            "confirmation_phrase": f"CLEAR {preview['sections_count']}",
            "preview_expires_in_seconds": SECTION_SNAPSHOT_TOKEN_MAX_AGE_SECONDS,
        }
    )
    return result


def _load_section_snapshot_token(token: str, *, user_id: object) -> dict[str, Any]:
    if not token:
        raise SectionSnapshotClearError("preview_token is required")
    try:
        payload = signing.loads(
            token,
            salt=SECTION_SNAPSHOT_TOKEN_SALT,
            max_age=SECTION_SNAPSHOT_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired as exc:
        raise SectionSnapshotPreviewStale(
            "The preview expired. Generate a new preview before clearing."
        ) from exc
    except signing.BadSignature as exc:
        raise SectionSnapshotClearError("Invalid preview token") from exc

    if not isinstance(payload, dict):
        raise SectionSnapshotClearError("Invalid preview token")
    if payload.get("purpose") != "clear-current-section-snapshot" or payload.get("version") != 1:
        raise SectionSnapshotClearError("Invalid preview token")
    if str(payload.get("user_id")) != str(user_id):
        raise SectionSnapshotClearError("This preview belongs to a different administrator")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise SectionSnapshotClearError("Invalid preview token")
    payload["scope"] = _normalise_section_snapshot_scope(
        program=scope.get("program"),
        gender=scope.get("gender"),
        all_programs=scope.get("all_programs"),
    )
    return payload


def _raise_if_scraper_running() -> None:
    from core.services.scrape_ops import get_scrape_status

    status = get_scrape_status()
    if status.get("running"):
        raise SectionSnapshotBusy(
            "The batch scraper is running. Stop it before clearing the section snapshot.",
            details={
                "pid": status.get("pid"),
                "started_at": status.get("started_at"),
            },
        )


def clear_section_snapshot(
    *,
    preview_token: str,
    confirm: str,
    user_id: object,
) -> dict[str, Any]:
    """Execute a previously previewed current-section snapshot clear."""

    token_payload = _load_section_snapshot_token(preview_token, user_id=user_id)
    scope = token_payload["scope"]

    if not _SECTION_SNAPSHOT_CLEAR_LOCK.acquire(blocking=False):
        raise SectionSnapshotBusy("Another section snapshot clear is already running")
    try:
        from core.services.section_snapshot_guard import section_snapshot_operation_guard

        with section_snapshot_operation_guard(blocking=False) as acquired:
            if not acquired:
                raise SectionSnapshotBusy(
                    "Section data maintenance is starting or already running; retry shortly"
                )

            _raise_if_scraper_running()
            current = _build_section_snapshot_preview(scope)
            if current["_fingerprint"] != str(token_payload.get("fingerprint", "")):
                raise SectionSnapshotPreviewStale(
                    "Section data changed after the preview. Generate a new preview before clearing."
                )

            expected_confirmation = f"CLEAR {current['sections_count']}"
            if str(confirm).strip() != expected_confirmation:
                raise SectionSnapshotClearError(
                    f"Confirmation required: {expected_confirmation}",
                    details={"expected_confirmation": expected_confirmation},
                )
            if current["sections_count"] == 0:
                raise SectionSnapshotClearError(
                    "No sections match this scope; there is nothing to clear"
                )

            backup = create_backup_snapshot()

            with transaction.atomic():
                # Lock the target section rows where the database supports it, then
                # verify the preview once more inside the mutation transaction.
                list(
                    TermSection.objects.select_for_update()
                    .filter(id__in=current["_selected_ids"])
                    .values_list("id", flat=True)
                )
                _raise_if_scraper_running()
                locked = _build_section_snapshot_preview(scope)
                if locked["_fingerprint"] != current["_fingerprint"]:
                    raise SectionSnapshotPreviewStale(
                        "Section data changed while clearing. Nothing was deleted; preview again."
                    )

                student_links_deleted = StudentTermSection.objects.filter(
                    id__in=locked["_affected_link_ids"]
                ).delete()[0]
                memberships_deleted = TermSectionProgram.objects.filter(
                    id__in=locked["_membership_delete_ids"]
                ).delete()[0]
                meetings_deleted = TermSectionMeeting.objects.filter(
                    term_section_id__in=locked["_physical_delete_ids"]
                ).delete()[0]
                sections_deleted = TermSection.objects.filter(
                    id__in=locked["_physical_delete_ids"],
                    scenario__isnull=True,
                ).delete()[0]

            result = _public_section_snapshot_preview(locked)
            result.update(
                {
                    "ok": True,
                    "backup": backup,
                    "deleted": {
                        "sections": sections_deleted,
                        "meetings": meetings_deleted,
                        "program_memberships": memberships_deleted,
                        "student_links": student_links_deleted,
                    },
                }
            )
            return result
    finally:
        _SECTION_SNAPSHOT_CLEAR_LOCK.release()
