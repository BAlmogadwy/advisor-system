# mypy: disable-error-code="no-untyped-def,arg-type"

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, time
from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper

from core.management.commands.export_release_seed import (
    ALLOWED_MODELS,
    SIGNATURE_ALGORITHM,
    ReleaseSeedJSONEncoder,
    _frozen_sqlite_alias,
    _manifest_signature_value,
    canonical_content_sha256,
)
from core.models import (
    AuditLog,
    Course,
    Instructor,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
    UserScope,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _strong_release_signing_key(monkeypatch):
    monkeypatch.setenv(
        "RELEASE_SEED_SIGNING_KEY",
        "9c4785723dc524554acd065e559452245d0ef08ee24d774ba1c93c15e1fb5db8",
    )


def _frozen_snapshot(path: Path) -> Path:
    path.unlink(missing_ok=True)
    connection.ensure_connection()
    with sqlite3.connect(path) as destination:
        connection.connection.backup(destination)
    return path


def _export(destination: Path, *extra: str) -> tuple[Path, Path, dict, list[dict]]:
    snapshot = _frozen_snapshot(destination.parent / "frozen-release-source.sqlite3")
    source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    stdout = StringIO()
    call_command(
        "export_release_seed",
        str(destination),
        "--sqlite-frozen-copy",
        str(snapshot),
        *extra,
        stdout=stdout,
    )
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == source_hash
    assert not any(Path(f"{snapshot}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    fixture = (
        destination
        if destination.name.endswith(".json.gz")
        else destination / "release-seed.json.gz"
    )
    manifest = fixture.with_name(fixture.name.removesuffix(".json.gz") + ".manifest.json")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    records = json.loads(gzip.decompress(fixture.read_bytes()).decode("utf-8"))
    assert "release records" in stdout.getvalue()
    return fixture, manifest, manifest_data, records


def _record(records: list[dict], model: str, pk: int) -> dict:
    return next(record for record in records if record["model"] == model and record["pk"] == pk)


def test_export_is_allowlisted_filtered_checksummed_and_read_only(tmp_path):
    user_model = get_user_model()
    student_group = Group.objects.create(name="STUDENT")
    privileged_group = Group.objects.create(name="GENERAL_ACADEMIC_ADVISOR")

    student = Student.objects.create(student_id=700001, name="طالب", program="AI")
    course = Course.objects.create(course_code="AI101", description="مدخل", credit_hours=3)
    StudentCourse.objects.create(student=student, course=course, status="studying")

    valid_user = user_model.objects.create_user(username="valid-student")
    valid_user.groups.add(student_group)
    UserScope.objects.create(user=valid_user, student_id=student.student_id)

    stale_user = user_model.objects.create_user(username="stale-student")
    stale_user.groups.add(student_group)
    UserScope.objects.create(user=stale_user, student_id=799999)

    privileged_user = user_model.objects.create_user(username="privileged")
    privileged_user.groups.add(student_group, privileged_group)
    UserScope.objects.create(user=privileged_user, student_id=788888)

    global_section = TermSection.objects.create(
        course_code="AI",
        course_number="101",
        course_key="AI101",
        course_name="AI101",
        section="M1",
    )
    TermSectionProgram.objects.create(term_section=global_section, program="AI")
    TermSectionMeeting.objects.create(
        term_section=global_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=global_section,
    )
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="test")
    scenario_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI",
        course_number="101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=scenario_section,
        day="MON",
        start_time="11:00",
        end_time="12:00",
    )
    AuditLog.objects.create(ts_utc="2026-01-01T00:00:00Z", action="test-only")

    before = {
        "students": Student.objects.count(),
        "users": user_model.objects.count(),
        "scopes": UserScope.objects.count(),
        "sections": TermSection.objects.count(),
        "audit": AuditLog.objects.count(),
    }
    fixture, _, manifest, records = _export(tmp_path / "release.json.gz")

    assert before == {
        "students": Student.objects.count(),
        "users": user_model.objects.count(),
        "scopes": UserScope.objects.count(),
        "sections": TermSection.objects.count(),
        "audit": AuditLog.objects.count(),
    }
    assert manifest["profile"]["allowed_models"] == list(ALLOWED_MODELS)
    assert manifest["profile"]["natural_foreign_keys"] is True
    assert manifest["profile"]["natural_primary_keys"] is False
    assert "core.auditlog" in manifest["profile"]["excluded_models"]
    assert "core.timetablescenario" in manifest["profile"]["excluded_models"]
    assert "telegram_gateway.telegramlink" in manifest["profile"]["excluded_models"]
    assert {record["model"] for record in records} <= set(ALLOWED_MODELS)

    exported_users = {record["pk"] for record in records if record["model"] == "auth.user"}
    exported_scopes = {record["pk"] for record in records if record["model"] == "core.userscope"}
    assert valid_user.pk in exported_users
    assert valid_user.pk in exported_scopes
    assert stale_user.pk not in exported_users
    assert stale_user.pk not in exported_scopes
    assert privileged_user.pk in exported_users
    assert privileged_user.pk in exported_scopes

    exported_sections = {
        record["pk"] for record in records if record["model"] == "core.termsection"
    }
    assert exported_sections == {global_section.pk}
    assert scenario_section.pk not in {
        record["fields"]["term_section"]
        for record in records
        if record["model"] == "core.termsectionmeeting"
    }

    # PKs are preserved, while the User -> Group relation uses Group's natural key.
    valid_user_record = _record(records, "auth.user", valid_user.pk)
    assert valid_user_record["pk"] == valid_user.pk
    assert ["STUDENT"] in valid_user_record["fields"]["groups"]

    fixture_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert manifest["fixture"]["sha256"] == fixture_hash
    assert manifest["fixture"]["canonical_content_sha256"] == canonical_content_sha256(records)
    assert manifest["fixture"]["total_records"] == len(records)
    assert manifest["fixture"]["per_model"]["core.student"]["exported"] == 1
    assert manifest["fixture"]["per_model"]["core.termsection"] == {
        "source": 2,
        "exported": 1,
        "filtered": 1,
    }
    assert manifest["fixture"]["filters"] == {
        "invalid_student_scopes_removed": 1,
        "otherwise_unscoped_student_users_removed": 1,
    }
    assert manifest["runtime"]["python"]
    assert manifest["runtime"]["django"]
    assert "commit" in manifest["git"] and "dirty" in manifest["git"]
    assert manifest["migrations"]["leaf_nodes"]
    assert manifest["source_database"]["vendor"] == "sqlite"
    assert manifest["signature"]["algorithm"] == SIGNATURE_ALGORITHM
    assert manifest["signature"]["value"] == _manifest_signature_value(manifest)


def test_existing_release_pair_requires_force_and_is_replaced_together(tmp_path):
    Student.objects.create(student_id=700001, name="First")
    destination = tmp_path / "artifacts"
    fixture, manifest, _, _ = _export(destination)
    original_fixture = fixture.read_bytes()
    original_manifest = manifest.read_bytes()

    Student.objects.create(student_id=700002, name="Second")
    snapshot = _frozen_snapshot(tmp_path / "second-frozen-source.sqlite3")
    with pytest.raises(CommandError, match="already exists"):
        call_command(
            "export_release_seed",
            str(destination),
            "--sqlite-frozen-copy",
            str(snapshot),
        )
    assert fixture.read_bytes() == original_fixture
    assert manifest.read_bytes() == original_manifest

    _, _, replaced_manifest, records = _export(destination, "--force")
    assert replaced_manifest["fixture"]["per_model"]["core.student"]["exported"] == 2
    assert len([record for record in records if record["model"] == "core.student"]) == 2


def test_destination_inside_repository_is_refused_without_writing():
    destination = Path(settings.BASE_DIR) / "forbidden-release-seed.json.gz"
    destination.unlink(missing_ok=True)
    with pytest.raises(CommandError, match="outside the project"):
        call_command("export_release_seed", str(destination))
    assert not destination.exists()


def test_writable_sqlite_source_is_refused_without_writing(tmp_path):
    destination = tmp_path / "refused-release.json.gz"
    with pytest.raises(CommandError, match="mode=ro&immutable=1"):
        call_command("export_release_seed", str(destination))
    assert not destination.exists()


def test_frozen_sqlite_hard_link_to_live_database_is_refused(tmp_path, monkeypatch):
    live_database = _frozen_snapshot(tmp_path / "live-default.sqlite3")
    linked_snapshot = tmp_path / "linked-snapshot.sqlite3"
    try:
        os.link(live_database, linked_snapshot)
    except OSError as exc:
        pytest.skip(f"filesystem hard links are unavailable: {exc}")

    monkeypatch.setitem(connection.settings_dict, "NAME", str(live_database))
    with pytest.raises(CommandError, match="separate frozen SQLite copy"):
        with _frozen_sqlite_alias(linked_snapshot):
            raise AssertionError("hard-linked live database was opened")


def test_insecure_development_signing_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("RELEASE_SEED_SIGNING_KEY", "django-insecure-dev-only-change-me")
    snapshot = _frozen_snapshot(tmp_path / "insecure-key-source.sqlite3")
    destination = tmp_path / "insecure-key-release.json.gz"

    with pytest.raises(CommandError, match="strong dedicated RELEASE_SEED_SIGNING_KEY"):
        call_command(
            "export_release_seed",
            str(destination),
            "--sqlite-frozen-copy",
            str(snapshot),
        )
    assert not destination.exists()


def test_frozen_alias_bypasses_wal_hook_without_source_side_effects(tmp_path):
    snapshot = _frozen_snapshot(tmp_path / "wal-hook-proof.sqlite3")
    before_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    with _frozen_sqlite_alias(snapshot) as (_, frozen_connection):
        with frozen_connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM students")
            assert cursor.fetchone()[0] >= 0
        assert frozen_connection.settings_dict["RELEASE_SEED_READ_ONLY"] is True

    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before_hash
    assert not any(Path(f"{snapshot}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_regular_sqlite_connection_still_enables_wal(tmp_path):
    database = dict(connection.settings_dict)
    database.update(
        {
            "NAME": str(tmp_path / "regular.sqlite3"),
            "OPTIONS": {},
        }
    )
    database.pop("RELEASE_SEED_READ_ONLY", None)
    wrapper = SQLiteDatabaseWrapper(database, alias="wal_hook_regular_probe")
    try:
        wrapper.ensure_connection()
        with wrapper.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            assert cursor.fetchone()[0].lower() == "wal"
    finally:
        wrapper.close()


def test_canonical_digest_normalises_m2m_order_but_detects_scalar_changes():
    base = {
        "model": "auth.group",
        "pk": 1,
        "fields": {
            "name": "STUDENT",
            "permissions": [
                ["add_student", "core", "student"],
                ["view_student", "core", "student"],
            ],
        },
    }
    reordered = json.loads(json.dumps(base))
    reordered["fields"]["permissions"].reverse()
    changed = json.loads(json.dumps(base))
    changed["fields"]["name"] = "ADVISOR"

    assert canonical_content_sha256([base]) == canonical_content_sha256([reordered])
    assert canonical_content_sha256([base]) != canonical_content_sha256([changed])


def test_release_seed_records_preserve_datetime_and_time_microseconds(tmp_path):
    stamp = datetime(2026, 8, 14, 9, 10, 11, 123456, tzinfo=UTC)
    instructor = Instructor.objects.create(
        full_name="Precision Test",
        normalised_name="precision test",
    )
    Instructor.objects.filter(pk=instructor.pk).update(created_at=stamp, updated_at=stamp)

    _, _, _, records = _export(tmp_path / "precision-release.json.gz")
    record = _record(records, "core.instructor", instructor.pk)
    assert record["fields"]["created_at"] == "2026-08-14T09:10:11.123456Z"
    assert record["fields"]["updated_at"] == "2026-08-14T09:10:11.123456Z"

    encoded_time = json.dumps(
        time(9, 10, 11, 654321),
        cls=ReleaseSeedJSONEncoder,
    )
    assert encoded_time == '"09:10:11.654321"'
