from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import re
import subprocess
from argparse import ArgumentParser
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.core.management.sql import emit_post_migrate_signal
from django.db import connections, router, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Exists, OuterRef
from psycopg2 import sql as postgres_sql  # type: ignore[import-untyped]

from core.management.commands.export_release_seed import (
    ALLOWED_MODELS,
    FILTER_DESCRIPTIONS,
    PROFILE_NAME,
    RELEASE_SEED_SIGNING_KEY_ENV,
    SIGNATURE_ALGORITHM,
    _manifest_signature_value,
    _serialise_records,
    _sqlite_file_path,
    _student_identity_filters,
    canonical_content_sha256,
    validate_release_signing_key,
)
from core.models import Student, StudentTermSection, TermSection

FORMAT_VERSION = 1
CONFIRMATION_VALUE = "REPLACE_TARGET_DATABASE_WITH_RELEASE_SEED"
KILL_SWITCH_ENV = "RELEASE_SEED_DATABASE_REPLACEMENT"
KILL_SWITCH_VALUE = "ALLOW_CHECKSUM_BOUND_DATABASE_REPLACEMENT"
PRODUCTION_DATABASE_NAME = "advisor_system_db"
WRITERS_SUSPENDED_VALUE = "ALL_APPLICATION_WRITERS_ARE_SUSPENDED"
POSTGRES_REHEARSAL_ENV = "RELEASE_SEED_POSTGRES_REHEARSAL"
POSTGRES_REHEARSAL_VALUE = "ALLOW_NON_PRODUCTION_POSTGRES_REHEARSAL"
CONNECTION_FENCE_CONFIRMATION_VALUE = "FENCE_NEW_CONNECTIONS_AND_RESTORE_AFTER_IMPORT"
POSTGRES_MAINTENANCE_DATABASE = "postgres"
RENDER_POSTGRES_HOST_PATTERN = re.compile(r"(?:dpg-[a-z0-9-]+|[a-z0-9.-]+\.render\.com)\Z")
EMERGENCY_FENCE_RESTORE_GUIDANCE = (
    "PostgreSQL connection fence restoration failed; the data transaction may already have "
    "committed. Emergency action: from the postgres maintenance database, the database owner "
    "must restore ALLOW_CONNECTIONS, then verify the manifest digest and per-model counts "
    "before deciding whether to retry."
)

# A stable, application-specific signed bigint used only to serialize release
# seed replacements. PostgreSQL releases transaction advisory locks on both
# commit and rollback, including when the client disappears.
POSTGRES_ADVISORY_LOCK_KEY = 7_214_718_837_332_451_921

REGENERATED_MODELS = {
    "auth.permission",
    "contenttypes.contenttype",
}

COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
SAFE_DATABASE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_DECOMPRESSED_FIXTURE_BYTES = 512 * 1024 * 1024


def _fail(reason: str) -> CommandError:
    return CommandError(f"Release seed refused: {reason}.")


def _require_exact_keys(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _fail(f"invalid {location} structure")
    return value


def _is_non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_string_list(value: Any, location: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise _fail(f"invalid {location}")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonstandard_json_number(value: str) -> Any:
    raise ValueError(f"non-standard JSON number: {value}")


def _strict_json_loads(payload: str) -> Any:
    return json.loads(
        payload,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonstandard_json_number,
    )


def _manifest_path(fixture_path: Path, supplied: str | None) -> Path:
    if supplied:
        return Path(supplied).expanduser().resolve()
    name = fixture_path.name
    if name.endswith(".json.gz"):
        name = f"{name[: -len('.json.gz')]}.manifest.json"
    else:
        name = f"{fixture_path.stem}.manifest.json"
    return fixture_path.with_name(name)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        manifest = _strict_json_loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail("manifest is unavailable or invalid JSON") from exc

    return _require_exact_keys(
        manifest,
        {
            "format_version",
            "profile",
            "created_at_utc",
            "fixture",
            "source_database",
            "git",
            "runtime",
            "migrations",
            "signature",
        },
        "manifest",
    )


def _validate_manifest_signature(manifest: dict[str, Any]) -> None:
    try:
        validate_release_signing_key()
    except CommandError as exc:
        raise _fail(f"a strong dedicated {RELEASE_SEED_SIGNING_KEY_ENV} is required") from exc
    signature = _require_exact_keys(
        manifest["signature"], {"algorithm", "value"}, "manifest signature"
    )
    value = signature["value"]
    if (
        signature["algorithm"] != SIGNATURE_ALGORITHM
        or not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _fail("manifest signature is invalid")
    try:
        expected = _manifest_signature_value(manifest)
    except CommandError as exc:
        raise _fail(
            f"manifest signature could not be verified with {RELEASE_SEED_SIGNING_KEY_ENV}"
        ) from exc
    if not hmac.compare_digest(value, expected):
        raise _fail(
            "manifest signature does not match; exporter and importer must receive the same "
            f"ephemeral {RELEASE_SEED_SIGNING_KEY_ENV}"
        )


def _validate_profile(manifest: dict[str, Any]) -> None:
    if manifest["format_version"] != FORMAT_VERSION:
        raise _fail("unsupported format version")
    if not isinstance(manifest["created_at_utc"], str) or not manifest["created_at_utc"]:
        raise _fail("invalid creation timestamp")

    profile = _require_exact_keys(
        manifest["profile"],
        {
            "name",
            "allowed_models",
            "excluded_models",
            "filter_rules",
            "natural_foreign_keys",
            "natural_primary_keys",
        },
        "profile",
    )
    installed_models = {
        model._meta.label_lower for model in apps.get_models(include_auto_created=False)
    }
    expected_excluded = sorted(installed_models - set(ALLOWED_MODELS))
    if (
        profile["name"] != PROFILE_NAME
        or profile["allowed_models"] != list(ALLOWED_MODELS)
        or profile["excluded_models"] != expected_excluded
        or profile["filter_rules"] != list(FILTER_DESCRIPTIONS)
        or profile["natural_foreign_keys"] is not True
        or profile["natural_primary_keys"] is not False
    ):
        raise _fail("profile does not match the importer contract")

    for label in ALLOWED_MODELS:
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError) as exc:
            raise _fail("profile contains an unavailable model") from exc
        if model is None or model._meta.label_lower != label:
            raise _fail("profile contains an unavailable model")


def _validate_manifest_metadata(manifest: dict[str, Any]) -> None:
    source = manifest["source_database"]
    if not isinstance(source, dict) or not isinstance(source.get("vendor"), str):
        raise _fail("invalid source database metadata")

    git = _require_exact_keys(manifest["git"], {"commit", "dirty"}, "git metadata")
    if git["commit"] is not None and not isinstance(git["commit"], str):
        raise _fail("invalid git metadata")
    if git["dirty"] is not None and not isinstance(git["dirty"], bool):
        raise _fail("invalid git metadata")

    runtime = _require_exact_keys(manifest["runtime"], {"python", "django"}, "runtime metadata")
    if any(not isinstance(runtime[key], str) or not runtime[key] for key in runtime):
        raise _fail("invalid runtime metadata")

    migrations = _require_exact_keys(
        manifest["migrations"],
        {"leaf_nodes", "applied", "unapplied_leaf_nodes"},
        "migration metadata",
    )
    _require_string_list(migrations["leaf_nodes"], "source migration leaves")
    _require_string_list(migrations["applied"], "source applied migrations")
    if migrations["unapplied_leaf_nodes"] != []:
        raise _fail("source database did not have all migrations applied")
    if not set(migrations["leaf_nodes"]).issubset(set(migrations["applied"])):
        raise _fail("source migration metadata is inconsistent")


def _validate_fixture_manifest(
    fixture_info: Any,
    *,
    fixture_path: Path,
    fixture_payload: bytes,
) -> tuple[dict[str, int], str]:
    fixture = _require_exact_keys(
        fixture_info,
        {
            "file_name",
            "compression",
            "encoding",
            "sha256",
            "canonical_content_sha256",
            "size_bytes",
            "per_model",
            "total_records",
            "filters",
        },
        "fixture metadata",
    )
    if (
        fixture["file_name"] != fixture_path.name
        or fixture["compression"] != "gzip"
        or fixture["encoding"] != "utf-8"
    ):
        raise _fail("fixture identity or encoding does not match the manifest")
    if not _is_non_negative_integer(fixture["size_bytes"]):
        raise _fail("invalid fixture size")
    if fixture["size_bytes"] != len(fixture_payload):
        raise _fail("fixture size does not match the manifest")

    expected_hash = fixture["sha256"]
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
        or not hmac.compare_digest(hashlib.sha256(fixture_payload).hexdigest(), expected_hash)
    ):
        raise _fail("fixture checksum does not match the manifest")
    content_hash = fixture["canonical_content_sha256"]
    if (
        not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(character not in "0123456789abcdef" for character in content_hash)
    ):
        raise _fail("canonical content digest is invalid")

    if not _is_non_negative_integer(fixture["total_records"]):
        raise _fail("invalid total record count")
    per_model = fixture["per_model"]
    if not isinstance(per_model, dict) or set(per_model) != set(ALLOWED_MODELS):
        raise _fail("per-model manifest does not match the allowlist")

    expected_counts: dict[str, int] = {}
    for label in ALLOWED_MODELS:
        counts = _require_exact_keys(
            per_model[label], {"source", "exported", "filtered"}, "per-model counts"
        )
        if any(not _is_non_negative_integer(counts[key]) for key in counts):
            raise _fail("invalid per-model counts")
        if counts["source"] != counts["exported"] + counts["filtered"]:
            raise _fail("inconsistent per-model counts")
        expected_counts[label] = counts["exported"]
    if sum(expected_counts.values()) != fixture["total_records"]:
        raise _fail("total record count does not match per-model counts")

    filters = _require_exact_keys(
        fixture["filters"],
        {
            "invalid_student_scopes_removed",
            "otherwise_unscoped_student_users_removed",
        },
        "filter counts",
    )
    if any(not _is_non_negative_integer(value) for value in filters.values()):
        raise _fail("invalid filter counts")
    return expected_counts, content_hash


def _expected_serialized_fields(model: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for field in [*model._meta.fields, *model._meta.many_to_many]:
        if field.serialize and not field.primary_key:
            fields[field.name] = field
    return fields


def _validate_field_value(field: Any, value: Any) -> None:
    if value is None:
        if not getattr(field, "null", False):
            raise _fail("fixture contains null for a required field")
        return

    if field.many_to_many:
        if not isinstance(value, list):
            raise _fail("fixture contains an invalid many-to-many value")
        related_pk = field.remote_field.model._meta.pk
        natural_key = hasattr(field.remote_field.model._default_manager, "get_by_natural_key")
        for item in value:
            if natural_key:
                if not isinstance(item, list | tuple) or not item:
                    raise _fail("fixture contains an invalid natural relation")
            else:
                try:
                    related_pk.to_python(item)
                except (TypeError, ValueError) as exc:
                    raise _fail("fixture contains an invalid related key") from exc
        return

    if field.is_relation:
        natural_key = hasattr(field.remote_field.model._default_manager, "get_by_natural_key")
        if natural_key:
            if not isinstance(value, list | tuple) or not value:
                raise _fail("fixture contains an invalid natural relation")
            return
        field = field.target_field

    try:
        field.to_python(value)
    except (TypeError, ValueError) as exc:
        raise _fail("fixture contains an invalid field value") from exc


def _validate_records(
    decompressed: bytes,
    expected_counts: dict[str, int],
    expected_content_sha256: str,
) -> tuple[str, int]:
    try:
        text = decompressed.decode("utf-8")
        records = _strict_json_loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail("fixture is not valid UTF-8 JSON") from exc
    if not isinstance(records, list):
        raise _fail("fixture root must be a JSON list")

    actual_counts: Counter[str] = Counter()
    identities: set[tuple[str, str]] = set()
    for record in records:
        item = _require_exact_keys(record, {"model", "pk", "fields"}, "fixture record")
        label = item["model"]
        if not isinstance(label, str) or label not in ALLOWED_MODELS:
            raise _fail("fixture contains an unknown or disallowed model")
        model = apps.get_model(label)
        try:
            normalized_pk = model._meta.pk.to_python(item["pk"])
        except (TypeError, ValueError) as exc:
            raise _fail("fixture contains an invalid primary key") from exc
        if normalized_pk is None:
            raise _fail("fixture contains an empty primary key")
        identity = (label, str(normalized_pk))
        if identity in identities:
            raise _fail("fixture contains a duplicate model primary key")
        identities.add(identity)

        fields = item["fields"]
        expected_fields = _expected_serialized_fields(model)
        if not isinstance(fields, dict) or set(fields) != set(expected_fields):
            raise _fail("fixture record fields do not match the installed model")
        for name, field in expected_fields.items():
            _validate_field_value(field, fields[name])
        actual_counts[label] += 1

    if len(records) != sum(expected_counts.values()):
        raise _fail("fixture record total does not match the manifest")
    if any(actual_counts[label] != expected_counts[label] for label in ALLOWED_MODELS):
        raise _fail("fixture per-model counts do not match the manifest")
    if not hmac.compare_digest(canonical_content_sha256(records), expected_content_sha256):
        raise _fail("fixture canonical content does not match the manifest")
    return text, len(records)


def _validate_artifact(
    fixture_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, int], str, int, str]:
    manifest = _read_manifest(manifest_path)
    _validate_manifest_signature(manifest)
    _validate_profile(manifest)
    _validate_manifest_metadata(manifest)
    try:
        fixture_payload = fixture_path.read_bytes()
    except OSError as exc:
        raise _fail("fixture is unavailable") from exc
    expected_counts, expected_content_sha256 = _validate_fixture_manifest(
        manifest["fixture"],
        fixture_path=fixture_path,
        fixture_payload=fixture_payload,
    )
    try:
        with gzip.GzipFile(fileobj=BytesIO(fixture_payload)) as stream:
            decompressed = stream.read(MAX_DECOMPRESSED_FIXTURE_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise _fail("fixture gzip stream is invalid") from exc
    if len(decompressed) > MAX_DECOMPRESSED_FIXTURE_BYTES:
        raise _fail("fixture expands beyond the importer safety limit")
    fixture_text, total_records = _validate_records(
        decompressed, expected_counts, expected_content_sha256
    )
    return manifest, expected_counts, fixture_text, total_records, expected_content_sha256


def _target_migration_state(using: str, source_manifest: dict[str, Any]) -> set[tuple[str, str]]:
    connection = connections[using]
    executor = MigrationExecutor(connection)
    executor.loader.check_consistent_history(connection)
    target_leaves = set(executor.loader.graph.leaf_nodes())
    if executor.migration_plan(sorted(target_leaves)):
        raise _fail("target database has unapplied migrations")

    source_leaves = set()
    for value in source_manifest["migrations"]["leaf_nodes"]:
        if value.count(".") != 1:
            raise _fail("source migration leaf is invalid")
        source_leaves.add(tuple(value.split(".", 1)))
    if source_leaves != target_leaves:
        raise _fail("source and target migration leaves are incompatible")
    return set(MigrationRecorder(connection).applied_migrations())


def _validate_production_checkout(
    using: str,
    manifest: dict[str, Any],
    *,
    allow_rehearsal: bool,
) -> None:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    if allow_rehearsal:
        if os.getenv(POSTGRES_REHEARSAL_ENV) != POSTGRES_REHEARSAL_VALUE:
            raise _fail("non-production PostgreSQL rehearsal gate is not enabled")
    elif settings.DEBUG:
        raise _fail("PostgreSQL replacement requires production settings")

    manifest_git = manifest["git"]
    expected_commit = manifest_git["commit"]
    if (
        not isinstance(expected_commit, str)
        or COMMIT_SHA_PATTERN.fullmatch(expected_commit) is None
        or manifest_git["dirty"] is not False
    ):
        raise _fail("release artifact is not bound to a clean production commit")

    try:
        current_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=settings.BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        tracked_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=settings.BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _fail("production checkout identity could not be verified") from exc
    if current_commit != expected_commit or tracked_status.strip():
        raise _fail("production checkout does not match the release artifact")


def _validate_sqlite_rehearsal_target(using: str) -> None:
    connection = connections[using]
    if connection.vendor != "sqlite":
        return
    if using == "default":
        raise _fail("SQLite rehearsal requires a non-default disposable database alias")
    target_path = _sqlite_file_path(connection.settings_dict.get("NAME"))
    if target_path is None or not target_path.is_file():
        raise _fail("SQLite rehearsal target must be an existing disposable database file")
    project_root = Path(settings.BASE_DIR).resolve()
    try:
        target_path.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise _fail("SQLite rehearsal target must be outside the project directory")

    default_path = _sqlite_file_path(connections["default"].settings_dict.get("NAME"))
    if default_path is not None:
        try:
            is_live_database = os.path.samefile(target_path, default_path)
        except OSError as exc:
            raise _fail("SQLite rehearsal target identity could not be safely verified") from exc
        if is_live_database:
            raise _fail("SQLite rehearsal cannot target the live default database")


def _validate_postgres_target_context(
    using: str,
    *,
    expected_database_name: str,
    confirmed_database_name: str,
    expected_host: str,
    confirmed_current_user: str,
    writers_confirmation: str,
    allow_rehearsal: bool,
) -> None:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    if writers_confirmation != WRITERS_SUSPENDED_VALUE:
        raise _fail("exact application-writers suspension confirmation was not supplied")

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        actual_database_name, actual_current_user = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND pid <> pg_backend_pid()
               AND backend_type = 'client backend'
            """
        )
        other_client_sessions = cursor.fetchone()[0]

    actual_host = str(connection.settings_dict.get("HOST") or "").strip().lower().rstrip(".")

    if (
        not isinstance(actual_database_name, str)
        or not expected_database_name
        or expected_database_name != actual_database_name
        or confirmed_database_name != actual_database_name
    ):
        raise _fail("both database-name confirmations must exactly match current_database()")
    if not actual_host or expected_host != actual_host:
        raise _fail("host confirmation must exactly match the normalized connection host")
    if not isinstance(actual_current_user, str) or confirmed_current_user != actual_current_user:
        raise _fail("user confirmation must exactly match current_user")
    if not allow_rehearsal and actual_database_name != PRODUCTION_DATABASE_NAME:
        raise _fail("production replacement is bound to the configured production database")
    if not allow_rehearsal and RENDER_POSTGRES_HOST_PATTERN.fullmatch(actual_host) is None:
        raise _fail("production replacement requires a Render PostgreSQL host")
    if allow_rehearsal and (
        actual_database_name == PRODUCTION_DATABASE_NAME
        or RENDER_POSTGRES_HOST_PATTERN.fullmatch(actual_host) is not None
    ):
        raise _fail("PostgreSQL rehearsal cannot use the production database identity")
    if other_client_sessions != 0:
        raise _fail("other PostgreSQL client sessions are still connected")


@contextmanager
def _postgres_connection_fence(
    using: str,
    *,
    database_name: str,
    confirmed_current_user: str,
    enabled: bool,
    confirmation: str,
) -> Iterator[None]:
    connection = connections[using]
    if connection.vendor != "postgresql":
        yield
        return
    if not enabled or confirmation != CONNECTION_FENCE_CONFIRMATION_VALUE:
        raise _fail("explicit PostgreSQL connection-fence confirmation was not supplied")
    if not connection.get_autocommit():
        raise _fail("PostgreSQL connection fence requires an autocommit owner connection")
    if database_name == POSTGRES_MAINTENANCE_DATABASE:
        raise _fail("target database cannot also be the maintenance database")

    maintenance_connection = None
    fence_may_be_applied = False
    try:
        try:
            connection_params = dict(connection.get_connection_params())
            connection_params["dbname"] = POSTGRES_MAINTENANCE_DATABASE
            database_driver = getattr(connection, "Database", None)
            if database_driver is None:
                raise RuntimeError("PostgreSQL database driver is unavailable")
            maintenance_connection = database_driver.connect(**connection_params)
            maintenance_connection.autocommit = True
            with maintenance_connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user")
                identity = cursor.fetchone()
            if identity != (POSTGRES_MAINTENANCE_DATABASE, confirmed_current_user):
                raise RuntimeError("maintenance database identity mismatch")
        except Exception:
            if maintenance_connection is not None:
                try:
                    maintenance_connection.close()
                except Exception:
                    pass
                maintenance_connection = None
            raise _fail(
                "PostgreSQL maintenance connection could not be established or verified"
            ) from None

        with maintenance_connection.cursor() as cursor:
            # A network failure can occur after PostgreSQL executes the ALTER
            # but before the client receives confirmation. From this point on,
            # restoration is mandatory even when execute() raises.
            fence_may_be_applied = True
            cursor.execute(
                postgres_sql.SQL("ALTER DATABASE {database} WITH ALLOW_CONNECTIONS false").format(
                    database=postgres_sql.Identifier(database_name)
                )
            )
            cursor.execute(
                "SELECT datallowconn FROM pg_database WHERE datname = %s",
                [database_name],
            )
            state = cursor.fetchone()
            if state is None or state[0] is not False:
                raise _fail("PostgreSQL connection fence could not be verified")
        yield
    finally:
        restoration_failed = False
        if fence_may_be_applied:
            try:
                if maintenance_connection is None or not maintenance_connection.autocommit:
                    raise RuntimeError("maintenance connection is unavailable")
                with maintenance_connection.cursor() as cursor:
                    cursor.execute(
                        postgres_sql.SQL(
                            "ALTER DATABASE {database} WITH ALLOW_CONNECTIONS true"
                        ).format(database=postgres_sql.Identifier(database_name))
                    )
                    cursor.execute(
                        "SELECT datallowconn FROM pg_database WHERE datname = %s",
                        [database_name],
                    )
                    state = cursor.fetchone()
                    if state is None or state[0] is not True:
                        raise RuntimeError("connection fence restoration was not verified")
            except Exception:
                restoration_failed = True
        if maintenance_connection is not None:
            try:
                maintenance_connection.close()
            except Exception:
                # The gate state was verified above. A local close failure must
                # not replace either the body result or the recovery diagnosis.
                pass
        if restoration_failed:
            raise CommandError(EMERGENCY_FENCE_RESTORE_GUIDANCE) from None


def _take_postgres_advisory_lock(using: str) -> None:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [POSTGRES_ADVISORY_LOCK_KEY])


def _flush_target_database(using: str) -> None:
    connection = connections[using]
    migration_table = MigrationRecorder.Migration._meta.db_table
    table_names = [
        table
        for table in connection.introspection.table_names(include_views=False)
        if table != migration_table
    ]
    if any(SAFE_DATABASE_IDENTIFIER.fullmatch(table) is None for table in table_names):
        raise CommandError("Target database contains an unsupported table identifier.")
    sql = connection.ops.sql_flush(
        no_style(),
        table_names,
        reset_sequences=True,
        allow_cascade=False,
    )
    connection.ops.execute_sql_flush(sql)
    if sql:
        emit_post_migrate_signal(verbosity=0, interactive=False, db=using)


def _sequence_position_is_safe(
    *,
    maximum_key: int | None,
    last_value: int,
    is_called: bool,
    increment: int,
) -> bool:
    if maximum_key is None:
        return True
    if increment <= 0:
        return False
    next_value = last_value + increment if is_called else last_value
    return next_value > maximum_key


def _validate_postgres_sequences(using: str) -> None:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return

    # Catalog identifiers are resolved by PostgreSQL, then composed through
    # psycopg2 Identifier objects. No nextval()/setval() call is made.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_ns.nspname,
                   table_rel.relname,
                   table_attr.attname,
                   seq_ns.nspname,
                   seq_rel.relname,
                   pg_seq.seqincrement
              FROM pg_class AS seq_rel
              JOIN pg_namespace AS seq_ns ON seq_ns.oid = seq_rel.relnamespace
              JOIN pg_depend AS dep
                ON dep.objid = seq_rel.oid
               AND dep.classid = 'pg_class'::regclass
               AND dep.refclassid = 'pg_class'::regclass
               AND dep.deptype IN ('a', 'i')
              JOIN pg_class AS table_rel ON table_rel.oid = dep.refobjid
              JOIN pg_namespace AS table_ns ON table_ns.oid = table_rel.relnamespace
              JOIN pg_attribute AS table_attr
                ON table_attr.attrelid = table_rel.oid
               AND table_attr.attnum = dep.refobjsubid
              JOIN pg_sequence AS pg_seq ON pg_seq.seqrelid = seq_rel.oid
             WHERE seq_rel.relkind = 'S'
               AND table_rel.relkind IN ('r', 'p')
               AND table_ns.nspname = ANY (current_schemas(false))
             ORDER BY table_ns.nspname, table_rel.relname, table_attr.attname
            """
        )
        sequences = cursor.fetchall()

        for (
            table_schema,
            table_name,
            column_name,
            sequence_schema,
            sequence_name,
            increment,
        ) in sequences:
            table = postgres_sql.Identifier(table_schema, table_name)
            column = postgres_sql.Identifier(column_name)
            sequence = postgres_sql.Identifier(sequence_schema, sequence_name)
            cursor.execute(
                postgres_sql.SQL("SELECT MAX({column}) FROM {table}").format(
                    column=column,
                    table=table,
                )
            )
            maximum_key = cursor.fetchone()[0]
            cursor.execute(
                postgres_sql.SQL("SELECT last_value, is_called FROM {sequence}").format(
                    sequence=sequence
                )
            )
            last_value, is_called = cursor.fetchone()
            if not _sequence_position_is_safe(
                maximum_key=maximum_key,
                last_value=last_value,
                is_called=is_called,
                increment=increment,
            ):
                raise CommandError("Imported sequence positions failed validation.")


def _validate_loaded_database(
    using: str,
    expected_counts: dict[str, int],
    expected_content_sha256: str,
    migrations_before: set[tuple[str, str]],
) -> None:
    connection = connections[using]
    for label, expected in expected_counts.items():
        model = apps.get_model(label)
        if model._default_manager.using(using).count() != expected:
            raise CommandError("Imported model counts failed validation.")

    invalid_scope_users, _ = _student_identity_filters(using)
    if invalid_scope_users:
        raise CommandError("Imported student scope integrity failed validation.")

    missing_student = StudentTermSection.objects.using(using).annotate(
        student_exists=Exists(
            Student.objects.using(using).filter(student_id=OuterRef("student_id"))
        )
    )
    if missing_student.filter(student_exists=False).exists():
        raise CommandError("Imported academic relations failed validation.")
    if TermSection.objects.using(using).filter(scenario__isnull=False).exists():
        raise CommandError("Imported section scope failed validation.")

    connection.check_constraints()
    _validate_postgres_sequences(using)
    for model in apps.get_models(include_auto_created=False):
        label = model._meta.label_lower
        if (
            model._meta.managed
            and label not in ALLOWED_MODELS
            and label not in REGENERATED_MODELS
            and model._default_manager.using(using).exists()
        ):
            raise CommandError("Excluded runtime data remained after replacement.")

    loaded_records, _ = _serialise_records(using)
    if not hmac.compare_digest(canonical_content_sha256(loaded_records), expected_content_sha256):
        raise CommandError("Imported canonical content failed validation.")

    migrations_after = set(MigrationRecorder(connection).applied_migrations())
    if migrations_after != migrations_before:
        raise CommandError("Migration history changed during replacement.")


def _load_verified_fixture(using: str, fixture_text: str) -> None:
    connection = connections[using]
    deferred_objects = []
    loaded_models: set[type[Any]] = set()
    with connection.constraint_checks_disabled():
        objects = serializers.deserialize(
            "json",
            fixture_text,
            using=using,
            ignorenonexistent=False,
            handle_forward_references=True,
        )
        for item in objects:
            model = item.object.__class__
            if not router.allow_migrate_model(using, model):
                raise CommandError("Release seed model is not permitted on the target database.")
            item.save(using=using)
            loaded_models.add(model)
            if item.deferred_fields:
                deferred_objects.append(item)
        for item in deferred_objects:
            item.save_deferred_fields(using=using)

    connection.check_constraints(table_names=[model._meta.db_table for model in loaded_models])
    sequence_sql = connection.ops.sequence_reset_sql(no_style(), list(loaded_models))
    if sequence_sql:
        with connection.cursor() as cursor:
            for statement in sequence_sql:
                cursor.execute(statement)


class Command(BaseCommand):
    help = (
        "Atomically replace a target database with an HMAC-authenticated academic "
        f"release seed. Export and import must receive the same ephemeral "
        f"{RELEASE_SEED_SIGNING_KEY_ENV}."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("fixture", help="Path to the release-seed .json.gz fixture.")
        parser.add_argument(
            "--manifest",
            help="Manifest path; defaults to the fixture's sibling .manifest.json file.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Configured Django database alias to replace (default: default).",
        )
        parser.add_argument(
            "--confirm-replace-target-database",
            default="",
            help=f"Must equal {CONFIRMATION_VALUE} exactly.",
        )
        parser.add_argument(
            "--allow-sqlite-rehearsal",
            action="store_true",
            help="Allow a destructive SQLite rehearsal. Never use for a real import.",
        )
        parser.add_argument(
            "--expected-target-database",
            default="",
            help="First exact PostgreSQL current_database() name confirmation.",
        )
        parser.add_argument(
            "--confirm-target-database-name",
            default="",
            help="Second exact PostgreSQL current_database() name confirmation.",
        )
        parser.add_argument(
            "--expected-target-host",
            default="",
            help="Exact normalized Django PostgreSQL connection HOST confirmation.",
        )
        parser.add_argument(
            "--confirm-target-current-user",
            default="",
            help="Exact PostgreSQL current_user confirmation.",
        )
        parser.add_argument(
            "--confirm-writers-suspended",
            default="",
            help=f"Must equal {WRITERS_SUSPENDED_VALUE} exactly for PostgreSQL.",
        )
        parser.add_argument(
            "--allow-nonproduction-postgres-rehearsal",
            action="store_true",
            help=(
                "Allow a non-production PostgreSQL rehearsal only when the separate "
                f"{POSTGRES_REHEARSAL_ENV} gate is enabled."
            ),
        )
        parser.add_argument(
            "--enable-postgres-connection-fence",
            action="store_true",
            help=(
                "Disable new target-database connections for the import window. Emergency "
                "recovery: from the postgres maintenance database, the owner must run "
                "ALTER DATABASE <confirmed-target> WITH ALLOW_CONNECTIONS true, then verify "
                "the manifest digest and counts before retrying."
            ),
        )
        parser.add_argument(
            "--confirm-connection-fence",
            default="",
            help=f"Must equal {CONNECTION_FENCE_CONFIRMATION_VALUE} exactly.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["confirm_replace_target_database"] != CONFIRMATION_VALUE:
            raise _fail("exact destructive confirmation was not supplied")
        if os.getenv(KILL_SWITCH_ENV) != KILL_SWITCH_VALUE:
            raise _fail("database replacement kill-switch is not enabled")

        using = str(options["database"])
        if using not in connections:
            raise _fail("unknown database alias")
        connection = connections[using]
        if connection.vendor == "sqlite" and not options["allow_sqlite_rehearsal"]:
            raise _fail("SQLite is allowed only for an explicit rehearsal")
        if connection.vendor not in {"postgresql", "sqlite"}:
            raise _fail("unsupported target database backend")
        _validate_sqlite_rehearsal_target(using)

        allow_postgres_rehearsal = bool(options["allow_nonproduction_postgres_rehearsal"])
        fixture_path = Path(str(options["fixture"])).expanduser().resolve()
        manifest_path = _manifest_path(fixture_path, options.get("manifest"))
        (
            manifest,
            expected_counts,
            fixture_text,
            total_records,
            expected_content_sha256,
        ) = _validate_artifact(fixture_path, manifest_path)
        expected_database_name = str(options["expected_target_database"])
        confirmed_database_name = str(options["confirm_target_database_name"])
        expected_host = str(options["expected_target_host"])
        confirmed_current_user = str(options["confirm_target_current_user"])
        writers_confirmation = str(options["confirm_writers_suspended"])
        _validate_postgres_target_context(
            using,
            expected_database_name=expected_database_name,
            confirmed_database_name=confirmed_database_name,
            expected_host=expected_host,
            confirmed_current_user=confirmed_current_user,
            writers_confirmation=writers_confirmation,
            allow_rehearsal=allow_postgres_rehearsal,
        )
        _validate_production_checkout(using, manifest, allow_rehearsal=allow_postgres_rehearsal)

        try:
            with _postgres_connection_fence(
                using,
                database_name=expected_database_name,
                confirmed_current_user=confirmed_current_user,
                enabled=bool(options["enable_postgres_connection_fence"]),
                confirmation=str(options["confirm_connection_fence"]),
            ):
                # This is the authoritative migration check. It runs after new
                # connections are fenced and after a second zero-session check,
                # closing the schema-change race between preflight and replacement.
                _validate_postgres_target_context(
                    using,
                    expected_database_name=expected_database_name,
                    confirmed_database_name=confirmed_database_name,
                    expected_host=expected_host,
                    confirmed_current_user=confirmed_current_user,
                    writers_confirmation=writers_confirmation,
                    allow_rehearsal=allow_postgres_rehearsal,
                )
                migrations_before = _target_migration_state(using, manifest)
                with transaction.atomic(using=using):
                    _take_postgres_advisory_lock(using)
                    _validate_postgres_target_context(
                        using,
                        expected_database_name=expected_database_name,
                        confirmed_database_name=confirmed_database_name,
                        expected_host=expected_host,
                        confirmed_current_user=confirmed_current_user,
                        writers_confirmation=writers_confirmation,
                        allow_rehearsal=allow_postgres_rehearsal,
                    )
                    _flush_target_database(using)
                    _load_verified_fixture(using, fixture_text)
                    _validate_loaded_database(
                        using,
                        expected_counts,
                        expected_content_sha256,
                        migrations_before,
                    )
        except Exception as exc:
            if isinstance(exc, CommandError) and (
                str(exc).startswith("Release seed refused:")
                or str(exc) == EMERGENCY_FENCE_RESTORE_GUIDANCE
            ):
                raise
            raise CommandError(
                "Release seed replacement failed; target transaction was rolled back."
            ) from None

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {total_records} records across {len(ALLOWED_MODELS)} models."
            )
        )
