"""Run seed properties independent of Django, solvers and saved timetables."""

import random
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from core.services import timetable_run_seed as seeds
from scheduler.bridge import _search_seeds


@pytest.mark.parametrize("value", [True, False, -1, 1 << 31, 1.0, "42", [], {}])
def test_invalid_seed_is_rejected_without_drawing_randomness(value, monkeypatch):
    def unexpected_draw(*args):
        pytest.fail("Invalid input must not be replaced by a random seed")

    monkeypatch.setattr(seeds.secrets, "randbelow", unexpected_draw)
    with pytest.raises(ValueError, match="seed must be an integer"):
        seeds.resolve_run_seed(value)


@pytest.mark.parametrize("value", [0, 42, seeds.MAX_RUN_SEED])
def test_explicit_seed_is_retained_without_a_new_draw(value, monkeypatch):
    def unexpected_draw(*args):
        pytest.fail("A replay seed must not be replaced")

    monkeypatch.setattr(seeds.secrets, "randbelow", unexpected_draw)
    assert seeds.resolve_run_seed(value) == value


def test_missing_seed_draws_once_per_resolution_including_zero(monkeypatch):
    values = iter([0, 103, seeds.MAX_RUN_SEED])
    bounds = []

    def draw(bound):
        bounds.append(bound)
        return next(values)

    monkeypatch.setattr(seeds.secrets, "randbelow", draw)
    assert [seeds.resolve_run_seed() for _ in range(3)] == [0, 103, seeds.MAX_RUN_SEED]
    assert bounds == [1 << 31] * 3


def test_stage_streams_replay_without_consuming_each_other_or_global_randomness():
    process_random = random.getstate()
    with seeds.seeded_timetable_run(128):
        first = seeds.random_for("greedy:term1:AI")
        expected = [first.random() for _ in range(8)]
        other = seeds.random_for("local-search")
        for _ in range(100):
            other.random()
        replay = seeds.random_for("greedy:term1:AI")
        assert [replay.random() for _ in range(8)] == expected
        assert seeds.seed_for("local-search") != seeds.seed_for("greedy:term1:AI")
    assert random.getstate() == process_random
    assert seeds.current_run_seed() is None
    assert seeds.random_for("greedy:term1:AI") is None
    assert seeds.seed_for("polisher", fallback=42) == 42
    assert seeds.seed_for("rooming", fallback=0) == 0


def test_nested_context_restores_seed_after_success_and_failure():
    assert seeds.current_run_seed() is None
    with seeds.seeded_timetable_run(0):
        assert seeds.current_run_seed() == 0
        with seeds.seeded_timetable_run(12):
            assert seeds.current_run_seed() == 12
        assert seeds.current_run_seed() == 0
        with pytest.raises(RuntimeError), seeds.seeded_timetable_run(99):
            raise RuntimeError("Failed solve")
        assert seeds.current_run_seed() == 0
    assert seeds.current_run_seed() is None


def test_cancellation_signal_also_clears_worker_context():
    class CancelRun(BaseException):
        pass

    with pytest.raises(CancelRun), seeds.seeded_timetable_run(5):
        raise CancelRun
    assert seeds.current_run_seed() is None


def test_simultaneous_worker_threads_keep_independent_seeds():
    together = Barrier(2)

    def worker(seed):
        assert seeds.current_run_seed() is None
        with seeds.seeded_timetable_run(seed):
            before = seeds.seed_for("greedy")
            together.wait(timeout=5)
            assert seeds.current_run_seed() == seed
            assert seeds.seed_for("greedy") == before
        assert seeds.current_run_seed() is None
        return before

    with seeds.seeded_timetable_run(777), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, seed) for seed in (10, 20)]
        assert len({future.result(timeout=10) for future in futures}) == 2
        assert seeds.current_run_seed() == 777
    assert seeds.current_run_seed() is None


def test_scheduler_keeps_legacy_single_and_portfolio_seeds():
    assert _search_seeds(None, 1) == (0,)
    assert _search_seeds(None, 3) == (1, 2, 3)


def test_scheduler_explicit_seed_controls_distinct_repeatable_portfolio():
    assert _search_seeds(0, 1) == (0,)
    assert _search_seeds(123, 1) == (123,)
    selected = _search_seeds(0, 8)
    assert selected == _search_seeds(0, 8)
    assert selected != _search_seeds(1, 8)
    assert len(set(selected)) == 8
    assert all(0 <= value <= seeds.MAX_RUN_SEED for value in selected)


@pytest.mark.parametrize("value", [True, -1, 1 << 31, "42"])
def test_scheduler_rejects_invalid_explicit_seed(value):
    with pytest.raises(ValueError, match="seed must be an integer"):
        _search_seeds(value, 3)


@pytest.mark.django_db
@pytest.mark.parametrize("runs", [1, 3])
def test_scheduler_bridge_forwards_explicit_zero_to_the_selected_solver(monkeypatch, runs):
    """Exercise the real bridge up to its solve boundary, without running a solver."""
    from core.models import TimetableScenario
    from scheduler import bridge

    scenario = TimetableScenario.objects.create(
        name="Seed wiring", academic_year="1448", term="1", gender="M"
    )
    snapshot = object()
    observed = []
    monkeypatch.setattr(bridge, "build_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(bridge, "assign_instructors", lambda value: value)

    class SolverReached(Exception):
        pass

    def capture(value, **kwargs):
        assert value is snapshot
        observed.append(kwargs)
        raise SolverReached

    monkeypatch.setattr(bridge, "plan", capture)
    monkeypatch.setattr(bridge, "plan_portfolio", capture)
    with pytest.raises(SolverReached):
        bridge.build_into_scenario(
            scenario.pk,
            academic_year="1448",
            term=1,
            programs=["AI"],
            gender="M",
            runs=runs,
            seed=0,
        )
    assert len(observed) == 1
    if runs == 1:
        assert observed[0]["seed"] == 0
        assert "seeds" not in observed[0]
    else:
        assert observed[0]["seeds"] == _search_seeds(0, runs)
        assert observed[0]["seeds"] != (1, 2, 3)


def test_scheduler_invalid_seed_is_rejected_before_reading_any_database():
    from scheduler.bridge import build_into_scenario

    # This test deliberately has no django_db marker; touching the ORM fails it.
    with pytest.raises(ValueError, match="seed must be an integer"):
        build_into_scenario(
            -1, academic_year="1448", term=1, programs=["AI"], gender="M", seed=True
        )
