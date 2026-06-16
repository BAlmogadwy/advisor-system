"""Reconcile orphaned async planner jobs.

PlannerJobs run in-process and are not durable across restarts, so a server
stop/reap can leave a job stranded in ``running`` forever. This command marks
such stale jobs (running past the stale window, or queued but never dispatched)
as ``failed``. Safe to run repeatedly; ideal as a startup / pre-deploy step.

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
            help="Stale window in minutes (default: TIMETABLE_PLANNER_JOB_STALE_MINUTES).",
        )

    def handle(self, *args, **options) -> None:
        count = reconcile_stale_planner_jobs(stale_minutes=options.get("minutes"))
        self.stdout.write(f"Reconciled {count} stale planner job(s) -> failed.")
