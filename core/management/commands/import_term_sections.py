from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.term_sections import import_term_sections_from_csv


class Command(BaseCommand):
    help = "Import cleaned term course sections CSV into advisor DB table term_course_sections"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--csv", required=True, help="Path to cleaned CSV")
        parser.add_argument("--year", required=True, help="Academic year label (e.g. 1447)")
        parser.add_argument("--term", required=True, help="Term label (e.g. 1 or Fall)")
        parser.add_argument(
            "--department",
            action="store_true",
            help="Tag imported rows as source_tag=department (default: other)",
        )
        parser.add_argument(
            "--truncate",
            action="store_true",
            help="Delete existing rows for this year+term before import",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            summary = import_term_sections_from_csv(
                csv_path=options["csv"],
                academic_year=str(options["year"]),
                term=str(options["term"]),
                source_tag="department" if bool(options["department"]) else "other",
                truncate_existing_term=bool(options["truncate"]),
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Imported: {summary}"))
