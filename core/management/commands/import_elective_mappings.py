"""Publish elective mappings from a reviewed file.

Dry-run by default, like every destructive command here. The report and the write
compute the SAME plan from the same code, so what the dry run prints is what an
apply would do — a report produced by a different path is a report about a
different program.

    python manage.py import_elective_mappings mappings.csv
    python manage.py import_elective_mappings mappings.csv --apply

Replacement is never inferred from omission. A file listing two of a slot's three
approved courses is one somebody forgot to finish, not an instruction to withdraw
the third:

    python manage.py import_elective_mappings mappings.csv --apply \\
        --replace-year 1448 --replace-term 1

`--reversal-out` writes what would be REMOVED back in input format, so a
publication can be undone from the state that existed rather than by replaying the
instruction that changed it.
"""

from __future__ import annotations

import pathlib
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.elective_import import apply_plan, as_csv, build_plan


class Command(BaseCommand):
    help = "Validate and publish elective slot-to-course mappings from a CSV file."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("path", help="CSV with the columns listed in the module docstring.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without it this validates and reports only.",
        )
        parser.add_argument("--replace-year", default="", help="With --replace-term: see below.")
        parser.add_argument(
            "--replace-term",
            default="",
            help="Remove mappings for this year/term that the file does not list. "
            "Both flags are required together; omission alone never deletes.",
        )
        parser.add_argument(
            "--reversal-out",
            default="",
            help="Write the rows that would be REMOVED to this path, in input format.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = pathlib.Path(options["path"])
        if not path.exists():
            raise CommandError(f"no such file: {path}")

        replace_year = str(options["replace_year"] or "").strip()
        replace_term = str(options["replace_term"] or "").strip()
        if bool(replace_year) != bool(replace_term):
            raise CommandError("--replace-year and --replace-term must be given together")

        plan = build_plan(
            path.read_text(encoding="utf-8-sig"),
            replace_year=replace_year,
            replace_term=replace_term,
        )

        if not plan.ok:
            self.stdout.write(
                self.style.ERROR(f"{len(plan.problems)} problem(s); nothing written.")
            )
            for problem in plan.problems:
                self.stdout.write(f"  {problem}")
            # All-or-nothing. A partially applied publication leaves a slot
            # half-approved with no record of which half, and the readiness gate
            # opens on it.
            raise CommandError("the file failed validation")

        self.stdout.write(plan.summary())
        for record in plan.add:
            self.stdout.write(
                f"  + {record['academic_year']}/{record['term']} "
                f"{record['programme']}/{record['slot_code']} -> {record['course_code']}"
            )
        for record in plan.remove:
            self.stdout.write(
                self.style.WARNING(
                    f"  - {record['academic_year']}/{record['term']} "
                    f"{record['programme']}/{record['slot_code']} (elective {record['elective_id']})"
                )
            )

        if options["reversal_out"] and plan.remove:
            pathlib.Path(options["reversal_out"]).write_text(as_csv(plan.remove), encoding="utf-8")
            self.stdout.write(f"reversal written to {options['reversal_out']}")

        if not options["apply"]:
            self.stdout.write("Dry run — nothing written. Re-run with --apply to publish.")
            return

        result = apply_plan(plan)
        self.stdout.write(
            self.style.SUCCESS(
                f"Published: {result['added']} added, {result['retained']} already present, "
                f"{result['removed']} removed."
            )
        )
        self.stdout.write("Run `elective_mapping_readiness --with-students` to confirm the gate.")
