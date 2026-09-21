"""Send a data-free test email through Twilio SendGrid.

python manage.py send_test_email --to you@example.com
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.sendgrid_email import SendGridDeliveryError, send_transactional_email


class Command(BaseCommand):
    help = "Send a data-free test email to confirm Twilio SendGrid delivery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            required=True,
            help="Recipient address. The command never prints it.",
        )

    def handle(self, *args, **options):
        to = str(options["to"] or "").strip()
        enabled = bool(getattr(settings, "STUDENT_OTP_SENDGRID_ENABLED", False))
        self.stdout.write(
            "provider=sendgrid "
            f"enabled={'true' if enabled else 'false'} "
            f"api_key={'set' if settings.SENDGRID_API_KEY else 'missing'} "
            f"from_email={'set' if settings.SENDGRID_FROM_EMAIL else 'missing'} "
            f"timeout_seconds={settings.SENDGRID_TIMEOUT_SECONDS}"
        )
        if not enabled:
            raise CommandError("SendGrid student email is disabled.")
        try:
            message_id = send_transactional_email(
                to,
                "Advisor system — SendGrid delivery test",
                "This is a delivery test. It contains no student data or verification code.",
            )
        except SendGridDeliveryError:
            raise CommandError("SendGrid did not accept the test message.") from None
        self.stdout.write(
            self.style.SUCCESS(
                f"SendGrid accepted the test message; message_id={message_id or 'not-returned'}."
            )
        )
