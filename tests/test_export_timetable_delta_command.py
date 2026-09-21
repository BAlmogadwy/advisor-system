# mypy: disable-error-code="no-untyped-def"

from __future__ import annotations

import hashlib
import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from core.management.commands import export_timetable_delta
from core.models import (
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.timetable_delta import canonical_state_document, state_sha256

pytestmark = pytest.mark.django_db(transaction=True)


def _snapshot(path: Path) -> Path:
    path.unlink(missing_ok=True)
    connection.ensure_connection()
    with sqlite3.connect(path) as destination:
        connection.connection.backup(destination)
    return path


def _section(number: str, section: str = "A", **overrides) -> TermSection:
    values = {
        "course_code": "AI",
        "course_number": number,
        "course_key": f"AI{number}",
        "course_name": f"Course {number}",
        "section": section,
        "source_tag": "scraper_timetable",
        "source_file": "C:/private/local-source.xlsx",
    }
    values.update(overrides)
    return TermSection.objects.create(**values)


def _registrar_link(student_id: int, section: TermSection) -> StudentTermSection:
    return StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        source="scraper_timetable",
        term_section=section,
    )


def _export(base: Path, target: Path, output: Path) -> tuple[bytes, dict, str]:
    stdout = StringIO()
    call_command(
        "export_timetable_delta",
        str(base),
        str(target),
        str(output),
        stdout=stdout,
    )
    payload = output.read_bytes()
    artifact = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert output.with_name(f"{output.name}.sha256").read_text(encoding="ascii") == f"{digest}\n"
    assert digest in stdout.getvalue()
    return payload, artifact, digest


def test_export_is_scoped_deterministic_and_uses_complete_touched_student_sets(tmp_path):
    stable = Student.objects.create(
        student_id=700001,
        program="AI",
        section="G1",
        status="active",
        name="Must not be exported",
    )
    unchanged = Student.objects.create(
        student_id=700002, program="AI", section="G1", status="active"
    )
    unscheduled = Student.objects.create(
        student_id=700003, program="AI", section="G1", status="active"
    )
    roster_changed = Student.objects.create(
        student_id=700004, program="AI", section="G1", status="active"
    )

    first = _section("101")
    second = _section("102")
    no_longer_scheduled = _section("104")
    removed_orphan = _section("199", source_tag="other")
    TermSectionProgram.objects.create(term_section=first, program="AI", assignment_source="import")
    TermSectionProgram.objects.create(
        term_section=first, program="CY", assignment_source="observed"
    )
    first_meeting = TermSectionMeeting.objects.create(
        term_section=first,
        day="SUN",
        start_time="09:00",
        end_time="10:00",
        building="B1",
        floor_wing="F1",
        room="R1",
        instructor="Instructor A",
    )
    second_meeting = TermSectionMeeting.objects.create(
        term_section=second,
        day="MON",
        start_time="10:00",
        end_time="11:00",
        building="Old",
        floor_wing="F2",
        room="R2",
        instructor="Instructor B",
    )
    unscheduled_meeting = TermSectionMeeting.objects.create(
        term_section=no_longer_scheduled,
        day="TUE",
        start_time="11:00",
        end_time="12:00",
        room="R4",
        instructor="Instructor C",
    )
    _registrar_link(stable.student_id, first)
    _registrar_link(unchanged.student_id, first)
    _registrar_link(unscheduled.student_id, no_longer_scheduled)
    _registrar_link(roster_changed.student_id, first)
    StudentTermSection.objects.create(
        student_id=stable.student_id,
        academic_year="1448",
        term="1",
        source="registration_plan_1448_t1",
        term_section=first,
    )

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="private")
    scenario_section = _section("998", scenario=scenario, source_tag="tw_auto")
    _registrar_link(stable.student_id, scenario_section)
    baseline = _snapshot(tmp_path / "baseline.sqlite3")
    baseline_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()

    StudentTermSection.objects.filter(
        student_id=stable.student_id,
        source="scraper_timetable",
        term_section=first,
    ).delete()
    _registrar_link(stable.student_id, second)
    third = _section("103", available_capacity=20)
    _registrar_link(stable.student_id, third)
    TermSectionMeeting.objects.create(
        term_section=third,
        day="WED",
        start_time="12:00",
        end_time="13:00",
        room="R3",
        instructor="Instructor D",
    )

    Student.objects.filter(pk=roster_changed.pk).update(program="DS")
    StudentTermSection.objects.filter(
        student_id=roster_changed.student_id,
        source="scraper_timetable",
        term_section=first,
    ).delete()
    _registrar_link(roster_changed.student_id, second)
    target_only = Student.objects.create(
        student_id=700005, program="AI", section="G1", status="active"
    )
    _registrar_link(target_only.student_id, third)

    StudentTermSection.objects.filter(source="registration_plan_1448_t1").delete()
    TermSectionProgram.objects.filter(term_section=first, assignment_source="import").delete()
    TermSectionProgram.objects.create(term_section=first, program="DS", assignment_source="import")
    TermSectionProgram.objects.filter(term_section=first, assignment_source="observed").delete()
    TermSectionProgram.objects.create(
        term_section=second, program="CY", assignment_source="observed"
    )

    first_meeting.delete()
    TermSectionMeeting.objects.create(
        term_section=first,
        day="THU",
        start_time="09:00",
        end_time="10:00",
        building="B1",
        floor_wing="F1",
        room="R9",
        instructor="Instructor A",
    )
    second_meeting.building = "New"
    second_meeting.save(update_fields=["building"])
    unscheduled_meeting.delete()
    no_longer_scheduled.registered_count = 9
    no_longer_scheduled.save(update_fields=["registered_count"])
    _section("197", source_tag="other")  # Unreferenced target-only state is excluded.
    removed_orphan.delete()  # Missing global sections never become delete operations.
    scenario_section.course_name = "Changed scenario data"
    scenario_section.save(update_fields=["course_name"])

    target = _snapshot(tmp_path / "target.sqlite3")
    target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    first_payload, artifact, first_digest = _export(baseline, target, tmp_path / "delta-one.json")
    second_payload, second_artifact, second_digest = _export(
        baseline, target, tmp_path / "delta-two.json"
    )

    assert first_payload == second_payload
    assert first_digest == second_digest
    assert artifact == second_artifact
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == baseline_hash
    assert hashlib.sha256(target.read_bytes()).hexdigest() == target_hash
    assert artifact["schema_version"] == "academic_timetable_delta.v1"
    assert artifact["scope"]["student_term_sections"] == {
        "academic_year": "1448",
        "term": "1",
        "source": "scraper_timetable",
        "mode": "replace_complete_set_for_touched_students",
        "untouched_students": "preserve",
    }
    assert artifact["sections"]["removals"] == []
    assert artifact["metadata"]["data_classification"] == "restricted_student_timetable"
    assert artifact["metadata"]["contains_student_identifiers"] is True
    assert artifact["metadata"]["excluded_non_registrar_changes"] == [
        {
            "academic_year": "1448",
            "term": "1",
            "source": "registration_plan_1448_t1",
            "operation": "removed",
            "count": 1,
        }
    ]
    assert artifact["metadata"]["excluded_student_relationship_changes"] == {
        "students_by_reason": {"absent_from_base": 1, "roster_semantics_changed": 1},
        "relationships_added": 2,
        "relationships_removed": 1,
        "action": "preserve_production_and_route_to_separate_roster_sync",
    }

    touched = artifact["student_term_sections"]["touched_students"]
    assert touched == [
        {
            "student_id": stable.student_id,
            "expected_program": "AI",
            "expected_section": "G1",
            "expected_status": "active",
            "base_sections": [{"course_code": "AI", "course_number": "101", "section": "A"}],
            "target_sections": [
                {"course_code": "AI", "course_number": "102", "section": "A"},
                {"course_code": "AI", "course_number": "103", "section": "A"},
            ],
        }
    ]
    artifact_text = first_payload.decode("utf-8")
    assert str(roster_changed.student_id) not in artifact_text
    assert str(target_only.student_id) not in artifact_text
    assert "Must not be exported" not in artifact_text
    for forbidden in ("source_file", "created_at", "updated_at", "scenario_id", "AI998"):
        assert forbidden not in artifact_text

    assert artifact["programs"] == {"additions": [], "updates": [], "removals": []}
    assert artifact["metadata"]["excluded_import_program_churn"] == {
        "added": 1,
        "updated": 0,
        "removed": 1,
        "applied": False,
        "reason": "production_import_and_manual_memberships_are_authoritative",
    }
    assert artifact["metadata"]["observed_program_churn"]["applied"] is False
    assert artifact["meetings"]["updates"] == [
        {
            "before": {
                "course_code": "AI",
                "course_number": "102",
                "section": "A",
                "day": "MON",
                "start_time": "10:00",
                "end_time": "11:00",
                "building": "Old",
                "floor_wing": "F2",
                "room": "R2",
                "instructor": "Instructor B",
            },
            "after": {
                "course_code": "AI",
                "course_number": "102",
                "section": "A",
                "day": "MON",
                "start_time": "10:00",
                "end_time": "11:00",
                "building": "New",
                "floor_wing": "F2",
                "room": "R2",
                "instructor": "Instructor B",
            },
        }
    ]
    unscheduled_upsert = next(
        row for row in artifact["sections"]["upserts"] if row["course_number"] == "104"
    )
    assert unscheduled_upsert["meeting_mode"] == "preserve"
    assert unscheduled_upsert["meetings_complete"] is False
    assert unscheduled_upsert["meetings"] == []
    assert not any(row["course_number"] == "104" for row in artifact["meetings"]["removals"])
    assert not any(row["course_number"] == "197" for row in artifact["sections"]["upserts"])
    assert artifact["base"]["state_sha256"]
    assert artifact["base"]["scoped_state_sha256"]
    assert artifact["base"]["observed_basis_sha256"]
    assert artifact["target"]["state_sha256"]


def test_export_rejects_live_or_non_frozen_inputs_and_existing_output(tmp_path, settings):
    section = _section("201")
    student = Student.objects.create(student_id=710001, program="AI", section="G1", status="active")
    _registrar_link(student.student_id, section)
    baseline = _snapshot(tmp_path / "baseline.sqlite3")
    target = _snapshot(tmp_path / "target.sqlite3")
    output = tmp_path / "delta.json"
    _export(baseline, target, output)

    with pytest.raises(CommandError, match="already exists"):
        call_command("export_timetable_delta", str(baseline), str(target), str(output))
    with pytest.raises(CommandError, match="distinct frozen snapshots"):
        call_command(
            "export_timetable_delta",
            str(baseline),
            str(baseline),
            str(tmp_path / "same.json"),
        )

    data_bearing = Path(f"{baseline}-wal")
    data_bearing.write_bytes(b"not-empty")
    with pytest.raises(CommandError, match="data-bearing WAL"):
        call_command(
            "export_timetable_delta",
            str(baseline),
            str(target),
            str(tmp_path / "unsafe.json"),
        )
    data_bearing.unlink()

    default_name = str(settings.DATABASES["default"]["NAME"])
    if default_name and default_name != ":memory:" and not default_name.startswith("file:"):
        with pytest.raises(CommandError, match="live local database"):
            call_command(
                "export_timetable_delta",
                default_name,
                str(target),
                str(tmp_path / "live.json"),
            )


def test_force_cannot_overwrite_sources_sidecar_collision_or_live_database(tmp_path, monkeypatch):
    section = _section("202")
    student = Student.objects.create(student_id=710002, program="AI", section="G1", status="active")
    _registrar_link(student.student_id, section)
    baseline = _snapshot(tmp_path / "baseline.sqlite3")
    target = _snapshot(tmp_path / "target.sqlite3")
    baseline_before = baseline.read_bytes()
    target_before = target.read_bytes()

    with pytest.raises(CommandError, match="must not overwrite"):
        call_command(
            "export_timetable_delta",
            str(baseline),
            str(target),
            str(baseline),
            "--force",
        )
    assert baseline.read_bytes() == baseline_before

    sidecar_target = tmp_path / "sidecar-collision.json.sha256"
    sidecar_target.write_bytes(target_before)
    with pytest.raises(CommandError, match="must not overwrite"):
        call_command(
            "export_timetable_delta",
            str(baseline),
            str(sidecar_target),
            str(tmp_path / "sidecar-collision.json"),
            "--force",
        )
    assert sidecar_target.read_bytes() == target_before

    live_path = tmp_path / "future-live.sqlite3"
    monkeypatch.setattr(
        export_timetable_delta,
        "_configured_live_sqlite_path",
        lambda: live_path,
    )
    with pytest.raises(CommandError, match="must not overwrite"):
        call_command(
            "export_timetable_delta",
            str(baseline),
            str(target),
            str(live_path),
            "--force",
        )
    assert not live_path.exists()


def test_artifact_is_not_published_when_sidecar_replace_fails(tmp_path, monkeypatch):
    output = tmp_path / "delta.json"
    sidecar = tmp_path / "delta.json.sha256"
    output.write_bytes(b"old artifact")
    sidecar.write_bytes(b"old digest")
    real_replace = export_timetable_delta.os.replace

    def fail_sidecar_replace(source, destination):
        if Path(destination) == sidecar:
            raise OSError("forced sidecar publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(export_timetable_delta.os, "replace", fail_sidecar_replace)
    with pytest.raises(OSError, match="forced sidecar publication failure"):
        export_timetable_delta._write_artifacts(
            output,
            b"new artifact",
            sidecar,
            b"new digest",
            force=True,
        )

    assert output.read_bytes() == b"old artifact"
    assert sidecar.read_bytes() == b"old digest"


def test_state_hash_is_backend_neutral_and_row_order_independent():
    section = {
        "course_code": "AI",
        "course_number": "301",
        "section": "A",
        "source_tag": "test",
        "course_name": "State",
        "available_capacity": None,
        "registered_count": 1,
    }
    meeting_one = {
        "course_code": "AI",
        "course_number": "301",
        "section": "A",
        "day": "SUN",
        "start_time": "09:00",
        "end_time": "10:00",
        "building": "",
        "floor_wing": "",
        "room": "R1",
        "instructor": "",
    }
    meeting_two = {**meeting_one, "day": "MON"}
    first = canonical_state_document(
        sections=[section],
        programs=[],
        meetings=[meeting_one, meeting_two],
        student_term_sections=[],
    )
    second = canonical_state_document(
        sections=[dict(reversed(list(section.items())))],
        programs=[],
        meetings=[meeting_two, meeting_one],
        student_term_sections=[],
    )
    assert state_sha256(first) == state_sha256(second)
