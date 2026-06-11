from __future__ import annotations

import socket

from django.core.management.base import BaseCommand

from core.services.timetable_repair_jobs import run_repair_worker_loop


class Command(BaseCommand):
    help = "Run queued timetable repair analysis/simulation jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
        parser.add_argument(
            "--sleep",
            type=float,
            default=2.0,
            help="Idle sleep seconds between queue polls.",
        )
        parser.add_argument(
            "--worker-id",
            default="",
            help="Optional stable worker identifier for repair job locks.",
        )
        parser.add_argument(
            "--no-recover-stale",
            action="store_true",
            help="Do not recover stale running repair jobs before polling the queue.",
        )
        parser.add_argument(
            "--stale-seconds",
            type=int,
            default=1800,
            help="Seconds after which a running job heartbeat is considered stale.",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=3,
            help="Maximum worker attempts before stale jobs are failed instead of requeued.",
        )

    def handle(self, *args, **options):
        worker_id = options.get("worker_id") or f"repair-worker@{socket.gethostname()}"
        executed = run_repair_worker_loop(
            worker_id=worker_id,
            once=bool(options.get("once")),
            idle_sleep_seconds=float(options.get("sleep") or 2.0),
            recover_stale=not bool(options.get("no_recover_stale")),
            stale_after_seconds=int(options.get("stale_seconds") or 1800),
            max_attempts=int(options.get("max_attempts") or 3),
        )
        self.stdout.write(self.style.SUCCESS(f"Repair worker executed {executed} job(s)."))
