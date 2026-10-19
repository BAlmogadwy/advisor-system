"""Tests for shared (multi-board) section detection and canonicalisation."""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
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


# ── Locks freeze days and times, not rooms (lock contract 2026-10-18) ──


def _owner_copy(section, *, day, start, end="12:25", room=""):
    """Place ``section`` in a second scenario too, so it is externally owned."""
    other = TimetableScenario.objects.create(academic_year="1448", term="1", name="owner")
    return _place(_board(other, "Term 5", 5), section, day=day, start=start, end=end, room=room)


def _snapshot(section):
    """Every placement (all scenarios) and meeting row of a section, column by column."""
    return (
        list(SectionPlacement.objects.filter(term_section=section).order_by("pk").values()),
        list(TermSectionMeeting.objects.filter(term_section=section).order_by("pk").values()),
    )


def test_locked_copy_at_canonical_time_takes_the_canonical_room():
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")
    locked = _place(b2, section, day="MON", start="10:45", room="R2", locked=True)
    # The room divergence shows up as a phantom second meeting row.
    for room in ("R1", "R2"):
        TermSectionMeeting.objects.create(
            term_section=section, day="MON", start_time="10:45", end_time="12:25", room=room
        )
    report = canonicalise_shared_sections(scn.id, apply=True)
    locked.refresh_from_db()
    assert (locked.day, locked.start_time, locked.end_time) == ("MON", "10:45", "12:25")
    assert locked.room == "R1"
    assert locked.is_locked is True  # the lock itself survives the room change
    assert report.sections_canonicalised == 1
    assert report.sections_skipped_locked == 0
    assert report.placements_rewritten == 1
    assert report.remaining_divergent_count == 0
    assert any("to the canonical room; their days and times are kept" in n for n in report.notes)
    # TSM is rebuilt in the same transaction: one class, one meeting row.
    assert list(
        TermSectionMeeting.objects.filter(term_section=section).values_list(
            "day", "start_time", "end_time", "room"
        )
    ) == [("MON", "10:45", "12:25", "R1")]


def test_locked_multi_meeting_copy_is_re_roomed_whole_without_moving():
    """Any locked meeting anchors the copy; its unlocked companion is re-roomed with it."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical: MON+WED in R1
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn, code="AI201")
    _place(b1, section, day="MON", start="10:50", end="12:05", room="R1")
    _place(b1, section, day="WED", start="10:50", end="12:05", room="R1")
    mon = _place(b2, section, day="MON", start="10:50", end="12:05", room="R2", locked=True)
    wed = _place(b2, section, day="WED", start="10:50", end="12:05", room="R3")
    report = canonicalise_shared_sections(scn.id, apply=True)
    rows = {
        p.id: (p.day, p.start_time, p.end_time, p.room, p.is_locked)
        for p in SectionPlacement.objects.filter(board=b2, term_section=section)
    }
    assert rows == {
        mon.id: ("MON", "10:50", "12:05", "R1", True),
        wed.id: ("WED", "10:50", "12:05", "R1", False),
    }
    assert report.sections_canonicalised == 1
    assert report.placements_rewritten == 2
    assert report.remaining_divergent_count == 0


def test_locked_copy_rewrites_only_the_meetings_whose_room_differs():
    """A meeting already in the canonical room is not saved again or counted as rewritten."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical: MON+WED in R1
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn, code="AI201")
    _place(b1, section, day="MON", start="10:50", end="12:05", room="R1")
    _place(b1, section, day="WED", start="10:50", end="12:05", room="R1")
    mon = _place(b2, section, day="MON", start="10:50", end="12:05", room="R1", locked=True)
    wed = _place(b2, section, day="WED", start="10:50", end="12:05", room="R3")
    mon_stamp = SectionPlacement.objects.get(pk=mon.pk).updated_at
    report = canonicalise_shared_sections(scn.id, apply=True)
    mon.refresh_from_db()
    wed.refresh_from_db()
    assert (mon.room, mon.is_locked, wed.room) == ("R1", True, "R1")
    assert mon.updated_at == mon_stamp  # untouched row, not re-saved
    assert report.placements_rewritten == 1
    assert report.sections_canonicalised == 1
    assert report.remaining_divergent_count == 0


def test_locked_copy_with_one_meeting_at_another_time_is_skipped_whole():
    """Room-only means EVERY meeting time matches; a partial match keeps every old room."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn, code="AI201")
    _place(b1, section, day="MON", start="10:50", end="12:05", room="R1")
    _place(b1, section, day="WED", start="10:50", end="12:05", room="R1")
    _place(b2, section, day="MON", start="10:50", end="12:05", room="R2", locked=True)
    _place(b2, section, day="THU", start="10:50", end="12:05", room="R3")
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert {
        (p.day, p.room) for p in SectionPlacement.objects.filter(board=b2, term_section=section)
    } == {("MON", "R2"), ("THU", "R3")}
    assert report.sections_canonicalised == 0
    assert report.sections_skipped_locked == 1
    assert report.placements_rewritten == 0
    assert any("resolve the lock manually" in n for n in report.notes)


def test_locked_copy_missing_a_canonical_meeting_is_skipped():
    """A lock freezes the contact pattern: a copy with fewer meetings is a time difference."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical: MON+WED
    b2 = _board(scn, "Term 7", 7)  # locked: MON only
    section = _section(scn, code="AI201")
    _place(b1, section, day="MON", start="10:50", end="12:05", room="R1")
    _place(b1, section, day="WED", start="10:50", end="12:05", room="R1")
    _place(b2, section, day="MON", start="10:50", end="12:05", room="R2", locked=True)
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert [
        (p.day, p.room) for p in SectionPlacement.objects.filter(board=b2, term_section=section)
    ] == [("MON", "R2")]
    assert report.sections_skipped_locked == 1
    assert report.placements_rewritten == 0


def test_room_only_and_time_divergent_locked_copies_in_one_section():
    """The room-only decision is per board: one locked copy is re-roomed, the other is not."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)  # locked, same time, other room
    b3 = _board(scn, "Term 9", 9)  # locked, other time
    section = _section(scn)
    _place(b1, section, day="TUE", start="10:45", room="R1")
    _place(b2, section, day="TUE", start="10:45", room="R2", locked=True)
    _place(b3, section, day="WED", start="16:30", room="R3", locked=True)
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert SectionPlacement.objects.get(board=b2, term_section=section).room == "R1"
    b3_row = SectionPlacement.objects.get(board=b3, term_section=section)
    assert (b3_row.day, b3_row.start_time, b3_row.room) == ("WED", "16:30", "R3")
    assert report.sections_canonicalised == 1
    assert report.sections_skipped_locked == 1
    assert report.placements_rewritten == 1
    assert report.remaining_divergent_count == 1


def test_locked_copy_keeps_its_room_when_the_canonical_meeting_has_none():
    """Re-rooming never trades a locked meeting's room for no room."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical, unroomed
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="")
    locked = _place(b2, section, day="MON", start="10:45", room="R2", locked=True)
    report = canonicalise_shared_sections(scn.id, apply=True)
    locked.refresh_from_db()
    assert (locked.day, locked.start_time, locked.room, locked.is_locked) == (
        "MON",
        "10:45",
        "R2",
        True,
    )
    assert report.sections_canonicalised == 0
    assert report.sections_skipped_locked == 1
    assert report.placements_rewritten == 0
    assert report.remaining_divergent_count == 1


def test_locked_copy_keeps_its_room_when_the_canonical_meeting_is_unassigned():
    """``UNASSIGNED`` is the rooming sentinel for "no room", not a room: R2 must survive."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="UNASSIGNED")
    locked = _place(b2, section, day="MON", start="10:45", room="R2", locked=True)
    report = canonicalise_shared_sections(scn.id, apply=True)
    locked.refresh_from_db()
    assert (locked.day, locked.start_time, locked.room, locked.is_locked) == (
        "MON",
        "10:45",
        "R2",
        True,
    )
    assert report.sections_canonicalised == 0
    assert report.sections_skipped_locked == 1
    assert report.placements_rewritten == 0
    assert any("resolve the lock manually" in n for n in report.notes)


@pytest.mark.parametrize("locked_room", ["", "UNASSIGNED"])
def test_unroomed_locked_copy_takes_the_canonical_room(locked_room):
    """The no-room guard is one-way: a locked copy WITHOUT a room still gains one."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical, roomed
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")
    locked = _place(b2, section, day="MON", start="10:45", room=locked_room, locked=True)
    report = canonicalise_shared_sections(scn.id, apply=True)
    locked.refresh_from_db()
    assert (locked.day, locked.start_time, locked.room, locked.is_locked) == (
        "MON",
        "10:45",
        "R1",
        True,
    )
    assert report.sections_canonicalised == 1
    assert report.sections_skipped_locked == 0
    assert report.placements_rewritten == 1
    assert report.remaining_divergent_count == 0


def test_externally_owned_locked_copy_keeps_its_room():
    """Another scenario also places this section: even a room-only fix is not made here."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")
    _place(b2, section, day="MON", start="10:45", room="R2", locked=True)
    _owner_copy(section, day="MON", start="10:45", room="R2")
    before = _snapshot(section)
    report = canonicalise_shared_sections(scn.id, apply=True)
    assert _snapshot(section) == before
    assert report.sections_canonicalised == 0
    # Skipped as externally owned, not counted as a lock skip.
    assert report.sections_skipped_locked == 0
    assert report.placements_rewritten == 0
    assert report.remaining_divergent_count == 1
    assert any("GSE1|S1: another scenario also places" in n for n in report.notes)


@pytest.mark.parametrize("locked", [False, True])
def test_externally_owned_copy_keeps_its_time_and_room(locked):
    """The whole section is skipped, unlocked copies included: day, time and room stay put."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)  # canonical
    b2 = _board(scn, "Term 7", 7)
    section = _section(scn)
    _place(b1, section, day="MON", start="10:45", room="R1")
    mine = _place(b2, section, day="WED", start="16:30", end="17:45", room="R2", locked=locked)
    _owner_copy(section, day="WED", start="16:30", end="17:45", room="R2")
    for day, start, end, room in (("MON", "10:45", "12:25", "R1"), ("WED", "16:30", "17:45", "R2")):
        TermSectionMeeting.objects.create(
            term_section=section, day=day, start_time=start, end_time=end, room=room
        )
    before = _snapshot(section)
    report = canonicalise_shared_sections(scn.id, apply=True)
    mine.refresh_from_db()
    assert (mine.day, mine.start_time, mine.end_time, mine.room, mine.is_locked) == (
        "WED",
        "16:30",
        "17:45",
        "R2",
        locked,
    )
    # Placements in both scenarios and the meeting rows are byte-identical.
    assert _snapshot(section) == before
    assert report.sections_canonicalised == 0
    assert report.sections_skipped_locked == 0
    assert report.placements_rewritten == 0
    assert report.remaining_divergent_count == 1
    assert any("another scenario also places" in n for n in report.notes)


def test_externally_owned_section_does_not_stop_the_rest_of_the_run():
    """The external skip is per section: a later local section is still canonicalised."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    external = _section(scn, code="GSE1")  # lower id, so it is processed first
    local = _section(scn, code="GSE2")
    _place(b1, external, day="MON", start="10:45", room="R1")
    _place(b2, external, day="WED", start="16:30", room="R2")
    _owner_copy(external, day="WED", start="16:30", room="R2")
    _place(b1, local, day="TUE", start="10:45", room="R3")
    moved = _place(b2, local, day="THU", start="16:30", room="R4")
    before = _snapshot(external)
    report = canonicalise_shared_sections(scn.id, apply=True)
    moved.refresh_from_db()
    assert (moved.day, moved.start_time, moved.room) == ("TUE", "10:45", "R3")
    assert _snapshot(external) == before
    assert report.sections_canonicalised == 1
    assert report.placements_rewritten == 1
    assert report.remaining_divergent_count == 1


def test_dry_run_and_summary_account_for_externally_owned_sections():
    """The dry run counts only what apply will rewrite; the summary names external skips."""
    scn = _scenario()
    b1 = _board(scn, "Term 5", 5)
    b2 = _board(scn, "Term 7", 7)
    external = _section(scn, code="GSE1")
    local = _section(scn, code="GSE2")
    _place(b1, external, day="MON", start="10:45", room="R1")
    _place(b2, external, day="WED", start="16:30", room="R2")
    _owner_copy(external, day="WED", start="16:30", room="R2")
    _place(b1, local, day="TUE", start="10:45", room="R3")
    _place(b2, local, day="THU", start="16:30", room="R4")

    dry = canonicalise_shared_sections(scn.id, apply=False)
    assert dry.notes[-1] == (
        "DRY RUN — would rewrite 1 boards across 1 incoherent sections "
        "(1 more are externally owned and stay frozen). Pass apply=True to write."
    )
    applied = canonicalise_shared_sections(scn.id, apply=True)
    assert applied.placements_rewritten == 1
    assert applied.notes[-1].startswith(
        "Canonicalised 1 sections (1 placements rewritten, 1 externally owned); 1 still incoherent."
    )
