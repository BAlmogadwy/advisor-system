import csv
import io
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REQ_CSV = BASE_DIR / "data" / "department_courses.csv"
DEFAULT_PRE_CSV = BASE_DIR / "data" / "department_prerequisites.csv"
BACKUP_DIR = BASE_DIR / "runtime" / "db_backups"


def create_backup_snapshot() -> dict[str, Any]:
    db_path = Path(str(settings.DATABASES["default"]["NAME"]))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"db_{ts}.sqlite3"
    shutil.copy2(db_path, backup_path)
    return {
        "ok": True,
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "size_bytes": int(backup_path.stat().st_size),
    }


def run_integrity_checks() -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute("PRAGMA integrity_check")
        pragma_row = cur.fetchone()
        integrity_result = str(pragma_row[0]) if pragma_row else "unknown"

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

    return {
        "students_count": students_count,
        "student_courses_count": student_courses_count,
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

        StudentCourse.objects.filter(student_id__in=student_ids).delete()
        qs.delete()

    return {"ok": True, "backup": backup, **preview}


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

    rows: list[dict[str, Any]] = []
    for row in reader:
        code = str(row.get("course_code", "")).strip().upper().replace(" ", "")
        if not code:
            continue
        pterm = int(str(row.get("programme_term", "0")).strip())
        credits = int(str(row.get("credit_hours", "0")).strip())
        ctype = str(row.get("type", "CORE")).strip() or "CORE"
        rows.append(
            {
                "program": program,
                "course_code": code,
                "type": ctype,
                "programme_term": pterm,
                "credit_hours": credits,
            }
        )

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
            ProgrammeRequirement.objects.update_or_create(
                program=r["program"],
                course_code=r["course_code"],
                defaults={
                    "type": r["type"],
                    "programme_term": r["programme_term"],
                    "credit_hours": r["credit_hours"],
                },
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
    filepath: str,
    program: str,
    encoding: str = "windows-1256",
) -> dict[str, Any]:
    """Parse an Oracle study-plan export and return a preview (no DB writes).

    Returns a dict with ``preview_rows`` (flat list ready for editable table),
    ``metadata``, ``summary``, ``warnings``, and existing DB row counts.
    """
    from core.services.oracle_plan_parser import map_course_type, parse_oracle_plan

    parsed = parse_oracle_plan(filepath, encoding=encoding)

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
            en_name = str(row.get("en_name", "")).strip()
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
                course_key__in=course_codes, source_tag="external"
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
