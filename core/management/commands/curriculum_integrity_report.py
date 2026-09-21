"""Does any prerequisite name a course its own programme's plan does not contain?

The check itself lives in `core.services.curriculum_integrity`, shared with the
regression test that pins it — a report and a test disagreeing about what counts
as an orphan is exactly the drift this arrangement prevents.

The class is silent by construction: an unsatisfiable prerequisite raises nothing,
it simply blocks its course forever, and downstream the graduation forecast just
stops producing an estimate.  `DS2/MATH471 -> MATH204` cost all 191 DS2 students
their forecast and was found only by tracing back from the missing number.

Read-only.  It changes nothing and is safe to run against production.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from core.services.curriculum_integrity import find_orphan_prerequisites


class Command(BaseCommand):
    help = "Report prerequisite rows naming a course absent from that programme's plan."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--program",
            action="append",
            dest="programs",
            help="Limit to this programme (repeatable). Default: every programme.",
        )
        parser.add_argument(
            "--fail-on-orphan",
            action="store_true",
            help="Exit non-zero if any orphan is found. For a gate.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        findings = find_orphan_prerequisites(programs=options.get("programs"))
        if not findings:
            self.stdout.write(
                self.style.SUCCESS("No orphan prerequisites: every referenced course is in plan.")
            )
            return

        self.stdout.write(self.style.ERROR(f"{len(findings)} orphan prerequisite reference(s):"))
        for finding in findings:
            self.stdout.write(f"  {finding.describe()}")
        self.stdout.write(
            "\nEach of these can never be satisfied. The course it guards is blocked for "
            "every student in that programme, and any credit-hour gate downstream of it "
            "goes with it."
        )
        if options.get("fail_on_orphan"):
            raise SystemExit(1)
