from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from core.models import TermSection, TermSectionMeeting

REQUIRED_COLUMNS = {
    "course_name",
    "course_code",
    "course_number",
    "section",
    "day",
    "start_time",
    "end_time",
    "building",
    "floor_wing",
    "room",
    "instructor",
}


def ensure_term_sections_schema() -> None:
    # Schema is managed by Django migrations.
    # Keep this function as a compatibility no-op for existing call sites.
    return


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    code = (row.get("course_code") or "").strip().upper()
    number = (row.get("course_number") or "").strip()
    course_key = f"{code}{number}".replace(" ", "").upper()
    return {
        "course_name": (row.get("course_name") or "").strip(),
        "course_code": code,
        "course_number": number,
        "course_key": course_key,
        "section": (row.get("section") or "").strip().upper(),
        "available_capacity": (row.get("available_capacity") or "").strip(),
        "registered_count": (row.get("registered_count") or "").strip(),
        "day": (row.get("day") or "").strip(),
        "start_time": (row.get("start_time") or "").strip(),
        "end_time": (row.get("end_time") or "").strip(),
        "building": (row.get("building") or "").strip(),
        "floor_wing": (row.get("floor_wing") or "").strip(),
        "room": (row.get("room") or "").strip().upper(),
        "instructor": (row.get("instructor") or "").strip(),
    }


def _load_rows(csv_path: str | Path) -> tuple[Path, list[dict[str, str]]]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
        rows = [_normalize_row(r) for r in reader]
    return path, rows


def preview_term_sections_from_csv(
    csv_path: str | Path,
    academic_year: str = "",
    term: str = "",
    source_tag: str = "other",
    max_preview_rows: int = 300,
) -> dict[str, object]:
    path, rows = _load_rows(csv_path)
    preview_rows = [{"source_tag": source_tag, **row} for row in rows[:max_preview_rows]]
    return {
        "source_tag": source_tag,
        "source": str(path),
        "total_rows": len(rows),
        "preview_count": len(preview_rows),
        "preview_rows": preview_rows,
    }


def import_term_sections_from_csv(
    csv_path: str | Path,
    academic_year: str = "",
    term: str = "",
    source_tag: str = "other",
    truncate_existing_term: bool = True,
) -> dict[str, int | str]:
    from django.db import transaction

    path, rows = _load_rows(csv_path)

    deleted_meetings = 0
    deleted_sections = 0

    with transaction.atomic():
        if truncate_existing_term:
            # Only truncate global (non-scenario) sections.
            # Scenario-owned sections are managed by the timetable builder.
            global_sections = TermSection.objects.filter(scenario__isnull=True)
            deleted_meetings = TermSectionMeeting.objects.filter(
                term_section__scenario__isnull=True
            ).count()
            TermSectionMeeting.objects.filter(term_section__scenario__isnull=True).delete()
            deleted_sections = global_sections.count()
            global_sections.delete()

        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            key = (row["course_key"], row["section"])
            grouped.setdefault(key, []).append(row)

        inserted_sections = 0
        inserted_meetings = 0
        now_str = datetime.now(UTC).isoformat()

        for (course_key, section), meeting_rows in grouped.items():
            first = meeting_rows[0]
            course_code = first["course_code"]
            course_number = first["course_number"]

            cap_str = str(first.get("available_capacity", ""))
            reg_str = str(first.get("registered_count", ""))

            ts, _created = TermSection.objects.update_or_create(
                scenario=None,  # imported sections are global, not scenario-owned
                course_key=course_key,
                section=section,
                defaults={
                    "source_tag": source_tag,
                    "course_name": first["course_name"],
                    "available_capacity": int(cap_str) if cap_str.isdigit() else None,
                    "registered_count": int(reg_str) if reg_str.isdigit() else None,
                    "course_code": course_code,
                    "course_number": course_number,
                    "source_file": str(path),
                    "updated_at": now_str,
                },
            )
            inserted_sections += 1

            # Clear existing meetings for this section and re-insert
            TermSectionMeeting.objects.filter(term_section=ts).delete()

            for row in meeting_rows:
                TermSectionMeeting.objects.update_or_create(
                    term_section=ts,
                    day=row["day"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    room=row["room"],
                    instructor=row["instructor"],
                    defaults={
                        "building": row["building"],
                        "floor_wing": row["floor_wing"],
                        "updated_at": now_str,
                    },
                )
                inserted_meetings += 1

    total_sections = TermSection.objects.count()
    total_meetings = TermSectionMeeting.objects.count()

    return {
        "source_tag": source_tag,
        "truncate_existing": bool(truncate_existing_term),
        "deleted_sections": int(deleted_sections),
        "deleted_meetings": int(deleted_meetings),
        "inserted_or_updated": inserted_meetings,
        "rows_total": total_sections,
        "meetings_total": total_meetings,
        "source": str(path),
    }
