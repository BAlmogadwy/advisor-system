"""The smallest durable record that makes a Telegram chat an authenticated student.

Three tables, and each exists because leaving it out reopens a specific hole.

**`TelegramLink` — the mapping, and nothing about the person.** A Telegram user id
is the only field that identifies the chat. There is deliberately no username, no
display name, no phone number and no profile photo: none of them is needed to
deliver an answer, all of them are personal data, and a column that exists will
eventually be filled and then read. The two partial unique constraints are the
whole authorisation story in schema form — one active Telegram identity per
student, one student per active Telegram identity, enforced by the database rather
than by a check some future code path forgets to run.

**`TelegramLinkToken` — proof that a link was ASKED for, held as a hash.** The raw
token is generated once, sent to the student's own chat, and never stored; what is
stored is its SHA-256, exactly as a password would be. So a database read cannot
mint a link. Expiry and single use are columns rather than conventions, and
`consume` claims them in one conditional UPDATE, because reading "is it unused?"
and then writing "now it is used" is two statements with a race between them, and
that race is a token that links twice.

**`TelegramUpdateReceipt` — Telegram's `update_id`, and only that.** Telegram
retries any update it does not get a prompt `200` for, so without this the same
question is asked twice, answered twice, and charged twice. `update_id` is the
primary key so uniqueness is the storage rather than a constraint layered on top,
and no part of the update body is kept: the receipt has to prove "seen", not
"what".
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, transaction
from django.db.models.functions import Now
from django.utils import timezone


class TelegramLink(models.Model):
    """One Telegram identity bound to one authenticated student account."""

    STATUS_ACTIVE = "ACTIVE"
    STATUS_REVOKED = "REVOKED"
    STATUS_CHOICES = [(STATUS_ACTIVE, "Active"), (STATUS_REVOKED, "Revoked")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: Telegram's numeric id for the PERSON (`message.from.id`), never the room.
    #: In a private chat Telegram sets `chat.id` to the same value, and the parser
    #: refuses any private update where the two disagree — so this one column is
    #: both who is asking and where to reply, without storing the room separately.
    telegram_user_id = models.BigIntegerField(db_index=True)

    #: The university student this chat speaks for. Matches the convention used by
    #: `AdvisorConversation.student_id`: a plain integer, not a foreign key, so a
    #: roster re-import cannot cascade a student's conversations away.
    student_id = models.IntegerField(db_index=True)

    #: The exact Django account whose authenticated session approved this link.
    #: ``student_id`` remains the academic subject key, while this relation makes
    #: account lifecycle changes (deactivation, deletion, role/scope changes)
    #: observable on every Telegram turn.  SET_NULL preserves the revoked-history
    #: row when an account is deleted; a null binding is never authorised.
    university_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    #: The thread this chat is currently in. Telegram has no sidebar, so "which
    #: conversation" has to live somewhere; `/new` points it at a fresh one.
    #: SET_NULL rather than CASCADE — deleting a thread must not delete the link
    #: and silently sign the student out of the bot.
    current_conversation = models.ForeignKey(
        "core.AdvisorConversation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_links",
    )

    linked_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    #: Operational only, and coarse: it answers "is this link still in use?" for an
    #: administrator pruning dormant links. It is not an activity log.
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "telegram_links"
        constraints = [
            # Revoked rows are kept as history, so both constraints are PARTIAL —
            # a plain unique on `telegram_user_id` would make re-linking after an
            # `/unlink` impossible, and the obvious workaround (delete on unlink)
            # destroys the evidence that a link ever existed.
            models.UniqueConstraint(
                fields=["telegram_user_id"],
                condition=models.Q(status="ACTIVE"),
                name="uq_tg_active_telegram_user",
            ),
            # The other direction, and the one that actually stops cross-student
            # reuse: without it, two Telegram accounts could both hold an active
            # link to the same student.
            models.UniqueConstraint(
                fields=["student_id"],
                condition=models.Q(status="ACTIVE"),
                name="uq_tg_active_student",
            ),
        ]

    def __str__(self) -> str:
        # No identifiers: this string reaches the admin changelist and any log line
        # that interpolates the object.
        return f"TelegramLink({self.id}/{self.status})"

    def revoke(self) -> None:
        """Take the link out of service immediately.

        Kept as a row rather than deleted so that "this chat was linked and is not
        any more" stays answerable, and so the partial unique constraints let the
        same student link a new device afterwards.
        """
        now = timezone.now()
        with transaction.atomic():
            self.status = self.STATUS_REVOKED
            self.revoked_at = now
            self.current_conversation = None
            self.save(update_fields=["status", "revoked_at", "current_conversation"])
            # Revocation is an authentication boundary. Any still-live ceremony
            # tied to either side of this link must die with it; otherwise an old
            # approved code can recreate the link without a fresh browser login.
            TelegramLinkToken.objects.filter(consumed_at__isnull=True).filter(
                models.Q(telegram_user_id=self.telegram_user_id)
                | models.Q(approved_student_id=self.student_id)
            ).update(consumed_at=now)


class TelegramLinkToken(models.Model):
    """A short-lived invitation, and the two-sided proof that redeems it.

    The token alone is a **bearer** credential: it says which chat asked, and
    anybody holding the URL can open it. On its own that is not enough to link,
    and treating it as though it were is an account takeover — an attacker types
    `/link` in their own chat, forwards the URL to a student, and the student's
    ordinary university login plus one confirmation button binds the *attacker's*
    chat to the *student's* record. The confirmation page cannot help: it has no
    identifier to show, by design.

    So redemption is two-sided and the halves travel in opposite directions:

    * the **token** goes server → chat → (possibly forwarded) → browser, and
      proves which chat opened the ceremony;
    * the **confirmation code** goes server → browser → chat, and is accepted only
      from the chat the token was minted in.

    A victim who follows a forwarded URL is returned a code and told to send it
    from the chat where they asked to link. They never asked, so they have no such
    chat — and if they message the bot anyway, the lookup finds no approved token
    for *their* chat and fails closed. Completing the attack now needs the student
    to relay a secret the page tells them never to share, through a channel outside
    the flow, rather than to press one button.

    Both secrets are stored only as SHA-256.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: SHA-256 of the token, hex. The token itself is never written down — it goes
    #: to the student's chat and lives only in the URL they open.
    token_hash = models.CharField(max_length=64, unique=True)

    #: Which chat opened the ceremony. NOT an identity, and never read as one: it
    #: says where the confirmation code must come back from.
    telegram_user_id = models.BigIntegerField(db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    #: Set when a signed-in student presses confirm in the browser. This is the
    #: student the link WILL bind to, taken from the session at that moment and
    #: never from the token, the URL or the chat.
    approved_student_id = models.IntegerField(null=True, blank=True)
    #: The exact account present in the authenticated browser session at
    #: approval.  Confirmation revalidates this account before copying the
    #: binding to ``TelegramLink``.
    approved_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    #: SHA-256 of the confirmation code shown in the browser. Hashed for the same
    #: reason the token is: a database read must not be able to complete a link.
    confirm_code_hash = models.CharField(max_length=64, blank=True, default="")

    #: Wrong codes tried from the chat. Bounded so a typo loop, or a chat probing
    #: an approval it did not earn, cannot run indefinitely.
    confirm_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "telegram_link_tokens"

    def __str__(self) -> str:
        return f"TelegramLinkToken({self.id})"

    @property
    def is_live(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()

    @property
    def is_approved(self) -> bool:
        return self.is_live and self.approved_student_id is not None


class TelegramUpdateReceipt(models.Model):
    """Proof that an update was accepted, and its optional durable work envelope.

    Telegram redelivers any update whose webhook call did not return `200`
    promptly, and a redelivery that reaches the adviser is a second stored
    question, a second stored answer and a second model call. The receipt is
    claimed before any of that happens.  Most receipts remain the tiny terminal
    ``INLINE`` records they always were.  Linked adviser questions and ordered
    commands may instead use the same row as a database-backed queue job, keeping
    idempotency and enqueueing atomic rather than coordinating two tables.
    """

    KIND_INLINE = "INLINE"
    KIND_QUESTION = "QUESTION"
    KIND_COMMAND = "COMMAND"
    KIND_CHOICES = (
        (KIND_INLINE, "Inline receipt"),
        (KIND_QUESTION, "Advisor question"),
        (KIND_COMMAND, "Ordered command"),
    )

    STATUS_QUEUED = "QUEUED"
    STATUS_RUNNING = "RUNNING"
    STATUS_SUCCEEDED = "SUCCEEDED"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    #: Telegram's own counter, used directly as the primary key so that "seen
    #: twice" is a primary-key collision rather than a check somebody has to
    #: remember to write.
    update_id = models.BigIntegerField(primary_key=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Existing callers create only a receipt.  These defaults deliberately make
    # those rows terminal so deploying this migration cannot queue historical
    # updates, nor change the current webhook before it is explicitly integrated.
    kind = models.CharField(
        max_length=16,
        choices=KIND_CHOICES,
        default=KIND_INLINE,
        db_default=KIND_INLINE,
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_SUCCEEDED,
        db_default=STATUS_SUCCEEDED,
    )

    # The exact link row is the authorisation and ordering key.  SET_NULL is
    # fail-closed: deleting/anonymising a link makes queued work unexecutable while
    # retaining the update-id receipt that suppresses a Telegram replay.
    link = models.ForeignKey(
        TelegramLink,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="update_jobs",
    )
    conversation = models.ForeignKey(
        "core.AdvisorConversation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_update_jobs",
    )
    assistant_message = models.ForeignKey(
        "core.AdvisorMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_update_jobs",
    )

    # Normalised text only, never the raw Telegram update.  It is cleared as soon
    # as execution has materialised a delivery or the job becomes terminal.
    payload_text = models.TextField(blank=True, default="", db_default="")
    delivery_payload = models.JSONField(default=dict, blank=True, db_default={})
    delivery_cursor = models.PositiveIntegerField(default=0, db_default=0)
    result_code = models.CharField(max_length=64, blank=True, default="", db_default="")
    error_code = models.CharField(max_length=64, blank=True, default="", db_default="")

    available_at = models.DateTimeField(default=timezone.now, db_default=Now())
    attempt_count = models.PositiveSmallIntegerField(default=0, db_default=0)
    locked_by = models.CharField(max_length=128, blank=True, default="", db_default="")
    locked_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "telegram_update_receipts"
        indexes = [
            models.Index(fields=["status", "available_at", "update_id"], name="idx_tg_job_ready"),
            models.Index(fields=["link", "status", "update_id"], name="idx_tg_job_link_fifo"),
            models.Index(fields=["status", "lease_expires_at"], name="idx_tg_job_lease"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["link"],
                condition=models.Q(status="RUNNING", link__isnull=False),
                name="uq_tg_running_link",
            )
        ]

    def __str__(self) -> str:
        return f"TelegramUpdateReceipt({self.update_id})"
