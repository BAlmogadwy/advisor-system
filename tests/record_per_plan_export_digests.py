"""Record the per-plan export digests that ``test_tw_programme_view`` (PV1) replays.

Recorded at ``cd15266`` (6a), before sub-PR 6b moved the export's programme
roles into ``timetable_programme_view``: the oracle must describe the export
before the move, or "the workbooks are unchanged" proves nothing. Never
re-record it on a branch that changes the export. Record with lxml blocked,
as CI and production run::

    OPENPYXL_LXML=False TW_PER_PLAN_DIGESTS=<file> python -m pytest \\
        -p no:cacheprovider tests/record_per_plan_export_digests.py

``TW_PER_PLAN_DUMP=<file>`` writes every sheet's lines instead of digests, to
compare two trees with ``tests/per_plan_export_digests_support.py``.

Not collected by default (no ``test_`` prefix); pytest runs it when named.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.per_plan_export_digests_support import (
    SCENARIOS,
    build,
    digests_of,
    scenario_exports,
)

_produced: dict[str, dict] = {}


@pytest.mark.django_db
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_record_one_scenario(monkeypatch, settings, name) -> None:
    if not (os.environ.get("TW_PER_PLAN_DIGESTS") or os.environ.get("TW_PER_PLAN_DUMP")):
        pytest.fail("Set TW_PER_PLAN_DIGESTS (or TW_PER_PLAN_DUMP) to the file to write.")
    exports = scenario_exports(build(name, monkeypatch, settings))
    if os.environ.get("TW_PER_PLAN_DUMP"):
        _produced[name] = exports
    else:
        _produced[name] = {search: digests_of(lines) for search, lines in exports.items()}


def test_write_the_file() -> None:
    """Runs last (definition order): every scenario's result, in ``SCENARIOS`` order."""
    assert list(_produced) == list(SCENARIOS), "a scenario failed to record"
    target = os.environ.get("TW_PER_PLAN_DUMP") or os.environ["TW_PER_PLAN_DIGESTS"]
    Path(target).write_text(
        json.dumps(_produced, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
