from __future__ import annotations

import pytest
from django.test import override_settings

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat
from core.services.timetable_local_search_v2 import (
    generate_all_repattern_moves,
    generate_swap_moves,
)
from core.services.timetable_optimizer_v2 import build_locked_section_ids_for_scenario
from core.services.timetable_rooming import assign_rooms_to_board


def _section(
    section_id: str, course_code: str, pattern_id: str, day: int, start: int
) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=day, start_min=start, end_min=start + 75)],
        max_capacity=30,
        reserve_capacity=0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id=pattern_id,
    )


def _pattern(pattern_id: str, day: int, start: int) -> CanonicalPattern:
    meeting = SectionMeeting(day=day, start_min=start, end_min=start + 75)
    return CanonicalPattern(
        pattern_id=pattern_id,
        signature=pattern_id,
        meetings=[meeting],
        pattern_family="ONCAMPUS_LEC_75",
        duration_permutation="75",
        is_lab_mixed=False,
        meeting_count=1,
        days_used=frozenset({day}),
        slot_fingerprint=f"{day}:{start}",
    )


def test_local_search_move_generation_skips_locked_sections() -> None:
    sections = {
        "AI101_S1": _section("AI101_S1", "AI101", "p1", 0, 540),
        "DS201_S1": _section("DS201_S1", "DS201", "p2", 1, 540),
    }
    catalog = {
        "ONCAMPUS_LEC_75": [
            _pattern("p1", 0, 540),
            _pattern("p2", 1, 540),
            _pattern("p3", 2, 540),
        ]
    }
    locked = {"AI101_S1"}

    repattern_moves = generate_all_repattern_moves(
        sections,
        catalog,
        locked_section_ids=locked,
    )
    swap_moves = generate_swap_moves(
        sections,
        catalog,
        hotspot_courses=["AI101", "DS201"],
        locked_section_ids=locked,
    )

    assert repattern_moves
    assert all(move.section_id_a not in locked for move in repattern_moves)
    assert all(
        move.section_id_a not in locked and move.section_id_b not in locked for move in swap_moves
    )


@pytest.mark.django_db
def test_locked_section_ids_freeze_whole_section_when_one_meeting_is_locked() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="lock map")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        is_locked=True,
    )

    assert build_locked_section_ids_for_scenario(scenario.id) == {"AI101_S1"}


@pytest.mark.django_db
def test_optimiser_rooming_respects_locked_unassigned_rooms() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="room lock")
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
    )
    Room.objects.create(
        room_code="AI-101",
        capacity=40,
        room_type="lecture",
        department="AI",
        section="M",
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI101",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
    )
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
        is_locked=True,
    )

    result = assign_rooms_to_board(board.id, respect_locked=True)

    placement.refresh_from_db()
    assert placement.room == "UNASSIGNED"
    assert result["assigned"] == 0
    assert result["locked_skipped"] == 1


@pytest.mark.django_db
def test_cpsat_polisher_keeps_locked_sections_out_of_solution() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="cpsat lock")
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=990001,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    for course in ["AI101", "DS201"]:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=990001,
            course_key=course,
            course_code=course,
            primary_term=1,
            status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
            source="test",
        )
    sections = [
        _section("AI101_S1", "AI101", "p1", 0, 540),
        _section("DS201_S1", "DS201", "p1", 0, 540),
    ]
    profiles = {
        "990001": StudentProfile(
            student_id="990001",
            department="AI",
            recommended_courses=["AI101", "DS201"],
            risk_tier=RiskTier.C,
            intra_tier_score=0,
        )
    }
    current_eval = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
    )

    result = polish_scenario_with_cpsat(
        scenario_id=scenario.id,
        current_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
        current_eval=current_eval,
        time_limit_seconds=5,
        locked_section_ids={"AI101_S1"},
    )

    assert result is not None
    improved_ids = {section.section_id for section in result["improved_sections"]}
    assert "AI101_S1" not in improved_ids


@pytest.mark.django_db
def test_cpsat_polisher_excludes_blocked_cells() -> None:
    """The global polisher must relocate sections off a blocked cell, never onto one."""
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="cpsat blocked",
        blocked_slots=[{"day": "SUN", "start": "09:00"}],
    )
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=990002,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    for course in ["AI101", "DS201"]:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=990002,
            course_key=course,
            course_code=course,
            primary_term=1,
            status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
            source="test",
        )
    # Both sections start on the blocked SUN 09:00 cell (day 0, 540) and overlap,
    # so the polisher must move at least one — and can use neither's blocked cell.
    sections = [
        _section("AI101_S1", "AI101", "p1", 0, 540),
        _section("DS201_S1", "DS201", "p1", 0, 540),
    ]
    profiles = {
        "990002": StudentProfile(
            student_id="990002",
            department="AI",
            recommended_courses=["AI101", "DS201"],
            risk_tier=RiskTier.C,
            intra_tier_score=0,
        )
    }
    current_eval = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
    )

    result = polish_scenario_with_cpsat(
        scenario_id=scenario.id,
        current_sections=sections,
        student_profiles=profiles,
        course_rigidity={"AI101": 0.5, "DS201": 0.5},
        current_eval=current_eval,
        time_limit_seconds=5,
        locked_section_ids=set(),
    )

    assert result is not None, "polisher should improve two overlapping sections"
    weekdays = ["SUN", "MON", "TUE", "WED", "THU"]
    for section in result["improved_sections"]:
        for meeting in section.meetings:
            day_str = weekdays[meeting.day]
            start_str = f"{meeting.start_min // 60:02d}:{meeting.start_min % 60:02d}"
            assert (day_str, start_str) != ("SUN", "09:00")


@pytest.mark.django_db
@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_load_balanced_preserves_locked_cross_listed_section() -> None:
    """rebalance must not move or drop a locked section — including a cross-listed
    one where course_key != course_code (the case the row-flag approach fixes)."""
    from core.services.timetable_load_balanced import rebalance_and_persist_board

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="lb lock")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    specs = [
        ("AI101", "AI101", "S1", "SUN", "09:00", "10:15", False),
        ("DS201", "DS201", "S1", "MON", "10:30", "11:45", False),
        ("AIX301", "AI301", "S1", "TUE", "13:00", "14:15", True),  # course_key != course_code
    ]
    locked_pk = None
    for course_key, course_code, sec, day, start, end, is_locked in specs:
        ts = TermSection.objects.create(
            scenario=scenario,
            course_code=course_code,
            course_number=course_code,
            course_key=course_key,
            course_name=course_code,
            section=sec,
            source_tag="tw_auto",
        )
        placement = SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room=f"R-{course_code}",
            is_locked=is_locked,
        )
        if is_locked:
            locked_pk = placement.pk

    rebalance_and_persist_board(board.id, max_seconds=1.0)

    row = SectionPlacement.objects.get(pk=locked_pk)
    assert row.is_locked is True
    assert (row.day, row.start_time, row.end_time, row.room) == ("TUE", "13:00", "14:15", "R-AI301")


@pytest.mark.django_db
@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_old_sa_preserves_locked_section() -> None:
    """Simulated annealing must not relocate or drop a locked section."""
    from core.services.timetable_local_search import optimize_and_persist_board

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="sa lock")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    specs = [
        ("AI101", "AI101", "S1", "SUN", "09:00", "10:15", False),
        ("DS201", "DS201", "S1", "MON", "10:30", "11:45", False),
        ("AIX301", "AI301", "S1", "TUE", "13:00", "14:15", True),
    ]
    locked_pk = None
    for course_key, course_code, sec, day, start, end, is_locked in specs:
        ts = TermSection.objects.create(
            scenario=scenario,
            course_code=course_code,
            course_number=course_code,
            course_key=course_key,
            course_name=course_code,
            section=sec,
            source_tag="tw_auto",
        )
        placement = SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room=f"R-{course_code}",
            is_locked=is_locked,
        )
        if is_locked:
            locked_pk = placement.pk

    optimize_and_persist_board(board.id, max_seconds=1.0)

    row = SectionPlacement.objects.get(pk=locked_pk)
    assert row.is_locked is True
    assert (row.day, row.start_time, row.end_time, row.room) == ("TUE", "13:00", "14:15", "R-AI301")


@pytest.mark.django_db
def test_sa_evaluator_gate_never_persists_regression() -> None:
    """WS-B: SA must never persist a worse student-assignment outcome than its
    greedy baseline. With a real demand graph the gate is active; the invariant
    after_score <= baseline_score holds whether SA improved, was neutral, or
    regressed-and-rolled-back."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_local_search import optimize_and_persist_board
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="sa gate")
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 3", nominal_term=3, program="TS"
    )
    Room.objects.create(room_code="R1", capacity=40, department="TS", room_type="lecture")
    Room.objects.create(room_code="R2", capacity=40, department="TS", room_type="lecture")
    courses = ["TS301", "TS302", "TS303"]
    for code in courses:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=code,
            course_key=code,
            department="TS",
            programme_term=3,
            credit_hours=3,
            planned_sections=1,
            max_per_section=40,
            total_demand=24,
        )
    for i in range(8):
        sid = 990100 + i
        Student.objects.get_or_create(
            student_id=sid,
            defaults={
                "program": "TS",
                "section": "M",
                "name": f"S{i}",
                "total_earned_credits": 60,
                "current_registered_credits": 15,
            },
        )
        ScenarioStudentMap.objects.create(
            scenario=scenario, student_id=sid, primary_term=3, recommended_courses=courses
        )
        for code in courses:
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario,
                student_id=sid,
                course_key=code,
                course_code=code,
                primary_term=3,
                status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                source="test",
            )

    auto_place_scenario(scenario.id, strategy="compact")  # greedy baseline

    profiles = build_student_profiles_for_scenario(scenario.id)
    rigidity = build_course_rigidity_for_scenario(scenario.id)
    assert profiles, "demand graph should yield student profiles so the gate is active"
    baseline = evaluate_generated_timetable_candidate(
        "baseline", build_section_states_for_scenario(scenario.id), profiles, rigidity
    )
    baseline_score = tuple(baseline.lexicographic_score)

    result = optimize_and_persist_board(board.id, max_seconds=2.0)

    assert "sa_evaluator_rolled_back" in result, "evaluator gate should have run"
    after = evaluate_generated_timetable_candidate(
        "after", build_section_states_for_scenario(scenario.id), profiles, rigidity
    )
    assert tuple(after.lexicographic_score) <= baseline_score


@pytest.mark.django_db
def test_publish_blocked_when_placement_on_blocked_slot() -> None:
    """Publish-readiness legality gate: a placement on an institutionally
    blocked slot must block publication."""
    from core.services.timetable_workspace import check_publish_readiness

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="pub blocked",
        blocked_slots=[{"day": "SUN", "start": "09:00"}],
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is False
    assert any("blocked slot" in blocker for blocker in readiness["blockers"])


@pytest.mark.django_db
@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_persist_solver_result_preserves_locked_auto_section() -> None:
    """A locked, auto-placed (tw_auto) row must survive a re-solve untouched.

    Without the lock-aware delete filter, persist_solver_result deletes every
    tw_auto row and get_or_create recreates them with is_locked defaulting to
    False — silently destroying the registrar's lock. With TIMETABLE_ENFORCE_LOCKS
    on, the locked row is excluded from deletion and preserved byte-identical.
    """
    from core.services.timetable_solver import persist_solver_result

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="solver lock persist"
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="tw_auto",
    )
    locked = SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day="TUE",
        start_time="13:00",
        end_time="14:15",
        room="R-LOCK",
        is_locked=True,
    )

    # Re-solve persist with no new placements: the locked row must remain.
    persist_solver_result(board.id, {"status": "optimal", "placements": []})

    survivors = SectionPlacement.objects.filter(pk=locked.pk)
    assert survivors.count() == 1, "locked auto-placed row was deleted on re-solve"
    row = survivors.get()
    assert row.is_locked is True
    assert (row.day, row.start_time, row.room) == ("TUE", "13:00", "R-LOCK")


@pytest.mark.django_db
@override_settings(TIMETABLE_ENFORCE_LOCKS=False)
def test_persist_solver_result_flag_off_parity_deletes_auto_rows() -> None:
    """Flag-off parity: with locks disabled the old behaviour is retained —
    tw_auto rows (even is_locked ones) are cleared, proving the fix is gated."""
    from core.services.timetable_solver import persist_solver_result

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="solver lock paroff"
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="tw_auto",
    )
    locked = SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day="TUE",
        start_time="13:00",
        end_time="14:15",
        room="R-LOCK",
        is_locked=True,
    )

    persist_solver_result(board.id, {"status": "optimal", "placements": []})

    assert not SectionPlacement.objects.filter(pk=locked.pk).exists()


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@pytest.mark.django_db
def test_solver_excludes_blocked_cells() -> None:
    """The per-board CP-SAT solver must never place a meeting on a blocked cell."""
    from core.services.timetable_solver import solve_board

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="solver blocked",
        blocked_slots=[{"day": "SUN", "start": "09:00"}, {"day": "MON", "start": "09:00"}],
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_key="AI101",
        department="AI",
        programme_term=1,
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
    )

    result = solve_board(board.id, time_limit_seconds=5)

    assert result["status"] in ("optimal", "feasible")
    assert result["placed"] == 1
    blocked = {("SUN", "09:00"), ("MON", "09:00")}
    for placement in result["placements"]:
        for meeting in placement["meetings"]:
            assert (meeting["day"], meeting["start"]) not in blocked


@pytest.mark.django_db
@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_solver_same_course_section_avoids_locked_sibling() -> None:
    """A regenerated same-course section must not overlap a locked sibling's slot."""
    from core.services.timetable_solver import solve_board

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="solver lock sib"
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_key="AI101",
        department="AI",
        programme_term=1,
        credit_hours=3,
        planned_sections=2,
        max_per_section=30,
        total_demand=40,
    )
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day="TUE",
        start_time="13:00",
        end_time="14:15",
        room="R1",
        is_locked=True,
    )

    result = solve_board(board.id, time_limit_seconds=5)

    # already=1 (locked S1 counted) → solver regenerates exactly S2.
    assert result["placed"] == 1
    lk_start, lk_end = _mins("13:00"), _mins("14:15")
    for placement in result["placements"]:
        for meeting in placement["meetings"]:
            if meeting["day"] == "TUE":
                ms, me = _mins(meeting["start"]), _mins(meeting["end"])
                assert not (ms < lk_end and me > lk_start), "S2 overlaps the locked sibling slot"
