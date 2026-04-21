"""PR7 commit 7 — flag-off parity helper.

Strips PR7-introduced async/job fields from a planner result payload so
byte-equality comparisons against pre-PR7 snapshots hold. Mirrors the
``timetable_pr6_parity`` / ``timetable_pr5_parity`` pattern.

PR7 did not introduce any new keys inside the planner *result* payload
itself — all new surface area (``PlannerJob`` row, REST endpoints,
``last_stage_seen``, ``cancel_requested``) lives around the result, not
inside it. So this helper is a conservative identity-preserving scrub:
it copies the payload, removes any forward-compatibility keys the PR7
runner might have attached at the top level, and leaves the PR6 shape
(``placements``, ``decision_trace``, ``stage_telemetry``, ``perturbation_metric``,
``changes_by_stage``, etc.) untouched.

Kept as a first-class module so consumers have a stable import path
regardless of whether future PR7 follow-ups start annotating payloads.
"""

from __future__ import annotations

from typing import Any

_PR7_RESULT_KEYS: tuple[str, ...] = (
    "planner_job_id",
    "last_stage_seen",
    "cancel_requested",
    "async_dispatch_metadata",
)


def strip_pr7_fields_for_parity(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` with PR7-added keys removed.

    Planner result semantics (placements, scoring, telemetry) are never
    touched — this helper only scrubs around the edges.
    """
    stripped = dict(payload)
    for key in _PR7_RESULT_KEYS:
        stripped.pop(key, None)
    return stripped


__all__ = ["strip_pr7_fields_for_parity"]
