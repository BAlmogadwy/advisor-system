from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

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
from core.services.rbac import ROLE_STUDENT
from telegram_gateway.models import TelegramLink, TelegramUpdateReceipt
from whatsapp_gateway.models import (
    WhatsAppConversation,
    WhatsAppMessageLog,
    WhatsAppOtpChallenge,
    WhatsAppUserLink,
)

pytestmark = pytest.mark.django_db

QUESTION_SECRET = "student question that must disappear"
ANSWER_SECRET = "assistant answer that must disappear"


def _session_for(user: User) -> str:
    client = Client()
    client.force_login(user)
    key = client.session.session_key
    assert key is not None
    return key


def _student_account(*, username: str, student_id: int, is_staff: bool = False) -> User:
    user = User.objects.create_user(username=username, is_staff=is_staff)
    student_group, _ = Group.objects.get_or_create(name=ROLE_STUDENT)
    user.groups.add(student_group)
    UserScope.objects.create(user=user, student_id=student_id)
    return user


def _build_cutover_world() -> dict[str, object]:
    student_id = 4400001
    student_user = _student_account(username="student-cutover", student_id=student_id)
    # Even if a malformed legacy account carries the STUDENT group, an operator
    # account is outside this command's logout boundary.
    staff_user = _student_account(username="staff-cutover", student_id=4499999, is_staff=True)
    ordinary_user = User.objects.create_user(username="ordinary-cutover")
    UserScope.objects.create(user=ordinary_user)

    student_session = _session_for(student_user)
    staff_session = _session_for(staff_user)
    ordinary_session = _session_for(ordinary_user)

    student = Student.objects.create(student_id=student_id, name="Preserved academic row")
    AuditLog.objects.create(
        ts_utc="2026-08-21T00:00:00Z",
        action="PRESERVED_AUDIT_EVENT",
        details_json='{"chain":"must-survive"}',
        prev_hash="previous",
        entry_hash="current",
    )

    conversation = AdvisorConversation.objects.create(
        student_id=student_id,
        title="transcript title",
    )
    question = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=QUESTION_SECRET,
    )
    answer = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content=ANSWER_SECRET,
        in_reply_to=question,
    )
    AdvisorMessageCitation.objects.create(
        message=answer,
        policy_id="POLICY.SECRET",
        document_title="citation snapshot",
    )
    AdvisorFeedback.objects.create(
        message=answer,
        student_id=student_id,
        rating=AdvisorFeedback.HELPFUL,
        comment="feedback body",
    )
    escalation = AdvisorEscalation.objects.create(
        conversation=conversation,
        source_message=answer,
        student_id=student_id,
        reason_code=AdvisorEscalation.Reason.STUDENT_REQUESTED,
        student_note="escalation body",
    )
    AdvisorEscalationEvent.objects.create(
        escalation=escalation,
        kind=AdvisorEscalationEvent.Kind.OPENED,
    )

    draft = PlannerDraft.objects.create(
        student_id=student_id,
        course_codes=["AI433"],
        source_message=answer,
        generated_inputs={"preserved": True},
        expires_at=timezone.now() + timedelta(days=1),
    )
    link = TelegramLink.objects.create(
        telegram_user_id=700001,
        student_id=student_id,
        university_user=student_user,
        current_conversation=conversation,
    )

    queued = TelegramUpdateReceipt.objects.create(
        update_id=101,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        link=link,
        conversation=conversation,
        assistant_message=answer,
        payload_text=QUESTION_SECRET,
        delivery_payload={"text": ANSWER_SECRET},
    )
    running = TelegramUpdateReceipt.objects.create(
        update_id=102,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        conversation=conversation,
        payload_text="running question",
        delivery_payload={"text": "running answer"},
        locked_by="worker-1",
        locked_at=timezone.now(),
        lease_expires_at=timezone.now() + timedelta(minutes=1),
    )
    succeeded = TelegramUpdateReceipt.objects.create(
        update_id=103,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_SUCCEEDED,
        conversation=conversation,
        assistant_message=answer,
        payload_text="legacy terminal question body",
        delivery_payload={"text": "legacy terminal answer body"},
    )
    command = TelegramUpdateReceipt.objects.create(
        update_id=104,
        kind=TelegramUpdateReceipt.KIND_COMMAND,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        link=link,
        payload_text="/new",
        delivery_payload={"command": "preserved"},
    )
    StudentLoginOTP.objects.create(
        student_id=student_id,
        code_hash="a" * 64,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    whatsapp_link = WhatsAppUserLink.objects.create(
        wa_id="966500000001",
        phone_number="+966500000001",
        role=ROLE_STUDENT,
        user=student_user,
        student=student,
        verified_at=timezone.now(),
    )
    WhatsAppOtpChallenge.objects.create(
        wa_id=whatsapp_link.wa_id,
        university_id=str(student_id),
        resolved_role=ROLE_STUDENT,
        resolved_user=student_user,
        resolved_student=student,
        email_masked="old***@taibahu.edu.sa",
        otp_hash="b" * 64,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    WhatsAppConversation.objects.create(
        wa_id=whatsapp_link.wa_id,
        state='{"last_question":"channel transcript state"}',
        last_message_at=timezone.now(),
    )
    WhatsAppMessageLog.objects.create(
        wa_id=whatsapp_link.wa_id,
        direction=WhatsAppMessageLog.DIRECTION_INBOUND,
        message_type="text",
        text_preview="WhatsApp question preview that must disappear",
    )

    return {
        "student_session": student_session,
        "staff_session": staff_session,
        "ordinary_session": ordinary_session,
        "draft": draft,
        "link": link,
        "queued": queued,
        "running": running,
        "succeeded": succeeded,
        "command": command,
        "whatsapp_link": whatsapp_link,
    }


def test_cutover_is_dry_by_default_and_reports_only_counts() -> None:
    world = _build_cutover_world()
    out = StringIO()

    call_command("deployment_cutover", stdout=out)

    assert AdvisorConversation.objects.count() == 1
    assert AdvisorMessage.objects.count() == 2
    assert StudentLoginOTP.objects.count() == 1
    assert WhatsAppOtpChallenge.objects.count() == 1
    assert WhatsAppConversation.objects.count() == 1
    assert WhatsAppMessageLog.objects.count() == 1
    assert WhatsAppUserLink.objects.count() == 1
    assert Session.objects.filter(session_key=world["student_session"]).exists()
    assert TelegramUpdateReceipt.objects.get(update_id=101).status == (
        TelegramUpdateReceipt.STATUS_QUEUED
    )

    output = out.getvalue()
    assert "BEFORE:" in output
    assert "DRY RUN" in output
    assert "--confirm" in output
    assert "AFTER:" not in output
    assert QUESTION_SECRET not in output
    assert ANSWER_SECRET not in output


def test_confirmed_cutover_removes_transcripts_and_student_auth_only(monkeypatch) -> None:
    world = _build_cutover_world()
    broken = Session.objects.create(
        session_key="broken-session-row",
        session_data="not-a-decodable-session",
        expire_date=timezone.now() + timedelta(days=1),
    )
    original_decode = Session.get_decoded

    def decode_or_raise(self):
        if self.pk == broken.pk:
            raise ValueError("legacy signing failure")
        return original_decode(self)

    monkeypatch.setattr(Session, "get_decoded", decode_or_raise)
    out = StringIO()

    call_command("deployment_cutover", "--confirm", stdout=out)

    assert not AdvisorConversation.objects.exists()
    assert not AdvisorMessage.objects.exists()
    assert not AdvisorMessageCitation.objects.exists()
    assert not AdvisorFeedback.objects.exists()
    assert not AdvisorEscalation.objects.exists()
    assert not AdvisorEscalationEvent.objects.exists()

    assert not StudentLoginOTP.objects.exists()
    assert not WhatsAppOtpChallenge.objects.exists()
    assert not WhatsAppConversation.objects.exists()
    assert not WhatsAppMessageLog.objects.exists()
    assert not Session.objects.filter(session_key=world["student_session"]).exists()
    assert Session.objects.filter(session_key=world["staff_session"]).exists()
    assert Session.objects.filter(session_key=world["ordinary_session"]).exists()
    assert Session.objects.filter(session_key=broken.pk).exists()

    link = TelegramLink.objects.get(pk=world["link"].pk)
    assert link.current_conversation_id is None
    assert link.university_user_id is not None
    whatsapp_link = WhatsAppUserLink.objects.get(pk=world["whatsapp_link"].pk)
    assert whatsapp_link.user_id is not None
    assert whatsapp_link.student_id is not None
    assert whatsapp_link.status == WhatsAppUserLink.STATUS_ACTIVE

    assert TelegramUpdateReceipt.objects.count() == 4
    for update_id in (101, 102):
        receipt = TelegramUpdateReceipt.objects.get(update_id=update_id)
        assert receipt.status == TelegramUpdateReceipt.STATUS_CANCELLED
        assert receipt.payload_text == ""
        assert receipt.delivery_payload == {}
        assert receipt.conversation_id is None
        assert receipt.assistant_message_id is None
        assert receipt.locked_by == ""
        assert receipt.locked_at is None
        assert receipt.lease_expires_at is None
        assert receipt.finished_at is not None

    succeeded = TelegramUpdateReceipt.objects.get(update_id=103)
    assert succeeded.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert succeeded.payload_text == ""
    assert succeeded.delivery_payload == {}
    assert succeeded.conversation_id is None
    assert succeeded.assistant_message_id is None

    command = TelegramUpdateReceipt.objects.get(update_id=104)
    assert command.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert command.payload_text == "/new"
    assert command.delivery_payload == {"command": "preserved"}

    draft = PlannerDraft.objects.get(pk=world["draft"].pk)
    assert draft.source_message_id is None
    assert draft.course_codes == ["AI433"]
    assert draft.generated_inputs == {"preserved": True}
    assert Student.objects.count() == 1
    assert AuditLog.objects.get().entry_hash == "current"

    output = out.getvalue()
    assert "BEFORE:" in output
    assert "AFTER:" in output
    assert "advisor_conversations=0" in output
    assert "student_sessions=0" in output
    assert "whatsapp_otp_challenges=0" in output
    assert "whatsapp_conversations=0" in output
    assert "whatsapp_message_logs=0" in output
    assert "whatsapp_user_links=1" in output
    assert "Deployment cutover completed" in output
    assert QUESTION_SECRET not in output
    assert ANSWER_SECRET not in output


def test_cutover_mutations_roll_back_together(monkeypatch) -> None:
    from core.management.commands import deployment_cutover

    _build_cutover_world()

    def fail_session_delete(database, session_keys):  # noqa: ARG001
        raise RuntimeError("simulated late failure")

    monkeypatch.setattr(deployment_cutover, "_delete_student_sessions", fail_session_delete)

    with pytest.raises(RuntimeError, match="simulated late failure"):
        call_command("deployment_cutover", "--confirm", stdout=StringIO())

    assert AdvisorConversation.objects.count() == 1
    assert AdvisorMessage.objects.count() == 2
    assert StudentLoginOTP.objects.count() == 1
    assert WhatsAppOtpChallenge.objects.count() == 1
    assert WhatsAppConversation.objects.count() == 1
    assert WhatsAppMessageLog.objects.count() == 1
    assert WhatsAppUserLink.objects.count() == 1
    queued = TelegramUpdateReceipt.objects.get(update_id=101)
    assert queued.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert queued.payload_text == QUESTION_SECRET
    assert queued.delivery_payload == {"text": ANSWER_SECRET}


def test_secure_sqlite_steps_are_guarded_and_run_around_a_confirmed_cutover(
    monkeypatch,
) -> None:
    from core.management.commands import deployment_cutover

    events: list[str] = []
    monkeypatch.setattr(
        deployment_cutover,
        "_enable_sqlite_secure_delete",
        lambda connection: events.append(f"before:{connection.vendor}"),
    )
    monkeypatch.setattr(
        deployment_cutover,
        "_compact_sqlite_after_commit",
        lambda connection: events.append(f"after:{connection.vendor}"),
    )

    call_command("deployment_cutover", "--secure-sqlite", stdout=StringIO())
    assert events == [], "a dry run changed SQLite connection/storage state"

    call_command(
        "deployment_cutover",
        "--confirm",
        "--secure-sqlite",
        stdout=StringIO(),
    )
    assert events == ["before:sqlite", "after:sqlite"]


def test_secure_sqlite_checkpoints_the_wal_before_and_after_vacuum() -> None:
    from core.management.commands.deployment_cutover import _compact_sqlite_after_commit

    executed: list[str] = []
    checkpoint_results = iter([(0, 0, 0), (0, 0, 0)])

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        def execute(self, sql: str) -> None:
            executed.append(sql)

        def fetchone(self):
            return next(checkpoint_results)

    class Connection:
        def cursor(self):
            return Cursor()

    _compact_sqlite_after_commit(Connection())

    assert executed == [
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "VACUUM",
        "PRAGMA wal_checkpoint(TRUNCATE)",
    ]


def test_secure_sqlite_rejects_a_busy_post_vacuum_wal() -> None:
    from core.management.commands.deployment_cutover import _compact_sqlite_after_commit

    checkpoint_results = iter([(0, 0, 0), (1, 1, 0)])

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        def execute(self, sql: str) -> None:  # noqa: ARG002
            return None

        def fetchone(self):
            return next(checkpoint_results)

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(CommandError, match="cutover committed"):
        _compact_sqlite_after_commit(Connection())
