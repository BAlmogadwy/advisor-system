"""Delete expired student-login challenges after a short audit window.

An OTP is useful only for minutes. Keeping its salted code hash, request-address
fingerprint, and provider receipt indefinitely creates privacy cost without
student value. The scheduled retention job keeps expired rows for 24 hours so a
same-day delivery incident can still be diagnosed, then removes them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import StudentLoginOTP

OTP_RETENTION = timedelta(hours=24)


class Command(BaseCommand):
    help = "Delete student-login OTP rows that have been expired for at least 24 hours."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it this command only reports the count.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        cutoff = timezone.now() - OTP_RETENTION
        doomed = StudentLoginOTP.objects.filter(expires_at__lte=cutoff)
        count = doomed.count()

        if not options["apply"]:
            self.stdout.write(
                f"Would delete {count} student-login OTP row(s) expired at least 24 hours ago. "
                "Re-run with --apply to do it."
            )
            return

        doomed.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} student-login OTP row(s) expired at least 24 hours ago."
            )
        )
