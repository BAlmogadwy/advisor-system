"""Compare room-assignment outcomes across different capacity buffers.

Read-only dry-run: never persists. Reports, per board, how many rooms would
be assigned vs left UNASSIGNED at each requested buffer value, plus how
many placements are rejected *because of* the buffer (a raw-capacity room
existed but was too small once the buffer was applied).

Example::

    python manage.py timetable_buffer_compare --board-id 42
    python manage.py timetable_buffer_compare --scenario-id 7 --buffers 1.0 1.05 1.1
"""

from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import DeliveryBoard
from core.services.timetable_rooming import simulate_buffer_impact


class Command(BaseCommand):
    help = (
        "Read-only comparison of capacity-buffer impact on room assignment. Never persists changes."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--board-id", type=int, help="Single DeliveryBoard id")
        group.add_argument(
            "--scenario-id",
            type=int,
            help="All DeliveryBoards for a scenario",
        )
        parser.add_argument(
            "--buffers",
            nargs="+",
            type=float,
            default=None,
            help="Buffer values to compare (defaults to 1.0 and current setting)",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        current = float(getattr(settings, "TIMETABLE_CAPACITY_BUFFER", 1.1))
        buffers = opts["buffers"] or sorted({1.0, current})

        if opts["board_id"] is not None:
            board_ids = [opts["board_id"]]
        else:
            board_ids = list(
                DeliveryBoard.objects.filter(scenario_id=opts["scenario_id"])
                .order_by("display_order", "id")
                .values_list("id", flat=True)
            )
            if not board_ids:
                raise CommandError(f"No boards found for scenario {opts['scenario_id']}")

        self.stdout.write(
            self.style.NOTICE(f"Buffer comparison — current setting {current}, testing {buffers}")
        )

        for board_id in board_ids:
            result = simulate_buffer_impact(board_id, buffers)
            if not result["results"]:
                self.stdout.write(f"board {board_id}: no data")
                continue
            self.stdout.write(f"\nboard {board_id} ({', '.join(result['programmes'])})")
            for row in result["results"]:
                self.stdout.write(
                    "  buffer={buffer:<4}  assigned={assigned:<4}  "
                    "unassigned={unassigned:<4}  "
                    "rejected_by_buffer_vs_1.0={rej}".format(
                        buffer=row["buffer"],
                        assigned=row["assigned"],
                        unassigned=row["unassigned"],
                        rej=row["rejected_by_buffer_vs_1_0"],
                    )
                )
