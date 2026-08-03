"""Delete planner drafts that expired, and everything they were holding.

A draft is a hand-off, not a saved plan: it lives 24 hours and then describes a
catalogue that may not exist. The model documented that and the table carried an
index on `expires_at` built for exactly this sweep — and nothing ever ran it, so the
index supported a query no code issued and rows accumulated forever.

**Two retentions, because two kinds of row.** An expired draft that was never
generated is a course list the student abandoned: no product value, no audit value,
and it goes as soon as it expires. An expired draft that WAS generated holds the
alternatives and, in `generated_inputs`, the baseline the solver saw — which is the
student's registered timetable with instructor names, room numbers and per-section
enrolment counts. That is worth a short window, because it is the row that answers
"what was I shown?" when a student asks a day later. It is not worth keeping
indefinitely, and it is the more sensitive of the two.

Age is the ONLY criterion. Not whether the originating conversation still exists —
`source_message` is a provenance pointer with `on_delete=SET_NULL`, so a student
tidying their chat history must not lose a live draft — and not whether the student
row still exists, since `student_id` is a bare integer across the adviser so a
roster re-import cannot cascade drafts away.

Runs daily, as a scheduled job, not only at deploy: a release chain fires when
someone ships, and retention that depends on shipping is not retention.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from core.models import PlannerDraft

#: An abandoned course list. Nothing here is worth explaining later.
UNGENERATED_GRACE = timedelta(days=1)

#: A generated one is kept long enough to answer "what was I shown yesterday?",
#: and no longer — it carries the most sensitive payload in the table.
GENERATED_GRACE = timedelta(days=7)


class Command(BaseCommand):
    help = "Delete expired planner drafts, ungenerated ones sooner than generated ones."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--ungenerated-days",
            type=int,
            default=UNGENERATED_GRACE.days,
            help=f"Days past expiry to keep never-generated drafts "
            f"(default {UNGENERATED_GRACE.days}).",
        )
        parser.add_argument(
            "--generated-days",
            type=int,
            default=GENERATED_GRACE.days,
            help=f"Days past expiry to keep generated drafts (default {GENERATED_GRACE.days}).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it this only reports, as every "
            "destructive command in this project does.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        now = timezone.now()
        ungenerated_cutoff = now - timedelta(days=max(0, int(options["ungenerated_days"])))
        generated_cutoff = now - timedelta(days=max(0, int(options["generated_days"])))

        # "Generated" is the same predicate the application uses, expressed in SQL:
        # a result belongs to the version that produced it.
        was_generated = Q(generated_version__gt=0) & Q(generated_version=F("version"))

        doomed = PlannerDraft.objects.filter(
            (Q(expires_at__lt=ungenerated_cutoff) & ~was_generated)
            | (Q(expires_at__lt=generated_cutoff) & was_generated)
        )
        counts = {
            "ungenerated": doomed.exclude(was_generated).count(),
            "generated": doomed.filter(was_generated).count(),
        }
        total = counts["ungenerated"] + counts["generated"]

        if not options["apply"]:
            self.stdout.write(
                f"Would delete {total} planner draft(s): "
                f"{counts['ungenerated']} never generated (expired before "
                f"{ungenerated_cutoff.date()}), "
                f"{counts['generated']} generated (expired before {generated_cutoff.date()}). "
                "Re-run with --apply to do it."
            )
            return

        deleted, _ = doomed.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} expired planner draft row(s): "
                f"{counts['ungenerated']} never generated, {counts['generated']} generated."
            )
        )
