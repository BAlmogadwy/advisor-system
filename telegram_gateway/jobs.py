"""Database-backed execution for ordered Telegram adviser work.

The webhook may acknowledge a question only after its ``update_id`` and the work
it represents are durable.  ``TelegramUpdateReceipt`` is therefore both the
idempotency receipt and, for linked questions/commands, the queue envelope.

This module deliberately knows neither how to answer an academic question nor
how commands work.  The executor is injected; production resolves it lazily from
``telegram_gateway.bot.execute_durable_job``.  That keeps imports acyclic and
lets tests exercise queue semantics without a model or Telegram connection.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Callable, Mapping
from datetime import timedelta
from math import ceil
from time import sleep
from typing import Any

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.services.advisor_turn import STALE_GENERATION

from .models import TelegramLink, TelegramUpdateReceipt

# A recovered queue job may resume a stale PENDING AdvisorMessage. Its lease must
# therefore outlive both the shared stale threshold and the configured maximum
# tool loop; otherwise a second worker can claim the turn while the first worker
# is still legitimately inside its bounded model calls.
_MAX_CONFIGURED_TURN_SECONDS = max(
    max(1, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS", 4)))
    * max(1.0, float(getattr(settings, "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS", 75))),
    max(1, int(getattr(settings, "VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS", 5)))
    * max(1.0, float(getattr(settings, "VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS", 75))),
)
MIN_LEASE_SECONDS = ceil(STALE_GENERATION.total_seconds() + _MAX_CONFIGURED_TURN_SECONDS + 60)
DEFAULT_LEASE_SECONDS = max(30 * 60, MIN_LEASE_SECONDS)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_IDLE_SLEEP_SECONDS = 1.0
DEFAULT_MAX_PENDING_PER_LINK = 10
MAX_ERROR_CODE_CHARS = 64

ACTIVE_STATUSES = (
    TelegramUpdateReceipt.STATUS_QUEUED,
    TelegramUpdateReceipt.STATUS_RUNNING,
)
TERMINAL_STATUSES = (
    TelegramUpdateReceipt.STATUS_SUCCEEDED,
    TelegramUpdateReceipt.STATUS_FAILED,
    TelegramUpdateReceipt.STATUS_CANCELLED,
)

Executor = Callable[[TelegramUpdateReceipt], Mapping[str, Any] | None]
Deliver = Callable[[TelegramUpdateReceipt, str], Mapping[str, Any] | bool | None]

_CODE_CHARACTERS = re.compile(r"[^A-Za-z0-9_.:-]+")


class RetryJob(Exception):
    """Ask the durable worker to retry this job after a bounded delay."""

    def __init__(self, error_code: str = "retry_requested", *, delay_seconds: float = 0) -> None:
        super().__init__(error_code)
        self.error_code = _safe_code(error_code, "retry_requested")
        self.delay_seconds = max(0.0, float(delay_seconds or 0))


class PermanentJobError(Exception):
    """Finish this job as failed without retrying it."""

    def __init__(self, error_code: str = "permanent_failure") -> None:
        super().__init__(error_code)
        self.error_code = _safe_code(error_code, "permanent_failure")


class QueueFull(Exception):
    """The link already has the maximum number of unfinished jobs."""


class LinkUnavailable(Exception):
    """The authorising link stopped being active before enqueue completed."""


class AdmissionLimited(Exception):
    """The chat exhausted its durable ingress budget."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("telegram ingress rate limited")
        self.retry_after = max(1, int(retry_after or 1))


def enqueue_question_or_command(
    *,
    update_id: int,
    link: TelegramLink,
    kind: str,
    payload_text: str = "",
    delivery_payload: Mapping[str, Any] | None = None,
    available_at: Any = None,
) -> tuple[TelegramUpdateReceipt, bool]:
    """Atomically claim one Telegram update and enqueue its linked work.

    The return shape follows ``get_or_create``: ``(job, created)``.  A duplicate
    update never overwrites the first payload, even if the repeated body differs.
    """

    parsed_update_id = _update_id(update_id)
    if kind not in {
        TelegramUpdateReceipt.KIND_QUESTION,
        TelegramUpdateReceipt.KIND_COMMAND,
    }:
        raise ValueError("Durable Telegram work must be QUESTION or COMMAND.")
    if not isinstance(link, TelegramLink) or link.pk is None:
        raise ValueError("A persisted TelegramLink is required.")

    rejection: Exception | None = None
    with transaction.atomic():
        # Serialise admission on the exact link. This makes the cap real under
        # concurrent webhook requests instead of a count-then-insert suggestion.
        locked_link = (
            TelegramLink.objects.select_for_update()
            .filter(pk=link.pk, status=TelegramLink.STATUS_ACTIVE)
            .first()
        )
        if locked_link is None:
            raise LinkUnavailable

        existing = TelegramUpdateReceipt.objects.filter(update_id=parsed_update_id).first()
        if existing is not None:
            return existing, False

        from core.services.rate_limit import TELEGRAM_INGRESS
        from core.services.rate_limit import consume as spend_budget

        admission = spend_budget(TELEGRAM_INGRESS, int(locked_link.telegram_user_id))
        if not admission.allowed:
            rejection = AdmissionLimited(admission.retry_after)

        if rejection is None:
            configured_cap = getattr(
                settings,
                "TELEGRAM_MAX_PENDING_PER_LINK",
                DEFAULT_MAX_PENDING_PER_LINK,
            )
            try:
                pending_cap = max(1, int(configured_cap))
            except (TypeError, ValueError):
                pending_cap = DEFAULT_MAX_PENDING_PER_LINK
            pending = TelegramUpdateReceipt.objects.filter(
                link=locked_link,
                status__in=ACTIVE_STATUSES,
            ).count()
            if pending >= pending_cap:
                # Raise only AFTER the atomic block. The ingress unit must commit
                # even though no job is admitted; otherwise a full queue becomes
                # an unlimited database-write and Bot API reply path.
                rejection = QueueFull()

        if rejection is None:
            return (
                TelegramUpdateReceipt.objects.create(
                    update_id=parsed_update_id,
                    link=locked_link,
                    kind=kind,
                    status=TelegramUpdateReceipt.STATUS_QUEUED,
                    payload_text=str(payload_text or ""),
                    delivery_payload=dict(delivery_payload or {}),
                    available_at=available_at or timezone.now(),
                ),
                True,
            )

    if rejection is not None:
        raise rejection
    raise RuntimeError("Telegram queue admission ended without a result.")


def make_job_available(update_id: int) -> bool:
    """Release a newly enqueued job after its progress acknowledgement was attempted.

    Question jobs enter the queue with a short future ``available_at`` so an
    external worker cannot send the final answer before the webhook has sent
    ``WORKING``.  This conditional update is the second phase of that hand-off.
    If the web process dies between the phases, the original future timestamp is
    the fail-safe: the durable job becomes claimable on its own shortly after.
    """

    released = TelegramUpdateReceipt.objects.filter(
        update_id=_update_id(update_id),
        status=TelegramUpdateReceipt.STATUS_QUEUED,
    ).update(available_at=timezone.now())
    return bool(released)


def run_job(
    update_id: int,
    worker_id: str = "telegram-worker",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    executor: Executor | None = None,
    deliver: Deliver | None = None,
) -> TelegramUpdateReceipt | None:
    """Claim and execute one specific queued job, respecting per-link FIFO."""

    job = _claim_specific_job(
        _update_id(update_id),
        worker_id=_worker_id(worker_id),
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    if job is None:
        return TelegramUpdateReceipt.objects.filter(update_id=update_id).first()
    return _execute_claimed_job(job, executor=executor, deliver=deliver, max_attempts=max_attempts)


def run_next_job(
    worker_id: str = "telegram-worker",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    execute: Executor | None = None,
    deliver: Deliver | None = None,
) -> TelegramUpdateReceipt | None:
    """Recover stale work, claim the next FIFO-eligible job, and execute it."""

    worker = _worker_id(worker_id)
    cancel_jobs_for_revoked_links()
    recover_stale_jobs(max_attempts=max_attempts, worker_id=worker)
    job = _claim_next_job(
        worker_id=worker,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    if job is None:
        return None
    return _execute_claimed_job(job, executor=execute, deliver=deliver, max_attempts=max_attempts)


def store_delivery(
    job: TelegramUpdateReceipt,
    *,
    messages: list[str] | tuple[str, ...] = (),
    result_code: str = "completed",
    assistant_message_id: Any = None,
    conversation_id: Any = None,
    delivery_payload: Mapping[str, Any] | None = None,
) -> bool:
    """Persist an executor result under the current lease and discard input text."""

    payload = dict(delivery_payload or {})
    payload["messages"] = [str(message) for message in messages]
    values: dict[str, Any] = {
        "delivery_payload": payload,
        "delivery_cursor": 0,
        "payload_text": "",
        "result_code": _safe_code(result_code, "completed"),
        "error_code": "",
        "lease_expires_at": timezone.now() + _lease_duration(job),
    }
    if assistant_message_id is not None:
        values["assistant_message_id"] = assistant_message_id
    if conversation_id is not None:
        values["conversation_id"] = conversation_id
    updated = _owned_running(job).update(**values)
    if updated:
        for field, value in values.items():
            setattr(job, field, value)
    return bool(updated)


def mark_delivery_progress(job: TelegramUpdateReceipt, cursor: int) -> bool:
    """Advance the durable delivery cursor monotonically under the current lease."""

    next_cursor = max(0, int(cursor))
    lease_expires_at = timezone.now() + _lease_duration(job)
    updated = (
        _owned_running(job)
        .filter(delivery_cursor__lt=next_cursor)
        .update(
            delivery_cursor=next_cursor,
            lease_expires_at=lease_expires_at,
        )
    )
    if updated:
        job.delivery_cursor = next_cursor
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def requeue_delivery(
    job: TelegramUpdateReceipt,
    *,
    error_code: str = "delivery_failed",
    delay_seconds: float | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Requeue a leased job, or fail it once the attempt cap is exhausted."""

    if int(job.attempt_count or 0) >= _max_attempts(max_attempts):
        return finish_job(
            job,
            status=TelegramUpdateReceipt.STATUS_FAILED,
            error_code=error_code,
        )
    delay = (
        _retry_delay(job.attempt_count) if delay_seconds is None else max(0.0, float(delay_seconds))
    )
    values = {
        "status": TelegramUpdateReceipt.STATUS_QUEUED,
        "available_at": timezone.now() + timedelta(seconds=delay),
        "error_code": _safe_code(error_code, "delivery_failed"),
        "locked_by": "",
        "locked_at": None,
        "lease_expires_at": None,
        "finished_at": None,
    }
    updated = _owned_running(job).update(**values)
    if updated:
        for field, value in values.items():
            setattr(job, field, value)
    return bool(updated)


def finish_job(
    job: TelegramUpdateReceipt,
    *,
    status: str = TelegramUpdateReceipt.STATUS_SUCCEEDED,
    result_code: str | None = None,
    error_code: str = "",
    clear_delivery: bool = True,
) -> bool:
    """Finish a job only if this caller still owns its lease."""

    if status not in TERMINAL_STATUSES:
        raise ValueError("finish_job requires a terminal status.")
    values: dict[str, Any] = {
        "status": status,
        "payload_text": "",
        "error_code": _safe_code(error_code, "") if error_code else "",
        "locked_by": "",
        "locked_at": None,
        "lease_expires_at": None,
        "finished_at": timezone.now(),
    }
    if result_code is not None:
        values["result_code"] = _safe_code(result_code, "completed")
    if clear_delivery:
        values["delivery_payload"] = {}
    updated = _owned_running(job).update(**values)
    if updated:
        for field, value in values.items():
            setattr(job, field, value)
    return bool(updated)


def recover_stale_jobs(
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: int = 100,
    worker_id: str = "telegram-worker",
) -> int:
    """Requeue expired leases, failing jobs that exhausted their attempt cap."""

    now = timezone.now()
    stale_ids = list(
        TelegramUpdateReceipt.objects.filter(status=TelegramUpdateReceipt.STATUS_RUNNING)
        .filter(Q(lease_expires_at__lt=now) | Q(lease_expires_at__isnull=True))
        .order_by("lease_expires_at", "update_id")
        .values_list("update_id", flat=True)[: max(1, int(limit or 1))]
    )
    recovered = 0
    for update_id in stale_ids:
        with transaction.atomic():
            qs = TelegramUpdateReceipt.objects.filter(update_id=update_id)
            if connection.features.has_select_for_update:
                qs = qs.select_for_update()
            job = qs.first()
            if (
                job is None
                or job.status != TelegramUpdateReceipt.STATUS_RUNNING
                or (job.lease_expires_at is not None and job.lease_expires_at >= timezone.now())
            ):
                continue
            now = timezone.now()
            active_link = job.link is not None and job.link.status == TelegramLink.STATUS_ACTIVE
            if not active_link:
                job.status = TelegramUpdateReceipt.STATUS_CANCELLED
                job.error_code = "link_revoked"
                job.payload_text = ""
                job.delivery_payload = {}
                job.finished_at = now
            elif int(job.attempt_count or 0) >= _max_attempts(max_attempts):
                job.status = TelegramUpdateReceipt.STATUS_FAILED
                job.error_code = "lease_expired_max_attempts"
                job.payload_text = ""
                job.delivery_payload = {}
                job.finished_at = now
            else:
                job.status = TelegramUpdateReceipt.STATUS_QUEUED
                job.error_code = "lease_recovered"
                job.available_at = now
                job.finished_at = None
            job.locked_by = ""
            job.locked_at = None
            job.lease_expires_at = None
            job.save(
                update_fields=[
                    "status",
                    "error_code",
                    "payload_text",
                    "delivery_payload",
                    "available_at",
                    "finished_at",
                    "locked_by",
                    "locked_at",
                    "lease_expires_at",
                ]
            )
            recovered += 1
    return recovered


def cancel_jobs_for_revoked_links() -> int:
    """Cancel queued/running work whose exact authorising link is no longer active."""

    now = timezone.now()
    return (
        TelegramUpdateReceipt.objects.filter(status__in=ACTIVE_STATUSES)
        .filter(Q(link__isnull=True) | ~Q(link__status=TelegramLink.STATUS_ACTIVE))
        .update(
            status=TelegramUpdateReceipt.STATUS_CANCELLED,
            payload_text="",
            delivery_payload={},
            error_code="link_revoked",
            locked_by="",
            locked_at=None,
            lease_expires_at=None,
            finished_at=now,
        )
    )


def run_worker_loop(
    *,
    worker_id: str = "",
    once: bool = False,
    idle_sleep_seconds: float = DEFAULT_IDLE_SLEEP_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    execute: Executor | None = None,
    deliver: Deliver | None = None,
) -> int:
    """Poll and execute durable jobs until stopped; return the claimed-job count."""

    worker = _worker_id(worker_id or f"telegram-worker@{socket.gethostname()}")
    executed = 0
    while True:
        close_old_connections()
        try:
            job = run_next_job(
                worker,
                lease_seconds,
                max_attempts,
                execute=execute,
                deliver=deliver,
            )
        finally:
            close_old_connections()
        if job is None:
            if once:
                return executed
            sleep(max(0.1, float(idle_sleep_seconds)))
            continue
        executed += 1
        if once:
            return executed


def _claim_next_job(
    *, worker_id: str, lease_seconds: int, max_attempts: int
) -> TelegramUpdateReceipt | None:
    now = timezone.now()
    earlier = TelegramUpdateReceipt.objects.filter(
        link_id=OuterRef("link_id"),
        update_id__lt=OuterRef("update_id"),
        status__in=ACTIVE_STATUSES,
    )
    with transaction.atomic():
        qs = (
            TelegramUpdateReceipt.objects.filter(
                status=TelegramUpdateReceipt.STATUS_QUEUED,
                available_at__lte=now,
                link__status=TelegramLink.STATUS_ACTIVE,
                attempt_count__lt=_max_attempts(max_attempts),
            )
            .annotate(has_earlier_active=Exists(earlier))
            .filter(has_earlier_active=False)
            .order_by("update_id")
        )
        if connection.features.has_select_for_update:
            qs = qs.select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked
            )
        job = qs.first()
        if job is None:
            _fail_exhausted_queued_jobs(max_attempts=max_attempts)
            return None
        return _claim_locked(job, worker_id=worker_id, lease_seconds=lease_seconds)


def _claim_specific_job(
    update_id: int, *, worker_id: str, lease_seconds: int, max_attempts: int
) -> TelegramUpdateReceipt | None:
    with transaction.atomic():
        qs = TelegramUpdateReceipt.objects.filter(update_id=update_id)
        if connection.features.has_select_for_update:
            qs = qs.select_for_update()
        job = qs.first()
        if job is None or job.status != TelegramUpdateReceipt.STATUS_QUEUED:
            return None
        now = timezone.now()
        if job.available_at > now:
            return None
        if job.link is None or job.link.status != TelegramLink.STATUS_ACTIVE:
            _cancel_locked(job, "link_revoked")
            return None
        if int(job.attempt_count or 0) >= _max_attempts(max_attempts):
            _fail_locked(job, "max_attempts_exhausted")
            return None
        if TelegramUpdateReceipt.objects.filter(
            link_id=job.link_id,
            update_id__lt=job.update_id,
            status__in=ACTIVE_STATUSES,
        ).exists():
            return None
        return _claim_locked(job, worker_id=worker_id, lease_seconds=lease_seconds)


def _claim_locked(
    job: TelegramUpdateReceipt, *, worker_id: str, lease_seconds: int
) -> TelegramUpdateReceipt:
    now = timezone.now()
    job.status = TelegramUpdateReceipt.STATUS_RUNNING
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.locked_by = worker_id
    job.locked_at = now
    job.lease_expires_at = now + timedelta(
        seconds=max(MIN_LEASE_SECONDS, int(lease_seconds or MIN_LEASE_SECONDS))
    )
    job.finished_at = None
    job.save(
        update_fields=[
            "status",
            "attempt_count",
            "locked_by",
            "locked_at",
            "lease_expires_at",
            "finished_at",
        ]
    )
    return job


def _execute_claimed_job(
    job: TelegramUpdateReceipt,
    *,
    executor: Executor | None,
    deliver: Deliver | None,
    max_attempts: int,
) -> TelegramUpdateReceipt:
    if not _link_is_active(job):
        finish_job(
            job,
            status=TelegramUpdateReceipt.STATUS_CANCELLED,
            error_code="link_revoked",
        )
        return _fresh(job)

    # A delivery already materialised before a worker died. Resume it directly;
    # the adviser/command side effect must not run again.
    materialised = bool(job.result_code or job.delivery_payload)
    if not materialised:
        execute = executor or _default_executor
        try:
            raw_result = execute(job)
        except RetryJob as exc:
            requeue_delivery(
                job,
                error_code=exc.error_code,
                delay_seconds=exc.delay_seconds,
                max_attempts=max_attempts,
            )
            return _fresh(job)
        except PermanentJobError as exc:
            finish_job(
                job,
                status=TelegramUpdateReceipt.STATUS_FAILED,
                error_code=exc.error_code,
            )
            return _fresh(job)
        except Exception as exc:  # noqa: BLE001 - only the exception CLASS becomes durable.
            requeue_delivery(
                job,
                error_code=type(exc).__name__,
                max_attempts=max_attempts,
            )
            return _fresh(job)

        # A command with a transactional side effect may have materialised its
        # delivery inside the same transaction. Refresh before writing the
        # ordinary executor result so that atomic marker is never overwritten.
        job.refresh_from_db()
        if not (job.result_code or job.delivery_payload):
            result = dict(raw_result or {})
            messages = result.pop("messages", ()) or ()
            if isinstance(messages, str):
                messages = (messages,)
            extra_payload = result.pop("delivery_payload", {}) or {}
            if not store_delivery(
                job,
                messages=tuple(str(message) for message in messages),
                result_code=str(result.pop("result_code", "completed") or "completed"),
                assistant_message_id=result.pop("assistant_message_id", None),
                conversation_id=result.pop("conversation_id", None),
                delivery_payload={**dict(extra_payload), **result},
            ):
                return _fresh(job)

    messages = list((job.delivery_payload or {}).get("messages") or [])
    if not messages:
        finish_job(job, result_code=job.result_code or "completed")
        return _fresh(job)

    send = deliver or _default_deliver
    for index in range(int(job.delivery_cursor or 0), len(messages)):
        if not _link_is_active(job):
            finish_job(
                job,
                status=TelegramUpdateReceipt.STATUS_CANCELLED,
                error_code="link_revoked",
            )
            return _fresh(job)
        try:
            outcome = send(job, str(messages[index]))
        except Exception as exc:  # noqa: BLE001 - only the exception CLASS becomes durable.
            outcome = {"ok": False, "error": type(exc).__name__}
        if not _delivery_succeeded(outcome):
            error_code, retryable, delay = _delivery_failure(outcome)
            if retryable:
                requeue_delivery(
                    job,
                    error_code=error_code,
                    delay_seconds=delay,
                    max_attempts=max_attempts,
                )
            else:
                finish_job(
                    job,
                    status=TelegramUpdateReceipt.STATUS_FAILED,
                    error_code=error_code,
                )
            return _fresh(job)
        if not mark_delivery_progress(job, index + 1):
            return _fresh(job)

    finish_job(job, result_code=job.result_code or "completed")
    return _fresh(job)


def _default_executor(job: TelegramUpdateReceipt) -> Mapping[str, Any] | None:
    from .bot import execute_durable_job

    return execute_durable_job(job)


def _default_deliver(job: TelegramUpdateReceipt, text: str) -> Mapping[str, Any] | bool | None:
    from . import linking
    from .transport import send_text

    link = linking.active_link_by_id(job.link_id)
    if link is None:
        return {"ok": False, "error": "link_revoked", "permanent": True}
    return send_text(chat_id=int(link.telegram_user_id), text=text)


def _delivery_succeeded(outcome: Mapping[str, Any] | bool | None) -> bool:
    if outcome is None:
        return True
    if isinstance(outcome, bool):
        return outcome
    return bool(outcome.get("ok"))


def _delivery_failure(
    outcome: Mapping[str, Any] | bool | None,
) -> tuple[str, bool, float | None]:
    if not isinstance(outcome, Mapping):
        return "delivery_failed", True, None
    code = _safe_code(outcome.get("error") or outcome.get("reason"), "delivery_failed")
    status = outcome.get("status")
    try:
        http_status = int(status) if status is not None else 0
    except (TypeError, ValueError):
        http_status = 0
    permanent = bool(outcome.get("permanent")) or (400 <= http_status < 500 and http_status != 429)
    retry_after = outcome.get("retry_after")
    delay = None
    if retry_after is not None:
        try:
            delay = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            delay = None
    return code, not permanent, delay


def _link_is_active(job: TelegramUpdateReceipt) -> bool:
    if not job.link_id:
        return False
    from . import linking

    return linking.active_link_by_id(job.link_id) is not None


def _owned_running(job: TelegramUpdateReceipt):
    if not job.locked_by or job.locked_at is None:
        return TelegramUpdateReceipt.objects.none()
    return TelegramUpdateReceipt.objects.filter(
        update_id=job.update_id,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        locked_by=job.locked_by,
        locked_at=job.locked_at,
    )


def _lease_duration(job: TelegramUpdateReceipt) -> timedelta:
    if job.locked_at is not None and job.lease_expires_at is not None:
        duration = job.lease_expires_at - job.locked_at
        if duration.total_seconds() > 0:
            return duration
    return timedelta(seconds=DEFAULT_LEASE_SECONDS)


def _cancel_locked(job: TelegramUpdateReceipt, error_code: str) -> None:
    job.status = TelegramUpdateReceipt.STATUS_CANCELLED
    job.payload_text = ""
    job.delivery_payload = {}
    job.error_code = _safe_code(error_code, "cancelled")
    job.locked_by = ""
    job.locked_at = None
    job.lease_expires_at = None
    job.finished_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "payload_text",
            "delivery_payload",
            "error_code",
            "locked_by",
            "locked_at",
            "lease_expires_at",
            "finished_at",
        ]
    )


def _fail_locked(job: TelegramUpdateReceipt, error_code: str) -> None:
    job.status = TelegramUpdateReceipt.STATUS_FAILED
    job.payload_text = ""
    job.delivery_payload = {}
    job.error_code = _safe_code(error_code, "failed")
    job.locked_by = ""
    job.locked_at = None
    job.lease_expires_at = None
    job.finished_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "payload_text",
            "delivery_payload",
            "error_code",
            "locked_by",
            "locked_at",
            "lease_expires_at",
            "finished_at",
        ]
    )


def _fail_exhausted_queued_jobs(*, max_attempts: int) -> int:
    now = timezone.now()
    return TelegramUpdateReceipt.objects.filter(
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        attempt_count__gte=_max_attempts(max_attempts),
    ).update(
        status=TelegramUpdateReceipt.STATUS_FAILED,
        payload_text="",
        delivery_payload={},
        error_code="max_attempts_exhausted",
        finished_at=now,
    )


def _fresh(job: TelegramUpdateReceipt) -> TelegramUpdateReceipt:
    return TelegramUpdateReceipt.objects.select_related(
        "link", "conversation", "assistant_message"
    ).get(update_id=job.update_id)


def _retry_delay(attempt_count: int) -> float:
    return float(min(300, 5 * (2 ** max(0, int(attempt_count or 1) - 1))))


def _safe_code(raw: Any, default: str) -> str:
    clean = _CODE_CHARACTERS.sub("_", str(raw or "").strip())
    return (clean or default)[:MAX_ERROR_CODE_CHARS]


def _update_id(raw: Any) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError("Telegram update_id must be an integer.")
    return int(raw)


def _worker_id(raw: Any) -> str:
    return str(raw or "telegram-worker").strip()[:128] or "telegram-worker"


def _max_attempts(raw: Any) -> int:
    return max(1, int(raw or DEFAULT_MAX_ATTEMPTS))


__all__ = [
    "ACTIVE_STATUSES",
    "AdmissionLimited",
    "DEFAULT_IDLE_SLEEP_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_PENDING_PER_LINK",
    "DEFAULT_MAX_ATTEMPTS",
    "LinkUnavailable",
    "MIN_LEASE_SECONDS",
    "PermanentJobError",
    "QueueFull",
    "RetryJob",
    "TERMINAL_STATUSES",
    "cancel_jobs_for_revoked_links",
    "enqueue_question_or_command",
    "finish_job",
    "mark_delivery_progress",
    "make_job_available",
    "recover_stale_jobs",
    "requeue_delivery",
    "run_job",
    "run_next_job",
    "run_worker_loop",
    "store_delivery",
]
