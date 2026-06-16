"""Tests for the post-build instructor-day compaction pass.

The pass shrinks each instructor's within-day idle gaps by relocating their
sessions in time, strictly guarded so students/feasibility never regress and
flag-gated (default off). These tests use a student-free scenario so the
instructor optimisation is exercised directly.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def _gappy_board():
    """One instructor teaching two MON courses with a 2h45 midday hole."""
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_pr4_instructor import normalise_instructor

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="AI M T1 compact",
        gender="M",
        programs=["AI"],
        slot_config=_SLOTS,
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    instr = Instructor.objects.create(
        full_name="Dr Gap", normalised_name=normalise_instructor("Dr Gap")
    )
    placements = [("C1", "09:00", "10:15"), ("C2", "13:00", "14:15")]  # MON, 2h45 hole
    for code, start, end in placements:
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instr, role="primary"
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            section="S1",
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="compact_test",
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time=start, end_time=end, room="", instructor=""
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=False,
        )
        apply_primary_instructor(ts, scenario, board, ts.course_code)
    return scenario


def _instructor_idle(scenario):
    """Total within-day idle minutes for the (single) instructor."""
    from collections import defaultdict

    from core.models import SectionPlacement, TermSectionMeeting

    instr = {
        ts: nm
        for ts, nm in TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .exclude(instructor="")
        .values_list("term_section_id", "instructor")
    }
    byday = defaultdict(list)
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        nm = instr.get(p.term_section_id)
        if nm:
            h, m = p.start_time.split(":")
            he, me = p.end_time.split(":")
            byday[(nm, p.day)].append((int(h) * 60 + int(m), int(he) * 60 + int(me)))
    total = 0
    for sess in byday.values():
        sess = sorted(sess)
        total += sum(
            g for g in (sess[i + 1][0] - sess[i][1] for i in range(len(sess) - 1)) if g > 0
        )
    return total


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_compaction_reduces_idle() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    assert _instructor_idle(scenario) == 165  # 2h45 hole before

    report = compact_instructor_schedules(scenario.id)

    assert report["enabled"] is True
    assert (
        report["instructor_impact"]["total_idle_after"]
        < report["instructor_impact"]["total_idle_before"]
    )
    assert report["search"]["moves_accepted"] >= 1
    assert _instructor_idle(scenario) < 165  # hole shrunk on the real board
    # No student / feasibility regression (vacuous here, but the gates must hold).
    assert report["protected"]["feasibility_after"] == report["protected"]["feasibility_before"]


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_compaction_is_idempotent() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    compact_instructor_schedules(scenario.id)
    idle_after_first = _instructor_idle(scenario)

    second = compact_instructor_schedules(scenario.id)
    assert second["search"]["moves_accepted"] == 0  # already compact → no more moves
    assert _instructor_idle(scenario) == idle_after_first


@pytest.mark.django_db(transaction=True)
@override_settings(TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=False)
def test_compaction_noop_when_flag_off() -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    scenario = _gappy_board()
    report = compact_instructor_schedules(scenario.id)
    assert report == {"enabled": False}
    assert _instructor_idle(scenario) == 165  # untouched when off
