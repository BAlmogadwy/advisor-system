# PR5 — Definition of Ready

**Branch:** `refactor/pr5-solver-pipeline-trace-parity` (to be created)
**Base:** master @ `71bf988` (post-PR4 instructor realism + semantic cleanup)
**Theme:** Solver-pipeline decision-trace parity — record which stage (SA / CP-SAT / chain / rooming-repair) last changed the chosen placement recorded in `decision_trace`, the same way PR3 made greedy placements first-class.

This DoR is the pre-code agreement on scope, non-scope, acceptance bar, flag plan, and commit sequence. No code lands on this branch until ChatGPT signs off.

---

## Why this PR, why now

PR3 shipped `decision_trace` for every greedy placement: chosen slot + up to 3 scored alternatives + typed rejection reasons. PR4 extended the reason alphabet with `INSTRUCTOR_CLASH`. Together they close the explainability loop **for the greedy placer only**.

The V2 pipeline has four more stages that can change placements after the greedy pass:

1. **`timetable_local_search` (SA polish)** — relocate moves that swap a section into a different slot/room. Today: silently rewrites `SectionPlacement` rows. Trace says nothing.
2. **`timetable_cpsat_polisher` (CP-SAT improvement)** — constraint-solver that may re-place several sections at once for a better objective. Today: silently persists the improved solution via `persist_solver_result`. Trace says nothing.
3. **`timetable_chain_search` (chain-swap)** — multi-section rotations. Today: silent.
4. **`timetable_rooming` second pass** — repairs UNASSIGNED rooms under the capacity buffer. Today: records the rejection in `room_failures` but not in `decision_trace`.

Registrars reading `decision_trace` after a full Run see greedy's reasoning frozen at phase 1 — they cannot tell which placements were moved by SA, which swapped by CP-SAT, and whether a "changed" section in `perturbation_metric.changes_from_baseline_count` was changed by SA or by a re-run of greedy against different demand.

PR5 closes that gap in one branch because all four stages share the same two primitives: the `DecisionTrace` dataclass and the `Alternative` list. A single `stage_origin` field on `DecisionTrace` + a small set of new acceptance codes brings them under the same surface PR3 built.

---

## In scope

### 1. `stage_origin` on `DecisionTrace` (chosen-placement only)

`DecisionTrace` gains a `stage_origin: Literal["greedy", "sa", "cpsat", "chain", "rooming_repair"]` field. Default `"greedy"` preserves PR3 bit-for-bit at the individual-entry level: existing consumers that don't read the field stay working.

**Semantic rule (ChatGPT amendment 3).** `stage_origin` means "the stage that **last** changed the chosen placement currently recorded in this trace." Not "a stage that touched it at some point" — the final stage responsible for the current location.

**Not on `Alternative`.** `Alternative.stage_origin` is intentionally NOT added in PR5. The motivation per ChatGPT amendment 3: keep the PR5 contract smaller; alternatives are still greedy-era artefacts (scored candidates that were not chosen). If a future PR produces stage-origin alternatives (e.g. CP-SAT objectives recording displaced options), that PR extends `Alternative` then.

**Wire-through.** Stages 3–6 update `DecisionTrace.stage_origin` on the chosen entry when they move a section. Rooming-repair updates it when it reassigns a room without moving the slot.

**Schema stability.** Additive — existing keys unchanged. Old payloads stored as `result_json` under earlier PRs are forward-compatible because readers treat missing `stage_origin` as `"greedy"`.

### 2. Typed acceptance codes (no rejection codes in PR5)

Per ChatGPT amendment 1: PR5's trace records **what actually changed the final placement**, not **what the solver briefly considered and discarded**. Rejected SA moves are optimisation internals, not user-facing provenance, and belong on a separate debug surface if ever needed.

| Code | Emitted by | Meaning |
|---|---|---|
| `SA_RELOCATE_ACCEPTED` | `timetable_local_search` | SA moved a section to a new slot/room; cost delta improved or Metropolis-accepted. Context: `{"from_slot": "...", "to_slot": "...", "cost_delta": -3}`. |
| `CPSAT_IMPROVED` | `timetable_cpsat_polisher` | CP-SAT re-placed the section; the previous greedy slot appears as `previous_slot` in the entry. Context: `{"previous_slot": "...", "objective_before": ..., "objective_after": ...}`. |
| `CHAIN_ROTATED` | `timetable_chain_search` | Section moved as part of a chain. Context: `{"chain_length": 3, "chain_id": "...", "positions_in_chain": [0, 1, 2]}`. |
| `ROOMING_REPAIR_REASSIGNED` | `timetable_rooming` (2nd pass) | Room repair swapped an UNASSIGNED to a real room without moving the slot. Context: `{"previous_room": "UNASSIGNED", "new_room": "A203", "via_buffer": false}`. |

All four codes live in a new `core/services/timetable_solver_codes.py` module alongside PR2's room failure codes. One module = one source of truth. Renaming any is breaking.

**Out of PR5, tracked for later.** `SA_RELOCATE_REJECTED` — dropped per amendment 1. If rejected-move telemetry is ever needed, it ships as its own debug/diagnostic surface outside `decision_trace` (e.g. a separate `sa_diagnostics` payload section). Not a PR5 deliverable.

### 3. `perturbation_metric` provenance

`perturbation_metric` gains a nested `changes_by_stage: {"greedy": int, "sa": int, "cpsat": int, "chain": int, "rooming_repair": int}` sub-dict (ChatGPT amendment 2: keep it nested inside `perturbation_metric`, not a sibling on the top-level payload — all change accounting lives in one place).

**Invariant (documented + test-enforced):** `sum(changes_by_stage.values()) == changes_from_baseline_count`. Missing stages are omitted from the sub-dict OR zeroed; the serialiser picks one convention and every scenario follows it (we pick: always include all five keys, zero when absent — reader-friendly).

`changes_from_baseline_count` flat counter stays for back-compat.

### 4. Ops surface

`python manage.py pr5_acceptance_report` — extends the PR3 `pr3_acceptance_report` with the new stage buckets. Reads `result_json`, prints a per-stage change count, flags boards where `changes_by_stage["sa"]` or `changes_by_stage["cpsat"]` exceed a configurable threshold (default: 30% of total placements).

### 5. Shadow mode

PR5 is additive — no behaviour change under flag-off. Flag-on simply populates new trace fields and `changes_by_stage`. This is unlike PR3 (warm-start changed the seed) and PR4 (new rejections change scoring). **The acceptance bar for PR5 is therefore schema-stability + provenance coverage, not solver determinism.**

---

## Amendments applied (ChatGPT round 1 — 2026-04-20)

1. **`SA_RELOCATE_REJECTED` dropped from scope.** PR5 trace records accepted final changes only. Rejected SA moves, if ever needed, ship as a separate debug surface outside `decision_trace`. Test 3 removed from the test-cases list. Commit 3 emits only the accepted variant.
2. **`changes_by_stage` stays nested under `perturbation_metric`.** Confirms the original instinct. Invariant documented: `sum(changes_by_stage.values()) == changes_from_baseline_count`. Convention: include all five keys always, zero when absent.
3. **`stage_origin` scope tightened.** On `DecisionTrace` only (not `Alternative`). Semantic rule documented: "the stage that **last** changed the chosen placement currently recorded in this trace."
4. **Flag-off parity relaxed from bit-for-bit to semantic.** Acceptance bar #6 no longer uses raw `json.dumps(sort_keys=True)` equality. Replaced with semantic parity on the pre-PR5 subset of payload fields (placements, decision_trace ignoring new PR5 fields, perturbation_metric top-level counts, room_failures / breakdown / counts, feasible-rate). Implementation: normalised comparison helper that strips PR5-added keys, then compares.
5. **Wallclock ceiling kept at 1.3x PR4 baseline.** Not tightened to 1.1x. PR5 wires provenance into four mutation-heavy stages; more exposure to low-level overhead than prior PRs warrants.

Commit-plan adjustment (ChatGPT amendment + amendment 3 combined): commit 2 ships `stage_origin` on `DecisionTrace` and `timetable_solver_codes.py` only — no `Alternative.stage_origin`, no rejected-stage codes.

---

## Out of scope

- **Rewriting `decision_trace` at solver-pipeline level.** The primitives (`DecisionTrace`, `Alternative`) are PR3's and stay unchanged. PR5 adds one field to `DecisionTrace`, it does not replace the dataclass.
- **`SA_RELOCATE_REJECTED` or any rejected-move telemetry.** Dropped per amendment 1. Future debug-surface question, not PR5.
- **`Alternative.stage_origin`.** Deferred per amendment 3. Future PR if any stage produces stage-origin alternatives.
- **Bit-for-bit JSON parity.** Replaced with semantic parity per amendment 4.
- **Turning observational `ROOM_HEURISTIC_MISMATCH` into hard-reject.** Now moot after PR4's unified predicate — the mismatch is definitionally impossible under the promoted default. Retiring the observational code path entirely is a PR6+ question.
- **Multi-instructor parsing.** Carried from PR4 A6. Still blocked on data (zero delimiters in the scan).
- **Board-level overlap minimiser.** Tracked in "Deferred" for a future PR. Would be its own theme — does not fit under "trace parity".
- **Configurable soft-constraint weights.** Registrar-facing feature, not a refactor. Orthogonal.
- **Dropping `perturbation_metric` back-compat.** The `changes_from_baseline_count` flat counter stays, even though the new `changes_by_stage` sub-dict supersedes it.

---

## Acceptance bar

1. **Schema presence.** Every `DecisionTrace` entry across the V2 pipeline carries a non-null `stage_origin` field. Measured by running the scenario pack end-to-end and asserting `all(entry["stage_origin"] in VALID_STAGES for entry in trace.values())`.

2. **Stage coverage.** At least one test fixture exercises each of the four codes (`SA_RELOCATE_ACCEPTED`, `CPSAT_IMPROVED`, `CHAIN_ROTATED`, `ROOMING_REPAIR_REASSIGNED`).

3. **Perturbation provenance.** `sum(changes_by_stage.values()) == changes_from_baseline_count` for every scenario in the pack. Back-compat is mechanical, not asserted-by-wishful-thinking.

4. **Feasible-rate floor.** `>= 99%` of PR4 baseline (post-merge master `71bf988`) on the scenario pack. PR5 is schema-additive; any feasibility drop is a bug.

5. **Performance.** Planner wallclock p95 `<= 1.3x` PR4 baseline (kept at PR4's ceiling per amendment 5, not tightened).

6. **Flag-off semantic parity.** With `TIMETABLE_PR5_STAGE_TRACE_ENABLED=false`, result payloads are semantically identical to master `71bf988` on the pre-PR5 subset (amendment 4). Normalised comparator strips PR5-added keys (`stage_origin` on trace entries, `perturbation_metric.changes_by_stage`), then asserts exact equality on the remaining structure across 12 scenarios. New PR5 keys may be absent, or present with their default / empty values, depending on flag-off implementation.

---

## Test cases (failing in commit 1)

Fixture-backed, one per new code path. Pattern mirrors PR3 and PR4.

1. `TestStageOriginGreedyDefault::test_greedy_placement_has_stage_origin_greedy` — flag-on, no SA/CP-SAT invocation; every trace entry has `stage_origin == "greedy"`.
2. `TestSARelocateEmission::test_accepted_move_appears_in_trace` — fixture where SA provably improves cost; assert trace's chosen entry for the moved section carries `SA_RELOCATE_ACCEPTED` context and `stage_origin == "sa"`.
3. `TestCPSATImprovementEmission::test_cpsat_swap_appears_in_trace` — fixture where CP-SAT materially improves the objective; assert moved sections carry `CPSAT_IMPROVED` with `previous_slot` populated and `stage_origin == "cpsat"`.
4. `TestChainRotationEmission::test_chain_swap_appears_in_trace` — fixture engineered to make chain-search take a 3-section rotation. Assert `CHAIN_ROTATED` and `stage_origin == "chain"`.
5. `TestRoomingRepairEmission::test_repair_reassigned_appears_in_trace` — fixture where UNASSIGNED→assigned on 2nd pass. Assert `ROOMING_REPAIR_REASSIGNED` and `stage_origin == "rooming_repair"`.
6. `TestChangesByStageSum::test_sum_equals_changes_from_baseline` — invariant: `sum(changes_by_stage.values()) == changes_from_baseline_count` across 4 scenarios (2 with SA-only, 2 with SA+CP-SAT).
7. `TestStageOriginSemantic::test_last_changer_wins` — fixture where greedy places a section, SA moves it, CP-SAT moves it again; assert final `stage_origin == "cpsat"` (amendment 3: "last changer", not "first toucher").
8. `TestFlagOffSemanticParity::test_payload_matches_pr4_master_ignoring_new_fields` — flag off, normalised comparator over 3 canonical fixtures vs captured PR4-era baseline.
9. `TestPR5AcceptanceCLI::test_report_lists_per_stage_counts` — `python manage.py pr5_acceptance_report --scenario N` emits per-stage tallies.

Existing PR1/PR2/PR3/PR4 tests must continue to pass unmodified. The PR3 + PR4 acceptance packs remain the CI gate for everything they shipped.

---

## Flag plan

| Flag | Default (commit 3) | Default (commit 8) | Controls |
|---|---|---|---|
| `TIMETABLE_PR5_STAGE_TRACE_ENABLED` | `False` | `True` | Gates population of `stage_origin` on `DecisionTrace`, the four new `SA_/CPSAT_/CHAIN_/ROOMING_REPAIR_` codes, and `perturbation_metric.changes_by_stage`. Off = PR4 behaviour (trace entries have no `stage_origin` set — readers treat missing as `"greedy"`). |

One flag is enough because all four new codes + the `stage_origin` field + `changes_by_stage` are a single semantic group (solver-pipeline trace provenance). Unlike PR4 (two independent behaviour flips), PR5 is one additive surface.

---

## Rollback

- **Flag kill-switch.** `TIMETABLE_PR5_STAGE_TRACE_ENABLED=false` — env var, no redeploy. Payload reverts to PR4-equivalent semantics (bar-6 semantic parity test enforces this).
- **Commit-8 revert.** Restores default `False`. Keeps the plumbing code but disables the behaviour.
- **Merge revert.** Drops the whole feature.

All three are independent.

---

## Commit plan

| # | Commit | What lands |
|---|---|---|
| 0 | PR5 DoR (amended) | This file (`docs/PR5-DOR.md`) post ChatGPT round 1 amendments. Branch initialised from master `71bf988`. |
| 1 | Failing tests + scenario-pack additions | The 9 test cases above as `tests/test_pr5_*.py`; fixture JSONs under `snapshots/planner-refactor-2026-04-20/fixtures/pr5_*.json`. Green-behind-flag (flag default `False`). |
| 2 | `stage_origin` on `DecisionTrace` + codes module | Add `stage_origin` field to `DecisionTrace` only (NOT `Alternative` — amendment 3); default `"greedy"`. Update greedy placer to set `"greedy"` explicitly. Create `core/services/timetable_solver_codes.py` with the four acceptance sentinels (no rejection codes — amendment 1). No solver-side emission yet. |
| 3 | SA trace emission | Wire `timetable_local_search.optimize_board` to append `SA_RELOCATE_ACCEPTED` context to the chosen entry and update `stage_origin = "sa"` on moved sections. Flag-gated. No `SA_RELOCATE_REJECTED` (amendment 1). |
| 4 | CP-SAT trace emission | Wire `timetable_cpsat_polisher.polish_scenario_with_cpsat` to emit `CPSAT_IMPROVED` with `previous_slot` and update `stage_origin = "cpsat"`. Flag-gated. |
| 5 | Chain-search trace emission | Wire `timetable_chain_search` to emit `CHAIN_ROTATED` with chain context and update `stage_origin = "chain"`. Flag-gated. |
| 6 | Rooming 2nd-pass trace emission | Wire the repair path in `timetable_rooming.assign_rooms_to_board` to emit `ROOMING_REPAIR_REASSIGNED` and update `stage_origin = "rooming_repair"`. Flag-gated. |
| 7 | `changes_by_stage` + acceptance CLI | Extend `perturbation_metric` with the nested `changes_by_stage` sub-dict (amendment 2) + invariant enforcement. Ship `python manage.py pr5_acceptance_report`. Flag-gated. |
| 8 | Promotion note (docs-only) | `docs/PR5-DOR.md` closeout block + `docs/PR5-PROMOTION-NOTE.md` with rollback narrative. Flag promoted to `True`. Memory update. No code change. |

Commits 1–2 are green-behind-flag (new structures unused at runtime). Commits 3–7 are additive behind the flag. Commit 8 is docs + flag flip only.

---

## Split decision

**Status:** unified. PR5 stays single-PR. Previously there was a split-path contingency around SA rejected-move noise — now moot since amendment 1 dropped `SA_RELOCATE_REJECTED` entirely.

---

## Pre-condition / Sign-off

- [x] ChatGPT round 1 review completed (2026-04-20).
- [x] Amendment round 1 applied (amendments 1–5 above).
- [ ] ChatGPT round 2 approval of amended DoR.
- [ ] Commit 0 (DoR) + commit 1 (failing tests + scenario pack) land after approval.
