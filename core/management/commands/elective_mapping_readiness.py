"""Which programmes are ready for an elective-options screen, and which are not.

The gate itself lives in `core.services.elective_readiness`, shared with the
student surface — a report and a screen disagreeing about readiness is exactly the
failure this exists to prevent. This is the operational view over it.

Live today, for the programmes that actually have students: 2 of 28 slots ready,
zero programmes ready, 2035 student-slot pairs would see an empty screen.

Read-only. It changes nothing and is safe to run against production.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand

from core.services.elective_readiness import readiness


class Command(BaseCommand):
    help = "Report whether each programme's elective slots are ready to be shown to students."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--with-students",
            action="store_true",
            help="Only programmes that have at least one student on file.",
        )
        parser.add_argument(
            "--fail-on-unready",
            action="store_true",
            help="Exit non-zero if any reported programme is not ready. For a gate.",
        )
        parser.add_argument("--year", default="", help="Defaults to the configured term.")
        parser.add_argument("--term", default="", help="Defaults to the configured term.")

    def handle(self, *args: Any, **options: Any) -> None:
        rows = readiness(options["year"], options["term"])
        if options["with_students"]:
            rows = [r for r in rows if r["students"] > 0]
        if not rows:
            self.stdout.write("No elective slots found.")
            return

        from core.services.planner_drafts import planning_term

        year = options["year"] or planning_term()[0]
        term = options["term"] or planning_term()[1]
        self.stdout.write(f"Elective mapping readiness for {year}/term {term}")
        self.stdout.write("")
        head = f"{'Programme':<10} {'Slot':<7} {'Students':>8} {'Mapping':>8} {'Options':>8}  Ready"
        self.stdout.write(head)
        self.stdout.write("-" * len(head))
        for r in rows:
            self.stdout.write(
                f"{r['programme']:<10} {r['slot']:<7} {r['students']:>8} "
                f"{'Yes' if r['mapping_exists'] else 'No':>8} {r['active_options']:>8}  "
                f"{'Yes' if r['ready'] else 'No'}"
            )
            for problem in r["problems"]:
                self.stdout.write(f"{'':<10} {'':<7} {'':>8} {'':>8} {'':>8}    ! {problem}")

        by_programme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_programme[r["programme"]].append(r)
        ready = sorted(p for p, rs in by_programme.items() if all(x["ready"] for x in rs))
        unready = sorted(p for p, rs in by_programme.items() if not all(x["ready"] for x in rs))

        self.stdout.write("")
        self.stdout.write(f"Programmes ready:     {', '.join(ready) if ready else 'none'}")
        self.stdout.write(f"Programmes NOT ready: {', '.join(unready) if unready else 'none'}")
        blocked = sum(r["students"] for r in rows if not r["ready"])
        self.stdout.write(
            f"{sum(1 for r in rows if r['ready'])}/{len(rows)} slots ready; "
            f"{blocked} student-slot pairs would see an empty screen."
        )

        if options["fail_on_unready"] and unready:
            raise SystemExit(1)
