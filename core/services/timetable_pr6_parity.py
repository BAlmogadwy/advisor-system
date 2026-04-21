"""
core/services/timetable_pr6_parity.py
PR6 commit 7 — flag-off semantic parity comparator.

Mirror of ``timetable_pr5_parity``. When
``TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED`` is False the optimiser result
must match the pre-PR6 payload on the *intersection* of schemas — i.e.
every pre-PR6 key retains its value / shape, and no PR6-added field
leaks through.

PR6 added exactly one top-level key: ``stage_telemetry``. This helper
drops it so byte-equality diffs against a pre-PR6 master snapshot hold
for callers that run under the kill-switch.

No PR6 field is nested inside ``decision_trace`` / ``perturbation_metric``
/ rooming payloads / scores, so nothing else needs scrubbing here.
"""

from __future__ import annotations

import copy


def strip_pr6_fields_for_parity(result: dict) -> dict:
    """Return a deep-copied view of ``result`` with PR6-added fields stripped.

    Currently removes only the top-level ``stage_telemetry`` block.
    Leaves pre-PR6 keys untouched so the comparator can diff against a
    master-branch snapshot key-by-key.
    """
    normalised = copy.deepcopy(result or {})
    normalised.pop("stage_telemetry", None)
    return normalised


__all__ = ["strip_pr6_fields_for_parity"]
