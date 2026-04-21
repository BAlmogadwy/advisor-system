# PR6 — SCENARIO PACK

Seven fixtures exercise the stage-telemetry payload introduced in PR6.
Fixture grammar is identical to PR3/PR5 (sections, slot_pool, rooms,
blocked_slots, baseline_placements, locks), so `tests/pr6_fixture_loader.py`
delegates to `load_pr3_fixture`.

All fixtures live under
`snapshots/planner-refactor-2026-04-20/fixtures/pr6_*.json`.

---

## Fixture inventory

| # | Fixture | Exercises | Expected non-zero keys |
|---|---|---|---|
| 1 | `pr6_greedy_telemetry.json` | Vanilla greedy-only placement | `greedy.ms`, `greedy.iterations` |
| 2 | `pr6_sa_telemetry.json` | Same shape as `pr5_sa_relocate` — forces SA to relocate | `greedy.*`, `sa.*` |
| 3 | `pr6_cpsat_telemetry.json` | Same shape as `pr5_cpsat_improve` — CP-SAT polish runs | `greedy.*`, `cpsat.*` |
| 4 | `pr6_chain_telemetry.json` | Same shape as `pr5_chain_rotation` — chain pass triggers | `greedy.*`, `chain.*` |
| 5 | `pr6_rooming_repair_telemetry.json` | Same shape as `pr5_rooming_repair` — rooming-repair reassigns | `greedy.*`, `rooming_repair.*` |
| 6 | `pr6_flag_off_parity.json` | Any scenario — verifies flag-off zeroes all telemetry | (all zero) |
| 7 | `pr6_aggregation.json` | Multi-board scenario — scenario telemetry = sum of board telemetry | varies |

---

## Why these seven

- **Per-stage isolation (1–5)**: each fixture singles out one stage so the
  corresponding emission test can assert `stage.ms > 0` and
  `stage.iterations > 0` without cross-contamination from other stages.
- **Flag-off parity (6)**: pinned to verify the
  `strip_pr6_fields_for_parity` helper neutralises telemetry for PR5-era
  byte-equality comparisons.
- **Aggregation correctness (7)**: pinned to the PR6 DoR acceptance bar
  "scenario-level telemetry must equal the sum of board-level telemetry
  for the same run".

---

## Status

Commit 1 ships **stub fixtures** (scenario shape + `expected`
placeholders) to drive the failing-test skeleton. Commits 3–7 refine
each fixture's expected values as stage instrumentation lands.
