from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time
from io import StringIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

import django
from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connections, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import QuerySet
from django.utils.timezone import is_aware

PROFILE_NAME = "academic-accounts-v1"
SIGNATURE_ALGORITHM = "hmac-sha256"
RELEASE_SEED_SIGNING_KEY_ENV = "RELEASE_SEED_SIGNING_KEY"
INSECURE_SIGNING_KEYS = {
    "django-insecure-dev-only-change-me",
    "change-me-generate-a-real-key",
}
INSECURE_SIGNING_KEY_MARKERS = ("change-me", "replace-me", "example", "dev-only")
MINIMUM_SIGNING_KEY_LENGTH = 32
STUDENT_ROLE = "STUDENT"
PRIVILEGED_ROLES = {"SUPER_ADMIN", "GENERAL_ACADEMIC_ADVISOR"}

# This is intentionally an allowlist. Adding a model to the project must not
# silently publish it in the next production seed.
ALLOWED_MODELS = (
    "auth.group",
    "auth.user",
    "core.academicadvisor",
    "core.course",
    "core.courseinstructor",
    "core.electivecourse",
    "core.electivetermmapping",
    "core.instructor",
    "core.prerequisite",
    "core.programmerequirement",
    "core.room",
    "core.student",
    "core.studentcourse",
    "core.termsection",
    "core.termsectionprogram",
    "core.termsectionmeeting",
    "core.studenttermsection",
    "core.userscope",
)

FILTER_DESCRIPTIONS = (
    "Export only global current-snapshot TermSection rows (scenario is null).",
    "Export section memberships, meetings, and student links only for exported sections.",
    "Remove STUDENT-role UserScope rows that do not resolve to an exported Student.",
    "Remove non-staff student accounts left unscoped solely by that invalid scope.",
)

SQLITE_SNAPSHOT_ALIAS = "release_seed_frozen_snapshot"


class ReleaseSeedJSONEncoder(DjangoJSONEncoder):
    """Preserve database timestamp precision in the signed fixture."""

    def default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            rendered = value.isoformat()
            if rendered.endswith("+00:00"):
                rendered = f"{rendered[:-6]}Z"
            return rendered
        if isinstance(value, time):
            if is_aware(value):
                raise ValueError("Release seeds cannot serialize timezone-aware times.")
            return value.isoformat()
        return super().default(value)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommandError("Release seed contains a value that cannot be canonicalised.") from exc


def canonical_content_sha256(records: list[dict[str, Any]]) -> str:
    """Digest every serialized model, PK, scalar field, and M2M value."""

    normalized: list[dict[str, Any]] = []
    for record in records:
        label = str(record.get("model") or "")
        model = apps.get_model(label)
        fields = dict(record.get("fields") or {})
        many_to_many = {field.name for field in model._meta.many_to_many}
        for name in many_to_many & fields.keys():
            value = fields[name]
            if isinstance(value, list):
                fields[name] = sorted(
                    value,
                    key=lambda item: _canonical_json_bytes(item),
                )
        normalized.append({"model": label, "pk": record.get("pk"), "fields": fields})

    normalized.sort(
        key=lambda record: (
            record["model"],
            type(record["pk"]).__name__,
            _canonical_json_bytes(record["pk"]),
        )
    )
    return _sha256_bytes(_canonical_json_bytes(normalized))


def _manifest_signature_value(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    return hmac.new(
        _release_signing_key_bytes(),
        _canonical_json_bytes(unsigned),
        hashlib.sha256,
    ).hexdigest()


def _release_signing_key_bytes() -> bytes:
    key = os.getenv(RELEASE_SEED_SIGNING_KEY_ENV, "")
    lowered = key.lower()
    if (
        len(key) < MINIMUM_SIGNING_KEY_LENGTH
        or key in INSECURE_SIGNING_KEYS
        or any(marker in lowered for marker in INSECURE_SIGNING_KEY_MARKERS)
    ):
        raise CommandError(
            f"A strong dedicated {RELEASE_SEED_SIGNING_KEY_ENV} is required for release seeds."
        )
    return key.encode("utf-8")


def validate_release_signing_key() -> None:
    _release_signing_key_bytes()


def sign_manifest(manifest: dict[str, Any]) -> None:
    validate_release_signing_key()
    manifest["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "value": _manifest_signature_value(manifest),
    }


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def _sqlite_file_path(database_name: Any) -> Path | None:
    name = str(database_name or "")
    if not name or name == ":memory:":
        return None
    if name.startswith("file:"):
        parsed = urlsplit(name)
        if parse_qs(parsed.query, keep_blank_values=True).get("mode") == ["memory"]:
            return None
        raw_path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        if not raw_path:
            return None
        return Path(raw_path).expanduser().resolve()
    return Path(name).expanduser().resolve()


def _file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    stat = path.stat()
    return {
        "present": True,
        "size_bytes": stat.st_size,
        "modified_at_utc": _utc_iso(stat.st_mtime),
        "sha256": _sha256_file(path),
    }


def _source_database_fingerprint(connection: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"vendor": connection.vendor}
    if connection.vendor != "sqlite":
        return result

    path = _sqlite_file_path(connection.settings_dict.get("NAME"))
    if path is None:
        result["sqlite_file"] = {"available": False, "reason": "memory-or-uri-database"}
        return result

    result["sqlite_file"] = {
        "available": True,
        # The full workstation path is deliberately not published.
        "file_name": path.name,
        "main": _file_fingerprint(path),
        "wal": _file_fingerprint(Path(f"{path}-wal")),
        "shm": _file_fingerprint(Path(f"{path}-shm")),
        "journal": _file_fingerprint(Path(f"{path}-journal")),
    }
    return result


def _content_fingerprint(source: dict[str, Any]) -> tuple[Any, ...] | None:
    sqlite_file = source.get("sqlite_file")
    if not isinstance(sqlite_file, dict) or not sqlite_file.get("available"):
        return None
    main = sqlite_file.get("main") or {}
    wal = sqlite_file.get("wal") or {}
    shm = sqlite_file.get("shm") or {}
    journal = sqlite_file.get("journal") or {}
    return (
        main.get("present"),
        main.get("size_bytes"),
        main.get("sha256"),
        wal.get("present"),
        wal.get("size_bytes"),
        wal.get("sha256"),
        shm.get("present"),
        shm.get("size_bytes"),
        shm.get("sha256"),
        journal.get("present"),
        journal.get("size_bytes"),
        journal.get("sha256"),
    )


def _require_read_only_source(connection: Any) -> None:
    if connection.vendor != "sqlite":
        return
    name = str(connection.settings_dict.get("NAME") or "")
    if not name.startswith("file:"):
        raise CommandError(
            "SQLite export requires a frozen mode=ro&immutable=1 URI, not a writable database."
        )
    params = parse_qs(urlsplit(name).query, keep_blank_values=True)
    if params.get("mode") != ["ro"] or params.get("immutable") != ["1"]:
        raise CommandError(
            "SQLite export requires a frozen mode=ro&immutable=1 URI, not a writable database."
        )
    source = _source_database_fingerprint(connection).get("sqlite_file") or {}
    if not source.get("available") or not (source.get("main") or {}).get("present"):
        raise CommandError("Frozen SQLite release source is unavailable.")
    if any((source.get(name) or {}).get("present") for name in ("wal", "shm", "journal")):
        raise CommandError("Frozen SQLite release source must not have WAL or journal sidecars.")


@contextmanager
def _frozen_sqlite_alias(path: Path) -> Iterator[tuple[str, SQLiteDatabaseWrapper]]:
    frozen_path = path.expanduser().resolve()
    if not frozen_path.is_file():
        raise CommandError("Frozen SQLite release source is unavailable.")
    default_path = _sqlite_file_path(connections["default"].settings_dict.get("NAME"))
    if default_path is not None:
        try:
            is_live_database = os.path.samefile(frozen_path, default_path)
        except OSError as exc:
            raise CommandError(
                "Could not safely verify that the frozen SQLite copy is separate from the "
                "live local database."
            ) from exc
        if is_live_database:
            raise CommandError(
                "Export from a separate frozen SQLite copy, not the live local database."
            )

    if SQLITE_SNAPSHOT_ALIAS in connections.databases:
        connections[SQLITE_SNAPSHOT_ALIAS].close()
    database = dict(connections.databases["default"])
    database.update(
        {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": f"{frozen_path.as_uri()}?mode=ro&immutable=1",
            "OPTIONS": {"uri": True},
            "RELEASE_SEED_READ_ONLY": True,
            "HOST": "",
            "PORT": "",
            "USER": "",
            "PASSWORD": "",
        }
    )
    # Establish the isolated wrapper before publishing the alias. This also
    # keeps Django's test database guard from mistaking the frozen snapshot for
    # an undeclared shared test database.
    connection = SQLiteDatabaseWrapper(database, alias=SQLITE_SNAPSHOT_ALIAS)
    connection.ensure_connection()
    connections.databases[SQLITE_SNAPSHOT_ALIAS] = database
    setattr(connections._connections, SQLITE_SNAPSHOT_ALIAS, connection)  # type: ignore[attr-defined]
    try:
        yield SQLITE_SNAPSHOT_ALIAS, connection
    finally:
        connection.close()
        connections.databases.pop(SQLITE_SNAPSHOT_ALIAS, None)
        try:
            delattr(connections._connections, SQLITE_SNAPSHOT_ALIAS)  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _git_state() -> dict[str, Any]:
    base_dir = Path(settings.BASE_DIR)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=base_dir,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=base_dir,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit or None, "dirty": bool(status.strip())}


def _migration_state(connection: Any) -> dict[str, Any]:
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    loader.check_consistent_history(connection)
    leaves = sorted(f"{app}.{name}" for app, name in loader.graph.leaf_nodes())
    applied = sorted(
        f"{app}.{name}" for app, name in MigrationRecorder(connection).applied_migrations()
    )
    applied_set = set(applied)
    return {
        "leaf_nodes": leaves,
        "applied": applied,
        "unapplied_leaf_nodes": [leaf for leaf in leaves if leaf not in applied_set],
    }


def _student_identity_filters(using: str) -> tuple[set[int], set[int]]:
    user_model = apps.get_model("auth", "User")
    scope_model = apps.get_model("core", "UserScope")
    student_model = apps.get_model("core", "Student")

    valid_student_ids = set(student_model.objects.using(using).values_list("student_id", flat=True))
    invalid_scope_users: set[int] = set()
    scopes = (
        scope_model.objects.using(using).select_related("user").prefetch_related("user__groups")
    )
    for scope in scopes:
        group_names = {group.name for group in scope.user.groups.all()}
        is_student = (
            not scope.user.is_superuser
            and STUDENT_ROLE in group_names
            and not (group_names & PRIVILEGED_ROLES)
        )
        if is_student and scope.student_id not in valid_student_ids:
            invalid_scope_users.add(int(scope.user_id))

    removable_users = set(
        user_model.objects.using(using)
        .filter(pk__in=invalid_scope_users, is_staff=False, is_superuser=False)
        .values_list("pk", flat=True)
    )
    return invalid_scope_users, removable_users


def _queryset_for_model(
    label: str,
    *,
    using: str,
    invalid_scope_users: set[int],
    removable_users: set[int],
) -> QuerySet[Any]:
    model = apps.get_model(label)
    queryset = model._default_manager.using(using).all()

    if label == "auth.user":
        queryset = queryset.exclude(pk__in=removable_users)
    elif label == "core.userscope":
        queryset = queryset.exclude(user_id__in=invalid_scope_users)
    elif label == "core.termsection":
        queryset = queryset.filter(scenario__isnull=True)
    elif label in {"core.termsectionprogram", "core.termsectionmeeting"}:
        queryset = queryset.filter(term_section__scenario__isnull=True)
    elif label == "core.studenttermsection":
        queryset = queryset.filter(term_section__scenario__isnull=True)

    return cast(QuerySet[Any], queryset.order_by(model._meta.pk.name))


def _serialise_records(using: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    invalid_scope_users, removable_users = _student_identity_filters(using)
    records: list[dict[str, Any]] = []
    model_counts: dict[str, dict[str, int]] = {}

    for label in ALLOWED_MODELS:
        model = apps.get_model(label)
        source_count = model._default_manager.using(using).count()
        queryset = _queryset_for_model(
            label,
            using=using,
            invalid_scope_users=invalid_scope_users,
            removable_users=removable_users,
        )
        stream = StringIO()
        serializers.serialize(
            "json",
            queryset,
            stream=stream,
            use_natural_foreign_keys=True,
            use_natural_primary_keys=False,
            cls=ReleaseSeedJSONEncoder,
        )
        model_records = json.loads(stream.getvalue())
        records.extend(model_records)
        model_counts[label] = {
            "source": source_count,
            "exported": len(model_records),
            "filtered": source_count - len(model_records),
        }

    exported_counts = Counter(str(record.get("model")) for record in records)
    for label, counts in model_counts.items():
        if exported_counts[label] != counts["exported"]:
            raise CommandError("Release seed count validation failed before writing output.")

    return records, {
        "per_model": model_counts,
        "total_records": len(records),
        "filters": {
            "invalid_student_scopes_removed": len(invalid_scope_users),
            "otherwise_unscoped_student_users_removed": len(removable_users),
        },
    }


def _compressed_fixture(records: list[dict[str, Any]]) -> bytes:
    payload = (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # A fixed mtime makes identical source snapshots produce identical fixtures.
    return gzip.compress(payload, compresslevel=9, mtime=0)


def _manifest_path(fixture_path: Path) -> Path:
    name = fixture_path.name
    if name.endswith(".json.gz"):
        stem = name[: -len(".json.gz")]
    else:
        stem = fixture_path.stem
    return fixture_path.with_name(f"{stem}.manifest.json")


def _destination(raw_output: str) -> tuple[Path, Path]:
    supplied = Path(raw_output).expanduser()
    if supplied.exists() and supplied.is_dir():
        fixture_path = supplied / "release-seed.json.gz"
    elif str(supplied).lower().endswith(".json.gz"):
        fixture_path = supplied
    else:
        fixture_path = supplied / "release-seed.json.gz"

    fixture_path = fixture_path.resolve()
    manifest_path = _manifest_path(fixture_path)
    project_root = Path(settings.BASE_DIR).resolve()
    try:
        fixture_path.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise CommandError("Release artifacts must be written outside the project repository.")
    return fixture_path, manifest_path


def _write_file(path: Path, payload: bytes) -> None:
    # The path was reserved by NamedTemporaryFile immediately before this call.
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_pair_atomically(
    fixture_path: Path,
    fixture_payload: bytes,
    manifest_path: Path,
    manifest_payload: bytes,
    *,
    force: bool,
) -> None:
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = fixture_path.with_name(f".{fixture_path.name}.export.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise CommandError("Another release-seed export is active for this destination.") from exc

    temporary_paths: list[Path] = []
    try:
        os.close(lock_fd)
        existing = [path.name for path in (fixture_path, manifest_path) if path.exists()]
        if existing and not force:
            raise CommandError(
                "Release artifact already exists; pass --force to replace both artifacts."
            )

        with tempfile.NamedTemporaryFile(
            prefix=f".{fixture_path.name}.", suffix=".tmp", dir=fixture_path.parent, delete=False
        ) as handle:
            fixture_tmp = Path(handle.name)
        temporary_paths.append(fixture_tmp)
        _write_file(fixture_tmp, fixture_payload)

        with tempfile.NamedTemporaryFile(
            prefix=f".{manifest_path.name}.", suffix=".tmp", dir=fixture_path.parent, delete=False
        ) as handle:
            manifest_tmp = Path(handle.name)
        temporary_paths.append(manifest_tmp)
        _write_file(manifest_tmp, manifest_payload)

        # The manifest is the commit marker and is replaced last. Consumers must
        # verify its checksum before loading the fixture.
        os.replace(fixture_tmp, fixture_path)
        temporary_paths.remove(fixture_tmp)
        os.replace(manifest_tmp, manifest_path)
        temporary_paths.remove(manifest_tmp)
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


class Command(BaseCommand):
    help = (
        "Export a read-only, production release seed containing durable academic "
        f"and account data only. The manifest is signed with {RELEASE_SEED_SIGNING_KEY_ENV}; "
        "the importer must receive the same ephemeral key."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "output",
            help="Destination directory or .json.gz file outside the project repository.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Configured Django database alias to read (default: default).",
        )
        parser.add_argument(
            "--sqlite-frozen-copy",
            help=(
                "Open this separate SQLite snapshot as mode=ro&immutable=1. "
                "The snapshot must have no WAL/journal sidecars."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace an existing fixture and manifest as one release pair.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        using = str(options["database"])
        fixture_path, manifest_path = _destination(str(options["output"]))
        frozen_copy = options.get("sqlite_frozen_copy")
        if frozen_copy:
            if using != "default":
                raise CommandError("Use either --database or --sqlite-frozen-copy, not both.")
            with _frozen_sqlite_alias(Path(str(frozen_copy))) as (snapshot_alias, connection):
                self._export(
                    using=snapshot_alias,
                    connection=connection,
                    fixture_path=fixture_path,
                    manifest_path=manifest_path,
                    force=bool(options["force"]),
                )
            return
        if using not in connections:
            raise CommandError("Unknown database alias.")
        self._export(
            using=using,
            connection=connections[using],
            fixture_path=fixture_path,
            manifest_path=manifest_path,
            force=bool(options["force"]),
        )

    def _export(
        self,
        *,
        using: str,
        connection: Any,
        fixture_path: Path,
        manifest_path: Path,
        force: bool,
    ) -> None:
        _require_read_only_source(connection)

        source_before = _source_database_fingerprint(connection)
        with transaction.atomic(using=using):
            records, counts = _serialise_records(using)
            migrations = _migration_state(connection)
        source_after = _source_database_fingerprint(connection)
        before_content = _content_fingerprint(source_before)
        if before_content is not None and before_content != _content_fingerprint(source_after):
            raise CommandError(
                "SQLite source changed during export; no release artifact was written."
            )

        fixture_payload = _compressed_fixture(records)
        content_sha256 = canonical_content_sha256(records)
        installed_models = {
            model._meta.label_lower for model in apps.get_models(include_auto_created=False)
        }
        manifest = {
            "format_version": 1,
            "profile": {
                "name": PROFILE_NAME,
                "allowed_models": list(ALLOWED_MODELS),
                "excluded_models": sorted(installed_models - set(ALLOWED_MODELS)),
                "filter_rules": list(FILTER_DESCRIPTIONS),
                "natural_foreign_keys": True,
                "natural_primary_keys": False,
            },
            "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "fixture": {
                "file_name": fixture_path.name,
                "compression": "gzip",
                "encoding": "utf-8",
                "sha256": _sha256_bytes(fixture_payload),
                "canonical_content_sha256": content_sha256,
                "size_bytes": len(fixture_payload),
                **counts,
            },
            "source_database": source_after,
            "git": _git_state(),
            "runtime": {
                "python": sys.version.split()[0],
                "django": django.get_version(),
            },
            "migrations": migrations,
        }
        sign_manifest(manifest)
        manifest_payload = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _write_pair_atomically(
            fixture_path,
            fixture_payload,
            manifest_path,
            manifest_payload,
            force=force,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(records)} release records with an authenticated manifest."
            )
        )
