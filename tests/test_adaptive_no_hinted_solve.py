"""The adaptive placer does not run the hinted CP-SAT placement solve.

Measured on 2026-11-23 (six saved scenarios, Generate and Optimise Current, production
settings): the "Phase 2" solve of ``_adaptive_scenario`` was called five times a Generate,
returned "optimal" with nothing placed every time, and was never kept. The reason is in its
model: it places only the sections still free on the board, and the greedy placer has
already persisted every section it could place, so

- with nothing left to place the model is empty (a no-op);
- with sections left (the ones the greedy could not place) the model must place ALL of them
  (``add_exactly_one``), the solver cannot finish inside 3-8 s and answers UNKNOWN (30 s on
  a crowded board did not change that), burning its whole budget;
- the warm-start hints name the greedy's placements, which are not in the model at all;
- the keep test compares the sections the solve placed with ALL the sections the greedy
  placed, and a kept result would have had ``persist_solver_result`` clear every auto-placed
  section of the board and write back only the solve's (the greedy's placements replaced by
  the leftovers); it was never kept in 60 measured solves.

So the placer no longer calls it. ``solve_board_with_hints`` itself is untouched.
"""

from __future__ import annotations

import pytest

from core.models import SectionPlacement
from tests.meeting_pattern_support import (
    FLAGS,
    build_scenario,
    planned_sections,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def flags(settings):
    for name, value in FLAGS.items():
        setattr(settings, name, value)
    return settings


@pytest.fixture
def hinted_calls(monkeypatch) -> list[tuple]:
    """Every call to the hinted solve, counted; a call answers as the real one did."""
    from core.services import timetable_solver

    calls: list[tuple] = []

    def stub(board_id, greedy_placements, time_limit_seconds=8.0):
        calls.append((board_id, len(greedy_placements), time_limit_seconds))
        return {"status": "timeout", "placed": 0, "placements": [], "objective": 0}

    monkeypatch.setattr(timetable_solver, "solve_board_with_hints", stub)
    return calls


def _placed_sections(scenario) -> int:
    return (
        SectionPlacement.objects.filter(board__scenario=scenario)
        .values("term_section_id")
        .distinct()
        .count()
    )


def test_adaptive_never_runs_the_hinted_solve_when_everything_is_placed(
    flags, hinted_calls
) -> None:
    from core.services.timetable_autoplace import _adaptive_scenario

    scenario = build_scenario()
    result = _adaptive_scenario(scenario.id, candidate_gen_budget=1.5)

    assert hinted_calls == []
    phase = result["adaptive_phases"]["Term 1"]
    assert phase["greedy"]["skipped"] == 0
    assert phase["cpsat"]["status"] == "skipped"
    assert _placed_sections(scenario) == sum(planned_sections(scenario).values())


def test_adaptive_never_runs_the_hinted_solve_when_sections_are_left_unplaced(
    flags, hinted_calls
) -> None:
    """The case that used to burn the budget: nine sections a course on the small grid,
    the greedy leaves sections it cannot place, and none is handed to a solver."""
    from core.services.timetable_autoplace import _adaptive_scenario

    scenario = build_scenario(
        small_grid=True, planned={key: 9 for key in planned_sections(build_scenario(seed=5))}
    )
    result = _adaptive_scenario(scenario.id, candidate_gen_budget=1.5)

    phase = result["adaptive_phases"]["Term 1"]
    assert phase["greedy"]["skipped"] > 0, "the board must leave sections unplaced"
    assert hinted_calls == []
    assert phase["cpsat"]["status"] == "skipped"
    # What the greedy placed is still on the board.
    assert _placed_sections(scenario) == phase["greedy"]["placed"]
