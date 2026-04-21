"""PR7 commit 7 — planner-job audit report.

Registrar-facing CLI: prints a compact table of recent ``PlannerJob``
rows with status, mode, last_stage_seen, timing, and whether a cancel
was requested. Read-only — never re-runs the planner, never mutates
rows.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.models import PlannerJob


class Command(BaseCommand):
    help = "Report recent planner jobs with last_stage_seen + status."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Max rows to list (most recent first). Default 20.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        limit = int(options.get("limit") or 20)
        rows = PlannerJob.objects.order_by("-submitted_at")[:limit]
        header = (
            f"{'job_id':36}  {'status':10}  {'mode':18}  "
            f"{'last_stage_seen':18}  {'cancel':6}  submitted_at"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        if not rows:
            self.stdout.write("(no planner jobs recorded)")
            return
        for job in rows:
            self.stdout.write(
                f"{str(job.id):36}  {job.status:10}  {job.mode:18}  "
                f"{(job.last_stage_seen or '-'):18}  "
                f"{'yes' if job.cancel_requested else 'no':6}  "
                f"{job.submitted_at.isoformat(timespec='seconds') if job.submitted_at else '-'}"
            )
