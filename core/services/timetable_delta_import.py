from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.db import DatabaseError, connection, transaction
from django.db.migrations.recorder import MigrationRecorder

from core.models import (
    BoardSectionVisibility,
    DeliveryBoard,
    SectionPlacement,
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
from core.services.timetable_delta import (
    CANONICALIZATION_VERSION,
    DELTA_SCHEMA_VERSION,
    EXPORTER_VERSION,
    TimetableDeltaError,
    canonical_json_bytes,
    canonical_state_document,
    derived_course_key,
    normalise_section_key,
    section_key_dict,
    state_sha256,
)

SectionKey = tuple[str, str, str]
MeetingKey = tuple[SectionKey, str, str, str, str, str]
ProgramKey = tuple[SectionKey, str]
StudentLinkKey = tuple[int, str, str, SectionKey, str]

EXPECTED_OPERATION_KEYS = (
    "sections_created",
    "sections_updated",
    "section_upserts",
    "programs_added",
    "programs_updated",
    "programs_removed",
    "meetings_added",
    "meetings_updated",
    "meetings_removed",
    "students_replaced",
    "student_term_sections_added",
    "student_term_sections_removed",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_POSTGRES_ADVISORY_LOCK_KEY = 0x41545631444C5441  # "ATV1DLTA"
_POSTGRES_LOCK_TIMEOUT = "5s"
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_RELEASE_ACADEMIC_YEAR = "1448"
_RELEASE_TERM = "1"
_RELEASE_SOURCE = "scraper_timetable"
_RELEASE_SCOPE = (_RELEASE_ACADEMIC_YEAR, _RELEASE_TERM, _RELEASE_SOURCE)
_SUPPORTED_MEETING_DAYS = frozenset({"SUN", "MON", "TUE", "WED", "THU"})
_EXCLUDED_PAYLOADS = [
    "accounts",
    "students_except_touched_id_and_expected_program",
    "courses",
    "registration_plan_and_other_student_link_sources",
    "scenario_sections",
    "runtime_state",
]

_SECTION_KEY_FIELDS = frozenset({"course_code", "course_number", "section"})
_PROGRAM_RECORD_FIELDS = _SECTION_KEY_FIELDS | {"program_code", "assignment_source"}
_MEETING_NESTED_FIELDS = frozenset(
    {"day", "start_time", "end_time", "building", "floor_wing", "room", "instructor"}
)
_MEETING_RECORD_FIELDS = _SECTION_KEY_FIELDS | _MEETING_NESTED_FIELDS
_COUNT_FIELDS = frozenset({"sections", "programs", "meetings", "student_term_sections"})


@dataclass(frozen=True)
class LoadedDelta:
    path: Path
    sha256: str
    document: dict[str, Any]


@dataclass(frozen=True)
class DeltaPlan:
    operations: dict[str, int]
    base_state_sha256: str
    section_keys: tuple[SectionKey, ...]
    touched_student_ids: tuple[int, ...]
    touched_scopes: tuple[tuple[str, str, str], ...]
    observed_programs_added: int
    observed_programs_removed: int


@dataclass(frozen=True)
class TouchedStudent:
    expected_program: str
    expected_section: str
    expected_status: str
    base_sections: frozenset[SectionKey]
    target_sections: frozenset[SectionKey]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TimetableDeltaError(f"Artifact JSON contains duplicate key {key!r}.")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> Any:
    raise TimetableDeltaError(f"Artifact JSON contains non-finite number {value!r}.")


def load_timetable_delta(path: str | Path) -> LoadedDelta:
    artifact_path = Path(path).expanduser().resolve()
    if not artifact_path.is_file():
        raise TimetableDeltaError(f"Timetable delta artifact does not exist: {artifact_path}")
    if artifact_path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise TimetableDeltaError("Timetable delta artifact exceeds the 16 MiB safety limit.")
    payload = artifact_path.read_bytes()
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise TimetableDeltaError("Timetable delta artifact exceeds the 16 MiB safety limit.")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except UnicodeDecodeError as exc:
        raise TimetableDeltaError("Timetable delta artifact must be UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise TimetableDeltaError("Timetable delta artifact is not valid JSON.") from exc
    if not isinstance(document, dict):
        raise TimetableDeltaError("Timetable delta artifact root must be an object.")
    _validate_artifact_shape(document)
    if payload != canonical_json_bytes(document) + b"\n":
        raise TimetableDeltaError(
            "Timetable delta artifact must use the exporter canonical JSON encoding."
        )
    return LoadedDelta(path=artifact_path, sha256=digest, document=document)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TimetableDeltaError(f"{label} must be an object.")
    return value


def _strict_object(value: Any, label: str, fields: set[str] | frozenset[str]) -> dict[str, Any]:
    result = _mapping(value, label)
    if set(result) != set(fields):
        raise TimetableDeltaError(f"{label} does not match the strict artifact schema.")
    return result


def _record_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TimetableDeltaError(f"{label} must be a list of objects.")
    return value


def _strict_record_list(
    value: Any,
    label: str,
    fields: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    records = _record_list(value, label)
    for index, record in enumerate(records):
        if set(record) != set(fields):
            raise TimetableDeltaError(
                f"{label}[{index}] does not match the strict artifact schema."
            )
    return records


def _validate_count_object(value: Any, label: str, fields: frozenset[str]) -> None:
    counts = _strict_object(value, label, fields)
    for count in counts.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TimetableDeltaError(f"{label} values must be non-negative integers.")


def _validate_dynamic_scalar_map(
    value: Any,
    label: str,
    *,
    value_type: type,
) -> None:
    mapping = _mapping(value, label)
    if any(not isinstance(key, str) or not key for key in mapping):
        raise TimetableDeltaError(f"{label} keys must be nonblank strings.")
    if any(isinstance(item, bool) or not isinstance(item, value_type) for item in mapping.values()):
        raise TimetableDeltaError(f"{label} has an invalid scalar value.")
    if value_type is int and any(item < 0 for item in mapping.values()):
        raise TimetableDeltaError(f"{label} values cannot be negative.")


def _validate_strict_artifact_schema(document: dict[str, Any]) -> None:
    _strict_object(
        document,
        "artifact root",
        {
            "schema_version",
            "metadata",
            "scope",
            "base",
            "target",
            "expected_operations",
            "sections",
            "programs",
            "meetings",
            "student_term_sections",
        },
    )
    metadata = _strict_object(
        document.get("metadata"),
        "metadata",
        {
            "exporter_version",
            "canonicalization_version",
            "generated_at_utc",
            "generated_at_basis",
            "data_classification",
            "contains_student_identifiers",
            "integrity_note",
            "observed_program_churn",
            "excluded_import_program_churn",
            "excluded_non_registrar_changes",
            "excluded_global_state",
            "excluded_student_relationship_changes",
        },
    )
    churn_fields = {"added", "updated", "removed", "applied", "reason"}
    _strict_object(
        metadata["observed_program_churn"], "metadata.observed_program_churn", churn_fields
    )
    _strict_object(
        metadata["excluded_import_program_churn"],
        "metadata.excluded_import_program_churn",
        churn_fields,
    )
    _strict_record_list(
        metadata["excluded_non_registrar_changes"],
        "metadata.excluded_non_registrar_changes",
        {"academic_year", "term", "source", "operation", "count"},
    )
    _strict_object(
        metadata["excluded_global_state"],
        "metadata.excluded_global_state",
        {
            "target_orphan_sections",
            "target_section_creates_outside_scope",
            "base_sections_missing_from_target",
            "empty_target_meeting_sets_preserved",
        },
    )
    excluded_students = _strict_object(
        metadata["excluded_student_relationship_changes"],
        "metadata.excluded_student_relationship_changes",
        {"students_by_reason", "relationships_added", "relationships_removed", "action"},
    )
    _validate_dynamic_scalar_map(
        excluded_students["students_by_reason"],
        "metadata.excluded_student_relationship_changes.students_by_reason",
        value_type=int,
    )

    scope = _strict_object(
        document.get("scope"),
        "scope",
        {
            "term_sections",
            "student_term_sections",
            "program_assignments",
            "observed_program_assignments",
            "excluded_payloads",
        },
    )
    _strict_object(
        scope["student_term_sections"],
        "scope.student_term_sections",
        {"academic_year", "term", "source", "mode", "untouched_students"},
    )
    excluded_payloads = scope["excluded_payloads"]
    if not isinstance(excluded_payloads, list) or any(
        not isinstance(item, str) for item in excluded_payloads
    ):
        raise TimetableDeltaError("scope.excluded_payloads must be a list of strings.")

    base = _strict_object(
        document.get("base"),
        "base",
        {
            "database",
            "migrations",
            "counts",
            "state_sha256",
            "state_program_scope",
            "scoped_state_sha256",
            "scoped_counts",
            "observed_basis_sha256",
            "observed_basis_counts",
        },
    )
    target = _strict_object(
        document.get("target"),
        "target",
        {
            "database",
            "migrations",
            "source_counts",
            "source_state_sha256",
            "state_sha256",
            "state_program_scope",
            "scoped_counts",
        },
    )
    for label, parent in (("base", base), ("target", target)):
        _strict_object(
            parent["database"],
            f"{label}.database",
            {"sha256", "size_bytes", "modified_at_utc"},
        )
        migrations = _strict_object(
            parent["migrations"],
            f"{label}.migrations",
            {"applied_count", "applied_sha256", "highest_name_by_app"},
        )
        _validate_dynamic_scalar_map(
            migrations["highest_name_by_app"],
            f"{label}.migrations.highest_name_by_app",
            value_type=str,
        )
    _validate_count_object(base["counts"], "base.counts", _COUNT_FIELDS)
    _validate_count_object(base["scoped_counts"], "base.scoped_counts", _COUNT_FIELDS)
    _validate_count_object(target["source_counts"], "target.source_counts", _COUNT_FIELDS)
    _validate_count_object(target["scoped_counts"], "target.scoped_counts", _COUNT_FIELDS)
    _validate_count_object(
        base["observed_basis_counts"],
        "base.observed_basis_counts",
        frozenset({"students", "missing_programs"}),
    )

    sections = _strict_object(document.get("sections"), "sections", {"upserts", "removals"})
    section_fields = _SECTION_KEY_FIELDS | {
        "source_tag",
        "course_name",
        "available_capacity",
        "registered_count",
        "programs",
        "programs_complete",
        "meetings",
        "meetings_complete",
        "meeting_mode",
    }
    section_upserts = _strict_record_list(sections["upserts"], "sections.upserts", section_fields)
    _strict_record_list(sections["removals"], "sections.removals", _SECTION_KEY_FIELDS)
    for index, section in enumerate(section_upserts):
        _strict_record_list(
            section["programs"],
            f"sections.upserts[{index}].programs",
            {"program_code", "assignment_source"},
        )
        _strict_record_list(
            section["meetings"],
            f"sections.upserts[{index}].meetings",
            _MEETING_NESTED_FIELDS,
        )

    programs = _strict_object(
        document.get("programs"), "programs", {"additions", "updates", "removals"}
    )
    _strict_record_list(programs["additions"], "programs.additions", _PROGRAM_RECORD_FIELDS)
    _strict_record_list(programs["removals"], "programs.removals", _PROGRAM_RECORD_FIELDS)
    program_updates = _strict_record_list(
        programs["updates"], "programs.updates", {"before", "after"}
    )
    for index, update in enumerate(program_updates):
        _strict_object(
            update["before"], f"programs.updates[{index}].before", _PROGRAM_RECORD_FIELDS
        )
        _strict_object(update["after"], f"programs.updates[{index}].after", _PROGRAM_RECORD_FIELDS)

    meetings = _strict_object(
        document.get("meetings"), "meetings", {"additions", "updates", "removals"}
    )
    _strict_record_list(meetings["additions"], "meetings.additions", _MEETING_RECORD_FIELDS)
    _strict_record_list(meetings["removals"], "meetings.removals", _MEETING_RECORD_FIELDS)
    meeting_updates = _strict_record_list(
        meetings["updates"], "meetings.updates", {"before", "after"}
    )
    for index, update in enumerate(meeting_updates):
        _strict_object(
            update["before"], f"meetings.updates[{index}].before", _MEETING_RECORD_FIELDS
        )
        _strict_object(update["after"], f"meetings.updates[{index}].after", _MEETING_RECORD_FIELDS)

    student_changes = _strict_object(
        document.get("student_term_sections"),
        "student_term_sections",
        {"touched_students"},
    )
    touched_students = _strict_record_list(
        student_changes["touched_students"],
        "student_term_sections.touched_students",
        {
            "student_id",
            "expected_program",
            "expected_section",
            "expected_status",
            "base_sections",
            "target_sections",
        },
    )
    for index, student in enumerate(touched_students):
        _strict_record_list(
            student["base_sections"],
            f"student_term_sections.touched_students[{index}].base_sections",
            _SECTION_KEY_FIELDS,
        )
        _strict_record_list(
            student["target_sections"],
            f"student_term_sections.touched_students[{index}].target_sections",
            _SECTION_KEY_FIELDS,
        )


def _nonblank(value: Any, label: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise TimetableDeltaError(f"{label} must be a string.")
    result = value.strip()
    if not result:
        raise TimetableDeltaError(f"{label} must be nonblank.")
    if max_length is not None and len(result) > max_length:
        raise TimetableDeltaError(f"{label} cannot exceed {max_length} characters.")
    return result


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TimetableDeltaError(f"{label} must be a string.")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TimetableDeltaError(f"{label} must be a string SHA-256 digest.")
    digest = value
    if not _SHA256_RE.fullmatch(digest):
        raise TimetableDeltaError(f"{label} must be a lowercase SHA-256 digest.")
    return digest


def _integer_or_none(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TimetableDeltaError(f"{label} must be a non-negative integer or null.")
    return int(value)


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TimetableDeltaError(f"{label} must be a non-negative integer.")
    return int(value)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TimetableDeltaError(f"{label} must be a boolean.")
    return value


def _utc_timestamp(value: Any, label: str) -> str:
    raw = _text(value, label)
    if not raw.endswith("Z"):
        raise TimetableDeltaError(f"{label} must be an ISO-8601 UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise TimetableDeltaError(f"{label} must be a valid ISO-8601 UTC timestamp.") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise TimetableDeltaError(f"{label} must be a UTC timestamp.")
    return raw


def _validate_churn_metadata(
    value: Mapping[str, Any],
    label: str,
    *,
    reason: str,
) -> None:
    for field in ("added", "updated", "removed"):
        _nonnegative_integer(value.get(field), f"{label}.{field}")
    if _boolean(value.get("applied"), f"{label}.applied") is not False:
        raise TimetableDeltaError(f"{label}.applied must be false.")
    if value.get("reason") != reason:
        raise TimetableDeltaError(f"{label}.reason is not supported by this importer.")


def _validate_migration_metadata(value: Mapping[str, Any], label: str) -> None:
    _nonnegative_integer(value.get("applied_count"), f"{label}.applied_count")
    _sha256(value.get("applied_sha256"), f"{label}.applied_sha256")
    highest = _mapping(value.get("highest_name_by_app"), f"{label}.highest_name_by_app")
    for app, name in highest.items():
        _nonblank(app, f"{label}.highest_name_by_app key")
        _nonblank(name, f"{label}.highest_name_by_app[{app!r}]")


def _validate_database_fingerprint(value: Mapping[str, Any], label: str) -> None:
    _sha256(value.get("sha256"), f"{label}.sha256")
    _nonnegative_integer(value.get("size_bytes"), f"{label}.size_bytes")
    _utc_timestamp(value.get("modified_at_utc"), f"{label}.modified_at_utc")


def _validate_meeting_record_scalars(record: Mapping[str, Any], label: str) -> None:
    _meeting_key(record, label)
    _meeting_text(record, "building", label)
    _meeting_text(record, "floor_wing", label)


def _validate_artifact_semantics(document: Mapping[str, Any]) -> None:
    metadata = _mapping(document.get("metadata"), "metadata")
    exact_metadata = {
        "exporter_version": EXPORTER_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "generated_at_basis": "target_snapshot_mtime",
        "data_classification": "restricted_student_timetable",
        "integrity_note": "sha256_requires_an_operator_supplied_expected_digest",
    }
    for field, expected in exact_metadata.items():
        if metadata.get(field) != expected:
            raise TimetableDeltaError(f"metadata.{field} is not supported by this importer.")
    _utc_timestamp(metadata.get("generated_at_utc"), "metadata.generated_at_utc")
    contains_student_identifiers = _boolean(
        metadata.get("contains_student_identifiers"),
        "metadata.contains_student_identifiers",
    )
    _validate_churn_metadata(
        _mapping(metadata.get("observed_program_churn"), "metadata.observed_program_churn"),
        "metadata.observed_program_churn",
        reason="derived_from_final_registrar_links_and_production_student_programs",
    )
    _validate_churn_metadata(
        _mapping(
            metadata.get("excluded_import_program_churn"),
            "metadata.excluded_import_program_churn",
        ),
        "metadata.excluded_import_program_churn",
        reason="production_import_and_manual_memberships_are_authoritative",
    )
    for index, change in enumerate(metadata.get("excluded_non_registrar_changes", [])):
        label = f"metadata.excluded_non_registrar_changes[{index}]"
        _nonblank(change.get("academic_year"), f"{label}.academic_year")
        _nonblank(change.get("term"), f"{label}.term")
        _nonblank(change.get("source"), f"{label}.source")
        if change.get("operation") not in {"added", "removed"}:
            raise TimetableDeltaError(f"{label}.operation is not supported.")
        _nonnegative_integer(change.get("count"), f"{label}.count")
    for field, value in _mapping(
        metadata.get("excluded_global_state"), "metadata.excluded_global_state"
    ).items():
        _nonnegative_integer(value, f"metadata.excluded_global_state.{field}")
    excluded_students = _mapping(
        metadata.get("excluded_student_relationship_changes"),
        "metadata.excluded_student_relationship_changes",
    )
    for field in ("relationships_added", "relationships_removed"):
        _nonnegative_integer(
            excluded_students.get(field),
            f"metadata.excluded_student_relationship_changes.{field}",
        )
    if excluded_students.get("action") != "preserve_production_and_route_to_separate_roster_sync":
        raise TimetableDeltaError(
            "metadata.excluded_student_relationship_changes.action is not supported."
        )

    scope = _mapping(document.get("scope"), "scope")
    exact_scope = {
        "term_sections": "scenario_is_null_and_referenced_by_target_scope",
        "program_assignments": "assignment_source_import_only",
        "observed_program_assignments": "rebuild_from_final_links",
    }
    for field, expected in exact_scope.items():
        if scope.get(field) != expected:
            raise TimetableDeltaError(f"scope.{field} is not supported by this importer.")
    if scope.get("excluded_payloads") != _EXCLUDED_PAYLOADS:
        raise TimetableDeltaError("scope.excluded_payloads does not match the safe export scope.")

    base = _mapping(document.get("base"), "base")
    target = _mapping(document.get("target"), "target")
    _validate_database_fingerprint(_mapping(base.get("database"), "base.database"), "base.database")
    _validate_database_fingerprint(
        _mapping(target.get("database"), "target.database"), "target.database"
    )
    base_migrations = _mapping(base.get("migrations"), "base.migrations")
    target_migrations = _mapping(target.get("migrations"), "target.migrations")
    _validate_migration_metadata(base_migrations, "base.migrations")
    _validate_migration_metadata(target_migrations, "target.migrations")
    if base_migrations != target_migrations:
        raise TimetableDeltaError("Base and target migration metadata must match exactly.")
    for field in ("state_sha256", "scoped_state_sha256", "observed_basis_sha256"):
        _sha256(base.get(field), f"base.{field}")
    for field in ("source_state_sha256", "state_sha256"):
        _sha256(target.get(field), f"target.{field}")
    if base.get("state_program_scope") != "all_global_rows":
        raise TimetableDeltaError("base.state_program_scope is not supported.")
    if target.get("state_program_scope") != "import_only_with_observed_rebuilt_separately":
        raise TimetableDeltaError("target.state_program_scope is not supported.")

    sections = _mapping(document.get("sections"), "sections")
    for index, record in enumerate(sections.get("upserts", [])):
        label = f"sections.upserts[{index}]"
        _section_key(record, label)
        _nonblank(record.get("source_tag"), f"{label}.source_tag")
        _text(record.get("course_name"), f"{label}.course_name")
        _integer_or_none(record.get("available_capacity"), f"{label}.available_capacity")
        _integer_or_none(record.get("registered_count"), f"{label}.registered_count")
        _boolean(record.get("programs_complete"), f"{label}.programs_complete")
        _boolean(record.get("meetings_complete"), f"{label}.meetings_complete")
        if not isinstance(record.get("meeting_mode"), str):
            raise TimetableDeltaError(f"{label}.meeting_mode must be a string.")
        for program_index, program in enumerate(record.get("programs", [])):
            program_label = f"{label}.programs[{program_index}]"
            _program_code(program, program_label)
            _nonblank(program.get("assignment_source"), f"{program_label}.assignment_source")
        for meeting_index, meeting in enumerate(record.get("meetings", [])):
            full_meeting = {**section_key_dict(_section_key(record, label)), **meeting}
            _validate_meeting_record_scalars(
                full_meeting,
                f"{label}.meetings[{meeting_index}]",
            )

    programs = _mapping(document.get("programs"), "programs")
    for action in ("additions", "removals"):
        for index, record in enumerate(programs.get(action, [])):
            label = f"programs.{action}[{index}]"
            _program_key(record, label)
            _nonblank(record.get("assignment_source"), f"{label}.assignment_source")
    for index, update in enumerate(programs.get("updates", [])):
        for side in ("before", "after"):
            record = update[side]
            label = f"programs.updates[{index}].{side}"
            _program_key(record, label)
            _nonblank(record.get("assignment_source"), f"{label}.assignment_source")

    meetings = _mapping(document.get("meetings"), "meetings")
    for action in ("additions", "removals"):
        for index, record in enumerate(meetings.get(action, [])):
            _validate_meeting_record_scalars(record, f"meetings.{action}[{index}]")
    for index, update in enumerate(meetings.get("updates", [])):
        for side in ("before", "after"):
            _validate_meeting_record_scalars(
                update[side],
                f"meetings.updates[{index}].{side}",
            )

    touched_records = document["student_term_sections"]["touched_students"]
    if contains_student_identifiers != bool(touched_records):
        raise TimetableDeltaError(
            "metadata.contains_student_identifiers does not match the artifact payload."
        )
    for index, record in enumerate(touched_records):
        label = f"student_term_sections.touched_students[{index}]"
        student_id = record.get("student_id")
        if isinstance(student_id, bool) or not isinstance(student_id, int):
            raise TimetableDeltaError(f"{label}.student_id must be an integer.")
        _program_code({"program_code": record.get("expected_program")}, label)
        _nonblank(record.get("expected_section"), f"{label}.expected_section")
        _nonblank(record.get("expected_status"), f"{label}.expected_status")
        for field in ("base_sections", "target_sections"):
            for section_index, section_record in enumerate(record.get(field, [])):
                _section_key(section_record, f"{label}.{field}[{section_index}]")


def _section_key(record: Mapping[str, Any], label: str) -> SectionKey:
    try:
        course_code = _nonblank(record.get("course_code"), f"{label}.course_code")
        course_number = _nonblank(record.get("course_number"), f"{label}.course_number")
        section = _nonblank(record.get("section"), f"{label}.section")
        key = normalise_section_key(course_code, course_number, section)
        if key != (course_code, course_number, section):
            raise TimetableDeltaError("section natural key fields must be canonical")
        return key
    except TimetableDeltaError as exc:
        raise TimetableDeltaError(f"{label}: {exc}") from exc


def _meeting_text(record: Mapping[str, Any], field: str, label: str) -> str:
    return _text(record.get(field), f"{label}.{field}")


def _program_code(record: Mapping[str, Any], label: str) -> str:
    raw_program = _nonblank(record.get("program_code"), f"{label}.program_code")
    program = normalize_section_program(raw_program)
    if raw_program != program:
        raise TimetableDeltaError(
            f"{label}.program_code must use its canonical uppercase representation."
        )
    return program


def _meeting_key(record: Mapping[str, Any], label: str) -> MeetingKey:
    section_key = _section_key(record, label)
    day = _text(record.get("day"), f"{label}.day")
    if day not in _SUPPORTED_MEETING_DAYS:
        raise TimetableDeltaError(f"{label}.day must be a canonical SUN-THU code.")
    start_time = _valid_time(record.get("start_time"), f"{label}.start_time")
    end_time = _valid_time(record.get("end_time"), f"{label}.end_time")
    if start_time >= end_time:
        raise TimetableDeltaError(f"{label} start_time must be earlier than end_time.")
    return (
        section_key,
        day,
        start_time,
        end_time,
        _meeting_text(record, "room", label),
        _meeting_text(record, "instructor", label),
    )


def _valid_time(value: Any, label: str) -> str:
    raw = _text(value, label)
    match = re.fullmatch(r"(\d{2}):(\d{2})", raw)
    if not match:
        raise TimetableDeltaError(f"{label} must use HH:MM format.")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise TimetableDeltaError(f"{label} must be a valid time.")
    return raw


def _program_key(record: Mapping[str, Any], label: str) -> ProgramKey:
    section_key = _section_key(record, label)
    return section_key, _program_code(record, label)


def _student_link_key(record: Mapping[str, Any], label: str) -> StudentLinkKey:
    raw_student_id = record.get("student_id")
    if isinstance(raw_student_id, bool) or not isinstance(raw_student_id, int):
        raise TimetableDeltaError(f"{label}.student_id must be an integer.")
    return (
        raw_student_id,
        _nonblank(record.get("academic_year"), f"{label}.academic_year"),
        _nonblank(record.get("term"), f"{label}.term"),
        _section_key(record, label),
        _nonblank(record.get("source"), f"{label}.source"),
    )


def _index_unique(
    records: list[dict[str, Any]],
    key_builder: Any,
    *,
    label: str,
) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for index, record in enumerate(records):
        item_label = f"{label}[{index}]"
        key = key_builder(record, item_label)
        if key in result:
            raise TimetableDeltaError(f"{label} contains duplicate natural key {key!r}.")
        result[key] = record
    return result


def _validate_artifact_shape(document: dict[str, Any]) -> None:
    _validate_strict_artifact_schema(document)
    _validate_artifact_semantics(document)
    if document.get("schema_version") != DELTA_SCHEMA_VERSION:
        raise TimetableDeltaError(
            f"Unsupported delta schema version {document.get('schema_version')!r}."
        )
    base = _mapping(document.get("base"), "base")
    _sha256(base.get("state_sha256"), "base.state_sha256")
    target = _mapping(document.get("target"), "target")
    _sha256(target.get("state_sha256"), "target.state_sha256")

    sections = _mapping(document.get("sections"), "sections")
    upserts = _record_list(sections.get("upserts"), "sections.upserts")
    removals = _record_list(sections.get("removals"), "sections.removals")
    if removals:
        raise TimetableDeltaError("Section removals are forbidden; the artifact must contain none.")
    section_index = _index_unique(upserts, _section_key, label="sections.upserts")
    database_keys: set[tuple[str, str]] = set()
    for key, record in section_index.items():
        database_key = (derived_course_key(key[0], key[1]), key[2])
        if database_key in database_keys:
            raise TimetableDeltaError(
                "Artifact split section identities collapse to one course_key/section: "
                f"{database_key!r}."
            )
        database_keys.add(database_key)
        _nonblank(record.get("source_tag"), f"section {key!r}.source_tag")
        _integer_or_none(record.get("available_capacity"), "available_capacity")
        _integer_or_none(record.get("registered_count"), "registered_count")
        if "source_file" in record or "created_at" in record or "updated_at" in record:
            raise TimetableDeltaError(
                "Section upserts must not transport source_file or local timestamps."
            )
        meeting_mode = record.get("meeting_mode")
        if meeting_mode not in {"replace", "preserve"}:
            raise TimetableDeltaError(
                f"Section {key!r} meeting_mode must be 'replace' or 'preserve'."
            )
        if meeting_mode == "replace" and record.get("meetings_complete") is not True:
            raise TimetableDeltaError(
                f"Section {key!r} replacement must declare meetings_complete=true."
            )
        if meeting_mode == "preserve" and record.get("meetings_complete") is not False:
            raise TimetableDeltaError(
                f"Section {key!r} preserve mode must declare meetings_complete=false."
            )
        if record.get("programs_complete") is not True:
            raise TimetableDeltaError(f"Section {key!r} must declare programs_complete=true.")
        meetings = _record_list(record.get("meetings"), f"section {key!r}.meetings")
        programs = _record_list(record.get("programs"), f"section {key!r}.programs")
        _index_unique(
            [{**section_key_dict(key), **item} for item in meetings],
            _meeting_key,
            label=f"section {key!r}.meetings",
        )
        indexed_programs = _index_unique(
            [{**section_key_dict(key), **item} for item in programs],
            _program_key,
            label=f"section {key!r}.programs",
        )
        for program_key, program in indexed_programs.items():
            source = _text(
                program.get("assignment_source"),
                f"program {program_key!r}.assignment_source",
            )
            if source != "import":
                raise TimetableDeltaError(
                    "Artifacts may mutate only assignment_source='import'; "
                    "observed memberships are rebuilt from production registrations."
                )

    _validate_explicit_operations(document, section_index)
    _validate_student_operations(document)
    _validate_expected_operations(document)
    _validate_release_scope(document)


def _validate_explicit_operations(
    document: dict[str, Any],
    section_index: Mapping[SectionKey, dict[str, Any]],
) -> None:
    programs = _mapping(document.get("programs"), "programs")
    if any(programs.get(action) for action in ("additions", "updates", "removals")):
        raise TimetableDeltaError(
            "This registrar delta must preserve import/manual memberships; "
            "explicit program operations are forbidden."
        )
    for action in ("additions", "removals"):
        records = _record_list(programs.get(action), f"programs.{action}")
        indexed = _index_unique(records, _program_key, label=f"programs.{action}")
        for key, record in indexed.items():
            if key[0] not in section_index:
                raise TimetableDeltaError(
                    f"Program {action[:-1]} targets no section upsert: {key!r}."
                )
            if record.get("assignment_source") != "import":
                raise TimetableDeltaError(
                    "Explicit program operations may target only assignment_source='import'."
                )
    updates = _record_list(programs.get("updates"), "programs.updates")
    for index, update in enumerate(updates):
        before = _mapping(update.get("before"), f"programs.updates[{index}].before")
        after = _mapping(update.get("after"), f"programs.updates[{index}].after")
        if _program_key(before, "program update before") != _program_key(
            after, "program update after"
        ):
            raise TimetableDeltaError("Program updates cannot change their natural key.")
        if (
            before.get("assignment_source") != "import"
            or after.get("assignment_source") != "import"
        ):
            raise TimetableDeltaError("Program updates may target only import provenance.")

    meetings = _mapping(document.get("meetings"), "meetings")
    for action in ("additions", "removals"):
        records = _record_list(meetings.get(action), f"meetings.{action}")
        indexed = _index_unique(records, _meeting_key, label=f"meetings.{action}")
        for key in indexed:
            if key[0] not in section_index:
                raise TimetableDeltaError(
                    f"Meeting {action[:-1]} targets no section upsert: {key!r}."
                )
    updates = _record_list(meetings.get("updates"), "meetings.updates")
    for index, update in enumerate(updates):
        before = _mapping(update.get("before"), f"meetings.updates[{index}].before")
        after = _mapping(update.get("after"), f"meetings.updates[{index}].after")
        if _meeting_key(before, "meeting update before") != _meeting_key(
            after, "meeting update after"
        ):
            raise TimetableDeltaError("Meeting updates cannot change their natural key.")


def _validate_student_operations(document: dict[str, Any]) -> None:
    changes = _mapping(document.get("student_term_sections"), "student_term_sections")
    _record_list(changes.get("touched_students"), "student_term_sections.touched_students")
    unexpected = set(changes) - {"touched_students"}
    if unexpected:
        raise TimetableDeltaError(
            "Student changes must use complete touched-student snapshots, not row-level ops."
        )


def _validate_expected_operations(document: dict[str, Any]) -> None:
    expected = _mapping(document.get("expected_operations"), "expected_operations")
    if set(expected) != set(EXPECTED_OPERATION_KEYS):
        raise TimetableDeltaError(
            "expected_operations must contain exactly: " + ", ".join(EXPECTED_OPERATION_KEYS)
        )
    for key in EXPECTED_OPERATION_KEYS:
        value = expected[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TimetableDeltaError(f"expected_operations.{key} must be non-negative integer.")


def capture_current_timetable_state() -> dict[str, Any]:
    """Capture the same portable state that the frozen-snapshot exporter hashes."""

    section_rows = list(
        TermSection.objects.filter(scenario__isnull=True).values(
            "id",
            "source_tag",
            "course_name",
            "available_capacity",
            "registered_count",
            "course_code",
            "course_number",
            "course_key",
            "section",
        )
    )
    key_by_id: dict[int, SectionKey] = {}
    sections: list[dict[str, Any]] = []
    seen_sections: set[SectionKey] = set()
    seen_database_keys: set[tuple[str, str]] = set()
    for section_row in section_rows:
        section_key = normalise_section_key(
            section_row["course_code"],
            section_row["course_number"],
            section_row["section"],
        )
        expected_course_key = derived_course_key(section_key[0], section_key[1])
        stored_course_key = str(section_row.get("course_key") or "").replace(" ", "").upper()
        if stored_course_key != expected_course_key:
            raise TimetableDeltaError(
                "A production global section has a non-canonical course_key: "
                f"{stored_course_key!r} != {expected_course_key!r}."
            )
        if section_key in seen_sections:
            raise TimetableDeltaError(f"Production global section key collides: {section_key!r}.")
        seen_sections.add(section_key)
        database_key = (expected_course_key, section_key[2])
        if database_key in seen_database_keys:
            raise TimetableDeltaError(
                "Production split section identities collapse to one course_key/section: "
                f"{database_key!r}."
            )
        seen_database_keys.add(database_key)
        key_by_id[int(section_row["id"])] = section_key
        sections.append(
            {
                **section_key_dict(section_key),
                "source_tag": str(section_row.get("source_tag") or ""),
                "course_name": str(section_row.get("course_name") or ""),
                "available_capacity": section_row.get("available_capacity"),
                "registered_count": section_row.get("registered_count"),
            }
        )

    programs: list[dict[str, Any]] = []
    seen_programs: set[ProgramKey] = set()
    for program_row in TermSectionProgram.objects.filter(term_section_id__in=key_by_id).values(
        "term_section_id", "program", "assignment_source"
    ):
        section_key = key_by_id[int(program_row["term_section_id"])]
        program = normalize_section_program(program_row.get("program"))
        program_key = (section_key, program)
        if not program:
            raise TimetableDeltaError(
                f"Production has blank program membership on {section_key!r}."
            )
        if program_key in seen_programs:
            raise TimetableDeltaError(f"Production program key collides: {program_key!r}.")
        seen_programs.add(program_key)
        programs.append(
            {
                **section_key_dict(section_key),
                "program_code": program,
                "assignment_source": str(program_row.get("assignment_source") or ""),
            }
        )

    meetings: list[dict[str, Any]] = []
    seen_meetings: set[MeetingKey] = set()
    for meeting_row in TermSectionMeeting.objects.filter(term_section_id__in=key_by_id).values(
        "term_section_id",
        "day",
        "start_time",
        "end_time",
        "building",
        "floor_wing",
        "room",
        "instructor",
    ):
        section_key = key_by_id[int(meeting_row["term_section_id"])]
        meeting_record: dict[str, Any] = {
            **section_key_dict(section_key),
            "day": str(meeting_row.get("day") or ""),
            "start_time": str(meeting_row.get("start_time") or ""),
            "end_time": str(meeting_row.get("end_time") or ""),
            "building": str(meeting_row.get("building") or ""),
            "floor_wing": str(meeting_row.get("floor_wing") or ""),
            "room": str(meeting_row.get("room") or ""),
            "instructor": str(meeting_row.get("instructor") or ""),
        }
        meeting_key = _meeting_key(meeting_record, "production meeting")
        if meeting_key in seen_meetings:
            raise TimetableDeltaError(f"Production meeting key collides: {meeting_key!r}.")
        seen_meetings.add(meeting_key)
        meetings.append(meeting_record)

    student_links: list[dict[str, Any]] = []
    seen_links: set[StudentLinkKey] = set()
    for link_row in StudentTermSection.objects.filter(term_section_id__in=key_by_id).values(
        "student_id",
        "academic_year",
        "term",
        "term_section_id",
        "source",
    ):
        section_key = key_by_id[int(link_row["term_section_id"])]
        link_record: dict[str, Any] = {
            "student_id": int(link_row["student_id"]),
            "academic_year": str(link_row.get("academic_year") or ""),
            "term": str(link_row.get("term") or ""),
            **section_key_dict(section_key),
            "source": str(link_row.get("source") or ""),
        }
        link_key = _student_link_key(link_record, "production student relationship")
        if link_key in seen_links:
            raise TimetableDeltaError("Production student relationship natural keys collide.")
        seen_links.add(link_key)
        student_links.append(link_record)

    return canonical_state_document(
        sections=sections,
        programs=programs,
        meetings=meetings,
        student_term_sections=student_links,
    )


def observed_basis_sha256() -> tuple[str, dict[str, int]]:
    """Hash programs that can influence rebuilding observed memberships."""

    referenced_ids = set(
        StudentTermSection.objects.filter(term_section__scenario__isnull=True).values_list(
            "student_id", flat=True
        )
    )
    found = {
        int(student_id): normalize_section_program(program)
        for student_id, program in Student.objects.filter(
            student_id__in=referenced_ids
        ).values_list("student_id", "program")
    }
    rows = [
        {"student_id": student_id, "program": found.get(student_id, "")}
        for student_id in sorted(referenced_ids)
    ]
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest(), {
        "students": len(rows),
        "missing_programs": sum(not row["program"] for row in rows),
    }


def _artifact_touched_student_ids(document: Mapping[str, Any]) -> tuple[int, ...]:
    result: set[int] = set()
    for record in document["student_term_sections"]["touched_students"]:
        student_id = record.get("student_id")
        if isinstance(student_id, bool) or not isinstance(student_id, int):
            raise TimetableDeltaError("Touched-student identifiers must be integers.")
        result.add(student_id)
    return tuple(sorted(result))


def _lock_current_timetable_state(touched_student_ids: Iterable[int]) -> None:
    section_ids = list(
        TermSection.objects.select_for_update()
        .filter(scenario__isnull=True)
        .order_by("id")
        .values_list("id", flat=True)
    )
    list(
        TermSectionProgram.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    list(
        TermSectionMeeting.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    locked_registration_rows = list(
        StudentTermSection.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list("id", "student_id")
    )
    referenced_student_ids = {
        int(student_id) for _registration_id, student_id in locked_registration_rows
    }
    student_ids = sorted(referenced_student_ids | set(touched_student_ids))
    list(
        Student.objects.select_for_update()
        .filter(student_id__in=student_ids)
        .order_by("student_id")
        .values_list("student_id", flat=True)
    )
    list(
        SectionPlacement.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    list(
        BoardSectionVisibility.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )


def _current_migration_metadata() -> dict[str, Any]:
    rows = list(
        MigrationRecorder.Migration.objects.using(connection.alias)
        .select_for_update()
        .order_by("app", "name")
        .values_list("app", "name")
    )
    applied = [{"app": str(app), "name": str(name)} for app, name in rows]
    highest_name_by_app: dict[str, str] = {}
    for record in applied:
        highest_name_by_app[record["app"]] = record["name"]
    return {
        "applied_count": len(applied),
        "applied_sha256": hashlib.sha256(canonical_json_bytes(applied)).hexdigest(),
        "highest_name_by_app": highest_name_by_app,
    }


def _assert_current_migration_state(document: Mapping[str, Any]) -> None:
    current = _current_migration_metadata()
    if current != document["base"]["migrations"]:
        raise TimetableDeltaError(
            "Production migration metadata does not match the frozen snapshots."
        )


def _take_database_import_lock() -> None:
    if connection.vendor != "postgresql":
        return
    table_models = (
        TermSection,
        TermSectionProgram,
        TermSectionMeeting,
        StudentTermSection,
        Student,
        SectionPlacement,
        BoardSectionVisibility,
        DeliveryBoard,
        MigrationRecorder.Migration,
    )
    table_names = sorted({model._meta.db_table for model in table_models})
    quoted_tables = ", ".join(connection.ops.quote_name(name) for name in table_names)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                [_POSTGRES_ADVISORY_LOCK_KEY],
            )
            acquired = bool(cursor.fetchone()[0])
            if not acquired:
                raise TimetableDeltaError(
                    "Another database timetable import owns the transaction lock."
                )
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, true)",
                [_POSTGRES_LOCK_TIMEOUT],
            )
            # Identifiers come only from installed Django model metadata and are
            # backend-quoted above; no artifact or operator text enters this SQL.
            lock_sql = (  # nosec B608
                f"LOCK TABLE {quoted_tables} IN SHARE ROW EXCLUSIVE MODE"
            )
            cursor.execute(lock_sql)
    except DatabaseError as exc:
        raise TimetableDeltaError(
            "Could not acquire the bounded PostgreSQL timetable table locks."
        ) from exc


def _sorted_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(record) for record in records), key=canonical_json_bytes)


def _artifact_section_index(document: Mapping[str, Any]) -> dict[SectionKey, dict[str, Any]]:
    upserts = document["sections"]["upserts"]
    return {_section_key(record, "section upsert"): record for record in upserts}


def _current_section_index() -> dict[SectionKey, TermSection]:
    result: dict[SectionKey, TermSection] = {}
    database_keys: set[tuple[str, str]] = set()
    for section in TermSection.objects.filter(scenario__isnull=True):
        key = normalise_section_key(section.course_code, section.course_number, section.section)
        if derived_course_key(key[0], key[1]) != str(section.course_key).replace(" ", "").upper():
            raise TimetableDeltaError(f"Production section has mismatched course_key: {key!r}.")
        if key in result:
            raise TimetableDeltaError(f"Production section key collides: {key!r}.")
        database_key = (derived_course_key(key[0], key[1]), key[2])
        if database_key in database_keys:
            raise TimetableDeltaError(
                f"Production split section identities collapse: {database_key!r}."
            )
        database_keys.add(database_key)
        result[key] = section
    return result


def _meeting_record(section_key: SectionKey, meeting: TermSectionMeeting) -> dict[str, Any]:
    return {
        **section_key_dict(section_key),
        "day": str(meeting.day or ""),
        "start_time": str(meeting.start_time or ""),
        "end_time": str(meeting.end_time or ""),
        "building": str(meeting.building or ""),
        "floor_wing": str(meeting.floor_wing or ""),
        "room": str(meeting.room or ""),
        "instructor": str(meeting.instructor or ""),
    }


def _nested_meeting_record(section_key: SectionKey, record: Mapping[str, Any]) -> dict[str, Any]:
    full = {
        **section_key_dict(section_key),
        "day": _nonblank(record.get("day"), "meeting.day"),
        "start_time": _valid_time(record.get("start_time"), "meeting.start_time"),
        "end_time": _valid_time(record.get("end_time"), "meeting.end_time"),
        "building": _meeting_text(record, "building", "meeting"),
        "floor_wing": _meeting_text(record, "floor_wing", "meeting"),
        "room": _meeting_text(record, "room", "meeting"),
        "instructor": _meeting_text(record, "instructor", "meeting"),
    }
    if full["start_time"] >= full["end_time"]:
        raise TimetableDeltaError(f"Meeting start must precede end for {section_key!r}.")
    return full


def _expected_meeting_operations(
    section_index: Mapping[SectionKey, dict[str, Any]],
    current_sections: Mapping[SectionKey, TermSection],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    additions: list[dict[str, Any]] = []
    removals: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for section_key, target_section in section_index.items():
        if target_section.get("meeting_mode") == "preserve":
            if target_section.get("meetings"):
                raise TimetableDeltaError(
                    f"Preserve-mode section {section_key!r} cannot carry meeting rows."
                )
            continue
        target_records = [
            _nested_meeting_record(section_key, row) for row in target_section["meetings"]
        ]
        target_by_key = {
            _meeting_key(record, "target meeting"): record for record in target_records
        }
        current = current_sections.get(section_key)
        current_records = (
            [_meeting_record(section_key, meeting) for meeting in current.meetings.all()]
            if current is not None
            else []
        )
        current_by_key = {
            _meeting_key(record, "current meeting"): record for record in current_records
        }
        additions.extend(target_by_key[key] for key in set(target_by_key) - set(current_by_key))
        removals.extend(current_by_key[key] for key in set(current_by_key) - set(target_by_key))
        updates.extend(
            {"before": current_by_key[key], "after": target_by_key[key]}
            for key in set(current_by_key).intersection(target_by_key)
            if current_by_key[key] != target_by_key[key]
        )
    return _sorted_records(additions), _sorted_records(updates), _sorted_records(removals)


def _touched_student_targets(
    document: Mapping[str, Any],
) -> dict[int, TouchedStudent]:
    changes = document["student_term_sections"]
    records = _record_list(changes.get("touched_students"), "touched_students")
    result: dict[int, TouchedStudent] = {}
    for index, record in enumerate(records):
        label = f"student_term_sections.touched_students[{index}]"
        student_id = record.get("student_id")
        if isinstance(student_id, bool) or not isinstance(student_id, int):
            raise TimetableDeltaError(f"{label}.student_id must be an integer.")
        if student_id in result:
            raise TimetableDeltaError("A touched-student record is duplicated.")
        expected_program = _program_code(
            {"program_code": record.get("expected_program")},
            label,
        )
        sets: dict[str, set[SectionKey]] = {}
        for field in ("base_sections", "target_sections"):
            rows = _record_list(record.get(field), f"{label}.{field}")
            keys: set[SectionKey] = set()
            for target_index, target in enumerate(rows):
                key = _section_key(target, f"{label}.{field}[{target_index}]")
                if key in keys:
                    raise TimetableDeltaError(
                        f"A touched-student record has a duplicate {field} section key."
                    )
                keys.add(key)
            sets[field] = keys
        if sets["base_sections"] == sets["target_sections"]:
            raise TimetableDeltaError("A touched-student record has no registrar snapshot change.")
        result[student_id] = TouchedStudent(
            expected_program=expected_program,
            expected_section=_nonblank(
                record.get("expected_section"), f"{label}.expected_section"
            ).upper(),
            expected_status=_nonblank(
                record.get("expected_status"), f"{label}.expected_status"
            ).upper(),
            base_sections=frozenset(sets["base_sections"]),
            target_sections=frozenset(sets["target_sections"]),
        )
    return result


def _current_release_links(
    current_sections: Mapping[SectionKey, TermSection],
) -> dict[int, set[SectionKey]]:
    key_by_id = {int(section.id): key for key, section in current_sections.items()}
    result: dict[int, set[SectionKey]] = {}
    for student_id, section_id in StudentTermSection.objects.filter(
        academic_year=_RELEASE_ACADEMIC_YEAR,
        term=_RELEASE_TERM,
        source=_RELEASE_SOURCE,
        term_section_id__in=key_by_id,
    ).values_list("student_id", "term_section_id"):
        result.setdefault(int(student_id), set()).add(key_by_id[int(section_id)])
    return result


def _capture_release_scoped_state(
    *,
    current_state: Mapping[str, Any],
    touched: Mapping[int, TouchedStudent],
) -> dict[str, Any]:
    release_links = [
        record
        for record in current_state["student_term_sections"]
        if (
            record["academic_year"],
            record["term"],
            record["source"],
        )
        == _RELEASE_SCOPE
    ]
    relevant_keys = {_section_key(record, "release relationship") for record in release_links}
    for target in touched.values():
        relevant_keys.update(target.base_sections)
        relevant_keys.update(target.target_sections)
    return canonical_state_document(
        sections=(
            record
            for record in current_state["sections"]
            if _section_key(record, "scoped section") in relevant_keys
        ),
        programs=(
            record
            for record in current_state["programs"]
            if _section_key(record, "scoped program") in relevant_keys
            and record.get("assignment_source") == "import"
        ),
        meetings=(
            record
            for record in current_state["meetings"]
            if _section_key(record, "scoped meeting") in relevant_keys
        ),
        student_term_sections=release_links,
    )


def _student_record(student_id: int, section_key: SectionKey) -> dict[str, Any]:
    return {
        "student_id": student_id,
        "academic_year": _RELEASE_ACADEMIC_YEAR,
        "term": _RELEASE_TERM,
        **section_key_dict(section_key),
        "source": _RELEASE_SOURCE,
    }


def _expected_student_operations(
    touched: Mapping[int, TouchedStudent],
    current_links: Mapping[int, set[SectionKey]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    additions: list[dict[str, Any]] = []
    removals: list[dict[str, Any]] = []
    for student_id, target in touched.items():
        current_keys = current_links.get(student_id, set())
        additions.extend(
            _student_record(student_id, key) for key in target.target_sections - current_keys
        )
        removals.extend(
            _student_record(student_id, key) for key in current_keys - target.target_sections
        )
    return _sorted_records(additions), _sorted_records(removals)


def _assert_exact_records(
    actual: Iterable[Mapping[str, Any]],
    expected: Iterable[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    actual_sorted = _sorted_records(actual)
    expected_sorted = _sorted_records(expected)
    if actual_sorted != expected_sorted:
        raise TimetableDeltaError(
            f"Artifact {label} do not exactly match the locked production base-to-target diff."
        )


def _validate_touched_students(
    touched: Mapping[int, TouchedStudent],
) -> dict[int, str]:
    students = {
        int(student_id): (
            normalize_section_program(program),
            str(section or "").strip().upper(),
            str(status or "").strip().upper(),
        )
        for student_id, program, section, status in Student.objects.filter(
            student_id__in=touched
        ).values_list("student_id", "program", "section", "status")
    }
    unknown = sorted(set(touched) - set(students))
    if unknown:
        raise TimetableDeltaError(
            f"Artifact references {len(unknown)} unknown production student record(s)."
        )
    mismatched = sorted(
        student_id
        for student_id, target in touched.items()
        if students.get(student_id)
        != (target.expected_program, target.expected_section, target.expected_status)
    )
    if mismatched:
        raise TimetableDeltaError(
            "Touched-student programme/cohort/status does not match production for "
            f"{len(mismatched)} record(s)."
        )
    return {student_id: values[0] for student_id, values in students.items()}


def _validate_release_scope(document: Mapping[str, Any]) -> None:
    declared = _mapping(
        document["scope"].get("student_term_sections"), "scope.student_term_sections"
    )
    scope = (
        _text(declared.get("academic_year"), "scope.student_term_sections.academic_year"),
        _text(declared.get("term"), "scope.student_term_sections.term"),
        _text(declared.get("source"), "scope.student_term_sections.source"),
    )
    if scope != _RELEASE_SCOPE:
        raise TimetableDeltaError(
            "This importer accepts only scraper_timetable academic year 1448 term 1."
        )
    if declared.get("mode") != "replace_complete_set_for_touched_students":
        raise TimetableDeltaError("Registrar scope must use complete touched-student replacement.")
    if declared.get("untouched_students") != "preserve":
        raise TimetableDeltaError("Registrar scope must preserve untouched students.")


def _expected_operations(
    *,
    section_index: Mapping[SectionKey, dict[str, Any]],
    current_sections: Mapping[SectionKey, TermSection],
    meeting_additions: list[dict[str, Any]],
    meeting_updates: list[dict[str, Any]],
    meeting_removals: list[dict[str, Any]],
    student_additions: list[dict[str, Any]],
    student_removals: list[dict[str, Any]],
    students_replaced: int,
) -> dict[str, int]:
    created = len(set(section_index) - set(current_sections))
    return {
        "sections_created": created,
        "sections_updated": len(section_index) - created,
        "section_upserts": len(section_index),
        "programs_added": 0,
        "programs_updated": 0,
        "programs_removed": 0,
        "meetings_added": len(meeting_additions),
        "meetings_updated": len(meeting_updates),
        "meetings_removed": len(meeting_removals),
        "students_replaced": students_replaced,
        "student_term_sections_added": len(student_additions),
        "student_term_sections_removed": len(student_removals),
    }


def _predict_observed_programs(
    *,
    current_sections: Mapping[SectionKey, TermSection],
    touched: Mapping[int, TouchedStudent],
    current_release_links: Mapping[int, set[SectionKey]],
) -> tuple[dict[SectionKey, set[str]], int, int]:
    affected_keys = (
        set().union(
            *(
                current_release_links.get(student_id, set()) | target.target_sections
                for student_id, target in touched.items()
            )
        )
        if touched
        else set()
    )
    affected_existing = {
        key: current_sections[key] for key in affected_keys if key in current_sections
    }
    section_key_by_id = {int(section.id): key for key, section in affected_existing.items()}
    registrations: dict[SectionKey, set[int]] = {key: set() for key in affected_keys}
    for section_id, student_id in StudentTermSection.objects.filter(
        term_section_id__in=section_key_by_id
    ).values_list("term_section_id", "student_id"):
        registrations[section_key_by_id[int(section_id)]].add(int(student_id))
    for student_id, target in touched.items():
        for key in current_release_links.get(student_id, set()):
            registrations.setdefault(key, set()).discard(student_id)
        for key in target.target_sections:
            registrations.setdefault(key, set()).add(student_id)
    registered_ids = set().union(*registrations.values()) if registrations else set()
    program_by_student = {
        int(student_id): normalize_section_program(program)
        for student_id, program in Student.objects.filter(
            student_id__in=registered_ids
        ).values_list("student_id", "program")
    }
    wanted = {
        key: {
            program_by_student[student_id]
            for student_id in student_ids
            if program_by_student.get(student_id)
        }
        for key, student_ids in registrations.items()
    }
    non_observed: dict[SectionKey, set[str]] = {key: set() for key in affected_keys}
    for section_id, program in (
        TermSectionProgram.objects.filter(term_section_id__in=section_key_by_id)
        .exclude(assignment_source="observed")
        .values_list("term_section_id", "program")
    ):
        non_observed[section_key_by_id[int(section_id)]].add(normalize_section_program(program))
    desired = {key: wanted.get(key, set()) - non_observed.get(key, set()) for key in affected_keys}
    current_observed: dict[SectionKey, set[str]] = {key: set() for key in affected_keys}
    for section_key, program in TermSectionProgram.objects.filter(
        term_section_id__in=section_key_by_id,
        assignment_source="observed",
    ).values_list("term_section_id", "program"):
        # The first selected value is an id despite the variable's descriptive name.
        key = section_key_by_id[int(section_key)]
        current_observed.setdefault(key, set()).add(normalize_section_program(program))
    added = sum(
        len(desired.get(key, set()) - current_observed.get(key, set())) for key in affected_keys
    )
    removed = sum(
        len(current_observed.get(key, set()) - desired.get(key, set())) for key in affected_keys
    )
    return desired, added, removed


def _assert_board_eligibility(
    *,
    current_sections: Mapping[SectionKey, TermSection],
    desired_observed: Mapping[SectionKey, set[str]],
) -> None:
    affected = set(desired_observed)
    section_by_id = {
        int(section.id): key for key, section in current_sections.items() if key in affected
    }
    non_observed: dict[SectionKey, set[str]] = {key: set() for key in affected}
    for section_id, program in (
        TermSectionProgram.objects.filter(term_section_id__in=section_by_id)
        .exclude(assignment_source="observed")
        .values_list("term_section_id", "program")
    ):
        non_observed[section_by_id[int(section_id)]].add(normalize_section_program(program))
    final_programs = {
        key: non_observed.get(key, set()) | desired_observed.get(key, set()) for key in affected
    }
    placements = SectionPlacement.objects.filter(term_section_id__in=section_by_id).select_related(
        "board"
    )
    visibility = BoardSectionVisibility.objects.filter(
        term_section_id__in=section_by_id
    ).select_related("board")

    def assert_reference(reference: Any) -> None:
        key = section_by_id[int(reference.term_section_id)]
        board_program = normalize_section_program(reference.board.program)
        if board_program and board_program not in final_programs.get(key, set()):
            raise TimetableDeltaError(
                "Observed-membership reconciliation would make a placed/visible "
                f"section ineligible for board programme {board_program}: {key!r}."
            )

    for placement in placements:
        assert_reference(placement)
    for visible_section in visibility:
        assert_reference(visible_section)


def _assert_placed_meetings_preserved(
    *,
    section_index: Mapping[SectionKey, dict[str, Any]],
    current_sections: Mapping[SectionKey, TermSection],
) -> None:
    target_slots_by_id: dict[int, set[tuple[str, str, str, str]]] = {}
    for section_key, target in section_index.items():
        section = current_sections.get(section_key)
        if section is None or target.get("meeting_mode") != "replace":
            continue
        target_slots_by_id[int(section.id)] = {
            (
                meeting["day"],
                meeting["start_time"],
                meeting["end_time"],
                meeting["room"],
            )
            for meeting in (_nested_meeting_record(section_key, row) for row in target["meetings"])
        }
    if not target_slots_by_id:
        return
    stale_count = 0
    for placement in SectionPlacement.objects.filter(term_section_id__in=target_slots_by_id).only(
        "term_section_id", "day", "start_time", "end_time", "room"
    ):
        slot = (
            str(placement.day or ""),
            str(placement.start_time or ""),
            str(placement.end_time or ""),
            str(placement.room or ""),
        )
        if slot not in target_slots_by_id[int(placement.term_section_id)]:
            stale_count += 1
    if stale_count:
        raise TimetableDeltaError(
            f"Meeting replacement would invalidate {stale_count} existing section placement(s)."
        )


def _validate_section_scope(
    *,
    section_index: Mapping[SectionKey, dict[str, Any]],
    current_sections: Mapping[SectionKey, TermSection],
    current_links: Mapping[int, set[SectionKey]],
    touched: Mapping[int, TouchedStudent],
) -> None:
    available = set(current_sections) | set(section_index)
    target_touched_keys = (
        set().union(*(target.target_sections for target in touched.values())) if touched else set()
    )
    unresolved = sorted(target_touched_keys - available)
    if unresolved:
        raise TimetableDeltaError(
            f"Touched student target references unknown section(s): {unresolved[:10]!r}."
        )
    final_scope_keys = set().union(*current_links.values()) if current_links else set()
    for student_id, target in touched.items():
        final_scope_keys.difference_update(current_links.get(student_id, set()))
        final_scope_keys.update(target.target_sections)
    unreferenced_existing = sorted(
        key for key in section_index if key in current_sections and key not in final_scope_keys
    )
    if unreferenced_existing:
        raise TimetableDeltaError(
            "Existing section upserts must be referenced by the final production "
            f"scraper scope: {unreferenced_existing[:10]!r}."
        )
    orphan_new = sorted(
        key
        for key in section_index
        if key not in current_sections and key not in target_touched_keys
    )
    if orphan_new:
        raise TimetableDeltaError(
            "New sections require an explicit touched-student target reference: "
            f"{orphan_new[:10]!r}."
        )


def _assert_import_program_targets(
    *,
    section_index: Mapping[SectionKey, dict[str, Any]],
    current_sections: Mapping[SectionKey, TermSection],
) -> None:
    mismatched = 0
    for section_key, target_section in section_index.items():
        target_programs = {
            (
                _program_code(row, "section target program"),
                _nonblank(
                    row.get("assignment_source"),
                    "section target program.assignment_source",
                ),
            )
            for row in target_section["programs"]
        }
        current_section = current_sections.get(section_key)
        current_programs = (
            set(
                current_section.program_links.filter(assignment_source="import").values_list(
                    "program", "assignment_source"
                )
            )
            if current_section is not None
            else set()
        )
        if target_programs != current_programs:
            mismatched += 1
    if mismatched:
        raise TimetableDeltaError(
            "Registrar delta target differs from production import/manual programme "
            f"memberships for {mismatched} section(s)."
        )


def _assert_no_scenario_scope_rows(touched: Mapping[int, TouchedStudent]) -> None:
    scenario_scope_rows = StudentTermSection.objects.filter(
        student_id__in=touched,
        academic_year=_RELEASE_ACADEMIC_YEAR,
        term=_RELEASE_TERM,
        source=_RELEASE_SOURCE,
        term_section__scenario__isnull=False,
    ).count()
    if scenario_scope_rows:
        raise TimetableDeltaError(
            "Touched students have scraper rows on scenario-owned sections; "
            "global natural-key replacement is unsafe."
        )


def _section_target_metadata_matches(
    section: TermSection,
    record: Mapping[str, Any],
) -> bool:
    return (
        str(section.source_tag or ""),
        str(section.course_name or ""),
        section.available_capacity,
        section.registered_count,
    ) == (
        _nonblank(record.get("source_tag"), "section.source_tag"),
        _text(record.get("course_name"), "section.course_name"),
        record.get("available_capacity"),
        record.get("registered_count"),
    )


def _verify_already_applied_target(
    loaded: LoadedDelta,
    *,
    current_state: Mapping[str, Any],
) -> str:
    """Prove the stale-base state is exactly the intended target without writing."""

    document = loaded.document
    section_index = _artifact_section_index(document)
    current_sections = _current_section_index()
    touched = _touched_student_targets(document)
    _validate_touched_students(touched)
    _assert_no_scenario_scope_rows(touched)
    current_links = _current_release_links(current_sections)
    stale_touched = sum(
        current_links.get(student_id, set()) != set(target.target_sections)
        for student_id, target in touched.items()
    )
    if stale_touched:
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its target; touched-student "
            f"targets differ for {stale_touched} record(s)."
        )
    _validate_section_scope(
        section_index=section_index,
        current_sections=current_sections,
        current_links=current_links,
        touched=touched,
    )
    missing_sections = sum(key not in current_sections for key in section_index)
    metadata_mismatches = sum(
        key in current_sections
        and not _section_target_metadata_matches(current_sections[key], record)
        for key, record in section_index.items()
    )
    if missing_sections or metadata_mismatches:
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its target; section targets differ."
        )
    _assert_import_program_targets(
        section_index=section_index,
        current_sections=current_sections,
    )
    meeting_ops = _expected_meeting_operations(section_index, current_sections)
    if any(meeting_ops):
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its target; pending meeting "
            "operations remain."
        )
    student_ops = _expected_student_operations(touched, current_links)
    if any(student_ops):
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its target; pending registrar "
            "relationship operations remain."
        )
    _assert_placed_meetings_preserved(
        section_index=section_index,
        current_sections=current_sections,
    )
    scoped_state = _capture_release_scoped_state(
        current_state=current_state,
        touched=touched,
    )
    result_digest = state_sha256(scoped_state)
    if result_digest != _sha256(document["target"]["state_sha256"], "target.state_sha256"):
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its complete scoped target."
        )
    scoped_counts = {
        "sections": len(scoped_state["sections"]),
        "programs": len(scoped_state["programs"]),
        "meetings": len(scoped_state["meetings"]),
        "student_term_sections": len(scoped_state["student_term_sections"]),
    }
    if scoped_counts != document["target"]["scoped_counts"]:
        raise TimetableDeltaError(
            "Current state is neither the artifact base nor its complete target counts."
        )
    desired_observed, observed_added, observed_removed = _predict_observed_programs(
        current_sections=current_sections,
        touched=touched,
        current_release_links=current_links,
    )
    if observed_added or observed_removed:
        raise TimetableDeltaError(
            "Current target relationships exist, but observed memberships are not reconciled."
        )
    _assert_board_eligibility(
        current_sections=current_sections,
        desired_observed=desired_observed,
    )
    return result_digest


def _build_delta_plan(
    loaded: LoadedDelta,
    *,
    current_state: Mapping[str, Any],
    current_digest: str,
) -> DeltaPlan:
    document = loaded.document
    expected_base = _sha256(document["base"]["state_sha256"], "base.state_sha256")
    if current_digest != expected_base:
        raise TimetableDeltaError(
            "Production timetable base-state digest does not match this artifact; no write is safe."
        )
    full_counts = {
        "sections": len(current_state["sections"]),
        "programs": len(current_state["programs"]),
        "meetings": len(current_state["meetings"]),
        "student_term_sections": len(current_state["student_term_sections"]),
    }
    if document["base"]["counts"] != full_counts:
        raise TimetableDeltaError("Production timetable base-state counts are stale.")
    section_index = _artifact_section_index(document)
    current_sections = _current_section_index()
    _assert_import_program_targets(
        section_index=section_index,
        current_sections=current_sections,
    )
    touched = _touched_student_targets(document)
    _validate_touched_students(touched)
    basis_digest, basis_counts = observed_basis_sha256()
    expected_basis = _sha256(
        document["base"].get("observed_basis_sha256"),
        "base.observed_basis_sha256",
    )
    if basis_digest != expected_basis:
        raise TimetableDeltaError(
            "Production student-program basis changed since export; observed memberships "
            "cannot be rebuilt safely."
        )
    if document["base"].get("observed_basis_counts") != basis_counts:
        raise TimetableDeltaError("Observed-basis counts do not match production.")
    scoped_state = _capture_release_scoped_state(
        current_state=current_state,
        touched=touched,
    )
    if state_sha256(scoped_state) != _sha256(
        document["base"].get("scoped_state_sha256"),
        "base.scoped_state_sha256",
    ):
        raise TimetableDeltaError("Production registrar scoped-state digest is stale.")
    scoped_counts = {
        "sections": len(scoped_state["sections"]),
        "programs": len(scoped_state["programs"]),
        "meetings": len(scoped_state["meetings"]),
        "student_term_sections": len(scoped_state["student_term_sections"]),
    }
    if document["base"].get("scoped_counts") != scoped_counts:
        raise TimetableDeltaError("Production registrar scoped counts are stale.")
    current_links = _current_release_links(current_sections)
    stale_students = sorted(
        student_id
        for student_id, target in touched.items()
        if current_links.get(student_id, set()) != set(target.base_sections)
    )
    if stale_students:
        raise TimetableDeltaError(
            "Touched-student base section set does not match production for "
            f"{len(stale_students)} record(s)."
        )
    _validate_section_scope(
        section_index=section_index,
        current_sections=current_sections,
        current_links=current_links,
        touched=touched,
    )
    _assert_no_scenario_scope_rows(touched)

    meeting_additions, meeting_updates, meeting_removals = _expected_meeting_operations(
        section_index, current_sections
    )
    _assert_exact_records(
        document["meetings"]["additions"], meeting_additions, label="meeting additions"
    )
    _assert_exact_records(document["meetings"]["updates"], meeting_updates, label="meeting updates")
    _assert_exact_records(
        document["meetings"]["removals"], meeting_removals, label="meeting removals"
    )
    _assert_placed_meetings_preserved(
        section_index=section_index,
        current_sections=current_sections,
    )

    student_additions, student_removals = _expected_student_operations(touched, current_links)
    operations = _expected_operations(
        section_index=section_index,
        current_sections=current_sections,
        meeting_additions=meeting_additions,
        meeting_updates=meeting_updates,
        meeting_removals=meeting_removals,
        student_additions=student_additions,
        student_removals=student_removals,
        students_replaced=len(touched),
    )
    if document["expected_operations"] != operations:
        raise TimetableDeltaError(
            "Artifact expected operation counts do not match the locked production plan."
        )
    desired_observed, observed_added, observed_removed = _predict_observed_programs(
        current_sections=current_sections,
        touched=touched,
        current_release_links=current_links,
    )
    _assert_board_eligibility(
        current_sections=current_sections,
        desired_observed=desired_observed,
    )
    touched_scopes = (_RELEASE_SCOPE,) if touched else ()
    return DeltaPlan(
        operations=operations,
        base_state_sha256=current_digest,
        section_keys=tuple(sorted(section_index)),
        touched_student_ids=tuple(sorted(touched)),
        touched_scopes=touched_scopes,
        observed_programs_added=observed_added,
        observed_programs_removed=observed_removed,
    )


def _validate_operator_expectations(
    *,
    loaded: LoadedDelta,
    plan: DeltaPlan,
    expected_artifact_sha256: str | None,
    expected_base_state_sha256: str | None,
    expected_operations: Mapping[str, int] | None,
    apply: bool,
) -> None:
    if not apply:
        if expected_artifact_sha256 is not None and (
            _sha256(expected_artifact_sha256, "expected artifact SHA-256") != loaded.sha256
        ):
            raise TimetableDeltaError("Artifact SHA-256 expectation does not match the file.")
        return
    if expected_artifact_sha256 is None:
        raise TimetableDeltaError("--apply requires --expect-sha256.")
    if _sha256(expected_artifact_sha256, "expected artifact SHA-256") != loaded.sha256:
        raise TimetableDeltaError("Artifact SHA-256 expectation does not match the file.")
    if expected_base_state_sha256 is None:
        raise TimetableDeltaError("--apply requires --expect-base-state-sha256.")
    if _sha256(expected_base_state_sha256, "expected base-state SHA-256") != plan.base_state_sha256:
        raise TimetableDeltaError("Base-state SHA-256 expectation does not match production.")
    if expected_operations is None or set(expected_operations) != set(EXPECTED_OPERATION_KEYS):
        raise TimetableDeltaError("--apply requires one --expect-count for every operation key.")
    normalized: dict[str, int] = {}
    for key, value in expected_operations.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TimetableDeltaError(f"Expected count {key!r} must be non-negative.")
        normalized[key] = value
    if normalized != plan.operations:
        raise TimetableDeltaError("Operator expected counts do not match the locked plan.")


def _validate_already_applied_expectations(
    *,
    loaded: LoadedDelta,
    expected_artifact_sha256: str | None,
    expected_base_state_sha256: str | None,
    expected_operations: Mapping[str, int] | None,
    apply: bool,
) -> None:
    if expected_artifact_sha256 is not None and (
        _sha256(expected_artifact_sha256, "expected artifact SHA-256") != loaded.sha256
    ):
        raise TimetableDeltaError("Artifact SHA-256 expectation does not match the file.")
    if not apply:
        return
    if expected_artifact_sha256 is None:
        raise TimetableDeltaError("--apply requires --expect-sha256.")
    if expected_base_state_sha256 is None:
        raise TimetableDeltaError("--apply requires --expect-base-state-sha256.")
    artifact_base = _sha256(
        loaded.document["base"]["state_sha256"],
        "base.state_sha256",
    )
    if (
        _sha256(
            expected_base_state_sha256,
            "expected base-state SHA-256",
        )
        != artifact_base
    ):
        raise TimetableDeltaError(
            "Base-state SHA-256 expectation does not match the artifact's original base."
        )
    if expected_operations is None or set(expected_operations) != set(EXPECTED_OPERATION_KEYS):
        raise TimetableDeltaError("--apply requires one --expect-count for every operation key.")
    normalized: dict[str, int] = {}
    for key, value in expected_operations.items():
        normalized[key] = _nonnegative_integer(value, f"Expected count {key!r}")
    if normalized != loaded.document["expected_operations"]:
        raise TimetableDeltaError(
            "Operator expected counts do not match the artifact's original operation counts."
        )


def _apply_section_upserts(
    loaded: LoadedDelta,
    *,
    now: str,
) -> dict[SectionKey, TermSection]:
    current = _current_section_index()
    provenance = f"timetable_delta:{loaded.sha256[:16]}"
    for key, record in _artifact_section_index(loaded.document).items():
        section = current.get(key)
        values = {
            "source_tag": _nonblank(record.get("source_tag"), "section.source_tag"),
            "course_name": _text(record.get("course_name"), "section.course_name"),
            "available_capacity": record.get("available_capacity"),
            "registered_count": record.get("registered_count"),
            "course_code": key[0],
            "course_number": key[1],
            "course_key": derived_course_key(key[0], key[1]),
            "section": key[2],
            "updated_at": now,
        }
        if section is None:
            section = TermSection.objects.create(
                scenario=None,
                source_file=provenance,
                created_at=now,
                **values,
            )
            current[key] = section
        else:
            for field, value in values.items():
                setattr(section, field, value)
            section.save(update_fields=[*values])
    return current


def _apply_meeting_targets(
    document: Mapping[str, Any],
    sections: Mapping[SectionKey, TermSection],
    *,
    now: str,
) -> None:
    for section_key, target_section in _artifact_section_index(document).items():
        if target_section.get("meeting_mode") == "preserve":
            continue
        section = sections[section_key]
        desired_records = [
            _nested_meeting_record(section_key, row) for row in target_section["meetings"]
        ]
        desired = {_meeting_key(row, "target meeting"): row for row in desired_records}
        existing_rows = list(TermSectionMeeting.objects.filter(term_section=section))
        existing = {
            _meeting_key(_meeting_record(section_key, row), "current meeting"): row
            for row in existing_rows
        }
        remove_ids = [row.id for key, row in existing.items() if key not in desired]
        if remove_ids:
            TermSectionMeeting.objects.filter(id__in=remove_ids).delete()
        for key, record in desired.items():
            row = existing.get(key)
            if row is None:
                TermSectionMeeting.objects.create(
                    term_section=section,
                    day=record["day"],
                    start_time=record["start_time"],
                    end_time=record["end_time"],
                    building=record["building"],
                    floor_wing=record["floor_wing"],
                    room=record["room"],
                    instructor=record["instructor"],
                    created_at=now,
                    updated_at=now,
                )
                continue
            if row.building != record["building"] or row.floor_wing != record["floor_wing"]:
                row.building = record["building"]
                row.floor_wing = record["floor_wing"]
                row.updated_at = now
                row.save(update_fields=["building", "floor_wing", "updated_at"])


def _apply_student_targets(
    document: Mapping[str, Any],
    sections: Mapping[SectionKey, TermSection],
    *,
    now: str,
) -> set[int]:
    touched = _touched_student_targets(document)
    touched_section_ids: set[int] = set()
    for student_id, target in touched.items():
        existing_rows = list(
            StudentTermSection.objects.filter(
                student_id=student_id,
                academic_year=_RELEASE_ACADEMIC_YEAR,
                term=_RELEASE_TERM,
                source=_RELEASE_SOURCE,
                term_section__scenario__isnull=True,
            ).select_related("term_section")
        )
        existing = {
            normalise_section_key(
                row.term_section.course_code,
                row.term_section.course_number,
                row.term_section.section,
            ): row
            for row in existing_rows
        }
        touched_section_ids.update(int(row.term_section_id) for row in existing_rows)
        remove_ids = [row.id for key, row in existing.items() if key not in target.target_sections]
        if remove_ids:
            StudentTermSection.objects.filter(id__in=remove_ids).delete()
        for key in target.target_sections:
            section = sections[key]
            touched_section_ids.add(int(section.id))
            if key in existing:
                continue
            StudentTermSection.objects.create(
                student_id=student_id,
                academic_year=_RELEASE_ACADEMIC_YEAR,
                term=_RELEASE_TERM,
                source=_RELEASE_SOURCE,
                term_section=section,
                created_at=now,
                updated_at=now,
            )
    return touched_section_ids


def _assert_postconditions(loaded: LoadedDelta) -> str:
    document = loaded.document
    touched = _touched_student_targets(document)
    current_sections = _current_section_index()
    current_links = _current_release_links(current_sections)
    stale = [
        student_id
        for student_id, target in touched.items()
        if current_links.get(student_id, set()) != set(target.target_sections)
    ]
    if stale:
        raise TimetableDeltaError(
            f"Postcondition failed for {len(stale)} touched-student record(s)."
        )
    meeting_ops = _expected_meeting_operations(_artifact_section_index(document), current_sections)
    if any(meeting_ops):
        raise TimetableDeltaError(
            "Postcondition failed: pending section-meeting operations remain."
        )
    student_ops = _expected_student_operations(touched, current_links)
    if any(student_ops):
        raise TimetableDeltaError(
            "Postcondition failed: pending student-relationship operations remain."
        )
    for key, record in _artifact_section_index(document).items():
        section = current_sections[key]
        actual = (
            str(section.source_tag or ""),
            str(section.course_name or ""),
            section.available_capacity,
            section.registered_count,
        )
        expected = (
            _nonblank(record.get("source_tag"), "section.source_tag"),
            _text(record.get("course_name"), "section.course_name"),
            record.get("available_capacity"),
            record.get("registered_count"),
        )
        if actual != expected:
            raise TimetableDeltaError(f"Postcondition failed for section metadata {key!r}.")
    current_state = capture_current_timetable_state()
    scoped_state = _capture_release_scoped_state(current_state=current_state, touched=touched)
    result_digest = state_sha256(scoped_state)
    expected_digest = _sha256(document["target"]["state_sha256"], "target.state_sha256")
    if result_digest != expected_digest:
        raise TimetableDeltaError(
            "Postcondition failed: effective target scoped-state digest does not match."
        )
    expected_counts = document["target"].get("scoped_counts")
    actual_counts = {
        "sections": len(scoped_state["sections"]),
        "programs": len(scoped_state["programs"]),
        "meetings": len(scoped_state["meetings"]),
        "student_term_sections": len(scoped_state["student_term_sections"]),
    }
    if expected_counts != actual_counts:
        raise TimetableDeltaError("Postcondition failed: effective scoped counts differ.")
    _desired, observed_added, observed_removed = _predict_observed_programs(
        current_sections=current_sections,
        touched=touched,
        current_release_links=current_links,
    )
    if observed_added or observed_removed:
        raise TimetableDeltaError("Postcondition failed: observed memberships are not reconciled.")
    return result_digest


def import_timetable_delta_artifact(
    artifact_path: str | Path,
    *,
    apply: bool = False,
    expected_artifact_sha256: str | None = None,
    expected_base_state_sha256: str | None = None,
    expected_operations: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply the guarded registrar timetable delta."""

    if not isinstance(apply, bool):
        raise TimetableDeltaError("apply must be a boolean.")
    loaded = load_timetable_delta(artifact_path)
    with section_snapshot_operation_guard(blocking=False) as acquired:
        if not acquired:
            raise TimetableDeltaError(
                "Another section snapshot operation is running; retry after it finishes."
            )
        with transaction.atomic():
            _take_database_import_lock()
            _lock_current_timetable_state(_artifact_touched_student_ids(loaded.document))
            _assert_current_migration_state(loaded.document)
            current_state = capture_current_timetable_state()
            current_digest = state_sha256(current_state)
            artifact_base_digest = _sha256(
                loaded.document["base"]["state_sha256"],
                "base.state_sha256",
            )
            if current_digest != artifact_base_digest:
                result_digest = _verify_already_applied_target(
                    loaded,
                    current_state=current_state,
                )
                _validate_already_applied_expectations(
                    loaded=loaded,
                    expected_artifact_sha256=expected_artifact_sha256,
                    expected_base_state_sha256=expected_base_state_sha256,
                    expected_operations=expected_operations,
                    apply=apply,
                )
                zero_pending = {key: 0 for key in EXPECTED_OPERATION_KEYS}
                return {
                    "mode": "already_applied",
                    "already_applied": True,
                    "writes_performed": False,
                    "base_state_match": False,
                    "target_state_match": True,
                    "artifact_sha256": loaded.sha256,
                    "base_state_sha256": artifact_base_digest,
                    "current_state_sha256": current_digest,
                    "operations": dict(loaded.document["expected_operations"]),
                    "pending_operations": zero_pending,
                    "touched_students": len(_artifact_touched_student_ids(loaded.document)),
                    "scope": {
                        "academic_year": _RELEASE_ACADEMIC_YEAR,
                        "term": _RELEASE_TERM,
                        "source": _RELEASE_SOURCE,
                    },
                    "observed_programs_predicted_added": 0,
                    "observed_programs_predicted_removed": 0,
                    "internal_postcondition_zero_pending_operations": True,
                    "result_state_sha256": result_digest,
                }
            plan = _build_delta_plan(
                loaded,
                current_state=current_state,
                current_digest=current_digest,
            )
            _validate_operator_expectations(
                loaded=loaded,
                plan=plan,
                expected_artifact_sha256=expected_artifact_sha256,
                expected_base_state_sha256=expected_base_state_sha256,
                expected_operations=expected_operations,
                apply=apply,
            )
            result_digest = ""
            if apply:
                section_count_before = TermSection.objects.filter(scenario__isnull=True).count()
                now = datetime.now(UTC).isoformat()
                sections = _apply_section_upserts(loaded, now=now)
                _apply_meeting_targets(loaded.document, sections, now=now)
                touched_section_ids = _apply_student_targets(loaded.document, sections, now=now)
                if touched_section_ids:
                    reconcile_observed_section_programs(touched_section_ids)
                section_count_after = TermSection.objects.filter(scenario__isnull=True).count()
                if (
                    section_count_after
                    != section_count_before + plan.operations["sections_created"]
                ):
                    raise TimetableDeltaError(
                        "Section-count postcondition failed; all changes were rolled back."
                    )
                result_digest = _assert_postconditions(loaded)
            return {
                "mode": "applied" if apply else "dry_run",
                "already_applied": False,
                "writes_performed": bool(apply),
                "base_state_match": True,
                "target_state_match": bool(apply),
                "artifact_sha256": loaded.sha256,
                "base_state_sha256": plan.base_state_sha256,
                "current_state_sha256": current_digest,
                "operations": plan.operations,
                "pending_operations": (
                    {key: 0 for key in EXPECTED_OPERATION_KEYS} if apply else dict(plan.operations)
                ),
                "touched_students": len(plan.touched_student_ids),
                "scope": {
                    "academic_year": _RELEASE_ACADEMIC_YEAR,
                    "term": _RELEASE_TERM,
                    "source": _RELEASE_SOURCE,
                },
                "observed_programs_predicted_added": plan.observed_programs_added,
                "observed_programs_predicted_removed": plan.observed_programs_removed,
                "internal_postcondition_zero_pending_operations": bool(apply),
                "result_state_sha256": result_digest,
            }
