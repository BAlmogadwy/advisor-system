from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.section_programmes import (
    normalize_section_program,
    reconcile_observed_section_programs,
)
from core.services.section_snapshot_guard import section_snapshot_operation_guard

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
PROGRAM_COLUMNS = ("programs", "programmes", "program", "programme")
SUPPORTED_SECTION_DAYS = frozenset({"SUN", "MON", "TUE", "WED", "THU"})


class NormalizedSectionRow(TypedDict):
    course_name: str
    course_code: str
    course_number: str
    course_key: str
    section: str
    available_capacity: str
    registered_count: str
    day: str
    start_time: str
    end_time: str
    building: str
    floor_wing: str
    room: str
    instructor: str
    programs: list[str]


class EffectiveProgramAssignment(TypedDict):
    program: str
    assignment_source: str


def ensure_term_sections_schema() -> None:
    # Schema is managed by Django migrations.
    # Keep this function as a compatibility no-op for existing call sites.
    return


def _parse_programs(row: dict[str, str]) -> list[str]:
    by_normalized_header = {
        str(header or "").strip().lower(): value for header, value in row.items()
    }
    raw = ""
    for column in PROGRAM_COLUMNS:
        if column in by_normalized_header:
            raw = by_normalized_header[column] or ""
            break

    programs = {
        normalized
        for token in re.split(r"[,;|]", raw)
        if (normalized := normalize_section_program(token))
    }
    return sorted(programs)


def _normalize_time(value: object) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return raw
    hour, minute = match.groups()
    return f"{int(hour):02d}:{minute}"


def _normalize_row(row: dict[str, str]) -> NormalizedSectionRow:
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
        "day": (row.get("day") or "").strip().upper(),
        "start_time": _normalize_time(row.get("start_time")),
        "end_time": _normalize_time(row.get("end_time")),
        "building": (row.get("building") or "").strip(),
        "floor_wing": (row.get("floor_wing") or "").strip(),
        "room": (row.get("room") or "").strip().upper(),
        "instructor": (row.get("instructor") or "").strip(),
        "programs": _parse_programs(row),
    }


def _time_minutes(value: str) -> int | None:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not match:
        return None
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _validate_normalized_row(row: NormalizedSectionRow, row_number: int) -> None:
    prefix = f"CSV row {row_number}"
    if not row["course_code"] or not row["course_number"] or not row["course_key"]:
        raise ValueError(f"{prefix}: course_code and course_number must be nonblank")
    if not row["section"]:
        raise ValueError(f"{prefix}: section must be nonblank")
    if row["day"] not in SUPPORTED_SECTION_DAYS:
        supported = ", ".join(sorted(SUPPORTED_SECTION_DAYS))
        raise ValueError(f"{prefix}: day must be one of {supported}")

    start_minutes = _time_minutes(row["start_time"])
    end_minutes = _time_minutes(row["end_time"])
    if start_minutes is None:
        raise ValueError(f"{prefix}: start_time must use valid HH:MM format")
    if end_minutes is None:
        raise ValueError(f"{prefix}: end_time must use valid HH:MM format")
    if start_minutes >= end_minutes:
        raise ValueError(f"{prefix}: start_time must be earlier than end_time")
    for field in ("available_capacity", "registered_count"):
        value = row[field]
        if value and not re.fullmatch(r"\d+", value):
            raise ValueError(f"{prefix}: {field} must be a non-negative integer")


def _load_rows(
    csv_path: str | Path,
) -> tuple[Path, list[NormalizedSectionRow], bool]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = REQUIRED_COLUMNS - set(fieldnames)
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
        normalized_headers = {str(header or "").strip().lower() for header in fieldnames}
        has_program_column = bool(normalized_headers.intersection(PROGRAM_COLUMNS))
        rows = []
        for row_number, raw_row in enumerate(reader, start=2):
            row = _normalize_row(raw_row)
            _validate_normalized_row(row, row_number)
            rows.append(row)
        if not rows:
            raise ValueError("CSV contains no section rows")
    return path, rows, has_program_column


SectionKey = tuple[str, str]


def _validate_grouped_rows(
    grouped: dict[SectionKey, list[NormalizedSectionRow]],
) -> None:
    """Reject rows whose import result would otherwise depend on file order."""
    for (course_key, section), section_rows in grouped.items():
        first = section_rows[0]
        inconsistent_fields: set[str] = set()
        for row in section_rows[1:]:
            comparisons = (
                ("course_name", row["course_name"], first["course_name"]),
                ("course_code", row["course_code"], first["course_code"]),
                ("course_number", row["course_number"], first["course_number"]),
                ("course_key", row["course_key"], first["course_key"]),
                ("section", row["section"], first["section"]),
                (
                    "available_capacity",
                    row["available_capacity"],
                    first["available_capacity"],
                ),
                (
                    "registered_count",
                    row["registered_count"],
                    first["registered_count"],
                ),
            )
            inconsistent_fields.update(
                field for field, value, expected in comparisons if value != expected
            )
        if inconsistent_fields:
            fields = ", ".join(sorted(inconsistent_fields))
            raise ValueError(
                f"Inconsistent section metadata for {course_key} section {section}: "
                f"{fields} must match across all rows."
            )

        locations_by_meeting: dict[tuple[str, ...], tuple[str, ...]] = {}
        for row in section_rows:
            meeting_key = (
                row["day"],
                row["start_time"],
                row["end_time"],
                row["room"],
                row["instructor"],
            )
            location = (row["building"], row["floor_wing"])
            existing_location = locations_by_meeting.setdefault(meeting_key, location)
            if existing_location != location:
                raise ValueError(
                    f"Inconsistent meeting location for {course_key} section {section}: "
                    "duplicate day/time/room/instructor rows must use the same "
                    "building and floor_wing."
                )


def _group_rows(
    rows: list[NormalizedSectionRow],
) -> dict[SectionKey, list[NormalizedSectionRow]]:
    grouped: dict[SectionKey, list[NormalizedSectionRow]] = {}
    for row in rows:
        grouped.setdefault((row["course_key"], row["section"]), []).append(row)
    _validate_grouped_rows(grouped)
    return grouped


def _normalize_default_programs(default_programs: list[Any] | None) -> list[str]:
    if default_programs is None:
        return []
    if not isinstance(default_programs, list):
        raise ValueError("default_programs must be a list of programme codes")
    return sorted(
        {normalized for raw in default_programs if (normalized := normalize_section_program(raw))}
    )


def _validate_known_programs(
    rows: list[NormalizedSectionRow],
    default_programs: list[str],
) -> None:
    supplied = set(default_programs)
    supplied.update(program for row in rows for program in row["programs"])
    if not supplied:
        return

    known = {
        normalized
        for raw in ProgrammeRequirement.objects.values_list("program", flat=True).distinct()
        if (normalized := normalize_section_program(raw))
    }
    known.update(
        normalized
        for raw in TermSectionProgram.objects.values_list("program", flat=True).distinct()
        if (normalized := normalize_section_program(raw))
    )
    unknown = sorted(supplied - known)
    if unknown:
        raise ValueError(
            "Unknown programme code(s): "
            f"{', '.join(unknown)}. Programme codes must exist in a degree plan "
            "or an existing section programme assignment."
        )


def _requested_programs_by_section(
    grouped: dict[SectionKey, list[NormalizedSectionRow]],
    default_programs: list[str],
) -> dict[SectionKey, list[str]]:
    requested: dict[SectionKey, list[str]] = {}
    for key, section_rows in grouped.items():
        csv_programs = {program for row in section_rows for program in row["programs"]}
        # A rich CSV is section-authoritative. Defaults fill only a section for
        # which the CSV supplies no programme values at all.
        requested[key] = sorted(csv_programs or set(default_programs))
    return requested


def _unique_meeting_count(
    grouped: dict[SectionKey, list[NormalizedSectionRow]],
) -> int:
    meetings: set[tuple[str, ...]] = set()
    for (course_key, section), section_rows in grouped.items():
        for row in section_rows:
            meetings.add(
                (
                    course_key,
                    section,
                    row["day"],
                    row["start_time"],
                    row["end_time"],
                    row["room"],
                    row["instructor"],
                )
            )
    return len(meetings)


def _build_import_impact(
    grouped: dict[SectionKey, list[NormalizedSectionRow]],
    *,
    has_program_column: bool,
    default_programs: list[str],
) -> tuple[
    dict[str, object],
    dict[SectionKey, list[str]],
    dict[SectionKey, list[str]],
    dict[SectionKey, str],
    dict[SectionKey, list[EffectiveProgramAssignment]],
]:
    """Predict the membership state produced by this merge against the live DB."""
    requested_by_section = _requested_programs_by_section(grouped, default_programs)
    course_keys = {course_key for course_key, _section in grouped}
    existing_sections = list(
        TermSection.objects.filter(
            scenario__isnull=True,
            course_key__in=course_keys,
        )
    )
    existing_by_key = {
        (str(section.course_key), str(section.section)): section
        for section in existing_sections
        if (str(section.course_key), str(section.section)) in grouped
    }
    existing_ids = {int(section.id) for section in existing_by_key.values()}

    existing_links: dict[int, dict[str, str]] = {section_id: {} for section_id in existing_ids}
    for section_id, program, source in TermSectionProgram.objects.filter(
        term_section_id__in=existing_ids
    ).values_list("term_section_id", "program", "assignment_source"):
        existing_links[int(section_id)][str(program)] = str(source)

    registrations = list(
        StudentTermSection.objects.filter(term_section_id__in=existing_ids).values_list(
            "term_section_id",
            "student_id",
        )
    )
    student_ids = {int(student_id) for _section_id, student_id in registrations}
    student_programs: dict[int, str] = {}
    for student_id, raw_program in Student.objects.filter(student_id__in=student_ids).values_list(
        "student_id", "program"
    ):
        program = normalize_section_program(raw_program)
        if program:
            student_programs[int(student_id)] = program

    observed_by_section: dict[int, set[str]] = {section_id: set() for section_id in existing_ids}
    for section_id, student_id in registrations:
        program = student_programs.get(int(student_id), "")
        if program:
            observed_by_section[int(section_id)].add(program)

    authoritative_imports = has_program_column or bool(default_programs)
    membership_adds = 0
    membership_removes = 0
    membership_promotions = 0
    membership_source_changes = 0
    fully_unassigned: list[dict[str, object]] = []
    programme_source_by_section: dict[SectionKey, str] = {}
    final_programs_by_section: dict[SectionKey, list[str]] = {}
    final_assignments_by_section: dict[SectionKey, list[EffectiveProgramAssignment]] = {}

    for key, requested_programs in requested_by_section.items():
        csv_programs = {program for row in grouped[key] for program in row["programs"]}
        requested_origin = ""
        if csv_programs:
            requested_origin = "csv"
        elif default_programs:
            requested_origin = "default"

        existing = existing_by_key.get(key)
        current = existing_links.get(int(existing.id), {}) if existing else {}
        final: dict[str, str] = {
            program: source
            for program, source in current.items()
            if source not in {"import", "observed"}
        }

        if authoritative_imports:
            for program in requested_programs:
                final[program] = "import"
        else:
            # Compatibility mode for an old CSV: it may refresh section/meeting
            # details, but cannot silently rewrite imported ownership.
            final.update(
                {program: source for program, source in current.items() if source == "import"}
            )

        if existing:
            for program in observed_by_section.get(int(existing.id), set()):
                final.setdefault(program, "observed")

        current_programs = set(current)
        final_programs = set(final)
        membership_adds += len(final_programs - current_programs)
        membership_removes += len(current_programs - final_programs)
        for program in current_programs & final_programs:
            if current[program] != final[program]:
                membership_source_changes += 1
                if current[program] != "import" and final[program] == "import":
                    membership_promotions += 1

        final_programs = set(final)
        requested_final = final_programs.intersection(requested_programs)
        retained_final = final_programs - requested_final
        if not final:
            programme_source_by_section[key] = "unassigned"
            fully_unassigned.append(
                {
                    "course_key": key[0],
                    "section": key[1],
                    "existing": existing is not None,
                }
            )
        elif requested_final and retained_final:
            programme_source_by_section[key] = "mixed"
        elif requested_final:
            programme_source_by_section[key] = requested_origin
        else:
            programme_source_by_section[key] = "preserved"
        final_programs_by_section[key] = sorted(final)
        final_assignments_by_section[key] = [
            {
                "program": program,
                "assignment_source": final[program],
            }
            for program in sorted(final)
        ]

    unique_sections = len(grouped)
    impact: dict[str, object] = {
        "sections_unique": unique_sections,
        "meeting_rows_unique": _unique_meeting_count(grouped),
        "sections_new": unique_sections - len(existing_by_key),
        "sections_existing": len(existing_by_key),
        "programme_assignments_effective": sum(
            len(programs) for programs in final_programs_by_section.values()
        ),
        "membership_adds": membership_adds,
        "membership_removes": membership_removes,
        "membership_promotions": membership_promotions,
        "membership_source_changes": membership_source_changes,
        "predicted_fully_unassigned_sections": len(fully_unassigned),
        "fully_unassigned_sections": fully_unassigned[:25],
    }
    return (
        impact,
        requested_by_section,
        final_programs_by_section,
        programme_source_by_section,
        final_assignments_by_section,
    )


def _import_preview_fingerprint(
    rows: list[NormalizedSectionRow],
    grouped: dict[SectionKey, list[NormalizedSectionRow]],
    *,
    source_path: str,
    has_program_column: bool,
    default_programs: list[str],
    source_tag: str,
) -> str:
    """Hash the complete input and affected state an import is about to overwrite."""
    canonical_rows = [
        (
            row["course_name"],
            row["course_code"],
            row["course_number"],
            row["course_key"],
            row["section"],
            row["available_capacity"],
            row["registered_count"],
            row["day"],
            row["start_time"],
            row["end_time"],
            row["building"],
            row["floor_wing"],
            row["room"],
            row["instructor"],
            tuple(row["programs"]),
        )
        for row in rows
    ]

    course_keys = {course_key for course_key, _section in grouped}
    matching_sections = [
        section
        for section in TermSection.objects.filter(
            scenario__isnull=True,
            course_key__in=course_keys,
        )
        if (str(section.course_key), str(section.section)) in grouped
    ]
    matching_sections.sort(key=lambda section: int(section.id))
    section_ids = [int(section.id) for section in matching_sections]

    section_state = [
        (
            int(section.id),
            str(section.course_key),
            str(section.section),
            str(section.source_tag),
            str(section.course_name),
            section.available_capacity,
            section.registered_count,
            str(section.course_code),
            str(section.course_number),
            str(section.source_file),
            str(section.created_at),
            str(section.updated_at),
        )
        for section in matching_sections
    ]
    meeting_state = list(
        TermSectionMeeting.objects.filter(term_section_id__in=section_ids)
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
            "created_at",
            "updated_at",
        )
    )
    membership_state = list(
        TermSectionProgram.objects.filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list(
            "id",
            "term_section_id",
            "program",
            "assignment_source",
            "created_at",
        )
    )
    registration_state = list(
        StudentTermSection.objects.filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list(
            "id",
            "term_section_id",
            "student_id",
            "academic_year",
            "term",
            "source",
            "created_at",
            "updated_at",
        )
    )
    registered_student_ids = sorted({int(row[2]) for row in registration_state})
    student_program_state = sorted(
        (
            int(student_id),
            normalize_section_program(program),
        )
        for student_id, program in Student.objects.filter(
            student_id__in=registered_student_ids
        ).values_list("student_id", "program")
    )

    payload = {
        "version": 2,
        "mutation_inputs": {
            "source_path": source_path,
            "has_program_column": has_program_column,
            "default_programs": default_programs,
            "source_tag": str(source_tag),
        },
        "normalized_rows": canonical_rows,
        "affected_database_state": {
            "sections": section_state,
            "meetings": meeting_state,
            "programme_links": membership_state,
            "registrations": registration_state,
            "student_programmes": student_program_state,
        },
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def preview_term_sections_from_csv(
    csv_path: str | Path,
    academic_year: str = "",
    term: str = "",
    source_tag: str = "other",
    max_preview_rows: int = 300,
    default_programs: list[Any] | None = None,
) -> dict[str, Any]:
    path, rows, has_program_column = _load_rows(csv_path)
    source_path = str(path.resolve())
    normalized_defaults = _normalize_default_programs(default_programs)
    grouped = _group_rows(rows)
    with section_snapshot_operation_guard(blocking=False) as acquired:
        if not acquired:
            raise ValueError(
                "Another current section snapshot operation is in progress; retry preview shortly."
            )
        _validate_known_programs(rows, normalized_defaults)
        (
            impact,
            requested_by_section,
            final_programs_by_section,
            programme_source_by_section,
            final_assignments_by_section,
        ) = _build_import_impact(
            grouped,
            has_program_column=has_program_column,
            default_programs=normalized_defaults,
        )
        preview_fingerprint = _import_preview_fingerprint(
            rows,
            grouped,
            source_path=source_path,
            has_program_column=has_program_column,
            default_programs=normalized_defaults,
            source_tag=source_tag,
        )
    predicted_unassigned = int(str(impact["predicted_fully_unassigned_sections"]))
    preview_rows = [
        {
            "source_tag": source_tag,
            **row,
            "requested_programs": requested_by_section[(row["course_key"], row["section"])],
            "effective_programs": final_programs_by_section[(row["course_key"], row["section"])],
            "effective_program_assignments": final_assignments_by_section[
                (row["course_key"], row["section"])
            ],
            "programme_source": programme_source_by_section[(row["course_key"], row["section"])],
        }
        for row in rows[:max_preview_rows]
    ]
    warning = ""
    if predicted_unassigned:
        warning = (
            f"Import is blocked: {predicted_unassigned} affected section(s) would "
            "have no programme membership. Select default programmes or supply "
            "programme values in the CSV."
        )
    elif not has_program_column and not normalized_defaults:
        warning = (
            "No programme column or defaults were supplied. Existing imported "
            "memberships will be preserved."
        )
    return {
        "source_tag": source_tag,
        "source": source_path,
        "preview_fingerprint": preview_fingerprint,
        "expected_confirmation": f"IMPORT {impact['sections_unique']}",
        "default_programs": normalized_defaults,
        "has_program_column": has_program_column,
        "program_membership_status": (
            "csv"
            if has_program_column
            else ("defaults" if normalized_defaults else "legacy_preserve")
        ),
        "can_import": predicted_unassigned == 0,
        "unassigned_section_count": predicted_unassigned,
        "unassigned_section_basis": "predicted_database_result",
        "program_membership_warning": warning,
        "impact": impact,
        "total_rows": len(rows),
        "preview_count": len(preview_rows),
        "preview_rows": preview_rows,
    }


def import_term_sections_from_csv(
    csv_path: str | Path,
    academic_year: str = "",
    term: str = "",
    source_tag: str = "other",
    truncate_existing_term: bool = False,
    default_programs: list[Any] | None = None,
    expected_preview_fingerprint: str | None = None,
    backup_before_import: bool = False,
) -> dict[str, object]:
    from django.db import transaction

    path, rows, has_program_column = _load_rows(csv_path)
    source_path = str(path.resolve())
    normalized_defaults = _normalize_default_programs(default_programs)
    grouped = _group_rows(rows)
    if not isinstance(backup_before_import, bool):
        raise ValueError("backup_before_import must be a boolean")

    # Replacement is deliberately not an import mode. It bypasses the signed
    # preview, backup, scraper guard, and planner-reference checks in DB Admin's
    # dedicated clear operation. Refuse before entering any write transaction.
    if truncate_existing_term:
        raise ValueError(
            "Replacing sections during CSV import is disabled. Use DB Admin > "
            "Clear Current Section Snapshot first, then run this import in merge mode."
        )

    with section_snapshot_operation_guard(blocking=False) as acquired:
        if not acquired:
            raise ValueError(
                "Another current section snapshot operation is in progress; retry shortly."
            )

        _validate_known_programs(rows, normalized_defaults)
        # Perform the first guarded preflight before taking the optional SQLite
        # snapshot. SQLite's WAL checkpoint cannot safely run inside atomic().
        preview_fingerprint = _import_preview_fingerprint(
            rows,
            grouped,
            source_path=source_path,
            has_program_column=has_program_column,
            default_programs=normalized_defaults,
            source_tag=source_tag,
        )
        if expected_preview_fingerprint is not None:
            expected = str(expected_preview_fingerprint).strip().lower()
            if not expected or expected != preview_fingerprint:
                raise ValueError("Import preview is stale; run preview again before importing.")

        (
            impact,
            requested_by_section,
            _final_programs,
            _programme_sources,
            _final_assignments,
        ) = _build_import_impact(
            grouped,
            has_program_column=has_program_column,
            default_programs=normalized_defaults,
        )
        predicted_unassigned = int(str(impact["predicted_fully_unassigned_sections"]))
        if predicted_unassigned:
            samples = impact["fully_unassigned_sections"]
            raise ValueError(
                f"Import would leave {predicted_unassigned} affected section(s) "
                "without a programme membership. Supply default_programs or CSV "
                f"programme values. Affected sample: {samples}"
            )

        backup: dict[str, object] | None = None
        if backup_before_import:
            from core.services.db_admin_ops import create_backup_snapshot

            backup_result = create_backup_snapshot()
            if not isinstance(backup_result, dict) or backup_result.get("ok") is not True:
                raise RuntimeError("Database backup failed; no sections were imported")
            backup = backup_result

        with transaction.atomic():
            # The guard serializes cooperating snapshot operations. Revalidate
            # inside the write transaction as well so an unrelated DB writer
            # cannot make the just-created backup stale before mutation.
            _validate_known_programs(rows, normalized_defaults)
            mutation_fingerprint = _import_preview_fingerprint(
                rows,
                grouped,
                source_path=source_path,
                has_program_column=has_program_column,
                default_programs=normalized_defaults,
                source_tag=source_tag,
            )
            if mutation_fingerprint != preview_fingerprint:
                raise ValueError("Import preview is stale; run preview again before importing.")

            (
                impact,
                requested_by_section,
                _final_programs,
                _programme_sources,
                _final_assignments,
            ) = _build_import_impact(
                grouped,
                has_program_column=has_program_column,
                default_programs=normalized_defaults,
            )
            predicted_unassigned = int(str(impact["predicted_fully_unassigned_sections"]))
            if predicted_unassigned:
                samples = impact["fully_unassigned_sections"]
                raise ValueError(
                    f"Import would leave {predicted_unassigned} affected section(s) "
                    "without a programme membership. Supply default_programs or CSV "
                    f"programme values. Affected sample: {samples}"
                )

            inserted_sections = 0
            inserted_meetings = 0
            upserted_program_links = 0
            removed_program_links = 0
            imported_section_ids: set[int] = set()
            authoritative_imports = has_program_column or bool(normalized_defaults)
            now_str = datetime.now(UTC).isoformat()

            for (course_key, section), meeting_rows in grouped.items():
                first = meeting_rows[0]
                cap_str = str(first.get("available_capacity", ""))
                reg_str = str(first.get("registered_count", ""))

                ts, _created = TermSection.objects.update_or_create(
                    scenario=None,
                    course_key=course_key,
                    section=section,
                    defaults={
                        "source_tag": source_tag,
                        "course_name": first["course_name"],
                        "available_capacity": int(cap_str) if cap_str.isdigit() else None,
                        "registered_count": int(reg_str) if reg_str.isdigit() else None,
                        "course_code": first["course_code"],
                        "course_number": first["course_number"],
                        "source_file": source_path,
                        "updated_at": now_str,
                    },
                )
                inserted_sections += 1
                imported_section_ids.add(int(ts.id))

                programs = requested_by_section[(course_key, section)]
                if authoritative_imports:
                    imported_links = TermSectionProgram.objects.filter(
                        term_section=ts,
                        assignment_source="import",
                    )
                    if programs:
                        removed_program_links += imported_links.exclude(
                            program__in=programs
                        ).delete()[0]
                    else:
                        removed_program_links += imported_links.delete()[0]

                for program in programs:
                    TermSectionProgram.objects.update_or_create(
                        term_section=ts,
                        program=program,
                        defaults={"assignment_source": "import"},
                    )
                    upserted_program_links += 1

                TermSectionMeeting.objects.filter(term_section=ts).delete()
                seen_meetings: set[tuple[str, ...]] = set()
                for row in meeting_rows:
                    meeting_key = (
                        row["day"],
                        row["start_time"],
                        row["end_time"],
                        row["room"],
                        row["instructor"],
                    )
                    if meeting_key in seen_meetings:
                        continue
                    seen_meetings.add(meeting_key)
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

            if imported_section_ids:
                reconcile_observed_section_programs(imported_section_ids)

            # Defense in depth: keep the refusal atomic even if impact logic and
            # mutation logic drift in a future change.
            imported_scope_unassigned_count = TermSection.objects.filter(
                id__in=imported_section_ids,
                program_links__isnull=True,
            ).count()
            if imported_scope_unassigned_count:
                raise ValueError(
                    f"Import produced {imported_scope_unassigned_count} unassigned "
                    "section(s); all changes were rolled back."
                )

    total_sections = TermSection.objects.count()
    total_meetings = TermSectionMeeting.objects.count()
    current_unassigned_section_count = TermSection.objects.filter(
        scenario__isnull=True,
        program_links__isnull=True,
    ).count()
    warning = ""
    if not has_program_column and not normalized_defaults:
        warning = (
            "No programme column or defaults were supplied; existing imported "
            "memberships were preserved."
        )

    return {
        "source_tag": source_tag,
        "preview_fingerprint": preview_fingerprint,
        "expected_confirmation": f"IMPORT {impact['sections_unique']}",
        "truncate_existing": False,
        "deleted_sections": 0,
        "deleted_meetings": 0,
        "inserted_or_updated": inserted_meetings,
        "sections_imported": inserted_sections,
        "program_links_upserted": upserted_program_links,
        "program_links_removed": removed_program_links,
        "default_programs": normalized_defaults,
        "has_program_column": has_program_column,
        "program_membership_status": (
            "csv"
            if has_program_column
            else ("defaults" if normalized_defaults else "legacy_preserve")
        ),
        "can_import": True,
        "unassigned_section_count": 0,
        "unassigned_section_basis": "actual_database_result",
        "imported_scope_unassigned_count": 0,
        "current_unassigned_section_count": current_unassigned_section_count,
        "program_membership_warning": warning,
        "backup": backup,
        "impact": impact,
        "rows_total": total_sections,
        "meetings_total": total_meetings,
        "source": source_path,
    }
