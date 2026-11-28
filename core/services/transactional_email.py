"""Submit one transactional message through the configured email transport."""

from __future__ import annotations

import http.client
import json
import logging
from contextlib import closing
from datetime import timedelta
from email.header import Header
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, transaction
from django.utils import timezone

from core.models import RateLimitBucket
from core.services import rate_limit
from core.services.sendgrid_email import SendGridDeliveryError as EmailDeliveryError
from core.services.sendgrid_email import sanitize_message_id
from core.services.sendgrid_email import send_transactional_email as _send_via_sendgrid

_BREVO_LOCK = "brevo_submission:lock"
_BREVO_ATTEMPT_PREFIX = "brevo_submission:attempt:"
_BREVO_DEFINITE_REJECTIONS = {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}
_SES_ENDPOINT = "https://email.ap-southeast-1.amazonaws.com"
# SES answers 408/409 for transient conflicts and 429 for throttling: retryable, not a refusal.
_SES_TRANSIENT_4XX = {408, 409, 429}

logger = logging.getLogger(__name__)


class _BrevoRejected(Exception):
    """Brevo explicitly declined this submission."""


def _charge_shared_budget() -> None:
    # Continue using the existing durable bucket and its configured limit during cutover.
    decision = rate_limit.consume_configured(
        rate_limit.SENDGRID_SUBMISSION,
        "global",
        max_calls=settings.SENDGRID_MAX_SUBMISSIONS,
        window_seconds=settings.SENDGRID_SUBMISSION_WINDOW_SECONDS,
    )
    if not decision.allowed:
        raise EmailDeliveryError("provider_budget_exhausted")


def _reserve_brevo_submission() -> bool:
    """Reserve one primary attempt within a strict rolling 24-hour limit."""
    with transaction.atomic():
        lock, _ = RateLimitBucket.objects.select_for_update().get_or_create(
            key=_BREVO_LOCK, defaults={"window_start": timezone.now}
        )
        now = timezone.now()
        recent = RateLimitBucket.objects.filter(
            key__startswith=_BREVO_ATTEMPT_PREFIX, window_start__gt=now - timedelta(days=1)
        ).count()
        if recent >= settings.BREVO_MAX_SUBMISSIONS:
            return False
        _charge_shared_budget()
        RateLimitBucket.objects.create(key=f"{_BREVO_ATTEMPT_PREFIX}{uuid4()}", window_start=now)
        lock.window_start = now
        lock.save(update_fields=["window_start"])
    return True


def _send_via_brevo(to_email: str, subject: str, body: str, *, timeout: int) -> str:
    sender = {"email": settings.BREVO_FROM_EMAIL or settings.STUDENT_OTP_FROM_EMAIL}
    if settings.STUDENT_OTP_FROM_NAME:
        sender["name"] = settings.STUDENT_OTP_FROM_NAME
    payload = json.dumps(
        {
            "sender": sender,
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": body,
        }
    ).encode("utf-8")
    connection = http.client.HTTPSConnection("api.brevo.com", timeout=timeout)
    try:
        connection.request(
            "POST",
            "/v3/smtp/email",
            body=payload,
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        status = response.status
        if status in _BREVO_DEFINITE_REJECTIONS:
            raise _BrevoRejected
        if status != 201:
            raise EmailDeliveryError("provider_unavailable")
        # The status already acknowledged the send. The optional receipt must
        # never cause a replacement message or invalidate an accepted code.
        try:
            receipt = json.loads(response.read(4096))
            message_id = receipt.get("messageId") if isinstance(receipt, dict) else None
            return sanitize_message_id(message_id) if isinstance(message_id, str) else ""
        except (OSError, ValueError, http.client.HTTPException):
            return ""
    except (OSError, ValueError, http.client.HTTPException):
        raise EmailDeliveryError("provider_unavailable") from None
    finally:
        connection.close()


def _send_via_ses(to_email: str, subject: str, body: str, *, timeout: int) -> str:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    from_name = settings.STUDENT_OTP_FROM_NAME
    if from_name:
        if from_name.isascii():
            from_name = '"' + from_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            from_name = Header(from_name, "utf-8", maxlinelen=998).encode(linesep="")
    from_address = settings.STUDENT_OTP_FROM_EMAIL
    if from_name:
        from_address = f"{from_name} <{from_address}>"

    try:
        # A session of its own per send: boto3's shared default session is not safe to
        # create clients from concurrently, and gunicorn runs several threads.
        client = boto3.session.Session().client(
            "sesv2",
            region_name=settings.AWS_REGION,
            endpoint_url=_SES_ENDPOINT,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            aws_session_token=settings.AWS_SESSION_TOKEN or None,
            config=Config(
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
        with closing(client):
            response = client.send_email(
                FromEmailAddress=from_address,
                Destination={"ToAddresses": [to_email]},
                Content={
                    "Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                    }
                },
            )
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise EmailDeliveryError("provider_unavailable")
        message_id = response.get("MessageId")
        return sanitize_message_id(message_id) if isinstance(message_id, str) else ""
    except EmailDeliveryError:
        raise
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        reason = (
            "provider_rejected"
            if 400 <= status < 500 and status not in _SES_TRANSIENT_4XX
            else "provider_unavailable"
        )
        raise EmailDeliveryError(reason) from None
    except Exception:  # provider details may contain sensitive data
        raise EmailDeliveryError("provider_unavailable") from None


def send_transactional_email(to_email: str, subject: str, body: str) -> str:
    """Return a provider receipt after acceptance, or raise a safe failure category."""
    provider = settings.STUDENT_OTP_EMAIL_PROVIDER
    if provider == "sendgrid":
        return _send_via_sendgrid(to_email, subject, body)
    if provider not in {"brevo", "brevo_ses"}:
        raise EmailDeliveryError("configuration_invalid")

    brevo_sender = settings.BREVO_FROM_EMAIL or settings.STUDENT_OTP_FROM_EMAIL
    if not brevo_sender or not settings.BREVO_API_KEY:
        raise EmailDeliveryError("configuration_missing")
    has_fallback = provider == "brevo_ses"
    if has_fallback and not (
        settings.STUDENT_OTP_FROM_EMAIL
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    ):
        raise EmailDeliveryError("configuration_missing")
    timeout = settings.STUDENT_OTP_EMAIL_TIMEOUT_SECONDS
    if (
        (has_fallback and settings.AWS_REGION != "ap-southeast-1")
        or not 1 <= timeout <= 60
        or not 1 <= settings.BREVO_MAX_SUBMISSIONS <= 300
        or min(settings.SENDGRID_MAX_SUBMISSIONS, settings.SENDGRID_SUBMISSION_WINDOW_SECONDS) < 1
        or "\r" in settings.STUDENT_OTP_FROM_NAME
        or "\n" in settings.STUDENT_OTP_FROM_NAME
    ):
        raise EmailDeliveryError("configuration_invalid")
    try:
        validate_email(brevo_sender)
        if has_fallback and settings.STUDENT_OTP_FROM_EMAIL != brevo_sender:
            validate_email(settings.STUDENT_OTP_FROM_EMAIL)
    except ValidationError:
        raise EmailDeliveryError("configuration_invalid") from None
    to_email = to_email.strip()
    try:
        validate_email(to_email)
    except ValidationError:
        raise EmailDeliveryError("invalid_message") from None
    if not subject.strip() or not body:
        raise EmailDeliveryError("invalid_message")

    try:
        if _reserve_brevo_submission():
            try:
                return _send_via_brevo(to_email, subject, body, timeout=timeout)
            except _BrevoRejected:
                if not has_fallback:
                    raise EmailDeliveryError("provider_rejected") from None
                # No recipient, code or key in the line: only that the fallback was taken.
                logger.warning(
                    "student OTP email: Brevo declined the submission; sending through SES"
                )
        elif not has_fallback:
            raise EmailDeliveryError("provider_budget_exhausted")
        else:
            logger.warning("student OTP email: Brevo daily cap reached; sending through SES")

        _charge_shared_budget()
    except DatabaseError:
        raise EmailDeliveryError("provider_unavailable") from None
    return _send_via_ses(to_email, subject, body, timeout=timeout)
