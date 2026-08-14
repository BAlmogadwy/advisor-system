# mypy: disable-error-code="no-untyped-def,arg-type"

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper
from django.db.migrations.recorder import MigrationRecorder
from django.db.models.query import QuerySet
from django.db.models.signals import m2m_changed, post_save, pre_save

from core.management.commands import import_release_seed
from core.management.commands.export_release_seed import (
    _serialise_records,
    canonical_content_sha256,
    sign_manifest,
)
from core.management.commands.import_release_seed import (
    CONFIRMATION_VALUE,
    KILL_SWITCH_ENV,
    KILL_SWITCH_VALUE,
)
from core.models import (
    AuditLog,
    Course,
    CourseInstructor,
    Instructor,
    Student,
    StudentCourse,
    UserScope,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _strong_release_signing_key(monkeypatch):
    monkeypatch.setenv(
        "RELEASE_SEED_SIGNING_KEY",
        "9c4785723dc524554acd065e559452245d0ef08ee24d774ba1c93c15e1fb5db8",
    )


def _source_records() -> tuple[Student, Course]:
    student = Student.objects.create(
        student_id=700001,
        name="Seed Student",
        program="AI",
    )
    course = Course.objects.create(
        course_code="AI101",
        description="Introduction",
        credit_hours=3,
    )
    StudentCourse.objects.create(student=student, course=course, status="studying")
    return student, course


def _export(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "frozen-release-source.sqlite3"
    snapshot.unlink(missing_ok=True)
    connection.ensure_connection()
    with sqlite3.connect(snapshot) as destination:
        connection.connection.backup(destination)
    fixture = tmp_path / "release-seed.json.gz"
    call_command(
        "export_release_seed",
        str(fixture),
        "--sqlite-frozen-copy",
        str(snapshot),
        stdout=StringIO(),
    )
    manifest = tmp_path / "release-seed.manifest.json"
    return fixture, manifest


@contextmanager
def _sqlite_rehearsal_target(path: Path):
    alias = "release_seed_import_rehearsal"
    path.unlink(missing_ok=True)
    connection.ensure_connection()
    with sqlite3.connect(path) as destination:
        connection.connection.backup(destination)

    database = dict(connection.settings_dict)
    database.update({"NAME": str(path.resolve()), "OPTIONS": {}})
    wrapper = SQLiteDatabaseWrapper(database, alias=alias)
    wrapper.ensure_connection()
    connections.databases[alias] = database
    setattr(connections._connections, alias, wrapper)  # type: ignore[attr-defined]
    try:
        yield alias
    finally:
        wrapper.close()
        connections.databases.pop(alias, None)
        try:
            delattr(connections._connections, alias)  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _replace(
    fixture: Path,
    manifest: Path,
    *,
    confirmation: str = CONFIRMATION_VALUE,
    allow_sqlite: bool = True,
    database: str,
) -> str:
    args = [
        "import_release_seed",
        str(fixture),
        "--manifest",
        str(manifest),
        "--confirm-replace-target-database",
        confirmation,
        "--database",
        database,
    ]
    if allow_sqlite:
        args.append("--allow-sqlite-rehearsal")
    stdout = StringIO()
    call_command(*args, stdout=stdout)
    return stdout.getvalue()


def _target_sentinel() -> Student:
    Student.objects.all().delete()
    Course.objects.all().delete()
    target = Student.objects.create(student_id=799999, name="Target Sentinel", program="DS")
    AuditLog.objects.create(ts_utc="2026-01-01T00:00:00Z", action="target-sentinel")
    return target


def _rewrite_manifest(path: Path, update, *, resign: bool = True) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    update(manifest)
    if resign:
        sign_manifest(manifest)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rewrite_fixture(
    path: Path,
    manifest_path: Path,
    update,
    *,
    resign: bool = True,
    recompute_content: bool = True,
) -> None:
    records = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    update(records)
    payload = gzip.compress(
        (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        compresslevel=9,
        mtime=0,
    )
    path.write_bytes(payload)

    def update_manifest(manifest):
        manifest["fixture"]["size_bytes"] = len(payload)
        manifest["fixture"]["sha256"] = hashlib.sha256(payload).hexdigest()
        if recompute_content:
            manifest["fixture"]["canonical_content_sha256"] = canonical_content_sha256(records)

    _rewrite_manifest(manifest_path, update_manifest, resign=resign)


def test_bad_confirmation_and_disabled_kill_switch_make_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="exact destructive confirmation"):
            _replace(fixture, manifest, confirmation="yes", database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()

        monkeypatch.delenv(KILL_SWITCH_ENV)
        with pytest.raises(CommandError, match="kill-switch"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_sqlite_requires_explicit_rehearsal_flag(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="SQLite is allowed only"):
            _replace(fixture, manifest, allow_sqlite=False, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()


def test_sqlite_rehearsal_unconditionally_rejects_live_default_database(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    with pytest.raises(CommandError, match="non-default disposable database alias"):
        _replace(fixture, manifest, database="default")

    assert Student.objects.filter(pk=target.pk, program="DS").exists()
    assert AuditLog.objects.filter(action="target-sentinel").exists()


def test_sqlite_rehearsal_rejects_hard_link_to_default_and_identity_probe_failure(
    tmp_path, monkeypatch
):
    live_database = tmp_path / "live-default.sqlite3"
    with sqlite3.connect(live_database) as database:
        database.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")
    linked_target = tmp_path / "linked-rehearsal.sqlite3"
    try:
        os.link(live_database, linked_target)
    except OSError as exc:
        pytest.skip(f"filesystem hard links are unavailable: {exc}")

    fake_connections = {
        "default": SimpleNamespace(settings_dict={"NAME": str(live_database)}),
        "release": SimpleNamespace(
            vendor="sqlite",
            settings_dict={"NAME": str(linked_target)},
        ),
    }
    monkeypatch.setattr(import_release_seed, "connections", fake_connections)
    with pytest.raises(CommandError, match="live default database"):
        import_release_seed._validate_sqlite_rehearsal_target("release")

    def identity_probe_failed(*args, **kwargs):
        raise OSError("identity unavailable")

    monkeypatch.setattr(import_release_seed.os.path, "samefile", identity_probe_failed)
    with pytest.raises(CommandError, match="identity could not be safely verified"):
        import_release_seed._validate_sqlite_rehearsal_target("release")


def test_bad_checksum_makes_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    fixture.write_bytes(fixture.read_bytes() + b"corruption")

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="fixture size|fixture checksum"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_tampered_fixture_and_matching_unsigned_manifest_changes_make_no_writes(
    tmp_path, monkeypatch
):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    def change_scalar(records):
        student = next(record for record in records if record["model"] == "core.student")
        student["fields"]["program"] = "DS"

    _rewrite_fixture(fixture, manifest, change_scalar, resign=False)
    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="manifest signature does not match"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_different_export_and_import_signing_keys_make_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    monkeypatch.setenv(
        "RELEASE_SEED_SIGNING_KEY",
        "e71b5fee2f162ff10b78ef01bb760734609d34bd07696833ef0451a216b05dcc",
    )

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="same ephemeral RELEASE_SEED_SIGNING_KEY"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()


def test_bad_profile_makes_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    _rewrite_manifest(manifest, lambda data: data["profile"].update(name="wrong-profile"))

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="profile does not match"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_bad_per_model_counts_make_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    def corrupt(data):
        counts = data["fixture"]["per_model"]["core.student"]
        counts["exported"] += 1

    _rewrite_manifest(manifest, corrupt)
    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="per-model counts|total record count"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_bad_canonical_content_digest_makes_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    _rewrite_manifest(
        manifest,
        lambda data: data["fixture"].update(canonical_content_sha256="0" * 64),
    )

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="canonical content"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_unknown_fixture_model_makes_no_writes(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    _rewrite_fixture(
        fixture,
        manifest,
        lambda records: records[0].update(model="core.unknown"),
        recompute_content=False,
    )

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="unknown or disallowed model"):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


def test_success_replaces_allowed_data_and_removes_runtime_rows(tmp_path, monkeypatch):
    source_student, _ = _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)
    migrations_before = set(MigrationRecorder.Migration.objects.values_list("app", "name"))

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        output = _replace(fixture, manifest, database=alias)

        assert "Imported" in output
        assert "across 18 models" in output
        assert Student.objects.using(alias).filter(pk=source_student.pk, program="AI").exists()
        assert not Student.objects.using(alias).filter(pk=target.pk).exists()
        assert Course.objects.using(alias).filter(course_code="AI101").exists()
        assert StudentCourse.objects.using(alias).filter(student_id=source_student.pk).exists()
        assert AuditLog.objects.using(alias).count() == 0
        assert (
            set(MigrationRecorder.Migration.objects.using(alias).values_list("app", "name"))
            == migrations_before
        )


def test_success_round_trips_timestamp_microseconds_exactly(tmp_path, monkeypatch):
    _source_records()
    stamp = datetime(2026, 8, 14, 9, 10, 11, 123456, tzinfo=UTC)
    target_stamp = datetime(2026, 8, 14, 9, 10, 11, 999999, tzinfo=UTC)
    instructor = Instructor.objects.create(
        full_name="Precision Test",
        normalised_name="precision test",
    )
    Instructor.objects.filter(pk=instructor.pk).update(created_at=stamp, updated_at=stamp)
    fixture, manifest = _export(tmp_path)

    _target_sentinel()
    Instructor.objects.filter(pk=instructor.pk).update(
        created_at=target_stamp,
        updated_at=target_stamp,
    )
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        _replace(fixture, manifest, database=alias)
        loaded = Instructor.objects.using(alias).get(pk=instructor.pk)
        assert loaded.created_at == stamp
        assert loaded.updated_at == stamp


def test_postgres_model_load_order_is_fk_topological_and_fixture_order_independent():
    order = import_release_seed._model_load_order()
    positions = {label: index for index, label in enumerate(order)}

    assert set(order) == set(import_release_seed.ALLOWED_MODELS)
    assert positions["core.instructor"] < positions["core.courseinstructor"]
    assert positions["core.electivecourse"] < positions["core.electivetermmapping"]
    assert positions["core.student"] < positions["core.studentcourse"]
    assert positions["core.course"] < positions["core.studentcourse"]
    assert positions["core.termsection"] < positions["core.termsectionprogram"]
    assert positions["core.termsection"] < positions["core.termsectionmeeting"]
    assert positions["core.termsection"] < positions["core.studenttermsection"]
    assert positions["auth.user"] < positions["core.userscope"]


def test_raw_batch_insert_uses_bounded_multirow_raw_statements(monkeypatch):
    calls: list[dict[str, Any]] = []

    def record_insert(
        queryset,
        objects,
        *,
        fields,
        returning_fields,
        raw,
        using,
        **kwargs,
    ):
        calls.append(
            {
                "size": len(objects),
                "field_names": [field.attname for field in fields],
                "returning_fields": returning_fields,
                "raw": raw,
                "using": using,
            }
        )
        return []

    monkeypatch.setattr(QuerySet, "_insert", record_insert)
    objects = [
        StudentCourse(
            id=index,
            student_id=700001,
            course_id=1,
            status=StudentCourse.Status.STUDYING,
        )
        for index in range(1, 4_502)
    ]

    statements = import_release_seed._raw_batch_insert(
        "default",
        StudentCourse,
        objects,
        require_primary_keys=True,
    )

    assert statements == 3
    assert [call["size"] for call in calls] == [2_000, 2_000, 501]
    assert all(call["raw"] is True for call in calls)
    assert all(call["returning_fields"] == [] for call in calls)
    assert all(call["using"] == "default" for call in calls)
    assert all("id" in call["field_names"] for call in calls)
    assert all(not item._state.adding and item._state.db == "default" for item in objects)


def test_raw_batch_insert_preserves_serialized_auto_now_values():
    stamp = datetime(2026, 8, 14, 10, 11, 12, 654321, tzinfo=UTC)
    instructor = Instructor(
        id=987654,
        full_name="Raw Timestamp",
        normalised_name="raw timestamp",
        created_at=stamp,
        updated_at=stamp,
    )

    import_release_seed._raw_batch_insert(
        "default",
        Instructor,
        [instructor],
        require_primary_keys=True,
    )

    loaded = Instructor.objects.get(pk=instructor.pk)
    assert loaded.created_at == stamp
    assert loaded.updated_at == stamp


def test_fixture_validation_rejects_duplicate_m2m_values_before_loading():
    field = Group._meta.get_field("permissions")

    with pytest.raises(CommandError, match="duplicate many-to-many"):
        import_release_seed._validate_field_value(
            field,
            [
                ["view_student", "core", "student"],
                ["view_student", "core", "student"],
            ],
        )


def test_batched_loader_preserves_forward_fk_m2m_counts_and_digest(tmp_path):
    student, course = _source_records()
    instructor = Instructor.objects.create(
        full_name="Forward Instructor",
        normalised_name="forward instructor",
    )
    course_link = CourseInstructor.objects.create(
        program="AI",
        course_code=course.course_code,
        section="M",
        instructor=instructor,
    )
    group = Group.objects.create(name="BATCHED_RELEASE_STUDENT")
    permission = Permission.objects.get(
        content_type__app_label="core",
        codename="view_student",
    )
    group.permissions.add(permission)
    user = get_user_model().objects.create_user(username="batched-release-user")
    user.groups.add(group)
    user.user_permissions.add(permission)
    UserScope.objects.create(user=user, student_id=student.pk)

    fixture, manifest_path = _export(tmp_path)
    (
        manifest,
        expected_counts,
        fixture_records,
        total_records,
        expected_digest,
    ) = import_release_seed._validate_artifact(fixture, manifest_path)
    assert total_records == sum(expected_counts.values())
    # The loader must derive its own schema order rather than trust fixture order.
    fixture_records = list(reversed(fixture_records))

    signal_events: list[str] = []

    def model_signal(sender, **kwargs):
        signal_events.append(sender._meta.label_lower)

    def relation_signal(sender, **kwargs):
        signal_events.append(sender._meta.label_lower)

    pre_save.connect(model_signal, sender=Instructor, dispatch_uid="release-seed-raw-pre")
    post_save.connect(model_signal, sender=Instructor, dispatch_uid="release-seed-raw-post")
    m2m_changed.connect(relation_signal, dispatch_uid="release-seed-raw-m2m")

    try:
        with _sqlite_rehearsal_target(tmp_path / "batched-target.sqlite3") as alias:
            migrations_before = set(
                MigrationRecorder.Migration.objects.using(alias).values_list("app", "name")
            )
            with transaction.atomic(using=alias):
                import_release_seed._flush_target_database(alias)
                regenerated_permission = Permission.objects.using(alias).get(
                    content_type__app_label="core",
                    codename="view_student",
                )
                target_permission_pk = permission.pk + 100_000
                Permission.objects.using(alias).filter(pk=regenerated_permission.pk).delete()
                Permission.objects.using(alias).create(
                    pk=target_permission_pk,
                    name=regenerated_permission.name,
                    codename=regenerated_permission.codename,
                    content_type_id=regenerated_permission.content_type_id,
                )
                statements = import_release_seed._load_verified_fixture_postgres(
                    alias,
                    fixture_records,
                )
                import_release_seed._validate_loaded_database(
                    alias,
                    expected_counts,
                    expected_digest,
                    migrations_before,
                )

            nonempty_models = sum(count > 0 for count in expected_counts.values())
            assert statements <= nonempty_models + 3
            assert (
                CourseInstructor.objects.using(alias).get(pk=course_link.pk).instructor_id
                == instructor.pk
            )
            loaded_group = Group.objects.using(alias).get(pk=group.pk)
            assert set(
                loaded_group.permissions.values_list("content_type__app_label", "codename")
            ) == {("core", "view_student")}
            loaded_user = get_user_model().objects.using(alias).get(pk=user.pk)
            assert set(loaded_user.groups.values_list("pk", flat=True)) == {group.pk}
            assert set(
                loaded_user.user_permissions.values_list("content_type__app_label", "codename")
            ) == {("core", "view_student")}
            loaded_permission = Permission.objects.using(alias).get(
                content_type__app_label="core",
                codename="view_student",
            )
            assert loaded_permission.pk == target_permission_pk
            assert loaded_permission.pk != permission.pk
            assert UserScope.objects.using(alias).get(pk=user.pk).student_id == student.pk
            loaded_records, _ = _serialise_records(alias)
            assert canonical_content_sha256(loaded_records) == expected_digest
            assert manifest["fixture"]["canonical_content_sha256"] == expected_digest
    finally:
        pre_save.disconnect(sender=Instructor, dispatch_uid="release-seed-raw-pre")
        post_save.disconnect(sender=Instructor, dispatch_uid="release-seed-raw-post")
        m2m_changed.disconnect(dispatch_uid="release-seed-raw-m2m")

    assert signal_events == []


def test_post_load_validation_failure_rolls_back_old_target(tmp_path, monkeypatch):
    _source_records()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    def fail_validation(*args, **kwargs):
        raise CommandError("forced post-load validation failure")

    monkeypatch.setattr(import_release_seed, "_validate_loaded_database", fail_validation)
    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(CommandError, match="transaction was rolled back"):
            _replace(fixture, manifest, database=alias)

        assert Student.objects.using(alias).filter(pk=target.pk, program="DS").exists()
        assert not Student.objects.using(alias).filter(pk=700001).exists()
        assert Course.objects.using(alias).count() == 0
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()


@pytest.mark.parametrize(
    ("maximum_key", "last_value", "is_called", "increment", "expected"),
    [
        (None, 1, False, 1, True),
        (10, 10, True, 1, True),
        (10, 11, False, 1, True),
        (10, 10, False, 1, False),
        (10, 9, True, 1, False),
        (10, 10, True, 0, False),
    ],
)
def test_sequence_high_water_check_is_side_effect_free_and_collision_safe(
    maximum_key, last_value, is_called, increment, expected
):
    assert (
        import_release_seed._sequence_position_is_safe(
            maximum_key=maximum_key,
            last_value=last_value,
            is_called=is_called,
            increment=increment,
        )
        is expected
    )


def test_postgres_sequence_reset_uses_visible_transactional_restart(monkeypatch):
    class Cursor:
        def __init__(self):
            self.executions: list[tuple[object, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params=None):
            self.executions.append((query, params))

        def fetchall(self):
            return [
                (
                    "public",
                    "courses",
                    "course_id",
                    "public",
                    "courses_course_id_seq",
                    1,
                    1,
                    1,
                    2_147_483_647,
                )
            ]

        def fetchone(self):
            return (42,)

    cursor = Cursor()

    class Operations:
        def sequence_reset_sql(self, *args, **kwargs):
            raise AssertionError("PostgreSQL must not use non-transactional setval() SQL")

    fake_connection = SimpleNamespace(
        vendor="postgresql",
        ops=Operations(),
        cursor=lambda: cursor,
    )
    monkeypatch.setattr(import_release_seed, "connections", {"release": fake_connection})

    import_release_seed._reset_loaded_sequences("release", [Course])

    assert len(cursor.executions) == 3
    catalog_query, catalog_params = cursor.executions[0]
    assert "pg_table_is_visible(table_rel.oid)" in str(catalog_query)
    assert catalog_params == [["courses"]]
    restart_query, restart_params = cursor.executions[-1]
    assert "ALTER SEQUENCE" in str(restart_query)
    assert "RESTART WITH" in str(restart_query)
    assert "Literal(43)" in str(restart_query)
    assert restart_params is None
    assert all("setval" not in str(query).lower() for query, _ in cursor.executions)


def test_postgres_checkout_binding_requires_release_commit_and_clean_tracked_state(
    monkeypatch, settings
):
    expected_commit = "a" * 40
    fake_connection = SimpleNamespace(vendor="postgresql")
    monkeypatch.setattr(import_release_seed, "connections", {"release": fake_connection})
    settings.DEBUG = False

    def clean_run(command, **kwargs):
        output = f"{expected_commit}\n" if command[1:3] == ["rev-parse", "HEAD"] else ""
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(import_release_seed.subprocess, "run", clean_run)
    manifest = {"git": {"commit": expected_commit, "dirty": False}}
    import_release_seed._validate_production_checkout("release", manifest, allow_rehearsal=False)

    manifest["git"]["dirty"] = True
    with pytest.raises(CommandError, match="clean production commit"):
        import_release_seed._validate_production_checkout(
            "release", manifest, allow_rehearsal=False
        )

    manifest["git"]["dirty"] = False

    def dirty_run(command, **kwargs):
        output = f"{expected_commit}\n" if command[1:3] == ["rev-parse", "HEAD"] else " M file"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(import_release_seed.subprocess, "run", dirty_run)
    with pytest.raises(CommandError, match="checkout does not match"):
        import_release_seed._validate_production_checkout(
            "release", manifest, allow_rehearsal=False
        )


def test_postgres_checkout_binding_rejects_debug_mode(monkeypatch, settings):
    monkeypatch.setattr(
        import_release_seed,
        "connections",
        {"release": SimpleNamespace(vendor="postgresql")},
    )
    settings.DEBUG = True
    with pytest.raises(CommandError, match="production settings"):
        import_release_seed._validate_production_checkout(
            "release",
            {"git": {"commit": "a" * 40, "dirty": False}},
            allow_rehearsal=False,
        )


class _PostgresContextCursor:
    def __init__(self, database_name: str, current_user: str, other_sessions: int):
        self.database_name = database_name
        self.current_user = current_user
        self.other_sessions = other_sessions
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.query = str(query)

    def fetchone(self):
        if "pg_stat_activity" in self.query:
            return (self.other_sessions,)
        return (self.database_name, self.current_user)


class _PostgresContextConnection:
    vendor = "postgresql"

    def __init__(
        self,
        database_name: str,
        *,
        host: str = "dpg-advisor.render.com",
        current_user: str = "advisor_owner",
        other_sessions: int = 0,
    ):
        self.database_name = database_name
        self.current_user = current_user
        self.other_sessions = other_sessions
        self.settings_dict = {"HOST": host}

    def cursor(self):
        return _PostgresContextCursor(
            self.database_name,
            self.current_user,
            self.other_sessions,
        )


def _postgres_target_kwargs(
    database_name: str,
    *,
    host: str = "dpg-advisor.render.com",
    current_user: str = "advisor_owner",
) -> dict[str, object]:
    return {
        "expected_database_name": database_name,
        "confirmed_database_name": database_name,
        "expected_host": host,
        "confirmed_current_user": current_user,
        "writers_confirmation": import_release_seed.WRITERS_SUSPENDED_VALUE,
        "allow_rehearsal": False,
    }


def test_postgres_target_rejects_wrong_database_name_and_connected_writers(monkeypatch):
    actual = import_release_seed.PRODUCTION_DATABASE_NAME
    connection = _PostgresContextConnection(actual)
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    with pytest.raises(CommandError, match="both database-name confirmations"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **{
                **_postgres_target_kwargs(actual),
                "expected_database_name": "wrong_database",
            },
        )

    connection.database_name = "nonproduction_database"
    with pytest.raises(CommandError, match="configured production database"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **_postgres_target_kwargs("nonproduction_database"),
        )

    connection.database_name = actual
    connection.other_sessions = 1
    with pytest.raises(CommandError, match="client sessions are still connected"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **_postgres_target_kwargs(actual),
        )


def test_postgres_target_binds_normalized_host_and_current_user(monkeypatch):
    actual = import_release_seed.PRODUCTION_DATABASE_NAME
    connection = _PostgresContextConnection(actual, host="DPG-ADVISOR.RENDER.COM.")
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    import_release_seed._validate_postgres_target_context(
        "release", **_postgres_target_kwargs(actual)
    )

    with pytest.raises(CommandError, match="host confirmation"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **_postgres_target_kwargs(actual, host="different.render.com"),
        )
    with pytest.raises(CommandError, match="user confirmation"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **_postgres_target_kwargs(actual, current_user="different_owner"),
        )


@pytest.mark.parametrize(
    ("database_name", "host"),
    [
        (import_release_seed.PRODUCTION_DATABASE_NAME, "127.0.0.1"),
        ("advisor_rehearsal", "dpg-advisor.render.com"),
    ],
)
def test_postgres_rehearsal_rejects_production_database_identity(monkeypatch, database_name, host):
    connection = _PostgresContextConnection(database_name, host=host)
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})
    kwargs = _postgres_target_kwargs(database_name, host=host)
    kwargs["allow_rehearsal"] = True

    with pytest.raises(CommandError, match="rehearsal cannot use the production"):
        import_release_seed._validate_postgres_target_context("release", **kwargs)


def test_postgres_target_requires_strong_writers_confirmation(monkeypatch):
    actual = import_release_seed.PRODUCTION_DATABASE_NAME
    monkeypatch.setattr(
        import_release_seed,
        "connections",
        {"release": _PostgresContextConnection(actual)},
    )
    with pytest.raises(CommandError, match="writers suspension confirmation"):
        import_release_seed._validate_postgres_target_context(
            "release",
            **{
                **_postgres_target_kwargs(actual),
                "writers_confirmation": "yes",
            },
        )


class _FenceMaintenanceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        rendered = str(query)
        if "current_database(), current_user" in rendered:
            self.connection.owner.events.append("identity")
            self.result = (
                self.connection.owner.maintenance_database,
                self.connection.owner.maintenance_user,
            )
        elif "ALLOW_CONNECTIONS false" in rendered:
            self.connection.events.append("disable")
            self.connection.allow_connections = False
            if self.connection.owner.fail_disable:
                raise RuntimeError("disable response lost")
        elif "ALLOW_CONNECTIONS true" in rendered:
            self.connection.events.append("restore")
            if self.connection.owner.fail_restore:
                raise RuntimeError("restore failed")
            self.connection.allow_connections = True
        elif "datallowconn" in rendered:
            self.connection.events.append("verify")
            self.result = (self.connection.allow_connections,)

    def fetchone(self):
        return self.result


class _FenceMaintenanceConnection:
    def __init__(self, owner):
        self.owner = owner
        self.events = owner.events
        self.allow_connections = True
        self._autocommit = False
        self.closed = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._autocommit = value
        self.events.append("maintenance-autocommit")

    def cursor(self):
        return _FenceMaintenanceCursor(self)

    def close(self):
        if not self.closed:
            self.events.append("close")
            self.closed = True


class _FenceDatabaseDriver:
    def __init__(self, owner):
        self.owner = owner

    def connect(self, **params):
        self.owner.events.append("connect")
        self.owner.connect_params = params
        if self.owner.fail_connect:
            raise RuntimeError("connect failed")
        connection = _FenceMaintenanceConnection(self.owner)
        self.owner.maintenance_connection = connection
        return connection


class _FenceConnection:
    vendor = "postgresql"

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        fail_disable: bool = False,
        fail_restore: bool = False,
        maintenance_database: str = "postgres",
        maintenance_user: str = "advisor_owner",
    ):
        self.events: list[str] = []
        self.fail_connect = fail_connect
        self.fail_disable = fail_disable
        self.fail_restore = fail_restore
        self.maintenance_database = maintenance_database
        self.maintenance_user = maintenance_user
        self.maintenance_connection: _FenceMaintenanceConnection | None = None
        self.connect_params: dict[str, object] | None = None
        self.connection_params = {
            "dbname": import_release_seed.PRODUCTION_DATABASE_NAME,
            "host": "dpg-advisor.render.com",
            "user": "advisor_owner",
            "password": "test-only-password",
            "sslmode": "require",
        }
        self.Database = _FenceDatabaseDriver(self)

    def get_autocommit(self):
        return True

    def get_connection_params(self):
        return dict(self.connection_params)

    def cursor(self):
        raise AssertionError("connection fence SQL must not use the target connection")


def test_postgres_connection_fence_orders_and_restores_on_body_failure(monkeypatch):
    connection = _FenceConnection()
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    with pytest.raises(RuntimeError, match="body failed"):
        with import_release_seed._postgres_connection_fence(
            "release",
            database_name=import_release_seed.PRODUCTION_DATABASE_NAME,
            confirmed_current_user="advisor_owner",
            enabled=True,
            confirmation=import_release_seed.CONNECTION_FENCE_CONFIRMATION_VALUE,
        ):
            connection.events.append("body")
            raise RuntimeError("body failed")

    assert connection.events == [
        "connect",
        "maintenance-autocommit",
        "identity",
        "disable",
        "verify",
        "body",
        "restore",
        "verify",
        "close",
    ]
    assert connection.connect_params == {
        **connection.connection_params,
        "dbname": import_release_seed.POSTGRES_MAINTENANCE_DATABASE,
    }
    assert connection.connection_params["dbname"] == import_release_seed.PRODUCTION_DATABASE_NAME
    assert connection.maintenance_connection is not None
    assert connection.maintenance_connection.allow_connections is True
    assert connection.maintenance_connection.closed is True


def test_postgres_connection_fence_requires_exact_gate_and_reports_restore_failure(
    monkeypatch,
):
    connection = _FenceConnection()
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    with pytest.raises(CommandError, match="connection-fence confirmation"):
        with import_release_seed._postgres_connection_fence(
            "release",
            database_name=import_release_seed.PRODUCTION_DATABASE_NAME,
            confirmed_current_user="advisor_owner",
            enabled=True,
            confirmation="yes",
        ):
            raise AssertionError("unreachable")
    assert connection.events == []

    connection.fail_restore = True
    with pytest.raises(CommandError, match="Emergency action"):
        with import_release_seed._postgres_connection_fence(
            "release",
            database_name=import_release_seed.PRODUCTION_DATABASE_NAME,
            confirmed_current_user="advisor_owner",
            enabled=True,
            confirmation=import_release_seed.CONNECTION_FENCE_CONFIRMATION_VALUE,
        ):
            pass
    assert connection.events == [
        "connect",
        "maintenance-autocommit",
        "identity",
        "disable",
        "verify",
        "restore",
        "close",
    ]
    assert connection.maintenance_connection is not None
    assert connection.maintenance_connection.closed is True


def test_postgres_connection_fence_restores_after_ambiguous_disable_failure(monkeypatch):
    connection = _FenceConnection(fail_disable=True)
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    with pytest.raises(RuntimeError, match="disable response lost"):
        with import_release_seed._postgres_connection_fence(
            "release",
            database_name=import_release_seed.PRODUCTION_DATABASE_NAME,
            confirmed_current_user="advisor_owner",
            enabled=True,
            confirmation=import_release_seed.CONNECTION_FENCE_CONFIRMATION_VALUE,
        ):
            raise AssertionError("unreachable")

    assert connection.events == [
        "connect",
        "maintenance-autocommit",
        "identity",
        "disable",
        "restore",
        "verify",
        "close",
    ]
    assert connection.maintenance_connection is not None
    assert connection.maintenance_connection.allow_connections is True
    assert connection.maintenance_connection.closed is True


@pytest.mark.parametrize(
    "connection_kwargs",
    [
        {"fail_connect": True},
        {"maintenance_database": "wrong_database"},
        {"maintenance_user": "wrong_owner"},
    ],
)
def test_postgres_connection_fence_fails_closed_before_disabling(monkeypatch, connection_kwargs):
    connection = _FenceConnection(**connection_kwargs)
    monkeypatch.setattr(import_release_seed, "connections", {"release": connection})

    with pytest.raises(CommandError, match="maintenance connection could not be"):
        with import_release_seed._postgres_connection_fence(
            "release",
            database_name=import_release_seed.PRODUCTION_DATABASE_NAME,
            confirmed_current_user="advisor_owner",
            enabled=True,
            confirmation=import_release_seed.CONNECTION_FENCE_CONFIRMATION_VALUE,
        ):
            raise AssertionError("unreachable")

    assert "disable" not in connection.events
    if connection.maintenance_connection is not None:
        assert connection.maintenance_connection.closed is True


def test_target_flush_never_requests_cascade(monkeypatch):
    observed: dict[str, bool] = {}

    class Introspection:
        def table_names(self, *, include_views):
            assert include_views is False
            return ["students", "courses", "django_migrations"]

    class Operations:
        def sql_flush(self, style, tables, *, reset_sequences, allow_cascade):
            observed["allow_cascade"] = allow_cascade
            assert set(tables) == {"students", "courses"}
            assert reset_sequences is True
            return []

        def execute_sql_flush(self, statements):
            assert statements == []

    fake_connection = SimpleNamespace(introspection=Introspection(), ops=Operations())
    monkeypatch.setattr(import_release_seed, "connections", {"release": fake_connection})
    import_release_seed._flush_target_database("release")
    assert observed == {"allow_cascade": False}
