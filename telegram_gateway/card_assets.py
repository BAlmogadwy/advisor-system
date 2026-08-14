"""The exact source assets needed by the private adviser-card renderer.

The durable Telegram worker must not depend on ``STATIC_ROOT`` being fresh. A
partially collected or stale WhiteNoise manifest can otherwise make every card
render fail even though the worker itself starts normally. These four files are
resolved through Django's static-file finders and exposed only through the
renderer-authenticated card asset view.

This is intentionally an allowlist, not a miniature static-file server.
"""

from __future__ import annotations

from pathlib import Path

from django.contrib.staticfiles import finders

CARD_ASSET_URL_PREFIX = "/telegram/card-assets/"
CARD_ASSET_CONTENT_TYPES = {
    "css/global.css": "text/css; charset=utf-8",
    "css/bootstrap-compat.css": "text/css; charset=utf-8",
    "js/shared-timetable.js": "text/javascript; charset=utf-8",
    "js/prereq-graph.js": "text/javascript; charset=utf-8",
    "js/page-student-advisor.js": "text/javascript; charset=utf-8",
    # global.css references this path. Browsers normally do not fetch it for the
    # card DOM, but keeping the exact dependency available avoids a renderer
    # changing behaviour when a shared selector evolves.
    "img/side-decor1.png": "image/png",
}


def resolve_card_asset(asset_path: str) -> Path | None:
    """Return one trusted source asset, never an arbitrary filesystem path."""

    normalised = str(asset_path or "").replace("\\", "/").lstrip("/")
    if normalised not in CARD_ASSET_CONTENT_TYPES:
        return None
    found = finders.find(normalised)
    if not found or isinstance(found, list | tuple):
        return None
    candidate = Path(found)
    return candidate if candidate.is_file() else None


def missing_card_assets() -> list[str]:
    """Names unavailable to the worker; safe to include in startup diagnostics."""

    return [name for name in CARD_ASSET_CONTENT_TYPES if resolve_card_asset(name) is None]


__all__ = [
    "CARD_ASSET_CONTENT_TYPES",
    "CARD_ASSET_URL_PREFIX",
    "missing_card_assets",
    "resolve_card_asset",
]
