"""Drop dead link tokens and old terminal update jobs.

Neither table is read once its row has done its job: a consumed or expired token
can never link again, and a receipt older than any plausible Telegram retry window
can never suppress a duplicate. Left alone they grow without bound — the receipts
in particular, at one row per message the bot ever receives.

Deliberately a management command rather than a call from the request path.
Retention is a SCHEDULE, not a side effect of traffic: the same reasoning
`purge_planner_drafts` is scheduled under in `render.yaml`, and for the same
reason — how long a student's data survives must not depend on how busy the bot is.

Dry by default. A command that deletes on a bare invocation is one that deletes
when somebody runs it to see what it does.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand

from telegram_gateway.linking import purge_expired, purgeable_terminal_receipts

#: A week is far longer than the longest token TTL and far longer than Telegram
#: retries a delivery, so nothing inside the window is still doing work.
DEFAULT_DAYS = 7


class Command(BaseCommand):
    help = "Delete dead Telegram link tokens and old terminal jobs. Dry-run unless --apply."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_DAYS,
            help=f"Delete rows older than this many days (default {DEFAULT_DAYS}).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it, only report what would go.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        days = max(1, int(options["days"]))
        older_than = timedelta(days=days)

        if not options["apply"]:
            from django.db.models import Q
            from django.utils import timezone

            from telegram_gateway.models import TelegramLinkToken

            cutoff = timezone.now() - older_than
            tokens = (
                TelegramLinkToken.objects.filter(created_at__lt=cutoff)
                .filter(Q(consumed_at__isnull=False) | Q(expires_at__lte=timezone.now()))
                .count()
            )
            receipts = purgeable_terminal_receipts(cutoff).count()
            self.stdout.write(
                f"DRY RUN — would delete {tokens} link token(s) and "
                f"{receipts} update receipt(s) older than {days} day(s)."
            )
            self.stdout.write("Re-run with --apply to delete.")
            return

        tokens, receipts = purge_expired(older_than)
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {tokens} link token(s) and {receipts} update receipt(s) "
                f"older than {days} day(s)."
            )
        )
