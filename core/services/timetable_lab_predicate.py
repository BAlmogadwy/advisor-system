"""PR4 commit 6 — centralised lab-room predicate.

Replaces the ``duration > 80`` literal that drifted between planner
(``timetable_autoplace``), room oracle (``timetable_room_oracle``), and
export/rooming (``timetable_rooming``) with a single authoritative
helper so all three sites classify the same meeting identically.

Public symbols:

- ``meeting_requires_lab_room(meeting_or_duration) -> bool`` — the
  single source of truth. Duration-only predicate: returns True iff
  the meeting's duration is at least 80 minutes. The ``>= 80`` boundary
  matches the oracle's original intent (see PR2 notes); the prior
  ``> 80`` strict comparison excluded boundary-length meetings from the
  lab pool by accident. Accepts either a meeting-like object with a
  ``.duration`` property / attribute, a raw integer duration in
  minutes, or a meeting with ``.start_time`` / ``.end_time`` strings.

- ``is_lab_heuristic_unified()`` — flag helper reading
  ``TIMETABLE_LAB_HEURISTIC_UNIFIED``. Default ``False`` until commit 8
  flips it. Lets the three call-sites opt into the unified helper
  while staying on their existing literal behaviour when the flag is
  off — gives the promotion note a clean kill-switch.

- ``LAB_HEURISTIC_UNIFIED_FLAG_SETTING`` — the setting-name constant
  (``"TIMETABLE_LAB_HEURISTIC_UNIFIED"``) so callers don't hard-code
  the string in multiple places.

The helper is intentionally NOT credit-hour-aware: PR4 scope (A4) is
to make the three call-sites agree on a duration boundary, not to
re-litigate the ``cr == 4`` gate. A duration-only predicate collapses
the three literals cleanly; credit-hour semantics, if still
desirable, is a follow-up layer above this helper rather than a
parameter inside it.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

LAB_HEURISTIC_UNIFIED_FLAG_SETTING = "TIMETABLE_LAB_HEURISTIC_UNIFIED"

_LAB_DURATION_MINUTES = 80


def is_lab_heuristic_unified() -> bool:
    """Reads ``TIMETABLE_LAB_HEURISTIC_UNIFIED`` from Django settings.

    Default ``False`` through commits 6–7; commit 8 flips to ``True``.
    """
    return bool(getattr(settings, LAB_HEURISTIC_UNIFIED_FLAG_SETTING, False))


def _coerce_duration(meeting_or_duration: Any) -> int:
    """Extract a minute-count from any of the shapes the helper accepts.

    Accepted shapes, in priority order:

    1. An ``int`` (raw minutes).
    2. Any object exposing a ``duration`` attribute/property that is an
       int (``_StubMeeting`` in tests, future model-level properties).
    3. Any object exposing ``start_time`` / ``end_time`` strings in
       ``HH:MM`` form — covers ``TermSectionMeeting`` and the planner's
       in-memory ``m`` dicts after being wrapped by the caller.

    Raises ``TypeError`` on shapes that match none of the above; callers
    are internal code paths so a hard error is preferable to silently
    defaulting to ``False``.
    """
    if isinstance(meeting_or_duration, bool):
        raise TypeError("meeting_requires_lab_room expected a meeting or duration, got bool")
    if isinstance(meeting_or_duration, int):
        return meeting_or_duration
    duration = getattr(meeting_or_duration, "duration", None)
    if isinstance(duration, int):
        return duration
    start = getattr(meeting_or_duration, "start_time", None)
    end = getattr(meeting_or_duration, "end_time", None)
    if isinstance(start, str) and isinstance(end, str):
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)
    raise TypeError(
        f"meeting_requires_lab_room could not extract duration from "
        f"{type(meeting_or_duration).__name__}"
    )


def meeting_requires_lab_room(meeting_or_duration: Any) -> bool:
    """Return True iff the meeting's duration is >= 80 minutes.

    This is the single authoritative predicate for "does this meeting
    need a lab room?". The 80-minute boundary is inclusive, matching
    the oracle's original intent documented in PR2 notes. The old
    ``> 80`` strict comparison that lived inline at each call-site
    excluded exactly 80-minute meetings from the lab pool — a silent
    off-by-one that commit 6 closes.
    """
    duration = _coerce_duration(meeting_or_duration)
    return duration >= _LAB_DURATION_MINUTES
