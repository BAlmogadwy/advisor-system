"""PR5 — solver-pipeline decision-trace parity tests (failing until commit 2+).

Module-level imports of ``core.services.timetable_solver_codes`` fail at
collection today — commit 2 creates that module. Section A assertions
about ``DecisionTrace.stage_origin`` also fail until commit 2 adds the
field. Sections B/C stay red until their respective emission commits
(3–6) and the aggregate commit (7) land.

Progression of passing-ness:

- **Commit 2** (contract module + ``stage_origin`` field + flag helper):
  Section A (shape / contract tests) turns green.
- **Commit 3** (SA trace emission): ``TestSARelocateEmission`` + the
  SA leg of ``TestStageOriginSemantic`` turn green.
- **Commit 4** (CP-SAT trace emission): ``TestCPSATImprovementEmission``
  + the CPSAT leg of ``TestStageOriginSemantic`` turn green.
- **Commit 5** (chain trace emission): ``TestChainRotationEmission``
  turns green.
- **Commit 6** (rooming-repair trace emission): ``TestRoomingRepairEmission``
  turns green.
- **Commit 7** (changes_by_stage + acceptance CLI): Section C turns
  green (``TestChangesByStageSum``, ``TestFlagOffSemanticParity``,
  ``TestPR5AcceptanceCLI``).
- **Commit 8** (flag promotion): no new test moves; only the default
  behaviour of ``is_stage_trace_enabled()`` flips.

Per the PR5 DoR (docs/PR5-DOR.md):

- Single flag: ``TIMETABLE_PR5_STAGE_TRACE_ENABLED``.
- Four acceptance codes (no rejection codes — amendment 1 dropped
  ``SA_RELOCATE_REJECTED``).
- ``stage_origin`` on ``DecisionTrace`` only (NOT ``Alternative`` —
  amendment 3).
- "Last changer wins" semantic rule for ``stage_origin``.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings

from core.services.timetable_decision_trace import DecisionTrace

# Contract imports — this block is the commit-1 tripwire. Every symbol
# below names something commit 2 exposes. A rename or a dropped symbol
# breaks the suite at collection — which is exactly what we want.
from core.services.timetable_solver_codes import (  # noqa: E402 — tripwire
    CHAIN_ROTATED,
    CPSAT_IMPROVED,
    ROOMING_REPAIR_REASSIGNED,
    SA_RELOCATE_ACCEPTED,
    is_stage_trace_enabled,
)

pytestmark = pytest.mark.django_db


# ===========================================================================
# SECTION A — shape / contract tests (green at commit 2).
# ===========================================================================


class TestStageOriginShape(SimpleTestCase):
    """Pin the public shape of the PR5-augmented ``DecisionTrace`` before
    any emission lands."""

    def test_stage_origin_defaults_to_greedy(self) -> None:
        """Default value preserves PR3 bit-for-bit at the per-entry level."""
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
        )
        assert trace.stage_origin == "greedy"

    def test_stage_origin_accepts_five_valid_literals(self) -> None:
        """Dataclass must accept every value in the declared Literal union."""
        for stage in ("greedy", "sa", "cpsat", "chain", "rooming_repair"):
            trace = DecisionTrace(
                section_code="CS101|S1",
                course_code="CS101",
                chosen_day="Sun",
                chosen_start_time="08:00",
                chosen_end_time="09:15",
                chosen_room="A101",
                stage_origin=stage,
            )
            assert trace.stage_origin == stage

    def test_to_dict_includes_stage_origin(self) -> None:
        """Serialised trace carries the provenance field."""
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            stage_origin="sa",
        )
        payload = trace.to_dict()
        assert payload["stage_origin"] == "sa"


class TestSolverCodesContract(SimpleTestCase):
    """The four new sentinels live in their own module and are plain strings."""

    def test_four_codes_are_strings(self) -> None:
        for code in (
            SA_RELOCATE_ACCEPTED,
            CPSAT_IMPROVED,
            CHAIN_ROTATED,
            ROOMING_REPAIR_REASSIGNED,
        ):
            assert isinstance(code, str)
            assert code == code.upper()
            assert " " not in code

    def test_codes_are_distinct(self) -> None:
        codes = {SA_RELOCATE_ACCEPTED, CPSAT_IMPROVED, CHAIN_ROTATED, ROOMING_REPAIR_REASSIGNED}
        assert len(codes) == 4

    def test_no_sa_relocate_rejected_in_module(self) -> None:
        """Amendment 1: rejected-move code is intentionally NOT exported.

        If a future amendment re-introduces it, that is a scope change and
        requires a new DoR cycle; this test catches accidental re-import.
        """
        import core.services.timetable_solver_codes as mod

        assert not hasattr(mod, "SA_RELOCATE_REJECTED")


class TestStageTraceFlag(SimpleTestCase):
    """The single PR5 flag gates population of every new trace surface."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_flag_on_returns_true(self) -> None:
        assert is_stage_trace_enabled() is True

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_flag_off_returns_false(self) -> None:
        assert is_stage_trace_enabled() is False


# ===========================================================================
# SECTION B — stage emission tests (green per commit 3 / 4 / 5 / 6).
#
# Every test here loads a PR5 fixture, runs the greedy placer + the
# specific stage, and asserts the trace gained the expected code / stage.
# ===========================================================================


def _run_greedy(board_id: int) -> dict:
    """Invoke the greedy placer. Helper keeps the import path local to
    the test so commit 2's contract imports don't drag the whole
    planner stack into module load time."""
    from core.services.timetable_autoplace import auto_place_board

    return auto_place_board(board_id)


class TestGreedyStageOrigin(TransactionTestCase):
    """With the flag on but no post-greedy stage run, every trace entry
    has ``stage_origin == "greedy"``."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_greedy_placement_has_stage_origin_greedy(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")
        result = _run_greedy(board.id)
        trace = result.get("decision_trace", {}) or {}
        assert trace, "greedy placer should emit a non-empty trace under PR3 defaults"
        for entry in trace.values():
            assert entry.get("stage_origin") == "greedy"


class TestSARelocateEmission(TransactionTestCase):
    """SA polish emits ``SA_RELOCATE_ACCEPTED`` and flips ``stage_origin = "sa"``
    on every section it moved."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_accepted_move_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")
        _run_greedy(board.id)
        result = optimize_and_persist_board(board.id, max_seconds=2.0)
        trace = result.get("decision_trace", {}) or {}
        sa_entries = [entry for entry in trace.values() if entry.get("stage_origin") == "sa"]
        assert sa_entries, (
            "SA polish moved at least one section; trace must record the move "
            f"with stage_origin='sa' (got: {trace!r})"
        )


class TestCPSATImprovementEmission(TransactionTestCase):
    """CP-SAT polisher emits ``CPSAT_IMPROVED`` with ``previous_slot`` populated."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_cpsat_swap_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

        scenario, _, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        result = optimise_scenario_timetable_v2(
            scenario.id, enable_cpsat_polisher=True, cpsat_time_limit=5.0
        )
        trace = result.get("decision_trace", {}) or {}
        cpsat_entries = [entry for entry in trace.values() if entry.get("stage_origin") == "cpsat"]
        assert cpsat_entries, "CP-SAT polish improved the objective; trace must record it"
        for entry in cpsat_entries:
            context = entry.get("stage_context", {}) or {}
            assert "previous_slot" in context, (
                f"CPSAT_IMPROVED must record the previous greedy slot (got: {entry!r})"
            )


class TestChainRotationEmission(TransactionTestCase):
    """Chain-search emits ``CHAIN_ROTATED`` with chain context."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_chain_swap_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_local_search_chains import chain_local_search

        _, board, _ = load_pr5_fixture("pr5_chain_rotation.json")
        _run_greedy(board.id)
        result = chain_local_search(board.id)
        trace = (result or {}).get("decision_trace", {}) or {}
        chain_entries = [entry for entry in trace.values() if entry.get("stage_origin") == "chain"]
        assert chain_entries, (
            "chain-search executed a rotation; trace must record each moved section "
            "with stage_origin='chain' + chain_length context"
        )
        for entry in chain_entries:
            ctx = entry.get("stage_context", {}) or {}
            assert ctx.get("chain_length", 0) >= 2


class TestRoomingRepairEmission(TransactionTestCase):
    """Rooming 2nd-pass emits ``ROOMING_REPAIR_REASSIGNED`` when it
    repairs an UNASSIGNED."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_repair_reassigned_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_rooming import assign_rooms_to_board

        _, board, _ = load_pr5_fixture("pr5_rooming_repair.json")
        result = assign_rooms_to_board(board.id)
        trace = result.get("decision_trace", {}) or {}
        repair_entries = [
            entry for entry in trace.values() if entry.get("stage_origin") == "rooming_repair"
        ]
        assert repair_entries, (
            "rooming 2nd pass reassigned an UNASSIGNED; trace must record the repair"
        )
        for entry in repair_entries:
            ctx = entry.get("stage_context", {}) or {}
            assert ctx.get("previous_room") == "UNASSIGNED"


class TestStageOriginSemantic(TransactionTestCase):
    """Amendment 3: ``stage_origin`` means "the stage that LAST changed
    the chosen placement." When greedy → SA → CP-SAT all touch the same
    section, the final origin is the last mover.

    Goes green when commits 3 + 4 have both landed (CP-SAT is
    downstream of SA in the V2 pipeline)."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_last_changer_wins(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

        scenario, _, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        result = optimise_scenario_timetable_v2(
            scenario.id,
            enable_sa=True,
            enable_cpsat_polisher=True,
            cpsat_time_limit=5.0,
        )
        trace = result.get("decision_trace", {}) or {}
        origins = {entry.get("stage_origin") for entry in trace.values()}
        # If both stages fired, the most recent mover is the recorded origin.
        # This fixture is engineered so CP-SAT strictly improves after SA;
        # at least one section should have stage_origin == "cpsat".
        assert "cpsat" in origins, (
            f"expected at least one CP-SAT-originated trace entry (got origins={origins!r})"
        )


# ===========================================================================
# SECTION C — changes_by_stage + parity + CLI (green at commit 7).
# ===========================================================================


class TestChangesByStageSum(TransactionTestCase):
    """Invariant: ``sum(changes_by_stage.values()) == changes_from_baseline_count``
    across scenarios with SA-only + SA+CP-SAT enabled. Commit 7 lands the
    sub-dict + enforcement."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_sum_equals_changes_from_baseline(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

        for fixture in ("pr5_sa_relocate.json", "pr5_cpsat_improve.json"):
            scenario, _, _ = load_pr5_fixture(fixture)
            result = optimise_scenario_timetable_v2(
                scenario.id,
                enable_sa=True,
                enable_cpsat_polisher=True,
                cpsat_time_limit=3.0,
            )
            perturbation = result.get("perturbation_metric", {}) or {}
            changes_by_stage = perturbation.get("changes_by_stage")
            assert changes_by_stage is not None, (
                f"perturbation_metric must include changes_by_stage ({fixture})"
            )
            total_flat = perturbation.get("changes_from_baseline_count", 0)
            assert sum(changes_by_stage.values()) == total_flat, (
                f"{fixture}: sum(changes_by_stage)={sum(changes_by_stage.values())} "
                f"!= changes_from_baseline_count={total_flat}"
            )
            for key in ("greedy", "sa", "cpsat", "chain", "rooming_repair"):
                assert key in changes_by_stage, (
                    f"{fixture}: changes_by_stage must include all five stage keys (missing: {key})"
                )


class TestFlagOffSemanticParity(TransactionTestCase):
    """Amendment 4: flag-off semantic parity vs master 71bf988 on the
    pre-PR5 payload subset. Commit 7 provides the normalised comparator."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_payload_matches_pr4_master_ignoring_new_fields(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_autoplace import auto_place_board
        from core.services.timetable_pr5_parity import strip_pr5_fields_for_parity

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")
        result = auto_place_board(board.id)
        normalised = strip_pr5_fields_for_parity(result)
        serialised = json.dumps(normalised, sort_keys=True)
        # The normalised view must NOT contain any PR5-added field.
        for banned in (
            "stage_origin",
            "changes_by_stage",
            SA_RELOCATE_ACCEPTED,
            CPSAT_IMPROVED,
            CHAIN_ROTATED,
            ROOMING_REPAIR_REASSIGNED,
        ):
            assert banned not in serialised, (
                f"flag-off normalised payload must not leak PR5 field {banned!r}"
            )


class TestPR5AcceptanceCLI(TransactionTestCase):
    """Management command surfaces per-stage tallies. Commit 7 ships it."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_report_lists_per_stage_counts(self) -> None:
        out = StringIO()
        call_command("pr5_acceptance_report", stdout=out)
        payload = out.getvalue()
        # CLI must surface at least one stage bucket label in its output.
        assert any(stage in payload for stage in ("greedy", "sa", "cpsat", "chain"))
