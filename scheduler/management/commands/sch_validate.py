"""Build a naive reference board and certify it independently — read-only.

S2 of the new subsystem. Writes nothing. Proves the rulebook and the checker work
end-to-end on real input, and establishes the baseline S3's real solver must beat.

    python manage.py sch_validate --year 1448 --term 1 --programs AI,AI2,DS,DS2 --gender M
    python manage.py sch_validate --year 1448 --term 1 --programs AI,DS --gender F --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from scheduler.intake import IntakeError, build_snapshot
from scheduler.placement import place_naively
from scheduler.rules import Enforcement, Severity
from scheduler.validate import validate

_MARK = {
    Enforcement.CHECK: "pass ",
    Enforcement.NOT_APPLICABLE: "n/a  ",
    Enforcement.EVIDENCE_GAP: "GAP  ",
    Enforcement.COVERAGE_GAP: "GAP  ",
}


class Command(BaseCommand):
    help = "Build a naive reference board and certify it against the rulebook (read-only)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--year", required=True)
        parser.add_argument("--term", type=int, required=True)
        parser.add_argument("--programs", required=True)
        parser.add_argument("--gender", required=True, choices=["M", "F", "m", "f"])
        parser.add_argument("--default-capacity", type=int, default=25)
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--show", type=int, default=5, help="violations to list per rule")

    def handle(self, *args, **options) -> None:
        try:
            snapshot = build_snapshot(
                academic_year=options["year"],
                term=options["term"],
                gender=options["gender"],
                programs=str(options["programs"]).split(","),
                default_capacity=options["default_capacity"],
            )
        except IntakeError as exc:
            raise CommandError(str(exc)) from exc

        board = place_naively(snapshot)
        report = validate(snapshot, board)

        if options["format"] == "json":
            self.stdout.write(json.dumps(report.as_dict(), indent=2, default=str))
            return

        w = self.stdout.write
        b = report.board_summary
        w(f"Naive reference board — {snapshot.academic_year} T{snapshot.term} {snapshot.gender}")
        w(
            f"  certification   : {report.certification.value}  ({report.violation_count} violations)"
        )
        w(f"  placements      : {b['placements']} across {b['sections_placed']} sections")
        w(
            f"  physical/roomed : {b['physical_meetings']} / {b['roomed']}  (unroomed {b['unroomed']})"
        )
        w(
            f"  fingerprints    : snapshot {report.snapshot_fingerprint[:16]} | "
            f"rulebook {report.rulebook_fingerprint[:16]}"
        )
        w("")
        for result in report.results:
            if result.severity is Severity.OBSERVED:
                w(f"  [obs  ] {result.rule_id:<14} {result.note}")
                continue
            mark = "FAIL " if result.violations else _MARK.get(result.enforcement, "?    ")
            detail = (
                f"{len(result.violations)} violations"
                if result.violations
                else (result.note or "ok")
            )
            w(f"  [{mark}] {result.rule_id:<14} {result.title:<34} {detail}")
            for violation in result.violations[: options["show"]]:
                w(f"            - {violation.message}")
            if len(result.violations) > options["show"]:
                w(f"            ... and {len(result.violations) - options['show']} more")
