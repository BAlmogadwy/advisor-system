"""Idempotent migration command for ``ExamTimetableRun.result_json`` payloads.

Run at deploy time when ``EXAM_RUN_SCHEMA_VERSION`` is bumped, so historic
rows are normalised on disk to the current schema. This shifts the
normalisation cost off the read path: every detail-view fetch and XLSX
export still goes through ``load_normalised_run`` defensively, but for
post-migration rows that's a no-op (idempotent identity), not a real
migration.

Examples::

    # Show what would change without writing anything
    python manage.py normalise_exam_runs --dry-run

    # Migrate every row
    python manage.py normalise_exam_runs

    # Re-migrate even rows already at the current version (forces a
    # round-trip through json.dumps; useful if the canonical key order
    # has changed and you want the on-disk JSON regenerated)
    python manage.py normalise_exam_runs --force

The command is idempotent: running it twice on the same database is a
no-op the second time. Each run reports a per-source-version count so
you can see what came in.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from core.models import ExamTimetableRun
from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    load_normalised_run,
)


def _audit_entry(from_version: int, to_version: int, source_text: str) -> dict[str, object]:
    """Build one append-only entry for the ``normalisation_audit`` list.

    Records what changed during a deploy-time migration so the change
    is traceable post-hoc. The ``source_hash`` is sha256 of the raw
    pre-migration ``result_json`` text — sufficient to verify a backup
    matches what the migration started from. We do NOT store the full
    pre-migration text; backups are the operator's responsibility.
    """
    return {
        "at": datetime.now(UTC).isoformat(),
        "from_version": from_version,
        "to_version": to_version,
        "source_hash": hashlib.sha256(
            source_text.encode("utf-8") if source_text else b""
        ).hexdigest(),
    }


class Command(BaseCommand):
    help = (
        "Normalise every ExamTimetableRun.result_json to the current "
        f"schema version (v{EXAM_RUN_SCHEMA_VERSION}). Idempotent."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Re-write rows already at the current schema version. "
                "Useful if canonical key order or default semantics changed "
                "without bumping the version constant."
            ),
        )
        parser.add_argument(
            "--ids",
            nargs="*",
            type=int,
            default=None,
            help="Only process these run ids (default: all rows).",
        )

    def handle(self, *args: object, **options: object) -> None:
        dry_run: bool = bool(options.get("dry_run"))
        force: bool = bool(options.get("force"))
        only_ids: list[int] | None = options.get("ids")  # type: ignore[assignment]

        qs = ExamTimetableRun.objects.all().order_by("id")
        if only_ids:
            qs = qs.filter(id__in=only_ids)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No ExamTimetableRun rows to process."))
            return

        source_versions: Counter[int] = Counter()
        unrenderable_ids: list[int] = []
        future_version_ids: list[int] = []
        migrated: list[int] = []
        skipped_already_current: list[int] = []

        for run in qs.iterator():
            normalised = load_normalised_run(run)
            status = normalised.get("status")
            # Defensive: never overwrite a payload we cannot interpret.
            # Corrupt rows are forensic evidence — the operator may want
            # to investigate before any automated fix-up. Future-version
            # rows came from a newer build of this app; downgrading them
            # could corrupt data the newer build expects to read back.
            if status == "unrenderable":
                unrenderable_ids.append(run.id)
                continue
            if status == "future_version_unrenderable":
                future_version_ids.append(run.id)
                continue

            # Try to read the raw stored version for telemetry; fall
            # back to 0 (legacy) if unreadable. This is informational
            # only — the actual migration was performed by
            # ``load_normalised_run`` already.
            try:
                raw = json.loads(run.result_json)
                if isinstance(raw, dict):
                    raw_version = raw.get("schema_version")
                    src_v = int(raw_version) if isinstance(raw_version, int) else 0
                else:
                    src_v = 0
            except (json.JSONDecodeError, TypeError, ValueError):
                src_v = 0

            source_versions[src_v] += 1

            if src_v >= EXAM_RUN_SCHEMA_VERSION and not force:
                skipped_already_current.append(run.id)
                continue

            # Append-only audit trail: every deploy-time migration
            # records when it ran, what version it migrated from/to,
            # and a sha256 of the pre-migration result_json text. The
            # list is preserved across migrations (a v0->v1->v2 row
            # ends up with two audit entries). No prior history is ever
            # mutated; existing entries are read, the new entry is
            # appended.
            normalised_dict = dict(normalised)
            audit_list = list(normalised_dict.get("normalisation_audit") or [])
            audit_list.append(_audit_entry(src_v, EXAM_RUN_SCHEMA_VERSION, run.result_json or ""))
            normalised_dict["normalisation_audit"] = audit_list

            new_payload = json.dumps(normalised_dict, ensure_ascii=False)
            if new_payload == run.result_json and not force:
                # Identical bytes after re-serialisation AND not migrating
                # versions — skip to avoid pointless write churn. Note: a
                # version bump always produces different bytes because the
                # audit entry is fresh, so this branch only fires for
                # already-current rows under non-force runs.
                skipped_already_current.append(run.id)
                continue

            migrated.append(run.id)
            if not dry_run:
                with transaction.atomic():
                    run.result_json = new_payload
                    run.save(update_fields=["result_json"])

        # ── Report ─────────────────────────────────────────────────
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}Processed {total} ExamTimetableRun row(s). "
            f"Current schema version: v{EXAM_RUN_SCHEMA_VERSION}."
        )
        self.stdout.write(f"{prefix}Source-version distribution:")
        for v in sorted(source_versions):
            self.stdout.write(f"{prefix}  v{v}: {source_versions[v]} row(s)")

        if migrated:
            verb = "Would migrate" if dry_run else "Migrated"
            self.stdout.write(self.style.SUCCESS(f"{prefix}{verb} {len(migrated)} row(s)."))
        else:
            self.stdout.write(f"{prefix}No rows needed migration.")

        if skipped_already_current:
            self.stdout.write(
                f"{prefix}Skipped {len(skipped_already_current)} row(s) "
                f"already at v{EXAM_RUN_SCHEMA_VERSION}."
            )

        if unrenderable_ids:
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{len(unrenderable_ids)} row(s) returned the "
                    "unrenderable sentinel (corrupt / missing JSON). "
                    "These were NOT modified — investigate manually:"
                )
            )
            for rid in unrenderable_ids[:20]:
                self.stdout.write(f"{prefix}  - id={rid}")
            if len(unrenderable_ids) > 20:
                self.stdout.write(f"{prefix}  ... and {len(unrenderable_ids) - 20} more")

        if future_version_ids:
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{len(future_version_ids)} row(s) carry a "
                    f"schema_version higher than this build's "
                    f"v{EXAM_RUN_SCHEMA_VERSION}. These were NOT modified "
                    "— downgrading a future-build payload could corrupt "
                    "data the newer build expects to read back. Either "
                    "upgrade the application or rebuild the run:"
                )
            )
            for rid in future_version_ids[:20]:
                self.stdout.write(f"{prefix}  - id={rid}")
            if len(future_version_ids) > 20:
                self.stdout.write(f"{prefix}  ... and {len(future_version_ids) - 20} more")
