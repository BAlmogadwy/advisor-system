"""PR8 — async job UX shim.

Python-side helpers consumed by ``scenario_detail`` / related templates:

- flag gating (``is_async_job_ui_enabled`` / ``is_async_job_ui_effective``)
- endpoint map so templates don't hardcode URLs
- status-pill class lookup
- polling cadence constant (2s — documented in DoR §2)
- terminal-status helper
- JS adapter static path
- scripted progression snapshots for acceptance rendering

No Django view / model imports here — PR8 is UI orchestration only,
it consumes PR7 endpoints unchanged.
"""

from __future__ import annotations

from typing import Any

PR7_FLAG = "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED"
PR8_FLAG = "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED"

JS_ADAPTER_PATH = "core/js/pr8_async_job_adapter.js"

POLL_INTERVAL_MS = 2000

_TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
_ACTIVE: frozenset[str] = frozenset({"queued", "running"})

_PILL_CLASSES: dict[str, str] = {
    "queued": "status-queued",
    "running": "status-running",
    "succeeded": "status-succeeded",
    "failed": "status-failed",
    "cancelled": "status-cancelled",
}


def is_async_job_ui_enabled() -> bool:  # back-compat re-export
    from core.services.timetable_flags import is_async_job_ui_enabled as _impl

    return _impl()


def is_async_planner_enabled() -> bool:  # back-compat re-export
    from core.services.timetable_flags import is_async_planner_enabled as _impl

    return _impl()


def is_async_job_ui_effective() -> bool:  # back-compat re-export
    from core.services.timetable_flags import is_async_job_ui_effective as _impl

    return _impl()


def is_terminal_status(status: str | None) -> bool:
    return (status or "") in _TERMINAL


def is_active_status(status: str | None) -> bool:
    return (status or "") in _ACTIVE


def status_pill_class(status: str | None) -> str:
    return _PILL_CLASSES.get(status or "", "status-unknown")


def endpoint_map() -> dict[str, str]:
    """The four PR7 endpoints the JS adapter calls.

    Static URLs — no templated `{url}` parts because PR7 views use
    ``<uuid:job_id>`` path converters that only land once a job id is
    known client-side.
    """
    return {
        "submit": "/planner-jobs/",
        "poll": "/planner-jobs/{job_id}/",
        "result": "/planner-jobs/{job_id}/result/",
        "cancel": "/planner-jobs/{job_id}/cancel/",
    }


def render_progression_snapshot(kind: str) -> list[dict[str, Any]]:
    """Scripted status progressions for the c7 acceptance pack.

    Returns the sequence of ``(status, last_stage_seen)`` frames a
    well-behaved PR7 job would emit through to the given terminal
    kind. UI tests use these to assert that each frame renders
    without breaking.
    """
    base: list[dict[str, Any]] = [
        {"status": "queued", "last_stage_seen": None},
        {"status": "running", "last_stage_seen": "greedy"},
    ]
    tail_by_kind: dict[str, list[dict[str, Any]]] = {
        "happy": [{"status": "succeeded", "last_stage_seen": "rooming_repair"}],
        "failed": [{"status": "failed", "last_stage_seen": "greedy", "error_message": "synthetic"}],
        "cancelled": [{"status": "cancelled", "last_stage_seen": "greedy"}],
    }
    if kind not in tail_by_kind:
        raise ValueError(f"unknown progression kind: {kind!r}")
    return base + tail_by_kind[kind]


__all__ = [
    "JS_ADAPTER_PATH",
    "POLL_INTERVAL_MS",
    "PR7_FLAG",
    "PR8_FLAG",
    "endpoint_map",
    "is_active_status",
    "is_async_job_ui_effective",
    "is_async_job_ui_enabled",
    "is_async_planner_enabled",
    "is_terminal_status",
    "render_progression_snapshot",
    "status_pill_class",
]
