"""Regression tests for two bugs found while rebuilding scenario 635.

Bug 1 — the instructor relocation passes (cap-repair, clash-repair, compaction)
guarded collisions by EXACT start-time set membership, but the board conflict
metric counts INTERVAL overlap. The lecture grid (10:30-11:45) and lab grid
(09:00-10:40) interleave, so a sibling/instructor session at a different start
could overlap a "free" candidate slot, manufacturing a same_board_overlaps the
safety gate then rolled back. The guards are now interval-aware.

Bug 2 — the full-rebuild candidate loop deleted only SectionPlacement between
strategies while auto_place re-created TermSectionMeeting via get_or_create, so a
section that moved slots across strategies accumulated stale meeting rows
(placements != meetings) and got skipped on persist. ``_reset_unlocked_placements``
now clears the meetings too (preserving locked ones).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from core.services.timetable_instructor_cap_repair import _creates_board_interval_overlap
from core.services.timetable_instructor_compaction import _interval_busy

# ── Bug 1a: compaction's in-memory interval check ────────────────────────────


def test_interval_busy_detects_interleaving_lecture_over_lab() -> None:
    # A 09:00-10:40 lab occupies day 0 (540..640).
    occupied = {(0, 540, 640)}
    # A 10:30-11:45 lecture (630..705) overlaps it in the 10:30-10:40 window —
    # different start minute, so the old start-only guard missed it.
    assert _interval_busy(occupied, 0, 630, 705) is True


def test_interval_busy_allows_adjacent_and_other_days() -> None:
    occupied = {(0, 540, 640)}  # day 0, 09:00-10:40
    assert _interval_busy(occupied, 0, 645, 720) is False  # starts after it ends
    assert _interval_busy(occupied, 1, 630, 705) is False  # different day
    assert _interval_busy(set(), 0, 630, 705) is False  # nothing occupied


# ── Bug 1b: cap/clash-repair's live-placement interval check ─────────────────


def _pl(course, board, day, start, end, iids=frozenset()):
    return SimpleNamespace(
        term_section=SimpleNamespace(course_code=course),
        board_id=board,
        day=day,
        start_time=start,
        end_time=end,
        _iids=iids,
    )


def _instr_ids(q):
    return getattr(q, "_iids", frozenset())


def test_board_overlap_blocks_same_course_sibling_interval() -> None:
    moving = _pl("PHYS103", board=1, day="MON", start="13:00", end="14:15")  # relocating
    sibling = _pl("PHYS103", board=1, day="MON", start="09:00", end="10:40")  # lab
    others = [sibling, moving]
    # Move PHYS103 onto MON 10:30-11:45 — overlaps the sibling lab interval.
    hit = _creates_board_interval_overlap(moving, "MON", 630, 705, frozenset(), others, _instr_ids)
    assert hit is True


def test_board_overlap_allows_parallel_different_course_other_room() -> None:
    moving = _pl("PHYS103", board=1, day="MON", start="13:00", end="14:15")
    other_course = _pl("MATH105", board=1, day="MON", start="10:30", end="11:45")
    others = [other_course, moving]
    # Different course, no shared students/instructor, overlapping time on the
    # same board — that is a NORMAL parallel section, not a counted overlap.
    hit = _creates_board_interval_overlap(moving, "MON", 630, 705, frozenset(), others, _instr_ids)
    assert hit is False


def test_board_overlap_blocks_same_instructor_interval_any_board() -> None:
    moving = _pl("CS101", board=1, day="MON", start="13:00", end="14:15", iids=frozenset({7}))
    elsewhere = _pl("MATH200", board=2, day="MON", start="09:00", end="10:40", iids=frozenset({7}))
    others = [elsewhere, moving]
    # Same instructor (7) would be double-booked across the overlap, even on a
    # different board — an instructor cannot be in two places at once.
    hit = _creates_board_interval_overlap(
        moving, "MON", 630, 705, frozenset({7}), others, _instr_ids
    )
    assert hit is True


def test_board_overlap_allows_disjoint_slot() -> None:
    moving = _pl("PHYS103", board=1, day="MON", start="13:00", end="14:15")
    sibling = _pl("PHYS103", board=1, day="MON", start="09:00", end="10:40")
    others = [sibling, moving]
    # Target SUN 09:00-10:15 — different day from the sibling → no overlap.
    hit = _creates_board_interval_overlap(moving, "SUN", 540, 615, frozenset(), others, _instr_ids)
    assert hit is False


# ── Bug 2: meeting rows cleared alongside unlocked placements ─────────────────


@pytest.mark.django_db(transaction=True)
def test_reset_unlocked_placements_clears_meetings_but_keeps_locked() -> None:
    from core.models import (
        DeliveryBoard,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import _reset_unlocked_placements

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="reset test",
        gender="M",
        programs=["AI"],
        slot_config=[{"start": "09:00", "end": "10:15"}],
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )

    def _section(code, locked):
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            section="S1",
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="reset_test",
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time="09:00", end_time="10:15", room="", instructor=""
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time="09:00",
            end_time="10:15",
            room="R1",
            is_locked=locked,
        )
        return ts

    unlocked = _section("C1", locked=False)
    locked = _section("C2", locked=True)
    # Stale extra meeting on the unlocked section (mimics cross-strategy drift).
    TermSectionMeeting.objects.create(
        term_section=unlocked,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="",
        instructor="",
    )

    _reset_unlocked_placements(scenario.id)

    # Unlocked: placement + BOTH meeting rows gone.
    assert not SectionPlacement.objects.filter(term_section=unlocked).exists()
    assert not TermSectionMeeting.objects.filter(term_section=unlocked).exists()
    # Locked: placement + meeting preserved.
    assert SectionPlacement.objects.filter(term_section=locked).exists()
    assert TermSectionMeeting.objects.filter(term_section=locked).count() == 1


@override_settings(TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=False)
def test_compaction_flag_off_is_noop() -> None:
    """Sanity: the interval-guard change keeps the flag-off no-op contract."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    # No DB access happens when the flag is off; it returns immediately.
    assert compact_instructor_schedules(0) == {"enabled": False}
