"""Tests for the board-persistence module (PR-1).

Pins the invariant that ``SectionPlacement`` and ``TermSectionMeeting`` are
reset and snapshot/restored *together* — the corruption class behind the
placements-only rollback (defect a) and the meeting drift the optimiser's
persist skip warns about.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_board_persistence import (
    reset_scenario,
    restore_scenario,
    snapshot_scenario,
)


def _make_section(
    scenario,
    board,
    course: str,
    sec: str,
    day: str,
    start: str,
    end: str,
    *,
    locked: bool = False,
    instructor: str = "",
    room: str = "R1",
    global_section: bool = False,
) -> TermSection:
    """Create a section with one placement and one matching meeting."""
    ts = TermSection.objects.create(
        scenario=None if global_section else scenario,
        course_code=course,
        course_number=course,
        course_key=course,
        course_name=course,
        section=sec,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
        is_locked=locked,
    )
    TermSectionMeeting.objects.create(
        term_section=ts,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
        instructor=instructor,
    )
    return ts


def _scenario_with_board(name: str):
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name=name)
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    return scenario, board


@pytest.mark.django_db
def test_reset_keep_locked_preserves_locked_placement_and_meeting() -> None:
    scenario, board = _scenario_with_board("reset-keep-locked")
    locked_ts = _make_section(
        scenario, board, "AI101", "S1", "SUN", "09:00", "10:15", locked=True, instructor="Dr Locked"
    )
    unlocked_ts = _make_section(
        scenario, board, "DS201", "S1", "MON", "10:30", "11:45", locked=False, instructor="Dr Free"
    )

    result = reset_scenario(scenario.id, keep_locked=True)

    # Locked section fully survives — placement AND its meeting (incl. instructor).
    assert SectionPlacement.objects.filter(term_section=locked_ts).count() == 1
    locked_meetings = TermSectionMeeting.objects.filter(term_section=locked_ts)
    assert locked_meetings.count() == 1
    assert locked_meetings.first().instructor == "Dr Locked"

    # Unlocked section is gone from BOTH tables (no stranded meeting).
    assert SectionPlacement.objects.filter(term_section=unlocked_ts).count() == 0
    assert TermSectionMeeting.objects.filter(term_section=unlocked_ts).count() == 0

    assert result.placements_deleted == 1
    assert result.meetings_deleted == 1
    assert result.locked_sections_preserved == 1


@pytest.mark.django_db
def test_reset_keeps_only_locked_meetings_of_a_partially_locked_section() -> None:
    """A section can have a mix of locked and unlocked placements (locking is
    per-placement). Reset must keep only the meeting that backs the surviving
    locked placement — not both — or the preserved section itself drifts."""
    scenario, board = _scenario_with_board("reset-mixed-lock")
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    # Two sessions of one section: Monday locked (pinned), Wednesday not.
    for day, start, end, locked in (
        ("MON", "09:00", "10:15", True),
        ("WED", "09:00", "10:15", False),
    ):
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=locked,
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day=day, start_time=start, end_time=end, room="R1"
        )

    reset_scenario(scenario.id, keep_locked=True)

    placements = SectionPlacement.objects.filter(term_section=ts)
    meetings = TermSectionMeeting.objects.filter(term_section=ts)
    # Exactly the locked Monday survives in BOTH tables — no stranded Wednesday
    # meeting (which would be placement != meeting drift on a preserved section).
    assert placements.count() == 1
    assert meetings.count() == 1
    assert placements.first().day == "MON"
    assert meetings.first().day == "MON"


@pytest.mark.django_db
def test_reset_hard_wipe_clears_everything() -> None:
    scenario, board = _scenario_with_board("reset-hard")
    _make_section(scenario, board, "AI101", "S1", "SUN", "09:00", "10:15", locked=True)
    _make_section(scenario, board, "DS201", "S1", "MON", "10:30", "11:45", locked=False)

    reset_scenario(scenario.id, keep_locked=False)

    assert SectionPlacement.objects.filter(board__scenario_id=scenario.id).count() == 0
    assert TermSectionMeeting.objects.filter(term_section__scenario_id=scenario.id).count() == 0


@pytest.mark.django_db
def test_snapshot_restore_roundtrips_placements_and_meeting_instructor() -> None:
    scenario, board = _scenario_with_board("roundtrip")
    ts = _make_section(
        scenario, board, "AI101", "S1", "SUN", "09:00", "10:15", instructor="Dr Original"
    )

    snapshot = snapshot_scenario(scenario.id)

    # Simulate a failed optimiser run: move the placement, blank the instructor,
    # and drop the meeting entirely (the exact drift a rollback must undo).
    SectionPlacement.objects.filter(term_section=ts).update(day="WED", start_time="14:30")
    TermSectionMeeting.objects.filter(term_section=ts).delete()

    restore_scenario(scenario.id, snapshot)

    placement = SectionPlacement.objects.get(term_section=ts)
    assert (placement.day, placement.start_time, placement.end_time) == ("SUN", "09:00", "10:15")

    meetings = TermSectionMeeting.objects.filter(term_section=ts)
    assert meetings.count() == 1
    restored = meetings.first()
    assert (restored.day, restored.start_time, restored.end_time) == ("SUN", "09:00", "10:15")
    assert restored.instructor == "Dr Original"


@pytest.mark.django_db
def test_restore_is_atomic_on_failure() -> None:
    scenario, board = _scenario_with_board("atomic")
    ts = _make_section(scenario, board, "AI101", "S1", "SUN", "09:00", "10:15", instructor="Dr A")
    snapshot = snapshot_scenario(scenario.id)

    # A failure mid-restore (after the delete-all) must roll the whole thing
    # back, leaving the pre-restore board intact — never a half-wiped scenario.
    with (
        patch.object(SectionPlacement.objects, "bulk_create", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        restore_scenario(scenario.id, snapshot)

    assert SectionPlacement.objects.filter(term_section=ts).count() == 1
    assert TermSectionMeeting.objects.filter(term_section=ts).get().instructor == "Dr A"


@pytest.mark.django_db
def test_global_section_meetings_untouched_by_reset_and_restore() -> None:
    scenario, board = _scenario_with_board("global-safe")
    global_ts = _make_section(
        scenario, board, "GEN100", "S1", "THU", "08:00", "09:15", global_section=True
    )
    _make_section(scenario, board, "AI101", "S1", "SUN", "09:00", "10:15", locked=False)

    # Global (scenario-null) sections are shared/imported — the optimiser must
    # never delete their meetings.
    reset_scenario(scenario.id, keep_locked=True)
    assert TermSectionMeeting.objects.filter(term_section=global_ts).count() == 1

    snapshot = snapshot_scenario(scenario.id)
    assert all(m["term_section_id"] != global_ts.id for m in snapshot.meetings)
    restore_scenario(scenario.id, snapshot)
    assert TermSectionMeeting.objects.filter(term_section=global_ts).count() == 1
