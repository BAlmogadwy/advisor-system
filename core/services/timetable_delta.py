from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DELTA_SCHEMA_VERSION = "academic_timetable_delta.v1"
STATE_SCHEMA_VERSION = "academic_timetable_state.v1"
CANONICALIZATION_VERSION = "json-sort-keys-ascii-v1"
EXPORTER_VERSION = "1"
REGISTRAR_ACADEMIC_YEAR = "1448"
REGISTRAR_TERM = "1"
REGISTRAR_SOURCE = "scraper_timetable"


class TimetableDeltaError(ValueError):
    """Raised when a frozen timetable snapshot cannot produce a safe delta."""


@dataclass(frozen=True)
class CapturedTimetableState:
    path: Path
    fingerprint: dict[str, Any]
    migrations: dict[str, Any]
    state: dict[str, Any]
    student_programs: dict[int, str]
    student_roster: dict[int, dict[str, str]]


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one JSON representation used by both exporter and importer."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TimetableDeltaError("Timetable state is not canonical JSON data.") from exc


def normalise_section_key(
    course_code: object,
    course_number: object,
    section: object,
) -> tuple[str, str, str]:
    """Normalize the durable, cross-database identity of a global section."""

    code = str(course_code or "").strip().upper()
    number = str(course_number or "").strip()
    section_name = str(section or "").strip().upper()
    if not code or not number or not section_name:
        raise TimetableDeltaError(
            "Global section keys require nonblank course_code, course_number, and section."
        )
    return code, number, section_name


def derived_course_key(course_code: object, course_number: object) -> str:
    code = str(course_code or "").strip().upper()
    number = str(course_number or "").strip()
    return f"{code}{number}".replace(" ", "").upper()


def section_key_dict(key: tuple[str, str, str]) -> dict[str, str]:
    return {
        "course_code": key[0],
        "course_number": key[1],
        "section": key[2],
    }


def _sorted_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(record) for record in records]
    return sorted(copied, key=canonical_json_bytes)


def canonical_state_document(
    *,
    sections: Iterable[Mapping[str, Any]],
    programs: Iterable[Mapping[str, Any]],
    meetings: Iterable[Mapping[str, Any]],
    student_term_sections: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the timestamp- and PK-free state document hashed before import."""

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "sections": _sorted_records(sections),
        "programs": _sorted_records(programs),
        "meetings": _sorted_records(meetings),
        "student_term_sections": _sorted_records(student_term_sections),
    }


def state_sha256(state: Mapping[str, Any]) -> str:
    """Hash state independently of input row order or database backend."""

    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise TimetableDeltaError("Unsupported timetable state schema version.")
    canonical = canonical_state_document(
        sections=_record_list(state, "sections"),
        programs=_record_list(state, "programs"),
        meetings=_record_list(state, "meetings"),
        student_term_sections=_record_list(state, "student_term_sections"),
    )
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


def _record_list(state: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value = state.get(name)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise TimetableDeltaError(f"Timetable state {name!r} must be a list of objects.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def snapshot_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "sha256": _sha256_file(path),
        "size_bytes": stat.st_size,
        "modified_at_utc": _utc_iso(stat.st_mtime),
    }


def validate_frozen_snapshot(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise TimetableDeltaError(f"Frozen SQLite snapshot does not exist: {resolved}")
    data_bearing_sidecars = [
        Path(f"{resolved}{suffix}")
        for suffix in ("-wal", "-journal")
        if Path(f"{resolved}{suffix}").exists() and Path(f"{resolved}{suffix}").stat().st_size > 0
    ]
    if data_bearing_sidecars:
        raise TimetableDeltaError(
            "Frozen SQLite snapshots must not have a data-bearing WAL or journal sidecar."
        )
    return resolved


def _read_rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in connection.execute(sql).fetchall()]
    except sqlite3.DatabaseError as exc:
        raise TimetableDeltaError("Snapshot does not have the expected timetable schema.") from exc


def _section_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    key = normalise_section_key(row["course_code"], row["course_number"], row["section"])
    stored_course_key = str(row.get("course_key") or "").replace(" ", "").upper()
    expected_course_key = derived_course_key(key[0], key[1])
    if stored_course_key != expected_course_key:
        raise TimetableDeltaError(
            "A global section's stored course_key does not match course_code/course_number: "
            f"{stored_course_key!r} != {expected_course_key!r}."
        )
    return key


def _capture_state(connection: sqlite3.Connection) -> dict[str, Any]:
    section_rows = _read_rows(
        connection,
        """
        SELECT id, source_tag, course_name, available_capacity, registered_count,
               course_code, course_number, course_key, section
        FROM term_sections
        WHERE scenario_id IS NULL
        """,
    )
    section_keys_by_id: dict[int, tuple[str, str, str]] = {}
    sections: list[dict[str, Any]] = []
    seen_section_keys: set[tuple[str, str, str]] = set()
    seen_database_keys: set[tuple[str, str]] = set()
    for row in section_rows:
        key = _section_identity(row)
        if key in seen_section_keys:
            raise TimetableDeltaError(f"Normalized global section key is duplicated: {key!r}.")
        seen_section_keys.add(key)
        database_key = (derived_course_key(key[0], key[1]), key[2])
        if database_key in seen_database_keys:
            raise TimetableDeltaError(
                "Two split section identities collapse to the same course_key/section: "
                f"{database_key!r}."
            )
        seen_database_keys.add(database_key)
        section_keys_by_id[int(row["id"])] = key
        sections.append(
            {
                **section_key_dict(key),
                "source_tag": str(row.get("source_tag") or ""),
                "course_name": str(row.get("course_name") or ""),
                "available_capacity": row.get("available_capacity"),
                "registered_count": row.get("registered_count"),
            }
        )

    programs: list[dict[str, Any]] = []
    seen_programs: set[tuple[str, str, str, str]] = set()
    for row in _read_rows(
        connection,
        """
        SELECT p.term_section_id, p.program, p.assignment_source
        FROM term_section_programs AS p
        INNER JOIN term_sections AS s ON s.id = p.term_section_id
        WHERE s.scenario_id IS NULL
        """,
    ):
        section_key = section_keys_by_id[int(row["term_section_id"])]
        program_code = str(row.get("program") or "").strip().upper()
        if not program_code:
            raise TimetableDeltaError("A global section has a blank program assignment.")
        program_natural_key = (*section_key, program_code)
        if program_natural_key in seen_programs:
            raise TimetableDeltaError(f"Program assignment is duplicated: {program_natural_key!r}.")
        seen_programs.add(program_natural_key)
        programs.append(
            {
                **section_key_dict(section_key),
                "program_code": program_code,
                "assignment_source": str(row.get("assignment_source") or ""),
            }
        )

    meetings: list[dict[str, Any]] = []
    seen_meetings: set[tuple[str, ...]] = set()
    for row in _read_rows(
        connection,
        """
        SELECT m.term_section_id, m.day, m.start_time, m.end_time,
               m.building, m.floor_wing, m.room, m.instructor
        FROM term_section_meetings AS m
        INNER JOIN term_sections AS s ON s.id = m.term_section_id
        WHERE s.scenario_id IS NULL
        """,
    ):
        section_key = section_keys_by_id[int(row["term_section_id"])]
        meeting = {
            **section_key_dict(section_key),
            "day": str(row.get("day") or ""),
            "start_time": str(row.get("start_time") or ""),
            "end_time": str(row.get("end_time") or ""),
            "building": str(row.get("building") or ""),
            "floor_wing": str(row.get("floor_wing") or ""),
            "room": str(row.get("room") or ""),
            "instructor": str(row.get("instructor") or ""),
        }
        meeting_natural_key = (
            *section_key,
            meeting["day"],
            meeting["start_time"],
            meeting["end_time"],
            meeting["room"],
            meeting["instructor"],
        )
        if meeting_natural_key in seen_meetings:
            raise TimetableDeltaError(
                f"Meeting natural key is duplicated: {meeting_natural_key!r}."
            )
        seen_meetings.add(meeting_natural_key)
        meetings.append(meeting)

    student_term_sections: list[dict[str, Any]] = []
    seen_student_links: set[tuple[Any, ...]] = set()
    for row in _read_rows(
        connection,
        """
        SELECT st.student_id, st.academic_year, st.term, st.source, st.term_section_id
        FROM student_term_sections AS st
        INNER JOIN term_sections AS s ON s.id = st.term_section_id
        WHERE s.scenario_id IS NULL
        """,
    ):
        section_key = section_keys_by_id[int(row["term_section_id"])]
        student_link = {
            "student_id": int(row["student_id"]),
            "academic_year": str(row.get("academic_year") or ""),
            "term": str(row.get("term") or ""),
            **section_key_dict(section_key),
            "source": str(row.get("source") or ""),
        }
        student_natural_key = (
            student_link["student_id"],
            student_link["academic_year"],
            student_link["term"],
            *section_key,
            student_link["source"],
        )
        if student_natural_key in seen_student_links:
            raise TimetableDeltaError("Student section relationship natural keys collide.")
        seen_student_links.add(student_natural_key)
        student_term_sections.append(student_link)

    return canonical_state_document(
        sections=sections,
        programs=programs,
        meetings=meetings,
        student_term_sections=student_term_sections,
    )


def _migration_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = _read_rows(
        connection,
        "SELECT app, name FROM django_migrations ORDER BY app, name",
    )
    applied = [{"app": str(row["app"]), "name": str(row["name"])} for row in rows]
    highest_name_by_app: dict[str, str] = {}
    for row in applied:
        highest_name_by_app[row["app"]] = row["name"]
    return {
        "applied_count": len(applied),
        "applied_sha256": hashlib.sha256(canonical_json_bytes(applied)).hexdigest(),
        "highest_name_by_app": highest_name_by_app,
    }


def capture_sqlite_timetable_state(path: Path) -> CapturedTimetableState:
    """Capture only production-safe timetable state from a frozen SQLite file."""

    resolved = validate_frozen_snapshot(path)
    before = snapshot_fingerprint(resolved)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise TimetableDeltaError("Frozen SQLite snapshot failed PRAGMA quick_check.")
            state = _capture_state(connection)
            migrations = _migration_metadata(connection)
            student_roster = {
                int(row["student_id"]): {
                    "program": str(row.get("program") or "").strip().upper(),
                    "section": str(row.get("section") or "").strip().upper(),
                    "status": str(row.get("status") or "").strip().lower(),
                }
                for row in _read_rows(
                    connection,
                    "SELECT student_id, program, section, status FROM students",
                )
            }
            student_programs = {
                student_id: record["program"] for student_id, record in student_roster.items()
            }
    except sqlite3.DatabaseError as exc:
        raise TimetableDeltaError("Could not read frozen SQLite timetable snapshot.") from exc

    after = snapshot_fingerprint(resolved)
    validate_frozen_snapshot(resolved)
    if before != after:
        raise TimetableDeltaError("Frozen SQLite snapshot changed while it was being read.")
    return CapturedTimetableState(
        path=resolved,
        fingerprint=after,
        migrations=migrations,
        state=state,
        student_programs=student_programs,
        student_roster=student_roster,
    )


def _key_from_record(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return normalise_section_key(
        record.get("course_code"), record.get("course_number"), record.get("section")
    )


def _index_records(
    records: Iterable[Mapping[str, Any]],
    key_builder: Any,
    *,
    label: str,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = tuple(key_builder(record))
        if key in indexed:
            raise TimetableDeltaError(f"Duplicate {label} natural key.")
        indexed[key] = dict(record)
    return indexed


def _program_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return (*_key_from_record(record), str(record.get("program_code") or ""))


def _meeting_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        *_key_from_record(record),
        str(record.get("day") or ""),
        str(record.get("start_time") or ""),
        str(record.get("end_time") or ""),
        str(record.get("room") or ""),
        str(record.get("instructor") or ""),
    )


def _student_link_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(record.get("student_id") or 0),
        str(record.get("academic_year") or ""),
        str(record.get("term") or ""),
        *_key_from_record(record),
        str(record.get("source") or ""),
    )


def _nested_program(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "program_code": record["program_code"],
        "assignment_source": record["assignment_source"],
    }


def _nested_meeting(record: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"course_code", "course_number", "section"}
    return {key: value for key, value in record.items() if key not in excluded}


def _is_registrar_link(record: Mapping[str, Any]) -> bool:
    return (
        record.get("academic_year") == REGISTRAR_ACADEMIC_YEAR
        and record.get("term") == REGISTRAR_TERM
        and record.get("source") == REGISTRAR_SOURCE
    )


def _student_section_sets(
    records: Iterable[Mapping[str, Any]],
) -> dict[int, set[tuple[str, str, str]]]:
    result: dict[int, set[tuple[str, str, str]]] = {}
    for record in records:
        result.setdefault(int(record["student_id"]), set()).add(_key_from_record(record))
    return result


def _scoped_state(
    state: Mapping[str, Any],
    *,
    section_keys: set[tuple[str, str, str]],
    student_links: Iterable[Mapping[str, Any]],
    programs: Iterable[Mapping[str, Any]],
    meetings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    return canonical_state_document(
        sections=(
            record
            for record in _record_list(state, "sections")
            if _key_from_record(record) in section_keys
        ),
        programs=programs,
        meetings=meetings,
        student_term_sections=student_links,
    )


def _scope_change_summary(
    base_links: Mapping[tuple[Any, ...], Mapping[str, Any]],
    target_links: Mapping[tuple[Any, ...], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    changes: Counter[tuple[str, str, str, str]] = Counter()
    for operation, keys, records in (
        ("added", set(target_links) - set(base_links), target_links),
        ("removed", set(base_links) - set(target_links), base_links),
    ):
        for key in keys:
            record = records[key]
            if _is_registrar_link(record):
                continue
            changes[
                (
                    str(record["academic_year"]),
                    str(record["term"]),
                    str(record["source"]),
                    operation,
                )
            ] += 1
    return [
        {
            "academic_year": key[0],
            "term": key[1],
            "source": key[2],
            "operation": key[3],
            "count": count,
        }
        for key, count in sorted(changes.items())
    ]


def _observed_basis(
    snapshot: CapturedTimetableState,
    links: Iterable[Mapping[str, Any]],
) -> tuple[str, dict[str, int]]:
    student_ids = sorted({int(record["student_id"]) for record in links})
    records = [
        {
            "student_id": student_id,
            "program": snapshot.student_programs.get(student_id, ""),
        }
        for student_id in student_ids
    ]
    missing = sum(not str(record["program"]) for record in records)
    return hashlib.sha256(canonical_json_bytes(records)).hexdigest(), {
        "students": len(records),
        "missing_programs": missing,
    }


def build_timetable_delta(
    base: CapturedTimetableState,
    target: CapturedTimetableState,
) -> dict[str, Any]:
    """Build the guarded 1448/1 registrar snapshot delta used for this release."""

    base_sections = _index_records(
        _record_list(base.state, "sections"), _key_from_record, label="section"
    )
    target_sections = _index_records(
        _record_list(target.state, "sections"), _key_from_record, label="section"
    )
    all_base_programs = _index_records(
        _record_list(base.state, "programs"), _program_key, label="program assignment"
    )
    all_target_programs = _index_records(
        _record_list(target.state, "programs"), _program_key, label="program assignment"
    )
    all_base_meetings = _index_records(
        _record_list(base.state, "meetings"), _meeting_key, label="meeting"
    )
    all_target_meetings = _index_records(
        _record_list(target.state, "meetings"), _meeting_key, label="meeting"
    )
    all_base_links = _index_records(
        _record_list(base.state, "student_term_sections"),
        _student_link_key,
        label="student section relationship",
    )
    all_target_links = _index_records(
        _record_list(target.state, "student_term_sections"),
        _student_link_key,
        label="student section relationship",
    )
    base_scope_links = {
        key: record for key, record in all_base_links.items() if _is_registrar_link(record)
    }
    target_scope_links = {
        key: record for key, record in all_target_links.items() if _is_registrar_link(record)
    }
    base_student_sections = _student_section_sets(base_scope_links.values())
    target_student_sections = _student_section_sets(target_scope_links.values())
    candidate_touched_student_ids = sorted(
        student_id
        for student_id in set(base_student_sections) | set(target_student_sections)
        if base_student_sections.get(student_id, set())
        != target_student_sections.get(student_id, set())
    )

    touched_students: list[dict[str, Any]] = []
    relationship_additions = 0
    relationship_removals = 0
    excluded_student_changes: Counter[str] = Counter()
    excluded_relationship_additions = 0
    excluded_relationship_removals = 0
    excluded_touched_ids: set[int] = set()
    for student_id in candidate_touched_student_ids:
        before_keys = base_student_sections.get(student_id, set())
        after_keys = target_student_sections.get(student_id, set())
        base_roster = base.student_roster.get(student_id)
        target_roster = target.student_roster.get(student_id)
        exclusion_reason = ""
        if base_roster is None:
            exclusion_reason = "absent_from_base"
        elif target_roster is None:
            exclusion_reason = "absent_from_target"
        elif not base_roster["program"] or not target_roster["program"]:
            exclusion_reason = "blank_program"
        elif base_roster != target_roster:
            exclusion_reason = "roster_semantics_changed"
        if exclusion_reason:
            excluded_student_changes[exclusion_reason] += 1
            excluded_relationship_additions += len(after_keys - before_keys)
            excluded_relationship_removals += len(before_keys - after_keys)
            excluded_touched_ids.add(student_id)
            continue

        if target_roster is None:
            raise TimetableDeltaError("Eligible touched student has no target roster state.")
        relationship_additions += len(after_keys - before_keys)
        relationship_removals += len(before_keys - after_keys)
        touched_students.append(
            {
                "student_id": student_id,
                "expected_program": target_roster["program"],
                "expected_section": target_roster["section"],
                "expected_status": target_roster["status"],
                "base_sections": [section_key_dict(key) for key in sorted(before_keys)],
                "target_sections": [section_key_dict(key) for key in sorted(after_keys)],
            }
        )

    base_scope_links_by_student: dict[int, list[dict[str, Any]]] = {}
    target_scope_links_by_student: dict[int, list[dict[str, Any]]] = {}
    for record in base_scope_links.values():
        base_scope_links_by_student.setdefault(int(record["student_id"]), []).append(record)
    for record in target_scope_links.values():
        target_scope_links_by_student.setdefault(int(record["student_id"]), []).append(record)
    effective_target_link_records: list[dict[str, Any]] = []
    for student_id in sorted(set(base_student_sections) | set(target_student_sections)):
        source = (
            base_scope_links_by_student
            if student_id in excluded_touched_ids
            else target_scope_links_by_student
        )
        effective_target_link_records.extend(source.get(student_id, []))
    effective_target_scope_links = _index_records(
        effective_target_link_records,
        _student_link_key,
        label="effective target student section relationship",
    )

    base_scope_section_keys = {_key_from_record(row) for row in base_scope_links.values()}
    target_scope_section_keys = {
        _key_from_record(row) for row in effective_target_scope_links.values()
    }
    publishable_target_links = (
        record
        for record in target_scope_links.values()
        if int(record["student_id"]) not in excluded_touched_ids
    )
    section_update_keys = {_key_from_record(record) for record in publishable_target_links}
    relevant_section_keys = base_scope_section_keys | target_scope_section_keys
    missing_target_sections = section_update_keys - set(target_sections)
    if missing_target_sections:
        raise TimetableDeltaError("A target registrar link references a missing global section.")

    base_import_programs = {
        key: record
        for key, record in all_base_programs.items()
        if _key_from_record(record) in section_update_keys
        and record.get("assignment_source") == "import"
    }
    target_source_import_programs = {
        key: record
        for key, record in all_target_programs.items()
        if _key_from_record(record) in section_update_keys
        and record.get("assignment_source") == "import"
    }
    base_meetings = {
        key: record
        for key, record in all_base_meetings.items()
        if _key_from_record(record) in section_update_keys
    }
    target_meetings = {
        key: record
        for key, record in all_target_meetings.items()
        if _key_from_record(record) in section_update_keys
    }

    preserved_programs_by_section: dict[tuple[str, str, str], list[dict[str, Any]]] = {
        key: [] for key in section_update_keys
    }
    for record in base_import_programs.values():
        preserved_programs_by_section[_key_from_record(record)].append(_nested_program(record))
    target_meetings_by_section: dict[tuple[str, str, str], list[dict[str, Any]]] = {
        key: [] for key in section_update_keys
    }
    base_meetings_by_section: dict[tuple[str, str, str], list[dict[str, Any]]] = {
        key: [] for key in section_update_keys
    }
    for record in target_meetings.values():
        target_meetings_by_section[_key_from_record(record)].append(_nested_meeting(record))
    for record in base_meetings.values():
        base_meetings_by_section[_key_from_record(record)].append(_nested_meeting(record))

    section_upserts: list[dict[str, Any]] = []
    created_section_count = 0
    updated_section_count = 0
    preserve_empty_meeting_sections = 0
    for key in sorted(section_update_keys):
        target_nested_meetings = _sorted_records(target_meetings_by_section[key])
        meetings_complete = bool(target_nested_meetings)
        if not meetings_complete:
            preserve_empty_meeting_sections += 1
        target_record = {
            **target_sections[key],
            "programs": _sorted_records(preserved_programs_by_section[key]),
            "programs_complete": True,
            "meetings": target_nested_meetings,
            "meetings_complete": meetings_complete,
            "meeting_mode": "replace" if meetings_complete else "preserve",
        }
        base_record = None
        if key in base_sections:
            base_record = {
                **base_sections[key],
                "programs": _sorted_records(
                    _nested_program(record)
                    for record in base_import_programs.values()
                    if _key_from_record(record) == key
                ),
                "programs_complete": True,
                "meetings": (
                    _sorted_records(base_meetings_by_section[key]) if meetings_complete else []
                ),
                "meetings_complete": meetings_complete,
                "meeting_mode": "replace" if meetings_complete else "preserve",
            }
        if base_record != target_record:
            section_upserts.append(target_record)
            if base_record is None:
                created_section_count += 1
            else:
                updated_section_count += 1

    excluded_import_program_churn = {
        "added": len(set(target_source_import_programs) - set(base_import_programs)),
        "updated": sum(
            target_source_import_programs[key] != base_import_programs[key]
            for key in set(target_source_import_programs) & set(base_import_programs)
        ),
        "removed": len(set(base_import_programs) - set(target_source_import_programs)),
        "applied": False,
        "reason": "production_import_and_manual_memberships_are_authoritative",
    }
    program_additions: list[dict[str, Any]] = []
    program_removals: list[dict[str, Any]] = []
    program_updates: list[dict[str, Any]] = []

    meeting_authoritative_keys = {key for key, rows in target_meetings_by_section.items() if rows}
    authoritative_base_meetings = {
        key: record
        for key, record in base_meetings.items()
        if _key_from_record(record) in meeting_authoritative_keys
    }
    authoritative_target_meetings = {
        key: record
        for key, record in target_meetings.items()
        if _key_from_record(record) in meeting_authoritative_keys
    }
    meeting_additions = _sorted_records(
        authoritative_target_meetings[key]
        for key in set(authoritative_target_meetings) - set(authoritative_base_meetings)
    )
    meeting_removals = _sorted_records(
        authoritative_base_meetings[key]
        for key in set(authoritative_base_meetings) - set(authoritative_target_meetings)
    )
    meeting_updates = _sorted_records(
        {
            "before": authoritative_base_meetings[key],
            "after": authoritative_target_meetings[key],
        }
        for key in set(authoritative_base_meetings) & set(authoritative_target_meetings)
        if authoritative_base_meetings[key] != authoritative_target_meetings[key]
    )

    observed_base = {
        key: record
        for key, record in all_base_programs.items()
        if record.get("assignment_source") == "observed"
    }
    observed_target = {
        key: record
        for key, record in all_target_programs.items()
        if record.get("assignment_source") == "observed"
    }
    observed_churn = {
        "added": len(set(observed_target) - set(observed_base)),
        "updated": sum(
            observed_target[key] != observed_base[key]
            for key in set(observed_target) & set(observed_base)
        ),
        "removed": len(set(observed_base) - set(observed_target)),
        "applied": False,
        "reason": "derived_from_final_registrar_links_and_production_student_programs",
    }

    base_scope_programs = (
        record
        for record in all_base_programs.values()
        if _key_from_record(record) in relevant_section_keys
        and record.get("assignment_source") == "import"
    )
    base_scope_meetings = (
        record
        for record in all_base_meetings.values()
        if _key_from_record(record) in relevant_section_keys
    )
    base_scoped_state = _scoped_state(
        base.state,
        section_keys=relevant_section_keys,
        student_links=base_scope_links.values(),
        programs=base_scope_programs,
        meetings=base_scope_meetings,
    )

    effective_target_sections: list[Mapping[str, Any]] = []
    effective_target_programs: list[Mapping[str, Any]] = []
    effective_target_meetings: list[Mapping[str, Any]] = []
    for key in sorted(relevant_section_keys):
        if key in section_update_keys:
            effective_target_sections.append(target_sections[key])
            effective_target_programs.extend(
                record
                for record in base_import_programs.values()
                if _key_from_record(record) == key
            )
            if target_meetings_by_section[key]:
                effective_target_meetings.extend(
                    record for record in target_meetings.values() if _key_from_record(record) == key
                )
            else:
                effective_target_meetings.extend(
                    record
                    for record in all_base_meetings.values()
                    if _key_from_record(record) == key
                )
        elif key in base_sections:
            effective_target_sections.append(base_sections[key])
            effective_target_programs.extend(
                record
                for record in all_base_programs.values()
                if _key_from_record(record) == key and record.get("assignment_source") == "import"
            )
            effective_target_meetings.extend(
                record for record in all_base_meetings.values() if _key_from_record(record) == key
            )
    target_scoped_state = canonical_state_document(
        sections=effective_target_sections,
        programs=effective_target_programs,
        meetings=effective_target_meetings,
        student_term_sections=effective_target_scope_links.values(),
    )

    base_observed_digest, base_observed_counts = _observed_basis(base, all_base_links.values())
    target_all_link_section_keys = {
        _key_from_record(record) for record in all_target_links.values()
    }
    target_orphan_keys = set(target_sections) - target_all_link_section_keys
    excluded_section_creates = set(target_sections) - set(base_sections) - section_update_keys
    global_sections_missing_from_target = set(base_sections) - set(target_sections)
    excluded_scope_changes = _scope_change_summary(all_base_links, all_target_links)

    expected_operations = {
        "sections_created": created_section_count,
        "sections_updated": updated_section_count,
        "section_upserts": len(section_upserts),
        "programs_added": len(program_additions),
        "programs_updated": len(program_updates),
        "programs_removed": len(program_removals),
        "meetings_added": len(meeting_additions),
        "meetings_updated": len(meeting_updates),
        "meetings_removed": len(meeting_removals),
        "students_replaced": len(touched_students),
        "student_term_sections_added": relationship_additions,
        "student_term_sections_removed": relationship_removals,
    }
    return {
        "schema_version": DELTA_SCHEMA_VERSION,
        "metadata": {
            "exporter_version": EXPORTER_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "generated_at_utc": target.fingerprint["modified_at_utc"],
            "generated_at_basis": "target_snapshot_mtime",
            "data_classification": "restricted_student_timetable",
            "contains_student_identifiers": bool(touched_students),
            "integrity_note": "sha256_requires_an_operator_supplied_expected_digest",
            "observed_program_churn": observed_churn,
            "excluded_import_program_churn": excluded_import_program_churn,
            "excluded_non_registrar_changes": excluded_scope_changes,
            "excluded_global_state": {
                "target_orphan_sections": len(target_orphan_keys),
                "target_section_creates_outside_scope": len(excluded_section_creates),
                "base_sections_missing_from_target": len(global_sections_missing_from_target),
                "empty_target_meeting_sets_preserved": preserve_empty_meeting_sections,
            },
            "excluded_student_relationship_changes": {
                "students_by_reason": dict(sorted(excluded_student_changes.items())),
                "relationships_added": excluded_relationship_additions,
                "relationships_removed": excluded_relationship_removals,
                "action": "preserve_production_and_route_to_separate_roster_sync",
            },
        },
        "scope": {
            "term_sections": "scenario_is_null_and_referenced_by_target_scope",
            "student_term_sections": {
                "academic_year": REGISTRAR_ACADEMIC_YEAR,
                "term": REGISTRAR_TERM,
                "source": REGISTRAR_SOURCE,
                "mode": "replace_complete_set_for_touched_students",
                "untouched_students": "preserve",
            },
            "program_assignments": "assignment_source_import_only",
            "observed_program_assignments": "rebuild_from_final_links",
            "excluded_payloads": [
                "accounts",
                "students_except_touched_id_and_expected_program",
                "courses",
                "registration_plan_and_other_student_link_sources",
                "scenario_sections",
                "runtime_state",
            ],
        },
        "base": {
            "database": base.fingerprint,
            "migrations": base.migrations,
            "counts": _state_counts(base.state),
            "state_sha256": state_sha256(base.state),
            "state_program_scope": "all_global_rows",
            "scoped_state_sha256": state_sha256(base_scoped_state),
            "scoped_counts": _state_counts(base_scoped_state),
            "observed_basis_sha256": base_observed_digest,
            "observed_basis_counts": base_observed_counts,
        },
        "target": {
            "database": target.fingerprint,
            "migrations": target.migrations,
            "source_counts": _state_counts(target.state),
            "source_state_sha256": state_sha256(target.state),
            "state_sha256": state_sha256(target_scoped_state),
            "state_program_scope": "import_only_with_observed_rebuilt_separately",
            "scoped_counts": _state_counts(target_scoped_state),
        },
        "expected_operations": expected_operations,
        "sections": {"upserts": _sorted_records(section_upserts), "removals": []},
        "programs": {
            "additions": program_additions,
            "updates": program_updates,
            "removals": program_removals,
        },
        "meetings": {
            "additions": meeting_additions,
            "updates": meeting_updates,
            "removals": meeting_removals,
        },
        "student_term_sections": {"touched_students": touched_students},
    }


def _state_counts(state: Mapping[str, Any]) -> dict[str, int]:
    return {
        "sections": len(_record_list(state, "sections")),
        "programs": len(_record_list(state, "programs")),
        "meetings": len(_record_list(state, "meetings")),
        "student_term_sections": len(_record_list(state, "student_term_sections")),
    }


def same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError as exc:
        raise TimetableDeltaError("Could not safely compare SQLite snapshot paths.") from exc
