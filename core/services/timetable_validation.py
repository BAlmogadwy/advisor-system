"""Placement-legality validator + slot-grid prayer-compliance guard.

Narrow, pure helpers used by the planner. ``lock_rejection`` /
``validate_candidate`` return ``RejectionReason | None``; ``None`` means
"no rejection" (either the rule is disabled by its flag, or the candidate
is compliant).

Prayer compliance is NOT a runtime placement rule — the planner uses fixed
slot grids, so it is a property of the grid checked once via
``assert_slot_grid_prayer_compliant`` (no lecture starts 11:30-12:59, no lab
starts 11:10-12:59). ``blocked_slot_keys`` builds the shared blocked-cell
exclusion set every placement stage consults.

Interval semantics:

    Meeting and locked windows are half-open intervals ``[start, end)``.
    Overlap is ``a.start < b.end AND a.end > b.start``.
    Exact boundary touch (e.g. one ends 12:00 and the next starts 12:00)
    is legal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings

# Rejection code sentinels — stable strings for payload consumers.
LOCK_RESPECT = "LOCK_RESPECT"


@dataclass(frozen=True)
class RejectionReason:
    """A rejection emitted by a placement-legality validator.

    Stable, JSON-serialisable shape so the planner return payload can
    carry a list of these directly. ``context`` carries optional extra
    detail (e.g. the locked cell that collided) for downstream logging or
    future dashboard surfaces.
    """

    code: str
    day: str
    start_time: str
    end_time: str
    course_code: str = ""
    context: dict | None = None

    def to_dict(self) -> dict:
        payload: dict[str, object] = {
            "reason": self.code,
            "day": self.day,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "course_code": self.course_code,
        }
        if self.context is not None:
            payload["context"] = self.context
        return payload


# ---------------------------------------------------------------------------
# Flag reads
# ---------------------------------------------------------------------------


def is_lock_enforcement_enabled() -> bool:
    return bool(getattr(settings, "TIMETABLE_ENFORCE_LOCKS", False))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _to_min(t: str) -> int:
    """Parse a ``HH:MM`` time string to minutes-since-midnight.

    Raises ``ValueError`` with a useful message on malformed input. This
    helper is used from planner paths, so failure-mode clarity matters —
    a clean exception beats a generic ``int()`` parse failure.
    """
    if not isinstance(t, str) or ":" not in t:
        raise ValueError(f"_to_min expected 'HH:MM' string, got {t!r}")
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"_to_min expected 'HH:MM' string, got {t!r}")
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"_to_min could not parse time components in {t!r}") from exc
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"_to_min got out-of-range hour/minute in {t!r}")
    return h * 60 + m


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """Half-open interval overlap: ``a.start < b.end AND a.end > b.start``.

    Exact boundary touch returns False (legal). Generic time-interval helper
    reused by the planner (e.g. ``auto_place_board``).
    """
    return _to_min(a_start) < _to_min(b_end) and _to_min(a_end) > _to_min(b_start)


def _same_day(a: str, b: str) -> bool:
    """Day codes compared case-insensitively.

    The autoplacer emits uppercase codes (``SUN``, ``MON``, ...) while
    locked cells may come from human input (``Sun``, ``Mon``). Normalise
    both sides so the rule does not silently miss a match.
    """
    return (a or "").strip().upper() == (b or "").strip().upper()


# ---------------------------------------------------------------------------
# Lock rule — direct-collision detection only in PR1 commit 2. Planner
# enforcement (preload + skip) lands in commit 4/8. Until then this is
# telemetry / candidate-collision detection, NOT the enforcement mechanism.
# ---------------------------------------------------------------------------


def lock_rejection(candidate: dict, locked_cells: Iterable[dict]) -> RejectionReason | None:
    """Return a ``LOCK_RESPECT`` rejection if the candidate collides with
    a locked cell (same day + start_time + room), else ``None``.

    In PR1 commit 2 this detects direct collisions only; planner enforcement
    comes from preloading locked placements into occupancy and skipping them
    in automatic placement (commit 4/8). This helper's output is telemetry
    and candidate-gen filtering — it does not by itself prevent the planner
    from emitting a colliding placement.
    """
    if not is_lock_enforcement_enabled():
        return None
    day = candidate["day"]
    c_start = candidate["start_time"]
    c_room = candidate.get("room", "")
    if not c_room:
        return None
    for cell in locked_cells:
        if (
            _same_day(cell.get("day", ""), day)
            and cell.get("start_time") == c_start
            and cell.get("room") == c_room
        ):
            return RejectionReason(
                code=LOCK_RESPECT,
                day=day,
                start_time=c_start,
                end_time=candidate.get("end_time", ""),
                course_code=str(candidate.get("course_code", "")),
                context={"locked_room": c_room},
            )
    return None


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def validate_candidate(
    candidate: dict,
    context: dict,
) -> list[RejectionReason]:
    """Run every applicable PR1 rule against a candidate placement.

    ``context`` shape::

        {
            "locked_cells": [{day, start_time, room}, ...],
        }

    Returns a list of ``RejectionReason`` (empty list if the candidate
    passes every enabled rule).
    """
    reasons: list[RejectionReason] = []
    locked = context.get("locked_cells", [])

    lr = lock_rejection(candidate, locked)
    if lr is not None:
        reasons.append(lr)

    return reasons


# ---------------------------------------------------------------------------
# Slot-grid prayer compliance (source-of-truth guard)
# ---------------------------------------------------------------------------
#
# The planner uses fixed slot grids. Prayer compliance is therefore a property
# of the GRID, checked once when a grid is resolved, rather than a per-meeting
# runtime rule re-evaluated by every placement stage. The institutional rule:
#
#   - no lecture may START in 11:30-12:59 (inclusive)
#   - no lab     may START in 11:10-12:59 (inclusive)
#
# The default grids satisfy this by construction; this guard only fires if a
# scenario's ``slot_config`` / ``lab_slot_config`` is hand-edited to a
# non-compliant grid.


def blocked_slot_keys(blocked_slots: Iterable[dict] | None) -> set[tuple[str, str]]:
    """Return the ``{(day, start)}`` exclusion set for a scenario's blocked slots.

    Single source of truth for the blocked-cell domain exclusion. Built with
    RAW membership (no case-folding) so every placement stage — the greedy
    enumerator ``_generate_meeting_options``, the canonical pattern catalog,
    the per-board CP-SAT solver, the polisher, load-balanced and SA — excludes
    exactly the same cells. ``blocked_slots`` entries are ``{day, start}`` dicts
    (``TimetableScenario.blocked_slots``).
    """
    return {(bs.get("day", ""), bs.get("start", "")) for bs in (blocked_slots or [])}


LECTURE_PRAYER_BLOCK = ("11:30", "12:59")
LAB_PRAYER_BLOCK = ("11:10", "12:59")


class SlotGridPrayerError(ValueError):
    """Raised when a slot grid would start a class inside a prayer window."""


def _starts_in_window(window: tuple[str, str], start: str) -> bool:
    return _to_min(window[0]) <= _to_min(start) <= _to_min(window[1])


def assert_slot_grid_prayer_compliant(
    slot_config: Iterable[dict] | None,
    lab_slot_config: Iterable[dict] | None,
) -> None:
    """Raise ``SlotGridPrayerError`` if any slot starts inside its prayer window.

    Lectures are checked against ``LECTURE_PRAYER_BLOCK`` and labs against
    ``LAB_PRAYER_BLOCK``. A ``None``/empty grid is vacuously compliant.
    """
    offenders: list[str] = []
    for slot in slot_config or []:
        start = slot.get("start", "")
        if start and _starts_in_window(LECTURE_PRAYER_BLOCK, start):
            offenders.append(f"lecture slot {slot.get('label', start)!r} starts at {start}")
    for slot in lab_slot_config or []:
        start = slot.get("start", "")
        if start and _starts_in_window(LAB_PRAYER_BLOCK, start):
            offenders.append(f"lab slot {slot.get('label', start)!r} starts at {start}")
    if offenders:
        raise SlotGridPrayerError(
            "slot grid violates prayer windows (lecture 11:30-12:59, lab 11:10-12:59): "
            + "; ".join(offenders)
        )
