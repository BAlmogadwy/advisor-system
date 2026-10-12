"""A timetable CP-SAT solve takes no more of the host than it can have.

On 2026-10-12 the production instance (0.5 CPU, 512 MB) was killed three times
by the Build's "optimal" candidate: a board model of 575k variables and
constraints that peaked at 800 MB and then ran out of time with nothing placed.
These tests pin the guard: a model is built only up to the element budget, a
model past it is answered like a solve that ran out of time (nothing persisted,
the greedy placement kept), and a solve asks for no more workers than the host
has CPUs.
"""

from __future__ import annotations

import pytest
from django.test import override_settings
from ortools.sat.python import cp_model

from core.models import DeliveryBoard, ScenarioSectionBudget, SectionPlacement, TimetableScenario
from core.services import cpsat_resources
from core.services.cpsat_resources import BudgetedCpModel, ModelTooLarge, cpsat_workers


@pytest.fixture(autouse=True)
def _fresh_host_cpus():
    cpsat_resources.host_cpus.cache_clear()
    yield
    cpsat_resources.host_cpus.cache_clear()


# ── the budgeted model ─────────────────────────────────────────


def test_model_stops_growing_past_its_budget() -> None:
    model = BudgetedCpModel(budget=5)
    a = model.new_bool_var("a")
    b = model.new_bool_var("b")
    x = model.new_int_var(0, 3, "x")
    model.add(a + b <= 1)
    model.add(x >= 1)
    assert model.elements == 5

    with pytest.raises(ModelTooLarge) as caught:
        model.new_bool_var("c")
    assert caught.value.budget == 5
    with pytest.raises(ModelTooLarge):
        model.add(a + b >= 1)


def test_a_budget_of_zero_is_no_limit() -> None:
    model = BudgetedCpModel(budget=0)
    for i in range(50):
        model.add(model.new_bool_var(f"v{i}") <= 1)
    assert model.elements == 100


@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=7)
def test_the_default_budget_is_the_setting() -> None:
    model = BudgetedCpModel()
    for i in range(7):
        model.new_bool_var(f"v{i}")
    with pytest.raises(ModelTooLarge):
        model.new_bool_var("one too many")


def test_a_model_within_budget_solves_like_a_plain_one() -> None:
    model = BudgetedCpModel(budget=100)
    a = model.new_bool_var("a")
    b = model.new_bool_var("b")
    model.add(a + b == 1)
    model.minimize(a)
    solver = cp_model.CpSolver()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert (solver.value(a), solver.value(b)) == (0, 1)


# ── the board solver behind the guard ──────────────────────────


def _one_course_board(name: str) -> DeliveryBoard:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name=name)
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
    return board


@pytest.mark.django_db
def test_a_board_within_budget_still_solves() -> None:
    from core.services.timetable_solver import solve_board

    board = _one_course_board("budget fits")
    result = solve_board(board.id, time_limit_seconds=5)

    assert result["status"] in ("optimal", "feasible")
    assert result["placed"] == 1
    assert "limit" not in result


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=10)
def test_a_board_past_the_budget_is_answered_like_a_timeout() -> None:
    from core.services.timetable_solver import solve_board

    board = _one_course_board("budget exceeded")
    result = solve_board(board.id, time_limit_seconds=5)

    assert result == {
        "status": "timeout",
        "placed": 0,
        "placements": [],
        "objective": 0,
        "limit": "model_size",
    }


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=10)
def test_a_hinted_solve_past_the_budget_reports_no_improvement() -> None:
    from core.services.timetable_solver import solve_board_with_hints

    board = _one_course_board("hinted budget exceeded")
    result = solve_board_with_hints(board.id, [], time_limit_seconds=5)

    assert result["status"] == "timeout"
    assert result["placed"] == 0
    assert result["improved"] is False
    assert result["limit"] == "model_size"


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=10)
def test_solve_and_persist_writes_nothing_when_the_model_is_turned_away() -> None:
    from core.services.timetable_solver import solve_and_persist_board

    board = _one_course_board("persist nothing")
    solve_and_persist_board(board.id, time_limit_seconds=5)

    assert not SectionPlacement.objects.filter(board=board).exists()


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=10)
def test_the_optimal_strategy_falls_back_to_greedy_when_the_model_is_turned_away() -> None:
    from core.services.timetable_autoplace import auto_place_scenario

    board = _one_course_board("optimal falls back")
    result = auto_place_scenario(board.scenario_id, strategy="optimal")

    assert result["total_placed"] == 1
    assert SectionPlacement.objects.filter(board=board).exists()


# ── workers ────────────────────────────────────────────────────


@override_settings(TIMETABLE_CPSAT_WORKERS=3)
def test_the_worker_setting_wins_but_never_exceeds_the_request() -> None:
    assert cpsat_workers() == 3
    assert cpsat_workers(2) == 2


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_workers_follow_a_half_cpu_cgroup_quota(tmp_path, monkeypatch) -> None:
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("50000 100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", cpu_max)

    assert cpsat_resources.host_cpus() == 1
    assert cpsat_workers() == 1


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_workers_follow_a_cgroup_v1_quota(tmp_path, monkeypatch) -> None:
    quota, period = tmp_path / "quota", tmp_path / "period"
    quota.write_text("250000\n")
    period.write_text("100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", tmp_path / "absent")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_QUOTA", quota)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_PERIOD", period)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 64)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)

    assert cpsat_resources.host_cpus() == 3
    assert cpsat_workers() == 3


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_an_unlimited_cgroup_uses_the_cpus_the_process_may_run_on(tmp_path, monkeypatch) -> None:
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("max 100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", cpu_max)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 4)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)

    assert cpsat_resources.host_cpus() == 4
    assert cpsat_workers() == 4
    assert cpsat_workers(2) == 2


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_WORKERS=1)
def test_the_board_solver_asks_for_the_hosts_workers(monkeypatch) -> None:
    from core.services.timetable_solver import solve_board

    seen: list[int] = []
    real_solve = cp_model.CpSolver.solve

    def recording_solve(self, model, *args, **kwargs):
        seen.append(self.parameters.num_workers)
        return real_solve(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", recording_solve)
    board = _one_course_board("one worker")
    solve_board(board.id, time_limit_seconds=5)

    assert seen == [1]


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_WORKERS=1)
def test_the_hinted_board_solver_asks_for_the_hosts_workers(monkeypatch) -> None:
    from core.services.timetable_solver import solve_board_with_hints

    seen: list[int] = []
    real_solve = cp_model.CpSolver.solve

    def recording_solve(self, model, *args, **kwargs):
        seen.append(self.parameters.num_workers)
        return real_solve(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", recording_solve)
    board = _one_course_board("one hinted worker")
    result = solve_board_with_hints(board.id, [], time_limit_seconds=5)

    assert seen == [1]
    assert result["placed"] == 1


# ── the cross-board polisher behind the guard ──────────────────


def _clashing_pair_polish_inputs():
    """Two courses one student needs, placed at the same hour: a polish that
    runs moves one of them."""
    from core.models import ScenarioStudentCourseRequest, ScenarioStudentMap
    from core.services.timetable_assignment_models import (
        RiskTier,
        SectionMeeting,
        SectionState,
        StudentProfile,
    )
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate

    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name="polish")
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
        SectionState(
            section_id=f"{code}_S1",
            course_code=code,
            meetings=[SectionMeeting(day=0, start_min=540, end_min=615)],
            max_capacity=30,
            reserve_capacity=0,
            pattern_family="ONCAMPUS_LEC_75",
            pattern_id="p1",
        )
        for code in ["AI101", "DS201"]
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
    rigidity = {"AI101": 0.5, "DS201": 0.5}
    current_eval = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=rigidity,
    )
    return {
        "scenario_id": scenario.id,
        "current_sections": sections,
        "student_profiles": profiles,
        "course_rigidity": rigidity,
        "current_eval": current_eval,
        "time_limit_seconds": 5,
    }


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_WORKERS=1)
def test_the_polisher_runs_within_budget_on_the_hosts_workers(monkeypatch) -> None:
    from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

    seen: list[int] = []
    real_solve = cp_model.CpSolver.solve

    def recording_solve(self, model, *args, **kwargs):
        seen.append(self.parameters.num_workers)
        return real_solve(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", recording_solve)
    result = polish_scenario_with_cpsat(**_clashing_pair_polish_inputs())

    assert seen == [1]
    assert result is not None


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=3)
def test_the_polisher_is_not_run_past_the_budget(monkeypatch) -> None:
    from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

    def no_solve(self, model, *args, **kwargs):
        raise AssertionError("a model past the budget must never reach the solver")

    monkeypatch.setattr(cp_model.CpSolver, "solve", no_solve)

    assert polish_scenario_with_cpsat(**_clashing_pair_polish_inputs()) is None
