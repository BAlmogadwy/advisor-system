"""The V2 flow's "optimal" placement solve is not built past a size ceiling.

Measured on 2026-11-23 (saved scenarios 662-669, no model-size cap, one worker): every one of
the ``solve_board`` calls of a Generate's "optimal" candidate and re-placement answered UNKNOWN with nothing
placed (the board then falls back to the compact placement, so the candidate is the compact
board). A 1.5 s limit is no limit for these models: they hold 63,000-100,000 variables and
constraints, building one takes 1.5-16 s of Python, and the solver then spends the whole limit
in presolve with 0 conflicts, 0 branches, 0 propagations. Given 30 s it finds a feasible board
(objective 6036, bound 3000-4000, no proof after 90 s on eight workers); given 1.5-8 s it finds
nothing. Production already refuses these models (``TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS=40000``)
and its Generate boards equal the uncapped runs' boards, digest for digest; a workstation has
no cap and paid 20-70 s of every Generate for the same answer.

So the V2 flow (its candidate sweep and the re-placement of its winner) passes the production
ceiling to the "optimal" strategy's solves; a standalone run is unchanged. A model past it is not built,
exactly as production's cap does it: the answer is "timeout", nothing placed.
"""

from __future__ import annotations

import pytest

from tests.meeting_pattern_support import (
    FLAGS,
    TWIN_KEY,
    X_KEY,
    board_of,
    build_scenario,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def flags(settings):
    for name, value in FLAGS.items():
        setattr(settings, name, value)
    return settings


def test_the_ceiling_is_the_production_cap() -> None:
    from core.services.timetable_benchmark import production_flags
    from core.services.timetable_solver import OPTIMAL_PLACEMENT_MAX_MODEL_ELEMENTS

    assert (
        production_flags()["TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS"]
        == OPTIMAL_PLACEMENT_MAX_MODEL_ELEMENTS
    )


@pytest.mark.parametrize(
    ("configured", "extra", "expected"),
    [
        (0, None, 0),  # nothing configured, nothing asked: no limit, as before
        (0, 0, 0),
        (0, 40_000, 40_000),  # a workstation, a sweep: the sweep's ceiling
        (100_000, 40_000, 40_000),  # the tighter of the two wins ...
        (30_000, 40_000, 30_000),  # ... whichever it is
        (30_000, None, 30_000),  # no ceiling asked: the configured one, as before
    ],
)
def test_the_tighter_of_the_configured_cap_and_the_ceiling_applies(
    settings, configured, extra, expected
) -> None:
    from core.services.cpsat_resources import tighter_model_limit

    settings.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = configured
    assert tighter_model_limit(extra) == expected


def test_solve_board_refuses_a_model_past_its_ceiling_and_solves_it_without(flags) -> None:
    from core.services.timetable_solver import solve_board

    scenario = build_scenario(planned={X_KEY: 1, TWIN_KEY: 1}, small_grid=True)
    board = board_of(scenario)

    refused = solve_board(board.id, time_limit_seconds=3, max_model_elements=50)
    assert refused["status"] == "timeout"
    assert refused["placed"] == 0
    assert refused["limit"] == "model_size"

    solved = solve_board(board.id, time_limit_seconds=3)
    assert solved["status"] in ("optimal", "feasible")
    assert solved["placed"] > 0


@pytest.mark.parametrize(
    ("kwargs", "ceiling"),
    [
        ({"cap_placement_model": True}, True),
        ({"cap_placement_model": True, "candidate_gen_budget": 1.5}, True),
        ({}, False),  # a standalone "optimal" run: unchanged, whatever its budget
        ({"candidate_gen_budget": 1.5}, False),
    ],
)
def test_only_a_run_that_asks_gives_its_optimal_solve_the_ceiling(
    flags, monkeypatch, kwargs, ceiling
) -> None:
    """A small board over 40,000 elements still solves on a workstation (an availability
    test relies on it), so the ceiling is for the V2 flow, which asks for it."""
    from core.services import timetable_solver
    from core.services.timetable_autoplace import auto_place_scenario

    seen: list[dict] = []

    def stub(scenario_id, time_limit_seconds=5.0, **kw):
        seen.append(kw)
        return {"boards": {}, "total_placed": 0}

    monkeypatch.setattr(timetable_solver, "solve_scenario", stub)
    scenario = build_scenario(planned={X_KEY: 1, TWIN_KEY: 1}, small_grid=True)
    auto_place_scenario(scenario.id, strategy="optimal", **kwargs)

    want = timetable_solver.OPTIMAL_PLACEMENT_MAX_MODEL_ELEMENTS if ceiling else None
    assert seen == [{"max_model_elements": want}]


def test_the_v2_sweep_asks_for_the_ceiling(flags, monkeypatch) -> None:
    from core.services import timetable_autoplace
    from core.services.timetable_optimizer_v2 import _generate_candidates_for_scenario

    calls: list[dict] = []

    def spy(scenario_id, **kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(timetable_autoplace, "auto_place_scenario", spy)
    scenario = build_scenario(planned={X_KEY: 1, TWIN_KEY: 1}, small_grid=True)
    _generate_candidates_for_scenario(scenario.id, strategies=["optimal"])

    assert [c.get("cap_placement_model") for c in calls] == [True]


def test_the_v2_winner_re_placement_asks_for_the_ceiling() -> None:
    """The re-placement of the winning strategy sits inside the long optimiser function, so
    this reads its source: both V2 calls of ``auto_place_scenario`` pass the flag. Without
    it an "optimal" winner is re-placed at the full 5 s budget (6-11 s a board, UNKNOWN)."""
    import inspect

    from core.services import timetable_optimizer_v2

    source = inspect.getsource(timetable_optimizer_v2)
    assert source.count("cap_placement_model=True") == 2
