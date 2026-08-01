"""Detect (and optionally repair) incoherent shared multi-board sections.

A section shown on more than one board is one logical class, but placements are
per board and nothing keeps them in agreement — so the same class can be
scheduled at a different day/time, or in a different room, on each board. Both
shapes yield phantom ``TermSectionMeeting`` rows.

Read-only by default. ``--apply`` performs the transactional repair.

Usage::

    python manage.py shared_sections 643            # report only
    python manage.py shared_sections 643 --apply    # make every board agree
    python manage.py shared_sections 643 --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.models import TimetableScenario
from core.services.timetable_shared_section import (
    analyze_shared_sections,
    canonicalise_shared_sections,
)


class Command(BaseCommand):
    help = "Report or repair shared multi-board sections that disagree across boards."

    def add_arguments(self, parser) -> None:
        parser.add_argument("scenario_id", type=int)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="write the repair (default is a read-only report / dry run)",
        )
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options) -> None:
        scenario_id = options["scenario_id"]
        try:
            TimetableScenario.objects.get(id=scenario_id)
        except TimetableScenario.DoesNotExist as exc:
            raise CommandError(f"Scenario {scenario_id} not found") from exc

        report = (
            canonicalise_shared_sections(scenario_id, apply=True)
            if options["apply"]
            else analyze_shared_sections(scenario_id)
        )
        data = report.as_dict()

        if options["format"] == "json":
            self.stdout.write(json.dumps(data, indent=2, default=str))
            return

        w = self.stdout.write
        w(f"Scenario {scenario_id} — shared multi-board sections")
        w(f"  shared across boards : {data['shared_count']}")
        w(
            f"  incoherent           : {data['divergent_count']} "
            f"({data['schedule_divergent_count']} different time, "
            f"{data['room_divergent_count']} same time / different room)"
        )
        for section in data["shared_sections"]:
            if not section["divergent"]:
                continue
            kind = "TIME" if section["schedule_divergent"] else "ROOM"
            w(f"\n  [{kind}] {section['course']}  boards {section['board_ids']}")
            for board_id, slots in section["schedule_by_board"].items():
                marker = "*" if board_id == section["canonical_board_id"] else " "
                shown = ", ".join(
                    f"{s['day']} {s['start']}-{s['end']}" + (f" @{s['room']}" if s["room"] else "")
                    for s in slots
                )
                w(f"    {marker} board {board_id}: {shown}")
        if data["applied"]:
            w(
                f"\n  APPLIED — {data['sections_canonicalised']} sections canonicalised, "
                f"{data['placements_rewritten']} placements rewritten, "
                f"{data['sections_skipped_locked']} skipped for locks, "
                f"{data['remaining_divergent_count']} still incoherent."
            )
        for note in data["notes"]:
            w(f"\n  {note}")
        if not data["applied"] and data["divergent_count"]:
            w("\n  (read-only — re-run with --apply to repair)")
