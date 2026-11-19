"""Reconcile orphaned async planner jobs.

Mark PostgreSQL rebuilds claimed with a protected worker lease as failed once
that lease is released. Other active jobs are eligible after the stale window.

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
            help="Grace period for jobs without a protected PostgreSQL rebuild lease "
            "(default: TIMETABLE_PLANNER_JOB_STALE_MINUTES).",
        )

    def handle(self, *args, **options) -> None:
        count = reconcile_stale_planner_jobs(stale_minutes=options.get("minutes"))
        self.stdout.write(f"Reconciled {count} stale planner job(s) -> failed.")
