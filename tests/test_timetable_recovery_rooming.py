"""Recovery room checks preserve physical feasibility and placement identity."""

from dataclasses import replace

import pytest

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_exact_rooming import ExactRoomingError, MeetingDemand, _solve_relaxed
from core.services.timetable_recovery_rooming import RecoveryRoomEvaluator


def demand(sid, start=540, end=615, rooms=("R1", "R2"), *, cap=20, pids=None):
    return MeetingDemand(
        demand_id=f"{sid}#SUN#{start}-{end}",
        term_section_id=sid,
        course_code=f"C{sid}",
        day="SUN",
        start_min=start,
        end_min=end,
        required_type="lecture",
        required_capacity=cap,
        required_gender="M",
        required_programmes=frozenset({"AI"}),
        placement_ids=pids or (sid,),
        board_ids=(1,),
        incumbent_rooms=("",),
        compatible_rooms=rooms,
    )


def state(sid, meetings):
    return SectionState(f"C{sid}_1", f"C{sid}", meetings, 40, 4)


def evaluator(demands, *, locked=None, capacities=None, credits=None, seconds=1):
    capacities = capacities or {"R1": 40, "R2": 40}
    return RecoveryRoomEvaluator(
        demands,
        {r: {"capacity": cap} for r, cap in capacities.items()},
        {d.term_section_id: f"C{d.term_section_id}_1" for d in demands},
        credits or {d.term_section_id: 3 for d in demands},
        locked,
        time_limit_seconds=seconds,
    )


def test_exact_global_swap_recovers_greedy_shortage():
    # Flexible A takes R1 in greedy, stranding restricted B. Exact swap fixes it.
    check = evaluator([demand(1), demand(2, rooms=("R1",))])
    result = check(None)
    assert result["unassigned_count"] == 0
    assert result["assignment_by_placement_id"] == {1: "R2", 2: "R1"}
    assert result["count_proven_optimal"]


def test_locked_room_and_partial_overlap_block_cross_board_meeting():
    a = demand(1, rooms=("R1",))
    b = replace(demand(2, 600, 675, rooms=("R1",)), board_ids=(2,))
    check = evaluator([a, b], locked={1: "R1"})
    result = check(None)
    assert result["unassigned_count"] == 1
    assert result["assignment_by_placement_id"] == {1: "R1", 2: ""}
    assert result["failures"][0]["reason"] == "ROOM_OCCUPIED"
    # Endpoint-touching intervals can reuse the room.
    moved = [state(1, [SectionMeeting(0, 540, 615)]), state(2, [SectionMeeting(0, 615, 690)])]
    assert check(moved)["unassigned_count"] == 0


def test_invalid_locked_room_preserved_and_not_reported_as_feasible():
    check = evaluator([demand(1, cap=45)], locked={1: "R1"})
    result = check(None)
    assert result["unassigned_count"] == result["shortage_lower_bound"] == 1
    assert result["assignment_by_placement_id"][1] == "R1"
    assert result["failures"][0]["reason"] == "LOCKED_ROOM_INVALID"


def test_conflicting_locked_rooms_are_both_reported_and_remain_reserved():
    check = evaluator([demand(1), demand(2), demand(3, rooms=("R1",))], locked={1: "R1", 2: "R1"})
    result = check(None)
    assert result["unassigned_count"] == 3
    assert result["assignment_by_placement_id"] == {1: "R1", 2: "R1", 3: ""}
    assert [f["reason"] for f in result["failures"]].count("LOCKED_ROOM_CONFLICT") == 2


def test_labs_use_buffered_capacity_and_candidate_cannot_shrink_demand():
    d = replace(demand(1, end=640, cap=29), required_type="lab", compatible_rooms=("LAB",))
    check = evaluator([d], capacities={"LAB": 25}, credits={1: 4})
    s = state(1, [SectionMeeting(0, 540, 640)])
    s.demand_capacity = 1
    result = check([s])
    assert result["unassigned_count"] == 1
    assert result["shortage_lower_bound"] == 1
    assert result["assignment_by_placement_id"][1] == ""
    assert result["failures"][0]["required_capacity"] == 29
    assert result["failures"][0]["best_capacity"] == 25


def test_shared_meeting_is_one_demand_with_same_room_on_every_row():
    check = evaluator([demand(1, pids=(11, 12))])
    same = SectionMeeting(0, 540, 615)
    result = check([state(1, [same, same])])
    assert result["physical_meetings"] == 1
    assert result["assignment_by_placement_id"][11] == result["assignment_by_placement_id"][12]


def test_mixed_lab_and_lecture_mapping_preserves_duration_and_original_ids():
    lecture = demand(1, pids=(11,))
    lab = replace(demand(1, 630, 730, ("LAB",), pids=(12,)), required_type="lab")
    check = evaluator([lecture, lab], capacities={"R1": 40, "R2": 40, "LAB": 25}, credits={1: 4})
    result = check([state(1, [SectionMeeting(2, 600, 700), SectionMeeting(3, 630, 705)])])
    assert result["unassigned_count"] == 0
    assert result["candidate_placements_by_id"] == {
        11: ("WED", "10:30", "11:45"),
        12: ("TUE", "10:00", "11:40"),
    }
    assert result["assignment_by_placement_id"][12] == "LAB"


def test_any_lock_freezes_whole_section_times_and_duration_changes_fail_closed():
    lecture = demand(1, pids=(11,))
    other = demand(1, 630, 705, pids=(12,))
    check = evaluator([lecture, other], locked={11: "R1"})
    with pytest.raises(ExactRoomingError, match="locked section"):
        check([state(1, [SectionMeeting(0, 540, 615), SectionMeeting(1, 630, 705)])])
    with pytest.raises(ExactRoomingError, match="contact durations"):
        check([state(1, [SectionMeeting(0, 540, 615), SectionMeeting(0, 630, 700)])])


def test_zero_budget_fallback_is_feasible_and_never_claims_proof():
    result = evaluator([demand(1), demand(2), demand(3)], seconds=0)(None)
    assert result["unassigned_count"] == 1
    assert not result["count_proven_optimal"]
    assert len({r for r in result["assignment_by_placement_id"].values() if r}) == 2
    assert result["shortage_lower_bound"] == 0


@pytest.mark.django_db
def test_empty_physical_snapshot_is_feasible_and_repeated_calls_do_not_query(
    django_assert_num_queries,
):
    check = evaluator([])
    with django_assert_num_queries(0):
        result = check([])
        assert result["unassigned_count"] == 0
        assert result["count_proven_optimal"]
        result["assignment_by_placement_id"][999] = "fake"
        assert check([])["assignment_by_placement_id"] == {}


def test_strict_solver_keeps_legacy_shortfall_contract_without_opt_in():
    result = _solve_relaxed([demand(1, rooms=("R1",), cap=45)], {"R1": 40}, time_limit_seconds=1)
    assert result.unroomed == ()
    assert result.shortfall == 5


def test_count_only_returns_proven_count_and_valid_assignment_without_full_objective_proof():
    result = _solve_relaxed(
        [demand(1), demand(2), demand(3)],
        {"R1": 40, "R2": 40},
        time_limit_seconds=1,
        strict_budget=True,
        count_only=True,
    )
    assert len(result.unroomed) == 1
    assert len(set(result.assignment.values())) == 2
    assert result.unroomed_proven_optimal
    assert not result.proven_optimal


def test_recoverable_sections_include_other_matchings_but_exclude_disjoint_and_oversized_demands():
    check = evaluator(
        [
            demand(1, rooms=("R1",)),
            demand(2, rooms=("R1",)),
            demand(3, rooms=("R1",)),
            demand(4, rooms=("R2",)),
            demand(5, rooms=("R1",), cap=45),
        ]
    )
    result = check(None)
    assert set(result["recoverable_section_ids"]) == {"C1_1", "C2_1", "C3_1"}
    assert result["unassigned_count"] == 3
    assert result["count_proven_optimal"]


def test_original_room_audit_counts_filled_but_undersized_and_conflicted_rooms():
    oversized = replace(demand(1, cap=45), incumbent_rooms=("R1",))
    conflicted = replace(demand(2), incumbent_rooms=("R1",))
    missing = demand(3)
    result = evaluator([oversized, conflicted, missing])(None)
    assert result["original_unassigned_count"] == 1
    assert result["original_incompatible_rooms"] == 1
    assert result["original_room_conflict_meetings"] == 2
    assert result["original_room_shortages"] == 3


def test_unknown_source_day_raises_typed_error_for_optional_stage_skip():
    with pytest.raises(ExactRoomingError, match="Unknown physical meeting day"):
        evaluator([replace(demand(1), day="BOGUS")])


def test_freeze_from_remote_constituent_lock_also_protects_physical_times():
    d = demand(1)
    check = RecoveryRoomEvaluator(
        [d],
        {"R1": {"capacity": 40}, "R2": {"capacity": 40}},
        {1: "C1_1"},
        {1: 3},
        frozen_section_ids={"C1_1"},
    )
    with pytest.raises(ExactRoomingError, match="locked section"):
        check([state(1, [SectionMeeting(1, 540, 615)])])


@pytest.mark.django_db
def test_factory_snapshots_metadata_buffer_shared_boards_and_remote_exclusion(
    settings, django_assert_num_queries
):
    from core.models import (
        DeliveryBoard,
        ProgrammeRequirement,
        Room,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSection,
        TimetableScenario,
    )
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator

    settings.TIMETABLE_CAPACITY_BUFFER = 1.1
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="room snapshot"
    )
    boards = [
        DeliveryBoard.objects.create(
            scenario=scenario, program=p, nominal_term=1, label=p, display_order=i
        )
        for i, p in enumerate(("AI", "CS"))
    ]
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI200",
        course_key="AI200",
        department="AI",
        credit_hours=4,
        planned_sections=1,
        max_per_section=40,
        total_demand=10,
        programme_term=1,
    )
    physical = TermSection.objects.create(
        scenario=scenario,
        course_code="AI200",
        course_key="AI200",
        section="M1",
        available_capacity=40,
    )
    remote = TermSection.objects.create(
        scenario=scenario,
        course_code="GS",
        course_key="GS101",
        course_number="101",
        section="M1",
        available_capacity=40,
    )
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    physical_rows = [
        SectionPlacement.objects.create(
            board=b, term_section=physical, day="SUN", start_time="09:00", end_time="10:40"
        )
        for b in boards
    ]
    remote_row = SectionPlacement.objects.create(
        board=boards[0],
        term_section=remote,
        day="SUN",
        start_time="09:00",
        end_time="10:40",
        room="GOOD",
        is_locked=True,
    )
    for code, cap, kind, gender, departments in [
        ("GOOD", 11, "lab", "M", "AI,CS"),
        ("SMALL", 10, "lab", "M", "AI,CS"),
        ("WRONG-TYPE", 100, "lecture", "M", "AI,CS"),
        ("WRONG-GENDER", 100, "lab", "F", "AI,CS"),
        ("WRONG-DEPT", 100, "lab", "M", "AI"),
    ]:
        Room.objects.create(
            room_code=code, capacity=cap, room_type=kind, section=gender, department=departments
        )
    check = build_recovery_room_evaluator(scenario.id, time_limit_seconds=1)
    with django_assert_num_queries(0):
        result = check(None)
        check(None)
    assert result["physical_meetings"] == 1
    assert result["unassigned_count"] == 0
    assert result["assignment_by_placement_id"] == {p.id: "GOOD" for p in physical_rows}
    assert remote_row.id not in result["candidate_placements_by_id"]
    # Construction and evaluation never update the original placements.
    remote_row.refresh_from_db()
    assert remote_row.room == "GOOD"
    assert (
        SectionPlacement.objects.filter(id__in=[p.id for p in physical_rows], room="").count() == 2
    )
