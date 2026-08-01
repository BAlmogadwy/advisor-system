"""Scenario-wide exact room assignment + shortfall decomposition — read-only.

Coordinates the shared room pool across every board (the per-board greedy cannot)
and reports whether all physical meetings can be roomed with zero capacity
shortfall. When they cannot, it separates the three causes the greedy conflates
into one silent ``UNASSIGNED``. Writes nothing.

Usage::

    python manage.py exact_rooming 643
    python manage.py exact_rooming 643 --seconds 60
    python manage.py exact_rooming 643 --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.services.timetable_exact_rooming import ExactRoomingError, plan_exact_rooming


class Command(BaseCommand):
    help = "Exact scenario-wide room assignment + shortfall decomposition (read-only)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("scenario_id", type=int)
        parser.add_argument("--seconds", type=float, default=30.0, help="solver budget")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options) -> None:
        seconds = max(5.0, min(120.0, float(options["seconds"])))
        try:
            report = plan_exact_rooming(options["scenario_id"], time_limit_seconds=seconds)
        except ExactRoomingError as exc:
            raise CommandError(str(exc)) from exc

        data = report.to_dict()
        if options["format"] == "json":
            self.stdout.write(json.dumps(data, indent=2, default=str))
            return

        w = self.stdout.write
        w(f"Scenario {data['scenario_id']} — exact rooming")
        w(f"  status            : {data['status']} (proven optimal: {data['proven_optimal']})")
        w(f"  feasible          : {data['feasible']}")
        w(f"  physical meetings : {data['physical_meetings']}  roomed: {data['roomed_meetings']}")
        shortfall = data["shortfall"]
        w(
            f"  capacity shortfall: {shortfall['total']} seat-units "
            f"({shortfall['unavoidable_inventory']} unavoidable from inventory, "
            f"{shortfall['congestion_reducible']} congestion-reducible)"
        )
        w(f"  rooms changed     : {data['room_changes']}   solve: {data['wall_time_seconds']}s")
        if data["no_compatible_room"]:
            w(f"\n  NO COMPATIBLE ROOM ({len(data['no_compatible_room'])}) — inventory gap:")
            for row in data["no_compatible_room"][:10]:
                w(
                    f"    {row['course']:<10} {row['day']:<4} needs {row['required_type']:<7} "
                    f"gender={row['required_gender'] or '-':<2} progs={','.join(row['required_programmes'])}"
                )
        if data["unroomable_meetings"]:
            w(
                f"\n  UNROOMABLE AT FIXED TIMES ({len(data['unroomable_meetings'])}) — "
                "room-count contention; these need a TIME change, not a bigger room:"
            )
            for row in data["unroomable_meetings"][:10]:
                w(
                    f"    {row['course']:<10} {row['day']:<4} "
                    f"{row['compatible_rooms']} compatible rooms, demand {row['buffered_demand']}"
                )
        if data["capacity_short_meetings"]:
            w(f"\n  ROOM TOO SMALL ({len(data['capacity_short_meetings'])}):")
            for row in data["capacity_short_meetings"][:10]:
                w(
                    f"    {row['course']:<10} {row['day']:<4} in {row['assigned_room']} "
                    f"(cap {row['room_capacity']} < {row['buffered_demand']}, short {row['deficit']})"
                )
        for note in data["notes"]:
            w(f"\n  {note}")
