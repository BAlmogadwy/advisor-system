"""PR8 — parity helper.

Strips PR8-introduced template-context keys so byte-equality checks
against pre-PR8 scenario-page renders hold. Planner result semantics
are never touched — this is a context-shaping helper only.
"""

from __future__ import annotations

from typing import Any

_PR8_CONTEXT_KEYS: tuple[str, ...] = (
    "pr8_job",
    "pr8_config_json",
    "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED",
)


def strip_pr8_ui_context(ctx: dict[str, Any]) -> dict[str, Any]:
    out = dict(ctx)
    for key in _PR8_CONTEXT_KEYS:
        out.pop(key, None)
    return out


__all__ = ["strip_pr8_ui_context"]
