from __future__ import annotations

import hashlib
import os
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.timetable_delta import (
    TimetableDeltaError,
    build_timetable_delta,
    canonical_json_bytes,
    capture_sqlite_timetable_state,
    same_file,
    validate_frozen_snapshot,
)


def _sidecar_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.name}.sha256")


def _lock_path(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}.export.lock")


def _configured_live_sqlite_path() -> Path | None:
    database = settings.DATABASES.get("default") or {}
    if database.get("ENGINE") != "django.db.backends.sqlite3":
        return None
    name = str(database.get("NAME") or "")
    if not name or name == ":memory:":
        return None
    if name.startswith("file:"):
        parsed = urlsplit(name)
        if "mode=memory" in parsed.query:
            return None
        raw_path = unquote(parsed.path)
        if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2:
            raw_path = raw_path[1:]
        return Path(raw_path).expanduser().resolve() if raw_path else None
    return Path(name).expanduser().resolve()


def _validate_sources(base_path: Path, target_path: Path) -> tuple[Path, Path]:
    base = validate_frozen_snapshot(base_path)
    target = validate_frozen_snapshot(target_path)
    if same_file(base, target):
        raise TimetableDeltaError("Baseline and target must be distinct frozen snapshots.")
    live_database = _configured_live_sqlite_path()
    if live_database is not None and live_database.exists():
        if same_file(base, live_database) or same_file(target, live_database):
            raise TimetableDeltaError(
                "Export from separate frozen SQLite copies, not the live local database."
            )
    return base, target


def _same_resolved_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        return same_file(left, right)
    return False


def _validate_destination_paths(
    output_path: Path,
    sidecar_path: Path,
    *,
    base_path: Path,
    target_path: Path,
) -> None:
    protected = [base_path, target_path]
    live_database = _configured_live_sqlite_path()
    if live_database is not None:
        protected.append(live_database)
    for candidate in (output_path, sidecar_path, _lock_path(output_path)):
        if any(_same_resolved_path(candidate, protected_path) for protected_path in protected):
            raise TimetableDeltaError(
                "Artifact output and SHA-256 sidecar must not overwrite a source snapshot "
                "or the configured live SQLite database."
            )


def _write_artifacts(
    output_path: Path,
    payload: bytes,
    sidecar_path: Path,
    sidecar_payload: bytes,
    *,
    force: bool,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(output_path)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CommandError(
            "Another timetable-delta export is active for this destination."
        ) from exc

    temporary_paths: list[Path] = []
    try:
        os.close(lock_fd)
        if not force and (output_path.exists() or sidecar_path.exists()):
            raise CommandError(
                "Timetable delta already exists; pass --force to replace both files."
            )

        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            output_tmp = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_paths.append(output_tmp)

        with tempfile.NamedTemporaryFile(
            prefix=f".{sidecar_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            sidecar_tmp = Path(handle.name)
            handle.write(sidecar_payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_paths.append(sidecar_tmp)

        os.replace(sidecar_tmp, sidecar_path)
        temporary_paths.remove(sidecar_tmp)
        # Publish the artifact last: its presence is the commit marker. A failed
        # sidecar replace therefore cannot expose a new artifact with a stale or
        # missing digest file.
        os.replace(output_tmp, output_path)
        temporary_paths.remove(output_tmp)
        try:
            os.chmod(output_path, 0o600)
            os.chmod(sidecar_path, 0o600)
        except OSError:
            # Windows ACLs, rather than POSIX mode bits, control the final access.
            pass
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


class Command(BaseCommand):
    help = (
        "Compare two separate frozen SQLite snapshots and export a deterministic, "
        "natural-key timetable delta. This command never reads or writes the live database."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("baseline", help="Frozen SQLite snapshot matching the online baseline.")
        parser.add_argument("target", help="Newer frozen SQLite snapshot to publish.")
        parser.add_argument("output", help="Destination JSON artifact path.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Atomically replace an existing artifact and SHA-256 sidecar.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        output_path = Path(str(options["output"])).expanduser().resolve()
        sidecar_path = _sidecar_path(output_path)
        try:
            base_path, target_path = _validate_sources(
                Path(str(options["baseline"])), Path(str(options["target"]))
            )
            _validate_destination_paths(
                output_path,
                sidecar_path,
                base_path=base_path,
                target_path=target_path,
            )
            base = capture_sqlite_timetable_state(base_path)
            target = capture_sqlite_timetable_state(target_path)
            artifact = build_timetable_delta(base, target)
        except TimetableDeltaError as exc:
            raise CommandError(str(exc)) from exc

        payload = canonical_json_bytes(artifact) + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        _write_artifacts(
            output_path,
            payload,
            sidecar_path,
            f"{digest}\n".encode("ascii"),
            force=bool(options["force"]),
        )
        self.stdout.write(
            self.style.SUCCESS(f"Exported timetable delta to {output_path} (sha256={digest}).")
        )
        self.stdout.write(f"SHA-256 sidecar: {sidecar_path}")
