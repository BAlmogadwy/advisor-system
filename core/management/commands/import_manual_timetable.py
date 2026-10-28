"""Read a hand-made timetable (CSV, one row per meeting) into a scenario.

Dry-run by default, like every writing command here. Nothing is written without
``--apply``, and the run that reports and the run that writes build the same plan
from the same code.

    python manage.py import_manual_timetable timetable.csv --scenario-id 71
    python manage.py import_manual_timetable timetable.csv --scenario-id 71 --apply

The scenario must already exist: generate it first, so its boards, per-course
budgets and student links come from the roster. A timetable carries none of that.
"""

from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.manual_timetable_import import (
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    ImportFailed,
    apply_plan,
    build_plan,
)


class Command(BaseCommand):
    help = (
        "Import a hand-made timetable CSV into an existing workspace scenario. "
        f"Columns: {', '.join(REQUIRED_COLUMNS)} (required); "
        f"{', '.join(OPTIONAL_COLUMNS)} (optional). Dry-run unless --apply."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("csv_path", help="The timetable CSV, one row per meeting")
        parser.add_argument(
            "--scenario-id",
            required=True,
            type=int,
            help="The scenario to fill (generate its scaffold first)",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the plan. Without it nothing is written.",
        )
        parser.add_argument(
            "--prefer-programme",
            default="",
            metavar="CODE",
            help=(
                "Mark every section of the sheet 'mainly for' this programme, e.g. AI2: "
                "its students are seated first and others prefer the course's other "
                "sections. Never costs a seat. A preferred_programme column overrides it."
            ),
        )
        parser.add_argument(
            "--allow-existing",
            action="store_true",
            help=(
                "Import although the scenario already holds placements. Generate builds "
                "a timetable by default, so this usually means adding the sheet's "
                "sections beside an engine's."
            ),
        )
        parser.add_argument(
            "--allow-offgrid",
            action="store_true",
            help=(
                "Accept meetings whose start matches no column of this scenario. "
                "They are saved but not drawn on the grid until the slots are changed."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        plan = build_plan(
            options["csv_path"],
            int(options["scenario_id"]),
            allow_offgrid=bool(options["allow_offgrid"]),
            prefer_programme=str(options["prefer_programme"] or ""),
            allow_existing=bool(options["allow_existing"]),
        )

        for warning in plan.warnings:
            self.stdout.write(self.style.WARNING(f"note: {warning}"))

        if plan.problems:
            self.stdout.write(
                self.style.ERROR(f"{len(plan.problems)} problem(s); nothing was written:")
            )
            for problem in plan.problems:
                self.stdout.write(f"  {problem}")
            if plan.sections:
                self.stdout.write(
                    f"({len(plan.sections)} section(s) would have been fine; "
                    "the whole sheet is refused so a half-imported timetable is never left.)"
                )
            raise CommandError("Fix the sheet and run again.")

        if not plan.sections:
            raise CommandError("The sheet has no readable sections.")

        self.stdout.write(
            f"{len(plan.sections)} section(s), {plan.meeting_count} meeting(s), "
            f"on scenario {plan.scenario_id}:"
        )
        for section in plan.sections:
            where = " ".join(
                f"{m['day']} {m['start_time']}-{m['end_time']}"
                + (f" {m['room']}" if m["room"] else " (no room)")
                for m in section.meetings
            )
            notes = f" mainly {section.preferred_programme}" if section.preferred_programme else ""
            notes += ", locked" if section.locked else ""
            self.stdout.write(
                f"  {section.board_label} | {section.course_code} {section.section} "
                f"(cap {section.capacity}{notes}) | {where}"
            )

        if not options["apply"]:
            self.stdout.write(self.style.SUCCESS("Dry run: nothing written. Add --apply to write."))
            return

        try:
            result = apply_plan(plan)
        except ImportFailed as exc:
            raise CommandError(f"Nothing was written. Stopped at {exc}") from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        clashing = result["sections_with_instructor_clashes"]
        if clashing:
            self.stdout.write(
                self.style.WARNING(
                    f"{clashing} section(s) share a teacher with another section. "
                    "A manual timetable is never refused for a clash."
                )
            )
        if result["critical_conflicts"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{result['critical_conflicts']} serious conflict(s) - a double-booked room "
                    "or two meetings of one course at once. Open the scenario and run Check."
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Written: {result['sections_created']} section(s), "
                f"{result['placements']} placement(s)."
            )
        )
