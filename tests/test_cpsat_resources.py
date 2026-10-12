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

import inspect
import logging
import re

import pytest
from django.test import override_settings
from ortools.sat.python import cp_model

from core.models import DeliveryBoard, ScenarioSectionBudget, SectionPlacement, TimetableScenario
from core.services import cpsat_resources, timetable_cpsat_polisher, timetable_solver
from core.services.cpsat_resources import BudgetedCpModel, ModelTooLarge, cpsat_workers
from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat
from core.services.timetable_solver import solve_board, solve_board_with_hints


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


def _bools(m, n=3):
    return [m.new_bool_var(f"v{i}") for i in range(n)]


# ── every builder counts, and nothing past the budget is built ─────
_BUILDERS = {
    "new_bool_var": lambda m, v, x: m.new_bool_var("n"),
    "new_int_var": lambda m, v, x: m.new_int_var(0, 5, "n"),
    "add": lambda m, v, x: m.add(v[0] + v[1] <= 1),
    "add_exactly_one": lambda m, v, x: m.add_exactly_one(v),
    "add_at_most_one": lambda m, v, x: m.add_at_most_one(v),
    "add_bool_or": lambda m, v, x: m.add_bool_or(v),
    "add_bool_and": lambda m, v, x: m.add_bool_and(v),
    "add_implication": lambda m, v, x: m.add_implication(v[0], v[1]),
    "add_max_equality": lambda m, v, x: m.add_max_equality(x, [v[0], v[1]]),
    "add_min_equality": lambda m, v, x: m.add_min_equality(x, [v[0], v[1]]),
}


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_every_counted_builder_counts_exactly_one(name) -> None:
    m = BudgetedCpModel(budget=1000)
    v = _bools(m)
    x = m.new_int_var(0, 5, "x")
    before = m.elements
    _BUILDERS[name](m, v, x)
    assert m.elements == before + 1


def test_a_refused_element_is_not_built() -> None:
    m = BudgetedCpModel(budget=2)
    m.new_bool_var("a")
    m.new_bool_var("b")
    with pytest.raises(ModelTooLarge):
        m.new_bool_var("c")
    assert len(m.proto.variables) == 2
    m2 = BudgetedCpModel(budget=1)
    a = m2.new_bool_var("a")
    with pytest.raises(ModelTooLarge):
        m2.add(a <= 1)
    assert len(m2.proto.constraints) == 0


# ── zero means unlimited, whichever way it is spelt ─────────────


@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=3)
def test_an_explicit_zero_budget_ignores_the_setting() -> None:
    m = BudgetedCpModel(budget=0)
    _bools(m, 10)
    assert m.elements == 10


@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=0)
def test_a_zero_setting_is_unlimited_not_the_default(monkeypatch) -> None:
    monkeypatch.setattr(cpsat_resources, "_DEFAULT_MAX_MODEL_ELEMENTS", 3)
    m = BudgetedCpModel()
    _bools(m, 10)
    assert m.elements == 10


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=0)
def test_a_board_solves_with_the_guard_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(cpsat_resources, "_DEFAULT_MAX_MODEL_ELEMENTS", 10)
    board = _one_course_board("guard off")
    result = solve_board(board.id, time_limit_seconds=5)
    assert result["placed"] == 1
    assert "limit" not in result


@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=-1)
def test_a_negative_setting_is_unlimited() -> None:
    m = BudgetedCpModel()
    _bools(m, 10)
    assert m.elements == 10


# ── host CPUs: unlimited, malformed and over-quota cgroups ───────


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_an_unlimited_cgroup_v1_quota_is_not_one_cpu(tmp_path, monkeypatch) -> None:
    quota, period = tmp_path / "q", tmp_path / "p"
    quota.write_text("-1\n")
    period.write_text("100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", tmp_path / "absent")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_QUOTA", quota)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_PERIOD", period)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 6)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)
    assert cpsat_resources.host_cpus() == 6


@pytest.mark.parametrize("content", ["garbage", "50000 0", "50000", "a b", ""])
@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_a_malformed_v2_cpu_max_is_ignored(content, tmp_path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text(content)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", f)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_QUOTA", tmp_path / "absent")
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 5)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)
    assert cpsat_resources.host_cpus() == 5


@pytest.mark.parametrize("q,p", [("junk", "100000"), ("100000", "0"), ("100000", "junk")])
@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_a_malformed_v1_quota_is_ignored(q, p, tmp_path, monkeypatch) -> None:
    qf, pf = tmp_path / "q", tmp_path / "p"
    qf.write_text(q)
    pf.write_text(p)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", tmp_path / "absent")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_QUOTA", qf)
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V1_PERIOD", pf)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 5)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)
    assert cpsat_resources.host_cpus() == 5


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_the_cpus_the_process_may_run_on_beat_cpu_count(tmp_path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("max 100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", f)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(cpsat_resources.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    assert cpsat_resources.host_cpus() == 2


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_a_quota_above_the_allowed_cpus_does_not_raise_them(tmp_path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("800000 100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", f)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 2)
    monkeypatch.delattr(cpsat_resources.os, "sched_getaffinity", raising=False)
    assert cpsat_resources.host_cpus() == 2


@override_settings(TIMETABLE_CPSAT_WORKERS=0)
def test_a_half_cpu_quota_gives_one_worker_on_any_host(tmp_path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("50000 100000\n")
    monkeypatch.setattr(cpsat_resources, "_CGROUP_V2_CPU_MAX", f)
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        cpsat_resources.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False
    )
    assert cpsat_workers() == 1


@override_settings(TIMETABLE_CPSAT_WORKERS=3)
def test_the_worker_setting_beats_the_hosts_cpus(monkeypatch) -> None:
    monkeypatch.setattr(cpsat_resources.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(cpsat_resources.os, "sched_getaffinity", lambda pid: {0}, raising=False)
    assert cpsat_resources.host_cpus() == 1
    assert cpsat_workers() == 3


# ── call sites ask for the setting, not a constant ──────────────


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_WORKERS=3)
def test_call_sites_use_the_configured_workers(monkeypatch) -> None:
    seen = []
    real = cp_model.CpSolver.solve

    def rec(self, model, *a, **k):
        seen.append(self.parameters.num_workers)
        return real(self, model, *a, **k)

    monkeypatch.setattr(cp_model.CpSolver, "solve", rec)
    board = _one_course_board("three workers")
    solve_board(board.id, time_limit_seconds=5)
    solve_board_with_hints(board.id, [], time_limit_seconds=5)
    polish_scenario_with_cpsat(**_clashing_pair_polish_inputs())
    assert seen == [3, 3, 3]


# ── the guard sees every builder the solver modules use ─────────


@pytest.mark.parametrize("module", [timetable_solver, timetable_cpsat_polisher])
def test_every_model_builder_the_solvers_call_is_counted(module) -> None:
    src = inspect.getsource(module)
    used = set(re.findall(r"\bmodel\.((?:add|new)\w*)\(", src))
    uncounted = {n for n in used if n not in BudgetedCpModel.__dict__ and n != "add_hint"}
    assert not uncounted, f"builders that bypass the model budget: {sorted(uncounted)}"


# ── a turned-away solve is logged ───────────────────────────────


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=10)
def test_a_turned_away_solve_is_logged(caplog) -> None:
    board = _one_course_board("log")
    with caplog.at_level(logging.WARNING):
        solve_board(board.id, time_limit_seconds=5)
    assert f"board {board.id}" in caplog.text and "grew past 10" in caplog.text


@pytest.mark.django_db
@override_settings(TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=3)
def test_a_skipped_polish_is_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        polish_scenario_with_cpsat(**_clashing_pair_polish_inputs())
    assert "polisher not run" in caplog.text
