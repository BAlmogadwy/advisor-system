"""A midday start is ordinary teaching time, not a "weak" slot (owner, 2026-11-22).

Two soft scores used to penalise a start between 12:00 and 14:00, which on the
declared grid is only the 13:00 period. The grid is the authority on legal
times, so a period it declares carries no hidden penalty. The early-morning,
late-afternoon and Thursday-afternoon penalties stay.
"""

from __future__ import annotations

import pytest

from core.services.timetable_quality import meeting_weak_slot_penalty
from core.services.timetable_repair import _weak_slot_penalty

SUNDAY, THURSDAY = 0, 4


@pytest.mark.parametrize("start", [12 * 60, 12 * 60 + 30, 13 * 60, 13 * 60 + 59])
def test_the_quality_score_does_not_penalise_a_midday_start(start: int) -> None:
    assert meeting_weak_slot_penalty(SUNDAY, start, start + 75) == 0


def test_the_quality_score_keeps_its_other_weak_slot_penalties() -> None:
    assert meeting_weak_slot_penalty(SUNDAY, 7 * 60 + 30, 8 * 60 + 45) == 40
    assert meeting_weak_slot_penalty(SUNDAY, 16 * 60, 17 * 60 + 15) == 45
    assert meeting_weak_slot_penalty(SUNDAY, 17 * 60, 18 * 60 + 40) == 45 + 60
    assert meeting_weak_slot_penalty(THURSDAY, 14 * 60 + 30, 15 * 60 + 45) == 35
    assert meeting_weak_slot_penalty(SUNDAY, 9 * 60, 10 * 60 + 15) == 0


@pytest.mark.parametrize("start", ["12:00", "12:30", "13:00", "13:59"])
def test_the_repair_ranking_does_not_penalise_a_midday_start(start: str) -> None:
    assert _weak_slot_penalty({"start_time": start}) == 0


def test_the_repair_ranking_keeps_its_other_weak_slot_penalties() -> None:
    assert _weak_slot_penalty({"start_time": "08:00"}) == 6
    assert _weak_slot_penalty({"start_time": "16:30"}) == 6
    assert _weak_slot_penalty({"start_time": "10:30"}) == 0
    assert _weak_slot_penalty({"start_time": "not a time"}) == 0
