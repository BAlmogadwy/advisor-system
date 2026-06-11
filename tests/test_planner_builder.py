from __future__ import annotations

import pytest

from core.services.planner_builder import Meeting, _overlap, _to_minutes, build_plans


def test_to_minutes_handles_malformed_times() -> None:
    """Regression: a dirty free-text time must not raise (it 500'd the build)."""
    assert _to_minutes("08:30") == 510
    assert _to_minutes("00:00") == 0
    for bad in ("8:00 AM", "0800", "", "8.30", "x:y"):
        assert _to_minutes(bad) == -1
    assert _to_minutes(None) == -1  # type: ignore[arg-type]


def test_overlap_treats_malformed_meeting_as_non_conflicting() -> None:
    good = Meeting(day="MON", start="08:00", end="10:00")
    overlapping = Meeting(day="MON", start="09:00", end="11:00")
    malformed = Meeting(day="MON", start="8:00 AM", end="10:00")
    assert _overlap(good, overlapping) is True
    assert _overlap(good, malformed) is False  # bad data -> no false conflict, no crash


@pytest.mark.django_db
def test_build_plans_survives_malformed_baseline_time() -> None:
    """One malformed time anywhere must not 500 the whole plan build."""
    baseline = [
        {
            "course_key": "CS101",
            "section": "M1",
            "day": "MON",
            "start_time": "8:00 AM",  # malformed
            "end_time": "10:00",
            "term_section_id": 1,
        }
    ]
    result = build_plans("1448", "1", [], baseline, True)
    assert "options" in result
    assert "summary" in result
