"""PR6 — acceptance pack (aggregation + parity + CLI).

Green at commit 7. Asserts:

- ``strip_pr6_fields_for_parity`` removes the PR6-added ``stage_telemetry``
  block and leaves pre-PR6 keys untouched.
- Scenario-level ``stage_telemetry`` equals the per-key sum of board-
  level ``stage_telemetry`` (DoR §3 aggregation rule, no averaging).
- Flag off ⇒ every stage key is zero, both in the payload and after the
  parity helper strip.
- ``python manage.py pr6_telemetry_report`` surfaces all five stage keys
  and the current flag state.
- ``stage_iterations`` values are non-negative ``int`` (no floats, no
  negatives).
"""

from __future__ import annotations

import io
import json

from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings

from core.services.timetable_pr6_parity import (  # noqa: E402 — tripwire
    strip_pr6_fields_for_parity,
)
from core.services.timetable_stage_telemetry import (
    empty_stage_telemetry,
    merge_stage_telemetry,
)

STAGE_KEYS = ("greedy", "sa", "cpsat", "chain", "rooming_repair")


class TestScenarioAggregation(TransactionTestCase):
    """Scenario-level telemetry must equal the per-key sum of board-level
    telemetry (no averaging, per DoR §3 aggregation rule)."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True)
    def test_scenario_sum_equals_board_sum(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_autoplace import auto_place_scenario

        scenario, _, _ = load_pr6_fixture("pr6_greedy_telemetry.json")
        scenario_result = auto_place_scenario(scenario.id)

        scenario_tel = scenario_result.get("stage_telemetry")
        self.assertIsNotNone(scenario_tel, "auto_place_scenario must return stage_telemetry")

        # Reduce board telemetry via merge_stage_telemetry so we're
        # testing the same aggregation semantic the production code uses.
        expected = empty_stage_telemetry()
        for board_result in scenario_result.get("boards", {}).values():
            bt = board_result.get("stage_telemetry") or empty_stage_telemetry()
            expected = merge_stage_telemetry(expected, bt)

        self.assertEqual(scenario_tel["stage_ms"], expected["stage_ms"])
        self.assertEqual(scenario_tel["stage_iterations"], expected["stage_iterations"])


class TestFlagOffZeroTelemetry(TransactionTestCase):
    """With the flag off, every stage key must be zero — even when the
    underlying stages run and emit PR5 decision-trace entries."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_all_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_autoplace import auto_place_scenario

        scenario, _, _ = load_pr6_fixture("pr6_greedy_telemetry.json")
        result = auto_place_scenario(scenario.id)
        tel = result.get("stage_telemetry")
        self.assertIsNotNone(tel)
        for k in STAGE_KEYS:
            self.assertEqual(tel["stage_ms"][k], 0)
            self.assertEqual(tel["stage_iterations"][k], 0)


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
        self.assertIn("placements", stripped)

    def test_strip_leaves_pre_pr6_keys_untouched(self) -> None:
        payload = {
            "placements": [{"course": "CS101"}],
            "decision_trace": {"a": {"stage_origin": "sa"}},
            "perturbation_metric": {"changes_from_baseline_count": 3},
            "final_score": [1, 2, 3],
            "stage_telemetry": empty_stage_telemetry(),
        }
        stripped = strip_pr6_fields_for_parity(payload)
        self.assertEqual(stripped["placements"], [{"course": "CS101"}])
        self.assertEqual(stripped["decision_trace"], {"a": {"stage_origin": "sa"}})
        self.assertEqual(stripped["perturbation_metric"], {"changes_from_baseline_count": 3})
        self.assertEqual(stripped["final_score"], [1, 2, 3])

    def test_strip_handles_missing_stage_telemetry(self) -> None:
        payload = {"placements": []}
        stripped = strip_pr6_fields_for_parity(payload)
        self.assertEqual(stripped, {"placements": []})


class TestPR6AcceptanceCLI(SimpleTestCase):
    """``python manage.py pr6_telemetry_report`` surfaces every stage key
    and the current flag state. Text and JSON formats both expected."""

    def test_text_format_lists_all_stage_keys(self) -> None:
        buf = io.StringIO()
        call_command("pr6_telemetry_report", stdout=buf)
        out = buf.getvalue()
        for k in STAGE_KEYS:
            self.assertIn(k, out, f"stage key {k!r} missing from report")
        self.assertIn("Flag enabled", out)

    def test_json_format_parses_and_has_stage_keys(self) -> None:
        buf = io.StringIO()
        call_command("pr6_telemetry_report", "--format", "json", stdout=buf)
        payload = json.loads(buf.getvalue())
        self.assertEqual(set(payload["stage_keys"]), set(STAGE_KEYS))
        self.assertIsInstance(payload["flag_enabled"], bool)
        self.assertIn("rows", payload)


class TestStageIterationsMonotonic(SimpleTestCase):
    """Iteration counters are non-negative integers — no floats, no negatives."""

    def test_iterations_are_non_negative_ints(self) -> None:
        tel = empty_stage_telemetry()
        for k in STAGE_KEYS:
            self.assertIsInstance(tel["stage_iterations"][k], int)
            self.assertGreaterEqual(tel["stage_iterations"][k], 0)
            self.assertIsInstance(tel["stage_ms"][k], int)
            self.assertGreaterEqual(tel["stage_ms"][k], 0)

    def test_merge_preserves_non_negative_ints(self) -> None:
        a = empty_stage_telemetry()
        a["stage_ms"]["greedy"] = 7
        a["stage_iterations"]["greedy"] = 42
        b = empty_stage_telemetry()
        b["stage_ms"]["greedy"] = 3
        b["stage_iterations"]["greedy"] = 8
        out = merge_stage_telemetry(a, b)
        self.assertEqual(out["stage_ms"]["greedy"], 10)
        self.assertEqual(out["stage_iterations"]["greedy"], 50)
        self.assertIsInstance(out["stage_ms"]["greedy"], int)
