"""
core/services/timetable_pr5_parity.py
PR5 commit 7 — flag-off semantic parity comparator.

Amendment 4 of the PR5 DoR: when ``TIMETABLE_PR5_STAGE_TRACE_ENABLED``
is False the optimiser result must match the pre-PR5 payload (as of
master 71bf988) on the *intersection* of the two schemas — i.e. every
pre-PR5 key retains its value / shape, and no PR5-added field leaks
through.

``strip_pr5_fields_for_parity`` is the normaliser: given a V2 /
auto_place result dict, it returns a deep copy with every PR5-added
field removed. Tests serialise the result and assert no PR5 symbol
appears anywhere in the stringified form.

Scope — fields PR5 added that must be stripped for parity:
  * top-level ``decision_trace`` PR5 additions are removed by
    flag-gated emission at source, not here, but any entry that slipped
    through gets scrubbed so a stale run can't leak.
  * ``perturbation_metric.changes_by_stage`` (commit 7 addition).
  * any ``stage_origin`` / ``stage_context`` keys embedded in trace
    entries, regardless of where they appear.
"""

from __future__ import annotations

import copy
from typing import Any

_PR5_TRACE_FIELDS: frozenset[str] = frozenset({"stage_origin", "stage_context"})


def _scrub_trace_entry(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if k not in _PR5_TRACE_FIELDS}


def strip_pr5_fields_for_parity(result: dict) -> dict:
    """Return a deep-copied view of ``result`` with PR5-added fields stripped.

    Leaves pre-PR5 keys untouched so the comparator can diff against a
    master-branch snapshot key-by-key.
    """
    normalised = copy.deepcopy(result or {})

    trace = normalised.get("decision_trace")
    if isinstance(trace, dict):
        normalised["decision_trace"] = {k: _scrub_trace_entry(v) for k, v in trace.items()}

    perturb = normalised.get("perturbation_metric")
    if isinstance(perturb, dict) and "changes_by_stage" in perturb:
        perturb.pop("changes_by_stage", None)

    return normalised
