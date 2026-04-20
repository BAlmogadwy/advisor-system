"""PR3 commit 2 — decision-trace contract module.

Defines the frozen dataclasses, rejection-code sentinels, and flag helper
that commits 3–8 will wire into the planner. This module lands ahead of
any capture logic so commit 1's tripwire tests (``tests/test_pr3_decision_trace.py``
Section A + ``tests/test_pr3_warm_start.py`` imports) can collect and
pass without any behaviour change in the planner.

Public symbols:

- ``DecisionTrace`` — one per placed section, captured by ``auto_place_board``
  from commit 3 onwards. Holds the chosen slot + up to 3 alternatives.
- ``Alternative`` — a single rejected candidate slot, tagged with a
  rejection-code sentinel so a registrar can see *why* the planner chose
  the slot it did over this one.
- ``INSTRUCTOR_CLASH`` / ``STUDENT_CONFLICT`` — the two new PR3 rejection
  sentinels, joining the PR1 (``PRAYER_OVERLAP``, ``LOCK_RESPECT`` — the
  actual strings emitted by ``timetable_validation``; the DoR alphabet
  uses the aspirational ``PRAYER_WINDOW_CLASH`` / ``LOCK_VIOLATION``
  names which never landed in PR1 code — renaming PR1 codes is out of
  scope for PR3) and PR2 (``NO_ROOM_*``, ``ROOM_*``) sets.
  ``STUDENT_CONFLICT`` is
  named *conflict* — not *overlap* — because it captures cohort
  semantics (≥1 shared student enrolled in another section at this slot),
  not geometric time-overlap. DoR sign-off amendment A.
- ``is_decision_trace_enabled()`` — reads ``TIMETABLE_PR3_DECISION_TRACE_ENABLED``.
  Default ``True`` from commit 2 onwards: capture is observational and
  safe to enable immediately. When ``False`` the planner still emits
  ``decision_trace={}`` in the payload for schema stability (DoR
  amendment: schema-stability clause).

Expected ``rejection_context`` keys by code (``Alternative.rejection_context``
is typed ``dict[str, Any]``; keys below are populated by commit 3's capture
code and left empty otherwise):

- ``INSTRUCTOR_CLASH``: ``clashing_section`` (``"<course>|<section>"``),
  ``clashing_instructor_id``.
- ``STUDENT_CONFLICT``: ``clashing_section``, ``shared_student_count``.
- ``PRAYER_OVERLAP``: ``prayer_window`` (e.g. ``"dhuhr"``).
- ``LOCK_RESPECT``: ``locked_section``.
- ``NO_ROOM_CAPACITY`` / ``NO_ROOM_GENDER`` / ``NO_ROOM_TYPE``: no extra
  keys — the candidate slot itself carries the day/time/room tried.
- ``ROOM_OCCUPIED``: ``occupying_section``.
- ``ROOM_BUFFER_REJECT``: ``raw_demand``, ``buffered_demand``, ``capacity``.
- ``ROOM_HEURISTIC_MISMATCH``: ``heuristic``, ``is_lab_course``.

Typing is intentionally loose (``dict[str, Any]``) — pinning a TypedDict
per code would balloon PR3 scope. Stable key names are the contract; the
docstring is the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

# ---------------------------------------------------------------------------
# Rejection-code sentinels (PR3-new).
#
# The two new codes added in PR3. Renaming either is a breaking change
# the moment commit 2 lands — every captured trace from commit 3 onwards
# writes these strings into ``Alternative.rejection_code``.
# ---------------------------------------------------------------------------

INSTRUCTOR_CLASH = "INSTRUCTOR_CLASH"
STUDENT_CONFLICT = "STUDENT_CONFLICT"


@dataclass(frozen=True)
class Alternative:
    """A single rejected candidate slot for a placed section.

    Emitted as part of a ``DecisionTrace``. Up to 3 alternatives per
    placed section (fixed cap, not configurable — DoR §trace-capture-scope).

    Fields:

    - ``day`` / ``start_time`` / ``end_time`` / ``room`` — the candidate
      slot that was considered and rejected.
    - ``rejection_code`` — one of the PR1+PR2+PR3 sentinels. No invented
      vague labels (acceptance bar #2).
    - ``rejection_context`` — code-specific detail bag; see the module
      docstring for the canonical key set per code. Loose typing
      (``dict[str, Any]``) so the emit site can add keys without
      dataclass schema churn.
    """

    day: str
    start_time: str
    end_time: str
    room: str
    rejection_code: str
    rejection_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "room": self.room,
            "rejection_code": self.rejection_code,
            "rejection_context": dict(self.rejection_context),
        }


@dataclass(frozen=True)
class DecisionTrace:
    """One placed section's chosen slot + top-N alternatives.

    Fields:

    - ``section_code`` — ``"<course>|<section>"`` (matches PR1 placement
      dict shape).
    - ``course_code`` — denormalised for downstream consumers that don't
      want to re-split ``section_code``.
    - ``chosen_day`` / ``chosen_start_time`` / ``chosen_end_time`` /
      ``chosen_room`` — flat fields. Kept flat (not wrapped in a
      ``ChosenSlot`` sub-dataclass) to keep ``to_dict()`` shape close
      to the existing planner result payload.
    - ``alternatives`` — tuple (not list) so the dataclass stays
      hashable / frozen-safe. Up to 3 entries, ordered by score rank
      (best rejected first).
    - ``stage_origin`` (PR5 commit 2) — which pipeline stage last
      changed this chosen placement. One of ``"greedy"``, ``"sa"``,
      ``"cpsat"``, ``"chain"``, ``"rooming_repair"``. Default
      ``"greedy"`` preserves PR3 behaviour at the per-entry level:
      consumers that don't read the field still see the old shape.
      Semantic rule (PR5 DoR amendment 3): "the stage that **last**
      changed the chosen placement currently recorded in this trace" —
      each of PR5 commits 3/4/5/6 updates this field when it moves a
      section. Deliberately NOT on ``Alternative``: alternatives remain
      greedy-era artefacts.
    - ``stage_context`` (PR5 commit 2) — code-specific detail bag keyed
      by the acceptance code that caused the last move. Canonical keys
      per PR5 code live in ``timetable_solver_codes``:
      ``previous_slot`` (CPSAT_IMPROVED), ``chain_length`` / ``chain_id``
      (CHAIN_ROTATED), ``previous_room`` / ``new_room``
      (ROOMING_REPAIR_REASSIGNED), ``from_slot`` / ``to_slot`` /
      ``cost_delta`` (SA_RELOCATE_ACCEPTED). Empty when
      ``stage_origin == "greedy"``.
    """

    section_code: str
    course_code: str
    chosen_day: str
    chosen_start_time: str
    chosen_end_time: str
    chosen_room: str
    alternatives: tuple[Alternative, ...] = ()
    stage_origin: str = "greedy"
    stage_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_code": self.section_code,
            "course_code": self.course_code,
            "chosen_day": self.chosen_day,
            "chosen_start_time": self.chosen_start_time,
            "chosen_end_time": self.chosen_end_time,
            "chosen_room": self.chosen_room,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "stage_origin": self.stage_origin,
            "stage_context": dict(self.stage_context),
        }


def is_decision_trace_enabled() -> bool:
    """Return whether PR3 decision-trace capture is on.

    Reads ``settings.TIMETABLE_PR3_DECISION_TRACE_ENABLED``. Default
    ``True`` from commit 2 onwards: trace capture is observational and
    does not change any planning decision, so it is safe to default-on
    immediately. Production can disable via
    ``TIMETABLE_PR3_DECISION_TRACE_ENABLED=false`` if a regression
    appears — when disabled, commit 3's capture code is skipped but the
    planner payload still includes ``decision_trace={}`` for schema
    stability (DoR sign-off amendment).
    """
    return bool(getattr(settings, "TIMETABLE_PR3_DECISION_TRACE_ENABLED", True))
