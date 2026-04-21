"""
core/services/timetable_stage_summary.py
PR5 commit 7 — standalone ``changes_by_stage`` aggregator.

Derived purely from ``decision_trace`` + the set of section codes that
actually changed from baseline. No optimiser state is reached into — the
helper is intentionally decoupled so tests and management commands can
call it directly without spinning up a full pipeline run.

Semantic model (ChatGPT commit-7 ruling):

* keys: ``greedy``, ``sa``, ``cpsat``, ``chain``, ``rooming_repair`` —
  all five are always present, zero-valued by default, so the schema
  is stable across feature flags and scenario shapes.
* value: number of sections whose **final** ``stage_origin`` equals that
  key AND whose section_code appears in the changed-from-baseline set.
* invariant: ``sum(changes_by_stage.values()) == changes_from_baseline_count``
  enforced by callers (e.g. V2 populates both fields from the same
  run and asserts the equality in tests).

Last-changer-wins is already encoded in the decision_trace overlay —
this helper does NOT re-resolve origin; it trusts what the trace says.
"""

from __future__ import annotations

from core.services.timetable_stage_telemetry import STAGE_KEYS as _STAGE_KEYS


def empty_changes_by_stage() -> dict[str, int]:
    """Return a fresh zero-filled bucket. Schema-stable sentinel."""
    return {k: 0 for k in _STAGE_KEYS}


def compute_changes_by_stage(
    decision_trace: dict[str, dict],
    changed_section_codes: set[str] | None = None,
) -> dict[str, int]:
    """Bucket the decision_trace entries by their final ``stage_origin``.

    Parameters
    ----------
    decision_trace:
        ``{section_code: entry_dict}`` as assembled by the V2 overlay
        (last-changer-wins already applied).
    changed_section_codes:
        Optional set of section codes that contribute to
        ``changes_from_baseline_count``. If provided, entries outside
        this set are ignored so the invariant
        ``sum == changes_from_baseline_count`` holds. If ``None``, every
        trace entry is counted (useful for pipeline-level summaries that
        don't care about baseline framing — e.g. the acceptance CLI).
    """
    bucket = empty_changes_by_stage()
    for section_code, entry in (decision_trace or {}).items():
        if changed_section_codes is not None and section_code not in changed_section_codes:
            continue
        origin = (entry or {}).get("stage_origin") or "greedy"
        if origin in bucket:
            bucket[origin] += 1
    return bucket
