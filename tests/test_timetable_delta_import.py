# mypy: disable-error-code="no-untyped-def,index"

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services import timetable_delta_import as importer
from core.services.timetable_delta import (
    DELTA_SCHEMA_VERSION,
    canonical_json_bytes,
    canonical_state_document,
    section_key_dict,
    state_sha256,
)

pytestmark = pytest.mark.django_db


def _section(code: str, number: str, name: str, *, source: str = "scraper_timetable"):
    return TermSection.objects.create(
        scenario=None,
        source_tag=source,
        course_code=code,
        course_number=number,
        course_key=f"{code}{number}",
        course_name=f"{code}{number}",
        section=name,
    )


@pytest.fixture
def delta_world(tmp_path):
    Student.objects.create(
        student_id=1001,
        program="AI",
        section="M",
        status="ACTIVE",
    )
    Student.objects.create(
        student_id=1002,
        program="DS",
        section="F",
        status="ACTIVE",
    )
    section_a = _section("AI", "101", "M1")
    section_c = _section("DS", "201", "F1")
    meeting_a = TermSectionMeeting.objects.create(
        term_section=section_a,
        day="MON",
        start_time="09:00",
        end_time="10:00",
        building="1",
        room="101",
    )
    TermSectionMeeting.objects.create(
        term_section=section_c,
        day="TUE",
        start_time="10:00",
        end_time="11:00",
        building="2",
        room="202",
    )
    TermSectionProgram.objects.create(
        term_section=section_a,
        program="AI",
        assignment_source="observed",
    )
    TermSectionProgram.objects.create(
        term_section=section_c,
        program="AI",
        assignment_source="observed",
    )
    TermSectionProgram.objects.create(
        term_section=section_c,
        program="DS",
        assignment_source="observed",
    )
    for student_id, section in ((1001, section_a), (1001, section_c), (1002, section_c)):
        StudentTermSection.objects.create(
            student_id=student_id,
            academic_year="1448",
            term="1",
            source="scraper_timetable",
            term_section=section,
        )
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        source="registration_plan_1448_t1",
        term_section=section_c,
    )

    base_state = importer.capture_current_timetable_state()
    touched = importer.TouchedStudent(
        expected_program="AI",
        expected_section="M",
        expected_status="ACTIVE",
        base_sections=frozenset({("AI", "101", "M1"), ("DS", "201", "F1")}),
        target_sections=frozenset({("AI", "101", "M1"), ("AI", "102", "M2")}),
    )
    base_scoped = importer._capture_release_scoped_state(
        current_state=base_state,
        touched={1001: touched},
    )
    basis_digest, basis_counts = importer.observed_basis_sha256()

    key_a = ("AI", "101", "M1")
    key_b = ("AI", "102", "M2")
    key_c = ("DS", "201", "F1")
    meeting_a_before = {
        **section_key_dict(key_a),
        "day": "MON",
        "start_time": "09:00",
        "end_time": "10:00",
        "building": "1",
        "floor_wing": "",
        "room": "101",
        "instructor": "",
    }
    meeting_a_after = {
        **section_key_dict(key_a),
        "day": "MON",
        "start_time": "11:00",
        "end_time": "12:00",
        "building": "1",
        "floor_wing": "",
        "room": "101",
        "instructor": "",
    }
    meeting_b = {
        **section_key_dict(key_b),
        "day": "WED",
        "start_time": "13:00",
        "end_time": "14:00",
        "building": "3",
        "floor_wing": "",
        "room": "303",
        "instructor": "",
    }
    meeting_c = {
        **section_key_dict(key_c),
        "day": "TUE",
        "start_time": "10:00",
        "end_time": "11:00",
        "building": "2",
        "floor_wing": "",
        "room": "202",
        "instructor": "",
    }
    section_a_target = {
        **section_key_dict(key_a),
        "source_tag": "scraper_timetable",
        "course_name": "AI101",
        "available_capacity": None,
        "registered_count": None,
    }
    section_b_target = {
        **section_key_dict(key_b),
        "source_tag": "scraper_timetable",
        "course_name": "AI102",
        "available_capacity": 20,
        "registered_count": 1,
    }
    section_c_target = {
        **section_key_dict(key_c),
        "source_tag": "scraper_timetable",
        "course_name": "DS201",
        "available_capacity": None,
        "registered_count": None,
    }
    target_links = [
        importer._student_record(1001, key_a),
        importer._student_record(1001, key_b),
        importer._student_record(1002, key_c),
    ]
    target_scoped = canonical_state_document(
        sections=[section_a_target, section_b_target, section_c_target],
        programs=[],
        meetings=[meeting_a_after, meeting_b, meeting_c],
        student_term_sections=target_links,
    )
    operations = {
        "sections_created": 1,
        "sections_updated": 1,
        "section_upserts": 2,
        "programs_added": 0,
        "programs_updated": 0,
        "programs_removed": 0,
        "meetings_added": 2,
        "meetings_updated": 0,
        "meetings_removed": 1,
        "students_replaced": 1,
        "student_term_sections_added": 1,
        "student_term_sections_removed": 1,
    }
    migration_metadata = importer._current_migration_metadata()
    artifact = {
        "schema_version": DELTA_SCHEMA_VERSION,
        "metadata": {
            "exporter_version": "1",
            "canonicalization_version": "json-sort-keys-ascii-v1",
            "generated_at_utc": "2026-09-02T00:00:00Z",
            "generated_at_basis": "target_snapshot_mtime",
            "data_classification": "restricted_student_timetable",
            "contains_student_identifiers": True,
            "integrity_note": "sha256_requires_an_operator_supplied_expected_digest",
            "observed_program_churn": {
                "added": 1,
                "updated": 0,
                "removed": 1,
                "applied": False,
                "reason": "derived_from_final_registrar_links_and_production_student_programs",
            },
            "excluded_import_program_churn": {
                "added": 0,
                "updated": 0,
                "removed": 0,
                "applied": False,
                "reason": "production_import_and_manual_memberships_are_authoritative",
            },
            "excluded_non_registrar_changes": [],
            "excluded_global_state": {
                "target_orphan_sections": 0,
                "target_section_creates_outside_scope": 0,
                "base_sections_missing_from_target": 0,
                "empty_target_meeting_sets_preserved": 0,
            },
            "excluded_student_relationship_changes": {
                "students_by_reason": {},
                "relationships_added": 0,
                "relationships_removed": 0,
                "action": "preserve_production_and_route_to_separate_roster_sync",
            },
        },
        "scope": {
            "term_sections": "scenario_is_null_and_referenced_by_target_scope",
            "student_term_sections": {
                "academic_year": "1448",
                "term": "1",
                "source": "scraper_timetable",
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
            "database": {
                "sha256": "0" * 64,
                "size_bytes": 1,
                "modified_at_utc": "2026-09-01T00:00:00Z",
            },
            "migrations": {
                **migration_metadata,
            },
            "counts": {
                name: len(base_state[name])
                for name in ("sections", "programs", "meetings", "student_term_sections")
            },
            "state_sha256": state_sha256(base_state),
            "state_program_scope": "all_global_rows",
            "scoped_state_sha256": state_sha256(base_scoped),
            "scoped_counts": {
                name: len(base_scoped[name])
                for name in ("sections", "programs", "meetings", "student_term_sections")
            },
            "observed_basis_sha256": basis_digest,
            "observed_basis_counts": basis_counts,
        },
        "target": {
            "database": {
                "sha256": "2" * 64,
                "size_bytes": 2,
                "modified_at_utc": "2026-09-02T00:00:00Z",
            },
            "migrations": {
                **migration_metadata,
            },
            "source_counts": {
                name: len(target_scoped[name])
                for name in ("sections", "programs", "meetings", "student_term_sections")
            },
            "source_state_sha256": state_sha256(target_scoped),
            "state_sha256": state_sha256(target_scoped),
            "state_program_scope": "import_only_with_observed_rebuilt_separately",
            "scoped_counts": {
                name: len(target_scoped[name])
                for name in ("sections", "programs", "meetings", "student_term_sections")
            },
        },
        "expected_operations": operations,
        "sections": {
            "removals": [],
            "upserts": [
                {
                    **section_a_target,
                    "programs": [],
                    "programs_complete": True,
                    "meetings": [
                        {
                            k: v
                            for k, v in meeting_a_after.items()
                            if k not in section_key_dict(key_a)
                        }
                    ],
                    "meetings_complete": True,
                    "meeting_mode": "replace",
                },
                {
                    **section_b_target,
                    "programs": [],
                    "programs_complete": True,
                    "meetings": [
                        {k: v for k, v in meeting_b.items() if k not in section_key_dict(key_b)}
                    ],
                    "meetings_complete": True,
                    "meeting_mode": "replace",
                },
            ],
        },
        "programs": {"additions": [], "updates": [], "removals": []},
        "meetings": {
            "additions": [meeting_a_after, meeting_b],
            "updates": [],
            "removals": [meeting_a_before],
        },
        "student_term_sections": {
            "touched_students": [
                {
                    "student_id": 1001,
                    "expected_program": "AI",
                    "expected_section": "M",
                    "expected_status": "ACTIVE",
                    "base_sections": [section_key_dict(key_a), section_key_dict(key_c)],
                    "target_sections": [section_key_dict(key_a), section_key_dict(key_b)],
                }
            ]
        },
    }
    path = tmp_path / "delta.json"
    payload = canonical_json_bytes(artifact) + b"\n"
    path.write_bytes(payload)
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "base_sha256": artifact["base"]["state_sha256"],
        "operations": operations,
        "section_a": section_a,
        "section_c": section_c,
        "meeting_a": meeting_a,
    }


def test_dry_run_is_read_only(delta_world):
    counts_before = (
        TermSection.objects.count(),
        TermSectionMeeting.objects.count(),
        StudentTermSection.objects.count(),
    )
    summary = importer.import_timetable_delta_artifact(delta_world["path"])
    assert summary["mode"] == "dry_run"
    assert summary["operations"] == delta_world["operations"]
    assert counts_before == (
        TermSection.objects.count(),
        TermSectionMeeting.objects.count(),
        StudentTermSection.objects.count(),
    )


def test_apply_replaces_only_touched_registrar_snapshot(delta_world):
    summary = importer.import_timetable_delta_artifact(
        delta_world["path"],
        apply=True,
        expected_artifact_sha256=delta_world["sha256"],
        expected_base_state_sha256=delta_world["base_sha256"],
        expected_operations=delta_world["operations"],
    )
    assert summary["internal_postcondition_zero_pending_operations"] is True
    assert TermSection.objects.filter(course_key="AI102", section="M2").exists()
    assert not TermSectionMeeting.objects.filter(id=delta_world["meeting_a"].id).exists()
    assert StudentTermSection.objects.filter(
        student_id=1001,
        source="scraper_timetable",
        term_section__course_key="AI102",
    ).exists()
    assert not StudentTermSection.objects.filter(
        student_id=1001,
        source="scraper_timetable",
        term_section=delta_world["section_c"],
    ).exists()
    assert StudentTermSection.objects.filter(
        student_id=1001,
        source="registration_plan_1448_t1",
        term_section=delta_world["section_c"],
    ).exists()
    assert StudentTermSection.objects.filter(
        student_id=1002,
        source="scraper_timetable",
        term_section=delta_world["section_c"],
    ).exists()
    observed_c = set(
        TermSectionProgram.objects.filter(
            term_section=delta_world["section_c"], assignment_source="observed"
        ).values_list("program", flat=True)
    )
    assert observed_c == {"AI", "DS"}, "registration-plan rows still support AI ownership"


def test_repeated_apply_is_recognized_read_only_with_original_pins(delta_world):
    kwargs = {
        "apply": True,
        "expected_artifact_sha256": delta_world["sha256"],
        "expected_base_state_sha256": delta_world["base_sha256"],
        "expected_operations": delta_world["operations"],
    }
    importer.import_timetable_delta_artifact(delta_world["path"], **kwargs)
    before = {
        "sections": list(TermSection.objects.order_by("id").values()),
        "meetings": list(TermSectionMeeting.objects.order_by("id").values()),
        "links": list(StudentTermSection.objects.order_by("id").values()),
        "programs": list(TermSectionProgram.objects.order_by("id").values()),
    }

    summary = importer.import_timetable_delta_artifact(delta_world["path"], **kwargs)

    assert summary["mode"] == "already_applied"
    assert summary["already_applied"] is True
    assert summary["operations"] == delta_world["operations"]
    assert set(summary["pending_operations"].values()) == {0}
    assert summary["internal_postcondition_zero_pending_operations"] is True
    assert before == {
        "sections": list(TermSection.objects.order_by("id").values()),
        "meetings": list(TermSectionMeeting.objects.order_by("id").values()),
        "links": list(StudentTermSection.objects.order_by("id").values()),
        "programs": list(TermSectionProgram.objects.order_by("id").values()),
    }


def test_repeated_apply_still_requires_original_operator_pins(delta_world):
    importer.import_timetable_delta_artifact(
        delta_world["path"],
        apply=True,
        expected_artifact_sha256=delta_world["sha256"],
        expected_base_state_sha256=delta_world["base_sha256"],
        expected_operations=delta_world["operations"],
    )
    with pytest.raises(importer.TimetableDeltaError, match="requires --expect-sha256"):
        importer.import_timetable_delta_artifact(delta_world["path"], apply=True)


def test_stale_non_target_state_is_not_mistaken_for_already_applied(delta_world):
    kwargs = {
        "apply": True,
        "expected_artifact_sha256": delta_world["sha256"],
        "expected_base_state_sha256": delta_world["base_sha256"],
        "expected_operations": delta_world["operations"],
    }
    importer.import_timetable_delta_artifact(delta_world["path"], **kwargs)
    TermSection.objects.filter(course_key="AI101", section="M1").update(
        source_tag="concurrent_edit"
    )
    with pytest.raises(importer.TimetableDeltaError, match="neither the artifact base nor"):
        importer.import_timetable_delta_artifact(delta_world["path"], **kwargs)
    assert TermSection.objects.get(course_key="AI101", section="M1").source_tag == "concurrent_edit"


def test_stale_base_digest_rolls_back_everything(delta_world):
    delta_world["section_a"].course_name = "concurrent edit"
    delta_world["section_a"].save(update_fields=["course_name"])
    with pytest.raises(importer.TimetableDeltaError, match="neither the artifact base nor"):
        importer.import_timetable_delta_artifact(
            delta_world["path"],
            apply=True,
            expected_artifact_sha256=delta_world["sha256"],
            expected_base_state_sha256=delta_world["base_sha256"],
            expected_operations=delta_world["operations"],
        )
    assert not TermSection.objects.filter(course_key="AI102", section="M2").exists()


def test_unknown_student_aborts(delta_world):
    Student.objects.filter(student_id=1001).delete()
    with pytest.raises(
        importer.TimetableDeltaError, match="unknown production student"
    ) as captured:
        importer.import_timetable_delta_artifact(delta_world["path"])
    assert "1001" not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("program", "DS"), ("section", "F"), ("status", "SUSPENDED")],
)
def test_touched_student_roster_drift_aborts(delta_world, field, value):
    Student.objects.filter(student_id=1001).update(**{field: value})
    with pytest.raises(importer.TimetableDeltaError, match="programme/cohort/status"):
        importer.import_timetable_delta_artifact(delta_world["path"])


def test_section_removal_artifact_is_rejected(delta_world):
    loaded = importer.load_timetable_delta(delta_world["path"])
    loaded.document["sections"]["removals"] = [
        {"course_code": "AI", "course_number": "101", "section": "M1"}
    ]
    path = delta_world["path"].with_name("unsafe.json")
    path.write_bytes(canonical_json_bytes(loaded.document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match="Section removals are forbidden"):
        importer.import_timetable_delta_artifact(path)


def test_normalized_section_collision_is_rejected(delta_world):
    loaded = importer.load_timetable_delta(delta_world["path"])
    duplicate = dict(loaded.document["sections"]["upserts"][0])
    duplicate["course_code"] = str(duplicate["course_code"]).lower()
    loaded.document["sections"]["upserts"].append(duplicate)
    path = delta_world["path"].with_name("collision.json")
    path.write_bytes(canonical_json_bytes(loaded.document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match="must be canonical"):
        importer.import_timetable_delta_artifact(path)

    loaded = importer.load_timetable_delta(delta_world["path"])
    split = dict(loaded.document["sections"]["upserts"][0])
    split["course_code"] = "A"
    split["course_number"] = "I101"
    loaded.document["sections"]["upserts"].append(split)
    path = delta_world["path"].with_name("split-collision.json")
    path.write_bytes(canonical_json_bytes(loaded.document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match="split section identities collapse"):
        importer.import_timetable_delta_artifact(path)


def test_apply_requires_artifact_and_operation_expectations(delta_world):
    with pytest.raises(importer.TimetableDeltaError, match="requires --expect-sha256"):
        importer.import_timetable_delta_artifact(delta_world["path"], apply=True)
    assert not TermSection.objects.filter(course_key="AI102", section="M2").exists()


def test_strict_schema_rejects_ignored_root_and_nested_payloads(delta_world):
    loaded = importer.load_timetable_delta(delta_world["path"])
    mutations = (
        lambda document: document.__setitem__("payload", {"ignored": True}),
        lambda document: document["sections"]["upserts"][0].__setitem__(
            "updated_at", "local timestamp"
        ),
        lambda document: document["sections"]["upserts"][0]["meetings"][0].__setitem__(
            "source_file", "C:/private/path"
        ),
        lambda document: document["student_term_sections"]["touched_students"][0].__setitem__(
            "name", "private payload"
        ),
    )
    for index, mutate in enumerate(mutations):
        document = copy.deepcopy(loaded.document)
        mutate(document)
        path = delta_world["path"].with_name(f"strict-{index}.json")
        path.write_bytes(canonical_json_bytes(document) + b"\n")
        with pytest.raises(importer.TimetableDeltaError, match="strict artifact schema"):
            importer.import_timetable_delta_artifact(path)


@pytest.mark.parametrize(
    ("path_parts", "value", "message"),
    [
        (("metadata", "exporter_version"), {}, "metadata.exporter_version"),
        (("scope", "program_assignments"), "all", "scope.program_assignments"),
        (("base", "database", "size_bytes"), True, "non-negative integer"),
        (("target", "source_state_sha256"), [], "string SHA-256"),
        (("sections", "upserts", 0, "course_name"), {}, "must be a string"),
        (
            ("sections", "upserts", 0, "meetings", 0, "building"),
            {},
            "must be a string",
        ),
        (
            ("student_term_sections", "touched_students", 0, "expected_status"),
            [],
            "must be a string",
        ),
    ],
)
def test_scalar_and_semantic_tampering_is_rejected(delta_world, path_parts, value, message):
    document = copy.deepcopy(importer.load_timetable_delta(delta_world["path"]).document)
    parent = document
    for part in path_parts[:-1]:
        parent = parent[part]
    parent[path_parts[-1]] = value
    path = delta_world["path"].with_name("semantic-tamper.json")
    path.write_bytes(canonical_json_bytes(document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match=message):
        importer.import_timetable_delta_artifact(path)


def test_production_migrations_must_match_both_snapshots(delta_world):
    document = copy.deepcopy(importer.load_timetable_delta(delta_world["path"]).document)
    for side in ("base", "target"):
        document[side]["migrations"]["applied_count"] += 1
    path = delta_world["path"].with_name("migration-drift.json")
    path.write_bytes(canonical_json_bytes(document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match="Production migration metadata"):
        importer.import_timetable_delta_artifact(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("day", "FRI", "SUN-THU"),
        ("start_time", "9:00", "HH:MM"),
        ("end_time", "08:00", "earlier than"),
    ],
)
def test_meeting_domain_tampering_is_rejected(delta_world, field, value, message):
    loaded = importer.load_timetable_delta(delta_world["path"])
    loaded.document["sections"]["upserts"][0]["meetings"][0][field] = value
    path = delta_world["path"].with_name(f"bad-{field}.json")
    path.write_bytes(canonical_json_bytes(loaded.document) + b"\n")
    with pytest.raises(importer.TimetableDeltaError, match=message):
        importer.import_timetable_delta_artifact(path)


def test_artifact_requires_canonical_encoding_and_size_cap(delta_world, monkeypatch):
    document = importer.load_timetable_delta(delta_world["path"]).document
    noncanonical = delta_world["path"].with_name("pretty.json")
    noncanonical.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(importer.TimetableDeltaError, match="canonical JSON encoding"):
        importer.import_timetable_delta_artifact(noncanonical)

    monkeypatch.setattr(importer, "_MAX_ARTIFACT_BYTES", 1)
    with pytest.raises(importer.TimetableDeltaError, match="16 MiB safety limit"):
        importer.import_timetable_delta_artifact(delta_world["path"])


def test_lock_query_dedupes_students_in_python_and_includes_zero_base_touched_student():
    Student.objects.create(
        student_id=9001,
        program="AI",
        section="M",
        status="ACTIVE",
    )
    Student.objects.create(
        student_id=9099,
        program="AI",
        section="M",
        status="ACTIVE",
    )
    section_one = _section("AI", "901", "M1")
    section_two = _section("AI", "902", "M2")
    for section in (section_one, section_two):
        StudentTermSection.objects.create(
            student_id=9001,
            academic_year="1448",
            term="1",
            source="scraper_timetable",
            term_section=section,
        )
    with CaptureQueriesContext(connection) as captured:
        with transaction.atomic():
            importer._lock_current_timetable_state([9099])
    sql = [query["sql"] for query in captured.captured_queries]
    registration_queries = [query for query in sql if 'FROM "student_term_sections"' in query]
    student_queries = [query for query in sql if 'FROM "students"' in query]
    assert registration_queries
    assert all("DISTINCT" not in query.upper() for query in registration_queries)
    assert student_queries
    assert student_queries[-1].count("9001") == 1
    assert "9099" in student_queries[-1]


def test_meeting_replacement_cannot_stale_existing_placement(delta_world):
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="integrity test",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI board",
        program="AI",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=delta_world["section_a"],
        day="MON",
        start_time="09:00",
        end_time="10:00",
        room="101",
    )
    with pytest.raises(
        importer.TimetableDeltaError, match="invalidate 1 existing section placement"
    ):
        importer.import_timetable_delta_artifact(delta_world["path"])
