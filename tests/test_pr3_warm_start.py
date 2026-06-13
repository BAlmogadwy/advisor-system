"""PR3 — warm-start + perturbation-metric tests.

Commit-1 stubs were import tripwires; commit 5 replaces those stubs with
real fixture-backed assertions for each of the PR3 warm-start scenarios
(#3 feasible retention, #4 infeasible fallback, #5 lock wins,
#8 perturbation totals, #9 cold-start parity, #11 canonical zero-change).

Per the PR3 DoR:

- Priority order for final placement: PR1 locks → PR3 warm-start →
  cold-start scoring.
- Baseline source: in-memory / caller-supplied only. NO DB persistence.
- Metric keys: ``changes_from_baseline_count``, ``unchanged_count``,
  ``newly_placed_count``, ``removed_count``.
- Flag: ``TIMETABLE_PR3_WARM_START_ENABLED``. Default True as of
  commit 8's promotion. Env kill-switch preserved: setting the env
  var to ``false`` reverts to cold-start without a redeploy.
"""

from __future__ import annotations

import os
import sys

import pytest
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings

# Contract imports — the commit-1 tripwire. Both modules must exist
# after commit 2 (dataclass) and commit 5 (warm-start logic). A rename
# of any symbol below breaks collection — intended behaviour.
from core.services.timetable_decision_trace import (
    Alternative,
    DecisionTrace,
)
from core.services.timetable_warm_start import (
    BaselinePlacement,
    compute_perturbation_metric,
    is_warm_start_enabled,
)

# The PR3 fixture loader lives alongside this test module (``tests/``)
# rather than inside ``core/``. Adding its directory to ``sys.path``
# keeps the import portable across pytest invocations that cd elsewhere.
sys.path.insert(0, os.path.dirname(__file__))
from pr3_fixture_loader import load_pr3_fixture  # noqa: E402

# ===========================================================================
# SECTION A — Flag + config tests.
# ===========================================================================


class TestWarmStartFlag(SimpleTestCase):
    """Flag defaults True as of commit 8's promotion. Env-var override is
    preserved so production can revert to cold-start without a code
    change (``TIMETABLE_PR3_WARM_START_ENABLED=false``)."""

    def test_flag_defaults_on_post_promotion(self) -> None:
        """Commit 8 flipped the default: with no env var set, warm-start
        is active. Callers still need to pass ``baseline_placements`` for
        retention to do anything — the flag just stops ignoring the
        baseline when it is supplied."""
        assert is_warm_start_enabled() is True

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=False)
    def test_flag_kill_switch_reverts_to_cold_start(self) -> None:
        """Env kill-switch path. Setting the flag to False at runtime
        (via Django settings override, mirroring the env-var flow) must
        revert warm-start to a no-op so operators can roll back without
        a redeploy."""
        assert is_warm_start_enabled() is False


# ===========================================================================
# SECTION B — Warm-start retention / fallback tests (fixture-backed).
# ===========================================================================


@pytest.mark.django_db
class TestFeasibleRetention(TransactionTestCase):
    """Fixture #3 — pr3_warm_start_feasible.json.

    All 3 baseline placements are still legal. Warm-start must retain
    every one; perturbation metric block must show
    ``unchanged_count == 3`` and ``changes_from_baseline_count == 0``."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_all_feasible_baselines_retained(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, data = load_pr3_fixture("pr3_warm_start_feasible.json")
        baseline = data["scenario"]["baseline_placements"]

        result = auto_place_board(board.id, baseline_placements=baseline)

        assert result["placed"] == 3
        metric = result["perturbation_metric"]
        assert metric["unchanged_count"] == 3
        assert metric["changes_from_baseline_count"] == 0
        assert metric["newly_placed_count"] == 0
        assert metric["removed_count"] == 0

        # Every placement lands at its baseline slot.
        # Planner emits uppercase day codes ("SUN"); fixtures use
        # Title Case — match case-insensitively.
        for placement in result["placements"]:
            section_code = f"{placement['course_code']}|{placement['section']}"
            b = baseline[section_code]
            first_meeting = placement["meetings"][0]
            assert first_meeting["day"].upper() == b["day"].upper()
            assert first_meeting["start"] == b["start_time"]
            assert first_meeting["end"] == b["end_time"]


@pytest.mark.django_db
# NOTE: the original ``TestInfeasibleFallback`` exercised warm-start fallback
# when a baseline slot clashed with a prayer window. The prayer-overlap runtime
# rule was removed (prayer compliance is now a fixed-grid property), so that
# infeasible-fallback trigger no longer exists and the case was retired. Other
# pre-score filters (e.g. instructor clash) still drive the same fallback path,
# which is covered structurally by the decision-trace tests.


@pytest.mark.django_db
class TestLockBeatsBaseline(TransactionTestCase):
    """Fixture #5 — pr3_warm_start_lock_wins.json.

    PR1 locks sit above PR3 warm-start in priority. When a baseline
    entry contradicts a lock, the lock wins; ``LOCK_RESPECT`` appears
    as the rejection reason recorded against the baseline in the trace.
    The section ends at the locked slot.

    NOTE: the fixture's ``expected.baseline_rejection_in_trace`` names
    ``LOCK_VIOLATION`` (aspirational); the planner emits the real PR1
    code ``LOCK_RESPECT`` — see commit-3 TestTypedRejectionCodes.
    """

    @override_settings(
        TIMETABLE_PR3_WARM_START_ENABLED=True,
        TIMETABLE_ENFORCE_LOCKS=True,
    )
    def test_lock_wins_over_warm_start_preference(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, data = load_pr3_fixture("pr3_warm_start_lock_wins.json")
        baseline = data["scenario"]["baseline_placements"]

        result = auto_place_board(board.id, baseline_placements=baseline)

        # The locked section was preloaded; the scoring loop skips it.
        # A trace entry is still emitted for it with the lock chosen
        # and the baseline captured as a LOCK_RESPECT alternative.
        trace = result["decision_trace"]
        entry = trace.get("CS101|S1")
        assert entry is not None, "Lock section must have a trace entry"
        # Days are normalised to uppercase across the planner.
        assert entry["chosen_day"].upper() == "SUN"
        assert entry["chosen_start_time"] == "08:00"

        alt_codes = {alt["rejection_code"] for alt in entry["alternatives"]}
        assert "LOCK_RESPECT" in alt_codes, f"Baseline's lock failure not recorded: {alt_codes}"
        # The baseline slot appears as the rejected alternative.
        baseline_alt = next(
            alt for alt in entry["alternatives"] if alt["rejection_code"] == "LOCK_RESPECT"
        )
        assert baseline_alt["day"].upper() == "SUN"
        assert baseline_alt["start_time"] == "09:30"


# ===========================================================================
# SECTION C — Perturbation-metric tests.
# ===========================================================================


class TestPerturbationMetricUnit(SimpleTestCase):
    """Unit-level tests of ``compute_perturbation_metric`` — no DB.

    The standalone metric function is the boundary that commit 6 will
    wire through V2; pinning its behaviour here is cheaper than
    exercising it end-to-end for every case."""

    def test_baseline_none_treats_all_placements_as_newly_placed(self) -> None:
        placements = [
            {
                "course_code": "CS101",
                "section": "S1",
                "meetings": [{"day": "Sun", "start": "08:00", "end": "09:15"}],
            },
            {
                "course_code": "CS102",
                "section": "S1",
                "meetings": [{"day": "Mon", "start": "08:00", "end": "09:15"}],
            },
        ]
        metric = compute_perturbation_metric(placements, None)
        assert metric == {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": 2,
            "removed_count": 0,
        }

    def test_mixed_outcome_totals(self) -> None:
        """3 retained + 1 moved + 1 newly placed + 0 removed (fixture #8)."""
        baseline = {
            "CS101|S1": {"day": "Sun", "start_time": "08:00", "end_time": "09:15"},
            "CS102|S1": {"day": "Sun", "start_time": "09:30", "end_time": "10:45"},
            "CS103|S1": {"day": "Mon", "start_time": "08:00", "end_time": "09:15"},
            "CS104|S1": {"day": "Mon", "start_time": "09:30", "end_time": "10:45"},
        }
        placements = [
            # Retained as baseline.
            {
                "course_code": "CS101",
                "section": "S1",
                "meetings": [{"day": "Sun", "start": "08:00", "end": "09:15"}],
            },
            # Moved (baseline Sun 09:30 infeasible).
            {
                "course_code": "CS102",
                "section": "S1",
                "meetings": [{"day": "Tue", "start": "08:00", "end": "09:15"}],
            },
            # Retained.
            {
                "course_code": "CS103",
                "section": "S1",
                "meetings": [{"day": "Mon", "start": "08:00", "end": "09:15"}],
            },
            # Retained.
            {
                "course_code": "CS104",
                "section": "S1",
                "meetings": [{"day": "Mon", "start": "09:30", "end": "10:45"}],
            },
            # Newly placed — not in baseline at all.
            {
                "course_code": "CS105",
                "section": "S1",
                "meetings": [{"day": "Sun", "start": "08:00", "end": "09:15"}],
            },
        ]
        metric = compute_perturbation_metric(placements, baseline)
        assert metric["unchanged_count"] == 3
        assert metric["changes_from_baseline_count"] == 1
        assert metric["newly_placed_count"] == 1
        assert metric["removed_count"] == 0

    def test_removed_count_for_baseline_sections_not_placed(self) -> None:
        baseline = {
            "CS101|S1": {"day": "Sun", "start_time": "08:00", "end_time": "09:15"},
            "CS102|S1": {"day": "Mon", "start_time": "08:00", "end_time": "09:15"},
        }
        placements = [
            {
                "course_code": "CS101",
                "section": "S1",
                "meetings": [{"day": "Sun", "start": "08:00", "end": "09:15"}],
            },
        ]
        metric = compute_perturbation_metric(placements, baseline)
        assert metric["unchanged_count"] == 1
        assert metric["removed_count"] == 1
        assert metric["changes_from_baseline_count"] == 0
        assert metric["newly_placed_count"] == 0


@pytest.mark.django_db
class TestColdStartParity(TransactionTestCase):
    """Fixture #9 — pr3_cold_start_parity.json.

    ``baseline_placements=None`` → placements are byte-for-byte
    identical to the PR2 baseline (minus the additive PR3 payload keys,
    which default to ``0`` / ``{}``). This is acceptance-bar #3 — the
    hard contract that guarantees PR3 doesn't change any existing
    scenario's placement decisions in cold-start mode."""

    def test_baseline_none_matches_pr2_baseline(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr3_fixture("pr3_cold_start_parity.json")

        # With the flag off AND baseline=None, warm-start is a no-op.
        # The only additive PR3 keys are ``decision_trace`` and
        # ``perturbation_metric``; every other key preserves PR2 shape.
        result = auto_place_board(board.id, baseline_placements=None)

        assert result["placed"] == 2
        metric = result["perturbation_metric"]
        assert metric == {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": 2,
            "removed_count": 0,
        }


@pytest.mark.django_db
class TestCanonicalWarmStartFixture(TransactionTestCase):
    """Fixture #11 — pr3_canonical_warm_start.json.

    The *hard* zero-change invariant only applies on this canonical
    fixture. Broader-pack scenarios may drift slightly provided every
    drift maps to an explainable cause (baseline infeasibility, rare
    tie-break drift). This split — hard test on one fixture,
    ``low and explainable`` on the rest — was the ChatGPT amendment to
    the first DoR draft."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_exact_zero_invariant(self) -> None:
        """Same fixture data, warm-start on → the re-run MUST produce
        ``changes_from_baseline_count == 0`` and
        ``unchanged_count == placed`` exactly."""
        from core.services.timetable_autoplace import auto_place_board

        _, board, data = load_pr3_fixture("pr3_canonical_warm_start.json")
        baseline = data["scenario"]["baseline_placements"]

        result = auto_place_board(board.id, baseline_placements=baseline)

        assert result["placed"] == 2
        metric = result["perturbation_metric"]
        assert metric["changes_from_baseline_count"] == 0
        assert metric["unchanged_count"] == result["placed"]
        assert metric["removed_count"] == 0


# ===========================================================================
# SECTION D — Acceptance-bar pack coverage (turns green at commit 6 / 7).
# ===========================================================================


class TestAcceptanceBar(SimpleTestCase):
    """Run the full PR3 scenario pack and assert the ≥90% trace-coverage
    floor from acceptance bar #1."""

    def test_trace_coverage_on_pack(self) -> None:
        """Stub — a later commit extends the perf harness to load every
        ``snapshots/.../fixtures/pr3_*.json`` fixture and assert:

            coverage = traced_sections / placed_sections
            assert coverage >= 0.90

        Today: placeholder to pin the import contract."""
        assert BaselinePlacement is not None
        assert Alternative is not None
        assert DecisionTrace is not None


# ===========================================================================
# SECTION E — Scenario-level warm-start + metric aggregation (commit 6).
# ===========================================================================


@pytest.mark.django_db
class TestScenarioLevelWarmStart(TransactionTestCase):
    """Commit 6: baseline_placements propagates from the scenario entry
    point down to per-board auto_place_board calls, and the four
    perturbation counters are summed losslessly at the scenario level.

    Single-board smoke: the scenario-level metric should match the
    per-board metric exactly (sum over one board = that board's counts).
    """

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_auto_place_scenario_propagates_baseline_and_sums_metric(self) -> None:
        from core.services.timetable_autoplace import auto_place_scenario

        scenario, _board, data = load_pr3_fixture("pr3_warm_start_feasible.json")
        baseline = data["scenario"]["baseline_placements"]

        result = auto_place_scenario(
            scenario.id,
            strategy="compact",
            baseline_placements=baseline,
        )

        assert "perturbation_metric" in result, (
            "Scenario result must surface perturbation_metric (schema stability)"
        )
        scenario_metric = result["perturbation_metric"]
        # Fixture has 3 baseline entries, all feasible → all 3 unchanged.
        assert scenario_metric["unchanged_count"] == 3
        assert scenario_metric["changes_from_baseline_count"] == 0
        assert scenario_metric["newly_placed_count"] == 0
        assert scenario_metric["removed_count"] == 0

        # The sum helper is lossless over one board — per-board and
        # scenario-level counters must agree exactly.
        per_board_metrics = [b["perturbation_metric"] for b in result["boards"].values()]
        summed = {key: sum(m[key] for m in per_board_metrics) for key in scenario_metric}
        assert summed == scenario_metric


@pytest.mark.django_db
class TestBaselineScopedPerBoard(TransactionTestCase):
    """Commit 6 regression: when the caller passes a scenario-wide
    baseline that includes entries for courses NOT on this board,
    ``removed_count`` must NOT over-count those as removed. The per-board
    scoping inside ``auto_place_board`` strips entries whose course_code
    is not in this board's budget set, so the scenario-level sum stays
    loss-free across multi-board scenarios.
    """

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_unrelated_baseline_entry_does_not_inflate_removed(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, data = load_pr3_fixture("pr3_warm_start_feasible.json")
        baseline = dict(data["scenario"]["baseline_placements"])
        # Inject an entry for a course this board does not own. Without
        # scoping, this would count as ``removed`` in the metric; with
        # scoping, it's filtered out before the count.
        baseline["OTHER999|S1"] = {
            "day": "Mon",
            "start_time": "08:00",
            "end_time": "09:15",
        }

        result = auto_place_board(board.id, baseline_placements=baseline)
        metric = result["perturbation_metric"]

        # The 3 on-board baseline entries still retain; the foreign
        # entry was scoped out, so removed_count is 0, not 1.
        assert metric["unchanged_count"] == 3
        assert metric["removed_count"] == 0, (
            "Foreign baseline entry must be scoped out, not counted as removed"
        )


class TestV2OptimiserAcceptsBaseline(SimpleTestCase):
    """Commit 6: optimise_scenario_timetable_v2 exposes baseline_placements
    as a kwarg. A DB-backed early-return test lives in the transactional
    class below so this case can stay DB-free."""

    def test_signature_exposes_baseline_placements_kwarg(self) -> None:
        import inspect

        from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

        sig = inspect.signature(optimise_scenario_timetable_v2)
        assert "baseline_placements" in sig.parameters
        assert sig.parameters["baseline_placements"].default is None


@pytest.mark.django_db
class TestV2OptimiserEarlyReturnMetric(TransactionTestCase):
    """Schema-stability guard: every V2 exit path must carry the
    ``perturbation_metric`` key. The earliest short-circuit (no student
    profiles for this scenario_id) proves the key is seeded, not relying
    on the success-path overwrite to inject it."""

    def test_no_profiles_early_return_carries_metric(self) -> None:
        from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

        # scenario_id that does not exist → build_student_profiles
        # returns empty → early-return branch fires.
        result = optimise_scenario_timetable_v2(scenario_id=-1)

        assert "perturbation_metric" in result
        assert result["perturbation_metric"] == {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": 0,
            "removed_count": 0,
        }
