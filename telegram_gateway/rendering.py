"""Turning the card page into a PNG, and failing quietly when it cannot.

Two rules govern this module, and both are about what happens when it does not
work.

**A failed screenshot must never cost the student their answer.** The answer is
already generated, validated and stored by the time anything here runs. So every
path returns `None` rather than raising: no Chromium installed, no browser
launch, a render timeout, a broken page — all of them degrade to "send the text
and the link", which is exactly the behaviour that shipped before images existed.

**It must be replaceable in tests.** Same shape as the transport: a module-level
renderer swapped by `set_renderer`, and a recording implementation that yields
deterministic bytes. Nothing in the test suite may start a browser — Playwright
is installed here, but Chromium is *not* installed on Render (`playwright install
chromium` is still missing from the build command), so a test that needs a real
browser would pass locally and fail in CI for a reason unrelated to the change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.conf import settings

logger = logging.getLogger(__name__)

#: Wall-clock ceiling for one card. A browser that has not produced a picture by
#: now is not going to; the turn should not wait on it.
RENDER_TIMEOUT_MS = 15_000

#: Card page width. Matches the `#sa-card-root` width in card.html — a phone
#: renders the image at its own width, and a desktop-width screenshot is
#: unreadable once Telegram scales it down.
VIEWPORT = (940, 900)


def images_enabled() -> bool:
    """Whether to send timetable pictures at all. Read at call time, default off.

    Separate from `TELEGRAM_ADVISOR_ENABLED` on purpose: a picture of a week grid
    is a compact record of where a student is and when, it is stored on Telegram's
    servers under a durable `file_id`, and it is far easier to forward than prose.
    That deserves its own switch and its own decision.
    """
    return bool(getattr(settings, "TELEGRAM_SEND_TIMETABLE_IMAGES", False))


class CardRenderer(Protocol):
    """What the gateway needs in order to get a PNG. Nothing else is called."""

    def render(self, url: str) -> bytes | None: ...


@dataclass
class RecordingRenderer:
    """Deterministic bytes, and a record of what was asked for. For tests."""

    #: A 1x1 PNG. Small, valid, and obviously not a real card.
    png: bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    requested: list[str] = field(default_factory=list)
    #: Set to make every render fail, to exercise the degrade-to-text path.
    fail: bool = False

    def render(self, url: str) -> bytes | None:
        self.requested.append(url)
        return None if self.fail else self.png


class PlaywrightCardRenderer:
    """The real one. Screenshots `#sa-card-root` on the signed card page."""

    def render(self, url: str) -> bytes | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("telegram: playwright is not installed; sending text only")
            return None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
                try:
                    page = browser.new_page(
                        viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                        device_scale_factor=2,  # legible after Telegram's downscale
                    )
                    page.goto(url, timeout=RENDER_TIMEOUT_MS, wait_until="load")
                    # Wait for the page to SAY it is done rather than sleeping.
                    # A fixed delay produces a half-drawn card on a slow machine
                    # and wastes time on a fast one.
                    page.wait_for_selector(
                        "#sa-card-root[data-card-ready]", timeout=RENDER_TIMEOUT_MS
                    )
                    root = page.query_selector("#sa-card-root")
                    if root is None or root.get_attribute("data-card-error"):
                        logger.warning(
                            "telegram: card page reported %s",
                            root.get_attribute("data-card-error") if root else "no root",
                        )
                        return None
                    return root.screenshot(type="png")
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            # Includes a missing Chromium, which is the expected state on Render
            # today. No URL in the log: it carries a signed token.
            logger.warning("telegram: card render failed (%s)", type(exc).__name__)
            return None


_RENDERER: CardRenderer | None = None


def get_renderer() -> CardRenderer:
    global _RENDERER
    if _RENDERER is None:
        _RENDERER = PlaywrightCardRenderer()
    return _RENDERER


def set_renderer(renderer: CardRenderer | None) -> None:
    """Install a renderer, or `None` to fall back to the real one."""
    global _RENDERER
    _RENDERER = renderer


def render_card(*, message_id: Any, base_url: str, option_index: int | None = None) -> bytes | None:
    """A PNG of this message's card, or `None` — never an exception.

    `base_url` is where the headless browser should reach this server. It is the
    LOCAL origin, not `TELEGRAM_PUBLIC_BASE_URL`: the browser runs beside the
    server, so sending it out through the public hostname would take a signed URL
    on a round trip through the internet for no reason.
    """
    if not images_enabled():
        return None
    if not base_url:
        # Said once, plainly. Without this the caller cannot tell "no picture
        # because it is switched off" from "no picture because nobody knows where
        # this server is".
        logger.warning("telegram: cannot render a card without an internal base URL")
        return None

    from .cards import sign_card

    token = sign_card(message_id=message_id, option_index=option_index)
    url = f"{str(base_url).rstrip('/')}/telegram/card/{token}/"
    try:
        return get_renderer().render(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: card renderer raised (%s)", type(exc).__name__)
        return None


def local_base_url(port: int | str | None = None) -> str:
    """Where the headless browser reaches this process.

    `port` is the port THIS process is listening on, taken from the webhook
    request that triggered the work (`SERVER_PORT`). Deriving it beats defaulting
    it: the first version hard-coded 8000, the dev server runs on 8001/8002, and
    the resulting failure was silent — the renderer fetched a port with nothing on
    it, returned `None`, and the turn degraded to text exactly as it does when
    Chromium is missing. A wrong default that fails the same way as a legitimate
    fallback is a default that hides its own misconfiguration.

    The explicit setting still wins, for deployments where the request's port is
    not where the app is reachable (a unix socket, a container port mapping).
    """
    configured = str(getattr(settings, "TELEGRAM_INTERNAL_BASE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    if port:
        return f"http://127.0.0.1:{int(port)}"
    logger.warning(
        "telegram: no internal base URL and no request port; "
        "set TELEGRAM_INTERNAL_BASE_URL or images will not render"
    )
    return ""


__all__ = [
    "RENDER_TIMEOUT_MS",
    "CardRenderer",
    "PlaywrightCardRenderer",
    "RecordingRenderer",
    "get_renderer",
    "images_enabled",
    "local_base_url",
    "render_card",
    "set_renderer",
]
