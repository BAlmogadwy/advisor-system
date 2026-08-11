from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from core.models import AdvisorConversation, AdvisorMessage, Student
from core.services import llm_backend
from core.services.advisor_channel_privacy import (
    TELEGRAM_SAFE_IDEMPOTENCY_PREFIX,
    TELEGRAM_SAFE_PROFILE,
)
from core.services.student_otp import provision_student_user
from telegram_gateway import jobs
from telegram_gateway.management.commands import telegram_advisor_worker as worker_command
from telegram_gateway.models import TelegramLink, TelegramUpdateReceipt

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
VALID_WORKER_SETTINGS = {
    "TELEGRAM_ADVISOR_ENABLED": True,
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_PUBLIC_BASE_URL": "https://advisor.example.edu",
    "LLM_BACKEND": "local",
    "LOCAL_LLM_BASE_URL": "http://127.0.0.1:1234/v1",
    "LOCAL_LLM_MODEL": "local-test-model",
}

WORKER_INHERITED_ENV = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADVISOR_ENABLED",
    "TELEGRAM_PUBLIC_BASE_URL",
    "TELEGRAM_LINK_TOKEN_TTL_SECONDS",
    "TELEGRAM_API_TIMEOUT_SECONDS",
    "LLM_BACKEND",
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_MODEL",
    "LOCAL_LLM_TIMEOUT_SECONDS",
    "LOCAL_LLM_MAX_TOKENS",
    "LOCAL_LLM_ALLOW_REMOTE",
    "ALIBABA_LLM_BASE_URL",
    "ALIBABA_LLM_API_KEY",
    "ALIBABA_LLM_MODEL",
    "ALIBABA_LLM_ENABLE_THINKING",
    "ALIBABA_LLM_TIMEOUT_SECONDS",
    "ALIBABA_LLM_MAX_TOKENS",
    "ALIBABA_LLM_MAX_RETRIES",
    "ALIBABA_LLM_ALLOW_LIVE_REQUESTS",
    "VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED",
    "VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS",
    "VIRTUAL_ADVISOR_MAX_TOOL_CALLS",
    "VIRTUAL_ADVISOR_LOOP_MAX_TOKENS",
    "VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS",
    "STUDENT_ADVISOR_V2_ENABLED",
    "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS",
    "STUDENT_ADVISOR_V2_MAX_TOOL_CALLS",
    "STUDENT_ADVISOR_V2_MAX_TOKENS",
    "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS",
}


def _link(*, chat_id: int = 7001, student_id: int = 1001) -> TelegramLink:
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": f"S{student_id}", "program": "CS", "section": "M"},
    )
    user = provision_student_user(student_id)
    return TelegramLink.objects.create(
        telegram_user_id=chat_id,
        student_id=student_id,
        university_user=user,
    )


def _enqueue(
    update_id: int,
    link: TelegramLink,
    text: str,
    *,
    kind: str = TelegramUpdateReceipt.KIND_QUESTION,
    available_at=None,
) -> TelegramUpdateReceipt:
    job, created = jobs.enqueue_question_or_command(
        update_id=update_id,
        link=link,
        kind=kind,
        payload_text=text,
        available_at=available_at,
    )
    assert created
    return job


def test_plain_receipts_keep_the_legacy_terminal_defaults():
    receipt = TelegramUpdateReceipt.objects.create(update_id=1)

    assert receipt.kind == TelegramUpdateReceipt.KIND_INLINE
    assert receipt.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert receipt.payload_text == ""


def test_old_webhook_insert_shape_survives_the_queue_migration():
    """The pre-deploy web process supplies only the original two columns."""

    received_at = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO "telegram_update_receipts" ("update_id", "received_at") VALUES (%s, %s)',
            [2, received_at],
        )

    receipt = TelegramUpdateReceipt.objects.get(update_id=2)
    assert receipt.kind == TelegramUpdateReceipt.KIND_INLINE
    assert receipt.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert receipt.payload_text == ""
    assert receipt.delivery_payload == {}
    assert receipt.delivery_cursor == 0
    assert receipt.result_code == ""
    assert receipt.error_code == ""
    assert receipt.attempt_count == 0
    assert receipt.locked_by == ""
    assert receipt.available_at is not None


@override_settings(**{**VALID_WORKER_SETTINGS, "TELEGRAM_ADVISOR_ENABLED": False})
def test_worker_refuses_to_consume_jobs_while_the_channel_is_disabled():
    with pytest.raises(CommandError, match="TELEGRAM_ADVISOR_ENABLED"):
        call_command("telegram_advisor_worker", "--once")


@override_settings(**{**VALID_WORKER_SETTINGS, "TELEGRAM_BOT_TOKEN": ""})
def test_worker_refuses_to_generate_answers_without_a_delivery_token():
    with pytest.raises(CommandError, match="TELEGRAM_BOT_TOKEN"):
        call_command("telegram_advisor_worker", "--once")


@override_settings(**{**VALID_WORKER_SETTINGS, "TELEGRAM_BOT_TOKEN": "not-a-token"})
def test_worker_refuses_a_malformed_delivery_token_before_polling(monkeypatch):
    monkeypatch.setattr(
        worker_command,
        "run_worker_loop",
        lambda **_kwargs: pytest.fail("worker polled before validating configuration"),
    )

    with pytest.raises(CommandError, match="TELEGRAM_BOT_TOKEN is malformed"):
        call_command("telegram_advisor_worker", "--once")


@override_settings(**VALID_WORKER_SETTINGS)
def test_worker_refuses_a_lease_shorter_than_the_shared_generation_window():
    with pytest.raises(CommandError, match="--lease-seconds"):
        call_command("telegram_advisor_worker", "--once", "--lease-seconds", "1")


@pytest.mark.parametrize(
    "public_base",
    [
        "",
        "http://advisor.example.edu",
        "https://advisor.example.edu/path",
        "https://user:password@advisor.example.edu",
        "https://advisor.example.edu:444",
        "https://bad_host.example.edu",
        "https://[",
    ],
)
@override_settings(**VALID_WORKER_SETTINGS)
def test_worker_refuses_an_unusable_public_origin_before_polling(monkeypatch, public_base):
    polled = False

    def poll(**_kwargs):
        nonlocal polled
        polled = True
        return 0

    monkeypatch.setattr(worker_command, "run_worker_loop", poll)
    with override_settings(TELEGRAM_PUBLIC_BASE_URL=public_base):
        with pytest.raises(CommandError, match="TELEGRAM_PUBLIC_BASE_URL"):
            call_command("telegram_advisor_worker", "--once")

    assert polled is False


@pytest.mark.parametrize(
    "llm_settings, error",
    [
        ({"LLM_BACKEND": "unknown"}, "LLM_BACKEND"),
        (
            {
                "LLM_BACKEND": "local",
                "LOCAL_LLM_BASE_URL": "https://remote-inference.example/v1",
                "LOCAL_LLM_ALLOW_REMOTE": True,
            },
            "loopback host",
        ),
        (
            {
                "LLM_BACKEND": "alibaba",
                "ALIBABA_LLM_BASE_URL": "",
                "ALIBABA_LLM_API_KEY": "",
                "ALIBABA_LLM_MODEL": "",
                "ALIBABA_LLM_ALLOW_LIVE_REQUESTS": True,
            },
            "ALIBABA_LLM_BASE_URL",
        ),
        (
            {
                "LLM_BACKEND": "alibaba",
                "ALIBABA_LLM_BASE_URL": (
                    "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
                ),
                "ALIBABA_LLM_API_KEY": "test-key",
                "ALIBABA_LLM_MODEL": "test-model",
                "ALIBABA_LLM_ALLOW_LIVE_REQUESTS": False,
            },
            "egress approval",
        ),
    ],
)
@override_settings(**VALID_WORKER_SETTINGS)
def test_worker_refuses_an_llm_configuration_that_cannot_execute(monkeypatch, llm_settings, error):
    monkeypatch.setattr(
        worker_command,
        "run_worker_loop",
        lambda **_kwargs: pytest.fail("worker polled before validating configuration"),
    )
    with override_settings(**llm_settings):
        with pytest.raises(CommandError, match=error):
            call_command("telegram_advisor_worker", "--once")


@override_settings(
    **{
        **VALID_WORKER_SETTINGS,
        "LLM_BACKEND": "alibaba",
        "ALIBABA_LLM_BASE_URL": (
            "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
        ),
        "ALIBABA_LLM_API_KEY": "test-key",
        "ALIBABA_LLM_MODEL": "test-model",
        "ALIBABA_LLM_ALLOW_LIVE_REQUESTS": True,
    }
)
def test_valid_worker_configuration_is_network_free_and_then_polls(monkeypatch):
    seen = {}

    def no_network(*_args, **_kwargs):
        pytest.fail("worker configuration validation opened a network connection")

    def poll(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(llm_backend, "_http_open", no_network)
    monkeypatch.setattr(worker_command, "run_worker_loop", poll)

    call_command("telegram_advisor_worker", "--once")

    assert seen["once"] is True


def test_render_worker_inherits_every_runtime_setting_from_the_web_service():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in blueprint["services"]}
    web_env = {entry["key"]: entry for entry in services["advisor-system"]["envVars"]}
    worker_env = {entry["key"]: entry for entry in services["advisor-telegram-worker"]["envVars"]}

    assert not [entry["key"] for entry in worker_env.values() if "sync" in entry]
    for key in WORKER_INHERITED_ENV:
        assert web_env[key]["sync"] is False, key
        assert worker_env[key]["fromService"] == {
            "name": "advisor-system",
            "type": "web",
            "envVarKey": key,
        }, key

    assert worker_env["TELEGRAM_SEND_TIMETABLE_IMAGES"] == {
        "key": "TELEGRAM_SEND_TIMETABLE_IMAGES",
        "value": "false",
    }


def test_enqueue_is_idempotent_and_never_overwrites_the_first_payload():
    link = _link()
    first, created = jobs.enqueue_question_or_command(
        update_id=10,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        payload_text="first question",
    )
    repeated, repeated_created = jobs.enqueue_question_or_command(
        update_id=10,
        link=link,
        kind=TelegramUpdateReceipt.KIND_COMMAND,
        payload_text="/new",
    )

    assert created is True and repeated_created is False
    assert first.pk == repeated.pk
    repeated.refresh_from_db()
    assert repeated.kind == TelegramUpdateReceipt.KIND_QUESTION
    assert repeated.payload_text == "first question"


@override_settings(TELEGRAM_MAX_PENDING_PER_LINK=2)
def test_enqueue_cap_is_atomic_and_duplicates_still_replay_at_the_cap():
    link = _link()
    _enqueue(14, link, "first")
    second = _enqueue(15, link, "second")

    repeated, created = jobs.enqueue_question_or_command(
        update_id=15,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        payload_text="must not replace second",
    )
    assert created is False
    assert repeated.pk == second.pk

    with pytest.raises(jobs.QueueFull):
        _enqueue(16, link, "third")

    jobs.run_job(14, executor=lambda _job: {"messages": [], "result_code": "done"})
    third = _enqueue(16, link, "third")
    assert third.status == TelegramUpdateReceipt.STATUS_QUEUED


@override_settings(TELEGRAM_MAX_PENDING_PER_LINK=1)
def test_queue_full_attempts_still_spend_the_durable_ingress_budget(monkeypatch):
    from core.services import rate_limit

    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TELEGRAM_INGRESS, (2, 600))
    link = _link()
    _enqueue(17, link, "first")

    with pytest.raises(jobs.QueueFull):
        _enqueue(18, link, "full")
    with pytest.raises(jobs.AdmissionLimited):
        _enqueue(19, link, "still full")


def test_future_head_blocks_its_link_but_not_another_link():
    first_link = _link()
    other_link = _link(chat_id=7002, student_id=1002)
    _enqueue(10, first_link, "first", available_at=timezone.now() + timedelta(minutes=5))
    _enqueue(11, first_link, "second")
    _enqueue(12, other_link, "other")
    seen: list[str] = []

    def execute(job):
        seen.append(job.payload_text)
        return {"messages": [], "result_code": "done"}

    claimed = jobs.run_next_job("worker-a", execute=execute)

    assert claimed is not None and claimed.update_id == 12
    assert seen == ["other"]
    assert (
        TelegramUpdateReceipt.objects.get(update_id=11).status
        == TelegramUpdateReceipt.STATUS_QUEUED
    )


def test_jobs_for_one_link_run_in_update_id_order():
    link = _link()
    _enqueue(21, link, "one")
    _enqueue(22, link, "/new", kind=TelegramUpdateReceipt.KIND_COMMAND)
    _enqueue(23, link, "two")
    seen: list[int] = []

    def execute(job):
        seen.append(job.update_id)
        return {"messages": [], "result_code": "done"}

    for _ in range(3):
        jobs.run_next_job("worker-a", execute=execute)

    assert seen == [21, 22, 23]


def test_only_one_running_job_is_allowed_per_link():
    link = _link()
    TelegramUpdateReceipt.objects.create(
        update_id=31,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TelegramUpdateReceipt.objects.create(
                update_id=32,
                link=link,
                kind=TelegramUpdateReceipt.KIND_QUESTION,
                status=TelegramUpdateReceipt.STATUS_RUNNING,
            )


def test_delivery_retry_resumes_at_the_first_unconfirmed_message():
    link = _link()
    _enqueue(40, link, "question")
    executions = 0
    deliveries: list[str] = []
    failed_once = False

    def execute(job):
        nonlocal executions
        executions += 1
        return {"messages": ["first", "second"], "result_code": "answered"}

    def deliver(job, text):
        nonlocal failed_once
        deliveries.append(text)
        if text == "second" and not failed_once:
            failed_once = True
            return {"ok": False, "error": "unreachable", "retry_after": 0}
        return {"ok": True}

    first_attempt = jobs.run_job(40, "worker-a", executor=execute, deliver=deliver)
    assert first_attempt is not None
    assert first_attempt.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert first_attempt.delivery_cursor == 1
    assert first_attempt.payload_text == ""

    second_attempt = jobs.run_job(40, "worker-b", executor=execute, deliver=deliver)
    assert second_attempt is not None
    assert second_attempt.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert second_attempt.delivery_cursor == 2
    assert second_attempt.delivery_payload == {}
    assert executions == 1
    assert deliveries == ["first", "second", "second"]


def test_executor_result_records_message_and_conversation_foreign_keys():
    link = _link()
    _enqueue(50, link, "question")
    conversation = AdvisorConversation.objects.create(student_id=link.student_id)
    assistant = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="answer",
    )

    result = jobs.run_job(
        50,
        "worker-a",
        executor=lambda job: {
            "messages": [],
            "result_code": "answered",
            "assistant_message_id": assistant.pk,
            "conversation_id": conversation.pk,
        },
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.assistant_message_id == assistant.pk
    assert result.conversation_id == conversation.pk
    assert result.result_code == "answered"


def test_a_fresh_pending_turn_is_deferred_instead_of_marked_successful(monkeypatch):
    link = _link()
    conversation = AdvisorConversation.objects.create(student_id=link.student_id)
    link.current_conversation = conversation
    link.save(update_fields=["current_conversation"])
    question = "Which courses should I take?"
    update_id = 55
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=question,
        idempotency_key=f"{TELEGRAM_SAFE_IDEMPOTENCY_PREFIX}{update_id}",
        generation_profile=TELEGRAM_SAFE_PROFILE,
        request_hash=hashlib.sha256(question.encode()).hexdigest(),
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now(),
    )
    _enqueue(update_id, link, question)

    def model_must_not_run(**kwargs):  # noqa: ARG001
        raise AssertionError("a live idempotent turn was generated twice")

    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor",
        model_must_not_run,
    )
    result = jobs.run_job(update_id, "worker-a")

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert result.error_code == "generation_in_progress"
    assert result.available_at > timezone.now()


def test_stale_lease_requeues_then_fails_at_the_attempt_cap():
    link = _link()
    job = _enqueue(60, link, "question")
    past = timezone.now() - timedelta(minutes=1)
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        attempt_count=1,
        locked_by="dead-worker",
        locked_at=past,
        lease_expires_at=past,
    )

    assert jobs.recover_stale_jobs(max_attempts=2) == 1
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert job.locked_by == ""

    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        attempt_count=2,
        locked_by="dead-again",
        locked_at=past,
        lease_expires_at=past,
    )
    assert jobs.recover_stale_jobs(max_attempts=2) == 1
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_FAILED
    assert job.error_code == "lease_expired_max_attempts"
    assert job.payload_text == ""


def test_an_expired_worker_cannot_finish_a_new_owners_lease():
    link = _link()
    old_lock = timezone.now() - timedelta(minutes=2)
    job = TelegramUpdateReceipt.objects.create(
        update_id=70,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        locked_by="worker-a",
        locked_at=old_lock,
        lease_expires_at=timezone.now() + timedelta(minutes=1),
    )
    stale_worker_copy = TelegramUpdateReceipt.objects.get(pk=job.pk)
    new_lock = timezone.now()
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(
        locked_by="worker-b",
        locked_at=new_lock,
        lease_expires_at=new_lock + timedelta(minutes=20),
    )

    assert jobs.finish_job(stale_worker_copy) is False
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_RUNNING
    assert job.locked_by == "worker-b"


def test_revocation_cancels_queued_work_and_clears_sensitive_payloads():
    link = _link()
    job = _enqueue(80, link, "private question")
    link.revoke()

    assert jobs.cancel_jobs_for_revoked_links() == 1
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_CANCELLED
    assert job.payload_text == ""
    assert job.delivery_payload == {}


def test_revocation_during_execution_prevents_delivery():
    link = _link()
    _enqueue(90, link, "private question")
    deliveries: list[str] = []

    def execute(job):
        link.revoke()
        return {"messages": ["private answer"], "result_code": "answered"}

    result = jobs.run_job(
        90,
        "worker-a",
        executor=execute,
        deliver=lambda job, text: deliveries.append(text),
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_CANCELLED
    assert deliveries == []


def test_exception_details_are_not_persisted():
    link = _link()
    _enqueue(100, link, "secret question")

    def execute(job):
        raise RuntimeError("student 1001 secret question")

    result = jobs.run_job(100, "worker-a", max_attempts=1, executor=execute)

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_FAILED
    assert result.error_code == "RuntimeError"
    assert "student" not in result.error_code
    assert result.payload_text == ""
