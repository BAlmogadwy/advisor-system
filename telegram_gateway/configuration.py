"""Configuration validation shared by Telegram deployment entry points.

These checks are deliberately network-free.  They prove that a configured value
is safe to use before a webhook is registered or a durable worker claims student
work; reachability remains an operational health check.
"""

from __future__ import annotations

from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


class TelegramConfigurationError(ValueError):
    """A Telegram setting is absent or cannot be used safely."""


_BOT_CREDENTIAL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_TELEGRAM_WEBHOOK_PORTS = frozenset({80, 88, 443, 8443})
_HTTPS_URL_VALIDATOR = URLValidator(schemes=["https"])


def validated_bot_token(value: str) -> str:
    """Return a syntactically usable Bot API token without exposing its secret."""

    token = str(value or "").strip()
    if not token:
        raise TelegramConfigurationError("TELEGRAM_BOT_TOKEN is not set.")
    head, separator, credential = token.partition(":")
    if (
        separator != ":"
        or not head.isdigit()
        or not credential
        or any(char not in _BOT_CREDENTIAL_CHARS for char in credential)
    ):
        raise TelegramConfigurationError("TELEGRAM_BOT_TOKEN is malformed.")
    return token


def validated_public_base_url(value: str) -> str:
    """Return one HTTPS origin accepted by Telegram's hosted webhook service."""

    base = str(value or "").strip()
    if not base:
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL is not set.")
    if "\\" in base or any(ord(char) < 0x20 or char.isspace() for char in base):
        raise TelegramConfigurationError(
            "TELEGRAM_PUBLIC_BASE_URL must be one HTTPS origin without whitespace."
        )
    try:
        parsed = urlparse(base)
        port = parsed.port
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL is not a valid URL.") from None

    try:
        _HTTPS_URL_VALIDATOR(base)
    except ValidationError:
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL is not a valid URL.") from None

    if parsed.scheme.lower() != "https":
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL must use https.")
    if not hostname:
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL must include a hostname.")
    if username is not None or password is not None:
        raise TelegramConfigurationError("TELEGRAM_PUBLIC_BASE_URL must not include credentials.")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise TelegramConfigurationError(
            "TELEGRAM_PUBLIC_BASE_URL must be an origin without a path or query."
        )
    if port is not None and port not in _TELEGRAM_WEBHOOK_PORTS:
        raise TelegramConfigurationError(
            "TELEGRAM_PUBLIC_BASE_URL uses a port Telegram does not support."
        )
    return base.rstrip("/")


__all__ = [
    "TelegramConfigurationError",
    "validated_bot_token",
    "validated_public_base_url",
]
