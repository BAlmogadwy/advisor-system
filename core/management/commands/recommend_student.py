from typing import Any, cast

from django.core.management.base import BaseCommand, CommandParser

from core.services.recommender import recommend_next_courses


class Command(BaseCommand):
    help = "Recommend next courses for a single student"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("student_id", type=int)
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--semester", type=int, required=True)

    def handle(self, *args: object, **options: Any) -> None:
        student_id = cast(int, options["student_id"])
        year = cast(int, options["year"])
        semester = cast(int, options["semester"])

        recommendations = recommend_next_courses(student_id, year, semester)
        self.stdout.write(str(recommendations))
