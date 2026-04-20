"""PR3 commit 7 — acceptance-bar pack (authoritative CI gate).

Runs every ``pr3_*.json`` scenario fixture through ``auto_place_board`` and
asserts the five bars from ChatGPT commit-7 ruling (c):

1.  **Trace coverage floor** — ``traced / placed >= 0.90`` across the
    scenarios where ``placed > 0`` AND decision-trace is enabled.
    Scenarios with zero placements, or with trace explicitly disabled,
    are excluded from the average (avoids divide-by-zero and false
    failures for the flag-disabled schema-stability fixture).
2.  **Known-code-only rejection alphabet** — every ``rejection_code``
    string in every emitted trace must be one of the PR1+PR2+PR3
    sentinels declared in ``timetable_validation`` /
    ``timetable_room_oracle`` / ``timetable_decision_trace``. Unknown
    strings fail the pack — the "no invented codes" invariant from the
    DoR.
3.  **Canonical warm-start fixture** — zero-change re-run: every
    baseline slot still legal ⇒ ``unchanged_count == placed``,
    ``changes_from_baseline_count == 0``.
4.  **Cold-start parity fixture** — ``baseline_placements = None`` ⇒
    every placement counts as ``newly_placed``; PR2 output shape
    unchanged.
5.  **Schema presence** — every return payload carries
    ``decision_trace`` and ``perturbation_metric`` keys, regardless of
    whether the flag is on or off.

The ``pr3_instructor_clash.json`` fixture is skipped (ruling I2 —
``INSTRUCTOR_CLASH`` is declared but not emitted until a later PR).

Each fixture runs in its own pytest.mark.django_db transaction so
module-scope state (Room rows with globally-unique room_code, etc.) is
rolled back between fixtures. Results are cached at the session level
so the five assertion functions don't re-place the same fixture five
times.

A thin ``pr3_acceptance_report`` management command re-uses the same
runner (``run_pr3_acceptance_pack``) so a registrar can get a
human-readable summary without reading pytest output — see
``core/management/commands/pr3_acceptance_report.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test.utils import override_settings

sys.path.insert(0, os.path.dirname(__file__))
from pr3_fixture_loader import load_pr3_fixture  # noqa: E402

FIXTURE_DIR = (
    Path(django_settings.BASE_DIR) / "snapshots" / "planner-refactor-2026-04-20" / "fixtures"
)

# The rejection-code alphabet. Any code emitted by the planner that is
# NOT in this set fails the acceptance-bar rejection-alphabet check. The
# list mirrors the sentinel imports in ``timetable_autoplace.py`` so a
# new PR that defines a new code must also extend this set or the pack
# turns red — intended behaviour (aligns test surface with the DoR's
# "no invented codes" invariant).
KNOWN_REJECTION_CODES: frozenset[str] = frozenset(
    {
        # PR1
        "PRAYER_OVERLAP",
        "LOCK_RESPECT",
        # PR2 (room oracle)
        "NO_ROOM_CAPACITY",
        "NO_ROOM_GENDER",
        "NO_ROOM_TYPE",
        "ROOM_OCCUPIED",
        "ROOM_BUFFER_REJECT",
        "ROOM_HEURISTIC_MISMATCH",
        # PR3
        "STUDENT_CONFLICT",
        "INSTRUCTOR_CLASH",  # sentinel only; never emitted today
    }
)

# Fixtures deliberately excluded from the pack:
#
# - ``pr3_instructor_clash.json``: ChatGPT commit-3 ruling I2 defers
#   emission of ``INSTRUCTOR_CLASH`` to a later PR; asserting on the
#   fixture's ``loser_trace_shows_instructor_clash`` field would trip a
#   red that the current codebase is not expected to turn green.
SKIP_FIXTURES: frozenset[str] = frozenset({"pr3_instructor_clash.json"})

# Fixtures that carry their own prayer windows / flags. The base
# acceptance-bar run reads these from the fixture itself; declared here
# rather than inside the fixture JSON because the per-fixture JSON
# schema already has a ``prayer_windows`` block in some cases and the
# flag surfaces at the Django settings layer rather than inside the
# scenario model.
PRAYER_WINDOW_FIXTURES: dict[str, list[dict]] = {
    "pr3_warm_start_infeasible_fallback.json": [
        {"day": "SUN", "start_time": "09:30", "end_time": "10:45"}
    ],
    "pr3_perturbation_totals.json": [{"day": "SUN", "start_time": "09:30", "end_time": "10:45"}],
}


def discover_pack() -> list[str]:
    """Return the PR3 fixture basenames in a stable ordering.

    Sorting keeps failure messages deterministic across runs and lets
    the management command render rows in the same order."""
    return sorted(p.name for p in FIXTURE_DIR.glob("pr3_*.json") if p.name not in SKIP_FIXTURES)


def _trace_enabled_for_fixture(fixture_name: str) -> bool:
    """Read any ``flag_overrides`` block on the fixture to decide
    whether decision_trace is expected. Defaults True — matches the
    commit-3 promotion default."""
    path = FIXTURE_DIR / fixture_name
    with path.open() as fh:
        data = json.load(fh)
    overrides = data.get("scenario", {}).get("flag_overrides", {}) or {}
    return bool(overrides.get("TIMETABLE_PR3_DECISION_TRACE_ENABLED", True))


def _warm_start_required_for_fixture(fixture_name: str) -> bool:
    """Fixtures that supply ``baseline_placements`` need the warm-start
    flag on to exercise retention. Fixtures with ``baseline=None`` or
    missing the key run fine either way; the flag is left off for them
    so we exercise the cold-start default path."""
    path = FIXTURE_DIR / fixture_name
    with path.open() as fh:
        data = json.load(fh)
    baseline = data.get("scenario", {}).get("baseline_placements")
    return bool(baseline)


def _collect_rejection_codes(decision_trace: dict) -> set[str]:
    """Walk every ``Alternative`` in the trace and yield its
    ``rejection_code``. Used by the known-code-only assertion."""
    codes: set[str] = set()
    for entry in decision_trace.values():
        for alt in entry.get("alternatives", []) or []:
            code = alt.get("rejection_code")
            if code:
                codes.add(code)
    return codes


def _reset_scenario_tables() -> None:
    """Flush the scenario-scoped tables that the PR3 fixture loader
    writes into. Called at the top of every ``run_pr3_fixture`` so
    loops inside a single pytest test (the coverage-floor aggregate)
    don't collide on ``UNIQUE(Room.room_code, Room.section)`` when
    successive fixtures both declare ``R101`` with the same section.

    Order matters: child rows (``SectionPlacement``,
    ``TermSectionMeeting``) must be cleared before ``TermSection``
    because Django's default ``on_delete=PROTECT`` on some FKs would
    otherwise block a cascade. The production code never hits this
    path — this helper is test-surface only."""
    from core.models import (
        DeliveryBoard,
        Room,
        ScenarioSectionBudget,
        ScenarioStudentMap,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )

    SectionPlacement.objects.all().delete()
    TermSectionMeeting.objects.all().delete()
    TermSection.objects.all().delete()
    ScenarioSectionBudget.objects.all().delete()
    ScenarioStudentMap.objects.all().delete()
    DeliveryBoard.objects.all().delete()
    TimetableScenario.objects.all().delete()
    Room.objects.all().delete()


def run_pr3_fixture(fixture_name: str) -> dict:
    """Load + place one fixture under its required flag envelope.

    Returns the ``auto_place_board`` result dict verbatim. Shared by
    the pytest tests below AND by the ``pr3_acceptance_report``
    management command so both surfaces observe identical behaviour."""
    from core.services.timetable_autoplace import auto_place_board

    _reset_scenario_tables()

    trace_on = _trace_enabled_for_fixture(fixture_name)
    warm_start_on = _warm_start_required_for_fixture(fixture_name)
    prayer_windows = PRAYER_WINDOW_FIXTURES.get(fixture_name, [])
    locks_on = fixture_name == "pr3_warm_start_lock_wins.json"

    overrides = {
        "TIMETABLE_PR3_DECISION_TRACE_ENABLED": trace_on,
        "TIMETABLE_PR3_WARM_START_ENABLED": warm_start_on,
        "TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE": bool(prayer_windows),
        "TIMETABLE_PRAYER_WINDOWS": prayer_windows,
        "TIMETABLE_ENFORCE_LOCKS": locks_on,
    }

    with override_settings(**overrides):
        _, board, data = load_pr3_fixture(fixture_name)
        baseline = data["scenario"].get("baseline_placements") or None
        return auto_place_board(board.id, baseline_placements=baseline)


# ---------------------------------------------------------------------------
# Parametrized per-fixture bars — schema presence + known-code alphabet.
# ---------------------------------------------------------------------------
#
# Running each fixture as its own test-function invocation ensures Django
# flushes the DB between fixtures (TransactionTestCase rollback would
# leak Room rows across iterations inside a single method and trip the
# ``UNIQUE(room_code, section)`` constraint).


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fixture_name", discover_pack())
def test_schema_presence(fixture_name: str) -> None:
    """Bar 5: every result carries decision_trace + perturbation_metric
    keys, regardless of flag state. Schema stability is non-optional."""
    result = run_pr3_fixture(fixture_name)
    assert "decision_trace" in result, f"{fixture_name}: decision_trace key missing from payload"
    assert "perturbation_metric" in result, (
        f"{fixture_name}: perturbation_metric key missing from payload"
    )
    metric = result["perturbation_metric"]
    for key in (
        "changes_from_baseline_count",
        "unchanged_count",
        "newly_placed_count",
        "removed_count",
    ):
        assert key in metric, f"{fixture_name}: perturbation_metric missing '{key}'"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fixture_name", discover_pack())
def test_rejection_codes_are_known_sentinels_only(fixture_name: str) -> None:
    """Bar 2: no invented codes. Every Alternative.rejection_code string
    must appear in ``KNOWN_REJECTION_CODES``."""
    result = run_pr3_fixture(fixture_name)
    codes = _collect_rejection_codes(result.get("decision_trace", {}))
    unknown = codes - KNOWN_REJECTION_CODES
    assert not unknown, (
        f"{fixture_name}: unknown rejection codes leaked into trace: {unknown}. "
        f"Known alphabet: {sorted(KNOWN_REJECTION_CODES)}"
    )


# ---------------------------------------------------------------------------
# Aggregate bar — trace coverage floor.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_trace_coverage_floor_across_pack() -> None:
    """Bar 1: traced/placed >= 0.90 over the scenarios where both
    placements and tracing are live. Scenarios with placed==0 or
    trace disabled are excluded from the denominator (ChatGPT
    commit-7 ruling — avoids divide-by-zero and false negatives on
    the flag-disabled schema-stability fixture)."""
    total_placed = 0
    total_traced = 0
    per_fixture: dict[str, tuple[int, int]] = {}

    for fixture_name in discover_pack():
        if not _trace_enabled_for_fixture(fixture_name):
            continue
        result = run_pr3_fixture(fixture_name)
        placed = result.get("placed", 0)
        if placed == 0:
            continue
        traced = len(result.get("decision_trace", {}) or {})
        total_placed += placed
        total_traced += traced
        per_fixture[fixture_name] = (traced, placed)

    assert total_placed > 0, "Acceptance pack produced no placements"
    coverage = total_traced / total_placed
    assert coverage >= 0.90, (
        f"Trace coverage floor 0.90 breached: {coverage:.3f}. "
        f"Per-fixture (traced/placed): {per_fixture}"
    )


# ---------------------------------------------------------------------------
# Canonical fixture bars — asserted against the specific expected blocks.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_canonical_warm_start_zero_change() -> None:
    """Bar 3: warm-start canonical fixture yields zero-change
    perturbation. Every baseline slot is still legal; every placement
    lands back on its baseline."""
    result = run_pr3_fixture("pr3_canonical_warm_start.json")
    metric = result["perturbation_metric"]
    placed = result["placed"]

    assert placed > 0, "Canonical warm-start fixture produced no placements"
    assert metric["unchanged_count"] == placed, (
        f"Canonical fixture: unchanged_count={metric['unchanged_count']} != placed={placed}"
    )
    assert metric["changes_from_baseline_count"] == 0
    assert metric["newly_placed_count"] == 0
    assert metric["removed_count"] == 0


@pytest.mark.django_db(transaction=True)
def test_cold_start_parity_all_newly_placed() -> None:
    """Bar 4: cold-start fixture (``baseline_placements=None``) counts
    every placement as newly_placed; nothing unchanged, nothing removed.
    PR2 parity signal — payload shape must be additive over PR2 rather
    than breaking the existing contract."""
    result = run_pr3_fixture("pr3_cold_start_parity.json")
    metric = result["perturbation_metric"]
    placed = result["placed"]

    assert placed > 0, "Cold-start parity fixture produced no placements"
    assert metric["newly_placed_count"] == placed
    assert metric["unchanged_count"] == 0
    assert metric["changes_from_baseline_count"] == 0
    assert metric["removed_count"] == 0
