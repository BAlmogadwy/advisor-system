import json
from argparse import ArgumentParser
from io import StringIO
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Dump an online seed fixture with student names redacted"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "-o",
            "--output",
            default="tmp/seed_online_no_student_names.json",
            help="Fixture path to write. Defaults to tmp/seed_online_no_student_names.json.",
        )
        parser.add_argument(
            "--student-name-placeholder",
            default="",
            help="Replacement value for core.Student.name. Defaults to an empty string.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        output_path = Path(opts["output"])
        placeholder = opts["student_name_placeholder"]

        raw_fixture = StringIO()
        call_command(
            "dumpdata",
            format="json",
            exclude=[
                "admin.LogEntry",
                "auth.Permission",
                "contenttypes",
                "sessions.Session",
            ],
            indent=2,
            use_natural_foreign_keys=True,
            stdout=raw_fixture,
        )

        try:
            records = json.loads(raw_fixture.getvalue())
        except json.JSONDecodeError as exc:
            raise CommandError(f"Django produced invalid JSON fixture: {exc}") from exc

        redacted = 0
        for record in records:
            if record.get("model") == "core.student":
                fields = record.get("fields") or {}
                if "name" in fields:
                    fields["name"] = placeholder
                    redacted += 1

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(records)} records to {output_path} "
                f"with {redacted} student name(s) redacted."
            )
        )
