"""PR6 — stage-telemetry contract + per-stage emission tests (failing
until commit 2+).

Module-level imports of ``core.services.timetable_stage_telemetry``
fail at collection today — commit 2 creates that module. Section A
assertions about the empty-telemetry shape also fail until commit 2.
Section B stays red until each stage's instrumentation lands
(commits 3–6).

Progression of passing-ness:

- **Commit 2** (telemetry module + helpers + flag): Section A
  (shape / contract tests) turns green.
- **Commit 3** (greedy timing/work capture): the greedy leg of
  Section B turns green.
- **Commit 4** (SA timing/work capture): the SA leg of Section B
  turns green.
- **Commit 5** (CP-SAT timing/work capture): the CP-SAT leg of
  Section B turns green.
- **Commit 6** (chain + rooming_repair timing/work capture): the
  remaining legs of Section B turn green.

Per PR6 DoR (docs/PR6-DOR.md):

- Single flag: ``TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED``.
- Five stage keys: ``greedy``, ``sa``, ``cpsat``, ``chain``,
  ``rooming_repair``.
- ``stage_ms`` values are integer milliseconds; ``stage_iterations``
  are integer work counts.
- Keys always present; value ``0`` when stage did not run.
- Monotonic clock (``time.monotonic`` / ``time.perf_counter``) only.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings

# Contract imports — the commit-1 tripwire. Every symbol below names
# something commit 2 exposes. A rename or a dropped symbol breaks the
# suite at collection.
from core.services.timetable_stage_telemetry import (  # noqa: E402 — tripwire
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
    merge_stage_telemetry,
    record_stage_iterations,
    record_stage_ms,
)


def _run_greedy(board_id: int) -> dict:
    """Invoke the greedy placer. Local import keeps the planner stack
    out of collection time (mirrors PR5's helper)."""
    from core.services.timetable_autoplace import auto_place_board

    return auto_place_board(board_id)


STAGE_KEYS = ("greedy", "sa", "cpsat", "chain", "rooming_repair")


# ===========================================================================
# SECTION A — shape / contract tests (green at commit 2).
# ===========================================================================


class TestEmptyTelemetryShape(SimpleTestCase):
    """Pin the empty-telemetry shape before any instrumentation lands."""

    def test_empty_has_both_subkeys(self) -> None:
        t = empty_stage_telemetry()
        self.assertIn("stage_ms", t)
        self.assertIn("stage_iterations", t)

    def test_stage_ms_five_keys_all_zero(self) -> None:
        t = empty_stage_telemetry()
        self.assertEqual(set(t["stage_ms"].keys()), set(STAGE_KEYS))
        for k in STAGE_KEYS:
            self.assertEqual(t["stage_ms"][k], 0)
            self.assertIsInstance(t["stage_ms"][k], int)

    def test_stage_iterations_five_keys_all_zero(self) -> None:
        t = empty_stage_telemetry()
        self.assertEqual(set(t["stage_iterations"].keys()), set(STAGE_KEYS))
        for k in STAGE_KEYS:
            self.assertEqual(t["stage_iterations"][k], 0)
            self.assertIsInstance(t["stage_iterations"][k], int)


class TestRecordAndMerge(SimpleTestCase):
    """Helper semantics: record_* writes the named key; merge sums by key."""

    def test_record_stage_ms_sets_key(self) -> None:
        t = empty_stage_telemetry()
        record_stage_ms(t, "sa", 42)
        self.assertEqual(t["stage_ms"]["sa"], 42)
        for k in STAGE_KEYS:
            if k != "sa":
                self.assertEqual(t["stage_ms"][k], 0)

    def test_record_stage_iterations_sets_key(self) -> None:
        t = empty_stage_telemetry()
        record_stage_iterations(t, "chain", 7)
        self.assertEqual(t["stage_iterations"]["chain"], 7)

    def test_merge_sums_corresponding_keys(self) -> None:
        a = empty_stage_telemetry()
        record_stage_ms(a, "greedy", 100)
        record_stage_iterations(a, "greedy", 5)
        b = empty_stage_telemetry()
        record_stage_ms(b, "greedy", 50)
        record_stage_ms(b, "sa", 30)
        record_stage_iterations(b, "greedy", 3)
        record_stage_iterations(b, "sa", 2)

        out = merge_stage_telemetry(a, b)
        self.assertEqual(out["stage_ms"]["greedy"], 150)
        self.assertEqual(out["stage_ms"]["sa"], 30)
        self.assertEqual(out["stage_iterations"]["greedy"], 8)
        self.assertEqual(out["stage_iterations"]["sa"], 2)
        for k in ("cpsat", "chain", "rooming_repair"):
            self.assertEqual(out["stage_ms"][k], 0)
            self.assertEqual(out["stage_iterations"][k], 0)


class TestFlagHelperDefault(SimpleTestCase):
    """Flag defaults ``False`` through commits 2–7, flipped in commit 8."""

    def test_flag_helper_exists_and_returns_bool(self) -> None:
        self.assertIsInstance(is_stage_telemetry_enabled(), bool)


# ===========================================================================
# SECTION B — per-stage emission tests (green at commits 3–6).
#
# Each test runs the planner over a single-stage fixture and asserts
# only that stage's keys are non-zero. Kept as placeholders here; the
# real planner harness is wired in commits 3–6 alongside the
# instrumentation.
# ===========================================================================


class TestGreedyStageEmission(TransactionTestCase):
    """Green at commit 3 — running greedy over pr6_greedy_telemetry.json
    with the flag on populates greedy.ms and greedy.iterations without
    touching any other stage key."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True)
    def test_greedy_populates_only_greedy_keys(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        _, board, _ = load_pr6_fixture("pr6_greedy_telemetry.json")
        result = _run_greedy(board.id)
        telemetry = result.get("stage_telemetry")

        self.assertIsNotNone(telemetry, "auto_place_board must return stage_telemetry")
        self.assertIn("stage_ms", telemetry)
        self.assertIn("stage_iterations", telemetry)
        self.assertGreater(telemetry["stage_ms"]["greedy"], 0)
        self.assertGreater(telemetry["stage_iterations"]["greedy"], 0)
        for k in ("sa", "cpsat", "chain", "rooming_repair"):
            self.assertEqual(telemetry["stage_ms"][k], 0, f"non-greedy stage {k}.ms must be zero")
            self.assertEqual(
                telemetry["stage_iterations"][k],
                0,
                f"non-greedy stage {k}.iterations must be zero",
            )

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_leaves_all_telemetry_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        _, board, _ = load_pr6_fixture("pr6_greedy_telemetry.json")
        result = _run_greedy(board.id)
        telemetry = result.get("stage_telemetry")

        self.assertIsNotNone(telemetry)
        for k in STAGE_KEYS:
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)


class TestSAStageEmission(SimpleTestCase):
    """Green at commit 4."""

    def test_sa_ms_populated_when_sa_runs(self) -> None:
        self.skipTest("wired at PR6 commit 4 (SA instrumentation)")


class TestCPSATStageEmission(SimpleTestCase):
    """Green at commit 5."""

    def test_cpsat_ms_populated_when_cpsat_runs(self) -> None:
        self.skipTest("wired at PR6 commit 5 (CP-SAT instrumentation)")


class TestChainStageEmission(SimpleTestCase):
    """Green at commit 6."""

    def test_chain_ms_populated_when_chain_runs(self) -> None:
        self.skipTest("wired at PR6 commit 6 (chain instrumentation)")


class TestRoomingRepairStageEmission(SimpleTestCase):
    """Green at commit 6."""

    def test_rooming_repair_ms_populated_when_repair_runs(self) -> None:
        self.skipTest("wired at PR6 commit 6 (rooming-repair instrumentation)")
