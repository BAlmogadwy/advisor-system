"""PR1 — placement-legality validator for prayer/lock enforcement.

Narrow, pure helpers used by the auto-placer and the V2 candidate-gen
filter. Each helper returns ``RejectionReason | None``; ``None`` means
"no rejection" (either the rule is disabled by its flag, or the candidate
is compliant).

Call sites:

- ``core.services.timetable_autoplace.auto_place_board`` — invokes the
  prayer and lock helpers before finalising a slot.
- ``core.services.timetable_optimizer_v2.build_section_states_for_scenario``
  — calls the same helpers as a candidate-gen filter.

Call sites explicitly EXCLUDED:

- ``core.services.timetable_rooming.assign_rooms_to_board`` — rooming runs
  after placement legality has already been decided; it may carry forward
  upstream rejection metadata but must not enforce placement-legality
  rules.

Interval semantics:

    Meeting and prayer windows are half-open intervals ``[start, end)``.
    Overlap is ``a.start < b.end AND a.end > b.start``.
    Exact boundary touch (e.g. meeting ends 12:00 and prayer starts 12:00)
    is legal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings

# Rejection code sentinels — stable strings for payload consumers.
PRAYER_OVERLAP = "PRAYER_OVERLAP"
LOCK_RESPECT = "LOCK_RESPECT"


@dataclass(frozen=True)
class RejectionReason:
    """A rejection emitted by a PR1 validator.

    Stable, JSON-serialisable shape so the planner return payload can
    carry a list of these directly. ``context`` carries optional extra
    detail (e.g. the prayer window that overlapped, the locked cell that
    collided) for downstream logging or future dashboard surfaces.
    """

    code: str
    day: str
    start_time: str
    end_time: str
    course_code: str = ""
    context: dict | None = None

    def to_dict(self) -> dict:
        payload = {
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


def is_prayer_overlap_rule_enabled() -> bool:
    return bool(getattr(settings, "TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE", False))


def is_lock_enforcement_enabled() -> bool:
    return bool(getattr(settings, "TIMETABLE_ENFORCE_LOCKS", False))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """Half-open interval overlap: ``a.start < b.end AND a.end > b.start``.

    Exact boundary touch returns False (legal).
    """
    return _to_min(a_start) < _to_min(b_end) and _to_min(a_end) > _to_min(b_start)


# ---------------------------------------------------------------------------
# Prayer-overlap rule
# ---------------------------------------------------------------------------


def prayer_overlap_rejection(
    meeting: dict, prayer_windows: Iterable[dict]
) -> RejectionReason | None:
    """Return a ``PRAYER_OVERLAP`` rejection if ``meeting`` overlaps any
    same-day prayer window, else ``None``.

    The flag ``TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE`` gates enforcement —
    when disabled, this always returns ``None``.

    ``meeting`` shape: ``{day, start_time, end_time, course_code?}``
    ``prayer_windows`` entry shape: ``{day, start_time, end_time}``
    """
    if not is_prayer_overlap_rule_enabled():
        return None
    day = meeting["day"]
    m_start = meeting["start_time"]
    m_end = meeting["end_time"]
    for prayer in prayer_windows:
        if prayer.get("day") != day:
            continue
        if _overlaps(m_start, m_end, prayer["start_time"], prayer["end_time"]):
            return RejectionReason(
                code=PRAYER_OVERLAP,
                day=day,
                start_time=m_start,
                end_time=m_end,
                course_code=str(meeting.get("course_code", "")),
                context={
                    "prayer_start": prayer["start_time"],
                    "prayer_end": prayer["end_time"],
                },
            )
    return None


# ---------------------------------------------------------------------------
# Lock rule — full implementation lands in commit 4. Stub for now so
# imports in the lock + e2e tests work and the flag-off / empty-lock
# paths pass.
# ---------------------------------------------------------------------------


def lock_rejection(candidate: dict, locked_cells: Iterable[dict]) -> RejectionReason | None:
    """Return a ``LOCK_RESPECT`` rejection if the candidate collides with
    a locked cell (same day + start_time + room), else ``None``.

    This is a skeleton with the final semantics to be completed in
    commit 4/8 alongside the preload + skip wiring. The current
    implementation already emits rejections correctly for direct
    collisions; commit 4 adds the preload path and the auto_place_board
    wiring so the rule is structurally enforced (not just telemetry).
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
            cell.get("day") == day
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
            "prayer_windows": [{day, start_time, end_time}, ...],
            "locked_cells":   [{day, start_time, room}, ...],
        }

    Returns a list of ``RejectionReason`` (empty list if the candidate
    passes every enabled rule). Rules are evaluated independently — a
    candidate can accumulate multiple rejection reasons on a single call
    (e.g. violates both prayer and lock).
    """
    reasons: list[RejectionReason] = []
    prayers = context.get("prayer_windows", [])
    locked = context.get("locked_cells", [])

    pr = prayer_overlap_rejection(candidate, prayers)
    if pr is not None:
        reasons.append(pr)

    lr = lock_rejection(candidate, locked)
    if lr is not None:
        reasons.append(lr)

    return reasons
