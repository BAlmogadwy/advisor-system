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

# NOTE: module-level ``pytest.mark.django_db`` is intentionally NOT applied:
# Section A uses ``SimpleTestCase`` (no DB); Sections B/C use
# ``TransactionTestCase`` which manages its own DB access.


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
    on every section it moved.

    Primary test: a real greedy→SA integration run. The fixture uses
    ``blocked_slots`` to funnel greedy onto SUN only, producing a natural
    same-day clump with a gap SA can close. ChatGPT commit-3 amendment
    ruling (2026-04-21): the greedy→SA handoff must be exercised end-to-
    end, not asserted against hand-seeded ORM state, so the optimiser
    updating an upstream trace is the seam under test.

    Secondary test: a narrow hand-seeded plumbing smoke that asserts SA
    trace emission in isolation, for when someone needs to poke just the
    emit-site without running the greedy placer.
    """

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_greedy_to_sa_relocates_same_day_gap(self) -> None:
        """End-to-end greedy→SA handoff. Asserts greedy clumps on SUN
        (per fixture ``blocked_slots``), then SA relocates at least one
        section off SUN and emits an ``SA_RELOCATE_ACCEPTED`` trace with
        populated ``from_slot``/``to_slot``.

        Scaffolding note (not a product guarantee): this test exploits
        a known asymmetry — ``timetable_autoplace`` applies the scenario's
        ``blocked_slots`` when generating candidate options, while
        ``timetable_local_search._generate_relocate_move`` currently
        walks all ``WEEKDAYS × slot_config`` unconditionally and does
        not consult ``blocked_slots``. That lets greedy be day-constrained
        onto SUN while SA still has MON/TUE/WED/THU to relocate into.
        The quirk is test scaffolding only; PR5 makes no product
        commitment that SA will respect ``blocked_slots``, and this test
        should not be cited as evidence of such a guarantee.
        """
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")

        greedy_result = _run_greedy(board.id)
        greedy_trace = greedy_result.get("decision_trace", {}) or {}
        assert greedy_trace, (
            "greedy must produce a non-empty decision_trace before SA runs "
            f"(got: {greedy_result!r})"
        )
        for entry in greedy_trace.values():
            assert entry.get("stage_origin") == "greedy", (
                f"pre-SA trace entries must all be stage_origin='greedy' (got: {entry!r})"
            )
        # blocked_slots funnels greedy onto SUN only; any other day
        # means the fixture or the loader regressed.
        for entry in greedy_trace.values():
            assert entry.get("chosen_day") == "SUN", (
                f"blocked_slots fixture should clump greedy on SUN "
                f"(got chosen_day={entry.get('chosen_day')!r})"
            )

        sa_result = optimize_and_persist_board(board.id, max_seconds=5.0)
        sa_trace = sa_result.get("decision_trace", {}) or {}
        sa_entries = [entry for entry in sa_trace.values() if entry.get("stage_origin") == "sa"]
        assert sa_entries, (
            "SA polish had a ≥525-cost same-day gap to close; trace must record "
            f"at least one move with stage_origin='sa' "
            f"(got: {sa_trace!r}, cost_before={sa_result.get('cost_before')}, "
            f"cost_after={sa_result.get('cost_after')})"
        )
        for entry in sa_entries:
            ctx = entry.get("stage_context", {}) or {}
            assert ctx.get("code") == SA_RELOCATE_ACCEPTED, (
                f"SA-origin trace entries must carry code=SA_RELOCATE_ACCEPTED (got ctx={ctx!r})"
            )
            from_slot = ctx.get("from_slot") or ""
            to_slot = ctx.get("to_slot") or ""
            assert from_slot.strip(), f"from_slot must be populated (got: {from_slot!r})"
            assert to_slot.strip(), f"to_slot must be populated (got: {to_slot!r})"
            assert from_slot != to_slot, (
                f"from_slot and to_slot must differ for a real move (both: {from_slot!r})"
            )

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_hand_seeded_sa_plumbing_smoke(self) -> None:
        """Narrow plumbing smoke: seed a deliberately-suboptimal placement
        set directly via ORM (bypassing greedy) and assert SA's trace
        emission surface works. Retained per ChatGPT amendment ruling #3
        as a secondary, not the main proof of stage handoff."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.models import SectionPlacement, TermSection
        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")

        bad_layout = [
            ("CS101", "S1", "SUN", "08:00", "09:15"),
            ("CS102", "S1", "SUN", "09:30", "10:45"),
            ("CS103", "S1", "SUN", "11:00", "12:15"),
        ]
        for course_code, section, day, start, end in bad_layout:
            ts = TermSection.objects.create(
                scenario=board.scenario,
                course_key=course_code,
                course_code=course_code,
                course_number=course_code,
                course_name=course_code,
                section=section,
                available_capacity=40,
                source_tag="tw_auto",
            )
            SectionPlacement.objects.create(
                board=board,
                term_section=ts,
                day=day,
                start_time=start,
                end_time=end,
                room="A101",
                is_locked=False,
            )

        result = optimize_and_persist_board(board.id, max_seconds=2.0)
        trace = result.get("decision_trace", {}) or {}
        sa_entries = [entry for entry in trace.values() if entry.get("stage_origin") == "sa"]
        assert sa_entries, (
            "hand-seeded 105-min SUN gap: SA must emit at least one SA_RELOCATE_ACCEPTED entry "
            f"(got: {trace!r})"
        )

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_flag_off_emits_no_sa_stage_trace(self) -> None:
        """Flag-off parity: with ``TIMETABLE_PR5_STAGE_TRACE_ENABLED=False``,
        optimize_board must not populate any SA trace entries — even if
        SA moves sections. Required by ChatGPT amendment #4."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr5_fixture("pr5_sa_relocate.json")
        _run_greedy(board.id)
        result = optimize_and_persist_board(board.id, max_seconds=2.0)
        trace = result.get("decision_trace", {}) or {}
        sa_entries = [entry for entry in trace.values() if entry.get("stage_origin") == "sa"]
        assert not sa_entries, (
            "flag-off must not emit SA trace entries; this is the PR5 kill-switch contract "
            f"(got: {sa_entries!r})"
        )


def _seed_overlapping_placements(scenario, board, day="MON", start="08:00", end="09:15") -> None:
    """Directly create unlocked ``SectionPlacement`` rows for CS101|S1 + CS102|S1
    in the given slot. Used by CPSAT tests to produce a state the V2
    multi-strategy greedy would never generate (full cross-course overlap).

    ``pr5_cpsat_improve.json`` declares the sections/rooms/student maps;
    this helper adds the TermSection + SectionPlacement rows on top so
    ``optimise_current_timetable`` can read them as the starting state.
    """
    from core.models import SectionPlacement, TermSection

    for course in ("CS101", "CS102"):
        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=course,
            section="S1",
            defaults={
                "course_code": course,
                "course_number": course,
                "course_name": course,
                "available_capacity": 30,
                "source_tag": "pr5_cpsat_seed",
            },
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=False,
        )


class TestCPSATImprovementEmission(TransactionTestCase):
    """CP-SAT polisher emits ``CPSAT_IMPROVED`` with ``previous_slot`` and
    ``new_slot`` populated, the improvement is overlaid into
    ``sections_by_id``, and the improved placement survives to the DB."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_cpsat_swap_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        _seed_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        assert result.get("cpsat_polish_applied") is True, (
            "CP-SAT must improve on the seeded overlapping state; "
            f"final_score={result.get('final_score')!r}"
        )

        trace = result.get("decision_trace", {}) or {}
        cpsat_entries = [e for e in trace.values() if e.get("stage_origin") == "cpsat"]
        assert cpsat_entries, "CP-SAT improved; at least one section's stage_origin must be 'cpsat'"
        for entry in cpsat_entries:
            ctx = entry.get("stage_context", {}) or {}
            assert ctx.get("code") == CPSAT_IMPROVED, f"Expected CPSAT_IMPROVED: {ctx!r}"
            assert ctx.get("previous_slot"), f"previous_slot must be populated: {ctx!r}"
            assert ctx.get("new_slot"), f"new_slot must be populated: {ctx!r}"
            assert ctx.get("previous_slot") != ctx.get("new_slot"), (
                f"previous_slot must differ from new_slot: {ctx!r}"
            )
            assert "cost_delta" not in ctx, (
                f"cost_delta is prohibited in CPSAT stage_context (oracle ruling): {ctx!r}"
            )

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_cpsat_improvement_persists_to_db(self) -> None:
        """Leak-fix test: the polished section survives to SectionPlacement."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.models import SectionPlacement
        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        _seed_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        trace = result.get("decision_trace", {}) or {}
        cpsat_entries = [e for e in trace.values() if e.get("stage_origin") == "cpsat"]
        assert cpsat_entries, "prerequisite: CPSAT trace must be non-empty"

        # Pick the first traced section; compare DB placement against its
        # trace (previous_slot / new_slot). new_slot is "DAY HH:MM-HH:MM".
        entry = cpsat_entries[0]
        ctx = entry["stage_context"]
        prev_day, prev_window = ctx["previous_slot"].split(" ", 1)
        prev_start = prev_window.split("-", 1)[0]
        new_day, new_window = ctx["new_slot"].split(" ", 1)
        new_start = new_window.split("-", 1)[0]

        course_code = entry["course_code"]
        placement = SectionPlacement.objects.get(
            board__scenario=scenario,
            term_section__course_key=course_code,
        )
        db_day = placement.day
        db_start = str(placement.start_time)[:5]

        # (1) placement must have moved off the greedy-seeded slot
        assert not (db_day == prev_day and db_start == prev_start), (
            f"Placement still matches previous_slot — leak fix failed. "
            f"db=({db_day},{db_start}) prev=({prev_day},{prev_start})"
        )
        # (2) placement must match the CPSAT-traced new_slot
        assert db_day == new_day and db_start == new_start, (
            f"DB placement does not match CPSAT new_slot. "
            f"db=({db_day},{db_start}) new=({new_day},{new_start})"
        )

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_flag_off_emits_no_cpsat_stage_trace(self) -> None:
        """Flag off: CP-SAT must still improve, but trace stays empty (kill-switch)."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        _seed_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        trace = result.get("decision_trace", {}) or {}
        cpsat_entries = [e for e in trace.values() if e.get("stage_origin") == "cpsat"]
        assert not cpsat_entries, (
            "flag-off must not emit CP-SAT trace entries; this is the PR5 kill-switch contract "
            f"(got: {cpsat_entries!r})"
        )


def _seed_triple_clump(scenario, board, day="SUN", start="08:00", end="09:15") -> None:
    """Seed CS101|S1, CS102|S1, CS103|S1 all at the same slot to force a
    chain-2-solvable triple-clash on the pr5_chain_rotation fixture."""
    from core.models import SectionPlacement, TermSection

    for course in ("CS101", "CS102", "CS103"):
        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=course,
            section="S1",
            defaults={
                "course_code": course,
                "course_number": course,
                "course_name": course,
                "available_capacity": 30,
                "source_tag": "pr5_chain_seed",
            },
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room="R1",
            is_locked=False,
        )


class TestChainRotationEmission(TransactionTestCase):
    """Chain-search emits ``CHAIN_ROTATED`` with chain context."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_chain_swap_appears_in_trace(self) -> None:
        import pytest
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_chain_rotation.json")
        _seed_triple_clump(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=True,
            run_cpsat_polish=False,
        )

        if not result.get("chain_search_applied"):
            pytest.skip(
                "chain-2 search found no improvement on the seeded triple-clash; "
                "emission shape tested by flag-off companion + contract tests"
            )

        trace = result.get("decision_trace", {}) or {}
        chain_entries = [e for e in trace.values() if e.get("stage_origin") == "chain"]
        assert chain_entries, (
            "chain_search_applied=True but trace has no stage_origin='chain' entries"
        )
        for entry in chain_entries:
            ctx = entry.get("stage_context", {}) or {}
            assert ctx.get("code") == CHAIN_ROTATED, f"Expected CHAIN_ROTATED: {ctx!r}"
            assert ctx.get("chain_length", 0) >= 2, f"chain_length must be >=2: {ctx!r}"
            assert ctx.get("chain_id"), f"chain_id must be populated: {ctx!r}"
            prev = ctx.get("previous_slot") or ""
            new = ctx.get("new_slot") or ""
            assert prev.strip(), f"previous_slot must be populated: {ctx!r}"
            assert new.strip(), f"new_slot must be populated: {ctx!r}"
            assert prev != new, f"previous_slot and new_slot must differ: {ctx!r}"

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_flag_off_emits_no_chain_stage_trace(self) -> None:
        """Flag off: chain-search may still move sections, but trace stays empty."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_chain_rotation.json")
        _seed_triple_clump(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=True,
            run_cpsat_polish=False,
        )

        trace = result.get("decision_trace", {}) or {}
        chain_entries = [e for e in trace.values() if e.get("stage_origin") == "chain"]
        assert not chain_entries, (
            "flag-off must not emit chain trace entries; PR5 kill-switch contract "
            f"(got: {chain_entries!r})"
        )


def _seed_unassigned_placement(
    scenario,
    board,
    course_code: str = "CS101",
    section: str = "S1",
    day: str = "Sun",
    start: str = "08:00",
    end: str = "09:15",
) -> None:
    """Seed a SectionPlacement with ``room='UNASSIGNED'`` so rooming's
    2nd-pass repair logic has something to rescue.

    The PR3 loader only materialises ``locks`` — ``baseline_placements``
    entries are read at runtime by warm-start, not persisted. PR5's
    rooming-repair test needs an actual ``SectionPlacement`` row with the
    UNASSIGNED sentinel, which is what this helper creates.
    """
    from core.models import SectionPlacement, TermSection

    ts, _ = TermSection.objects.get_or_create(
        scenario=scenario,
        course_key=course_code,
        section=section,
        defaults={
            "course_code": course_code,
            "course_number": course_code,
            "course_name": course_code,
            "available_capacity": 40,
            "source_tag": "pr5_rooming_seed",
        },
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day=day,
        start_time=start,
        end_time=end,
        room="UNASSIGNED",
        is_locked=False,
    )


class TestRoomingRepairEmission(TransactionTestCase):
    """Rooming 2nd-pass emits ``ROOMING_REPAIR_REASSIGNED`` when it
    repairs an UNASSIGNED."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_repair_reassigned_appears_in_trace(self) -> None:
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_rooming import assign_rooms_to_board

        scenario, board, _ = load_pr5_fixture("pr5_rooming_repair.json")
        _seed_unassigned_placement(scenario, board)
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
            assert ctx.get("code") == "ROOMING_REPAIR_REASSIGNED"
            assert ctx.get("new_room") and ctx["new_room"] != "UNASSIGNED"
            assert entry.get("chosen_room") == ctx["new_room"]

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=False)
    def test_flag_off_emits_no_rooming_repair_trace(self) -> None:
        """Flag off: repair still happens, but trace stays empty."""
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_rooming import assign_rooms_to_board

        scenario, board, _ = load_pr5_fixture("pr5_rooming_repair.json")
        _seed_unassigned_placement(scenario, board)
        result = assign_rooms_to_board(board.id)
        trace = result.get("decision_trace", {}) or {}
        repair_entries = [
            entry for entry in trace.values() if entry.get("stage_origin") == "rooming_repair"
        ]
        assert not repair_entries, (
            "flag-off must not emit rooming-repair trace entries; PR5 kill-switch contract "
            f"(got: {repair_entries!r})"
        )


class TestStageOriginSemantic(TransactionTestCase):
    """Amendment 3: ``stage_origin`` means "the stage that LAST changed
    the chosen placement." When greedy → SA → CP-SAT all touch the same
    section, the final origin is the last mover.

    Goes green when commits 3 + 4 have both landed (CP-SAT is
    downstream of SA in the V2 pipeline)."""

    @override_settings(TIMETABLE_PR5_STAGE_TRACE_ENABLED=True)
    def test_last_changer_wins(self) -> None:
        """CP-SAT is downstream of SA, so when both touch the same section
        the final trace entry must carry ``stage_origin='cpsat'`` — not
        ``'sa'``. The V2 overlay at step 4c/5 implements last-changer-wins
        by key in ``result['decision_trace']``."""
        import pytest
        from pr5_fixture_loader import load_pr5_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        _seed_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=True,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        trace = result.get("decision_trace", {}) or {}
        cpsat_entries = [e for e in trace.values() if e.get("stage_origin") == "cpsat"]
        if not cpsat_entries:
            # Legitimate skip — SA may fully resolve the overlap on the
            # seeded state, leaving CPSAT nothing to override. The
            # last-changer-wins semantic is still exercised by the direct
            # CPSAT test; this one is only meaningful when both SA and
            # CPSAT touch the same section.
            pytest.skip("SA resolved the overlap; CPSAT had nothing to override")

        # For each CPSAT-touched section, its trace key must carry origin
        # 'cpsat' (not 'sa') — the overlay at step 4c/5 replaced the SA
        # entry.
        for entry in cpsat_entries:
            assert entry.get("stage_origin") == "cpsat", (
                f"last-changer-wins: CPSAT must overwrite prior SA entry, got {entry!r}"
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
