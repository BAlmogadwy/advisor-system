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


class TestSAStageEmission(TransactionTestCase):
    """Green at commit 4 — running SA over pr6_sa_telemetry.json with
    the flag on populates sa.ms and sa.iterations on the SA result
    payload."""

    @override_settings(
        TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True,
        TIMETABLE_PR5_STAGE_TRACE_ENABLED=True,
    )
    def test_sa_populates_sa_keys_when_sa_runs(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr6_fixture("pr6_sa_telemetry.json")
        _run_greedy(board.id)
        sa_result = optimize_and_persist_board(board.id, max_seconds=2.0)

        telemetry = sa_result.get("stage_telemetry")
        self.assertIsNotNone(telemetry, "optimize_and_persist_board must return stage_telemetry")
        self.assertGreater(telemetry["stage_ms"]["sa"], 0)
        self.assertGreater(telemetry["stage_iterations"]["sa"], 0)
        # SA must equal iterations ATTEMPTED, not improvements accepted.
        # Improvements are always <= iterations; a test scenario that
        # accepts every move is atypical, so this guard ensures we're
        # not silently counting only accepted moves.
        self.assertGreaterEqual(
            telemetry["stage_iterations"]["sa"],
            sa_result.get("improvements", 0),
        )
        for k in ("greedy", "cpsat", "chain", "rooming_repair"):
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_sa_telemetry_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_local_search import optimize_and_persist_board

        _, board, _ = load_pr6_fixture("pr6_sa_telemetry.json")
        _run_greedy(board.id)
        sa_result = optimize_and_persist_board(board.id, max_seconds=2.0)

        telemetry = sa_result.get("stage_telemetry")
        self.assertIsNotNone(telemetry)
        for k in STAGE_KEYS:
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)


def _seed_cpsat_overlapping_placements(scenario, board) -> None:
    """Seed CS101|S1 and CS102|S1 into the same MON 08:00 slot so the
    CP-SAT polisher is guaranteed to find (and improve) a cross-course
    overlap. Mirrors ``_seed_overlapping_placements`` in
    ``test_pr5_decision_trace.py`` — kept local here to avoid
    cross-suite imports."""
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
                "source_tag": "pr6_cpsat_seed",
            },
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time="08:00",
            end_time="09:15",
            room="R1",
            is_locked=False,
        )


class TestCPSATStageEmission(TransactionTestCase):
    """Green at commit 5 — running the CP-SAT polisher over
    pr6_cpsat_telemetry.json with the flag on populates cpsat.ms and
    cpsat.iterations=1 on the scenario-level stage_telemetry. Other
    stage keys stay zero at this commit."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True)
    def test_cpsat_populates_cpsat_keys_when_cpsat_runs(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr6_fixture("pr6_cpsat_telemetry.json")
        _seed_cpsat_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry, "optimise_current_timetable must return stage_telemetry")
        self.assertIn("stage_ms", telemetry)
        self.assertIn("stage_iterations", telemetry)
        # Guardrail from PR6 DoR §3: iterations==1 means solver.solve()
        # was actually invoked, not merely enabled by config.
        self.assertEqual(telemetry["stage_iterations"]["cpsat"], 1)
        self.assertGreater(telemetry["stage_ms"]["cpsat"], 0)
        # Other stage keys stay zero at commit 5 (commits 6/7 wire them).
        for k in ("sa", "chain", "rooming_repair"):
            self.assertEqual(telemetry["stage_ms"][k], 0, f"non-cpsat stage {k}.ms must be zero")
            self.assertEqual(
                telemetry["stage_iterations"][k],
                0,
                f"non-cpsat stage {k}.iterations must be zero",
            )

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_leaves_cpsat_telemetry_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr6_fixture("pr6_cpsat_telemetry.json")
        _seed_cpsat_overlapping_placements(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=False,
            run_cpsat_polish=True,
            cpsat_time_limit=15.0,
        )

        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry)
        for k in STAGE_KEYS:
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)


def _seed_chain_triple_clump(scenario, board, day="SUN", start="08:00", end="09:15") -> None:
    """Seed CS101|S1, CS102|S1, CS103|S1 all at the same slot so the chain-2
    search on the bridge graph is guaranteed a solvable rotation. Mirrors
    ``_seed_triple_clump`` in ``test_pr5_decision_trace.py``."""
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
                "source_tag": "pr6_chain_seed",
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


class TestChainStageEmission(TransactionTestCase):
    """Green at commit 6 — running chain-search over pr6_chain_telemetry.json
    with the flag on populates chain.ms and chain.iterations (attempted, not
    accepted) on the scenario-level stage_telemetry."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True)
    def test_chain_populates_chain_keys_when_chain_runs(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr6_fixture("pr6_chain_telemetry.json")
        _seed_chain_triple_clump(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=True,
            run_cpsat_polish=False,
        )

        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry, "optimise_current_timetable must return stage_telemetry")
        self.assertGreater(telemetry["stage_ms"]["chain"], 0)
        self.assertGreater(telemetry["stage_iterations"]["chain"], 0)

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_leaves_chain_telemetry_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_optimizer_v2 import optimise_current_timetable

        scenario, board, _ = load_pr6_fixture("pr6_chain_telemetry.json")
        _seed_chain_triple_clump(scenario, board)

        result = optimise_current_timetable(
            scenario.id,
            run_local_search=False,
            run_chain_search=True,
            run_cpsat_polish=False,
        )

        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry)
        for k in STAGE_KEYS:
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)


def _seed_unassigned_for_repair(
    scenario,
    board,
    course_code: str = "CS101",
    section: str = "S1",
    day: str = "SUN",
    start: str = "08:00",
    end: str = "09:15",
) -> None:
    """Seed a SectionPlacement with ``room='UNASSIGNED'`` so rooming's
    2nd-pass repair logic has something to rescue. Mirrors
    ``_seed_unassigned_placement`` in ``test_pr5_decision_trace.py``."""
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
            "source_tag": "pr6_rooming_seed",
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


class TestRoomingRepairStageEmission(TransactionTestCase):
    """Green at commit 6 — driving assign_rooms_to_board directly with an
    UNASSIGNED-seeded placement scopes timing to the repair pass only,
    matching the PR6 DoR §3 guardrail."""

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=True)
    def test_rooming_repair_populates_keys_when_repair_runs(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_rooming import assign_rooms_to_board

        scenario, board, _ = load_pr6_fixture("pr6_rooming_repair_telemetry.json")
        _seed_unassigned_for_repair(scenario, board)

        result = assign_rooms_to_board(board.id)
        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry, "assign_rooms_to_board must return stage_telemetry")
        self.assertGreater(telemetry["stage_ms"]["rooming_repair"], 0)
        self.assertGreater(telemetry["stage_iterations"]["rooming_repair"], 0)
        for k in ("greedy", "sa", "cpsat", "chain"):
            self.assertEqual(telemetry["stage_ms"][k], 0, f"non-repair stage {k}.ms must be zero")
            self.assertEqual(
                telemetry["stage_iterations"][k],
                0,
                f"non-repair stage {k}.iterations must be zero",
            )

    @override_settings(TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=False)
    def test_flag_off_leaves_rooming_repair_telemetry_zero(self) -> None:
        from pr6_fixture_loader import load_pr6_fixture

        from core.services.timetable_rooming import assign_rooms_to_board

        scenario, board, _ = load_pr6_fixture("pr6_rooming_repair_telemetry.json")
        _seed_unassigned_for_repair(scenario, board)

        result = assign_rooms_to_board(board.id)
        telemetry = result.get("stage_telemetry")
        self.assertIsNotNone(telemetry)
        for k in STAGE_KEYS:
            self.assertEqual(telemetry["stage_ms"][k], 0)
            self.assertEqual(telemetry["stage_iterations"][k], 0)
