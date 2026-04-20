"""PR1 — prayer-overlap enforcement (flag-gated).

Exercises the prayer rule in isolation:
- Half-open interval semantics: [start, end).
- Overlap rule: a.start < b.end AND a.end > b.start.
- Exact boundary touch is legal (contiguous, not overlapping).
- Flag OFF = no enforcement.
- Flag ON = rejection with reason PRAYER_OVERLAP.

Tests reference prayer_overlap_rejection from core.services.timetable_validation.
The module will be created in the following commit; these tests are expected
to fail until then.
"""

from __future__ import annotations

import pytest
from django.test.utils import override_settings

from core.services.timetable_validation import (
    PRAYER_OVERLAP,
    prayer_overlap_rejection,
)

pytestmark = pytest.mark.django_db


def _meeting(day: str, start: str, end: str) -> dict:
    return {"day": day, "start_time": start, "end_time": end}


def _prayer(day: str, start: str, end: str) -> dict:
    return {"day": day, "start_time": start, "end_time": end}


# ---------------------------------------------------------------------------
# Flag-off bypass: prayer rule is a no-op unless TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE.
# ---------------------------------------------------------------------------


@override_settings(TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=False)
def test_flag_off_returns_none_even_on_direct_overlap() -> None:
    """With the flag disabled, even a strictly-overlapping meeting is not rejected."""
    meeting = _meeting("Sun", "11:55", "12:10")
    prayers = [_prayer("Sun", "12:00", "12:15")]
    assert prayer_overlap_rejection(meeting, prayers) is None


# ---------------------------------------------------------------------------
# Flag-on: all overlap shapes rejected; non-overlap shapes accepted.
# ---------------------------------------------------------------------------


@override_settings(TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=True)
class TestPrayerOverlapRuleEnabled:
    def test_strictly_before_passes(self) -> None:
        meeting = _meeting("Sun", "08:00", "09:15")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_strictly_after_passes(self) -> None:
        meeting = _meeting("Sun", "13:00", "14:15")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_exact_boundary_touch_before_passes(self) -> None:
        """Meeting ends exactly when prayer starts: half-open [start, end) ⇒ legal."""
        meeting = _meeting("Sun", "10:45", "12:00")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_exact_boundary_touch_after_passes(self) -> None:
        """Meeting starts exactly when prayer ends ⇒ legal."""
        meeting = _meeting("Sun", "12:15", "13:30")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_fully_inside_prayer_rejects(self) -> None:
        meeting = _meeting("Sun", "12:03", "12:08")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        result = prayer_overlap_rejection(meeting, prayers)
        assert result is not None
        assert result.code == PRAYER_OVERLAP

    def test_straddles_start_rejects(self) -> None:
        meeting = _meeting("Sun", "11:30", "12:05")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is not None

    def test_straddles_end_rejects(self) -> None:
        meeting = _meeting("Sun", "12:10", "13:00")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is not None

    def test_engulfs_prayer_rejects(self) -> None:
        """Meeting spans the entire prayer window ⇒ rejected."""
        meeting = _meeting("Sun", "11:30", "13:30")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is not None

    def test_multi_prayer_day_any_overlap_rejects(self) -> None:
        """Two prayers on the same day; overlap with either triggers rejection."""
        meeting = _meeting("Sun", "15:10", "16:25")
        prayers = [
            _prayer("Sun", "12:00", "12:15"),
            _prayer("Sun", "15:20", "15:35"),
        ]
        assert prayer_overlap_rejection(meeting, prayers) is not None

    def test_multi_prayer_day_none_overlap_passes(self) -> None:
        meeting = _meeting("Sun", "09:00", "10:15")
        prayers = [
            _prayer("Sun", "12:00", "12:15"),
            _prayer("Sun", "15:20", "15:35"),
        ]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_empty_prayer_schedule_passes(self) -> None:
        meeting = _meeting("Sun", "12:00", "13:00")
        assert prayer_overlap_rejection(meeting, []) is None

    def test_different_day_no_rejection(self) -> None:
        """Prayer on Sun does not affect Mon meeting even at identical times."""
        meeting = _meeting("Mon", "12:03", "12:08")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        assert prayer_overlap_rejection(meeting, prayers) is None

    def test_rejection_carries_context(self) -> None:
        meeting = _meeting("Sun", "12:03", "12:08")
        prayers = [_prayer("Sun", "12:00", "12:15")]
        result = prayer_overlap_rejection(meeting, prayers)
        assert result is not None
        assert result.code == PRAYER_OVERLAP
        assert result.day == "Sun"
        assert result.start_time == "12:03"
        assert result.end_time == "12:08"
