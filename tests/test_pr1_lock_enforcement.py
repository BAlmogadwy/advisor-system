"""PR1 — lock-respect enforcement (flag-gated).

Exercises the lock rule in isolation:
- Locked placements are preloaded into occupancy/state; auto-planner never
  attempts to overwrite or move them.
- Candidate onto a locked cell emits LOCK_RESPECT rejection (telemetry).
- Flag OFF = no enforcement (baseline behaviour).

Tests reference lock_rejection from core.services.timetable_validation.
The module will be created in the following commit; these tests are expected
to fail until then.
"""

from __future__ import annotations

from django.test import SimpleTestCase
from django.test.utils import override_settings

from core.services.timetable_validation import (
    LOCK_RESPECT,
    lock_rejection,
)


def _candidate(day: str, start: str, end: str, course: str, room: str = "") -> dict:
    return {
        "day": day,
        "start_time": start,
        "end_time": end,
        "course_code": course,
        "room": room,
    }


def _locked_cell(day: str, start: str, room: str) -> dict:
    return {"day": day, "start_time": start, "room": room}


# ---------------------------------------------------------------------------
# Flag-off bypass.
# ---------------------------------------------------------------------------


@override_settings(TIMETABLE_ENFORCE_LOCKS=False)
class TestLockRuleDisabled(SimpleTestCase):
    def test_flag_off_returns_none_even_on_direct_collision(self) -> None:
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="A101")
        locked = [_locked_cell("Sun", "08:00", "A101")]
        assert lock_rejection(cand, locked) is None


# ---------------------------------------------------------------------------
# Flag-on.
# ---------------------------------------------------------------------------


@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
class TestLockRuleEnabled(SimpleTestCase):
    def test_candidate_onto_locked_cell_rejects(self) -> None:
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="A101")
        locked = [_locked_cell("Sun", "08:00", "A101")]
        result = lock_rejection(cand, locked)
        assert result is not None
        assert result.code == LOCK_RESPECT

    def test_candidate_onto_empty_cell_passes(self) -> None:
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="A101")
        locked = [_locked_cell("Sun", "10:00", "A101")]  # different slot
        assert lock_rejection(cand, locked) is None

    def test_different_room_same_slot_passes(self) -> None:
        """Locked room A101 at Sun 08:00 doesn't block other rooms."""
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="B202")
        locked = [_locked_cell("Sun", "08:00", "A101")]
        assert lock_rejection(cand, locked) is None

    def test_different_slot_same_room_passes(self) -> None:
        cand = _candidate("Sun", "10:00", "11:15", "CS101", room="A101")
        locked = [_locked_cell("Sun", "08:00", "A101")]
        assert lock_rejection(cand, locked) is None

    def test_multiple_locks_any_collision_rejects(self) -> None:
        cand = _candidate("Mon", "10:00", "11:15", "CS101", room="B202")
        locked = [
            _locked_cell("Sun", "08:00", "A101"),
            _locked_cell("Mon", "10:00", "B202"),
        ]
        assert lock_rejection(cand, locked) is not None

    def test_empty_lock_set_passes(self) -> None:
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="A101")
        assert lock_rejection(cand, []) is None

    def test_rejection_carries_context(self) -> None:
        cand = _candidate("Sun", "08:00", "09:15", "CS101", room="A101")
        locked = [_locked_cell("Sun", "08:00", "A101")]
        result = lock_rejection(cand, locked)
        assert result is not None
        assert result.code == LOCK_RESPECT
        assert result.day == "Sun"
        assert result.start_time == "08:00"
        assert result.end_time == "09:15"
        assert result.course_code == "CS101"
