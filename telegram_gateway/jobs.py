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

import logging
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from math import ceil
from time import sleep
from typing import Any, NamedTuple

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from core.services.advisor_turn import STALE_GENERATION

from .models import TelegramLink, TelegramUpdateReceipt

logger = logging.getLogger(__name__)

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
DELIVERY_MANIFEST_VERSION = 2
DELIVERY_KIND_TEXT = "text"
# Kept as the v2 wire name for rollback compatibility. It now means one
# server-authored adviser-card recipe: timetable alternatives use indices and a
# baseline/graduation card uses the single ``None`` recipe.
DELIVERY_KIND_TIMETABLE_PHOTO = "timetable_photo"
MAX_DELIVERY_ITEMS = 64
MAX_TIMETABLE_PHOTOS = 4
DELIVERY_CURSOR_MODE_SPLIT = "split"

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
PhotoDeliver = Callable[[TelegramUpdateReceipt, bytes], Mapping[str, Any] | bool | None]
PhotoRender = Callable[
    [TelegramUpdateReceipt, Sequence[int | None]],
    Sequence[bytes | None],
]

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


class _DeliveryState(NamedTuple):
    photo_items: list[dict[str, Any]]
    text_items: list[dict[str, Any]]
    photo_cursor: int
    photo_attempt_count: int
    text_cursor: int
    text_phase_started: bool
    legacy_shared_cursor: bool
    rollback_text_takeover: bool
    text_first: bool


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
    render_photos: PhotoRender | None = None,
    deliver_photo: PhotoDeliver | None = None,
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
    return _execute_claimed_job(
        job,
        executor=executor,
        deliver=deliver,
        render_photos=render_photos,
        deliver_photo=deliver_photo,
        max_attempts=max_attempts,
    )


def run_next_job(
    worker_id: str = "telegram-worker",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    execute: Executor | None = None,
    deliver: Deliver | None = None,
    render_photos: PhotoRender | None = None,
    deliver_photo: PhotoDeliver | None = None,
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
    return _execute_claimed_job(
        job,
        executor=execute,
        deliver=deliver,
        render_photos=render_photos,
        deliver_photo=deliver_photo,
        max_attempts=max_attempts,
    )


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

    payload = _materialised_delivery_payload(
        messages=messages,
        delivery_payload=delivery_payload,
    )
    values: dict[str, Any] = {
        "delivery_payload": payload,
        "delivery_cursor": 0,
        "payload_text": "",
        "result_code": _safe_code(result_code, "completed"),
        "error_code": "",
        "lease_expires_at": timezone.now() + _lease_duration(job),
    }
    if any(item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO for item in payload["items"]) and not bool(
        payload.get("text_first")
    ):
        # Retries now belong to the optional photo phase, not adviser
        # generation. Keep the rollback-visible counter below its terminal
        # boundary so a previous worker can still drain `messages`.
        values["attempt_count"] = 0
    else:
        # Generation and delivery have independent budgets. The currently
        # leased send is text attempt one even when generation succeeded only
        # on its final retry.
        values["attempt_count"] = 1
    if assistant_message_id is not None:
        values["assistant_message_id"] = assistant_message_id
    if conversation_id is not None:
        values["conversation_id"] = conversation_id
    updated = _owned_running(job).update(**values)
    if updated:
        for field, value in values.items():
            setattr(job, field, value)
    return bool(updated)


def _materialised_delivery_payload(
    *,
    messages: list[str] | tuple[str, ...],
    delivery_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the only durable outbound shape accepted by new workers.

    Adviser-card pictures are recipes, never bytes, URLs, chat ids or message ids.
    The source message is the job's trusted foreign key and the renderer mints a
    short-lived card token only when the leased worker is ready to draw it.
    """

    raw = dict(delivery_payload) if isinstance(delivery_payload, Mapping) else {}
    supplied_items = raw.get("items")
    if isinstance(supplied_items, list):
        supplied = _normalise_delivery_items(supplied_items)
        photos = [item for item in supplied if item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO]
        supplied_text = [item for item in supplied if item["kind"] == DELIVERY_KIND_TEXT]
        authoritative_text = [
            {"kind": DELIVERY_KIND_TEXT, "text": str(message)}
            for message in messages
            if str(message)
        ]
        text_items = authoritative_text or supplied_text
        # A photo is only an enhancement to a validated answer. Never persist a
        # photo-only job, and apply the same total bound here that the reader
        # enforces so a producer cannot write a manifest it later rejects.
        if not text_items:
            photos = []
        # Keep the physical v2 layout readable by the previous photo-first
        # worker during a rolling deploy. New workers follow the explicit
        # ``text_first`` bit rather than treating list order as send order.
        items = photos + text_items[: max(0, MAX_DELIVERY_ITEMS - len(photos))]
    else:
        items = [{"kind": DELIVERY_KIND_TEXT, "text": str(message)} for message in messages][
            :MAX_DELIVERY_ITEMS
        ]
    # Keep the legacy key for one rollback window. A pre-v2 worker reads only
    # ``messages``; without this bridge, rolling back while a v2 row is queued
    # would mark it successful without sending its validated text.
    legacy_messages = [item["text"] for item in items if item["kind"] == DELIVERY_KIND_TEXT]
    return {
        "version": DELIVERY_MANIFEST_VERSION,
        # Keep the database delivery_cursor aligned with the legacy ``messages``
        # list at all times. Photo progress lives separately in this JSON, so a
        # rollback after any confirmed image still lets the previous worker send
        # every unconfirmed text chunk.
        "cursor_mode": DELIVERY_CURSOR_MODE_SPLIT,
        "photo_cursor": 0,
        "photo_attempt_count": 0,
        "text_phase_started": not bool(
            any(item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO for item in items)
        ),
        "text_first": True,
        "items": items,
        "messages": legacy_messages,
    }


def _delivery_items(payload: Any) -> list[dict[str, Any]] | None:
    """Read a typed manifest, with compatibility for already-queued text jobs."""

    if payload in (None, {}):
        return []
    if not isinstance(payload, Mapping):
        return None
    raw = dict(payload)
    if "version" in raw or "items" in raw:
        raw_items = raw.get("items")
        if raw.get("version") != DELIVERY_MANIFEST_VERSION or not isinstance(raw_items, list):
            return None
        items = _normalise_delivery_items(raw_items)
        if len(items) != len(raw_items):
            return None
        previous_kind = ""
        transitions = 0
        for item in items:
            kind = item["kind"]
            if previous_kind and kind != previous_kind:
                transitions += 1
            previous_kind = kind
        if transitions > 1:
            return None
        if (
            items
            and items[0]["kind"] == DELIVERY_KIND_TEXT
            and any(item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO for item in items)
            and raw.get("text_first") is not True
        ):
            # A text-first physical layout needs the explicit ordering bit;
            # otherwise older manifests would acquire ambiguous cursor meaning.
            return None
        if any(item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO for item in items) and not any(
            item["kind"] == DELIVERY_KIND_TEXT for item in items
        ):
            return None
        photo_indexes = [
            item["option_index"] for item in items if item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO
        ]
        if photo_indexes:
            expected_indexes: list[int | None]
            if photo_indexes == [None]:
                expected_indexes = [None]
            else:
                expected_indexes = list(range(len(photo_indexes)))
            if photo_indexes != expected_indexes:
                # Server-authored manifests are either one baseline card or the
                # ordered alternatives 0..N. Duplicate/gapped values indicate a
                # corrupted row and must not become duplicate/wrong photos.
                return None
        return items

    # Rows materialised by the first durable rollout used ``messages``. They may
    # still be waiting during a rolling deploy, so they must drain as text rather
    # than being mistaken for empty completed work.
    legacy = raw.get("messages")
    if not isinstance(legacy, list):
        return None
    return [
        {"kind": DELIVERY_KIND_TEXT, "text": str(message)}
        for message in legacy[:MAX_DELIVERY_ITEMS]
    ]


def _normalise_delivery_items(raw_items: Sequence[Any]) -> list[dict[str, Any]]:
    """Whitelist delivery items so persisted JSON can never choose a fetch URL."""

    items: list[dict[str, Any]] = []
    photo_count = 0
    for raw in raw_items:
        if len(items) >= MAX_DELIVERY_ITEMS or not isinstance(raw, Mapping):
            continue
        kind = raw.get("kind")
        if kind == DELIVERY_KIND_TEXT:
            text = raw.get("text")
            if isinstance(text, str) and text:
                items.append({"kind": DELIVERY_KIND_TEXT, "text": text})
            continue
        if kind != DELIVERY_KIND_TIMETABLE_PHOTO or photo_count >= MAX_TIMETABLE_PHOTOS:
            continue
        option_index = raw.get("option_index")
        if option_index is not None and (
            isinstance(option_index, bool)
            or not isinstance(option_index, int)
            or not 0 <= option_index < MAX_TIMETABLE_PHOTOS
        ):
            continue
        items.append(
            {
                "kind": DELIVERY_KIND_TIMETABLE_PHOTO,
                "option_index": option_index,
            }
        )
        photo_count += 1
    return items


def _delivery_state(
    payload: Any,
    items: list[dict[str, Any]],
    delivery_cursor: Any,
) -> _DeliveryState | None:
    """Validate split photo/text progress, including pre-split v2 queue rows."""

    try:
        text_cursor = int(delivery_cursor or 0)
    except (TypeError, ValueError):
        return None
    if text_cursor < 0 or not isinstance(payload, Mapping):
        return None

    photo_items = [item for item in items if item["kind"] == DELIVERY_KIND_TIMETABLE_PHOTO]
    text_items = [item for item in items if item["kind"] == DELIVERY_KIND_TEXT]
    if not photo_items:
        if text_cursor > len(text_items):
            return None
        return _DeliveryState([], text_items, 0, 0, text_cursor, True, False, False, True)

    raw = dict(payload)
    mode = raw.get("cursor_mode")
    if mode is None:
        # Compatibility with v2 rows materialised before the cursor was split.
        # Their DB cursor counted [photos..., text...]. Normalise them under the
        # lease before any new send; newly written rows always declare `split`.
        if "photo_cursor" in raw or "photo_attempt_count" in raw or "text_phase_started" in raw:
            return None
        if text_cursor <= len(photo_items):
            photo_cursor = text_cursor
            text_cursor = 0
            text_phase_started = False
        elif text_cursor <= len(items):
            photo_cursor = len(photo_items)
            text_cursor -= len(photo_items)
            text_phase_started = True
        else:
            return None
        if text_cursor > len(text_items):
            return None
        return _DeliveryState(
            photo_items,
            text_items,
            photo_cursor,
            0,
            text_cursor,
            text_phase_started,
            True,
            False,
            False,
        )
    if mode != DELIVERY_CURSOR_MODE_SPLIT:
        return None

    photo_cursor = raw.get("photo_cursor", 0)
    photo_attempt_count = raw.get("photo_attempt_count", 0)
    text_phase_started = raw.get("text_phase_started", False)
    text_first = raw.get("text_first", False)
    if (
        isinstance(photo_cursor, bool)
        or not isinstance(photo_cursor, int)
        or not 0 <= photo_cursor <= len(photo_items)
        or isinstance(photo_attempt_count, bool)
        or not isinstance(photo_attempt_count, int)
        or photo_attempt_count < 0
        or not isinstance(text_phase_started, bool)
        or not isinstance(text_first, bool)
        or text_cursor > len(text_items)
    ):
        return None
    rollback_text_takeover = not text_first and not text_phase_started and text_cursor > 0
    return _DeliveryState(
        photo_items,
        text_items,
        photo_cursor,
        photo_attempt_count,
        text_cursor,
        text_phase_started or rollback_text_takeover,
        False,
        rollback_text_takeover,
        text_first,
    )


def _persist_split_delivery_state(job: TelegramUpdateReceipt, state: _DeliveryState) -> bool:
    """Convert an already-queued shared-cursor v2 row under its current lease."""

    payload = dict(job.delivery_payload or {})
    payload["cursor_mode"] = DELIVERY_CURSOR_MODE_SPLIT
    payload["photo_cursor"] = state.photo_cursor
    payload["photo_attempt_count"] = state.photo_attempt_count
    payload["text_phase_started"] = state.text_phase_started
    lease_expires_at = timezone.now() + _lease_duration(job)
    values = {
        "delivery_payload": payload,
        "delivery_cursor": state.text_cursor,
        "lease_expires_at": lease_expires_at,
    }
    updated = _owned_running(job).update(**values)
    if updated:
        job.delivery_payload = payload
        job.delivery_cursor = state.text_cursor
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def _begin_text_after_skipping_photos(
    job: TelegramUpdateReceipt,
    state: _DeliveryState,
) -> bool:
    """Atomically abandon remaining photos and start required text delivery.

    A rolled-back text-only worker may already have consumed delivery attempts.
    Preserve the claimed counter in that takeover case; only a genuine photo
    phase receives a fresh first text attempt.
    """

    payload = dict(job.delivery_payload or {})
    skipped_photos = state.photo_cursor < len(state.photo_items)
    payload["cursor_mode"] = DELIVERY_CURSOR_MODE_SPLIT
    payload["photo_cursor"] = len(state.photo_items)
    payload["photo_attempt_count"] = state.photo_attempt_count
    payload["text_phase_started"] = True
    if skipped_photos:
        payload["image_degraded"] = True
    lease_expires_at = timezone.now() + _lease_duration(job)
    text_attempt_count = int(job.attempt_count or 0) if state.rollback_text_takeover else 1
    values = {
        "delivery_payload": payload,
        "delivery_cursor": state.text_cursor,
        "attempt_count": text_attempt_count,
        "lease_expires_at": lease_expires_at,
    }
    updated = _owned_running(job).update(**values)
    if updated:
        job.delivery_payload = payload
        job.delivery_cursor = state.text_cursor
        job.attempt_count = text_attempt_count
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def _begin_photo_delivery_attempt(
    job: TelegramUpdateReceipt,
    state: _DeliveryState,
) -> bool:
    """Count one optional-image attempt without consuming the legacy counter.

    ``attempt_count`` predates image delivery and is the only budget a rolled-
    back worker understands. Keeping it at zero throughout Chromium/sendPhoto
    work guarantees that a crash can still roll back to a text-delivery attempt.
    The durable JSON counter independently bounds image retries.
    """

    payload = dict(job.delivery_payload or {})
    if payload.get("cursor_mode") != DELIVERY_CURSOR_MODE_SPLIT:
        return False
    # Old split manifests used ``text_phase_started`` to mean photos were already
    # finished. New text-first manifests mark it true from creation and carry an
    # explicit ordering bit, so their post-text photo phase remains valid.
    if payload.get("text_phase_started") is True and payload.get("text_first") is not True:
        return False
    payload["photo_attempt_count"] = state.photo_attempt_count + 1
    lease_expires_at = timezone.now() + _lease_duration(job)
    values = {
        "delivery_payload": payload,
        "attempt_count": 0,
        "lease_expires_at": lease_expires_at,
    }
    updated = _owned_running(job).update(**values)
    if updated:
        job.delivery_payload = payload
        job.attempt_count = 0
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def mark_photo_progress(
    job: TelegramUpdateReceipt,
    cursor: int,
    *,
    image_degraded: bool = False,
) -> bool:
    """Advance the photo-only cursor without changing legacy text progress."""

    payload = dict(job.delivery_payload or {})
    if payload.get("cursor_mode") != DELIVERY_CURSOR_MODE_SPLIT:
        return False
    current = payload.get("photo_cursor", 0)
    next_cursor = int(cursor)
    if isinstance(current, bool) or not isinstance(current, int) or next_cursor <= current:
        return False
    payload["photo_cursor"] = next_cursor
    if image_degraded:
        payload["image_degraded"] = True
    lease_expires_at = timezone.now() + _lease_duration(job)
    updated = _owned_running(job).update(
        delivery_payload=payload,
        lease_expires_at=lease_expires_at,
    )
    if updated:
        job.delivery_payload = payload
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def _requeue_photo_delivery(
    job: TelegramUpdateReceipt,
    *,
    photo_attempt_count: int,
    error_code: str,
    delay_seconds: float | None = None,
) -> bool:
    """Requeue optional-image work while preserving its separate retry count."""

    delay = (
        _retry_delay(photo_attempt_count)
        if delay_seconds is None
        else max(0.0, float(delay_seconds))
    )
    values = {
        "status": TelegramUpdateReceipt.STATUS_QUEUED,
        # Required text is already confirmed (text-first manifests) or has not
        # started yet (legacy photo-first manifests). In both cases optional
        # images own a fresh, separately persisted retry counter.
        "attempt_count": 0,
        "available_at": timezone.now() + timedelta(seconds=delay),
        "error_code": _safe_code(error_code, "image_delivery_failed"),
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


def _begin_text_delivery(job: TelegramUpdateReceipt, *, photo_count: int) -> bool:
    """Start required text with a fresh bounded attempt budget exactly once."""

    payload = dict(job.delivery_payload or {})
    if payload.get("cursor_mode") != DELIVERY_CURSOR_MODE_SPLIT:
        return False
    if payload.get("text_phase_started") is True:
        return True
    if payload.get("photo_cursor") != photo_count:
        return False
    payload["text_phase_started"] = True
    lease_expires_at = timezone.now() + _lease_duration(job)
    values = {
        "delivery_payload": payload,
        # This running claim is text attempt one. Image retries no longer consume
        # the budget that protects delivery of the validated answer.
        "attempt_count": 1,
        "lease_expires_at": lease_expires_at,
    }
    updated = _owned_running(job).update(**values)
    if updated:
        job.delivery_payload = payload
        job.attempt_count = 1
        job.lease_expires_at = lease_expires_at
    return bool(updated)


def mark_delivery_progress(
    job: TelegramUpdateReceipt,
    cursor: int,
    *,
    reset_attempt_count: bool = False,
) -> bool:
    """Advance the legacy-compatible text cursor under the current lease."""

    next_cursor = max(0, int(cursor))
    lease_expires_at = timezone.now() + _lease_duration(job)
    values: dict[str, Any] = {
        "delivery_cursor": next_cursor,
        "lease_expires_at": lease_expires_at,
    }
    if reset_attempt_count:
        # The validated text is now fully confirmed. Optional card delivery owns
        # a separate budget, and the cursor makes this reset safe for a rolling
        # rollback: an older worker sees no remaining text to resend.
        values["attempt_count"] = 0
    updated = _owned_running(job).filter(delivery_cursor__lt=next_cursor).update(**values)
    if updated:
        for field, value in values.items():
            setattr(job, field, value)
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
            elif _photo_attempt_budget_exhausted(job, max_attempts=max_attempts):
                if _degrade_exhausted_photo_phase_locked(job, now=now):
                    recovered += 1
                    continue
                job.status = TelegramUpdateReceipt.STATUS_FAILED
                job.error_code = "invalid_delivery_manifest"
                job.payload_text = ""
                job.delivery_payload = {}
                job.finished_at = now
            elif int(job.attempt_count or 0) >= _max_attempts(max_attempts):
                if _degrade_exhausted_photo_phase_locked(job, now=now):
                    recovered += 1
                    continue
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
    render_photos: PhotoRender | None = None,
    deliver_photo: PhotoDeliver | None = None,
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
                render_photos=render_photos,
                deliver_photo=deliver_photo,
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
        if _photo_attempt_budget_exhausted(job, max_attempts=max_attempts):
            _degrade_exhausted_photo_phase_locked(job, now=now)
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
        if _photo_attempt_budget_exhausted(job, max_attempts=max_attempts):
            if not _degrade_exhausted_photo_phase_locked(job, now=now):
                _fail_locked(job, "invalid_delivery_manifest")
                return None
        if int(job.attempt_count or 0) >= _max_attempts(max_attempts):
            if not _degrade_exhausted_photo_phase_locked(job, now=now):
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
    # Materialised photo work has its own durable retry counter. Make the claim
    # itself rollback-safe (including max_attempts=1): there must be no committed
    # claim-to-render window in which an old worker sees a terminal DB counter.
    job.attempt_count = (
        0 if _pending_photo_delivery_state(job) is not None else int(job.attempt_count or 0) + 1
    )
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
    render_photos: PhotoRender | None,
    deliver_photo: PhotoDeliver | None,
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
            if not isinstance(extra_payload, Mapping):
                extra_payload = {}
            if not store_delivery(
                job,
                messages=tuple(str(message) for message in messages),
                result_code=str(result.pop("result_code", "completed") or "completed"),
                assistant_message_id=result.pop("assistant_message_id", None),
                conversation_id=result.pop("conversation_id", None),
                delivery_payload={**dict(extra_payload), **result},
            ):
                return _fresh(job)

    items = _delivery_items(job.delivery_payload)
    state = (
        _delivery_state(job.delivery_payload, items, job.delivery_cursor)
        if items is not None
        else None
    )
    if items is None or state is None:
        finish_job(
            job,
            status=TelegramUpdateReceipt.STATUS_FAILED,
            error_code="invalid_delivery_manifest",
        )
        return _fresh(job)
    if not items:
        finish_job(job, result_code=job.result_code or "completed")
        return _fresh(job)
    if state.rollback_text_takeover:
        if not _begin_text_after_skipping_photos(job, state):
            return _fresh(job)
        state = state._replace(
            photo_cursor=len(state.photo_items),
            text_phase_started=True,
            rollback_text_takeover=False,
        )
    elif state.legacy_shared_cursor:
        if not _persist_split_delivery_state(job, state):
            return _fresh(job)
        state = state._replace(legacy_shared_cursor=False)

    send_text_item = deliver or _default_deliver
    send_photo_item = deliver_photo or _default_deliver_photo
    render_photo_items = render_photos or _default_render_photos
    rendered_photos: dict[int, bytes | None] = {}
    # Test callers may inject a renderer explicitly. Production must instead
    # consult the switch for THIS stored presentation, not the union of both
    # media switches: enabling graduation maps must not make an already-queued
    # timetable spend three doomed render retries (and vice versa).
    images_enabled = render_photos is not None or _job_presentation_images_enabled(job)

    if state.text_first:
        # Required text always goes first in newly materialised manifests. It owns
        # the legacy DB cursor and retry budget, so a transient failure pauses
        # before any optional media can escape.
        for text_index in range(state.text_cursor, len(state.text_items)):
            if not _link_is_active(job):
                finish_job(
                    job,
                    status=TelegramUpdateReceipt.STATUS_CANCELLED,
                    error_code="link_revoked",
                )
                return _fresh(job)
            try:
                outcome = send_text_item(job, state.text_items[text_index]["text"])
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
            if not mark_delivery_progress(
                job,
                text_index + 1,
                reset_attempt_count=(
                    text_index + 1 == len(state.text_items)
                    and state.photo_cursor < len(state.photo_items)
                ),
            ):
                return _fresh(job)

    # Refresh the in-memory cursor after the text loop; lease-guarded persistence
    # updates the model instance, while this immutable snapshot still holds its
    # pre-send value.
    state = state._replace(text_cursor=job.delivery_cursor)

    if state.photo_cursor < len(state.photo_items) and images_enabled:
        if state.photo_attempt_count >= _max_attempts(max_attempts):
            if state.text_first:
                for photo_index in range(state.photo_cursor, len(state.photo_items)):
                    if not mark_photo_progress(job, photo_index + 1, image_degraded=True):
                        return _fresh(job)
                state = state._replace(photo_cursor=len(state.photo_items))
            else:
                if not _begin_text_after_skipping_photos(job, state):
                    return _fresh(job)
                state = state._replace(
                    photo_cursor=len(state.photo_items),
                    text_phase_started=True,
                )
        else:
            if not _begin_photo_delivery_attempt(job, state):
                return _fresh(job)
            state = state._replace(photo_attempt_count=state.photo_attempt_count + 1)

    for photo_index in range(state.photo_cursor, len(state.photo_items)):
        if not _link_is_active(job):
            finish_job(
                job,
                status=TelegramUpdateReceipt.STATUS_CANCELLED,
                error_code="link_revoked",
            )
            return _fresh(job)
        try:
            if not images_enabled:
                logger.warning(
                    "telegram: presentation image delivery disabled for job=%s; "
                    "delivering text fallback",
                    job.update_id,
                )
                if not mark_photo_progress(job, photo_index + 1, image_degraded=True):
                    return _fresh(job)
                continue
            if photo_index not in rendered_photos:
                pending_indexes = list(range(photo_index, len(state.photo_items)))
                option_indexes = [
                    state.photo_items[candidate_index]["option_index"]
                    for candidate_index in pending_indexes
                ]
                rendered = list(render_photo_items(job, option_indexes))
                for offset, candidate_index in enumerate(pending_indexes):
                    rendered_photos[candidate_index] = (
                        rendered[offset] if offset < len(rendered) else None
                    )
            png = rendered_photos.get(photo_index)
            if not png:
                if state.photo_attempt_count < _max_attempts(max_attempts):
                    _requeue_photo_delivery(
                        job,
                        photo_attempt_count=state.photo_attempt_count,
                        error_code="image_render_failed",
                    )
                    return _fresh(job)
                # A renderer outage must not strand the already-validated
                # answer behind the photo. On the final attempt the photo is
                # skipped and the text/web link still drains.
                logger.warning(
                    "telegram: timetable image render exhausted for job=%s; "
                    "delivering text fallback",
                    job.update_id,
                )
                if not mark_photo_progress(job, photo_index + 1, image_degraded=True):
                    return _fresh(job)
                continue
            # Rendering may take several seconds. Re-check the exact account
            # binding after it finishes and immediately before exporting the
            # timetable to Telegram.
            if not _link_is_active(job):
                finish_job(
                    job,
                    status=TelegramUpdateReceipt.STATUS_CANCELLED,
                    error_code="link_revoked",
                )
                return _fresh(job)
            outcome = send_photo_item(job, png)
        except Exception as exc:  # noqa: BLE001 - only the exception CLASS becomes durable.
            outcome = {"ok": False, "error": type(exc).__name__}
        if not _delivery_succeeded(outcome):
            error_code, retryable, delay = _delivery_failure(outcome)
            if not retryable or state.photo_attempt_count >= _max_attempts(max_attempts):
                # Telegram may reject a particular image permanently (or the
                # transient retry budget may expire). Preserve the required text
                # answer instead of making an optional rendering failure terminal.
                logger.warning(
                    "telegram: timetable image delivery degraded for job=%s (%s)",
                    job.update_id,
                    error_code,
                )
                if not mark_photo_progress(job, photo_index + 1, image_degraded=True):
                    return _fresh(job)
                continue
            if retryable:
                _requeue_photo_delivery(
                    job,
                    photo_attempt_count=state.photo_attempt_count,
                    error_code=error_code,
                    delay_seconds=delay,
                )
            return _fresh(job)
        if not mark_photo_progress(job, photo_index + 1):
            return _fresh(job)

    if not state.text_first:
        if state.photo_items and not state.text_phase_started:
            if not _begin_text_delivery(job, photo_count=len(state.photo_items)):
                return _fresh(job)
        for text_index in range(state.text_cursor, len(state.text_items)):
            if not _link_is_active(job):
                finish_job(
                    job,
                    status=TelegramUpdateReceipt.STATUS_CANCELLED,
                    error_code="link_revoked",
                )
                return _fresh(job)
            try:
                outcome = send_text_item(job, state.text_items[text_index]["text"])
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
            if not mark_delivery_progress(job, text_index + 1):
                return _fresh(job)

    finish_job(
        job,
        result_code=job.result_code or "completed",
        error_code=(
            "image_delivery_degraded"
            if bool((job.delivery_payload or {}).get("image_degraded"))
            else ""
        ),
    )
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


def _default_render_photos(
    job: TelegramUpdateReceipt,
    option_indexes: Sequence[int | None],
) -> Sequence[bytes | None]:
    """Render only server-authored recipes against the stored assistant message."""

    if not job.assistant_message_id or not option_indexes:
        return []
    from core.services.advisor_presentations import normalise_presentation

    from .rendering import presentation_images_enabled, render_cards, worker_card_origin

    assistant = job.assistant_message
    presentation = normalise_presentation(assistant.presentation if assistant else None)
    if not presentation_images_enabled(presentation):
        return []

    with worker_card_origin() as base_url:
        return render_cards(
            message_id=job.assistant_message_id,
            base_url=base_url,
            option_indexes=list(option_indexes),
        )


def _job_presentation_images_enabled(job: TelegramUpdateReceipt) -> bool:
    """Whether this job's exact stored card kind is allowed to leave the system."""

    if not job.assistant_message_id:
        return False
    from core.services.advisor_presentations import normalise_presentation

    from .rendering import presentation_images_enabled

    assistant = job.assistant_message
    presentation = normalise_presentation(assistant.presentation if assistant else None)
    return presentation_images_enabled(presentation)


def _default_deliver_photo(
    job: TelegramUpdateReceipt,
    png: bytes,
) -> Mapping[str, Any] | bool | None:
    from . import linking
    from .transport import send_photo

    link = linking.active_link_by_id(job.link_id)
    if link is None:
        return {"ok": False, "error": "link_revoked", "permanent": True}
    return send_photo(chat_id=int(link.telegram_user_id), png=png)


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


def _owned_running(job: TelegramUpdateReceipt) -> QuerySet[TelegramUpdateReceipt]:
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
    exhausted_ids = list(
        TelegramUpdateReceipt.objects.filter(
            status=TelegramUpdateReceipt.STATUS_QUEUED,
            attempt_count__gte=_max_attempts(max_attempts),
        ).values_list("update_id", flat=True)
    )
    changed = 0
    for update_id in exhausted_ids:
        with transaction.atomic():
            qs = TelegramUpdateReceipt.objects.filter(update_id=update_id)
            if connection.features.has_select_for_update:
                qs = qs.select_for_update()
            job = qs.first()
            if (
                job is None
                or job.status != TelegramUpdateReceipt.STATUS_QUEUED
                or int(job.attempt_count or 0) < _max_attempts(max_attempts)
            ):
                continue
            if not _degrade_exhausted_photo_phase_locked(job, now=timezone.now()):
                _fail_locked(job, "max_attempts_exhausted")
            changed += 1
    return changed


def _photo_attempt_budget_exhausted(
    job: TelegramUpdateReceipt,
    *,
    max_attempts: int,
) -> bool:
    """Return whether durable optional-image retries are fully consumed."""

    state = _pending_photo_delivery_state(job)
    return bool(state is not None and state.photo_attempt_count >= _max_attempts(max_attempts))


def _pending_photo_delivery_state(
    job: TelegramUpdateReceipt,
) -> _DeliveryState | None:
    """Return validated state while the job is exclusively delivering photos."""

    items = _delivery_items(job.delivery_payload)
    state = (
        _delivery_state(job.delivery_payload, items, job.delivery_cursor)
        if items is not None
        else None
    )
    if state is None or state.photo_cursor >= len(state.photo_items):
        return None
    if state.text_first:
        # New manifests cannot enter their optional-media phase until every
        # required text chunk is durably confirmed.
        return state if state.text_cursor == len(state.text_items) else None
    if state.text_phase_started:
        return None
    return state


def _degrade_exhausted_photo_phase_locked(
    job: TelegramUpdateReceipt,
    *,
    now: Any,
) -> bool:
    """Skip exhausted optional photos without resetting an active text budget.

    A worker can die during Chromium or ``sendPhoto`` on its last lease. The
    ordinary exhausted-job path would then clear the whole manifest, making the
    optional image cost the student the already-validated answer. This transition
    advances only across the remaining leading photo items and requeues the text
    phase with its own bounded attempts.
    """

    items = _delivery_items(job.delivery_payload)
    state = (
        _delivery_state(job.delivery_payload, items, job.delivery_cursor)
        if items is not None
        else None
    )
    if items is None or state is None or not state.photo_items:
        return False
    # A rolled-back worker can advance the legacy DB cursor while leaving the
    # split manifest's marker false. That means required text has already
    # started: skip the now-out-of-order photos and preserve both its exact text
    # cursor and consumed retry count. Genuine image-phase recovery still gives
    # required text its independent fresh budget.
    if (
        state.text_phase_started
        and not state.rollback_text_takeover
        and not (state.text_first and state.text_cursor == len(state.text_items))
    ):
        return False
    if not state.text_items:
        return False

    payload = dict(job.delivery_payload or {})
    payload["cursor_mode"] = DELIVERY_CURSOR_MODE_SPLIT
    payload["photo_cursor"] = len(state.photo_items)
    payload["photo_attempt_count"] = state.photo_attempt_count
    payload["text_phase_started"] = True
    skipped_photos = state.photo_cursor < len(state.photo_items)
    if skipped_photos:
        payload["image_degraded"] = True
    job.status = TelegramUpdateReceipt.STATUS_QUEUED
    job.delivery_payload = payload
    job.delivery_cursor = state.text_cursor
    job.attempt_count = int(job.attempt_count or 0) if state.rollback_text_takeover else 0
    job.error_code = "image_delivery_degraded" if skipped_photos else ""
    job.available_at = now
    job.locked_by = ""
    job.locked_at = None
    job.lease_expires_at = None
    job.finished_at = None
    job.save(
        update_fields=[
            "status",
            "delivery_payload",
            "delivery_cursor",
            "attempt_count",
            "error_code",
            "available_at",
            "locked_by",
            "locked_at",
            "lease_expires_at",
            "finished_at",
        ]
    )
    return True


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
