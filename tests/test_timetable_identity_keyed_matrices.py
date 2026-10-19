"""A display code is not a course identity: matrices, maps and filters keyed by it.

Demand rows are keyed by the planner identity (``CS112::PROGRAMMING_II``), so a
student-overlap matrix built from display codes is silently empty, and a map keyed
by display code lets two courses that share one (CS112 PROGRAMMING I / II, CYB386
internship / machine learning) overwrite or impersonate each other.
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_workspace import _time_mask

pytestmark = pytest.mark.django_db

NETS = "AI201::NEURAL_NETWORKS"
ALGEBRA = "MATH201::LINEAR_ALGEBRA"
PROG_I = "CS112::PROGRAMMING_I"
PROG_II = "CS112::PROGRAMMING_II"


def _scenario() -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448", term="2", name="identity keyed matrices"
    )


def _board(scenario: TimetableScenario) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario, label="Term 4", nominal_term=4, program="AI", display_order=1
    )


def _place(board, key, section, day, start, end, room=""):
    code = key.split("::", 1)[0]
    ts, _ = TermSection.objects.get_or_create(
        scenario=board.scenario,
        course_key=key,
        section=section,
        defaults={"course_code": code, "course_number": code},
    )
    TermSectionMeeting.objects.create(term_section=ts, day=day, start_time=start, end_time=end)
    return SectionPlacement.objects.create(
        board=board, term_section=ts, day=day, start_time=start, end_time=end, room=room
    )


def _need(scenario, key, students) -> None:
    for sid in students:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=sid,
            course_key=key,
            course_code=key.split("::", 1)[0],
            primary_term=4,
        )


# ── Board conflict detection ─────────────────────────────────────────────────


@pytest.mark.parametrize(("shared", "severity"), [(25, "critical"), (5, "warning")])
def test_board_conflicts_count_students_shared_by_named_courses(shared, severity) -> None:
    from core.services.timetable_workspace import detect_board_conflicts

    scenario = _scenario()
    board = _board(scenario)
    _need(scenario, NETS, range(1, shared + 1))
    _need(scenario, ALGEBRA, range(1, shared + 1))
    _place(board, NETS, "S1", "SUN", "08:00", "09:15")
    _place(board, ALGEBRA, "S1", "SUN", "08:00", "09:15")

    conflicts = detect_board_conflicts(board.id)

    assert [(o["shared_students"], o["severity"]) for o in conflicts["overlaps"]] == [
        (shared, severity)
    ]
    assert conflicts["summary"]["critical"] == (1 if severity == "critical" else 0)


@pytest.mark.parametrize("sibling_clears_the_slot", [True, False])
def test_affected_students_route_round_an_overlap_through_a_sibling_section(
    sibling_clears_the_slot,
) -> None:
    from core.services.timetable_workspace import compute_affected_students

    scenario = _scenario()
    board = _board(scenario)
    _need(scenario, NETS, range(1, 26))
    _need(scenario, ALGEBRA, range(1, 26))
    _place(board, NETS, "S1", "SUN", "08:00", "09:15")
    _place(board, ALGEBRA, "S1", "SUN", "08:00", "09:15")
    if sibling_clears_the_slot:
        # Same course, same board, another time: students can take this one.
        _place(board, NETS, "S2", "MON", "08:00", "09:15")

    impact = compute_affected_students(board.id)

    assert impact["affected_count"] == 25
    assert impact["blocked_count"] == (0 if sibling_clears_the_slot else 25)
    assert impact["overlap_details"][0]["resolvable"] is sibling_clears_the_slot


def test_optimiser_gate_trades_cross_course_overlaps_but_blocks_same_course_ones() -> None:
    from core.services.timetable_v2_runner import optimiser_safety_regression
    from core.services.timetable_workspace import compute_scenario_safety_summary

    scenario = _scenario()
    board = _board(scenario)
    _need(scenario, NETS, range(1, 26))
    _need(scenario, ALGEBRA, range(1, 26))
    _place(board, NETS, "S1", "SUN", "08:00", "09:15")
    algebra = _place(board, ALGEBRA, "S1", "MON", "08:00", "09:15")
    before = compute_scenario_safety_summary(scenario.id)

    # A cross-course overlap 25 students' demand spans: shown on the board as
    # critical, but a trade the student-outcome gate judges, not this one.
    SectionPlacement.objects.filter(id=algebra.id).update(day="SUN")
    traded = compute_scenario_safety_summary(scenario.id)
    assert traded["same_board_conflicts"]["overlaps"] == 1
    assert not optimiser_safety_regression(before, traded)["blocked"]

    # Two sections of one course at once is the hard rule the gate protects.
    _place(board, NETS, "S2", "SUN", "08:00", "09:15")
    clashed = compute_scenario_safety_summary(scenario.id)
    assert clashed["same_board_conflicts"]["same_course_overlaps"] == 1
    regression = optimiser_safety_regression(before, clashed)
    assert regression["blocked"]
    assert [(r["metric"], r["before"], r["after"]) for r in regression["regressions"]] == [
        ("same_board_overlaps", 0, 1)
    ]


# ── SA and load-balanced polish ──────────────────────────────────────────────


def _gap_board():
    """Two named courses four students share, 75 minutes apart on Sunday morning,
    and a third course nobody else takes alone on Monday."""
    scenario = _scenario()
    board = _board(scenario)
    _need(scenario, NETS, range(1, 5))
    _need(scenario, ALGEBRA, range(1, 5))
    _need(scenario, PROG_I, range(100, 103))
    _place(board, NETS, "S1", "SUN", "08:00", "09:15")
    _place(board, ALGEBRA, "S1", "SUN", "10:30", "11:45")
    _place(board, PROG_I, "S1", "MON", "08:00", "09:15")
    return board


def test_local_search_weights_gaps_by_students_the_named_courses_share() -> None:
    from core.services.timetable_local_search import optimize_board

    result = optimize_board(_gap_board().id, max_seconds=0)

    # 75-minute gap x min(4 shared, 30) x 0.5; an empty matrix weighs it 10.
    assert result["cost_before"] == 150.0


def test_load_balancer_weights_gaps_by_students_the_named_courses_share() -> None:
    from core.services.timetable_load_balanced import rebalance_board

    result = rebalance_board(_gap_board().id, max_seconds=0)

    # Daily load weighs each meeting by its course's shared students (Sunday
    # 4 + 4, Monday 1): imbalance (8 - 1) x 100, plus the 75-minute gap x
    # min(4 shared, 30). An empty matrix gives (2 - 1) x 100 + 75 x 10.
    assert result["score_before"] == 1000.0


def _sa_section(code, key, label, day="SUN", start="08:00", end="09:15"):
    meeting = {"day": day, "start": start, "end": end, "mask": _time_mask(day, start, end)}
    return {"code": code, "course_key": key, "label": label}, [meeting]


def test_sa_hard_rules_separate_two_courses_that_share_a_display_code() -> None:
    from core.services.timetable_local_search import _check_hard_constraints

    first, first_meetings = _sa_section("CS112", PROG_I, "S2")
    second, second_meetings = _sa_section("CS112", PROG_II, "S1")
    # A non-empty matrix in which the two CS112 courses share no student.
    matrix = {(ALGEBRA, NETS): 3}

    assert _check_hard_constraints({0: first_meetings, 1: second_meetings}, [first, second], matrix)


def test_sa_hard_rules_read_the_matrix_by_planner_identity() -> None:
    from core.services.timetable_local_search import _check_hard_constraints

    first, first_meetings = _sa_section("AI201", NETS, "S1")
    second, second_meetings = _sa_section("MATH201", ALGEBRA, "S1")
    matrix = {(NETS, ALGEBRA) if NETS < ALGEBRA else (ALGEBRA, NETS): 25}

    assert not _check_hard_constraints(
        {0: first_meetings, 1: second_meetings}, [first, second], matrix
    )


# ── Rooming repair ───────────────────────────────────────────────────────────


def _rooms(*capacities):
    return [
        {"room_code": f"R{capacity}", "capacity": capacity, "room_type": "lecture", "section": "M"}
        for capacity in capacities
    ]


def _failure(code="CS112"):
    return {
        "reason": "NO_ROOM_CAPACITY",
        "day": "SUN",
        "start_time": "08:00",
        "end_time": "09:15",
        "course_code": code,
        "section_code": "S1",
    }


def _record(placement, demand, failure, budget_map=None):
    return {
        "ts_id": placement.term_section_id,
        "day": "SUN",
        "start": "08:00",
        "end": "09:15",
        "demand": demand,
        "room_type": "lecture",
        "gender": "M",
        "course_key": placement.term_section.course_key,
        "section_code": "S1",
        "budget_map": budget_map or {},
        "failure": failure,
    }


def test_rooming_repair_prunes_only_the_failure_of_the_meeting_it_rescued() -> None:
    from core.services.timetable_rooming import RoomTracker, repair_unassigned_after_greedy

    board = _board(_scenario())
    tracker = RoomTracker(_rooms(30))
    small = _place(board, PROG_I, "S1", "SUN", "08:00", "09:15", room="UNASSIGNED")
    large = _place(board, PROG_II, "S1", "SUN", "08:00", "09:15", room="UNASSIGNED")
    # Both courses display as CS112 S1, so their failure records look identical.
    small_failure, large_failure = _failure(), _failure()
    room_failures = [small_failure, large_failure]
    records = [_record(small, 20, small_failure), _record(large, 50, large_failure)]

    repaired = repair_unassigned_after_greedy(board, tracker, records, {}, room_failures)

    assert repaired == 1
    small.refresh_from_db()
    assert small.room == "R30"
    assert len(room_failures) == 1 and room_failures[0] is large_failure


def test_rooming_repair_sizes_a_swapped_occupant_by_its_own_course() -> None:
    from core.services.timetable_rooming import (
        RoomTracker,
        _build_rooming_budget_maps,
        repair_unassigned_after_greedy,
    )

    scenario = _scenario()
    board = _board(scenario)
    tracker = RoomTracker(_rooms(40, 60))
    budgets = [
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=key,
            course_code="CS112",
            planned_sections=1,
            total_demand=demand,
        )
        for key, demand in ((PROG_II, 55), (PROG_I, 20))
    ]
    occupant = _place(board, PROG_II, "S1", "SUN", "08:00", "09:15", room="R60")
    TermSection.objects.filter(id=occupant.term_section_id).update(available_capacity=None)
    tracker.mark_used("SUN", "08:00", "09:15", "R60")
    stranded = _place(board, NETS, "S1", "SUN", "08:00", "09:15", room="UNASSIGNED")
    failure = _failure("AI201")
    records = [_record(stranded, 50, failure, _build_rooming_budget_maps(budgets, 1.1))]

    repaired = repair_unassigned_after_greedy(board, tracker, records, {}, [failure])

    # PROGRAMMING II needs 55 seats, so it cannot be moved into the 40-seat room
    # (PROGRAMMING I's 20 would have allowed it).
    assert repaired == 0
    occupant.refresh_from_db()
    assert occupant.room == "R60"


def test_greedy_queues_repairs_with_their_own_failure_and_keyed_demand(monkeypatch) -> None:
    from core.services import timetable_rooming
    from core.services.timetable_autoplace import auto_place_board
    from core.services.timetable_rooming import _budget_value_for_placement

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name="repair queue identity",
        slot_config=[{"start": "08:00", "end": "09:15"}],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="RQ", program="RQ", display_order=1
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=PROG_II,
        course_code="CS112",
        department="RQ",
        credit_hours=1,
        planned_sections=1,
        max_per_section=30,
        total_demand=30,
    )
    # Only a lab exists, so every lecture meeting is left UNASSIGNED and queued.
    Room.objects.create(
        room_code="RQ-LAB", capacity=60, room_type="lab", department="RQ", section="M"
    )
    captured = {}
    original = timetable_rooming.repair_unassigned_after_greedy

    def _capture(**kwargs):
        captured["records"] = list(kwargs["unassigned_records"])
        captured["failures"] = list(kwargs["room_failures"])
        return original(**kwargs)

    monkeypatch.setattr(timetable_rooming, "repair_unassigned_after_greedy", _capture)

    auto_place_board(board.id)

    records = captured["records"]
    assert records
    placement = SectionPlacement.objects.filter(board=board).first()
    for record in records:
        assert any(record["failure"] is failure for failure in captured["failures"])
        assert _budget_value_for_placement(placement, record["budget_map"], "raw", 0) == 30


# ── Generator internship / training exclusion ────────────────────────────────


@pytest.mark.parametrize(
    "catalogue_name", ["MACHINE LEARNING IN CYBERSECURITY", "FIELD EXPERIENCE/INTERNSHIP"]
)
def test_generator_excludes_the_internship_by_each_programmes_own_plan_name(
    monkeypatch, catalogue_name
) -> None:
    from core.services.timetable_generate import generate_workspace_scenario

    # One catalogue row per code: whichever name it holds must not decide for
    # the other programme's course.
    Course.objects.create(course_code="CYB386", description=catalogue_name, credit_hours=3)
    for program, name in (
        ("CYP", "FIELD EXPERIENCE/INTERNSHIP"),
        ("CYP2", "MACHINE LEARNING IN CYBERSECURITY"),
    ):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="CYB386",
            course_name=name,
            programme_term=8,
            credit_hours=3,
            type="Mandatory",
        )
    students = []
    for sid, program in [(996101, "CYP"), (996102, "CYP"), (997101, "CYP2"), (997102, "CYP2")]:
        students.append(sid)
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program=program,
            section="M",
            status="active",
        )
    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["CYB386"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448, 2, ["CYP", "CYP2"], section="M", scenario_name="CYB386 identity", run_autoplace=False
    )

    budgets = ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"])
    assert {(b.course_key, b.total_demand) for b in budgets} == {
        ("CYB386::MACHINE_LEARNING_IN_CYBERSECURITY", 2)
    }
