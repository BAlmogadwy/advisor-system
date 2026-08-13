"""Run the durable Telegram adviser queue."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import get_template

from core.services.llm_backend import LLMConfigError, get_llm_client
from telegram_gateway.card_assets import CARD_ASSET_URL_PREFIX, missing_card_assets
from telegram_gateway.cards import sign_renderer_request
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
from telegram_gateway.rendering import RENDER_TIMEOUT_MS, worker_card_origin

_CARD_TEMPLATE = "telegram_gateway/card.html"
_ORIGIN_PROBE_ASSET = "js/shared-timetable.js"
_ORIGIN_PROBE_TIMEOUT_SECONDS = 3.0
# Playwright's launch has its own deadline. The outer process deadline also
# bounds browser.close(), whose Python API has no timeout argument.
_IMAGE_PREFLIGHT_TIMEOUT_SECONDS = max(20.0, (RENDER_TIMEOUT_MS / 1000) + 10.0)
_IMAGE_PREFLIGHT_KILL_TIMEOUT_SECONDS = 5.0
_IMAGE_PREFLIGHT_ENV_NAMES = (
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PLAYWRIGHT_NODEJS_PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
)
_IMAGE_RUNTIME_CODES = frozenset(
    {
        "card_assets_unavailable",
        "card_origin_unavailable",
        "chromium_timeout",
        "chromium_unavailable",
        "image_runtime_unavailable",
    }
)


class ImageRuntimeValidationError(RuntimeError):
    """A sanitized reason the image-enabled worker must refuse to start."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _IMAGE_RUNTIME_CODES else "image_runtime_unavailable"
        self.code = safe_code
        super().__init__(safe_code)


def _image_preflight_environment() -> dict[str, str]:
    """Keep application credentials out of the browser smoke-test tree."""

    return {name: os.environ[name] for name in _IMAGE_PREFLIGHT_ENV_NAMES if name in os.environ}


def _terminate_image_preflight_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded teardown of the Python, Playwright, and browser tree."""

    # An abnormal Python exit can leave Chromium or its Playwright driver alive
    # in the process group. Reap the group even when the direct child is already
    # dead; this worker is long-lived and must not accumulate renderer orphans.
    direct_child_running = process.poll() is None

    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        try:
            subprocess.run(  # noqa: S603 - fixed system binary and numeric PID.
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_IMAGE_PREFLIGHT_KILL_TIMEOUT_SECONDS,
            )
        except Exception:
            if direct_child_running:
                try:
                    process.kill()
                except Exception:
                    pass
    else:
        try:
            # These APIs exist only on POSIX and this branch is unreachable on
            # Windows; Windows-targeted type stubs therefore omit them.
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except ProcessLookupError:
            pass
        except Exception:
            if direct_child_running:
                try:
                    process.kill()
                except Exception:
                    pass

    try:
        process.wait(timeout=_IMAGE_PREFLIGHT_KILL_TIMEOUT_SECONDS)
    except Exception:
        pass


def _probe_worker_card_origin(origin: str) -> None:
    """Prove the yielded loopback origin serves the allowlisted card assets."""

    url = f"{origin.rstrip('/')}{CARD_ASSET_URL_PREFIX}{_ORIGIN_PROBE_ASSET}"
    request = Request(
        url,
        headers={
            "X-Telegram-Card-Renderer": sign_renderer_request(),
            # Production's proxy header avoids SecurityMiddleware redirecting
            # the worker's intentionally HTTP-only loopback request.
            "X-Forwarded-Proto": "https",
        },
        method="GET",
    )
    # Explicitly bypass ambient proxy settings: this is a private loopback hop.
    with build_opener(ProxyHandler({})).open(
        request,
        timeout=_ORIGIN_PROBE_TIMEOUT_SECONDS,
    ) as response:  # noqa: S310 - the origin was restricted to loopback upstream.
        if response.status != 200:
            raise RuntimeError("card origin probe failed")
        response.read(1)


def validate_worker_image_runtime() -> None:
    """Prove the image runtime is usable before the first queue claim.

    No exception text, filesystem path or browser output crosses this boundary.
    The worker command reports only one of the fixed codes above.
    """

    try:
        get_template(_CARD_TEMPLATE)
        if missing_card_assets():
            raise ImageRuntimeValidationError("card_assets_unavailable")
    except ImageRuntimeValidationError:
        raise
    except Exception:
        raise ImageRuntimeValidationError("card_assets_unavailable") from None

    # The production worker normally starts an ephemeral loopback WSGI origin.
    # Entering and leaving the real context proves both bind and cleanup paths.
    try:
        with worker_card_origin() as origin:
            if not str(origin or "").strip():
                raise ImageRuntimeValidationError("card_origin_unavailable")
            _probe_worker_card_origin(str(origin))
    except ImageRuntimeValidationError:
        raise
    except Exception:
        raise ImageRuntimeValidationError("card_origin_unavailable") from None

    # Run the browser smoke test in a child process so the complete
    # import/launch/close sequence has a hard wall-clock ceiling. In particular,
    # Browser.close() itself exposes no timeout parameter.
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        f"    browser = p.chromium.launch(args=['--disable-dev-shm-usage'], timeout={RENDER_TIMEOUT_MS})\n"
        "    browser.close()\n"
    )
    popen_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": _image_preflight_environment(),
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and fixed script.
            [sys.executable, "-c", script],
            **popen_options,
        )
    except Exception:
        raise ImageRuntimeValidationError("chromium_unavailable") from None

    try:
        returncode = process.wait(timeout=_IMAGE_PREFLIGHT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_image_preflight_process_tree(process)
        raise ImageRuntimeValidationError("chromium_timeout") from None
    except Exception:
        _terminate_image_preflight_process_tree(process)
        raise ImageRuntimeValidationError("chromium_unavailable") from None
    if returncode != 0:
        _terminate_image_preflight_process_tree(process)
        raise ImageRuntimeValidationError("chromium_unavailable")


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

        if bool(getattr(settings, "TELEGRAM_SEND_TIMETABLE_IMAGES", False)) or bool(
            getattr(settings, "TELEGRAM_SEND_GRADUATION_IMAGES", False)
        ):
            try:
                validate_worker_image_runtime()
            except ImageRuntimeValidationError as exc:
                raise CommandError(
                    "Telegram timetable images are enabled, but the worker image "
                    f"runtime failed preflight ({exc.code}). Refusing to consume jobs."
                ) from None
            except Exception:
                raise CommandError(
                    "Telegram timetable images are enabled, but the worker image "
                    "runtime failed preflight (image_runtime_unavailable). "
                    "Refusing to consume jobs."
                ) from None
