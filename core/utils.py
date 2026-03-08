"""
core/utils.py
Shared utility functions used across multiple view modules.
Centralises common patterns to eliminate duplication.
"""

import json
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, JsonResponse

# Allowed directory for file imports (data/ under project root)
_ALLOWED_IMPORT_DIR = Path(settings.BASE_DIR) / "data"


def validate_import_path(raw_path: str) -> tuple[Path | None, str | None]:
    """Resolve *raw_path* and verify it lives under the allowed data directory.

    Returns (resolved_path, None) on success, or (None, error_message) on failure.
    """
    try:
        resolved = Path(raw_path).resolve(strict=False)
    except (OSError, ValueError) as exc:
        return None, f"Invalid path: {exc}"

    allowed_dir = _ALLOWED_IMPORT_DIR.resolve()

    try:
        resolved.relative_to(allowed_dir)
    except ValueError:
        return None, "Path must be inside the data/ directory."

    if not resolved.is_file():
        return None, f"File not found: {resolved.name}"

    return resolved, None


def parse_json_body(request: HttpRequest) -> tuple[dict, JsonResponse | None]:
    """Safely parse JSON body. Returns (payload, error_response)."""
    if not request.body:
        return {}, None
    try:
        data = json.loads(request.body.decode("utf-8"))
        return (data if isinstance(data, dict) else {}), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, JsonResponse({"error": "Invalid JSON body"}, status=400)


def parse_int_param(value: str | None, field: str) -> tuple[int | None, JsonResponse | None]:
    """Parse a required integer query parameter.

    Returns (parsed_int, None) on success, or (None, error_response) on failure.
    """
    if value is None:
        return None, JsonResponse(
            {"error": f"Missing required query parameter: {field}"}, status=400
        )
    try:
        return int(value), None
    except ValueError:
        return None, JsonResponse({"error": f"Invalid integer for {field}: {value}"}, status=400)


def safe_int(value: str | None, default: int) -> int:
    """Parse an optional integer query parameter, returning *default* on failure."""
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def safe_float(value: str | None, default: float) -> float:
    """Parse an optional float query parameter, returning *default* on failure."""
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default
