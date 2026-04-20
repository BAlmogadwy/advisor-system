"""Registrar-facing acceptance report for the PR3 scenario pack.

Thin wrapper over ``tests/test_pr3_acceptance_pack.run_pr3_fixture`` so a
reviewer can run the same placement exercises as the CI gate without
reading pytest output. The command intentionally does NOT re-implement
the bars — it reports what each fixture produced and flags any issue
the pytest module would have failed on.

Per ChatGPT commit-7 ruling (c): the pytest module is the authoritative
CI gate; this command is the ops surface. Keep them in sync by sharing
the runner.

Usage::

    python manage.py pr3_acceptance_report
    python manage.py pr3_acceptance_report --fixture pr3_canonical_warm_start.json
    python manage.py pr3_acceptance_report --format json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand

# The acceptance-pack runner lives in ``tests/`` so the pytest module
# and the management command observe identical setup. Add that directory
# to sys.path so the import works regardless of cwd.
_TESTS_DIR = Path(django_settings.BASE_DIR) / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


class Command(BaseCommand):
    help = "Run every PR3 scenario fixture and print a coverage / metric report."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--fixture",
            help=(
                "Run a single fixture (basename, e.g. "
                "'pr3_canonical_warm_start.json') instead of the whole pack."
            ),
        )
        parser.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            help="Output format (default: table)",
        )

    def handle(self, *args, **options) -> None:
        # Import lazily so `manage.py help` doesn't drag the test
        # runner stack in for every unrelated invocation.
        from test_pr3_acceptance_pack import (
            KNOWN_REJECTION_CODES,
            _collect_rejection_codes,
            _trace_enabled_for_fixture,
            discover_pack,
            run_pr3_fixture,
        )

        target = options.get("fixture")
        fixtures = [target] if target else discover_pack()

        rows: list[dict] = []
        total_placed = 0
        total_traced = 0

        for fixture_name in fixtures:
            if not os.path.exists(
                _TESTS_DIR.parent
                / "snapshots"
                / "planner-refactor-2026-04-20"
                / "fixtures"
                / fixture_name
            ):
                self.stderr.write(self.style.ERROR(f"Unknown fixture: {fixture_name}"))
                continue

            result = run_pr3_fixture(fixture_name)
            placed = result.get("placed", 0)
            trace_on = _trace_enabled_for_fixture(fixture_name)
            decision_trace = result.get("decision_trace", {}) or {}
            traced = len(decision_trace)
            metric = result.get("perturbation_metric", {}) or {}
            codes = _collect_rejection_codes(decision_trace)
            unknown = codes - KNOWN_REJECTION_CODES

            # "Sections missing alternatives" is a useful signal for the
            # registrar: even with trace enabled, a placement without
            # alternatives is a section whose candidate space was single-
            # valued (or whose losers all landed on the same slot as the
            # winner). Not a failure — just a thing to look at.
            missing_alts = sorted(
                section_code
                for section_code, entry in decision_trace.items()
                if not entry.get("alternatives")
            )

            coverage = (traced / placed) if (trace_on and placed > 0) else None

            rows.append(
                {
                    "fixture": fixture_name,
                    "placed": placed,
                    "trace_enabled": trace_on,
                    "traced": traced,
                    "coverage": coverage,
                    "perturbation_metric": metric,
                    "sections_missing_alternatives": missing_alts,
                    "unknown_rejection_codes": sorted(unknown),
                }
            )

            if trace_on and placed > 0:
                total_placed += placed
                total_traced += traced

        overall_coverage = (total_traced / total_placed) if total_placed else None

        if options["format"] == "json":
            self.stdout.write(
                json.dumps(
                    {
                        "fixtures": rows,
                        "overall_coverage": overall_coverage,
                        "total_placed": total_placed,
                        "total_traced": total_traced,
                    },
                    indent=2,
                )
            )
            return

        # Table format — fits a normal terminal without horizontal scroll.
        # Alignment is fixed-width so a registrar can scan the columns
        # without a spreadsheet.
        self.stdout.write("")
        self.stdout.write("PR3 acceptance pack report")
        self.stdout.write("=" * 78)

        header = f"{'fixture':<48} {'placed':>6} {'traced':>6} {'cov':>6}"
        self.stdout.write(header)
        self.stdout.write("-" * 78)

        for row in rows:
            cov = row["coverage"]
            # ASCII "-" instead of an em-dash so the report renders
            # cleanly on Windows cp1252 consoles.
            cov_str = f"{cov:.2f}" if cov is not None else "   -"
            self.stdout.write(
                f"{row['fixture']:<48} {row['placed']:>6} {row['traced']:>6} {cov_str:>6}"
            )

            metric = row["perturbation_metric"]
            if metric:
                counters = (
                    f"    unchanged={metric.get('unchanged_count', 0)} "
                    f"changed={metric.get('changes_from_baseline_count', 0)} "
                    f"newly_placed={metric.get('newly_placed_count', 0)} "
                    f"removed={metric.get('removed_count', 0)}"
                )
                self.stdout.write(counters)

            if row["sections_missing_alternatives"]:
                self.stdout.write(
                    "    sections_missing_alternatives: "
                    + ", ".join(row["sections_missing_alternatives"])
                )

            if row["unknown_rejection_codes"]:
                self.stdout.write(
                    self.style.ERROR(
                        "    UNKNOWN rejection codes: " + ", ".join(row["unknown_rejection_codes"])
                    )
                )

        self.stdout.write("-" * 78)
        overall_cov_str = f"{overall_coverage:.3f}" if overall_coverage is not None else "—"
        self.stdout.write(
            f"overall: placed={total_placed} traced={total_traced} coverage={overall_cov_str}"
        )

        any_unknown = any(row["unknown_rejection_codes"] for row in rows)
        if any_unknown:
            self.stdout.write(
                self.style.ERROR("FAIL: unknown rejection codes present in at least one fixture.")
            )
        if overall_coverage is not None and overall_coverage < 0.90:
            self.stdout.write(
                self.style.ERROR(
                    f"FAIL: overall coverage {overall_coverage:.3f} is below the 0.90 floor."
                )
            )
        if not any_unknown and (overall_coverage is None or overall_coverage >= 0.90):
            self.stdout.write(self.style.SUCCESS("OK: acceptance bars satisfied."))
