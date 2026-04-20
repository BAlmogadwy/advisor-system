"""PR5 fixture loader — thin extension over the PR3 loader.

PR5 scenario fixtures live under
``snapshots/planner-refactor-2026-04-20/fixtures/pr5_*.json`` and describe
scenarios engineered to exercise one specific post-greedy stage (SA,
CP-SAT, chain, rooming-repair). The JSON shape is identical to PR3's
(sections, slot_pool, rooms, baseline_placements, locks) so we reuse
``load_pr3_fixture`` under a PR5 filename.

A dedicated loader exists so PR5 tests don't have to import a PR3 helper
with "pr3" in its name — naming discipline that keeps search/grep and
future refactors legible. See PR3 loader H1/H2 discussion for the
equivalent reasoning at PR3 time.
"""

from __future__ import annotations

from pr3_fixture_loader import load_pr3_fixture


def load_pr5_fixture(
    fixture_name: str,
    *,
    program: str = "PR5",
    nominal_term: int = 1,
):
    """Materialise a ``pr5_*.json`` fixture.

    Returns ``(scenario, board, raw_fixture_dict)``. Delegates to
    ``load_pr3_fixture`` — the fixture grammar is identical.
    """
    return load_pr3_fixture(
        fixture_name,
        program=program,
        nominal_term=nominal_term,
    )
