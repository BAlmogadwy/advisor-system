"""Reconcile orphaned async planner jobs.

Mark running PostgreSQL jobs as failed once their worker lease is released.
Queued jobs and running jobs on SQLite are eligible after the stale window.

Usage::

    python manage.py reconcile_planner_jobs
    python manage.py reconcile_planner_jobs --minutes 30
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.services.planner_job_runner import reconcile_stale_planner_jobs


class Command(BaseCommand):
    help = "Mark orphaned (stale) async planner jobs as failed."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--minutes",
            type=int,
            default=None,
            help="Grace period for queued jobs and SQLite running jobs "
            "(default: TIMETABLE_PLANNER_JOB_STALE_MINUTES).",
        )

    def handle(self, *args, **options) -> None:
        count = reconcile_stale_planner_jobs(stale_minutes=options.get("minutes"))
        self.stdout.write(f"Reconciled {count} stale planner job(s) -> failed.")
