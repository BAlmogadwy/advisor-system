"""Register, inspect or remove the Telegram webhook, without handling the token.

The documented alternative is a `curl` with the bot token in the URL. That works,
and it also puts a live credential into shell history, into the terminal scrollback
and — if the command is ever pasted for help — into whatever it is pasted into.
This command reads the token from settings instead, so the value is never typed
and never displayed: every line it prints masks it.

`--set` is deliberately explicit rather than the default. Registering a webhook
points a real bot at a real host; it should be something someone chose to do.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from telegram_gateway.configuration import (
    TelegramConfigurationError,
    validated_bot_token,
    validated_public_base_url,
)

WEBHOOK_PATH = "/telegram/webhook/"

#: The only update type the gateway accepts. Registering more would have Telegram
#: deliver shapes the server refuses anyway — and the refusal is silent, so the
#: mismatch would show up as "the bot ignores me" rather than as an error.
ALLOWED_UPDATES = ["message"]

# Bot API `secret_token` values may contain only these characters. Requiring the
# same 32-character floor as the deployment checklist keeps this command from
# registering a guessable webhook secret by accident.
_WEBHOOK_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


def _validated_bot_token(token: str) -> str:
    """Reject malformed values before interpolating one into a request URL."""
    try:
        return validated_bot_token(token)
    except TelegramConfigurationError as exc:
        raise CommandError(str(exc)) from None


def _validated_public_base_url(value: str) -> str:
    """Return one HTTPS origin, never a URL carrying credentials or a path."""
    try:
        return validated_public_base_url(value)
    except TelegramConfigurationError as exc:
        raise CommandError(str(exc)) from None


def _mask(token: str) -> str:
    """Bot id is public; the half after the colon is the credential."""
    if not token:
        return "(unset)"
    head, _, _tail = token.partition(":")
    return f"{head}:{'*' * 8}" if head.isdigit() else "*" * 12


def _call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310
            decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                raise CommandError("Telegram returned an invalid response.")
            return decoded
    except HTTPError as exc:
        # The body can echo the request, and the request contains the token.
        raise CommandError(f"Telegram returned HTTP {exc.code} for {method}.") from None
    except (URLError, TimeoutError):
        raise CommandError("api.telegram.org is not reachable.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CommandError("Telegram returned an invalid response.") from None


class Command(BaseCommand):
    help = "Register, inspect or delete the Telegram webhook. Never prints the bot token."

    def add_arguments(self, parser: Any) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--set", action="store_true", help="Register the webhook.")
        group.add_argument("--info", action="store_true", help="Show the current registration.")
        group.add_argument("--delete", action="store_true", help="Remove the webhook.")
        parser.add_argument(
            "--keep-pending",
            action="store_true",
            help="Do not drop updates queued before this registration.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        token = str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")
        token = _validated_bot_token(token)
        if options["keep_pending"] and not options["set"]:
            raise CommandError("--keep-pending is valid only with --set.")
        self.stdout.write(f"bot: {_mask(token)}")

        if options["info"]:
            self._info(token)
            return
        if options["delete"]:
            result = _call(token, "deleteWebhook", {"drop_pending_updates": True})
            if not result.get("ok"):
                raise CommandError("deleteWebhook was refused by Telegram.")
            self.stdout.write(self.style.SUCCESS("Webhook deleted. The bot receives nothing."))
            return

        self._set(token, keep_pending=options["keep_pending"])

    def _set(self, token: str, *, keep_pending: bool) -> None:
        base = str(getattr(settings, "TELEGRAM_PUBLIC_BASE_URL", "") or "").strip()
        secret = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "")

        if not base:
            raise CommandError("TELEGRAM_PUBLIC_BASE_URL is not set.")
        base = _validated_public_base_url(base)
        if not secret:
            # Registering without one would leave the endpoint refusing every
            # update, which reads as "the bot is broken" rather than as this.
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not set; the webhook would refuse all.")
        if not _WEBHOOK_SECRET_RE.fullmatch(secret):
            raise CommandError(
                "TELEGRAM_WEBHOOK_SECRET must be 32-256 characters using only "
                "letters, digits, '_' and '-'."
            )
        if not getattr(settings, "TELEGRAM_ADVISOR_ENABLED", False):
            raise CommandError(
                "TELEGRAM_ADVISOR_ENABLED is false; the webhook would answer 404. "
                "Enable it and restart before registering."
            )

        url = f"{base}{WEBHOOK_PATH}"
        result = _call(
            token,
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ALLOWED_UPDATES,
                "drop_pending_updates": not keep_pending,
                # One ordered ingress stream matches the per-link FIFO durable
                # queue and avoids bursts across the two synchronous web workers.
                "max_connections": 1,
            },
        )
        if not result.get("ok"):
            raise CommandError("setWebhook was refused by Telegram.")
        self.stdout.write(self.style.SUCCESS(f"Webhook registered: {url}"))
        self.stdout.write(f"allowed_updates: {ALLOWED_UPDATES}")
        self._info(token)

    def _info(self, token: str) -> None:
        result = _call(token, "getWebhookInfo")
        if not result.get("ok") or not isinstance(result.get("result"), dict):
            raise CommandError("getWebhookInfo was refused by Telegram.")
        info = result["result"]
        self.stdout.write("")
        for key in (
            "url",
            "has_custom_certificate",
            "pending_update_count",
            "max_connections",
            "allowed_updates",
            "ip_address",
        ):
            if key in info:
                self.stdout.write(f"  {key}: {info[key]}")
        error = info.get("last_error_message")
        if error:
            self.stdout.write(self.style.WARNING(f"  last_error_message: {error}"))
            self.stdout.write(
                self.style.WARNING("  ^ Telegram could not deliver. Check the host is reachable.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("  no delivery errors"))
