"""A lock freezes a section's meeting times, never its room.

Regression for scenario 26 (2026-10-19): CS112 S1 and S3 were both locked into
one room at SUN/TUE 09:00. Free compatible rooms existed, yet every Optimise
Current kept the double-booking because recovery rooming pinned locked rooms.
Externally owned sections (also placed by another scenario) stay fully frozen.
"""

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_exact_rooming import ExactRoomingError
from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
from core.services.timetable_secondary_recovery_runner import (
    _persist_exact_room_result,
    _sync_recovered_meetings,
)
from core.services.timetable_v2_runner import run_v2_optimisation_guarded

pytestmark = pytest.mark.django_db

SLOTS = (("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15"))
TIME_FIELDS = ("id", "board_id", "term_section_id", "day", "start_time", "end_time", "is_locked")


@pytest.fixture
def double_booked(settings):
    """Two locked sections of one course share room R1 at the same times; R2 is free."""
    settings.TIMETABLE_ENFORCE_LOCKS = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = True
    settings.TIMETABLE_CANDIDATE_GEN_BUDGET_SECONDS = 0.01
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 0
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0.01
    settings.TIMETABLE_SECONDARY_RECOVERY_SECONDS = 0.01
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="Locked double booking", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 2", program="AI", nominal_term=2
    )
    for code in ("R1", "R2"):
        Room.objects.create(
            room_code=code, capacity=50, room_type="lecture", department="AI", section="M"
        )
    Student.objects.create(student_id=990101, registration_no="990101", name="S", program="AI")
    ScenarioStudentMap.objects.create(
        scenario=scenario, student_id=990101, primary_term=2, recommended_courses=["CS112"]
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=990101,
        course_key="CS112",
        course_code="CS112",
        primary_term=2,
    )
    # Two planned sections exist, so the required-section pre-step stays a no-op.
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="CS112",
        course_code="CS112",
        course_name="CS112",
        department="AI",
        credit_hours=3,
        planned_sections=2,
        max_per_section=30,
        total_demand=2,
        programme_term=2,
    )
    sections = []
    for label in ("S1", "S3"):
        section = TermSection.objects.create(
            scenario=scenario,
            course_key="CS112",
            course_code="CS112",
            course_number="CS112",
            course_name="CS112",
            section=label,
            source_tag="tw_auto",
            available_capacity=30,
        )
        for day, start, end in SLOTS:
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
                room="R1",
                is_locked=True,
            )
            TermSectionMeeting.objects.create(
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
                room="R1",
                instructor="",
                building="Original",
                floor_wing="F1",
            )
        sections.append(section)
    return scenario, board, sections


def _time_projection(scenario):
    return list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by("pk")
        .values(*TIME_FIELDS)
    )


def _rooms_by_slot(scenario):
    rooms: dict[str, set[str]] = {}
    for p in SectionPlacement.objects.filter(board__scenario=scenario):
        rooms.setdefault(p.day, set()).add(p.room)
    return rooms


def _externalise(sections):
    other = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="External owner", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=other, label="External", nominal_term=2, program="AI"
    )
    for section in sections:
        for p in SectionPlacement.objects.filter(term_section=section):
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=p.day,
                start_time=p.start_time,
                end_time=p.end_time,
                room=p.room,
            )


def test_optimise_current_rerooms_locked_double_booking_without_moving_times(double_booked):
    scenario, _board, _sections = double_booked
    times_before = _time_projection(scenario)
    before = build_recovery_room_evaluator(scenario.pk).original_room_report
    assert before["original_room_conflict_meetings"] == 4

    result = run_v2_optimisation_guarded(
        scenario.pk, mode="current", max_iterations=0, run_chain=False, run_cpsat=False
    )

    assert not result.get("error"), result.get("error")
    assert not result.get("safety_blocked"), result.get("safety_regression")
    assert result["secondary_recovery"]["applied"] is True
    assert _time_projection(scenario) == times_before
    # Each slot now uses both rooms: the sections are no longer double-booked.
    assert _rooms_by_slot(scenario) == {"SUN": {"R1", "R2"}, "TUE": {"R1", "R2"}}
    for p in SectionPlacement.objects.filter(board__scenario=scenario):
        meeting = TermSectionMeeting.objects.get(
            term_section_id=p.term_section_id, day=p.day, start_time=p.start_time
        )
        assert (meeting.room, meeting.building, meeting.floor_wing) == (p.room, "Original", "F1")
    after = build_recovery_room_evaluator(scenario.pk).original_room_report
    assert after["original_room_conflict_meetings"] == 0
    assert after["original_room_shortages"] == 0


def test_recovery_oracle_reroom_locked_meetings_but_keeps_times_frozen(double_booked):
    scenario, _board, _sections = double_booked
    evaluator = build_recovery_room_evaluator(scenario.pk)

    result = evaluator(None)

    assert result["unassigned_count"] == 0
    assert not any(f["reason"].startswith("LOCKED_") for f in result["failures"])
    by_slot: dict[str, set[str]] = {}
    for pid, (day, _start, _end) in result["candidate_placements_by_id"].items():
        by_slot.setdefault(day, set()).add(result["assignment_by_placement_id"][pid])
    assert by_slot == {"SUN": {"R1", "R2"}, "TUE": {"R1", "R2"}}
    # The time freeze is untouched: both locked sections remain unmovable.
    assert evaluator.locked_sections == {"CS112_S1", "CS112_S3"}


def test_externally_owned_double_booking_stays_frozen(double_booked):
    scenario, _board, sections = double_booked
    _externalise(sections)
    rows_before = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    evaluator = build_recovery_room_evaluator(scenario.pk)

    oracle = evaluator(None)

    assert {f["reason"] for f in oracle["failures"]} == {"LOCKED_ROOM_CONFLICT"}
    result = run_v2_optimisation_guarded(
        scenario.pk, mode="current", max_iterations=0, run_chain=False, run_cpsat=False
    )
    assert not result.get("error"), result.get("error")
    assert list(SectionPlacement.objects.filter(board__scenario=scenario).values()) == rows_before
    target = SectionPlacement.objects.filter(board__scenario=scenario).first()
    bad = {
        "candidate_placements_by_id": {target.pk: (target.day, target.start_time, target.end_time)},
        "assignment_by_placement_id": {target.pk: "R2"},
    }
    with pytest.raises(ValueError, match="externally owned"):
        _persist_exact_room_result(scenario.pk, bad, rows_before)


def _single_locked_row(scenario, room="R1"):
    row = SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").first()
    SectionPlacement.objects.filter(pk=row.pk).update(room=room)
    row.refresh_from_db()
    return row


def test_writer_applies_room_only_change_to_locked_row(double_booked):
    scenario, _board, _sections = double_booked
    row = _single_locked_row(scenario)
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    result = {
        "candidate_placements_by_id": {row.pk: (row.day, row.start_time, row.end_time)},
        "assignment_by_placement_id": {row.pk: "R2"},
    }

    assert _persist_exact_room_result(scenario.pk, result, rows) == 1

    after = SectionPlacement.objects.get(pk=row.pk)
    assert (after.day, after.start_time, after.end_time, after.room) == (
        row.day,
        row.start_time,
        row.end_time,
        "R2",
    )
    assert (after.is_locked, after.created_at, after.updated_at) == (
        True,
        row.created_at,
        row.updated_at,
    )


def test_writer_rejects_moving_a_locked_rows_time(double_booked):
    scenario, _board, _sections = double_booked
    row = _single_locked_row(scenario)
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    moved = {
        "candidate_placements_by_id": {row.pk: ("MON", row.start_time, row.end_time)},
        "assignment_by_placement_id": {row.pk: "R1"},
    }

    with pytest.raises(ValueError, match="locked placement's time"):
        _persist_exact_room_result(scenario.pk, moved, rows)
    assert SectionPlacement.objects.get(pk=row.pk).day == row.day


@pytest.mark.parametrize("room", ["", "UNASSIGNED"])
def test_writer_and_sync_keep_an_unroomable_locked_rows_representation(double_booked, room):
    scenario, _board, _sections = double_booked
    row = _single_locked_row(scenario, room)
    TermSectionMeeting.objects.filter(
        term_section_id=row.term_section_id, day=row.day, start_time=row.start_time
    ).update(room=room)
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario).order_by("pk").values()
    )
    result = {
        "candidate_placements_by_id": {row.pk: (row.day, row.start_time, row.end_time)},
        "assignment_by_placement_id": {row.pk: ""},
    }

    assert _persist_exact_room_result(scenario.pk, result, rows) == 0
    _sync_recovered_meetings(rows, meetings, result, scenario_id=scenario.pk)

    assert SectionPlacement.objects.get(pk=row.pk).room == room
    assert (
        list(
            TermSectionMeeting.objects.filter(term_section__scenario=scenario)
            .order_by("pk")
            .values()
        )
        == meetings
    )


def test_sync_updates_a_locked_meetings_room_only(double_booked):
    scenario, _board, _sections = double_booked
    row = _single_locked_row(scenario)
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario).order_by("pk").values()
    )
    result = {
        "candidate_placements_by_id": {row.pk: (row.day, row.start_time, row.end_time)},
        "assignment_by_placement_id": {row.pk: "R2"},
    }

    _sync_recovered_meetings(rows, meetings, result, scenario_id=scenario.pk)

    original = next(
        m
        for m in meetings
        if (m["term_section_id"], m["day"], m["start_time"])
        == (row.term_section_id, row.day, row.start_time)
    )
    # Identity and every other field (instructor, building, audit) are kept.
    assert TermSectionMeeting.objects.filter(pk=original["id"]).values().get() == {
        **original,
        "room": "R2",
    }
    others = sorted((m for m in meetings if m["id"] != original["id"]), key=lambda m: m["id"])
    assert (
        list(
            TermSectionMeeting.objects.exclude(pk=original["id"])
            .filter(term_section__scenario=scenario)
            .order_by("pk")
            .values()
        )
        == others
    )

    moved = {
        "candidate_placements_by_id": {row.pk: ("MON", row.start_time, row.end_time)},
        "assignment_by_placement_id": {row.pk: "R2"},
    }
    with pytest.raises(ExactRoomingError, match="locked meeting's time"):
        _sync_recovered_meetings(rows, meetings, moved, scenario_id=scenario.pk)
