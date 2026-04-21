"""PR6 fixture loader — thin extension over the PR3 loader.

PR6 fixtures live under
``snapshots/planner-refactor-2026-04-20/fixtures/pr6_*.json``. The JSON
grammar is identical to PR3/PR5 (sections, slot_pool, rooms,
blocked_slots, baseline_placements, locks), so this loader delegates to
``load_pr3_fixture`` under a PR6 filename.

The dedicated loader exists so PR6 tests don't have to import a helper
with "pr3" in its name. Naming discipline keeps grep legible.
"""

from __future__ import annotations

from pr3_fixture_loader import load_pr3_fixture


def load_pr6_fixture(
    fixture_name: str,
    *,
    program: str = "PR6",
    nominal_term: int = 1,
):
    """Materialise a ``pr6_*.json`` fixture.

    Returns ``(scenario, board, raw_fixture_dict)``. Delegates to
    ``load_pr3_fixture`` — the fixture grammar is identical.
    """
    return load_pr3_fixture(
        fixture_name,
        program=program,
        nominal_term=nominal_term,
    )
