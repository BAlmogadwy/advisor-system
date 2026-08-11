"""Run the durable Telegram adviser queue."""

from __future__ import annotations

import socket
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.llm_backend import LLMConfigError, get_llm_client
from telegram_gateway.configuration import (
    TelegramConfigurationError,
    validated_bot_token,
    validated_public_base_url,
)
from telegram_gateway.jobs import (
    DEFAULT_IDLE_SLEEP_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    MIN_LEASE_SECONDS,
    run_worker_loop,
)


class Command(BaseCommand):
    help = "Run queued Telegram adviser questions and ordered commands."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
        parser.add_argument(
            "--sleep",
            type=float,
            default=DEFAULT_IDLE_SLEEP_SECONDS,
            help="Idle sleep seconds between queue polls.",
        )
        parser.add_argument(
            "--worker-id",
            default="",
            help="Optional stable worker identifier for job leases.",
        )
        parser.add_argument(
            "--lease-seconds",
            type=int,
            default=DEFAULT_LEASE_SECONDS,
            help="Seconds before an abandoned RUNNING lease may be recovered.",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=DEFAULT_MAX_ATTEMPTS,
            help="Maximum claims before a repeatedly failing job becomes terminal.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not bool(getattr(settings, "TELEGRAM_ADVISOR_ENABLED", False)):
            raise CommandError(
                "TELEGRAM_ADVISOR_ENABLED is false; refusing to consume Telegram jobs."
            )
        self._validate_runtime_configuration()
        lease_seconds = int(options.get("lease_seconds") or DEFAULT_LEASE_SECONDS)
        if lease_seconds < MIN_LEASE_SECONDS:
            raise CommandError(
                f"--lease-seconds must be at least {MIN_LEASE_SECONDS} for the configured adviser timeouts."
            )
        worker_id = options.get("worker_id") or f"telegram-worker@{socket.gethostname()}"
        executed = run_worker_loop(
            worker_id=str(worker_id),
            once=bool(options.get("once")),
            idle_sleep_seconds=float(options.get("sleep") or DEFAULT_IDLE_SLEEP_SECONDS),
            lease_seconds=lease_seconds,
            max_attempts=int(options.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
        )
        self.stdout.write(self.style.SUCCESS(f"Telegram worker executed {executed} job(s)."))

    @staticmethod
    def _validate_runtime_configuration() -> None:
        """Fail before the first queue query when this process cannot serve work."""

        try:
            validated_bot_token(str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""))
            validated_public_base_url(str(getattr(settings, "TELEGRAM_PUBLIC_BASE_URL", "") or ""))
        except TelegramConfigurationError as exc:
            raise CommandError(f"{exc} Refusing to consume Telegram jobs.") from None

        try:
            # Constructing the selected client reuses the production endpoint,
            # credential and backend validation. It opens no socket; network I/O
            # starts only when a request method is called.
            client = get_llm_client()
        except (LLMConfigError, TypeError, ValueError) as exc:
            raise CommandError(f"The selected LLM configuration cannot execute: {exc}") from None
        if not client.config.allow_live_requests:
            raise CommandError(
                "The selected LLM configuration cannot execute because its deployment "
                "egress approval is disabled. Refusing to consume Telegram jobs."
            )
