"""Drop rate-limit rows nobody is counting against any more.

Housekeeping, not a hot path. The previous in-process throttle swept on every
hundredth request and used whichever window the CALLING endpoint happened to
configure, so any window longer than the shortest one anywhere was fictional — a
limit that read as configured and quietly reset itself in production.

Run it from the release chain, where the project already runs its other
once-per-deploy maintenance. The table is small by construction (one row per
budget per student who has ever used the adviser), so this is tidiness rather
than capacity.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from core.services.rate_limit import purge_expired


class Command(BaseCommand):
    help = "Delete rate-limit buckets whose window closed long ago."

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = purge_expired()
        self.stdout.write(self.style.SUCCESS(f"Removed {deleted} stale rate-limit buckets."))
