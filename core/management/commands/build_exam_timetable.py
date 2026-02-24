import json
from typing import Any, cast

from django.core.management.base import BaseCommand, CommandParser

from core.services.exam_timetable import build_exam_timetable


class Command(BaseCommand):
    help = "Build a conflict-free exam timetable from current studying enrolments"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--label", type=str, required=True, help="Run label")
        parser.add_argument(
            "--days",
            type=str,
            required=True,
            help="Comma-separated day labels (e.g. 'Sun,Mon,Tue,Wed,Thu')",
        )
        parser.add_argument(
            "--periods",
            type=str,
            required=True,
            help="Comma-separated period labels (e.g. '08:00-10:00,10:30-12:30,13:00-15:00')",
        )
        parser.add_argument(
            "--max-per-day",
            type=int,
            default=2,
            help="Max exams per student per day soft limit (default 2)",
        )

    def handle(self, *args: object, **options: Any) -> None:
        label = cast(str, options["label"])
        days = [d.strip() for d in cast(str, options["days"]).split(",") if d.strip()]
        periods = [p.strip() for p in cast(str, options["periods"]).split(",") if p.strip()]
        max_per_day = int(options.get("max_per_day", 2))

        self.stdout.write(f"Building exam timetable: label={label}")
        self.stdout.write(f"  Days: {days}")
        self.stdout.write(f"  Periods: {periods}")
        self.stdout.write(f"  Max exams/day: {max_per_day}")

        result = build_exam_timetable(label, days, periods, max_per_day=max_per_day)

        # Handle feasibility error
        if result.get("feasibility_error"):
            self.stderr.write(self.style.ERROR("\nInfeasible schedule!"))
            self.stderr.write(
                f"  {len(result['violations'])} bucket(s) have more courses "
                f"than available days ({days}):"
            )
            for v in result["violations"]:
                self.stderr.write(
                    f"  - {v['program']}/Term{v['programme_term']}: "
                    f"{v['bucket_size']} courses > {v['num_days']} days "
                    f"({', '.join(v['courses'])})"
                )
            self.stderr.write("Add more exam days or reduce bucket sizes.")
            return

        self.stdout.write(f"\nRun ID: {result.get('run_id')}")
        self.stdout.write(f"Courses: {result['courses_count']}")
        self.stdout.write(f"Students: {result['students_count']}")
        self.stdout.write(f"Conflict edges: {result['conflicts_count']}")
        self.stdout.write(f"Buckets: {result.get('bucket_count', 0)}")
        self.stdout.write(f"Slots used: {result['qa']['slots_used']}")
        self.stdout.write(f"Max exams/day/student: {result['qa']['max_exams_per_day_per_student']}")
        self.stdout.write(f"Same-slot conflicts: {result['qa']['conflict_count']}")
        self.stdout.write(f"Bucket day violations: {result['qa']['bucket_day_violations_count']}")

        self.stdout.write("\nFull QA Report:")
        self.stdout.write(json.dumps(result["qa"], indent=2, ensure_ascii=False))
