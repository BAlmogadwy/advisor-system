from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.term_sections import import_term_sections_from_csv


class Command(BaseCommand):
    help = "Merge a cleaned CSV into the current section snapshot"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--csv", required=True, help="Path to cleaned CSV")
        parser.add_argument(
            "--year",
            default="",
            help="Deprecated compatibility option; current sections are a single snapshot",
        )
        parser.add_argument(
            "--term",
            default="",
            help="Deprecated compatibility option; current sections are a single snapshot",
        )
        parser.add_argument(
            "--program",
            action="append",
            default=[],
            dest="default_programs",
            metavar="CODE",
            help=(
                "Default programme for sections without a CSV programme value; "
                "repeat for shared sections (for example --program AI --program DS)"
            ),
        )
        parser.add_argument(
            "--department",
            action="store_true",
            help="Tag imported rows as source_tag=department (default: other)",
        )
        parser.add_argument(
            "--truncate",
            action="store_true",
            help=(
                "Disabled safety guard: use DB Admin > Clear Current Section Snapshot "
                "before running a merge import"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            summary = import_term_sections_from_csv(
                csv_path=options["csv"],
                academic_year=str(options["year"]),
                term=str(options["term"]),
                source_tag="department" if bool(options["department"]) else "other",
                truncate_existing_term=bool(options["truncate"]),
                default_programs=list(options["default_programs"]),
                backup_before_import=True,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Imported: {summary}"))
