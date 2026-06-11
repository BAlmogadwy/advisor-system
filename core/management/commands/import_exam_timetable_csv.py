"""Import a manually-created exam timetable CSV as a saved run."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from core.models import ExamTimetableRun, Room
from core.services.course_identity import planner_course_key
from core.services.exam_run_schema import (
    STATUS_DERIVATION_VERSION,
    compute_enrolment_snapshot,
    derive_building_footprint,
    derive_multi_sitting_details,
    derive_status_surface,
    stamp_schema_version,
)
from core.services.exam_timetable import (
    _build_qa,
    _build_room_qa,
    _build_section_enrollment_from_enrolled_sets,
    assign_rooms_to_schedule,
    build_conflict_graph,
    build_credit_map,
    build_enrolled_sets_with_meta,
    build_plan_term_buckets,
    check_room_feasibility,
)

REQUIRED_COLUMNS = {"Day", "Date", "Period", "Time", "Course Name", "Course Code"}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalise_header(row: dict[str, str]) -> dict[str, str]:
    return {_clean(k): v for k, v in row.items()}


def _gregorian_date(raw_date: str) -> str:
    """Extract the Gregorian date from values like ``16/12/1447 H - 02/06/2026``."""
    text = _clean(raw_date)
    match = re.search(r"(\d{2}/\d{2}/\d{4})\s*$", text)
    return match.group(1) if match else text


def _day_label(day: str, raw_date: str) -> str:
    date = _gregorian_date(raw_date)
    return f"{_clean(day)} {date}".strip() if date else _clean(day)


def _period_label(period: str, time: str) -> str:
    period_text = _clean(period)
    time_text = _clean(time)
    if period_text and time_text:
        return f"{period_text} ({time_text})"
    return period_text or time_text


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [_normalise_header(row) for row in reader]
    except OSError as exc:
        raise CommandError(f"Could not read CSV: {exc}") from exc

    if not rows:
        raise CommandError("CSV has no data rows.")

    missing = REQUIRED_COLUMNS - set(rows[0])
    if missing:
        raise CommandError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    cleaned: list[dict[str, str]] = []
    for idx, row in enumerate(rows, start=2):
        code = _clean(row.get("Course Code"))
        day = _clean(row.get("Day"))
        period = _period_label(row.get("Period", ""), row.get("Time", ""))
        if not code or not day or not period:
            raise CommandError(f"Row {idx} must include Course Code, Day, Period, and Time.")
        cleaned.append(row)
    return cleaned


def _manual_display_map(rows: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    """Return ``(source_code, course_name) -> display_code``.

    Same-code, different-name rows use the app's duplicate display convention:
    ``CS112 (1)``, ``CS112 (2)``, and so on.
    """
    names_by_source: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        source = _clean(row["Course Code"])
        name = _clean(row.get("Course Name"))
        if name not in names_by_source[source]:
            names_by_source[source].append(name)

    display: dict[tuple[str, str], str] = {}
    for source, names in names_by_source.items():
        if len(names) == 1:
            display[(source, names[0])] = source
        else:
            for idx, name in enumerate(names, start=1):
                display[(source, name)] = f"{source} ({idx})"
    return display


def _source_for_display(display_code: str) -> str:
    if display_code.endswith(")") and " (" in display_code:
        return display_code.rsplit(" (", 1)[0]
    return display_code


def _build_enrolment_for_manual_courses(
    manual_meta: dict[str, dict[str, str]],
) -> tuple[dict[str, set[int]], dict[str, dict[str, str]]]:
    db_enrolled, db_meta = build_enrolled_sets_with_meta()
    db_by_source: dict[str, list[str]] = defaultdict(list)
    for display, meta in db_meta.items():
        source = _clean(meta.get("source_course_code")) or _source_for_display(display)
        db_by_source[source].append(display)
    for displays in db_by_source.values():
        displays.sort()

    enrolled: dict[str, set[int]] = {}
    merged_meta: dict[str, dict[str, str]] = {}
    for display, meta in manual_meta.items():
        source = _clean(meta.get("source_course_code")) or _source_for_display(display)
        matched_display = display if display in db_enrolled else ""
        if not matched_display:
            candidates = db_by_source.get(source, [])
            if len(candidates) == 1:
                matched_display = candidates[0]
            elif display in candidates:
                matched_display = display

        enrolled[display] = set(db_enrolled.get(matched_display, set()))
        db_match_meta = db_meta.get(matched_display, {})
        merged_meta[display] = {
            "source_course_code": source,
            "course_name": _clean(meta.get("course_name"))
            or _clean(db_match_meta.get("course_name")),
            "course_identity": _clean(db_match_meta.get("course_identity"))
            or _clean(meta.get("course_identity"))
            or source,
        }
    return enrolled, merged_meta


def _import_payload(
    *,
    rows: list[dict[str, str]],
    label: str,
    assign_rooms: bool,
) -> dict[str, Any]:
    display_by_source_name = _manual_display_map(rows)

    days: list[str] = []
    periods: list[str] = []
    slots: list[dict[str, Any]] = []
    slot_index_by_key: dict[tuple[str, str], int] = {}
    manual_meta: dict[str, dict[str, str]] = {}
    schedule_entries: list[dict[str, Any]] = []
    seen_course_entries: set[str] = set()

    for row in rows:
        day = _day_label(row["Day"], row["Date"])
        period = _period_label(row["Period"], row["Time"])
        if day not in days:
            days.append(day)
        if period not in periods:
            periods.append(period)

    for day in days:
        for period in periods:
            slot_index_by_key[(day, period)] = len(slots)
            slots.append({"index": len(slots), "day": day, "period": period})

    for row in rows:
        source = _clean(row["Course Code"])
        name = _clean(row.get("Course Name"))
        display = display_by_source_name[(source, name)]
        if display in seen_course_entries:
            raise CommandError(f"Course {display} appears more than once in the manual timetable.")
        seen_course_entries.add(display)

        day = _day_label(row["Day"], row["Date"])
        period = _period_label(row["Period"], row["Time"])
        manual_meta[display] = {
            "source_course_code": source,
            "course_name": name,
            "course_identity": planner_course_key(source, name) if name else source,
        }
        schedule_entries.append(
            {
                "course_code": display,
                "slot_index": slot_index_by_key[(day, period)],
                "day": day,
                "period": period,
                "source_course_code": source,
                "course_name": name,
                "course_identity": manual_meta[display]["course_identity"],
                "rooms": [],
            }
        )

    enrolled_sets, course_meta = _build_enrolment_for_manual_courses(manual_meta)
    for entry in schedule_entries:
        meta = course_meta.get(entry["course_code"], {})
        entry["course_identity"] = meta.get("course_identity") or entry["course_identity"]

    course_list = sorted(manual_meta)
    credit_map = build_credit_map(course_list)
    conflicts, _adj = build_conflict_graph(enrolled_sets)
    ptb, _cb = build_plan_term_buckets(set(course_list), course_meta=course_meta)

    section_enrollment: dict[str, list[dict[str, Any]]] = {}
    rooms_list: list[dict[str, Any]] = []
    room_feasibility: list[dict[str, Any]] = []
    if assign_rooms:
        section_enrollment = _build_section_enrollment_from_enrolled_sets(enrolled_sets)
        rooms_list = list(
            Room.objects.all().values(
                "room_code", "capacity", "section", "department", "building", "floor"
            )
        )
        room_feasibility = check_room_feasibility(section_enrollment, rooms_list)
        assign_rooms_to_schedule(schedule_entries, section_enrollment, rooms_list)

        room_meta_by_code = {str(room.get("room_code", "")): room for room in rooms_list}
        for entry in schedule_entries:
            for room_row in entry.get("rooms") or []:
                meta = room_meta_by_code.get(str(room_row.get("room_code", "")))
                if meta:
                    room_row.setdefault("building", str(meta.get("building", "") or ""))
                    room_row.setdefault("floor", str(meta.get("floor", "") or ""))

    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=2,
        plan_term_buckets=ptb,
        credit_map=credit_map,
    )
    qa["rooms"] = _build_room_qa(schedule_entries, rooms_list)
    qa["room_feasibility_violations"] = room_feasibility
    qa["rebalance_moves"] = 0
    qa["thin_threshold"] = 0
    qa["thin_courses"] = []
    qa["thin_clash_risk"] = []
    qa["multi_sitting_details"] = derive_multi_sitting_details(schedule_entries)
    qa["multi_sitting_sections"] = len(qa["multi_sitting_details"])
    qa["manual_override_count"] = qa.get("conflict_count", 0)
    qa["manual_override_details"] = list(qa.get("same_slot_conflicts", []))
    qa["building_footprint"] = derive_building_footprint(schedule_entries)

    sections_total = sum(len(v) for v in section_enrollment.values())
    synthetic_all = sum(
        1
        for sections in section_enrollment.values()
        for section in sections
        if str(section.get("section", "")).upper() == "ALL"
    )
    qa["enrolment_snapshot"] = compute_enrolment_snapshot(
        enrolled_sets,
        sections_count=sections_total,
        fallback_used=bool(synthetic_all and synthetic_all == len(section_enrollment)),
        synthetic_all_sections_count=synthetic_all,
    )

    all_students: set[int] = set()
    for student_ids in enrolled_sets.values():
        all_students.update(student_ids)

    buckets_summary = [
        {
            "program": program,
            "programme_term": term,
            "course_count": len(courses),
            "courses": sorted(courses),
        }
        for (program, term), courses in sorted(ptb.items())
    ]
    draft: dict[str, Any] = {
        "status": "ok",
        "students_count": len(all_students),
        "courses": course_list,
        "courses_count": len(course_list),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "slots": slots,
        "schedule": sorted(
            schedule_entries,
            key=lambda entry: (int(entry.get("slot_index", 0)), str(entry.get("course_code", ""))),
        ),
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(ptb),
        "credit_map": credit_map,
        "seed": None,
        "section_enrollment": section_enrollment,
        "rooms_count": len(rooms_list),
        "assign_rooms": assign_rooms,
        "rebuild_mode": "manual_csv_import",
        "source": {"type": "manual_csv"},
    }
    primary_status, status_flags = derive_status_surface(draft)
    draft["primary_status"] = primary_status
    draft["status_flags"] = status_flags
    draft["status_derivation_version"] = STATUS_DERIVATION_VERSION
    return stamp_schema_version(draft)


class Command(BaseCommand):
    help = "Import a manual exam timetable CSV as an ExamTimetableRun history row."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("csv_path", help="Path to the manual exam timetable CSV.")
        parser.add_argument(
            "--label",
            default="",
            help="Saved run label. Defaults to the CSV file stem.",
        )
        parser.add_argument(
            "--no-rooms",
            action="store_true",
            help="Import schedule and QA only; do not assign rooms.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and print a summary without creating a database row.",
        )

    def handle(self, *args: object, **options: object) -> None:
        path = Path(str(options["csv_path"])).expanduser()
        label = _clean(options.get("label")) or path.stem
        rows = _read_rows(path)
        payload = _import_payload(
            rows=rows,
            label=label,
            assign_rooms=not bool(options.get("no_rooms")),
        )
        payload["source"]["path"] = str(path)

        if options.get("dry_run"):
            self.stdout.write(
                self.style.SUCCESS(
                    "Validated manual timetable: "
                    f"{payload['courses_count']} course(s), "
                    f"{len(payload['slots'])} slot(s), "
                    f"{payload['qa'].get('conflict_count', 0)} same-slot conflict(s), "
                    f"status={payload.get('primary_status')}"
                )
            )
            return

        with transaction.atomic():
            run = ExamTimetableRun.objects.create(
                label=label,
                result_json=json.dumps(payload, ensure_ascii=False),
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported manual timetable as ExamTimetableRun #{run.id} ({label})."
            )
        )
