"""PR5 commit 2 — solver-pipeline trace code sentinels + flag helper.

Companion module to ``timetable_decision_trace`` (PR3) and
``timetable_room_oracle`` (PR2). Defines the four *acceptance* codes
emitted by the post-greedy stages of the V2 pipeline (SA polish,
CP-SAT polisher, chain search, rooming 2nd-pass repair) when they
materially change a placement, plus the flag helper that gates PR5's
new trace surface.

Public symbols:

- ``SA_RELOCATE_ACCEPTED`` — SA polish moved a section to a new slot or
  room. Emitted by ``timetable_local_search`` in commit 3.
- ``CPSAT_IMPROVED`` — CP-SAT polisher re-placed a section as part of a
  strictly-better joint solution. Emitted by
  ``timetable_cpsat_polisher`` in commit 4. The entry's
  ``stage_context`` MUST carry ``previous_slot``.
- ``CHAIN_ROTATED`` — chain-search moved a section as part of a
  multi-section rotation. Emitted by
  ``timetable_local_search_chains`` in commit 5. Context carries
  ``chain_length`` (>=2) and ``chain_id``.
- ``ROOMING_REPAIR_REASSIGNED`` — rooming 2nd-pass repaired an
  UNASSIGNED without moving the slot. Emitted by
  ``timetable_rooming.assign_rooms_to_board`` in commit 6. Context
  carries ``previous_room`` (always ``"UNASSIGNED"``) and ``new_room``.
- ``is_stage_trace_enabled()`` — reads
  ``settings.TIMETABLE_PR5_STAGE_TRACE_ENABLED``. Default ``False`` in
  commits 2–7 (new surface is off by default during development);
  flipped to ``True`` at commit 8.

No rejection codes.
PR5 amendment 1 (ChatGPT round 1, 2026-04-20) dropped
``SA_RELOCATE_REJECTED`` from scope: rejected SA moves are
optimisation internals, not user-facing provenance. If ever needed,
they ship as a separate debug surface outside ``decision_trace``.
Re-adding a rejection code here without a DoR amendment is a scope
regression — the PR5 contract test
``TestSolverCodesContract::test_no_sa_relocate_rejected_in_module``
guards against accidental re-introduction.

``stage_origin`` semantic rule (amendment 3):
``stage_origin`` on a ``DecisionTrace`` entry names the stage that
**last** changed the chosen placement currently recorded. Not "a stage
that touched it at some point" — the final stage responsible for the
current location. Each of commits 3/4/5/6 updates this field when it
moves a section.
"""

from __future__ import annotations

from django.conf import settings

# ---------------------------------------------------------------------------
# Acceptance-code sentinels.
#
# Renaming any of these is a breaking change the moment commit 2 lands —
# the strings end up in captured traces written to ``result_json`` on
# every V2 scenario run.
# ---------------------------------------------------------------------------

SA_RELOCATE_ACCEPTED = "SA_RELOCATE_ACCEPTED"
CPSAT_IMPROVED = "CPSAT_IMPROVED"
CHAIN_ROTATED = "CHAIN_ROTATED"
ROOMING_REPAIR_REASSIGNED = "ROOMING_REPAIR_REASSIGNED"

STAGE_TRACE_ENABLED_SETTING = "TIMETABLE_PR5_STAGE_TRACE_ENABLED"


def is_stage_trace_enabled() -> bool:
    """Return whether PR5 stage-trace provenance population is active.

    Reads ``settings.TIMETABLE_PR5_STAGE_TRACE_ENABLED``. Default
    ``False`` in commits 2–7; commit 8 flips the env default to
    ``"true"``. Production can flip via the env var without a redeploy.

    When ``False``:
    - ``stage_origin`` on fresh ``DecisionTrace`` entries still has its
      dataclass default of ``"greedy"`` (the field itself cannot be
      removed without breaking readers, amendment 4).
    - Commits 3–6 skip their emission code paths entirely — no new
      codes appear, no existing ``stage_origin`` values are rewritten,
      ``perturbation_metric.changes_by_stage`` stays absent.

    That combination is what acceptance bar #6 (semantic flag-off
    parity) enforces: the pre-PR5 subset of payload fields is byte-
    identical to the PR4 master baseline.
    """
    return bool(getattr(settings, STAGE_TRACE_ENABLED_SETTING, False))


__all__ = [
    "SA_RELOCATE_ACCEPTED",
    "CPSAT_IMPROVED",
    "CHAIN_ROTATED",
    "ROOMING_REPAIR_REASSIGNED",
    "STAGE_TRACE_ENABLED_SETTING",
    "is_stage_trace_enabled",
]
