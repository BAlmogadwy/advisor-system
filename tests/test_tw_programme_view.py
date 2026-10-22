"""6b: programme roles shared with the per-plan export (§7.4, sub-PR 6b).

PV1 holds the export's workbooks to an oracle recorded before the move
(``tests/fixtures/per_plan_export_digests.json``): moving the roles into their
own module changed no workbook.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.per_plan_export_digests_support import (
    SCENARIOS,
    build,
    digests_of,
    first_difference,
    scenario_exports,
)

DIGESTS = Path(__file__).resolve().parent / "fixtures" / "per_plan_export_digests.json"


# ── PV1: the export's workbooks are unchanged ────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_pv1_every_export_workbook_equals_the_oracle(monkeypatch, settings, name) -> None:
    """Per scenario and search: the file names, and each sheet's digest of its
    values, fills, fonts, merges and widths, in order."""
    recorded = json.loads(DIGESTS.read_text(encoding="utf-8"))[name]
    exports = scenario_exports(build(name, monkeypatch, settings))
    now = {search: digests_of(lines) for search, lines in exports.items()}
    difference = first_difference(recorded, now, (name,))
    assert difference is None, (
        f"{difference}\nDump both trees with TW_PER_PLAN_DUMP (tests/"
        "record_per_plan_export_digests.py) and compare them with "
        "tests/per_plan_export_digests_support.py to see the first line."
    )


def test_pv1_the_oracle_holds_every_scenario_and_search() -> None:
    """Not vacuous: every scenario and search is recorded, the SHARED search
    changes what some files show, and some exports draw term sheets."""
    from tests.per_plan_export_digests_support import SEARCHES

    recorded = json.loads(DIGESTS.read_text(encoding="utf-8"))
    assert list(recorded) == list(SCENARIOS)
    assert all(list(by_search) == list(SEARCHES) for by_search in recorded.values())
    shared = recorded["extra/shared_roles"]
    assert shared[""]["files"]["AI2.xlsx"] != shared["SHARED"]["files"]["AI2.xlsx"]
    sheets = [
        title
        for by_search in recorded.values()
        for result in by_search.values()
        for sheets in result.get("files", {}).values()
        for title in sheets
    ]
    assert sum(title.startswith("Term ") for title in sheets) > 80
