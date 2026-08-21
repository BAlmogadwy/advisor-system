"""Remove test-era student adviser state before opening a deployment.

This is intentionally a one-shot operator command, not ordinary retention.  Stop
the web application and every Telegram worker before applying it: a writer that
starts a new turn after the transaction commits can recreate the state this
command just removed.

The command is dry-run by default.  ``--confirm`` performs one atomic database
cutover which:

* deletes every adviser conversation and its transcript-owned cascade;
* preserves Telegram links and update-id receipts, while cancelling active
  QUESTION jobs and removing question/delivery bodies from every QUESTION receipt;
* deletes WhatsApp channel history while preserving its verified user links;
* deletes unused student and WhatsApp login OTP challenges; and
* logs out student-scoped browser sessions without touching staff/admin sessions.

Planner drafts, the audit chain, rate-limit/account state, Telegram bindings and
academic/student data are deliberately outside the deletion boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.contrib.auth import SESSION_KEY as AUTH_USER_SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.connection import ConnectionDoesNotExist

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorEscalationEvent,
    AdvisorFeedback,
    AdvisorMessage,
    AdvisorMessageCitation,
    AuditLog,
    PlannerDraft,
    Student,
    StudentLoginOTP,
    UserScope,
)
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)
from telegram_gateway.models import TelegramLink, TelegramUpdateReceipt
from whatsapp_gateway.models import (
    WhatsAppConversation,
    WhatsAppMessageLog,
    WhatsAppOtpChallenge,
    WhatsAppUserLink,
)

ACTIVE_QUESTION_STATUSES = (
    TelegramUpdateReceipt.STATUS_QUEUED,
    TelegramUpdateReceipt.STATUS_RUNNING,
)
CUTOVER_CANCEL_CODE = "DEPLOYMENT_CUTOVER"


def _student_user_ids(database: str) -> set[str]:
    """Return non-staff users whose effective persisted scope is STUDENT.

    A ``UserScope`` row alone has no role column; the STUDENT auth group is the
    role source used by the application.  Elevated roles win in ``get_user_role``,
    so a legacy dual-group account in SUPER_ADMIN or GENERAL_ACADEMIC_ADVISOR is
    preserved.  ``is_staff``/``is_superuser`` are an additional fail-safe: this
    launch command must never log an operator out.
    """

    ids = (
        UserScope.objects.using(database)
        .filter(user__groups__name=ROLE_STUDENT)
        .filter(user__is_staff=False, user__is_superuser=False)
        .exclude(user__groups__name__in=(ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR))
        .values_list("user_id", flat=True)
        .distinct()
    )
    return {str(user_id) for user_id in ids}


def _student_session_keys(database: str) -> tuple[list[str], int]:
    """Decode sessions defensively and return only authenticated student keys."""

    student_user_ids = _student_user_ids(database)
    if not student_user_ids:
        return [], 0

    keys: list[str] = []
    undecodable = 0
    sessions = (
        Session.objects.using(database).all().only("session_key", "session_data", "expire_date")
    )
    for session in sessions.iterator():
        try:
            data = session.get_decoded()
        except Exception:  # A damaged/old-signing-key row must not abort the purge.
            undecodable += 1
            continue
        user_id = data.get(AUTH_USER_SESSION_KEY)
        if user_id is not None and str(user_id) in student_user_ids:
            keys.append(str(session.pk))
    return keys, undecodable


def _counts(database: str) -> dict[str, int]:
    question_receipts = TelegramUpdateReceipt.objects.using(database).filter(
        kind=TelegramUpdateReceipt.KIND_QUESTION
    )
    question_payloads = question_receipts.filter(
        ~Q(payload_text="") | ~Q(delivery_payload={})
    ).count()
    student_session_keys, undecodable_sessions = _student_session_keys(database)
    return {
        # Transcript rows which must reach zero.
        "advisor_conversations": AdvisorConversation.objects.using(database).count(),
        "advisor_messages": AdvisorMessage.objects.using(database).count(),
        "advisor_message_citations": AdvisorMessageCitation.objects.using(database).count(),
        "advisor_feedback": AdvisorFeedback.objects.using(database).count(),
        "advisor_escalations": AdvisorEscalation.objects.using(database).count(),
        "advisor_escalation_events": AdvisorEscalationEvent.objects.using(database).count(),
        # Preserved anti-replay rows, with active/body-bearing subsets reported.
        "telegram_update_receipts": TelegramUpdateReceipt.objects.using(database).count(),
        "telegram_active_questions": question_receipts.filter(
            status__in=ACTIVE_QUESTION_STATUSES
        ).count(),
        "telegram_question_payloads": question_payloads,
        # Ephemeral authentication state which must reach zero.
        "student_login_otps": StudentLoginOTP.objects.using(database).count(),
        "whatsapp_otp_challenges": WhatsAppOtpChallenge.objects.using(database).count(),
        "student_sessions": len(student_session_keys),
        "undecodable_sessions_skipped": undecodable_sessions,
        # Explicit preservation boundary.
        "telegram_links": TelegramLink.objects.using(database).count(),
        "whatsapp_user_links": WhatsAppUserLink.objects.using(database).count(),
        # WhatsApp stores channel history outside AdvisorConversation, so these
        # independent transcript/state rows must also reach zero.
        "whatsapp_conversations": WhatsAppConversation.objects.using(database).count(),
        "whatsapp_message_logs": WhatsAppMessageLog.objects.using(database).count(),
        "planner_drafts": PlannerDraft.objects.using(database).count(),
        "audit_logs": AuditLog.objects.using(database).count(),
        "students": Student.objects.using(database).count(),
    }


def _lock_cutover_rows(database: str, student_session_keys: Iterable[str]) -> None:
    """Take row locks before changing cross-referenced launch state."""

    list(
        TelegramUpdateReceipt.objects.using(database)
        .select_for_update()
        .filter(kind=TelegramUpdateReceipt.KIND_QUESTION)
        .values_list("pk", flat=True)
    )
    list(
        AdvisorConversation.objects.using(database).select_for_update().values_list("pk", flat=True)
    )
    list(StudentLoginOTP.objects.using(database).select_for_update().values_list("pk", flat=True))
    list(
        WhatsAppOtpChallenge.objects.using(database)
        .select_for_update()
        .values_list("pk", flat=True)
    )
    list(
        WhatsAppConversation.objects.using(database)
        .select_for_update()
        .values_list("pk", flat=True)
    )
    list(
        WhatsAppMessageLog.objects.using(database).select_for_update().values_list("pk", flat=True)
    )
    list(
        Session.objects.using(database)
        .select_for_update()
        .filter(session_key__in=list(student_session_keys))
        .values_list("pk", flat=True)
    )


def _delete_student_sessions(database: str, session_keys: Iterable[str]) -> int:
    keys = list(session_keys)
    if not keys:
        return 0
    deleted, _ = Session.objects.using(database).filter(session_key__in=keys).delete()
    return int(deleted)


def _apply_cutover(database: str) -> None:
    now = timezone.now()
    with transaction.atomic(using=database):
        session_keys, _ = _student_session_keys(database)
        _lock_cutover_rows(database, session_keys)

        questions = TelegramUpdateReceipt.objects.using(database).filter(
            kind=TelegramUpdateReceipt.KIND_QUESTION
        )
        # Terminal rows are normally already body-free.  Scrub all QUESTION rows
        # anyway so a legacy/failed delivery cannot retain a transcript fragment.
        questions.update(payload_text="", delivery_payload={})
        questions.filter(status__in=ACTIVE_QUESTION_STATUSES).update(
            status=TelegramUpdateReceipt.STATUS_CANCELLED,
            payload_text="",
            delivery_payload={},
            delivery_cursor=0,
            result_code="",
            error_code=CUTOVER_CANCEL_CODE,
            locked_by="",
            locked_at=None,
            lease_expires_at=None,
            finished_at=now,
        )

        # These cascades own the complete adviser transcript: messages, citations,
        # feedback, escalations and escalation events.  SET_NULL references on
        # Telegram receipts/links and PlannerDraft preserve those rows safely.
        AdvisorConversation.objects.using(database).all().delete()
        StudentLoginOTP.objects.using(database).all().delete()
        WhatsAppOtpChallenge.objects.using(database).all().delete()
        WhatsAppConversation.objects.using(database).all().delete()
        WhatsAppMessageLog.objects.using(database).all().delete()
        _delete_student_sessions(database, session_keys)


def _enable_sqlite_secure_delete(db_connection: Any) -> None:
    with db_connection.cursor() as cursor:
        cursor.execute("PRAGMA secure_delete = ON")
        cursor.execute("PRAGMA secure_delete")
        row = cursor.fetchone()
    if not row or int(row[0]) != 1:
        raise CommandError("SQLite refused PRAGMA secure_delete=ON; no cutover was applied.")


def _compact_sqlite_after_commit(db_connection: Any) -> None:
    """Remove deleted bytes from SQLite's WAL and free database pages."""

    def checkpoint_and_require_empty(cursor: Any) -> None:
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint = cursor.fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise RuntimeError("the WAL checkpoint reported a busy writer")

    try:
        with db_connection.cursor() as cursor:
            checkpoint_and_require_empty(cursor)
            cursor.execute("VACUUM")
            # VACUUM itself writes a replacement database.  Checkpoint once more
            # afterwards so its sanitized write transaction cannot leave a WAL
            # segment behind when the command reports success.
            checkpoint_and_require_empty(cursor)
    except Exception as exc:
        # The atomic cutover has already committed at this point.  Say so plainly;
        # claiming rollback here would make the operator repeat a destructive run.
        raise CommandError(
            "The cutover committed, but SQLite WAL truncation/VACUUM failed. "
            "Keep all writers stopped and run a manual checkpoint plus VACUUM."
        ) from exc


class Command(BaseCommand):
    help = (
        "Purge adviser transcripts and student login state for deployment. "
        "Dry-run unless --confirm; stop all web/worker writers before applying."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Apply the destructive cutover. Without this flag, only counts are reported.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias to cut over (default: default).",
        )
        parser.add_argument(
            "--secure-sqlite",
            action="store_true",
            help=(
                "SQLite only: enable secure_delete before the transaction, then "
                "truncate the WAL and VACUUM after commit. All writers must be stopped."
            ),
        )

    def _write_counts(self, label: str, counts: dict[str, int]) -> None:
        self.stdout.write(f"{label}:")
        for name, count in counts.items():
            self.stdout.write(f"  {name}={count}")

    def handle(self, *args: Any, **options: Any) -> None:
        database = str(options["database"])
        try:
            db_connection = connections[database]
        except ConnectionDoesNotExist as exc:
            raise CommandError(f"Unknown database alias: {database}") from exc

        secure_sqlite = bool(options["secure_sqlite"])
        if secure_sqlite and db_connection.vendor != "sqlite":
            raise CommandError("--secure-sqlite is valid only for a SQLite database.")

        before = _counts(database)
        self._write_counts("BEFORE", before)

        if not options["confirm"]:
            self.stdout.write("DRY RUN — no rows were changed.")
            if secure_sqlite:
                self.stdout.write(
                    "SQLite secure deletion, WAL truncation and VACUUM would run after --confirm."
                )
            self.stdout.write(
                "Stop the web app and Telegram workers, then re-run with --confirm to apply."
            )
            return

        self.stdout.write(
            self.style.WARNING(
                "Applying deployment cutover; all web and Telegram worker writers must be stopped."
            )
        )
        if secure_sqlite:
            _enable_sqlite_secure_delete(db_connection)

        _apply_cutover(database)
        after = _counts(database)

        if secure_sqlite:
            _compact_sqlite_after_commit(db_connection)

        self._write_counts("AFTER", after)
        self.stdout.write(self.style.SUCCESS("Deployment cutover completed."))
