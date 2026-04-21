# PR6 — Promotion Note

**Branch:** `refactor/pr6-stage-telemetry`
**Base:** master @ `31f22dd` (post-PR5 solver-pipeline decision-trace parity)
**Promotion commit:** commit 8 (this commit)
**Theme:** Observability-only stage telemetry — "how long did each stage take, and how much work did it do".

---

## What shipped

PR6 adds wall-time and work-count counters for the five V2 pipeline stages (`greedy`, `sa`, `cpsat`, `chain`, `rooming_repair`) without ever affecting placement, rooming, or scoring decisions. The new payload key — `stage_telemetry` — is schema-stable (always present on every V2 exit path) and always carries both `stage_ms` and `stage_iterations` subdicts with all five stage keys.

Eight commits:

1. **DoR** — scope, flag plan, acceptance bars, guardrails (`docs/PR6-DOR.md`).
2. **Failing tests + telemetry helper module** — `core/services/timetable_stage_telemetry.py` with `empty_stage_telemetry`, `merge_stage_telemetry`, `record_stage_ms`, `record_stage_iterations`, `is_stage_telemetry_enabled`.
3. **Greedy stage instrumentation** — `auto_place_board` wraps its placement loop with a `perf_counter` fence; `greedy.iterations` = attempts counted (not successes).
4. **SA stage instrumentation** — `optimize_and_persist_board` wraps the SA pass; `sa.iterations` = attempts counted (not accepted moves).
5. **CP-SAT stage instrumentation** — `polish_scenario_with_cpsat` wraps `solver.solve()`; `cpsat.iterations == 1` only when the solver was actually invoked, not merely when polish was enabled by config.
6. **Chain + rooming_repair instrumentation** — `chain_local_search` outer loop (`chain.iterations` = iterations attempted, not accepted chains); `assign_rooms_to_board`'s repair pass scoped to `_repair_candidates > 0` so first-pass-only rooming leaves keys at zero; `rooming_repair.iterations` = reassignments count (matches `ROOMING_REPAIR_REASSIGNED` semantics).
7. **Scenario aggregation + parity + CLI + acceptance pack** — `auto_place_scenario` folds board-level `stage_telemetry` via `merge_stage_telemetry`; `optimise_scenario_timetable_v2` absorbs greedy telemetry from that scenario sum; `core/services/timetable_pr6_parity.py` with `strip_pr6_fields_for_parity`; `pr6_telemetry_report` management command; `tests/test_pr6_acceptance_pack.py` asserts the aggregation, parity, CLI, and monotonic-int invariants.
8. **This commit — promotion.** Flip `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED` default to `True` and land this note.

---

## Feature flag — promoted in this commit

| Flag | Old default | New default | Env var | Kill-switch |
|---|---|---|---|---|
| `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED` | `False` | **`True`** | same name | Set to `false` → telemetry writes short-circuit, every stage key stays at `0`. Payload shape unchanged. |

The flag is read on every stage-boundary via `is_stage_telemetry_enabled()` (in `core/services/timetable_stage_telemetry.py`). Flipping the env var on Render and restarting the worker is a live kill-switch — no redeploy required.

---

## Rollback path (tiered, cheapest first)

1. **Env kill-switch** — set `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=false` on Render, restart worker. Traffic-free, no redeploy. Every stage key reverts to `0` at runtime; the `stage_telemetry` block itself stays present, so downstream consumers that check for its existence still work. Overhead also drops to effectively zero because the instrumentation short-circuits before any `perf_counter` calls.
2. **Revert commit 8 only** — restores default `False`. Use if the issue is specifically the default-on promotion (e.g. a downstream analytics pipeline not ready for the new `stage_telemetry` field) rather than the instrumentation logic itself. Re-deploy needed. All PR6 plumbing stays in place and can be re-enabled via the env var.
3. **Revert the PR6 merge** — drops the entire PR6 feature set. Use only if the telemetry logic itself is the regression source (e.g. unexpected wall-clock overhead, scenario aggregation defect that corrupts board-level data) — not just the default.

---

## PR6-specific caveats

- **`stage_telemetry` is observational only.** It MUST NOT affect placement decisions, rooming outcomes, scores, or feasibility in any consumer. The flag-off path takes the same placement decisions as the flag-on path, byte-for-byte, modulo the telemetry counters. Any downstream logic that reads `stage_telemetry` should be read-only (dashboards, ops reporting, perf regression detection); routing placement decisions through timing data is a category error.
- **`stage_ms == 0` means "did not run".** A stage whose flag is off, or whose pipeline branch short-circuited before the instrumented boundary, reports `0` for both `stage_ms` and `stage_iterations`.
- **`stage_ms == 1` may mean "ran but faster than timer resolution".** The rooming-repair instrumentation uses a `max(1, int(elapsed_s * 1000))` clamp so a sub-1 ms repair pass doesn't silently collapse into the "did not run" bucket. Readers interpreting telemetry should not assume `stage_ms == 1` implies exactly 1 ms of wall time — it is the bottom of the reported-as-nonzero range. The clamp convention is documented once in the module docstring (`core/services/timetable_stage_telemetry.py`).
- **Aggregation is a sum, not an average.** Scenario-level `stage_ms` and `stage_iterations` are the per-key sum of board-level counters. PR6 deliberately does not surface per-board percentiles or averages — that muddies the contract and nothing currently reads such a field.

---

## Flag-off parity

When comparing flag-off PR6 output against a pre-PR6 master snapshot (e.g. `31f22dd`), normalise with `core/services/timetable_pr6_parity.strip_pr6_fields_for_parity` first. PR6 adds exactly one top-level key — `stage_telemetry` — and the parity helper drops it. No nested PR6 fields exist, so nothing else needs scrubbing.

---

## Acceptance

- PR6 stage-telemetry suite (`tests/test_pr6_stage_telemetry.py`) — 17/17 passing. Contract tests (shape, helpers, flag) + per-stage flag-on/flag-off pairs for all five stages.
- PR6 acceptance pack (`tests/test_pr6_acceptance_pack.py`) — 9/9 passing. Scenario aggregation, flag-off zero telemetry, parity helper semantics, CLI schema surface, monotonic-int invariants.
- PR5 decision-trace regression (`tests/test_pr5_decision_trace.py`) — 43 passing / 1 legitimate skip (unchanged from pre-PR6). PR5 acceptance codes + flag-off parity intact.
- PR3 acceptance pack (`tests/test_pr3_acceptance_pack.py`) — 21/21 stable.
- Pre-commit hooks pass (ruff clean; mypy + bandit skipped per the usual `SKIP=mypy,bandit` env).

---

## Known non-goals (out of scope for PR6, carry to PR7 or later)

- **Per-iteration timing arrays.** PR6 reports stage-boundary wall time only. A per-iteration trace is a separate observability module; it would inflate payload size materially and has no current consumer.
- **Rejection-cost telemetry.** Counts of pruned/rejected moves could be useful for diagnosing SA plateaus, but the acceptance-codes surface lives in PR5's trace, not PR6's counters. Adding a `rejections` counter to each stage is cleanly additive when a consumer needs it.
- **Percentile roll-ups.** Scenario aggregation is a sum. Percentiles / averages across runs are a registrar-facing analytics concern, not a payload concern.
- **CP-SAT solver internals.** `cpsat.iterations` is binary (`0` or `1`). Surfacing OR-Tools' internal search statistics (nodes, restarts, etc.) is a clean follow-up when someone wants to tune the polisher rather than just observe that it ran.

---

## Memory update

`MEMORY.md` under "Session Log" records the PR6 promotion:

- `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED` flipped to `True` default at commit 8.
- Env kill-switch live and verified.
- Sub-millisecond `max(1, ...)` clamp convention documented in the telemetry module.
- Test counts: 17 PR6 stage-telemetry + 9 PR6 acceptance pack (was 0 pre-PR6) + 43 PR5 regression (stable) + 21 PR3 acceptance (stable).
- Rollback doc: `docs/PR6-PROMOTION-NOTE.md` (this file).
