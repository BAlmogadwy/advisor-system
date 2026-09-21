from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def revoke_unverifiable_legacy_credentials(apps, schema_editor) -> None:  # noqa: ARG001
    """Fail closed for credentials created before an exact account was stored.

    A legacy row contains only ``student_id``. Inferring its owner from today's
    unique ``UserScope`` can silently bind an old Telegram credential to a
    replacement Django account after the original account was deleted. There is
    no historical account primary key with which to prove the match, so every
    active legacy link must be re-approved and every pending legacy approval must
    be burned. Revoked history rows remain for audit purposes.
    """

    TelegramLink = apps.get_model("telegram_gateway", "TelegramLink")
    TelegramLinkToken = apps.get_model("telegram_gateway", "TelegramLinkToken")
    now = timezone.now()

    TelegramLink.objects.filter(status="ACTIVE").update(
        status="REVOKED",
        revoked_at=now,
        current_conversation_id=None,
    )
    TelegramLinkToken.objects.filter(
        approved_student_id__isnull=False,
        consumed_at__isnull=True,
    ).update(consumed_at=now)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("telegram_gateway", "0002_durable_advisor_jobs"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramlink",
            name="university_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="telegramlinktoken",
            name="approved_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            revoke_unverifiable_legacy_credentials,
            migrations.RunPython.noop,
        ),
    ]
