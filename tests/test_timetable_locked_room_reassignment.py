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


@pytest.fixture
def compaction_settings(settings):
    settings.TIMETABLE_ENFORCE_LOCKS = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 4
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0


def _locked_extra_section(scenario, code, start, end, room):
    board = DeliveryBoard.objects.get(scenario=scenario, label="T1")
    section = TermSection.objects.create(
        scenario=scenario,
        course_key=code,
        course_code=code,
        course_number=code,
        course_name=code,
        section="S1",
        source_tag="tw_auto",
        available_capacity=30,
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="MON",
        start_time=start,
        end_time=end,
        room=room,
        is_locked=True,
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="MON", start_time=start, end_time=end, room=room, instructor=""
    )
    return section


@pytest.mark.usefixtures("compaction_settings")
def test_compaction_accepts_a_locked_meeting_rerooming_by_the_shared_exact_pass():
    """Moving a shared section re-rooms the scenario exactly; that may re-room a
    locked meeting (placement and canonical meeting) without rolling back."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules
    from tests.test_instructor_compaction import _gappy_board

    scenario = _gappy_board()
    Room.objects.create(
        room_code="R2", department="AI", capacity=50, room_type="lecture", section="M"
    )
    SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_code="C1"
    ).update(is_locked=True)
    # A second locked section double-booked with C1 at MON 09:00 in R1.
    _locked_extra_section(scenario, "L1", "09:00", "10:15", "R1")
    # C2 has a copy on another board, so moving it triggers the exact re-room.
    c2 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C2")
    shared = DeliveryBoard.objects.create(
        scenario=scenario, label="Shared", nominal_term=2, program="AI"
    )
    SectionPlacement.objects.create(
        board=shared,
        term_section=c2.term_section,
        day=c2.day,
        start_time=c2.start_time,
        end_time=c2.end_time,
        room=c2.room,
    )
    locked_ids = list(
        SectionPlacement.objects.filter(board__scenario=scenario, is_locked=True).values_list(
            "pk", flat=True
        )
    )
    times_before = list(
        SectionPlacement.objects.filter(pk__in=locked_ids).order_by("pk").values(*TIME_FIELDS)
    )

    report = compact_instructor_schedules(scenario.pk)

    assert not report.get("rollback_reason"), report.get("rollback_reason")
    assert report["search"]["moves_accepted"] == 1
    locked = SectionPlacement.objects.filter(pk__in=locked_ids).order_by("pk")
    assert list(locked.values(*TIME_FIELDS)) == times_before
    assert {p.room for p in locked} == {"R1", "R2"}
    moved = [p for p in locked if p.room == "R2"]
    meeting = TermSectionMeeting.objects.get(
        term_section_id=moved[0].term_section_id, day="MON", start_time="09:00"
    )
    assert meeting.room == "R2"


@pytest.mark.usefixtures("compaction_settings")
def test_classic_persist_guard_accepts_rooming_a_locked_unroomed_meeting(monkeypatch):
    from core.services import timetable_local_search as sa
    from tests.test_instructor_compaction import _gappy_board

    scenario = _gappy_board()
    locked = _locked_extra_section(scenario, "L1", "10:30", "11:45", "")
    board = DeliveryBoard.objects.get(scenario=scenario, label="T1")
    sections = [
        {"code": ts.course_key, "label": ts.section, "term_section_id": ts.pk}
        for ts in TermSection.objects.filter(scenario=scenario).order_by("pk")
    ]
    current = {
        p.term_section_id: (p.day, p.start_time, p.end_time)
        for p in SectionPlacement.objects.filter(board=board)
    }
    candidate = {
        "status": "optimized",
        "schedule": {
            i: [dict(zip(("day", "start", "end"), current[sec["term_section_id"]], strict=True))]
            for i, sec in enumerate(sections)
        },
        "sections": sections,
        "cost_before": 200,
        "cost_after": 100,
        "improvements": 1,
    }
    monkeypatch.setattr(sa, "optimize_board", lambda *a, **k: candidate)

    result = sa.optimize_and_persist_board(board.pk)

    assert not result.get("persist_rolled_back"), result.get("rollback_reasons")
    row = SectionPlacement.objects.get(term_section=locked)
    assert (row.day, row.start_time, row.end_time, row.is_locked) == (
        "MON",
        "10:30",
        "11:45",
        True,
    )
    assert row.room == "R1"


def _demand(sid, room, cap, rooms):
    from core.services.timetable_exact_rooming import MeetingDemand

    return MeetingDemand(
        demand_id=f"{sid}#SUN#540-615",
        term_section_id=sid,
        course_code=f"C{sid}",
        day="SUN",
        start_min=540,
        end_min=615,
        required_type="lecture",
        required_capacity=cap,
        required_gender="M",
        required_programmes=frozenset({"AI"}),
        placement_ids=(sid,),
        board_ids=(1,),
        incumbent_rooms=(room,),
        compatible_rooms=rooms,
    )


def _oracle(demands, capacities):
    from core.services.timetable_recovery_rooming import RecoveryRoomEvaluator

    return RecoveryRoomEvaluator(
        demands,
        {code: {"capacity": cap} for code, cap in capacities.items()},
        {d.term_section_id: f"C{d.term_section_id}_1" for d in demands},
        {d.term_section_id: 3 for d in demands},
        time_limit_seconds=1,
        frozen_section_ids={"C1_1"},  # section 1 is user-locked (time frozen)
    )


def test_locked_meeting_in_a_valid_room_is_never_dropped_for_an_unlocked_one():
    # Only R1 fits either meeting; the larger unlocked meeting would win R1 on a
    # bare count tie. A lock lets the room change, never lets it be lost.
    oracle = _oracle([_demand(1, "R1", 30, ("R1",)), _demand(2, "", 40, ("R1",))], {"R1": 40})

    result = oracle(None)

    assert result["assignment_by_placement_id"] == {1: "R1", 2: ""}
    assert result["unassigned_count"] == 1


def test_locked_meeting_still_moves_room_when_that_seats_both():
    # The locked meeting may move from R1 to R2 so the unlocked one can use R1.
    oracle = _oracle(
        [_demand(1, "R1", 30, ("R1", "R2")), _demand(2, "", 40, ("R1",))],
        {"R1": 40, "R2": 30},
    )

    result = oracle(None)

    assert result["assignment_by_placement_id"] == {1: "R2", 2: "R1"}
    assert result["unassigned_count"] == 0


def test_recovery_oracle_rejects_a_candidate_that_moves_a_locked_time(double_booked):
    from core.services.timetable_assignment_models import SectionMeeting
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario, _board, _sections = double_booked
    states = {s.section_id: s for s in build_section_states_for_scenario(scenario.pk)}
    evaluator = build_recovery_room_evaluator(scenario.pk)
    moved = states["CS112_S1"]
    first, *rest = sorted(moved.meetings, key=lambda m: (m.day, m.start_min))
    # Same contact durations, one meeting shifted to another day (WED).
    moved.meetings = [
        SectionMeeting(day=3, start_min=first.start_min, end_min=first.end_min),
        *rest,
    ]

    with pytest.raises(ExactRoomingError, match="moved locked section"):
        evaluator(list(states.values()))


def _evaluator(demands, capacities, locked_sections):
    from core.services.timetable_recovery_rooming import RecoveryRoomEvaluator

    return RecoveryRoomEvaluator(
        demands,
        {code: {"capacity": cap} for code, cap in capacities.items()},
        {d.term_section_id: f"C{d.term_section_id}_1" for d in demands},
        {d.term_section_id: 3 for d in demands},
        time_limit_seconds=1,
        frozen_section_ids=locked_sections,
    )


def _timed(demand, start, end):
    from dataclasses import replace

    return replace(
        demand,
        demand_id=f"{demand.term_section_id}#SUN#{start}-{end}",
        start_min=start,
        end_min=end,
    )


def test_locked_room_yields_only_when_that_seats_more_meetings():
    # Two unlocked meetings fit R1 back to back, but both overlap the locked one.
    # The unroomed count is still minimised exactly: 1 beats keeping the lock (2).
    locked = _timed(_demand(1, "R1", 30, ("R1",)), 540, 640)
    first = _timed(_demand(2, "", 30, ("R1",)), 540, 615)
    second = _timed(_demand(3, "", 30, ("R1",)), 630, 705)

    result = _evaluator([locked, first, second], {"R1": 40}, {"C1_1"})(None)

    assert result["assignment_by_placement_id"] == {1: "", 2: "R1", 3: "R1"}
    assert result["unassigned_count"] == 1
    assert result["count_proven_optimal"] is True


def test_room_count_outranks_any_number_of_locked_tie_breaks():
    # Dropping both locked meetings seats all three unlocked ones (2 unroomed
    # instead of 3). The count must win however many locks yield for it.
    first_lock = _timed(_demand(1, "R1", 30, ("R1",)), 540, 600)
    second_lock = _timed(_demand(4, "R1", 30, ("R1",)), 600, 660)
    early = _timed(_demand(2, "", 30, ("R1",)), 510, 570)
    middle = _timed(_demand(3, "", 30, ("R1",)), 570, 630)
    late = _timed(_demand(5, "", 30, ("R1",)), 630, 690)

    result = _evaluator(
        [first_lock, second_lock, early, middle, late], {"R1": 40}, {"C1_1", "C4_1"}
    )(None)

    assert result["assignment_by_placement_id"] == {1: "", 4: "", 2: "R1", 3: "R1", 5: "R1"}
    assert result["unassigned_count"] == 2
    assert result["count_proven_optimal"] is True


def test_double_booked_locked_meetings_both_keep_a_room_before_an_unlocked_one():
    # Locked 1 and 3 are double-booked in R1; R2 is spare. On the count tie the
    # larger unlocked meeting yields, so both locked meetings stay roomed.
    demands = [
        _demand(1, "R1", 30, ("R1", "R2")),
        _demand(3, "R1", 30, ("R1", "R2")),
        _demand(2, "", 40, ("R1", "R2")),
    ]

    result = _evaluator(demands, {"R1": 40, "R2": 40}, {"C1_1", "C3_1"})(None)

    rooms = result["assignment_by_placement_id"]
    assert {rooms[1], rooms[3]} == {"R1", "R2"} and rooms[2] == ""
    assert result["count_proven_optimal"] is True


def test_unlocked_incumbent_rooms_get_no_tie_break():
    # Without a lock the previous count-only behaviour stands: the larger
    # unroomed meeting may take R1 from its current, smaller holder.
    demands = [_demand(1, "R1", 30, ("R1",)), _demand(2, "", 40, ("R1",))]

    result = _evaluator(demands, {"R1": 40}, set())(None)

    assert result["assignment_by_placement_id"] == {1: "", 2: "R1"}


def test_solver_fallback_keeps_a_locked_incumbent_room(settings):
    # A model past the element budget is answered by the greedy warm start.
    from core.services.timetable_exact_rooming import _solve_relaxed

    settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 1
    locked, unlocked = _demand(1, "R1", 30, ("R1",)), _demand(2, "", 40, ("R1",))

    result = _solve_relaxed(
        [locked, unlocked],
        {"R1": 40},
        time_limit_seconds=1,
        strict_budget=True,
        count_only=True,
        keep_roomed={locked.demand_id},
    )

    assert result.status == "UNKNOWN"
    assert result.assignment == {locked.demand_id: "R1"}


def test_greedy_fallback_keeps_a_locked_incumbent_room():
    # The greedy warm start doubles as the timeout fallback; it must honour the
    # same rule instead of handing R1 to the larger unlocked meeting first.
    from core.services.timetable_exact_rooming import _greedy_room_assignment

    locked, unlocked = _demand(1, "R1", 30, ("R1",)), _demand(2, "", 40, ("R1",))

    assert _greedy_room_assignment([locked, unlocked], {"R1": 40}) == {unlocked.demand_id: "R1"}
    assert _greedy_room_assignment(
        [locked, unlocked], {"R1": 40}, keep_roomed={locked.demand_id}
    ) == {locked.demand_id: "R1"}


def test_solver_fallback_never_trades_the_count_for_a_lock(settings):
    # Seating the lock first would strand both unlocked meetings (2 unroomed);
    # the plain greedy seats them back to back (1 unroomed), so it is kept.
    from core.services.timetable_exact_rooming import _solve_relaxed

    settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 1
    locked = _timed(_demand(1, "R1", 30, ("R1",)), 540, 640)
    first = _timed(_demand(2, "", 40, ("R1",)), 540, 615)
    second = _timed(_demand(3, "", 40, ("R1",)), 630, 705)

    result = _solve_relaxed(
        [locked, first, second],
        {"R1": 40},
        time_limit_seconds=1,
        strict_budget=True,
        count_only=True,
        keep_roomed={locked.demand_id},
    )

    assert result.status == "UNKNOWN"
    assert result.assignment == {first.demand_id: "R1", second.demand_id: "R1"}


def test_tie_break_pin_is_reserved_only_when_locks_are_present(settings):
    # Lock-free callers keep the exact model-size admission they had before;
    # only a solve with a tie-break phase reserves one more pin.
    from core.services.timetable_exact_rooming import _solve_relaxed

    locked, unlocked = _demand(1, "R1", 30, ("R1",)), _demand(2, "", 40, ("R1",))

    def status(budget, keep):
        settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = budget
        return _solve_relaxed(
            [locked, unlocked],
            {"R1": 40},
            time_limit_seconds=1,
            strict_budget=True,
            count_only=True,
            keep_roomed=keep,
        ).status

    smallest = next(b for b in range(1, 200) if status(b, ()) != "UNKNOWN")

    assert status(smallest, {locked.demand_id}) == "UNKNOWN"
    assert status(smallest + 1, {locked.demand_id}) != "UNKNOWN"
