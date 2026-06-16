"""Instructor-clash enforcement + repair.

The clash rule (an instructor can't teach two sections at the same start) was
enforced only at greedy construction; the optimise stages could re-create it.
These tests cover the detection helper and the repair pass that clears clashes
already on a board.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_pr4_instructor import (
    count_instructor_clashes,
    has_instructor_clash,
)


def _sec(section_id, course_code, meetings):
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=30,
        reserve_capacity=0,
    )


def _by_id(*secs):
    return {s.section_id: s for s in secs}


def test_clash_same_instructor_same_slot() -> None:
    a = _sec("C1_S1", "C1", [(0, 540, 615)])  # SUN 09:00
    b = _sec("C2_S1", "C2", [(0, 540, 615)])  # SUN 09:00 — same instructor → clash
    smap = {"C1_S1": frozenset({7}), "C2_S1": frozenset({7})}
    assert has_instructor_clash(_by_id(a, b), smap) is True
    assert count_instructor_clashes(_by_id(a, b), smap) == 1


def test_no_clash_different_start_or_day_or_instructor() -> None:
    a = _sec("C1_S1", "C1", [(0, 540, 615)])
    b = _sec("C2_S1", "C2", [(0, 660, 735)])  # different start
    c = _sec("C3_S1", "C3", [(1, 540, 615)])  # different day
    d = _sec("C4_S1", "C4", [(0, 540, 615)])  # same slot, different instructor
    smap = {
        "C1_S1": frozenset({7}),
        "C2_S1": frozenset({7}),
        "C3_S1": frozenset({7}),
        "C4_S1": frozenset({9}),
    }
    assert has_instructor_clash(_by_id(a, b, c, d), smap) is False
    assert count_instructor_clashes(_by_id(a, b, c, d), smap) == 0


# ── Repair (DB integration) ──────────────────────────────────────────────────

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def _clashed_board():
    """One instructor teaching two DIFFERENT courses both at MON 09:00 (clash)."""
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
        name="AI M clash",
        gender="M",
        programs=["AI"],
        slot_config=_SLOTS,
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    instr = Instructor.objects.create(
        full_name="Dr Clash", normalised_name=normalise_instructor("Dr Clash")
    )
    for code in ("C1", "C2"):  # both MON 09:00 → instructor double-booked
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
            source_tag="clash_test",
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
            is_locked=False,
        )
        apply_primary_instructor(ts, scenario, board, ts.course_code)
    return scenario


def _clash_count(scenario):
    from collections import defaultdict

    from core.models import SectionPlacement, TermSectionMeeting

    instr = {
        ts: nm
        for ts, nm in TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .exclude(instructor="")
        .values_list("term_section_id", "instructor")
    }
    cell = defaultdict(int)
    for p in SectionPlacement.objects.filter(board__scenario=scenario).exclude(day=""):
        nm = instr.get(p.term_section_id)
        if nm:
            cell[(nm, p.day, p.start_time)] += 1
    return sum(c - 1 for c in cell.values() if c > 1)


@pytest.mark.django_db(transaction=True)
@override_settings(
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True, TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True
)
def test_clash_repair_clears_double_booking() -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    scenario = _clashed_board()
    assert _clash_count(scenario) == 1  # double-booked before

    report = repair_instructor_clashes(scenario.id)

    assert report["enabled"] is True
    assert report["detected"]
    assert report["repaired"]  # one session relocated
    assert _clash_count(scenario) == 0  # clash cleared on the real board
    assert report["remaining_clashes"] == 0


@pytest.mark.django_db(transaction=True)
@override_settings(TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=False)
def test_clash_repair_noop_when_flag_off() -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    scenario = _clashed_board()
    report = repair_instructor_clashes(scenario.id)
    assert report == {
        "enabled": False,
        "detected": [],
        "repaired": [],
        "unplaced": [],
        "locked_blocked": [],
        "remaining_clashes": 0,
        "student_score_before": None,
        "student_score_after": None,
    }
    assert _clash_count(scenario) == 1  # untouched when off
