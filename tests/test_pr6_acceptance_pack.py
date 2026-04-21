"""PR6 — acceptance pack (aggregation + parity + CLI). Green at commit 7.

The tripwire here is the import of ``core.services.timetable_pr6_parity``,
which commit 7 creates. Until then the whole file fails at collection
(which is exactly what PR6 commit 1 wants).
"""

from __future__ import annotations

from django.test import SimpleTestCase

from core.services.timetable_pr6_parity import (  # noqa: E402 — tripwire
    strip_pr6_fields_for_parity,
)

STAGE_KEYS = ("greedy", "sa", "cpsat", "chain", "rooming_repair")


class TestScenarioAggregation(SimpleTestCase):
    """Scenario-level telemetry must equal the sum of board-level telemetry."""

    def test_scenario_sum_equals_board_sum(self) -> None:
        self.skipTest("wired at PR6 commit 7 (scenario aggregation)")


class TestFlagOffZeroTelemetry(SimpleTestCase):
    """With the flag off, every stage key must be zero."""

    def test_flag_off_all_zero(self) -> None:
        self.skipTest("wired at PR6 commit 7 (flag-off fixture)")


class TestFlagOffParityHelper(SimpleTestCase):
    """``strip_pr6_fields_for_parity`` removes PR6 telemetry for byte-equality
    comparisons against pre-PR6 baselines."""

    def test_strip_removes_stage_telemetry_block(self) -> None:
        payload = {
            "placements": [],
            "stage_telemetry": {
                "stage_ms": {k: 0 for k in STAGE_KEYS},
                "stage_iterations": {k: 0 for k in STAGE_KEYS},
            },
        }
        stripped = strip_pr6_fields_for_parity(payload)
        self.assertNotIn("stage_telemetry", stripped)
        # Other keys must be left alone.
        self.assertIn("placements", stripped)


class TestPR6AcceptanceCLI(SimpleTestCase):
    """``python manage.py pr6_telemetry_report`` must agree with the planner
    payload for the same scenario."""

    def test_cli_matches_payload(self) -> None:
        self.skipTest("wired at PR6 commit 7 (CLI + acceptance report)")


class TestStageIterationsMonotonic(SimpleTestCase):
    """Iteration counters are non-negative integers — no floats, no negatives."""

    def test_iterations_are_non_negative_ints(self) -> None:
        self.skipTest("wired at PR6 commit 7 (payload shape assertions)")
