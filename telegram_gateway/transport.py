"""The only code in the project that talks to api.telegram.org.

One narrow interface, so that every test can replace it with a list. The repo has
already been bitten once by a client that reached the network from a test — the
`forbid_llm_network` fixture in `tests/conftest.py` exists because of it, and it
patches `core.services.llm_backend` specifically, so a second HTTP client in a
different module is outside its reach.

Two things close that gap here, and both matter:

* `send_message` fails **closed when unconfigured** — no bot token means it
  returns `skipped` without opening a socket, so a test that forgets to install a
  fake still cannot call Telegram;
* the active transport is a module-level object swapped by `set_transport`, so a
  test replaces the whole thing rather than monkeypatching a URL.

Delivery failures are returned, never raised past the caller. Telegram being
unreachable must not turn into a webhook error, because a non-200 makes Telegram
redeliver the update — and a redelivery whose only problem was the *reply* would
re-run the model to produce an answer that was already generated.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)

#: Telegram rejects a `sendMessage` whose text exceeds this. The splitter in
#: `formatting` targets a lower figure; this is the hard ceiling it must never
#: cross, kept here beside the API that enforces it.
TELEGRAM_MAX_MESSAGE_CHARS = 4096


#: Deadline for a send made INSIDE the webhook request. Render runs gunicorn with
#: two sync workers for the entire platform, so a Telegram stall on this path is
#: not a slow reply — it is the site having no worker. Deliberately far below
#: TELEGRAM_API_TIMEOUT_SECONDS, which governs the background path where blocking
#: costs nothing but the answer.
INLINE_TIMEOUT_SECONDS = 3.0


#: Telegram truncates a photo caption at this, not at 4096. The caveats an
#: adviser answer carries — «مقترحات للمراجعة فقط، ولا تُعد تسجيلًا فعليًا» —
#: must therefore never ride in a caption: the answer text goes as its own
#: message, and the caption is at most a label.
TELEGRAM_MAX_CAPTION_CHARS = 1024


class TelegramTransport(Protocol):
    """What the gateway needs from Telegram. Nothing more is ever called."""

    def send_message(
        self, *, chat_id: int, text: str, timeout: float | None = None
    ) -> dict[str, Any]: ...

    def send_photo(
        self,
        *,
        chat_id: int,
        png: bytes,
        caption: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class RecordingTransport:
    """A transport that keeps what it was asked to send. For tests and dry runs."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    #: Photos are kept separately, and only their SIZE — a test asserting on
    #: image bytes would be asserting on the renderer, not on the transport.
    photos: list[dict[str, Any]] = field(default_factory=list)
    #: Set to raise instead of recording, to exercise the delivery-failure path.
    fail_with: Exception | None = None

    def send_message(
        self, *, chat_id: int, text: str, timeout: float | None = None
    ) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"chat_id": chat_id, "text": text, "timeout": timeout})
        return {"ok": True, "recorded": True}

    def send_photo(
        self,
        *,
        chat_id: int,
        png: bytes,
        caption: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.photos.append({"chat_id": chat_id, "bytes": len(png or b""), "caption": caption})
        return {"ok": True, "recorded": True}

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


class HttpTelegramTransport:
    """The real one: `POST https://api.telegram.org/bot<token>/sendMessage`."""

    def send_message(
        self, *, chat_id: int, text: str, timeout: float | None = None
    ) -> dict[str, Any]:
        token = str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        if not token:
            # Fail closed, and say so in the return value rather than by raising:
            # an unconfigured deployment should be inert, not broken.
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

        if timeout is None:
            timeout = float(getattr(settings, "TELEGRAM_API_TIMEOUT_SECONDS", 30) or 30)
        payload = {
            "chat_id": int(chat_id),
            # NO `parse_mode`. The body is an adviser's answer, written by a model,
            # and any markup mode makes `*`, `_`, `[` and `` ` `` in that answer
            # into syntax — at best mangling a course code, at worst letting the
            # answer's own characters change how the rest of it renders. Plain text
            # has no escaping problem to get wrong.
            "text": text[:TELEGRAM_MAX_MESSAGE_CHARS],
            # A policy citation with a URL should not paint a preview card over the
            # answer that cited it.
            "disable_web_page_preview": True,
        }
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
                return _telegram_response(response, operation="sendMessage")
        except HTTPError as exc:
            logger.warning("telegram: sendMessage rejected with HTTP %s", exc.code)
            return _http_error_result(exc)
        except (URLError, TimeoutError) as exc:
            logger.warning("telegram: sendMessage transport failure (%s)", type(exc).__name__)
            return {"ok": False, "error": "unreachable"}

    def send_photo(
        self,
        *,
        chat_id: int,
        png: bytes,
        caption: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """`sendPhoto` as multipart/form-data.

        Hand-built rather than pulled from `requests`, which this project does not
        depend on. The boundary is random per call so a caption can never close
        the part it appears in.
        """
        token = str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        if not token:
            return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}
        if not png:
            return {"ok": False, "skipped": True, "reason": "empty_image"}

        if timeout is None:
            timeout = float(getattr(settings, "TELEGRAM_API_TIMEOUT_SECONDS", 30) or 30)

        boundary = f"----telegram{secrets.token_hex(16)}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )

        field("chat_id", str(int(chat_id)))
        if caption:
            # Truncated HERE and never relied on: the answer's caveats travel as
            # their own message, because a caption is capped at 1024 and silently
            # cutting a disclaimer is the failure this whole channel avoids.
            field("caption", caption[:TELEGRAM_MAX_CAPTION_CHARS])
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo";'
            ' filename="timetable.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
        )
        parts.append(png)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        request = Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
                return _telegram_response(response, operation="sendPhoto")
        except HTTPError as exc:
            logger.warning("telegram: sendPhoto rejected with HTTP %s", exc.code)
            return _http_error_result(exc)
        except (URLError, TimeoutError) as exc:
            logger.warning("telegram: sendPhoto transport failure (%s)", type(exc).__name__)
            return {"ok": False, "error": "unreachable"}


def _telegram_response(response: Any, *, operation: str) -> dict[str, Any]:
    """Validate a 2xx Bot API response and retain no Telegram response data."""

    payload = _json_object_from_body(response)
    if payload is None:
        logger.warning("telegram: %s returned an invalid response", operation)
        return {"ok": False, "error": "invalid_response"}
    if payload.get("ok") is not True:
        # Telegram sometimes reports application-level rejection with HTTP 200.
        # Descriptions and result objects can echo message/account data, so the
        # failure is deliberately reduced to one stable local code.
        logger.warning("telegram: %s was rejected by the Bot API", operation)
        return {"ok": False, "error": "telegram_rejected"}
    # A successful send needs no remote message id, echoed text, chat object, or
    # file metadata. Keeping only the acknowledgement also prevents a durable
    # caller from accidentally persisting Telegram's response body.
    return {"ok": True}


def _http_error_result(exc: HTTPError) -> dict[str, Any]:
    """Return a sanitized HTTP failure, including only a valid 429 delay."""

    result: dict[str, Any] = {"ok": False, "error": "http_error", "status": exc.code}
    if exc.code == 429:
        retry_after = _retry_after_from_http_error(exc)
        if retry_after is not None:
            result["retry_after"] = retry_after
    return result


def _retry_after_from_http_error(exc: HTTPError) -> int | None:
    """Read only Telegram's documented ``parameters.retry_after`` integer."""

    payload = _json_object_from_body(exc)
    if payload is None:
        return None
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    # JSON booleans are Python ints, so reject them explicitly. Telegram documents
    # this field as an Integer; accepting strings/floats would broaden the trusted
    # shape and could feed an unbounded value into a worker's timedelta.
    if isinstance(retry_after, bool) or not isinstance(retry_after, int):
        return None
    if not 1 <= retry_after <= 2_147_483_647:
        return None
    return retry_after


def _json_object_from_body(stream: Any) -> dict[str, Any] | None:
    """Parse a JSON object without ever returning or logging the raw body."""

    try:
        raw = stream.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - a malformed remote body must stay a delivery failure.
        return None
    return payload if isinstance(payload, dict) else None


_TRANSPORT: TelegramTransport | None = None


def get_transport() -> TelegramTransport:
    global _TRANSPORT
    if _TRANSPORT is None:
        _TRANSPORT = HttpTelegramTransport()
    return _TRANSPORT


def set_transport(transport: TelegramTransport | None) -> None:
    """Install a transport, or `None` to fall back to the real one."""
    global _TRANSPORT
    _TRANSPORT = transport


def send_photo(
    *, chat_id: int, png: bytes, caption: str = "", timeout: float | None = None
) -> dict[str, Any]:
    """Deliver one picture, absorbing transport failure.

    Same contract as `send_text`: a raise here would propagate into the webhook
    and turn a delivery problem into a non-200, which makes Telegram redeliver
    the update and the model answer it a second time. A lost picture is the
    cheaper failure — the answer text and the link still arrive.
    """
    try:
        return get_transport().send_photo(
            chat_id=int(chat_id), png=png, caption=caption, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: photo delivery failed (%s)", type(exc).__name__)
        return {"ok": False, "error": "delivery_failed"}


def send_text(*, chat_id: int, text: str, timeout: float | None = None) -> dict[str, Any]:
    """Deliver one message, absorbing transport failure.

    A raise here would propagate into the webhook and turn a *delivery* problem
    into a non-200, which makes Telegram redeliver the update and the model answer
    it a second time. The answer is already stored; the student can see it on the
    web. Losing the notification is the cheaper failure.
    """
    try:
        return get_transport().send_message(chat_id=int(chat_id), text=text, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: delivery failed (%s)", type(exc).__name__)
        return {"ok": False, "error": "delivery_failed"}


def delivery_succeeded(result: Any) -> bool:
    """Whether Telegram accepted one outbound unit.

    The transport deliberately returns data instead of raising so a delivery
    outage cannot make Telegram replay an already-generated academic answer.
    Durable workers still need one shared interpretation of that data; leaving
    each caller to inspect loosely shaped dictionaries is how a rejected send is
    accidentally marked delivered.
    """

    return isinstance(result, dict) and result.get("ok") is True


def delivery_is_retryable(result: Any) -> bool:
    """Classify a failed send without retaining Telegram's response body.

    Network errors, rate limiting and server errors may recover. Other 4xx
    responses describe a request/chat that retrying unchanged will not repair.
    An unconfigured deployment is treated as transient so a corrected secret can
    drain the durable queue rather than losing every answer accepted during the
    mistake.
    """

    if delivery_succeeded(result):
        return False
    if not isinstance(result, dict):
        return True
    status = result.get("status")
    try:
        status_i = int(status)
    except (TypeError, ValueError):
        return True
    if status_i == 429 or status_i >= 500:
        return True
    return not 400 <= status_i < 500


__all__ = [
    "INLINE_TIMEOUT_SECONDS",
    "TELEGRAM_MAX_CAPTION_CHARS",
    "TELEGRAM_MAX_MESSAGE_CHARS",
    "HttpTelegramTransport",
    "RecordingTransport",
    "TelegramTransport",
    "delivery_is_retryable",
    "delivery_succeeded",
    "get_transport",
    "send_photo",
    "send_text",
    "set_transport",
]
