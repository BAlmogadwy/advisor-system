"""Send a test email to verify SMTP (Gmail app-password) is configured.

    python manage.py send_test_email --to you@example.com

With EMAIL_HOST_PASSWORD unset it uses the console backend (prints to the log);
once the app-password is in .env it sends real mail via smtp.gmail.com.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send a test email to confirm the SMTP configuration works."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            default=getattr(settings, "EMAIL_HOST_USER", "") or "alfalak51@gmail.com",
            help="Recipient address (defaults to EMAIL_HOST_USER).",
        )

    def handle(self, *args, **options):
        to = options["to"]
        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.stdout.write(
            f"backend={backend}  host={settings.EMAIL_HOST}:{settings.EMAIL_PORT}  "
            f"tls={settings.EMAIL_USE_TLS}  user={settings.EMAIL_HOST_USER or '(unset)'}  "
            f"password={'set' if settings.EMAIL_HOST_PASSWORD else 'MISSING'}"
        )
        if backend == "smtp" and not settings.EMAIL_HOST_PASSWORD:
            self.stderr.write(
                self.style.WARNING(
                    "SMTP backend but no password — set EMAIL_HOST_PASSWORD in .env."
                )
            )
        try:
            sent = send_mail(
                subject="Advisor system — SMTP test",
                message="If you can read this, the OTP email pipeline works.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[to],
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 — surface the real SMTP error to the operator
            self.stderr.write(self.style.ERROR(f"send failed: {type(exc).__name__}: {exc}"))
            self.stderr.write(
                "Common cause: wrong/blank Gmail app-password, or 2-Step Verification off."
            )
            raise SystemExit(1) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"send_mail returned {sent} — check {to} (or the console log above)."
            )
        )
