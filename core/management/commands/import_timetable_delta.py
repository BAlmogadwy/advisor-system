from __future__ import annotations

import json
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.timetable_delta_import import (
    EXPECTED_OPERATION_KEYS,
    import_timetable_delta_artifact,
)


def _parse_expected_counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values:
        key, separator, value = str(raw).partition("=")
        if not separator or key not in EXPECTED_OPERATION_KEYS:
            raise CommandError(
                "--expect-count must use a supported KEY=COUNT; supported keys: "
                + ", ".join(EXPECTED_OPERATION_KEYS)
            )
        if key in result:
            raise CommandError(f"Duplicate --expect-count for {key}.")
        try:
            count = int(value)
        except ValueError as exc:
            raise CommandError(f"Expected count for {key} must be an integer.") from exc
        if count < 0:
            raise CommandError(f"Expected count for {key} cannot be negative.")
        result[key] = count
    return result


class Command(BaseCommand):
    help = (
        "Dry-run or atomically apply a SHA-pinned 1448/1 scraper timetable delta. "
        "Dry-run is the default."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("artifact", help="Path to the timetable delta JSON artifact")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the validated delta; without this flag no rows are changed.",
        )
        parser.add_argument(
            "--expect-sha256",
            default=None,
            help="Exact SHA-256 of the artifact; required with --apply.",
        )
        parser.add_argument(
            "--expect-base-state-sha256",
            default=None,
            help="Exact production logical base digest; required with --apply.",
        )
        parser.add_argument(
            "--expect-count",
            action="append",
            default=[],
            metavar="KEY=COUNT",
            help=(
                "Expected locked operation count; repeat once for every key printed by dry-run. "
                "All keys are required with --apply."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            expected_counts = _parse_expected_counts(list(options["expect_count"]))
            summary = import_timetable_delta_artifact(
                options["artifact"],
                apply=bool(options["apply"]),
                expected_artifact_sha256=options["expect_sha256"],
                expected_base_state_sha256=options["expect_base_state_sha256"],
                expected_operations=expected_counts or None,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        serialized = json.dumps(summary, ensure_ascii=True, sort_keys=True)
        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(serialized))
        elif summary.get("mode") == "already_applied":
            self.stdout.write(serialized)
            self.stdout.write(
                "TARGET ALREADY PRESENT: the complete target was verified and zero writes "
                "were performed."
            )
        else:
            self.stdout.write(serialized)
            self.stdout.write(
                "DRY RUN ONLY: re-run with --apply, both SHA expectations, and every "
                "--expect-count KEY=COUNT shown above."
            )
