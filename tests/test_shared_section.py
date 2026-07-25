"""Tests for shared (multi-board) section detection and canonicalisation."""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_shared_section import (
    analyze_shared_sections,
    canonicalise_shared_sections,
)

pytestmark = pytest.mark.django_db


def _scenario():
    return TimetableScenario.objects.create(academic_year="1448", term="1", name="shared")


def _board(scenario, label, order):
    return DeliveryBoard.objects.create(
        scenario=scenario, label=label, nominal_term=order, program="AI", display_order=order
    )


def _section(scenario, code="GSE1"):
    return TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=code,
        section="S1",
        available_capacity=40,
        registered_count=20,
        source_tag="test",
    )


def _place(board, section, *, day, start, end="12:25", room="", locked=False):
    return SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
        is_locked=locked,
    )


def test_single_board_section_is_not_shared():
    scn = _scenario()
    board = _board(scn, "Term 1", 1)
    _place(board, _section(scn), day="MON", start="10:45")
    report = analyze_shared_sections(scn.id)
    assert report.shared_count == 0
    assert report.divergent_count == 0


def test_coherent_shared_section_is_not_divergent():
    scn = _scenario()
    b1, b2 = _board(scn, "Term 5", 5), _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45")
    _place(b2, section, day="MON", start="10:45")  # same schedule → coherent
    report = analyze_shared_sections(scn.id)
    assert report.shared_count == 1
    assert report.divergent_count == 0


def test_same_time_different_room_is_room_divergent():
    """TSM dedups on (day, start, end, room), so a shared class in two rooms
    survives as two meeting rows — one class apparently meeting twice."""
    scn = _scenario()
    b1, b2 = _board(scn, "Term 5", 5), _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")
    _place(b2, section, day="MON", start="10:45", room="R2")  # same time, different room
    report = analyze_shared_sections(scn.id)
    assert report.divergent_count == 1
    assert report.room_divergent_count == 1
    assert report.schedule_divergent_count == 0


def test_room_divergence_is_canonicalised_to_one_room():
    scn = _scenario()
    b1, b2 = _board(scn, "Term 5", 5), _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")  # canonical
    _place(b2, section, day="MON", start="10:45", room="R2")
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert report.remaining_divergent_count == 0
    assert SectionPlacement.objects.get(board=b2, term_section=section).room == "R1"


def test_divergent_shared_section_detected_with_canonical_winner():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # lower display_order → canonical
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45")
    _place(b2, section, day="WED", start="16:30")  # divergent
    report = analyze_shared_sections(scn.id)
    assert report.divergent_count == 1
    assert report.shared_sections[0].canonical_board_id == b1.id


def test_canonicalise_dry_run_writes_nothing():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45")
    p2 = _place(b2, section, day="WED", start="16:30")
    report = canonicalise_shared_sections(scn.id, apply=False)
    assert not report.applied
    p2.refresh_from_db()
    assert p2.day == "WED"  # untouched


def test_canonicalise_apply_makes_boards_coherent_and_carries_the_room():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45", room="R1")
    _place(b2, section, day="WED", start="16:30", room="R2")
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert report.applied
    assert report.sections_canonicalised == 1
    assert report.remaining_divergent_count == 0
    b2_rows = SectionPlacement.objects.filter(board=b2, term_section=section)
    # The canonical room comes with the canonical time — a shared class is one
    # class in one room, and TSM's dedup key includes the room.
    assert [(p.day, p.start_time, p.room) for p in b2_rows] == [("TUE", "10:45", "R1")]


def test_canonicalise_updates_in_place_preserving_placement_id():
    """Repair runs/plan items reference placements with SET_NULL FKs, so the
    rewrite must not churn ids when it can update instead."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45", room="R1")
    original_id = _place(b2, section, day="WED", start="16:30", room="R2").id
    canonicalise_shared_sections(scn.id, apply=True)
    row = SectionPlacement.objects.get(board=b2, term_section=section)
    assert row.id == original_id  # updated in place, not deleted + recreated
    assert (row.day, row.start_time) == ("TUE", "10:45")


def test_already_agreeing_board_is_left_untouched():
    """A 3-board share where one board already matches must not be rewritten."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)  # already agrees
    b3 = _board(scn, "Term 9", 9)  # diverges
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45", room="R1")
    _place(b2, section, day="TUE", start="10:45", room="R1")
    _place(b3, section, day="WED", start="16:30", room="R2")
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert report.placements_rewritten == 1  # only b3, not b2
    assert report.remaining_divergent_count == 0


def test_canonicalise_skips_locked_and_reports_it_honestly():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45")
    _place(b2, section, day="WED", start="16:30", locked=True)  # locked → protected
    report = canonicalise_shared_sections(scn.id, apply=True)
    b2_row = SectionPlacement.objects.get(board=b2, term_section=section)
    assert b2_row.day == "WED"  # lock preserved
    assert any("locked" in n.lower() for n in report.notes)
    # The report must not claim success it did not achieve.
    assert report.sections_canonicalised == 0
    assert report.sections_skipped_locked == 1
    assert report.remaining_divergent_count == 1


def test_multi_meeting_section_canonicalised_wholesale():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical: MON+WED
    b2 = _board(scn, "Term 7", 7)  # divergent: THU+TUE
    section = _section(scn, code="AI201")
    _place(b1, section, day="MON", start="10:50", end="12:05")
    _place(b1, section, day="WED", start="10:50", end="12:05")
    _place(b2, section, day="THU", start="16:00", end="17:15")
    _place(b2, section, day="TUE", start="16:00", end="17:15")
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert report.applied
    after = analyze_shared_sections(scn.id)
    assert after.divergent_count == 0
    b2_rows = {
        (p.day, p.start_time)
        for p in SectionPlacement.objects.filter(board=b2, term_section=section)
    }
    assert b2_rows == {("MON", "10:50"), ("WED", "10:50")}
