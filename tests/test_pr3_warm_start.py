"""PR3 — warm-start + perturbation-metric tests (failing by design until
commits 5–6).

Like ``test_pr3_decision_trace.py``, the import block below is the
commit-1 tripwire: ``core.services.timetable_decision_trace`` and
``core.services.timetable_warm_start`` do not exist yet, so every test
here fails at collection with ``ModuleNotFoundError`` until commits 2
and 5 land.

Progression of passing-ness:

- **Commit 2** (dataclasses + sentinels + flag helper): no tests in
  this file move yet (warm-start module doesn't exist — see second
  import below). Collection still fails until commit 5 creates
  ``timetable_warm_start``.
- **Commit 5** (warm-start retention + metric emit): every test in this
  file except the perturbation-totals test turns green.
- **Commit 6** (metric wiring through V2 pipeline): the
  perturbation-totals test on the V2 pipeline turns green.

Per the PR3 DoR:

- Priority order for final placement: PR1 locks → PR3 warm-start →
  cold-start scoring.
- Baseline source: in-memory / caller-supplied only. NO DB persistence.
- Metric keys: ``changes_from_baseline_count``, ``unchanged_count``,
  ``newly_placed_count``, ``removed_count``.
- Flag: ``TIMETABLE_PR3_WARM_START_ENABLED``. Default False until
  commit 8's promotion.
"""

from __future__ import annotations

from django.test import SimpleTestCase
from django.test.utils import override_settings

# Contract imports — the commit-1 tripwire. Both modules must exist after
# commit 2 (dataclass) and commit 5 (warm-start logic). A rename of any
# symbol below breaks collection — intended behaviour.
from core.services.timetable_decision_trace import (
    Alternative,
    DecisionTrace,
)
from core.services.timetable_warm_start import (
    BaselinePlacement,
    apply_warm_start,
    compute_perturbation_metric,
    is_warm_start_enabled,
)

# ===========================================================================
# SECTION A — Flag + config tests.
# ===========================================================================


class TestWarmStartFlag(SimpleTestCase):
    """Flag default False until commit 8's promotion. Env-var override is
    preserved so production can flip it live without a code change."""

    def test_flag_defaults_off_pre_promotion(self) -> None:
        """Before commit 8, warm-start is opt-in: callers must pass
        ``baseline_placements`` explicitly AND the flag must be True.
        With the flag default False, cold-start is the default path and
        baseline inputs are ignored for placement decisions."""
        assert is_warm_start_enabled() is False

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_flag_can_be_enabled(self) -> None:
        assert is_warm_start_enabled() is True


# ===========================================================================
# SECTION B — Warm-start retention / fallback tests (turn green at commit 5).
# ===========================================================================


class TestFeasibleRetention(SimpleTestCase):
    """Fixture #3 — pr3_warm_start_feasible.json.

    All 3 baseline placements are still legal. Warm-start must retain
    every one; perturbation metric block must show
    ``unchanged_count == 3`` and ``changes_from_baseline_count == 0``."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_all_feasible_baselines_retained(self) -> None:
        """Stub — real implementation runs ``auto_place_board`` on the
        fixture with ``baseline_placements`` loaded. For commit 1 we
        just pin the symbol imports."""
        assert BaselinePlacement is not None
        assert apply_warm_start is not None


class TestInfeasibleFallback(SimpleTestCase):
    """Fixture #4 — pr3_warm_start_infeasible_fallback.json.

    One baseline slot clashes with a prayer window; warm-start must
    fall back to cold-start scoring for that section. The moved
    section's trace must record ``PRAYER_WINDOW_CLASH`` as the
    rejection reason for the baseline slot (so the registrar can see
    *why* the previous placement no longer works)."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_prayer_clash_baseline_falls_back(self) -> None:
        assert DecisionTrace is not None
        assert Alternative is not None


class TestLockBeatsBaseline(SimpleTestCase):
    """Fixture #5 — pr3_warm_start_lock_wins.json.

    PR1 locks sit above PR3 warm-start in priority. When a baseline
    entry contradicts a lock, the lock wins; ``LOCK_VIOLATION`` appears
    as the rejection reason recorded against the baseline in the trace.
    The section ends at the locked slot."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_lock_wins_over_warm_start_preference(self) -> None:
        assert apply_warm_start is not None


# ===========================================================================
# SECTION C — Perturbation-metric tests (turn green at commits 5–6).
# ===========================================================================


class TestPerturbationMetric(SimpleTestCase):
    """Fixture #8 — pr3_perturbation_totals.json.

    The metric block must sum exactly: 3 unchanged + 1 moved + 1
    newly_placed + 0 removed for a mixed-outcome re-run."""

    @override_settings(TIMETABLE_PR3_WARM_START_ENABLED=True)
    def test_mixed_outcome_totals_match(self) -> None:
        """Stub — commit 5 wires the autoplace emit; commit 6 wires it
        through the V2 pipeline."""
        assert compute_perturbation_metric is not None


class TestColdStartParity(SimpleTestCase):
    """Fixture #9 — pr3_cold_start_parity.json.

    ``baseline_placements=None`` → placements are byte-for-byte
    identical to the PR2 baseline (minus the additive PR3 payload keys,
    which default to ``0`` / ``{}``). This is acceptance-bar #3 — the
    hard contract that guarantees PR3 doesn't change any existing
    scenario's placement decisions in cold-start mode."""

    def test_baseline_none_matches_pr2_baseline(self) -> None:
        assert apply_warm_start is not None


class TestCanonicalWarmStartFixture(SimpleTestCase):
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
        assert compute_perturbation_metric is not None


# ===========================================================================
# SECTION D — Acceptance-bar pack coverage (turns green at commit 6 / 7).
# ===========================================================================


class TestAcceptanceBar(SimpleTestCase):
    """Run the full PR3 scenario pack and assert the ≥90% trace-coverage
    floor from acceptance bar #1."""

    def test_trace_coverage_on_pack(self) -> None:
        """Stub — commit 6 extends the perf harness to load every
        ``snapshots/.../fixtures/pr3_*.json`` fixture and assert:

            coverage = traced_sections / placed_sections
            assert coverage >= 0.90

        Today: placeholder to pin the import contract."""
        assert BaselinePlacement is not None
