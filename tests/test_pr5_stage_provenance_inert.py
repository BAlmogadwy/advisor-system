"""PR5 provenance fields must not affect planner decisions.

``stage_origin`` and ``stage_context`` on ``DecisionTrace`` are
documented as **provenance-only** (PR5 DoR §Implementation cautions):
consumers may read them for auditing but no placement / rooming code
path is allowed to branch on their values.

This test asserts the rule by:

1. Running the planner on a PR5-style fixture and snapshotting the
   placements + final score.
2. Re-running the planner after monkey-patching the DecisionTrace
   class so ``stage_origin`` and ``stage_context`` fields always
   carry sentinel garbage.
3. Verifying placements and final score are byte-identical.

If the assertion ever fails, someone has wired a planner decision
onto a provenance field — the fix is to pull that decision back onto
the real signal (stage telemetry timing, ms counts, solver codes,
etc.) not to rewrite this test.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

from django.test import TransactionTestCase

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


class TestStageProvenanceIsInert(TransactionTestCase):
    def _run(self):
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_autoplace import auto_place_scenario

        scenario, _, _ = load_pr5_fixture("pr5_sa_relocate.json")
        return auto_place_scenario(scenario.id)

    def _placements(self, result) -> list:
        boards = result.get("boards") or {}
        out = []
        for label in sorted(boards.keys()):
            for p in (boards[label] or {}).get("placements") or []:
                out.append(
                    (
                        label,
                        p.get("course_code"),
                        p.get("section_code"),
                        p.get("day"),
                        p.get("start_time"),
                        p.get("end_time"),
                        p.get("room_code"),
                    )
                )
        return sorted(out)

    def _final_score(self, result) -> tuple:
        boards = result.get("boards") or {}
        return tuple(sorted((k, (b or {}).get("final_score")) for k, b in boards.items()))

    def test_placements_invariant_under_provenance_mutation(self) -> None:
        baseline = self._run()
        baseline_placements = self._placements(baseline)
        baseline_score = self._final_score(baseline)

        from core.services import timetable_decision_trace as tdt

        original_to_dict = tdt.DecisionTrace.to_dict

        def poisoned_to_dict(self):  # type: ignore[no-untyped-def]
            out = original_to_dict(self)
            out["stage_origin"] = "POISONED"
            out["stage_context"] = {"garbage": True, "mutated": 42}
            return out

        from core.models import Room, TimetableScenario

        Room.objects.all().delete()
        TimetableScenario.objects.all().delete()

        with patch.object(tdt.DecisionTrace, "to_dict", poisoned_to_dict):
            mutated = self._run()

        self.assertEqual(self._placements(mutated), baseline_placements)
        self.assertEqual(self._final_score(mutated), baseline_score)
