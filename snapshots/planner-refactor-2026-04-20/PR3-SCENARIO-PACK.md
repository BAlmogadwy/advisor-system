# PR3 — Scenario pack freeze

Track: `planner-refactor-q2-2026`
PR: PR3 — decision-trace + minimal-perturbation
DoR: [`docs/PR3-DOR.md`](../../docs/PR3-DOR.md) (signed off 2026-04-20 with seven amendments)

## Base

- `base_commit`: `fdcd691` — master after PR2 ships (room oracle + typed infeasibility reporting live; `TIMETABLE_PR2_ROOM_ORACLE_ENABLED` default True).
- Branch: `refactor/pr3-decision-trace-minimal-perturbation`.

## Flags

- `TIMETABLE_PR3_DECISION_TRACE_ENABLED` — default **True** from commit 2. Pure observation — zero planning-decision change. Disabling it produces `decision_trace={}` in the payload (schema stability).
- `TIMETABLE_PR3_WARM_START_ENABLED` — default **True** as of commit 8's promotion (10-fixture acceptance pack cleared all bars). Behavioural change — biases which slot gets chosen when a feasible baseline exists. Env kill-switch preserved: `TIMETABLE_PR3_WARM_START_ENABLED=false` reverts to cold-start without a redeploy. See `docs/PR3-PROMOTION-NOTE.md`.

The split exists because trace capture is safe to default-on immediately (observational), but warm-start is a placement-decision change and rolls out separately so a warm-start regression can't force the trace off.

## Rejection-code alphabet

Trace entries MUST reuse existing code sentinels — no invented vague labels.

| Source PR | Code |
|---|---|
| PR1 | `PRAYER_WINDOW_CLASH`, `LOCK_VIOLATION` |
| PR2 | `NO_ROOM_CAPACITY`, `NO_ROOM_GENDER`, `NO_ROOM_TYPE`, `ROOM_OCCUPIED`, `ROOM_BUFFER_REJECT`, `ROOM_HEURISTIC_MISMATCH` |
| **PR3 (new)** | `INSTRUCTOR_CLASH`, `STUDENT_CONFLICT` |

`INSTRUCTOR_CLASH` — instructor already teaches another section at this slot.
`STUDENT_CONFLICT` — ≥1 student enrolled in another section already at this slot. Named *conflict* (not *overlap*) because this is cohort semantics, not geometric time-overlap.

## Trace-capture scope

| Stage | Capture |
|---|---|
| `auto_place_board` (greedy) | **MUST** — chosen + up to 3 alternatives per placed section |
| V2 local-search — accepted moves | **MAY** — best-effort append/update |
| V2 chain-search | **SHOULD NOT** |
| V2 CP-SAT polish | **SHOULD NOT** |
| V2 ranking / generation | **SHOULD NOT** |

Alternative count is **fixed at 3 max** — not configurable.

## Warm-start semantics

Framing: **retention from baseline**, not scoring bypass. `baseline_placements` is caller-supplied / in-memory only (no DB persistence in PR3).

Priority order for a section's final slot:
1. PR1 locks — hard, always win.
2. PR3 warm-start retention — if baseline is still feasible.
3. Cold-start scoring — if no baseline or baseline infeasible.

When a baseline becomes infeasible, its own rejection reason (e.g. `PRAYER_WINDOW_CLASH`) is recorded as an alternative in the moved section's trace, so a registrar can see *why* the previous placement no longer works.

## Fixtures (1:1 with tests)

| # | Fixture | Test | Turns green at |
|---|---|---|---|
| 1 | — (shape unit test, no fixture) | `TestDecisionTraceShape::test_to_dict_roundtrip` | commit 2 |
| 2 | `pr3_cold_start_trace.json` | `TestColdStartCapture::test_every_placed_section_has_alternatives` | commit 3 |
| 3 | `pr3_warm_start_feasible.json` | `TestFeasibleRetention::test_all_feasible_baselines_retained` | commit 5 |
| 4 | `pr3_warm_start_infeasible_fallback.json` | `TestInfeasibleFallback::test_prayer_clash_baseline_falls_back` | commit 5 |
| 5 | `pr3_warm_start_lock_wins.json` | `TestLockBeatsBaseline::test_lock_wins_over_warm_start_preference` | commit 5 |
| 6 | `pr3_instructor_clash.json` | `TestTypedRejectionCodes::test_instructor_clash_surfaces_in_trace` | commit 3 |
| 7 | `pr3_student_conflict.json` | `TestTypedRejectionCodes::test_student_conflict_surfaces_in_trace` | commit 3 |
| 8 | `pr3_perturbation_totals.json` | `TestPerturbationMetric::test_mixed_outcome_totals_match` | commit 6 |
| 9 | `pr3_cold_start_parity.json` | `TestColdStartParity::test_baseline_none_matches_pr2_baseline` | commit 5 (parity test fires earliest) |
| 10 | `pr3_trace_schema_disabled.json` | `TestSchemaStability::test_trace_key_present_even_when_disabled` | commit 3 |
| 11 | `pr3_canonical_warm_start.json` | `TestCanonicalWarmStartFixture::test_exact_zero_invariant` | commit 5 |

## Progression of passing-ness

- **Commit 1** (this one): all tests fail at collection with `ModuleNotFoundError` because `core.services.timetable_decision_trace` doesn't exist yet. That's the tripwire — the public API shape is pinned in tests BEFORE any implementation lands so commit 2 can't silently rename symbols.
- **Commit 2** (dataclasses + sentinels + flag helper): Section A (shape tests) turns green.
- **Commit 3** (trace capture in `auto_place_board`): fixture #2, #6, #7, #10 tests turn green.
- **Commit 4** (V2 local-search trace): fixtures already green stay green; no new test moves.
- **Commit 5** (warm-start logic): fixtures #3, #4, #5, #9, #11 turn green.
- **Commit 6** (perturbation-metric wiring through V2): fixture #8 turns green.
- **Commit 7** (`explain_timetable` management command): mgmt-command tests (in `test_pr3_explain_timetable.py`, added in commit 7) turn green.
- **Commit 8** (promotion + flag flip): `test_flag_defaults_on_after_promotion` updated to match the flipped default.

## Acceptance bar reminder

1. Trace coverage ≥90% on the scenario pack — asserted in `test_pr3_warm_start.py::TestAcceptanceBar::test_trace_coverage_on_pack`.
2. Rejection codes map to known sentinels only — asserted per-trace in each fixture-level test.
3. Cold-start parity with PR2 — asserted on fixture #9.
4. Warm-start minimal-change: exact-zero on canonical fixture (#11), low-and-explainable on broader pack.
5. Feasible-rate ≥99% of PR2 baseline.
6. Wallclock p95 ≤1.3× PR2 baseline — measured by the PR2 perf harness extended with a warm-start case around commit 6–7.
