"""PR2 commit 5 — rerun rooming with the oracle on and print typed failures.

Read-only by design: wraps the whole run in a transaction savepoint that
is rolled back on exit, so no ``SectionPlacement`` rows change. The
oracle flag is forced on locally via ``override_settings``, so the
command produces the refined ``room_failures`` payload even when the
scenario-wide default is still ``False``.

Primary contract — ``--scenario-id``:

    python manage.py report_room_failures --scenario-id 7

Single-board override:

    python manage.py report_room_failures --board-id 42

For each board the command prints:

1. a one-line header with board id + programme + gender
2. the ``room_failure_breakdown`` dict (observed codes only)
3. each individual failure (reason + course / section / day / slot)

Zero-failure boards print the header plus a single ``no failures`` line.
The command is intended for planner-pack reproducibility: re-running on
the same scenario on the same day should print identical output.
"""

from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings

from core.models import DeliveryBoard
from core.services.timetable_rooming import assign_rooms_to_board


class Command(BaseCommand):
    help = (
        "Re-run room assignment with the PR2 oracle flag forced on and "
        "print typed ``RoomFailureReason`` records per board. Never "
        "persists — all DB writes are rolled back on exit."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--scenario-id",
            type=int,
            help="Run rooming for every DeliveryBoard in the scenario",
        )
        group.add_argument(
            "--board-id",
            type=int,
            help="Run rooming for a single DeliveryBoard",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        scenario_id = opts.get("scenario_id")
        board_id = opts.get("board_id")

        if board_id is not None:
            board_ids = [board_id]
        else:
            board_ids = list(
                DeliveryBoard.objects.filter(scenario_id=scenario_id)
                .order_by("display_order", "id")
                .values_list("id", flat=True)
            )
            if not board_ids:
                raise CommandError(f"No boards found for scenario {scenario_id}")

        self.stdout.write(
            self.style.NOTICE(
                f"report_room_failures — scenario={scenario_id} board={board_id} "
                f"({len(board_ids)} board(s))"
            )
        )

        # Wrap the rerun in a savepoint so ``assign_rooms_to_board`` writes
        # to SectionPlacement.room are rolled back at the end — the command
        # must be safely idempotent and read-only observable.
        with transaction.atomic():
            sid = transaction.savepoint()
            try:
                with override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=True):
                    for bid in board_ids:
                        self._report_board(bid)
            finally:
                transaction.savepoint_rollback(sid)

    def _report_board(self, board_id: int) -> None:
        try:
            board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        except DeliveryBoard.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"board {board_id}: not found"))
            return

        programmes = board.program or ""
        self.stdout.write(
            f"\nboard {board_id} — programmes='{programmes}' scenario={board.scenario_id}"
        )

        result = assign_rooms_to_board(board_id)
        failures = result.get("room_failures", [])
        breakdown = result.get("room_failure_breakdown", {})

        self.stdout.write(
            f"  assigned={result.get('assigned', 0)} "
            f"unassigned={result.get('unassigned', 0)} "
            f"unplaced_count={result.get('unplaced_count', 0)} "
            f"buffer_only_rejects={result.get('buffer_only_rejects', 0)}"
        )

        if not failures:
            self.stdout.write("  no failures")
            return

        self.stdout.write(f"  breakdown: {breakdown}")
        for record in failures:
            self.stdout.write(
                "    {reason:<24} {course:<14} {section:<6} {day:<4} {start}-{end}".format(
                    reason=record.get("reason", "?"),
                    course=record.get("course_code", "?"),
                    section=record.get("section_code", "?"),
                    day=record.get("day", "") or "-",
                    start=record.get("start_time", "") or "-",
                    end=record.get("end_time", "") or "-",
                )
            )
