"""Secret-free Playwright child for Telegram timetable-card screenshots.

This module is executed as an isolated Python script by ``rendering.py``.  It is
deliberately standard-library-only until ``render_urls`` imports Playwright: no
Django settings, database models, bot transport, or LLM client are reachable via
an import side effect.  The parent sends short-lived signed loopback URLs and a
short-lived renderer proof over stdin; this process returns a small framed batch
of PNGs over stdout.
"""

from __future__ import annotations

import json
import logging
import struct
import sys
from typing import Any
from urllib.parse import urlparse

RENDER_TIMEOUT_MS = 15_000
VIEWPORT = (760, 900)
MAX_RENDER_BATCH_URLS = 4
MAX_RENDER_CHILD_INPUT_BYTES = 64 * 1024
MAX_RENDERED_PNG_BYTES = 8 * 1024 * 1024
PROTOCOL_MAGIC = b"TGCR1"

logger = logging.getLogger(__name__)


def _is_local_card_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        _ = parsed.port
    except ValueError:
        return False
    path = parsed.path
    if not path.startswith("/telegram/card/") or not path.endswith("/"):
        return False
    token = path[len("/telegram/card/") : -1]
    return bool(
        parsed.scheme.lower() == "http"
        and host in {"127.0.0.1", "localhost"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and token
        and "/" not in token
    )


def decode_request(raw: bytes) -> tuple[list[str], str]:
    """Decode and independently constrain the parent's tiny JSON request."""

    if not raw or len(raw) > MAX_RENDER_CHILD_INPUT_BYTES:
        raise ValueError("invalid request size")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"urls", "renderer_token", "version"}:
        raise ValueError("invalid request shape")
    if value.get("version") != 1:
        raise ValueError("invalid request version")
    urls = value.get("urls")
    renderer_token = value.get("renderer_token")
    if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_RENDER_BATCH_URLS:
        raise ValueError("invalid URL count")
    if not isinstance(renderer_token, str) or not 1 <= len(renderer_token) <= 2048:
        raise ValueError("invalid renderer proof")
    if not all(
        isinstance(url, str) and len(url) <= 4096 and _is_local_card_url(url) for url in urls
    ):
        raise ValueError("invalid card URL")
    origins = {
        (urlparse(url).scheme.lower(), urlparse(url).hostname, urlparse(url).port) for url in urls
    }
    if len(origins) != 1:
        raise ValueError("mixed card origins")
    return urls, renderer_token


def encode_response(images: list[bytes | None]) -> bytes:
    """Encode one bounded image-or-missing slot for every requested URL."""

    if not 1 <= len(images) <= MAX_RENDER_BATCH_URLS:
        raise ValueError("invalid image count")
    out = bytearray(PROTOCOL_MAGIC)
    out.append(len(images))
    for image in images:
        if not image:
            out.append(0)
            continue
        if len(image) > MAX_RENDERED_PNG_BYTES or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            out.append(0)
            continue
        out.append(1)
        out.extend(struct.pack("!I", len(image)))
        out.extend(image)
    return bytes(out)


def render_urls(urls: list[str], renderer_token: str) -> list[bytes | None]:
    """Render a validated batch inside one browser; the parent owns the deadline."""

    from playwright.sync_api import sync_playwright

    out: list[bytes | None] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--disable-dev-shm-usage"],
            timeout=RENDER_TIMEOUT_MS,
        )
        try:
            context = browser.new_context(
                viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                device_scale_factor=2,
                extra_http_headers={
                    "X-Forwarded-Proto": "https",
                    "X-Telegram-Card-Renderer": renderer_token,
                },
            )
            page = context.new_page()
            for url in urls:
                out.append(_render_one(page, url))
        finally:
            # Playwright exposes no timeout for close. The parent process's hard
            # deadline kills this entire process group if close wedges.
            browser.close()
    return out


def _render_one(page: Any, url: str) -> bytes | None:
    try:
        response = page.goto(
            url,
            timeout=RENDER_TIMEOUT_MS,
            wait_until="domcontentloaded",
        )
        status = getattr(response, "status", None)
        if status is not None and status != 200:
            logger.warning("card page returned HTTP %s", status)
            return None
        page.wait_for_selector("#sa-card-root[data-card-ready]", timeout=RENDER_TIMEOUT_MS)
        root = page.query_selector("#sa-card-root")
        if root is None or root.get_attribute("data-card-error"):
            logger.warning("card page did not become renderable")
            return None
        image = root.screenshot(type="png", timeout=RENDER_TIMEOUT_MS)
        return image if isinstance(image, bytes) else None
    except Exception as exc:  # noqa: BLE001 - only the class reaches discarded stderr.
        logger.warning("card render failed (%s)", type(exc).__name__)
        return None


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_RENDER_CHILD_INPUT_BYTES + 1)
        urls, renderer_token = decode_request(raw)
        encoded = encode_response(render_urls(urls, renderer_token))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except Exception as exc:  # noqa: BLE001 - parent sees only the exit status.
        logger.warning("isolated renderer failed (%s)", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
