"""Build and assess a scheduler input snapshot — read-only.

S1 of the new subsystem. Writes nothing: it extracts an immutable, fingerprinted
snapshot from institutional data and reports what that input can produce, before
any solving happens.

    python manage.py sch_snapshot --year 1448 --term 1 --programs AI,AI2,DS,DS2 --gender M
    python manage.py sch_snapshot --year 1448 --term 1 --programs AI,DS --gender F --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from scheduler.intake import IntakeError, build_snapshot
from scheduler.readiness import Severity, assess

_MARK = {Severity.BLOCKING: "BLOCK", Severity.WARNING: "WARN ", Severity.INFO: "info "}


class Command(BaseCommand):
    help = "Build a scheduler snapshot from institutional data and report readiness."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--year", required=True, help="Hijri academic year, e.g. 1448")
        parser.add_argument("--term", type=int, required=True)
        parser.add_argument("--programs", required=True, help="comma-separated, e.g. AI,AI2,DS,DS2")
        parser.add_argument("--gender", required=True, choices=["M", "F", "m", "f"])
        parser.add_argument(
            "--default-capacity",
            type=int,
            default=25,
            help="section size assumed where programme_requirements.max_capacity is NULL "
            "(mode of the declared data is 25). Its blast radius is always reported.",
        )
        parser.add_argument("--buffer", type=float, default=1.0)
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options) -> None:
        try:
            snapshot = build_snapshot(
                academic_year=options["year"],
                term=options["term"],
                gender=options["gender"],
                programs=str(options["programs"]).split(","),
                default_capacity=options["default_capacity"],
                buffer=options["buffer"],
            )
        except IntakeError as exc:
            raise CommandError(str(exc)) from exc

        report = assess(snapshot)

        if options["format"] == "json":
            self.stdout.write(json.dumps(report.as_dict(), indent=2, default=str))
            return

        s = report.snapshot_summary
        w = self.stdout.write
        w(f"Snapshot {s['academic_year']} T{s['term']} {s['gender']} — {', '.join(s['programs'])}")
        w(f"  status            : {report.status}")
        w(f"  students          : {s['students']:,}")
        w(f"  offerings/sections: {s['offerings']} / {s['sections']}")
        rooms = ", ".join(f"{k} {v}" for k, v in sorted(s["rooms"].items()))
        w(f"  rooms             : {rooms}")
        meetings = ", ".join(f"{k} {v}" for k, v in sorted(s["physical_meetings_per_week"].items()))
        w(f"  physical meetings : {meetings or 'none'} per week")
        cov = s["instructor_coverage"]
        w(
            f"  instructor linkage: {cov['sections_with_instructor']}/{cov['sections_total']} "
            f"sections ({cov['percent']}%) — partial by design"
        )
        w(
            f"  fingerprints      : snapshot {s['snapshot_fingerprint']} | grid {s['grid_fingerprint']} | source {s['source_fingerprint']}"
        )

        if report.findings:
            w("")
            w("  findings:")
            for finding in report.findings:
                w(f"    [{_MARK[finding.severity]}] {finding.code}: {finding.message}")
                for key in ("offerings", "shape"):
                    value = finding.detail.get(key)
                    if isinstance(value, list) and value:
                        shown = ", ".join(str(v) for v in value[:10])
                        more = f" (+{len(value) - 10} more)" if len(value) > 10 else ""
                        w(f"             {key}: {shown}{more}")
        if report.blocking:
            w("")
            w(
                f"  {len(report.blocking)} blocking finding(s) — this input cannot yield a valid board as-is."
            )
