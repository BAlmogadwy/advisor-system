from __future__ import annotations

from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

import pytest
from django.test import override_settings

from telegram_gateway import transport


class _Response:
    def __init__(self, body: bytes | str | object) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def _send(kind: str):
    client = transport.HttpTelegramTransport()
    if kind == "message":
        return client.send_message(chat_id=7, text="private answer")
    return client.send_photo(chat_id=7, png=b"\x89PNG-private")


@pytest.mark.parametrize("kind", ["message", "photo"])
def test_http_success_retains_only_a_minimal_acknowledgement(kind: str) -> None:
    body = b'{"ok":true,"result":{"text":"private answer","chat":{"id":7}}}'

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", return_value=_Response(body)),
    ):
        result = _send(kind)

    assert result == {"ok": True}
    assert "private" not in repr(result)


@pytest.mark.parametrize("kind", ["message", "photo"])
def test_http_2xx_api_rejection_is_a_delivery_failure(kind: str) -> None:
    body = b'{"ok":false,"description":"private answer rejected","result":{"text":"private"}}'

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", return_value=_Response(body)),
    ):
        result = _send(kind)

    assert result == {"ok": False, "error": "telegram_rejected"}
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"true",
        b"null",
        b'"ok"',
        b"\xff",
        object(),
    ],
)
@pytest.mark.parametrize("kind", ["message", "photo"])
def test_invalid_or_non_object_json_is_a_delivery_failure(kind: str, body: object) -> None:
    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", return_value=_Response(body)),
    ):
        result = _send(kind)

    assert result == {"ok": False, "error": "invalid_response"}


@pytest.mark.parametrize("kind", ["message", "photo"])
def test_http_429_exposes_only_the_nested_retry_after(kind: str) -> None:
    body = BytesIO(
        b'{"ok":false,"description":"private answer",'
        b'"retry_after":999,"parameters":{"retry_after":37,"other":"private"}}'
    )
    error = HTTPError("https://api.telegram.invalid", 429, "rate limited", None, body)

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", side_effect=error),
    ):
        result = _send(kind)

    assert result == {
        "ok": False,
        "error": "http_error",
        "status": 429,
        "retry_after": 37,
    }
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b'{"retry_after":12}',
        b'{"parameters":[]}',
        b'{"parameters":{"retry_after":true}}',
        b'{"parameters":{"retry_after":"12"}}',
        b'{"parameters":{"retry_after":1.5}}',
        b'{"parameters":{"retry_after":0}}',
        b'{"parameters":{"retry_after":-1}}',
        b'{"parameters":{"retry_after":2147483648}}',
    ],
)
def test_http_429_ignores_malformed_retry_parameters(body: bytes) -> None:
    error = HTTPError("https://api.telegram.invalid", 429, "rate limited", None, BytesIO(body))

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", side_effect=error),
    ):
        result = transport.HttpTelegramTransport().send_message(chat_id=7, text="private")

    assert result == {"ok": False, "error": "http_error", "status": 429}


def test_non_429_http_errors_never_read_or_return_the_response_body() -> None:
    class _ExplodingBody:
        def read(self):
            raise AssertionError("non-429 body was read")

    error = HTTPError(
        "https://api.telegram.invalid",
        400,
        "bad request",
        None,
        _ExplodingBody(),
    )

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", side_effect=error),
    ):
        result = transport.HttpTelegramTransport().send_message(chat_id=7, text="private")

    assert result == {"ok": False, "error": "http_error", "status": 400}


def test_unreadable_429_body_remains_a_sanitized_failure() -> None:
    class _ExplodingBody:
        def read(self):
            raise RuntimeError("private body failure")

    error = HTTPError(
        "https://api.telegram.invalid",
        429,
        "rate limited",
        None,
        _ExplodingBody(),
    )

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:secret"),
        mock.patch("telegram_gateway.transport.urlopen", side_effect=error),
    ):
        result = transport.HttpTelegramTransport().send_photo(chat_id=7, png=b"png")

    assert result == {"ok": False, "error": "http_error", "status": 429}


def test_injected_transport_boolean_results_are_preserved() -> None:
    class _BooleanTransport:
        def send_message(self, **_kwargs):
            return False

        def send_photo(self, **_kwargs):
            return True

    transport.set_transport(_BooleanTransport())
    try:
        assert transport.send_text(chat_id=7, text="x") is False
        assert transport.send_photo(chat_id=7, png=b"png") is True
    finally:
        transport.set_transport(None)
