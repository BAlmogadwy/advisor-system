"""Tests for the scenario-wide exact room assignment + shortfall decomposition.

Two layers:

* the solver core (`_solve_relaxed`, `_greedy_room_assignment`) is pinned on
  synthetic `MeetingDemand`s so feasibility, count-contention, capacity-shortfall
  and change-minimisation are proven without a full board build; and
* the DB-facing entry (`plan_exact_rooming` / `build_meeting_demands`) plus the
  read-only endpoint are exercised against small in-DB scenarios built via the
  ORM, covering the feasible path, the structural buckets, and the fail-closed
  error paths (degenerate intervals, cross-board gender conflict).
"""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_exact_rooming import (
    ExactRoomingError,
    MeetingDemand,
    _greedy_room_assignment,
    _solve_relaxed,
    build_meeting_demands,
    plan_exact_rooming,
)


def _demand(demand_id, start, end, rooms, cap, *, day="SUN", incumbent="") -> MeetingDemand:
    return MeetingDemand(
        demand_id=demand_id,
        term_section_id=hash(demand_id) & 0xFFFF,
        course_code=demand_id.split("#")[0],
        day=day,
        start_min=start,
        end_min=end,
        required_type="lecture",
        required_capacity=cap,
        required_gender="M",
        required_programmes=frozenset({"AI"}),
        placement_ids=(1,),
        board_ids=(1,),
        incumbent_rooms=(incumbent,) if incumbent else ("",),
        compatible_rooms=tuple(rooms),
    )


def test_feasible_board_rooms_everyone_zero_shortfall():
    caps = {"R1": 40, "R2": 40}
    demands = [
        _demand("A#SUN#09:00", 540, 615, ["R1", "R2"], 30),
        _demand("B#SUN#09:00", 540, 615, ["R1", "R2"], 30),  # same slot, needs the other room
        _demand("C#SUN#10:30", 630, 705, ["R1", "R2"], 30),
    ]
    result = _solve_relaxed(demands, caps, time_limit_seconds=10)
    assert result.proven_optimal
    assert result.unroomed == ()
    assert result.shortfall == 0
    assert len(result.assignment) == 3
    # A and B overlap and must not share a room.
    assert result.assignment["A#SUN#09:00"] != result.assignment["B#SUN#09:00"]


def test_room_count_contention_leaves_minimum_unroomed():
    # Three meetings at one instant, two rooms → exactly one must go unroomed.
    caps = {"R1": 40, "R2": 40}
    demands = [
        _demand("A#SUN#09:00", 540, 615, ["R1", "R2"], 30),
        _demand("B#SUN#09:00", 540, 615, ["R1", "R2"], 30),
        _demand("C#SUN#09:00", 540, 615, ["R1", "R2"], 30),
    ]
    result = _solve_relaxed(demands, caps, time_limit_seconds=10)
    assert result.proven_optimal
    assert len(result.unroomed) == 1
    assert len(result.assignment) == 2


def test_capacity_shortfall_is_minimised_not_prohibited():
    # Two overlapping meetings, one big room and one small; the big-demand
    # meeting must take the big room, the other absorbs the shortfall.
    caps = {"BIG": 40, "SMALL": 20}
    demands = [
        _demand("A#SUN#09:00", 540, 615, ["BIG", "SMALL"], 35),
        _demand("B#SUN#09:00", 540, 615, ["BIG", "SMALL"], 35),
    ]
    result = _solve_relaxed(demands, caps, time_limit_seconds=10)
    assert result.proven_optimal
    assert result.unroomed == ()  # both roomed — capacity is relaxed, not a filter
    assert result.shortfall == 15  # 35 - 20 for whoever lands in SMALL
    assert result.assignment["A#SUN#09:00"] != result.assignment["B#SUN#09:00"]


def test_change_minimisation_keeps_incumbent_rooms():
    # Non-overlapping meetings that both already sit in valid rooms should not move.
    caps = {"R1": 40, "R2": 40}
    demands = [
        _demand("A#SUN#09:00", 540, 615, ["R1", "R2"], 30, incumbent="R1"),
        _demand("B#SUN#10:30", 630, 705, ["R1", "R2"], 30, incumbent="R2"),
    ]
    result = _solve_relaxed(demands, caps, time_limit_seconds=10)
    assert result.proven_optimal
    assert result.changes == 0
    assert result.assignment["A#SUN#09:00"] == "R1"
    assert result.assignment["B#SUN#10:30"] == "R2"


def test_greedy_fallback_is_total_and_valid():
    caps = {"R1": 40, "R2": 30}
    demands = [
        _demand("A#SUN#09:00", 540, 615, ["R1", "R2"], 35),
        _demand("B#SUN#09:00", 540, 615, ["R1", "R2"], 25),
        _demand("C#SUN#09:00", 540, 615, ["R1", "R2"], 25),  # 3rd at the instant → unroomed
    ]
    greedy = _greedy_room_assignment(demands, caps)
    # No room double-booked at the shared instant.
    rooms_used = [greedy[k] for k in greedy]
    assert len(rooms_used) == len(set(rooms_used))
    # Best-fit-decreasing: the 35-demand takes the only big-enough room R1.
    assert greedy["A#SUN#09:00"] == "R1"


def test_online_style_empty_demandset_is_trivially_feasible():
    result = _solve_relaxed([], {"R1": 40}, time_limit_seconds=5)
    assert result.proven_optimal
    assert result.assignment == {}
    assert result.unroomed == ()


def test_determinism_same_input_same_assignment():
    caps = {"R1": 40, "R2": 40, "R3": 40}
    demands = [_demand(f"D{i}#SUN#09:00", 540, 615, ["R1", "R2", "R3"], 20) for i in range(3)]
    first = _solve_relaxed(demands, caps, time_limit_seconds=10)
    second = _solve_relaxed(demands, caps, time_limit_seconds=10)
    assert first.assignment == second.assignment


# ── DB-facing tests ───────────────────────────────────────────────────────────

pytestmark = pytest.mark.django_db


def _scenario(name="exact rooming"):
    return TimetableScenario.objects.create(academic_year="1448", term="1", name=name)


def _board(scenario, program="AI", label="Term 1"):
    return DeliveryBoard.objects.create(
        scenario=scenario, label=label, nominal_term=1, program=program, display_order=1
    )


def _budget(scenario, code, *, credit=3, demand=20, department="AI"):
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=code,
        department=department,
        credit_hours=credit,
        planned_sections=1,
        max_per_section=40,
        total_demand=demand,
        programme_term=1,
    )


def _section(scenario, code):
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


def _room(code, *, capacity=60, room_type="lecture", department="AI", section=""):
    return Room.objects.create(
        room_code=code,
        capacity=capacity,
        room_type=room_type,
        department=department,
        section=section,
    )


def _place(board, section_obj, *, day="SUN", start="09:00", end="10:15", room=""):
    return SectionPlacement.objects.create(
        board=board,
        term_section=section_obj,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
    )


def test_db_feasible_scenario_rooms_everyone():
    scn = _scenario()
    board = _board(scn)
    _room("AI-R1", capacity=40)
    _room("AI-R2", capacity=40)
    for code, day in (("AI101", "SUN"), ("AI102", "SUN"), ("AI103", "MON")):
        _budget(scn, code)
        _place(board, _section(scn, code), day=day)  # AI101/AI102 overlap on SUN 09:00
    report = plan_exact_rooming(scn.id, time_limit_seconds=10)
    assert report.proven_optimal
    assert report.feasible
    assert report.roomed_meetings == 3
    assert report.total_shortfall == 0
    assert not report.no_compatible_room
    # The two SUN 09:00 meetings must land in different rooms.
    sun_rooms = [room for did, room in report.assignment.items() if "#SUN#09:00" in did]
    assert len(sun_rooms) == 2
    assert sun_rooms[0] != sun_rooms[1]


def test_db_no_compatible_room_bucket_for_missing_lab():
    scn = _scenario()
    board = _board(scn)
    _room("AI-LEC", capacity=60, room_type="lecture")  # no lab room in inventory
    _budget(scn, "AI200", credit=4)  # 4-credit + 100-min meeting → requires a lab room
    _place(board, _section(scn, "AI200"), start="09:00", end="10:40")
    report = plan_exact_rooming(scn.id, time_limit_seconds=10)
    assert len(report.no_compatible_room) == 1
    assert report.no_compatible_room[0]["required_type"] == "lab"
    assert not report.feasible


def test_db_degenerate_interval_fails_closed():
    scn = _scenario()
    board = _board(scn)
    _room("AI-R1")
    _budget(scn, "AI101")
    _place(board, _section(scn, "AI101"), start="09:00", end="09:00")  # non-positive interval
    with pytest.raises(ExactRoomingError):
        plan_exact_rooming(scn.id, time_limit_seconds=5)


def test_db_empty_scenario_reports_empty():
    scn = _scenario()
    _board(scn)
    report = plan_exact_rooming(scn.id, time_limit_seconds=5)
    assert report.status == "EMPTY"
    assert report.physical_meetings == 0
    assert report.feasible


def test_db_plan_is_read_only():
    scn = _scenario()
    board = _board(scn)
    _room("AI-R1", capacity=40)
    _budget(scn, "AI101")
    placement = _place(board, _section(scn, "AI101"), room="PRE-EXISTING")
    plan_exact_rooming(scn.id, time_limit_seconds=5)
    placement.refresh_from_db()
    assert placement.room == "PRE-EXISTING"  # the preview never writes


def test_db_cross_board_meeting_requires_every_boards_programme():
    """A meeting shared across an AI board and a CS board must land in a room that
    serves BOTH programmes — the auditor validates each placement against its own
    board, so a union that admits an AI-only room would be a false pass."""
    scn = _scenario()
    ai_board = _board(scn, program="AI", label="AI Term 1")
    cs_board = _board(scn, program="CS", label="CS Term 1")
    _room("AI-ONLY", capacity=40, department="AI")  # serves AI, not CS
    _budget(scn, "SHARED", department="AI,CS")
    section = _section(scn, "SHARED")
    # Same section, same slot, on both boards → one physical meeting.
    _place(ai_board, section, day="SUN", start="09:00", end="10:15")
    _place(cs_board, section, day="SUN", start="09:00", end="10:15")

    demands = build_meeting_demands(scn.id)
    assert len(demands) == 1
    assert len(demands[0].board_ids) == 2
    # AI-ONLY serves AI but not CS → intersection over boards excludes it.
    assert demands[0].compatible_rooms == ()

    report = plan_exact_rooming(scn.id, time_limit_seconds=5)
    assert len(report.no_compatible_room) == 1
    assert not report.feasible


def test_db_online_meeting_needs_no_room():
    scn = _scenario()
    board = _board(scn)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _room("AI-R1", capacity=40)
    _budget(scn, "GS101")
    _place(board, _section(scn, "GS101"))
    demands = build_meeting_demands(scn.id)
    assert demands == []  # online meeting is excluded, not a room demand
