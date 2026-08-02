"""Delete planner drafts that expired, and everything they were holding.

A draft is a hand-off, not a saved plan: it lives 24 hours and then describes a
catalogue that may not exist. The model documented that and the table carried an
index on `expires_at` built for exactly this sweep — and nothing ever ran it, so
the index supported a query no code issued and rows accumulated forever.

What accumulates matters more than how many. `generated_inputs` holds the baseline
the solver saw, which is the student's registered timetable with instructor names,
room numbers and per-section enrolment counts. That is institutional data kept
alive by a screen the student closed a day ago.

Run it from the release chain beside `purge_rate_limit_buckets`, which solves the
same problem for the same reason.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import PlannerDraft

#: Kept past expiry so a student who lost a tab can be told what happened rather
#: than shown a 404, and so a support question the morning after has something to
#: look at. Well short of the point where the baseline inside is worth keeping.
GRACE = timedelta(days=7)


class Command(BaseCommand):
    help = "Delete planner drafts that expired more than the grace period ago."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=GRACE.days,
            help=f"Delete drafts that expired more than N days ago (default {GRACE.days}).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it this only reports, as every "
            "destructive command in this project does.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        days = max(0, int(options["days"]))
        cutoff = timezone.now() - timedelta(days=days)
        doomed = PlannerDraft.objects.filter(expires_at__lt=cutoff)
        count = doomed.count()

        if not options["apply"]:
            self.stdout.write(
                f"Would delete {count} planner draft(s) that expired before "
                f"{cutoff.isoformat()}. Re-run with --apply to do it."
            )
            return

        deleted, _ = doomed.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired planner draft row(s)."))
