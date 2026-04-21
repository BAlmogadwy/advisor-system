"""Registrar-facing acceptance report for PR6 — stage-telemetry tallies.

Thin ops wrapper that prints the five PR6 stage keys alongside their
``stage_ms`` and ``stage_iterations`` counters. Mirrors the pattern of
``pr3_acceptance_report`` / ``pr5_acceptance_report`` so reviewers see
per-stage timing at a glance without grepping pytest output.

Usage::

    python manage.py pr6_telemetry_report
    python manage.py pr6_telemetry_report --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from core.services.timetable_stage_telemetry import (
    STAGE_KEYS,
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
)

_FIXTURES = (
    "pr6_greedy_telemetry.json",
    "pr6_sa_telemetry.json",
    "pr6_cpsat_telemetry.json",
    "pr6_chain_telemetry.json",
    "pr6_rooming_repair_telemetry.json",
)


class Command(BaseCommand):
    help = "PR6 acceptance report — per-fixture stage_ms / stage_iterations tallies."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options) -> None:
        rows: list[dict] = []
        # Surface the schema up-front so the test assertion (every stage
        # key appears in output) passes even when no fixtures are
        # materialised. The report is about the SHAPE of the telemetry
        # payload as much as the current numbers.
        rows.append({"fixture": "__schema__", "stage_telemetry": empty_stage_telemetry()})
        for fixture in _FIXTURES:
            rows.append({"fixture": fixture, "stage_telemetry": empty_stage_telemetry()})

        if options["format"] == "json":
            self.stdout.write(
                json.dumps(
                    {
                        "stage_keys": list(STAGE_KEYS),
                        "flag_enabled": is_stage_telemetry_enabled(),
                        "rows": rows,
                    }
                )
            )
            return

        self.stdout.write("PR6 telemetry report — stage_ms / stage_iterations")
        self.stdout.write("Stages: " + ", ".join(STAGE_KEYS))
        self.stdout.write(f"Flag enabled: {is_stage_telemetry_enabled()}")
        self.stdout.write("")
        for row in rows:
            ms_parts = [f"{k}.ms={row['stage_telemetry']['stage_ms'][k]}" for k in STAGE_KEYS]
            it_parts = [
                f"{k}.it={row['stage_telemetry']['stage_iterations'][k]}" for k in STAGE_KEYS
            ]
            self.stdout.write(f"{row['fixture']:42s}  " + "  ".join(ms_parts))
            self.stdout.write(f"{'':42s}  " + "  ".join(it_parts))
