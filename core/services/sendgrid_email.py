"""Small, provider-specific adapter for transactional email.

The rest of the application supplies a recipient, subject, and plain-text body;
this module is the only place that knows the Twilio SendGrid SDK.  Exceptions are
collapsed to stable reason codes so provider bodies, API keys, recipients, and
message contents cannot accidentally reach application logs.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings
from python_http_client.exceptions import HTTPError  # type: ignore[import-untyped]
from sendgrid import SendGridAPIClient  # type: ignore[import-untyped]
from sendgrid.helpers.mail import Email, Mail  # type: ignore[import-untyped]

from core.services import rate_limit

_MESSAGE_ID_SAFE = re.compile(r"[^A-Za-z0-9._:@+\-]")
_GLOBAL_BUDGET_IDENTITY = "global"


class SendGridDeliveryError(Exception):
    """A safe-to-log SendGrid failure category (never the provider response)."""


def _message_id(headers: Any) -> str:
    """Return a bounded, display-safe X-Message-ID from an SDK response."""

    try:
        raw: Any = ""
        # python-http-client exposes response headers as ``HTTPMessage`` rather
        # than a ``Mapping``.  Both it and ordinary dict test doubles provide an
        # ``items()`` method, so use the smallest shared protocol and fail closed
        # if a malformed provider response does not implement it safely.
        items = getattr(headers, "items", None)
        header_items = items() if callable(items) else ()
        for key, value in header_items:
            normalized_key = (
                key.decode("ascii", errors="ignore") if isinstance(key, bytes) else str(key)
            )
            if normalized_key.lower() == "x-message-id":
                raw = value
                break
        if isinstance(raw, list | tuple):
            raw = raw[0] if raw else ""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")
        return _MESSAGE_ID_SAFE.sub("", str(raw))[:255]
    except Exception:  # noqa: BLE001 - an optional receipt must never fail delivery
        return ""


def send_transactional_email(to_email: str, subject: str, body: str) -> str:
    """Submit one plain-text message and return its sanitized provider id.

    SendGrid Mail Send acknowledges queued mail with HTTP 202.  Any other status
    is a failure; the response body is deliberately neither surfaced nor logged.
    """

    enabled = bool(getattr(settings, "STUDENT_OTP_SENDGRID_ENABLED", False))
    api_key = str(getattr(settings, "SENDGRID_API_KEY", "") or "").strip()
    from_email = str(getattr(settings, "SENDGRID_FROM_EMAIL", "") or "").strip()
    from_name = str(getattr(settings, "SENDGRID_FROM_NAME", "") or "").strip()
    if not enabled:
        raise SendGridDeliveryError("provider_disabled")
    if not api_key or not from_email:
        raise SendGridDeliveryError("configuration_missing")
    if not str(to_email).strip() or not str(subject).strip() or not str(body):
        raise SendGridDeliveryError("invalid_message")
    try:
        timeout = min(60, max(1, int(getattr(settings, "SENDGRID_TIMEOUT_SECONDS", 15))))
        max_submissions = int(getattr(settings, "SENDGRID_MAX_SUBMISSIONS", 4700))
        window_seconds = int(getattr(settings, "SENDGRID_SUBMISSION_WINDOW_SECONDS", 86_400))
        if max_submissions < 1 or window_seconds < 1:
            raise ValueError
    except (TypeError, ValueError):
        # Bad local configuration must not spend the provider allowance.
        raise SendGridDeliveryError("configuration_invalid") from None

    try:
        # One database-backed global row protects every email entry point and
        # every web process. Count before opening the socket: failures and
        # timeouts can still consume the provider allowance, so refunding
        # them would make the local guard weaker than the provider quota.
        decision = rate_limit.consume_configured(
            rate_limit.SENDGRID_SUBMISSION,
            _GLOBAL_BUDGET_IDENTITY,
            max_calls=max_submissions,
            window_seconds=window_seconds,
        )
        if not decision.allowed:
            raise SendGridDeliveryError("provider_budget_exhausted")

        client = SendGridAPIClient(api_key=api_key)
        # sendgrid-python 6.12.5 exposes the python-http-client instance here.
        # Assigning its public timeout attribute keeps the SDK's request path and
        # still lets lightweight test doubles omit the nested client entirely.
        transport = getattr(client, "client", None)
        if transport is not None:
            transport.timeout = timeout
        message = Mail(
            from_email=Email(from_email, from_name or None),
            to_emails=str(to_email).strip(),
            subject=str(subject),
            plain_text_content=str(body),
        )
        response = client.send(message)
    except SendGridDeliveryError:
        raise
    except HTTPError as exc:
        # python-http-client raises on every HTTP error, so classify only from
        # the numeric status.  Never inspect or surface its body or headers:
        # those can contain provider diagnostics or message metadata.
        try:
            status_code = int(exc.status_code)
        except (AttributeError, TypeError, ValueError):
            status_code = 0
        if 400 <= status_code < 500 and status_code not in {408, 429}:
            raise SendGridDeliveryError("provider_rejected") from None
        raise SendGridDeliveryError("provider_unavailable") from None
    except Exception:  # noqa: BLE001 - provider details may contain sensitive data
        raise SendGridDeliveryError("provider_unavailable") from None

    try:
        accepted = int(response.status_code) == 202
    except Exception:  # noqa: BLE001 - malformed response metadata is provider failure
        accepted = False
    if not accepted:
        raise SendGridDeliveryError("provider_rejected")
    return _message_id(getattr(response, "headers", {}))
