from collections import Counter
from typing import Any, cast

from django.core.management.base import BaseCommand, CommandParser

from core.models import Student
from core.services.recommender import recommend_next_courses


class Command(BaseCommand):
    help = "Run recommender for all students (or filtered by program/section)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--semester", type=int, required=True)
        parser.add_argument("--program", type=str, default=None)
        parser.add_argument("--section", type=str, default=None)

    def handle(self, *args: object, **options: Any) -> None:
        year = cast(int, options["year"])
        semester = cast(int, options["semester"])
        program = cast(str | None, options["program"])
        section = cast(str | None, options["section"])

        qs = Student.objects.all()
        if program:
            qs = qs.filter(program=program)
        if section:
            qs = qs.filter(section=section)

        student_ids = list(qs.values_list("student_id", flat=True))
        aggregate: Counter[str] = Counter()

        self.stdout.write(f"Found {len(student_ids)} students")
        for student_id in student_ids:
            recs = recommend_next_courses(student_id, year, semester)
            aggregate.update(recs)
            self.stdout.write(f"{student_id}: {recs}")

        self.stdout.write("\nTop recommended courses:")
        for code, count in aggregate.most_common(20):
            self.stdout.write(f"{code}: {count}")
