from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

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

_T = TypeVar("_T")

ROOT = Path(__file__).resolve().parents[1]
VALID_WORKER_SETTINGS = {
    "TELEGRAM_ADVISOR_ENABLED": True,
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_PUBLIC_BASE_URL": "https://advisor.example.edu",
    "TELEGRAM_SEND_TIMETABLE_IMAGES": False,
    "TELEGRAM_SEND_GRADUATION_IMAGES": False,
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
    "TELEGRAM_INTERNAL_BASE_URL",
    "TELEGRAM_SEND_TIMETABLE_IMAGES",
    "TELEGRAM_SEND_GRADUATION_IMAGES",
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
        assert key in web_env, key
        assert worker_env[key]["fromService"] == {
            "name": "advisor-system",
            "type": "web",
            "envVarKey": key,
        }, key


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


def _photo_manifest(*items: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": jobs.DELIVERY_MANIFEST_VERSION,
        "items": list(items),
    }


def _photo(index: int | None) -> dict[str, Any]:
    return {"kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO, "option_index": index}


def _text(value: str) -> dict[str, Any]:
    return {"kind": jobs.DELIVERY_KIND_TEXT, "text": value}


def _legacy_split_photo_manifest(
    *items: Mapping[str, Any], messages: Sequence[str]
) -> dict[str, Any]:
    """The already-queued photo-first v2 shape kept for rollback tests."""

    return {
        **_photo_manifest(*items),
        "messages": list(messages),
        "cursor_mode": jobs.DELIVERY_CURSOR_MODE_SPLIT,
        "photo_cursor": 0,
        "photo_attempt_count": 0,
        "text_phase_started": False,
    }


def _record_success(target: list[_T], value: _T) -> dict[str, bool]:
    target.append(value)
    return {"ok": True}


def _record_failure(target: list[_T], value: _T, error: str) -> dict[str, Any]:
    target.append(value)
    return {"ok": False, "error": error, "retry_after": 0}


def test_durable_manifest_delivers_text_before_photos_with_independent_cursors() -> None:
    link = _link()
    _enqueue(41, link, "question")
    calls: list[str] = []
    render_calls: list[list[int | None]] = []

    def render(
        _job: TelegramUpdateReceipt,
        option_indexes: Sequence[int | None],
    ) -> list[bytes]:
        render_calls.append(list(option_indexes))
        return [f"png-{index}".encode() for index in option_indexes]

    result = jobs.run_job(
        41,
        "worker-a",
        executor=lambda _job: {
            "delivery_payload": _photo_manifest(
                _photo(0),
                _photo(1),
                _text("answer"),
            ),
            "result_code": "answered",
        },
        render_photos=render,
        deliver_photo=lambda _job, png: _record_success(calls, png.decode()),
        deliver=lambda _job, text: _record_success(calls, text),
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.delivery_cursor == 1
    assert render_calls == [[0, 1]], "all remaining cards should share one browser batch"
    assert calls == ["answer", "png-0", "png-1"]


def test_text_retry_stops_before_any_photo_is_rendered_or_sent() -> None:
    link = _link()
    _enqueue(4101, link, "question")
    calls: list[str] = []
    failed_once = False

    def send_text(_job: TelegramUpdateReceipt, text: str) -> Mapping[str, Any]:
        nonlocal failed_once
        calls.append(text)
        if not failed_once:
            failed_once = True
            return {"ok": False, "error": "unreachable", "retry_after": 0}
        return {"ok": True}

    kwargs: dict[str, Any] = {
        "render_photos": lambda _job, _indexes: [b"png"],
        "deliver_photo": lambda _job, png: _record_success(calls, png.decode()),
        "deliver": send_text,
    }
    first = jobs.run_job(
        4101,
        "worker-a",
        executor=lambda _job: {
            "delivery_payload": _photo_manifest(_photo(0), _text("answer")),
            "result_code": "answered",
        },
        **kwargs,
    )

    assert first is not None and first.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert calls == ["answer"]
    assert first.delivery_payload["photo_cursor"] == 0

    second = jobs.run_job(4101, "worker-b", **kwargs)

    assert second is not None and second.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert calls == ["answer", "answer", "png"]


def test_photo_retry_resumes_at_the_first_unconfirmed_item_without_rerunning_executor() -> None:
    link = _link()
    _enqueue(42, link, "question")
    executions = 0
    render_calls: list[list[int | None]] = []
    deliveries: list[str] = []
    failed_once = False

    def execute(_job: TelegramUpdateReceipt) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {
            "delivery_payload": _photo_manifest(
                _photo(0),
                _photo(1),
                _text("answer"),
            ),
            "result_code": "answered",
        }

    def render(
        _job: TelegramUpdateReceipt,
        option_indexes: Sequence[int | None],
    ) -> list[bytes]:
        render_calls.append(list(option_indexes))
        return [f"png-{index}".encode() for index in option_indexes]

    def send_photo(_job: TelegramUpdateReceipt, png: bytes) -> Mapping[str, Any]:
        nonlocal failed_once
        label = png.decode()
        deliveries.append(label)
        if label == "png-1" and not failed_once:
            failed_once = True
            return {"ok": False, "error": "unreachable", "retry_after": 0}
        return {"ok": True}

    first = jobs.run_job(
        42,
        "worker-a",
        executor=execute,
        render_photos=render,
        deliver_photo=send_photo,
        deliver=lambda _job, text: _record_success(deliveries, text),
    )
    second = jobs.run_job(
        42,
        "worker-b",
        executor=execute,
        render_photos=render,
        deliver_photo=send_photo,
        deliver=lambda _job, text: _record_success(deliveries, text),
    )

    assert first is not None and first.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert first.delivery_cursor == 1
    assert first.delivery_payload["photo_cursor"] == 1
    assert second is not None and second.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert executions == 1
    assert render_calls == [[0, 1], [1]]
    assert deliveries == ["answer", "png-0", "png-1", "png-1"]


def test_text_delivery_retries_before_photos_are_attempted() -> None:
    link = _link()
    _enqueue(43, link, "question")
    photos: list[bytes] = []
    texts: list[str] = []
    failed_once = False

    def send_text(_job: TelegramUpdateReceipt, text: str) -> Mapping[str, Any]:
        nonlocal failed_once
        texts.append(text)
        if not failed_once:
            failed_once = True
            return {"ok": False, "error": "unreachable", "retry_after": 0}
        return {"ok": True}

    kwargs: dict[str, Any] = {
        "render_photos": lambda _job, _indexes: [b"png"],
        "deliver_photo": lambda _job, png: _record_success(photos, png),
        "deliver": send_text,
    }
    first = jobs.run_job(
        43,
        "worker-a",
        executor=lambda _job: {
            "delivery_payload": _photo_manifest(_photo(0), _text("answer")),
            "result_code": "answered",
        },
        **kwargs,
    )
    second = jobs.run_job(43, "worker-b", **kwargs)

    assert first is not None and first.delivery_cursor == 0
    assert first.delivery_payload["photo_cursor"] == 0
    assert first.delivery_payload["text_first"] is True
    assert second is not None and second.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert photos == [b"png"]
    assert texts == ["answer", "answer"]


@pytest.mark.parametrize("recovery_mode", ["ordinary_claim", "stale_at_cap"])
def test_confirmed_photo_progress_survives_rollback_and_roll_forward(recovery_mode: str) -> None:
    """Old text cursors and new photo cursors must remain mutually intelligible."""

    link = _link()
    now = timezone.now()
    job = TelegramUpdateReceipt.objects.create(
        update_id=4310,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        result_code="answered",
        delivery_payload=_legacy_split_photo_manifest(
            _photo(0),
            _text("validated answer"),
            _text("second chunk"),
            messages=["validated answer", "second chunk"],
        ),
        delivery_cursor=0,
        attempt_count=1,
        locked_by="new-photo-worker",
        locked_at=now,
        lease_expires_at=now + timedelta(minutes=10),
    )

    # New worker confirms photo 0, then dies before beginning the text phase.
    assert jobs.mark_photo_progress(job, 1)
    job.refresh_from_db()
    assert job.delivery_cursor == 0
    assert job.delivery_payload["photo_cursor"] == 1
    assert job.delivery_payload["text_phase_started"] is False

    # Roll back. The previous worker knows only messages[delivery_cursor:], so it
    # sends the first text chunk and advances only the database text cursor.
    previous_worker_messages = job.delivery_payload.get("messages") or []
    delivered = [previous_worker_messages[job.delivery_cursor]]
    old_worker_values: dict[str, Any] = {"delivery_cursor": 1, "attempt_count": 2}
    if recovery_mode == "ordinary_claim":
        old_worker_values.update(
            {
                "status": TelegramUpdateReceipt.STATUS_QUEUED,
                "available_at": now,
                "locked_by": "",
                "locked_at": None,
                "lease_expires_at": None,
            }
        )
    else:
        past = now - timedelta(minutes=1)
        old_worker_values.update(
            {
                "status": TelegramUpdateReceipt.STATUS_RUNNING,
                "attempt_count": 3,
                "locked_by": "rolled-back-worker",
                "locked_at": past,
                "lease_expires_at": past,
            }
        )
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(**old_worker_values)

    if recovery_mode == "stale_at_cap":
        assert jobs.recover_stale_jobs(max_attempts=3) == 1
        job.refresh_from_db()
        assert job.status == TelegramUpdateReceipt.STATUS_QUEUED
        assert job.delivery_cursor == 1
        assert job.delivery_payload["photo_cursor"] == 1
        assert job.delivery_payload["text_phase_started"] is True
        assert job.attempt_count == 3

    result = jobs.run_job(
        job.update_id,
        "rolled-forward-worker",
        max_attempts=3,
        executor=lambda _job: pytest.fail("roll-forward reran the adviser"),
        render_photos=lambda _job, _indexes: pytest.fail("confirmed photo was rendered again"),
        deliver_photo=lambda _job, _png: pytest.fail("confirmed photo was sent again"),
        deliver=lambda _job, text: _record_success(delivered, text),
    )

    assert result is not None
    if recovery_mode == "stale_at_cap":
        assert result.status == TelegramUpdateReceipt.STATUS_FAILED
        assert result.error_code == "max_attempts_exhausted"
        assert result.delivery_cursor == 1
        assert delivered == ["validated answer"]
    else:
        assert result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
        assert result.delivery_cursor == 2
        assert delivered == ["validated answer", "second chunk"]


def test_rollback_text_takeover_preserves_the_terminal_claim_count() -> None:
    link = _link()
    job = TelegramUpdateReceipt.objects.create(
        update_id=4314,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        delivery_payload=_legacy_split_photo_manifest(
            _photo(0),
            _text("already confirmed"),
            _text("still pending"),
            messages=["already confirmed", "still pending"],
        ),
        delivery_cursor=1,
        attempt_count=2,
    )
    delivered: list[str] = []

    result = jobs.run_job(
        job.update_id,
        "rolled-forward-worker",
        max_attempts=3,
        render_photos=lambda _job, _indexes: pytest.fail("out-of-order photo was rendered"),
        deliver_photo=lambda _job, _png: pytest.fail("out-of-order photo was sent"),
        deliver=lambda _job, text: _record_failure(
            delivered,
            text,
            "temporarily_unreachable",
        ),
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_FAILED
    assert result.error_code == "temporarily_unreachable"
    assert result.attempt_count == 3
    assert result.delivery_cursor == 1
    assert delivered == ["still pending"]


@pytest.mark.parametrize("third_photo_succeeds", [True, False])
def test_photo_attempts_do_not_exhaust_the_fresh_text_retry_budget(
    third_photo_succeeds: bool,
) -> None:
    link = _link()
    _enqueue(4311, link, "question")
    executions = 0
    renders = 0
    photo_attempts = 0
    text_attempts = 0

    def execute(_job: TelegramUpdateReceipt) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {
            "messages": ["validated answer"],
            "delivery_payload": _photo_manifest(_photo(0)),
            "result_code": "answered",
        }

    def render(
        _job: TelegramUpdateReceipt,
        _indexes: Sequence[int | None],
    ) -> list[bytes]:
        nonlocal renders
        renders += 1
        return [b"png-0"]

    def send_photo(_job: TelegramUpdateReceipt, _png: bytes) -> Mapping[str, Any]:
        nonlocal photo_attempts
        photo_attempts += 1
        if photo_attempts < 3 or not third_photo_succeeds:
            return {
                "ok": False,
                "error": "temporarily_unreachable",
                "retry_after": 0,
            }
        return {"ok": True}

    def send_text(_job: TelegramUpdateReceipt, _text: str) -> Mapping[str, Any]:
        nonlocal text_attempts
        text_attempts += 1
        if text_attempts == 1:
            return {
                "ok": False,
                "error": "temporarily_unreachable",
                "retry_after": 0,
            }
        return {"ok": True}

    shared: dict[str, Any] = {
        "max_attempts": 3,
        "executor": execute,
        "render_photos": render,
        "deliver_photo": send_photo,
        "deliver": send_text,
    }
    text_failure = jobs.run_job(4311, "text-worker-1", **shared)
    assert text_failure is not None
    assert text_failure.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert text_failure.attempt_count == 1
    assert text_failure.delivery_payload["photo_cursor"] == 0
    assert text_failure.delivery_payload["text_first"] is True
    assert text_failure.delivery_cursor == 0
    first = jobs.run_job(4311, "text-worker-2-photo-1", **shared)
    second = jobs.run_job(4311, "photo-worker-2", **shared)

    assert first is not None and first.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert second is not None and second.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert executions == 1
    assert renders == 2
    assert photo_attempts == 2
    assert text_attempts == 2

    result = jobs.run_job(4311, "photo-worker-3", **shared)

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.delivery_cursor == 1
    assert executions == 1
    assert renders == 3
    assert photo_attempts == 3
    assert text_attempts == 2
    assert result.error_code == ("" if third_photo_succeeds else "image_delivery_degraded")


def test_generation_photo_and_text_each_receive_their_full_retry_budget() -> None:
    """Late generation success must not spend either outbound retry budget."""

    link = _link()
    _enqueue(4312, link, "question")
    executions = 0
    photo_attempts = 0
    text_attempts = 0

    def execute(_job: TelegramUpdateReceipt) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        if executions < 3:
            raise jobs.RetryJob("model_unreachable", delay_seconds=0)
        return {
            "messages": ["validated answer"],
            "delivery_payload": _photo_manifest(_photo(0)),
            "result_code": "answered",
        }

    def send_photo(_job: TelegramUpdateReceipt, _png: bytes) -> Mapping[str, Any]:
        nonlocal photo_attempts
        photo_attempts += 1
        return {
            "ok": photo_attempts == 3,
            "error": "photo_unreachable",
            "retry_after": 0,
        }

    def send_text(_job: TelegramUpdateReceipt, _text: str) -> Mapping[str, Any]:
        nonlocal text_attempts
        text_attempts += 1
        return {
            "ok": text_attempts == 3,
            "error": "text_unreachable",
            "retry_after": 0,
        }

    shared: dict[str, Any] = {
        "max_attempts": 3,
        "executor": execute,
        "render_photos": lambda _job, _indexes: [b"png"],
        "deliver_photo": send_photo,
        "deliver": send_text,
    }

    generation_1 = jobs.run_job(4312, "generation-1", **shared)
    assert generation_1 is not None
    assert generation_1.status == TelegramUpdateReceipt.STATUS_QUEUED
    generation_2 = jobs.run_job(4312, "generation-2", **shared)
    assert generation_2 is not None
    assert generation_2.status == TelegramUpdateReceipt.STATUS_QUEUED
    text_1 = jobs.run_job(4312, "generation-3-text-1", **shared)
    assert text_1 is not None and text_1.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert text_1.attempt_count == 1
    assert text_1.delivery_payload["photo_attempt_count"] == 0

    text_2 = jobs.run_job(4312, "text-2", **shared)
    assert text_2 is not None and text_2.status == TelegramUpdateReceipt.STATUS_QUEUED
    first_photo = jobs.run_job(4312, "text-3-photo-1", **shared)
    assert first_photo is not None and first_photo.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert first_photo.delivery_payload["photo_attempt_count"] == 1
    photo_2 = jobs.run_job(4312, "photo-2", **shared)
    assert photo_2 is not None and photo_2.status == TelegramUpdateReceipt.STATUS_QUEUED
    result = jobs.run_job(4312, "photo-3", **shared)

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert executions == 3
    assert photo_attempts == 3
    assert text_attempts == 3


def test_legacy_photo_worker_crash_remains_drainable_by_the_previous_text_worker() -> None:
    """An already-queued photo-first row remains safe during a rollback."""

    class PhotoWorkerCrash(BaseException):
        pass

    link = _link()
    _enqueue(4313, link, "question")
    job = TelegramUpdateReceipt.objects.get(update_id=4313)
    now = timezone.now()
    job.status = TelegramUpdateReceipt.STATUS_RUNNING
    job.result_code = "answered"
    job.delivery_payload = _legacy_split_photo_manifest(
        _photo(0),
        _text("validated answer"),
        messages=["validated answer"],
    )
    job.attempt_count = 0
    job.locked_by = "materialising-worker"
    job.locked_at = now
    job.lease_expires_at = now + timedelta(minutes=10)
    job.save()
    jobs._requeue_photo_delivery(
        job,
        photo_attempt_count=0,
        error_code="ready",
        delay_seconds=0,
    )
    photo_attempts = 0

    def send_photo(_job: TelegramUpdateReceipt, _png: bytes) -> Mapping[str, Any]:
        nonlocal photo_attempts
        photo_attempts += 1
        if photo_attempts == 1:
            return {"ok": False, "error": "photo_unreachable", "retry_after": 0}
        raise PhotoWorkerCrash

    shared: dict[str, Any] = {
        "max_attempts": 2,
        "render_photos": lambda _job, _indexes: [b"png"],
        "deliver_photo": send_photo,
        "deliver": lambda _job, _text: pytest.fail("new worker reached text unexpectedly"),
    }
    first = jobs.run_job(
        4313,
        "photo-worker-1",
        **shared,
    )
    assert first is not None and first.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert first.attempt_count == 0

    with pytest.raises(PhotoWorkerCrash):
        jobs.run_job(4313, "photo-worker-2", **shared)

    job = TelegramUpdateReceipt.objects.get(update_id=4313)
    assert job.status == TelegramUpdateReceipt.STATUS_RUNNING
    assert job.attempt_count == 0
    assert job.attempt_count < 2
    assert job.delivery_payload["photo_attempt_count"] == 2
    assert job.delivery_payload["messages"] == ["validated answer"]
    assert job.delivery_cursor == 0

    # This is the exact decision and read contract of the pre-image worker: a
    # stale counter below the cap is requeued, then messages[cursor:] drains.
    past = timezone.now() - timedelta(minutes=1)
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(lease_expires_at=past)
    assert jobs.recover_stale_jobs(max_attempts=2) == 1
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert job.attempt_count + 1 < 2
    assert job.delivery_payload["messages"][job.delivery_cursor :] == ["validated answer"]


def test_exhausted_photo_render_does_not_cost_the_validated_text_answer() -> None:
    link = _link()
    _enqueue(44, link, "question")
    texts: list[str] = []

    result = jobs.run_job(
        44,
        "worker-a",
        max_attempts=1,
        executor=lambda _job: {
            "delivery_payload": _photo_manifest(_photo(0), _text("answer")),
            "result_code": "answered",
        },
        render_photos=lambda _job, _indexes: [],
        deliver_photo=lambda _job, _png: pytest.fail("an absent render was sent"),
        deliver=lambda _job, text: _record_success(texts, text),
    )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.delivery_cursor == 1
    assert result.error_code == "image_delivery_degraded"
    assert texts == ["answer"]


def test_legacy_materialised_messages_still_drain_after_the_manifest_rollout() -> None:
    link = _link()
    TelegramUpdateReceipt.objects.create(
        update_id=45,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        delivery_payload={"messages": ["legacy answer"]},
    )
    delivered: list[str] = []

    result = jobs.run_job(
        45,
        "worker-a",
        executor=lambda _job: pytest.fail("materialised legacy work executed again"),
        deliver=lambda _job, text: _record_success(delivered, text),
    )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert delivered == ["legacy answer"]


@override_settings(
    TELEGRAM_SEND_TIMETABLE_IMAGES=False,
    TELEGRAM_SEND_GRADUATION_IMAGES=False,
)
def test_disabling_images_drains_an_already_materialised_photo_job_as_text() -> None:
    link = _link()
    TelegramUpdateReceipt.objects.create(
        update_id=46,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        delivery_payload=_photo_manifest(_photo(0), _text("answer")),
    )
    delivered: list[str] = []

    result = jobs.run_job(
        46,
        "worker-a",
        deliver_photo=lambda _job, _png: pytest.fail("the disabled image was sent"),
        deliver=lambda _job, text: _record_success(delivered, text),
    )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.error_code == "image_delivery_degraded"
    assert delivered == ["answer"]


@pytest.mark.parametrize(
    ("update_id", "settings_overrides", "presentation", "option_index"),
    [
        (
            4601,
            {
                "TELEGRAM_SEND_TIMETABLE_IMAGES": False,
                "TELEGRAM_SEND_GRADUATION_IMAGES": True,
            },
            {
                "kind": "timetable_proposals",
                "baseline_kind": "REGISTERED",
                "baseline_sections": [{"course_code": "AI331"}],
            },
            0,
        ),
        (
            4602,
            {
                "TELEGRAM_SEND_TIMETABLE_IMAGES": True,
                "TELEGRAM_SEND_GRADUATION_IMAGES": False,
            },
            {
                "kind": "graduation_scenario",
                "graph": {"extraNodes": ["AI331"], "items": []},
            },
            None,
        ),
    ],
)
def test_the_other_image_switch_does_not_retry_a_disabled_queued_card(
    update_id: int,
    settings_overrides: dict[str, bool],
    presentation: dict[str, Any],
    option_index: int | None,
) -> None:
    """Each media consent governs its own already-materialised queue recipes."""

    link = _link()
    conversation = AdvisorConversation.objects.create(student_id=link.student_id)
    assistant = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="answer",
        presentation=presentation,
    )
    TelegramUpdateReceipt.objects.create(
        update_id=update_id,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        assistant_message=assistant,
        delivery_payload=jobs._materialised_delivery_payload(
            messages=["answer"],
            delivery_payload=_photo_manifest(_photo(option_index)),
        ),
    )
    delivered: list[str] = []

    with override_settings(**settings_overrides):
        result = jobs.run_job(
            update_id,
            "worker-a",
            deliver_photo=lambda _job, _png: pytest.fail("the disabled card was sent"),
            deliver=lambda _job, text: _record_success(delivered, text),
        )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.error_code == "image_delivery_degraded"
    assert result.delivery_payload == {}
    assert delivered == ["answer"]


def test_photo_manifest_persists_only_a_recipe_and_authoritative_text() -> None:
    link = _link()
    _enqueue(47, link, "question")

    result = jobs.run_job(
        47,
        "worker-a",
        executor=lambda _job: {
            "messages": ["validated answer"],
            "delivery_payload": _photo_manifest(
                {
                    "kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO,
                    "option_index": 0,
                    "url": "https://attacker.example/card",
                    "message_id": "untrusted",
                    "png": "base64-data",
                },
                _text("untrusted duplicate"),
                {"kind": "remote_photo", "url": "https://attacker.example/file"},
            ),
            "result_code": "answered",
        },
        render_photos=lambda _job, _indexes: [],
        deliver=lambda _job, _text: {"ok": True},
        max_attempts=2,
    )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert result.delivery_payload == {
        **_photo_manifest(_photo(0), _text("validated answer")),
        "messages": ["validated answer"],
        "cursor_mode": jobs.DELIVERY_CURSOR_MODE_SPLIT,
        "photo_cursor": 0,
        "photo_attempt_count": 1,
        "text_phase_started": False,
        "text_first": True,
    }
    serialized = str(result.delivery_payload)
    assert "attacker" not in serialized
    assert "message_id" not in serialized
    assert "base64" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        "scalar",
        1,
        True,
        {"version": 99, "items": [_text("answer")]},
        {"version": jobs.DELIVERY_MANIFEST_VERSION, "items": "not-a-list"},
        {"version": jobs.DELIVERY_MANIFEST_VERSION, "items": [{"kind": "unknown"}]},
        _photo_manifest(_text("already sent"), _photo(0), _text("later")),
        _photo_manifest(_photo(0), _photo(0), _text("duplicate")),
        _photo_manifest(_photo(0), _photo(2), _text("gapped")),
        _photo_manifest(_photo(None), _photo(0), _text("mixed baseline and option")),
    ],
)
def test_invalid_materialised_manifest_fails_visibly_without_crashing_the_worker(
    payload: Any,
) -> None:
    link = _link()
    TelegramUpdateReceipt.objects.create(
        update_id=48,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        delivery_payload=payload,
    )

    result = jobs.run_job(
        48,
        "worker-a",
        executor=lambda _job: pytest.fail("corrupt materialised work executed again"),
        deliver=lambda _job, _text: pytest.fail("corrupt materialised work was delivered"),
        deliver_photo=lambda _job, _png: pytest.fail("corrupt photo work was delivered"),
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_FAILED
    assert result.error_code == "invalid_delivery_manifest"


def test_delivery_cursor_past_the_manifest_fails_instead_of_silently_skipping_output() -> None:
    link = _link()
    TelegramUpdateReceipt.objects.create(
        update_id=49,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        result_code="answered",
        delivery_payload=_photo_manifest(_text("unsent answer")),
        delivery_cursor=2,
    )

    result = jobs.run_job(
        49,
        "worker-a",
        deliver=lambda _job, _text: pytest.fail("invalid cursor delivered an item"),
    )

    assert result is not None
    assert result.status == TelegramUpdateReceipt.STATUS_FAILED
    assert result.error_code == "invalid_delivery_manifest"


def test_new_manifest_keeps_validated_text_readable_by_the_previous_worker() -> None:
    """A rollback during deployment must degrade to text, never lose the answer."""

    payload = jobs._materialised_delivery_payload(
        messages=["validated answer", "second chunk"],
        delivery_payload=_photo_manifest(_photo(0), _text("untrusted")),
    )

    # This is the complete read contract of the worker immediately before v2.
    previous_worker_messages = payload.get("messages") or []
    assert previous_worker_messages == ["validated answer", "second chunk"]


def test_manifest_writer_enforces_the_same_total_bound_as_the_reader() -> None:
    supplied = [_photo(i) for i in range(jobs.MAX_TIMETABLE_PHOTOS)]
    messages = [f"chunk-{i}" for i in range(jobs.MAX_DELIVERY_ITEMS + 10)]

    payload = jobs._materialised_delivery_payload(
        messages=messages,
        delivery_payload=_photo_manifest(*supplied),
    )

    assert len(payload["items"]) == jobs.MAX_DELIVERY_ITEMS
    assert jobs._delivery_items(payload) == payload["items"]
    assert payload["messages"] == messages[: jobs.MAX_DELIVERY_ITEMS - len(supplied)]


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


def test_stale_last_attempt_during_photo_skips_images_but_preserves_text() -> None:
    link = _link()
    past = timezone.now() - timedelta(minutes=1)
    job = TelegramUpdateReceipt.objects.create(
        update_id=61,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        attempt_count=2,
        locked_by="dead-image-worker",
        locked_at=past,
        lease_expires_at=past,
        result_code="answered",
        delivery_payload=_photo_manifest(_photo(0), _photo(1), _text("answer")),
    )

    assert jobs.recover_stale_jobs(max_attempts=2) == 1
    job.refresh_from_db()
    assert job.status == TelegramUpdateReceipt.STATUS_QUEUED
    assert job.delivery_cursor == 0
    assert job.delivery_payload["photo_cursor"] == 2
    assert job.attempt_count == 0
    assert job.delivery_payload["image_degraded"] is True

    delivered: list[str] = []
    result = jobs.run_job(
        job.update_id,
        "text-fallback-worker",
        max_attempts=2,
        deliver=lambda _job, text: _record_success(delivered, text),
        deliver_photo=lambda _job, _png: pytest.fail("an exhausted photo was retried"),
    )
    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.error_code == "image_delivery_degraded"
    assert delivered == ["answer"]


def test_already_queued_exhausted_photo_job_gets_the_same_text_fallback() -> None:
    link = _link()
    job = TelegramUpdateReceipt.objects.create(
        update_id=62,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        attempt_count=2,
        result_code="answered",
        delivery_payload=_photo_manifest(_photo(0), _text("answer")),
    )
    delivered: list[str] = []

    result = jobs.run_job(
        job.update_id,
        "text-fallback-worker",
        max_attempts=2,
        deliver=lambda _job, text: _record_success(delivered, text),
        deliver_photo=lambda _job, _png: pytest.fail("an exhausted photo was retried"),
    )

    assert result is not None and result.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert result.error_code == "image_delivery_degraded"
    assert delivered == ["answer"]


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
