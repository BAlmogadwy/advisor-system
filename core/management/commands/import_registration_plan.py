"""Seed registered timetables from an approved registration-plan workbook.

Dry-run by default, like every destructive command here. The report and the write
compute the SAME plan from the same code, so what the dry run prints is what an
apply would do.

    python manage.py import_registration_plan plan.xlsx --year 1448 --term 1
    python manage.py import_registration_plan plan.xlsx --year 1448 --term 1 --apply

`--expect-links` and `--expect-students` are drift guards: a workbook that no
longer produces the numbers you checked stops rather than writing something else.
"""

from __future__ import annotations

import pathlib
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.registration_plan_import import (
    apply_plan,
    build_plan,
    check_students_exist,
)

ROSTERS_SHEET = "Section Rosters"
DETAIL_SHEET = "Student Courses (detail)"


class Command(BaseCommand):
    help = "Link students to term sections from a registration-plan workbook."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("path")
        parser.add_argument("--year", required=True)
        parser.add_argument("--term", required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without it this validates and reports only.",
        )
        parser.add_argument("--expect-links", type=int, default=None)
        parser.add_argument("--expect-students", type=int, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        import openpyxl

        path = pathlib.Path(options["path"])
        if not path.exists():
            raise CommandError(f"no such file: {path}")

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sheet in (ROSTERS_SHEET, DETAIL_SHEET):
            if sheet not in workbook.sheetnames:
                raise CommandError(f"the workbook has no {sheet!r} sheet")

        def rows(name: str) -> list[tuple]:
            return list(workbook[name].iter_rows(values_only=True))[1:]

        year, term = str(options["year"]).strip(), str(options["term"]).strip()
        plan = build_plan(rows(ROSTERS_SHEET), rows(DETAIL_SHEET), year, term)

        self.stdout.write(f"term {year}/{term}")
        self.stdout.write(plan.summary())

        # Reported, never repaired. The owner's decision is link-only: a section
        # whose time moved in the plan keeps its database time, and a student would
        # otherwise be shown a slot that contradicts the plan that seated them.
        if plan.time_disagreements:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(plan.time_disagreements)} section(s) differ from the database. "
                    "Times are NOT changed by this command:"
                )
            )
            for item in plan.time_disagreements:
                self.stdout.write(f"  {item['course']} {item['file_section']}")
                self.stdout.write(f"     workbook: {item['file_times']}")
                for section_id, times in item["database_times"].items():
                    self.stdout.write(f"     database #{section_id}: {times}")

        # THE GAP. Reported prominently because the seeded timetable is incomplete
        # by exactly this much, and a student looking at a blank Sunday evening
        # cannot tell "nothing scheduled" from "we do not hold it".
        if plan.uncovered:
            total = sum(len(v) for v in plan.uncovered.values())
            self.stdout.write(
                self.style.WARNING(
                    f"\nGAP — {total} registration(s) cannot be seeded because no section "
                    f"exists for these {len(plan.uncovered)} course(s):"
                )
            )
            for course, rows in sorted(plan.uncovered.items(), key=lambda kv: -len(kv[1])):
                slots = sorted({str(r["times"]) for r in rows})
                self.stdout.write(
                    f"  {course:8} {len(rows):4} registration(s), "
                    f"{len({r['student_id'] for r in rows})} student(s), "
                    f"{len(slots)} distinct slot(s) in the plan"
                )
                for slot in slots[:3]:
                    self.stdout.write(f"       {slot}")
            self.stdout.write(
                "  These students will have an INCOMPLETE week until those sections exist."
            )

        if not plan.ok:
            self.stdout.write(
                self.style.ERROR(f"\n{len(plan.problems)} problem(s); nothing written.")
            )
            for problem in plan.problems[:40]:
                self.stdout.write(f"  {problem}")
            raise CommandError("the workbook failed validation")

        missing = check_students_exist(plan)
        if missing:
            # Never invent a Student row from a registration file.
            raise CommandError(
                f"{len(missing)} student(s) in the workbook have no Student record: {missing[:10]}"
            )

        for flag, actual in (
            ("expect_links", len(plan.links)),
            ("expect_students", len(plan.students)),
        ):
            expected = options.get(flag)
            if expected is not None and expected != actual:
                raise CommandError(
                    f"--{flag.replace('_', '-')} says {expected}, the workbook produces {actual}"
                )

        if not options["apply"]:
            self.stdout.write("\nDry run — nothing written. Re-run with --apply to seed.")
            return

        result = apply_plan(plan, year, term)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSeeded: {result['written']} links for {result['students']} students "
                f"({result['removed']} pre-existing rows for those students replaced)."
            )
        )
