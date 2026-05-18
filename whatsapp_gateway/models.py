from django.conf import settings
from django.db import models
from django.utils import timezone


class WhatsAppUserLink(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_REVOKED = "revoked"
    STATUS_LOCKED = "locked"

    wa_id = models.TextField(unique=True)
    phone_number = models.TextField(blank=True, default="")
    role = models.TextField()
    status = models.TextField(default=STATUS_ACTIVE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    student = models.ForeignKey(
        "core.Student",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    advisor_id = models.TextField(blank=True, default="")
    departments = models.TextField(blank=True, default="")
    verified_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "whatsapp_user_links"
        indexes = [
            models.Index(fields=["wa_id"], name="idx_wal_wa_id"),
            models.Index(fields=["role", "status"], name="idx_wal_role_status"),
            models.Index(fields=["student"], name="idx_wal_student"),
            models.Index(fields=["advisor_id"], name="idx_wal_advisor"),
        ]

    def __str__(self) -> str:
        return f"WhatsAppUserLink({self.wa_id}/{self.role}/{self.status})"


class WhatsAppOtpChallenge(models.Model):
    STATUS_PENDING = "pending"
    STATUS_VERIFIED = "verified"
    STATUS_EXPIRED = "expired"
    STATUS_LOCKED = "locked"

    wa_id = models.TextField()
    phone_number = models.TextField(blank=True, default="")
    university_id = models.TextField()
    resolved_role = models.TextField()
    resolved_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    resolved_student = models.ForeignKey(
        "core.Student",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    resolved_advisor_id = models.TextField(blank=True, default="")
    resolved_departments = models.TextField(blank=True, default="")
    email_masked = models.TextField(blank=True, default="")
    otp_hash = models.TextField()
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)
    status = models.TextField(default=STATUS_PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "whatsapp_otp_challenges"
        indexes = [
            models.Index(fields=["wa_id", "status"], name="idx_woc_wa_status"),
            models.Index(fields=["university_id"], name="idx_woc_university_id"),
            models.Index(fields=["expires_at"], name="idx_woc_expires"),
        ]

    def __str__(self) -> str:
        return f"WhatsAppOtpChallenge({self.wa_id}/{self.status})"


class WhatsAppConversation(models.Model):
    wa_id = models.TextField(unique=True)
    state = models.TextField(blank=True, default="")
    last_auth_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    step_up_required = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "whatsapp_conversations"
        indexes = [
            models.Index(fields=["wa_id"], name="idx_wc_wa_id"),
            models.Index(fields=["state"], name="idx_wc_state"),
        ]

    def __str__(self) -> str:
        return f"WhatsAppConversation({self.wa_id}/{self.state})"


class WhatsAppMessageLog(models.Model):
    DIRECTION_INBOUND = "inbound"
    DIRECTION_OUTBOUND = "outbound"

    wa_id = models.TextField()
    direction = models.TextField()
    message_type = models.TextField(blank=True, default="")
    text_preview = models.TextField(blank=True, default="")
    status = models.TextField(blank=True, default="")
    provider_message_id = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "whatsapp_message_logs"
        indexes = [
            models.Index(fields=["wa_id"], name="idx_wml_wa_id"),
            models.Index(fields=["direction"], name="idx_wml_direction"),
            models.Index(fields=["created_at"], name="idx_wml_created"),
        ]

    def __str__(self) -> str:
        return f"WhatsAppMessageLog({self.wa_id}/{self.direction})"
