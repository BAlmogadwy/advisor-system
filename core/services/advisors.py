import csv
import io
import time
import unicodedata
from typing import Any

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from core.models import AcademicAdvisor, Student
from core.services.high_priority_missing import run_missing_high_priority_report


def normalize_arabic(text: str) -> str:
    """Normalize Arabic text for comparison: unify hamza/alef variants."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u0625", "\u0627")  # إ -> ا
    text = text.replace("\u0623", "\u0627")  # أ -> ا
    text = text.replace("\u0622", "\u0627")  # آ -> ا
    return text.strip()


def _normalize_departments(raw: str) -> tuple[str, list[str]]:
    parts = [p.strip().upper() for p in str(raw).replace(";", ",").split(",") if p.strip()]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen), seen


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


_HP_CACHE_TTL_SECONDS = 300
_hp_missing_cache: dict[str, tuple[float, dict[int, list[dict[str, object]]]]] = {}


def _get_high_priority_by_program(program: str) -> dict[int, list[dict[str, object]]]:
    now = time.time()
    cached = _hp_missing_cache.get(program)
    if cached and (now - cached[0]) < _HP_CACHE_TTL_SECONDS:
        return cached[1]

    report = run_missing_high_priority_report(
        year=0,
        semester=0,
        section=None,
        program=program,
        join_year_prefixes=None,
        term_parity=0,
        discount="1_over_d",
        min_score=2.0,
        top_k_per_student=5,
        studying_counts_as_passed=False,
    )

    parsed: dict[int, list[dict[str, object]]] = {}
    for r in report.get("items", []):
        if not isinstance(r, dict):
            continue
        sid = _to_int(r.get("student_id", 0))
        if not sid:
            continue
        merged: list[dict[str, object]] = []
        for c in r.get("missing_this_parity", []) or []:
            merged.append(
                {
                    "course_code": str(c.get("course_code", "")),
                    "score": _to_float(c.get("score", 0.0)),
                    "bucket": "this_parity",
                }
            )
        for c in r.get("missing_other", []) or []:
            merged.append(
                {
                    "course_code": str(c.get("course_code", "")),
                    "score": _to_float(c.get("score", 0.0)),
                    "bucket": "other",
                }
            )
        parsed[sid] = merged

    _hp_missing_cache[program] = (now, parsed)
    return parsed


def ensure_advisors_schema() -> None:
    # Schema is managed by Django migrations.
    # Keep this function as a compatibility no-op for existing call sites.
    return


def ensure_students_advisor_column() -> dict:
    # Column always exists in Django ORM model.
    return {"ok": True, "added": False, "column": "students.advisor_id"}


def _students_has_advisor_column() -> bool:
    # Column always exists in Django ORM model.
    return True


def upsert_academic_advisor(advisor_id: str, full_name: str, email: str, department: str) -> dict:
    department_text, departments = _normalize_departments(department)
    AcademicAdvisor.objects.update_or_create(
        advisor_id=advisor_id,
        defaults={
            "full_name": full_name,
            "email": email,
            "department": department_text,
        },
    )
    return {
        "ok": True,
        "advisor": {
            "advisor_id": advisor_id,
            "full_name": full_name,
            "email": email,
            "department": department_text,
            "departments": departments,
        },
    }


def list_academic_advisors() -> dict:
    rows = AcademicAdvisor.objects.order_by("full_name", "advisor_id").values_list(
        "advisor_id", "full_name", "email", "department",
    )
    items = []
    for r in rows:
        dep_text, deps = _normalize_departments(str(r[3]))
        items.append(
            {
                "advisor_id": str(r[0]),
                "full_name": str(r[1]),
                "email": str(r[2]),
                "department": dep_text,
                "departments": deps,
            }
        )
    return {"count": len(items), "items": items}


def assign_students_to_advisors(mappings: list[dict[str, Any]]) -> dict[str, Any]:
    from django.db import transaction

    updated = 0
    errors: list[dict] = []

    with transaction.atomic():
        for i, row in enumerate(mappings):
            try:
                student_id = _to_int(row.get("student_id"), 0)
                advisor_id = str(row.get("advisor_id", "")).strip()
            except Exception:
                errors.append({"index": i, "error": "invalid student_id/advisor_id"})
                continue

            if student_id <= 0:
                errors.append({"index": i, "error": "invalid student_id"})
                continue

            if not advisor_id:
                errors.append({"index": i, "student_id": row.get("student_id"), "error": "advisor_id is required"})
                continue

            if not AcademicAdvisor.objects.filter(advisor_id=advisor_id).exists():
                errors.append({"index": i, "student_id": student_id, "advisor_id": advisor_id, "error": "advisor not found"})
                continue

            count = Student.objects.filter(student_id=student_id).update(advisor_id=advisor_id)
            if count == 0:
                errors.append({"index": i, "student_id": student_id, "advisor_id": advisor_id, "error": "student not found"})
                continue

            updated += count

    return {
        "ok": True,
        "received": len(mappings),
        "updated": updated,
        "errors_count": len(errors),
        "errors": errors[:30],
    }


def parse_student_advisor_csv(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"student_id", "advisor_id"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("CSV must include headers: student_id, advisor_id")

    rows: list[dict[str, str]] = []
    for r in reader:
        sid = str(r.get("student_id", "")).strip()
        aid = str(r.get("advisor_id", "")).strip()
        if not sid:
            continue
        rows.append({"student_id": sid, "advisor_id": aid})
    return rows


def _apply_advisor_filters(
    items: list[dict[str, Any]],
    search: str | None = None,
    focus: str | None = None,
    program_filter: str | None = None,
) -> list[dict]:
    rows = list(items)

    s = (search or "").strip().lower()
    if s:
        rows = [
            r
            for r in rows
            if s in str(r.get("student_id", "")).lower() or s in str(r.get("name", "")).lower()
        ]

    p = (program_filter or "").strip().upper()
    if p:
        rows = [r for r in rows if str(r.get("program", "")).upper() == p]

    f = (focus or "all").strip().lower()
    if f == "risk":
        rows = [r for r in rows if r.get("gpa") is not None and _to_float(r.get("gpa")) < 2.0]
    elif f == "missing":
        rows = [r for r in rows if bool(r.get("has_high_priority_missing"))]
    elif f == "zerohours":
        rows = [r for r in rows if int(r.get("current_term_registered_hours", 0)) == 0]
    elif f == "attention":
        rows = [r for r in rows if bool(r.get("needs_attention"))]

    return rows


def list_students_by_advisor(
    advisor_id: str,
    search: str | None = None,
    focus: str | None = None,
    program_filter: str | None = None,
    forced_advisor_id: str | None = None,
    allowed_departments: list[str] | None = None,
) -> dict[str, Any]:
    advisor_obj = AcademicAdvisor.objects.filter(advisor_id=advisor_id).values_list(
        "advisor_id", "full_name", "email", "department",
    ).first()
    advisor = None
    if advisor_obj:
        dep_text, deps = _normalize_departments(str(advisor_obj[3]))
        advisor = {
            "advisor_id": str(advisor_obj[0]),
            "full_name": str(advisor_obj[1]),
            "email": str(advisor_obj[2]),
            "department": dep_text,
            "departments": deps,
        }

    if forced_advisor_id and advisor_id != forced_advisor_id:
        return {
            "advisor": advisor,
            "advisor_id": advisor_id,
            "mapping_ready": True,
            "count": 0,
            "shown_count": 0,
            "filters": {
                "search": search or "",
                "focus": focus or "all",
                "program_filter": program_filter or "",
            },
            "items": [],
            "summary": {},
            "error": "You can only access your assigned advisor portfolio.",
        }

    # Annotate students with current_term_registered_hours via subquery
    students_qs = Student.objects.filter(advisor_id=advisor_id).annotate(
        current_term_registered_hours=Coalesce(
            Sum(
                "student_courses__course__credit_hours",
                filter=Q(student_courses__status="studying"),
            ),
            0,
        ),
    ).order_by("student_id")

    items: list[dict[str, Any]] = []
    for s in students_qs:
        items.append(
            {
                "student_id": s.student_id,
                "registration_no": s.registration_no or "",
                "name": s.name or "",
                "program": s.program or "",
                "section": s.section or "",
                "status": s.status or "",
                "gpa": float(s.gpa) if s.gpa is not None else None,
                "total_registered_credits": s.total_registered_credits or 0,
                "total_earned_credits": s.total_earned_credits or 0,
                "current_term_registered_hours": s.current_term_registered_hours or 0,
            }
        )

    if allowed_departments:
        allow = {x.strip().upper() for x in allowed_departments if x and str(x).strip()}
        items = [x for x in items if str(x.get("program", "")).upper() in allow]

    gpas = [_to_float(x.get("gpa"), -1.0) for x in items if x.get("gpa") is not None]
    avg_gpa = round(sum(gpas) / len(gpas), 3) if gpas else None
    low_gpa_count = sum(1 for x in gpas if x < 2.0)

    by_program: dict[str, int] = {}
    for s in items:
        p = str(s.get("program") or "")
        by_program[p] = by_program.get(p, 0) + 1

    high_priority_by_student: dict[int, list[dict[str, object]]] = {}
    for prog in by_program.keys():
        if not prog:
            continue
        hp_for_program = _get_high_priority_by_program(prog)
        for sid, courses in hp_for_program.items():
            high_priority_by_student[sid] = courses

    for s in items:
        sid = _to_int(s.get("student_id", 0))
        missing_courses = high_priority_by_student.get(sid, [])
        has_missing = len(missing_courses) > 0
        s["has_high_priority_missing"] = has_missing
        s["high_priority_missing_courses"] = missing_courses

        gpa_val = s.get("gpa")
        gpa_num = _to_float(gpa_val, 0.0) if gpa_val is not None else None
        low_gpa = gpa_num is not None and gpa_num < 2.0
        zero_hours = _to_int(s.get("current_term_registered_hours", 0)) == 0

        attention_reasons: list[str] = []
        if low_gpa:
            attention_reasons.append("low_gpa")
        if has_missing:
            attention_reasons.append("high_priority_missing")
        if zero_hours:
            attention_reasons.append("zero_current_term_hours")

        missing_score = sum(_to_float(c.get("score", 0.0)) for c in missing_courses)
        gpa_penalty = max(0.0, 2.0 - gpa_num) * 5.0 if gpa_num is not None else 0.0
        zero_penalty = 3.0 if zero_hours else 0.0
        risk_score = round(gpa_penalty + missing_score + zero_penalty, 2)

        s["needs_attention"] = bool(attention_reasons)
        s["attention_reasons"] = attention_reasons
        s["risk_score"] = risk_score
        s["missing_courses_compact"] = "; ".join(
            f"{str(c.get('course_code', ''))}({_to_float(c.get('score', 0.0)):.2f})" for c in missing_courses
        )

    items.sort(
        key=lambda s: (
            0 if bool(s.get("needs_attention")) else 1,
            -_to_float(s.get("risk_score", 0.0)),
            _to_float(s.get("gpa", 99.0), 99.0) if s.get("gpa") is not None else 99.0,
            0 if bool(s.get("has_high_priority_missing")) else 1,
            _to_int(s.get("current_term_registered_hours", 0)),
            _to_int(s.get("student_id", 0)),
        )
    )

    filtered_items = _apply_advisor_filters(
        items,
        search=search,
        focus=focus,
        program_filter=program_filter,
    )

    return {
        "advisor": advisor,
        "advisor_id": advisor_id,
        "mapping_ready": True,
        "count": len(items),
        "shown_count": len(filtered_items),
        "filters": {
            "search": search or "",
            "focus": (focus or "all"),
            "program_filter": program_filter or "",
        },
        "items": filtered_items,
        "summary": {
            "avg_gpa": avg_gpa,
            "low_gpa_count": low_gpa_count,
            "program_breakdown": by_program,
            "high_priority_missing_count": sum(1 for s in items if bool(s.get("has_high_priority_missing"))),
            "needs_attention_count": sum(1 for s in items if bool(s.get("needs_attention"))),
            "very_high_risk_count": sum(1 for s in items if _to_float(s.get("risk_score", 0.0)) >= 8.0),
            "zero_current_term_hours_count": sum(
                1 for s in items if _to_int(s.get("current_term_registered_hours", 0)) == 0
            ),
            "two_plus_high_priority_missing_count": sum(
                1
                for s in items
                if len(s.get("high_priority_missing_courses", []) if isinstance(s.get("high_priority_missing_courses"), list) else []) >= 2
            ),
            "current_term_registered_hours_total": sum(_to_int(s.get("current_term_registered_hours", 0)) for s in items),
        },
    }
