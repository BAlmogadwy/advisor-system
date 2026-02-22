import csv
import io
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q

from core.models import Prerequisite, ProgrammeRequirement, Student, StudentCourse

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REQ_CSV = BASE_DIR / "import_old" / "data" / "department_courses.csv"
DEFAULT_PRE_CSV = BASE_DIR / "import_old" / "data" / "department_prerequisites.csv"
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

    invalid_term_rows = ProgrammeRequirement.objects.exclude(
        programme_term__range=(1, 10),
    ).filter(
        Q(programme_term__isnull=False),
    ).count() + ProgrammeRequirement.objects.filter(programme_term__isnull=True).count()

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


def preview_delete_students(program: str | None = None, section: str | None = None) -> dict[str, Any]:
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


def import_program_plan(program: str, csv_text: str, replace_existing: bool = False) -> dict[str, Any]:
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
        rows.append({"program": program, "course_code": code, "type": ctype, "programme_term": pterm, "credit_hours": credits})

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
                    prerequisite_course_code=str(row["prerequisite_course_code"]).replace(" ", "").upper(),
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
