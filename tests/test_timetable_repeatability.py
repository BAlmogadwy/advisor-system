"""A run is a function of (stored board, flags, seed): searches stop on work, not on the clock.

A search that stops on the wall clock ends on a different board each time the machine runs a
little faster or slower, whatever its seed. These tests pin that the searches stop on a count of
evaluated candidates instead, that the clock only acts as a ceiling, and that a ceiling that did
end a search is reported rather than silent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import timetable_local_search_v2 as local_search
from core.services import timetable_work_budget as wb
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_work_budget import WorkBudget, capture_search_stops
from tests.test_bench_timetable import (  # noqa: F401
    TUNING,
    _fixture_board,
    _search_that_moves_x,
    flags,
)
from tests.test_timetable_search_regressions import _pattern, _profile, _section


class _Clock:
    """A clock that moves ``step`` seconds every time it is read: a machine of a given speed."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


# ── the budget itself ──────────────────────────────────────────────────────


def test_a_the_work_limit_ends_a_search_whatever_the_clock_says() -> None:
    for step in (0.0, 0.001, 0.4):
        budget = WorkBudget("search", max_work=5, ceiling_seconds=1000, clock=_Clock(step))
        spent = 0
        while not budget.exhausted():
            budget.spend()
            spent += 1
        assert spent == 5
        assert budget.reason == wb.WORK_LIMIT


def test_b_the_ceiling_ends_a_search_that_has_no_work_limit_and_says_so() -> None:
    budget = WorkBudget("search", max_work=None, ceiling_seconds=1.0, clock=_Clock(0.25))
    spent = 0
    while not budget.exhausted():
        budget.spend()
        spent += 1
    assert budget.reason == wb.WALL_CEILING
    assert 0 < spent < 10


def test_c_work_wins_when_both_limits_are_passed_together() -> None:
    clock = _Clock(10.0)  # long past the ceiling at the first look
    budget = WorkBudget("search", max_work=3, ceiling_seconds=1.0, clock=clock)
    budget.work = 3
    assert budget.exhausted()
    assert budget.reason == wb.WORK_LIMIT


def test_d_a_search_that_never_hit_a_limit_completed_and_a_zero_cap_is_no_cap() -> None:
    with capture_search_stops() as stops:
        budget = WorkBudget("search", max_work=0, ceiling_seconds=None, clock=_Clock(0.0))
        budget.spend(7)
        assert not budget.exhausted()
        budget.finish()
    assert [(s["stage"], s["reason"], s["work"]) for s in stops] == [("search", "completed", 7)]


def test_e_stops_are_collected_per_run_and_repeats_of_one_stage_are_one_row() -> None:
    wb.record_search_stop(
        "x", wb.COMPLETED, work=1, work_limit=None, seconds=0, ceiling_seconds=None
    )
    with capture_search_stops() as stops:
        for _ in range(3):
            wb.record_search_stop(
                "room_solve", wb.COMPLETED, work=1, work_limit=None, seconds=0.1, ceiling_seconds=1
            )
        wb.record_search_stop(
            "room_solve", wb.WALL_CEILING, work=1, work_limit=None, seconds=1, ceiling_seconds=1
        )
    assert [(s["reason"], s["count"]) for s in stops] == [("completed", 3), ("wall_ceiling", 1)]
    report = wb.repeatability_report(stops)
    assert report["repeatable"] is False
    assert report["ceilings_hit"] == ["room_solve"]
    assert wb.repeatability_report([stops[0]])["repeatable"] is True
    with capture_search_stops() as inner:
        pass
    assert inner == []  # nothing leaks between runs


# ── the local search ───────────────────────────────────────────────────────


def _board():
    """Six course pairs, each needing ONE move (B from the late slot to the middle one) to close
    a gap: the best-improvement search fixes one pair per iteration, so a short work budget ends
    it part-way and where it ends is visible in the sections."""
    early, middle, late = (
        _pattern(n, s) for n, s in (("early", 540), ("middle", 630), ("late", 720))
    )
    sections, profiles = {}, {}
    for i in range(6):
        sections[f"A{i}"] = _section(f"A{i}", early)
        sections[f"B{i}"] = _section(f"B{i}", late)
        profiles[f"s{i}"] = _profile(f"s{i}", [f"A{i}", f"B{i}"])
    catalog = {"normal": [early, middle, late]}
    locked = {f"A{i}" for i in range(6)}
    return sections, catalog, profiles, locked


# One evaluation costs (number of sections) squared: the board below has twelve sections.
EVALUATION_COST = 12**2


def _search(settings, *, cap: int, ceiling: float, clock: float | None = None, monkeypatch=None):
    settings.TIMETABLE_LOCAL_SEARCH_MAX_WORK = cap * EVALUATION_COST
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = ceiling
    if clock is not None:
        monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(clock)))
    sections, catalog, profiles, locked = _board()
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}
    )
    with capture_search_stops() as stops:
        result = local_search.diagnostic_driven_local_search(
            baseline, sections, catalog, profiles, {}, locked_section_ids=locked, max_iterations=50
        )
    layout = tuple(sorted((sid, s.pattern_id) for sid, s in sections.items()))
    return layout, tuple(result.lexicographic_score), stops


def test_f_the_search_ends_on_the_same_board_on_a_fast_and_a_slow_machine(settings, monkeypatch):
    fast = _search(settings, cap=25, ceiling=1000, clock=0.0001, monkeypatch=monkeypatch)
    slow = _search(settings, cap=25, ceiling=1000, clock=0.05, monkeypatch=monkeypatch)
    assert fast[:2] == slow[:2]  # same layout, same score; only the seconds spent differ
    stop = fast[2][0]
    assert (stop["stage"], stop["reason"], stop["work"]) == (
        "search",
        wb.WORK_LIMIT,
        25 * EVALUATION_COST,
    )


def test_g_the_work_limit_really_binds_the_search_ends_before_it_converges(settings):
    capped = _search(settings, cap=25, ceiling=1000)[0]
    full, _, stops = _search(settings, cap=0, ceiling=1000)
    assert capped != full  # 25 evaluations are not enough to fix all six pairs
    assert stops[0]["reason"] == wb.COMPLETED


def test_h_without_a_work_limit_the_clock_decides_and_the_run_says_so(settings, monkeypatch):
    fast = _search(settings, cap=0, ceiling=3.0, clock=0.0001, monkeypatch=monkeypatch)
    slow = _search(settings, cap=0, ceiling=3.0, clock=0.5, monkeypatch=monkeypatch)
    assert fast[0] != slow[0]  # the control: this is what a wall-clock stop does
    assert fast[2][0]["reason"] == wb.COMPLETED
    assert slow[2][0]["reason"] == wb.WALL_CEILING
    assert wb.repeatability_report(slow[2])["repeatable"] is False
    assert wb.repeatability_report(fast[2])["repeatable"] is True


def test_i_a_slow_machine_that_reaches_the_ceiling_first_is_reported_not_silent(
    settings, monkeypatch
):
    layout, _, stops = _search(settings, cap=25, ceiling=1.0, clock=0.5, monkeypatch=monkeypatch)
    assert stops[0]["reason"] == wb.WALL_CEILING
    assert stops[0]["work"] < 25 * EVALUATION_COST


# ── secondary recovery ─────────────────────────────────────────────────────


def _recover(settings, *, cap: int, clock: float, monkeypatch):
    from tests.test_timetable_secondary_recovery import pattern, room_result, run, section

    settings.TIMETABLE_SECONDARY_RECOVERY_MAX_WORK = cap
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(clock)))
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 780)}
    with capture_search_stops() as stops:
        result = run(
            sections,
            [pattern(540), pattern(600), pattern(660), pattern(720), pattern(780)],
            lambda _states: room_result(0),
            teachers={"core": frozenset({7})},
        )
    report = result["report"]
    return report["accepted_moves"], report["stop_reason"], stops


def test_j_recovery_stops_on_work_and_is_the_same_on_a_fast_and_a_slow_machine(
    settings, monkeypatch
):
    fast = _recover(settings, cap=2, clock=0.0001, monkeypatch=monkeypatch)
    slow = _recover(settings, cap=2, clock=0.05, monkeypatch=monkeypatch)
    assert fast[:2] == slow[:2]
    assert fast[1] == "evaluation_limit"
    stop = next(s for s in fast[2] if s["stage"] == "recovery")
    assert stop["reason"] == wb.WORK_LIMIT and stop["work"] >= 2  # evaluations + room solves


def test_k_a_recovery_the_ceiling_ended_says_time_limit_and_is_reported(settings, monkeypatch):
    _moves, reason, stops = _recover(settings, cap=500, clock=6.0, monkeypatch=monkeypatch)
    assert reason == "time_limit"
    assert next(s for s in stops if s["stage"] == "recovery")["reason"] == wb.WALL_CEILING
    assert wb.repeatability_report(stops)["repeatable"] is False


# ── the room solver ────────────────────────────────────────────────────────


def test_l_a_bounded_room_solve_carries_a_deterministic_limit_and_a_wider_wall_ceiling(
    settings, monkeypatch
):
    from ortools.sat.python import cp_model

    from core.services.timetable_exact_rooming import _solve_relaxed
    from tests.test_cpsat_resources import _ROOM_CAPS, _SAME_SLOT

    settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 0
    settings.TIMETABLE_ROOM_SOLVE_DETERMINISTIC_PER_SECOND = 0.07
    seen = []
    real = cp_model.CpSolver.solve

    def spy(self, model, *args, **kwargs):
        seen.append((self.parameters.max_deterministic_time, self.parameters.max_time_in_seconds))
        return real(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", spy)
    with capture_search_stops() as stops:
        _solve_relaxed(_SAME_SLOT, _ROOM_CAPS, time_limit_seconds=0.5, strict_budget=True)
    assert seen[0][0] == pytest.approx(0.035)  # 0.07 per second x the nominal 0.5 s
    assert all(0 < d <= 0.035 for d, _wall in seen)  # what is left of it, phase after phase
    assert all(0.5 < wall <= 1.0 for _d, wall in seen)  # the ceiling is twice the nominal limit
    assert {s["stage"] for s in stops} == {"room_solve"}


def test_m_a_legacy_room_solve_is_left_as_it_was(settings, monkeypatch):
    from ortools.sat.python import cp_model

    from core.services.timetable_exact_rooming import _solve_relaxed
    from tests.test_cpsat_resources import _ROOM_CAPS, _SAME_SLOT

    settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 0
    settings.TIMETABLE_ROOM_SOLVE_DETERMINISTIC_PER_SECOND = 0.07
    seen = []
    real = cp_model.CpSolver.solve

    def spy(self, model, *args, **kwargs):
        seen.append(self.parameters.max_deterministic_time)
        return real(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", spy)
    _solve_relaxed(_SAME_SLOT, _ROOM_CAPS, time_limit_seconds=5)  # not strict_budget
    assert seen and all(d == float("inf") or d > 1e100 for d in seen)  # no deterministic limit


# ── the whole run ──────────────────────────────────────────────────────────


@pytest.mark.django_db
@pytest.mark.usefixtures("flags")
def test_n_two_runs_with_the_same_seed_end_on_the_same_board_and_say_how_each_search_ended(
    monkeypatch,
):
    from core.services import timetable_benchmark as bench

    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)
    rows = [bench.run_once(sc.id, "optimise_current", seed=7, tuning=TUNING) for _ in range(2)]
    assert [r["error"] for r in rows] == [None, None]
    assert rows[0]["board_digest"] == rows[1]["board_digest"]
    assert [t["digest"] for t in rows[0]["stage_trace"]] == [
        t["digest"] for t in rows[1]["stage_trace"]
    ]
    assert rows[0]["repeatable"] is True
    assert {s["stage"] for s in rows[0]["search_stops"]} >= {"search"}
    assert all(s["reason"] != wb.WALL_CEILING for s in rows[0]["search_stops"])


@pytest.mark.django_db
@pytest.mark.usefixtures("flags")
def test_o_a_run_whose_search_was_ended_by_the_ceiling_is_not_called_repeatable(
    monkeypatch, settings
):
    from core.services import timetable_benchmark as bench

    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)
    settings.TIMETABLE_LOCAL_SEARCH_MAX_WORK = 0
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 1.0
    monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(2.0)))  # a very slow machine
    row = bench.run_once(sc.id, "optimise_current", seed=7, tuning=TUNING)
    assert row["repeatable"] is False
    assert any(
        s["stage"] == "search" and s["reason"] == wb.WALL_CEILING for s in row["search_stops"]
    )


# ── annealing, the load balancer, the chain search, CP-SAT ─────────────────


def _placed_board():
    from core.models import DeliveryBoard
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run
    from tests.test_timetable_seeded_classic import _symmetric_scenario

    scenario = _symmetric_scenario()
    with seeded_timetable_run(3):
        auto_place_scenario(scenario.pk, strategy="compact")
    return DeliveryBoard.objects.get(scenario=scenario)


def _annealing_runs(settings, monkeypatch, run):
    settings.TIMETABLE_SA_MAX_ITERATIONS = 40
    out = []
    for step in (0.0001, 0.02):
        monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(step)))
        with capture_search_stops() as stops:
            result = run()
        out.append(
            (result["iterations"], result.get("cost_after", result.get("score_after")), stops)
        )
    return out


@pytest.mark.django_db
def test_p_annealing_stops_on_iterations_whatever_the_machine_speed(settings, monkeypatch):
    from core.services.timetable_local_search import optimize_board

    board = _placed_board()
    fast, slow = _annealing_runs(
        settings, monkeypatch, lambda: optimize_board(board.id, max_seconds=5)
    )
    assert fast[:2] == slow[:2] and fast[0] == 40
    assert [(s["stage"], s["reason"], s["work"]) for s in fast[2]] == [("sa", wb.WORK_LIMIT, 40)]


@pytest.mark.django_db
def test_q_the_load_balancer_stops_on_iterations_whatever_the_machine_speed(settings, monkeypatch):
    from core.services.timetable_load_balanced import rebalance_board

    board = _placed_board()
    fast, slow = _annealing_runs(
        settings, monkeypatch, lambda: rebalance_board(board.id, max_seconds=5)
    )
    assert fast[:2] == slow[:2] and fast[0] == 40
    assert [(s["stage"], s["reason"], s["work"]) for s in fast[2]] == [
        ("load_balance", wb.WORK_LIMIT, 40)
    ]


@pytest.mark.django_db
def test_r_annealing_with_no_iteration_cap_is_ended_by_the_clock_and_says_so(settings, monkeypatch):
    from core.services.timetable_local_search import optimize_board

    board = _placed_board()
    settings.TIMETABLE_SA_MAX_ITERATIONS = 0
    monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(0.5)))
    with capture_search_stops() as stops:
        optimize_board(board.id, max_seconds=2.0)
    assert [(s["stage"], s["reason"]) for s in stops] == [("sa", wb.WALL_CEILING)]


def test_s_a_chain_search_ended_by_its_deadline_is_reported(settings, monkeypatch):
    from core.services import timetable_local_search_chains as chains
    from tests.test_timetable_search_regressions import _instructor_chain_board

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    sections, catalog, instructors = _instructor_chain_board()
    profiles = {"a": _profile("a", ["A"]), "b": _profile("b", ["B"])}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}, section_instructor_ids=instructors
    )
    ended = {}
    for name, step in (("fast", 0.0), ("slow", 5.0)):
        monkeypatch.setattr(chains, "time", SimpleNamespace(perf_counter=_Clock(step)))
        with capture_search_stops() as stops:
            chains.chain_local_search(
                baseline,
                dict(sections),
                catalog,
                profiles,
                {},
                section_instructor_ids=instructors,
                max_iterations=3,
                chain_time_limit_seconds=2.0,
            )
        ended[name] = [(s["stage"], s["reason"]) for s in stops]
    assert ended["fast"] == [("chain", wb.COMPLETED)]
    assert ended["slow"] == [("chain", wb.WALL_CEILING)]


def test_t_a_cpsat_solve_gets_a_deterministic_limit_only_when_one_is_configured(settings):
    from ortools.sat.python import cp_model

    settings.TIMETABLE_CPSAT_DETERMINISTIC_PER_SECOND = 0.5
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert wb.deterministic_limit(solver, 8.0) == 4.0
    assert solver.parameters.max_deterministic_time == 4.0
    several = cp_model.CpSolver()
    several.parameters.num_workers = 8  # not repeatable whatever the limit: no limit is set
    settings.TIMETABLE_CPSAT_DETERMINISTIC_PER_SECOND = 0.5
    assert wb.deterministic_limit(several, 8.0) is None
    assert several.parameters.max_deterministic_time > 1e100
    settings.TIMETABLE_CPSAT_DETERMINISTIC_PER_SECOND = 0
    plain = cp_model.CpSolver()
    plain.parameters.num_workers = 1
    assert wb.deterministic_limit(plain, 8.0) is None
    assert plain.parameters.max_deterministic_time > 1e100


def test_u_a_cpsat_solve_that_stopped_short_of_proof_is_classified_by_what_ended_it():
    from ortools.sat.python import cp_model

    class _Solver:
        def __init__(self, det):
            self.deterministic_time = det

    assert wb.cpsat_stop_reason(cp_model.OPTIMAL, _Solver(9.0), 1.0) == wb.COMPLETED
    assert wb.cpsat_stop_reason(cp_model.INFEASIBLE, _Solver(9.0), 1.0) == wb.COMPLETED
    assert wb.cpsat_stop_reason(cp_model.FEASIBLE, _Solver(1.0), 1.0) == wb.WORK_LIMIT
    assert wb.cpsat_stop_reason(cp_model.UNKNOWN, _Solver(0.2), 1.0) == wb.WALL_CEILING
    assert wb.cpsat_stop_reason(cp_model.FEASIBLE, _Solver(0.2), None) == wb.WALL_CEILING


def test_v_the_recovery_calls_own_evaluation_cap_is_a_work_limit_not_a_completion(
    settings, monkeypatch
):
    from tests.test_timetable_secondary_recovery import pattern, room_result, run, section

    settings.TIMETABLE_SECONDARY_RECOVERY_MAX_WORK = 0
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    monkeypatch.setattr(wb, "time", SimpleNamespace(monotonic=_Clock(0.0001)))
    sections = {"core": section("core", "CORE", 540), "soft": section("soft", "SOFT", 780)}
    with capture_search_stops() as stops:
        result = run(
            sections,
            [pattern(540), pattern(600), pattern(660), pattern(720), pattern(780)],
            lambda _states: room_result(0),
            teachers={"core": frozenset({7})},
            max_evaluations=1,
        )
    assert result["report"]["stop_reason"] == "evaluation_limit"
    assert [s["reason"] for s in stops if s["stage"] == "recovery"] == [wb.WORK_LIMIT]
