"""Data capabilities exposed to lower-trust adviser channels.

Telegram private chats are authenticated, but Telegram is still an external
cloud surface and bot chats are not end-to-end encrypted.  The channel therefore
gets the planning adviser without the exact-result slice of the student record.

This is an evidence boundary, not a wording classifier: restricted capabilities
are not advertised, prior web/legacy answers are not supplied as history, and
every tool result is projected before either the model or deterministic fallback
logic can use it.  The gateway's input/output checks remain defence in depth.
"""

from __future__ import annotations

import copy
import re
from typing import Any

TELEGRAM_SAFE_PROFILE = "telegram_safe"
TELEGRAM_SAFE_IDEMPOTENCY_PREFIX = "tg-safe-v1:"
TELEGRAM_UNVALIDATED_PROFILE = "telegram_unvalidated"
TELEGRAM_WITHHELD_PROFILE = "telegram_withheld"

TELEGRAM_SYSTEM_RULES = """

Telegram channel data rule:
- This channel intentionally omits exact GPA/CGPA, marks, letter grades, failed-course
  results, transcript details, and registrar academic standing.
- Never infer, reconstruct, or ask another tool for an omitted value. For a request for
  one of those exact personal results, direct the student to the authenticated web adviser.
- Continue answering planning, prerequisites, recommendations, timetable, graduation
  scenarios, adviser-contact, and general policy questions from the available evidence.
"""

# These capabilities expose a transcript-like per-course status surface. Other
# planning capabilities remain available, but their results pass through the
# field projection below.
TELEGRAM_WITHHELD_TOOLS = frozenset({"get_student_context", "my_plan_by_term"})

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:gpa|cgpa|grade|grades|mark|marks|failed|failure|failures|"
    r"transcript|academic_standing|student_status|probation)(?:$|_)",
    re.IGNORECASE,
)
_SENSITIVE_STATUS = frozenset(
    {
        "failed",
        "fail",
        "academic probation",
        "probation",
        "academic warning",
        "warning",
        "suspended",
        "dismissed",
        "graduation expected",
    }
)


def is_telegram_safe_profile(profile: Any) -> bool:
    return str(profile or "").strip().lower() == TELEGRAM_SAFE_PROFILE


def project_tool_schemas(
    schemas: list[dict[str, Any]], *, profile: Any = ""
) -> list[dict[str, Any]]:
    """Remove transcript-shaped capabilities before the model chooses a tool."""

    if not is_telegram_safe_profile(profile):
        return schemas
    return [
        schema
        for schema in schemas
        if str((schema.get("function") or {}).get("name") or "") not in TELEGRAM_WITHHELD_TOOLS
    ]


def project_tool_result(tool_name: str, result: dict[str, Any], *, profile: Any = ""):
    """Return the evidence shape permitted on this channel.

    The raw executor result remains local to the call stack. The returned copy is
    the only version appended to the agent evidence list or sent to a provider.
    """

    if not is_telegram_safe_profile(profile):
        return result
    if str(tool_name or "") in TELEGRAM_WITHHELD_TOOLS:
        return {
            "tool": str(tool_name or ""),
            "ok": False,
            "error": "This record detail is available in the authenticated web adviser.",
        }
    projected = _scrub(copy.deepcopy(result))
    if isinstance(projected, dict):
        projected["channel_record_projection"] = TELEGRAM_SAFE_PROFILE
        return projected
    return {"ok": False, "error": "Capability result could not be projected safely."}


def project_history(history: Any, *, profile: Any = "") -> Any:
    """Never feed shared web/legacy history into the Telegram generation context.

    Existing conversations can contain exact results generated on the web, and
    those values may since have changed in the database. No value matcher can
    safely redact that history. Telegram turns therefore re-read current safe
    evidence each time instead of inheriting another surface's transcript.
    """

    if not is_telegram_safe_profile(profile):
        return history
    if not isinstance(history, list):
        return []
    return [
        item
        for item in history
        if isinstance(item, dict) and item.get("channel_profile") == TELEGRAM_SAFE_PROFILE
    ]


def fallback_tool(*, profile: Any = "") -> str:
    """A safe verified fallback when a tool loop produced no usable answer."""

    return "my_progress" if is_telegram_safe_profile(profile) else "get_student_context"


def system_prompt(base_prompt: str, *, profile: Any = "") -> str:
    if not is_telegram_safe_profile(profile):
        return base_prompt
    return str(base_prompt or "").rstrip() + TELEGRAM_SYSTEM_RULES


def _scrub(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalised = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if _SENSITIVE_KEY.search(normalised):
                continue
            # Credit policy derives this block from the student's registrar
            # standing. Removing only the source status while retaining the
            # tailored conclusion would disclose the same fact indirectly.
            if normalised == "qualification":
                continue
            if normalised in {"status", "current_status"} and _status_is_sensitive(raw_value):
                continue
            clean[key] = _scrub(raw_value, parent_key=normalised)
        return clean
    if isinstance(value, list):
        return [_scrub(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_scrub(item, parent_key=parent_key) for item in value]
    return value


def _status_is_sensitive(value: Any) -> bool:
    status = re.sub(r"[_-]+", " ", str(value or "").strip().casefold())
    return status in _SENSITIVE_STATUS


__all__ = [
    "TELEGRAM_SAFE_PROFILE",
    "TELEGRAM_SAFE_IDEMPOTENCY_PREFIX",
    "TELEGRAM_SYSTEM_RULES",
    "TELEGRAM_UNVALIDATED_PROFILE",
    "TELEGRAM_WITHHELD_TOOLS",
    "TELEGRAM_WITHHELD_PROFILE",
    "fallback_tool",
    "is_telegram_safe_profile",
    "project_history",
    "project_tool_result",
    "project_tool_schemas",
    "system_prompt",
]
