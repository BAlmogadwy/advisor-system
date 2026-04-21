# PR5 — Promotion Note

**Branch:** `refactor/pr5-solver-pipeline-trace-parity`
**Base:** master @ `71bf988` (post-PR4 instructor-realism + semantic cleanup)
**Promotion commit:** commit 8 (this commit)
**Theme:** Solver-pipeline decision-trace parity — every stage that changes a placement records *why*.

---

## What shipped

PR5 extends the PR3 decision-trace surface so every post-greedy stage (SA, chain, CP-SAT polish, rooming repair) leaves a typed provenance record on the section it touched. Before PR5, a registrar reading `decision_trace` could see greedy's reasoning but had no way to tell whether a later stage had rewritten the placement. After PR5, each trace entry carries `stage_origin` ("greedy" | "sa" | "cpsat" | "chain" | "rooming_repair") and a `stage_context` dict with a typed code plus stage-specific payload.

Eight commits:

1. **DoR** — scope, amendments, flag plan, acceptance bars.
2. **Failing tests + scenario-pack fixtures.**
3. **`stage_origin` field on `DecisionTrace`** + `core/services/timetable_solver_codes.py` module with the four acceptance codes (`SA_RELOCATE_ACCEPTED`, `CPSAT_IMPROVED`, `CHAIN_ROTATED`, `ROOMING_REPAIR_REASSIGNED`) + `is_stage_trace_enabled()` helper.
4. **SA trace emission** — each accepted SA relocate emits `stage_origin="sa"` with `SA_RELOCATE_ACCEPTED` and the `{previous_slot, new_slot}` pair.
5. **CP-SAT polisher trace emission** — each improved section emits `stage_origin="cpsat"` with `CPSAT_IMPROVED` and `{previous_slot, new_slot}`. Overlay fixes the previous leak where CP-SAT improvements were persisted but not reflected in `decision_trace`.
6. **Chain-search trace emission** — accepted chain-2 rotations emit `stage_origin="chain"` with `CHAIN_ROTATED` and `{chain_length, chain_id, previous_slot, new_slot}`. Rejected chains are intentionally silent (amendment 1).
7. **Rooming 2nd-pass trace emission** — `UNASSIGNED → real room` repairs emit `stage_origin="rooming_repair"` with `ROOMING_REPAIR_REASSIGNED` and `{previous_room, new_room}`. Strictly gated on the sentinel transition; empty-string → assigned (the normal first-pass path) never emits.
8. **`changes_by_stage` aggregator + `pr5_acceptance_report` CLI + flag-off parity comparator** — `perturbation_metric.changes_by_stage` is derived from the final `decision_trace` by `core/services/timetable_stage_summary.compute_changes_by_stage`, preserving the invariant `sum == changes_from_baseline_count`. `core/services/timetable_pr5_parity.strip_pr5_fields_for_parity` scrubs PR5-added fields for flag-off semantic comparisons.
9. **This commit — promotion.** Flip `TIMETABLE_PR5_STAGE_TRACE_ENABLED` default to `True` and land this note.

---

## Feature flags — promoted in this commit

| Flag | Old default | New default | Env var | Kill-switch |
|---|---|---|---|---|
| `TIMETABLE_PR5_STAGE_TRACE_ENABLED` | `False` | **`True`** | same name | Set to `false` → no stage-trace emission, payload reverts to PR4-equivalent semantics on the pre-PR5 subset. |

The flag is read on every request via `is_stage_trace_enabled()` (in `core/services/timetable_solver_codes.py`). Flipping the env var on Render and restarting the worker is a live kill-switch — no redeploy required.

---

## Rollback path (tiered, cheapest first)

1. **Env kill-switch** — set `TIMETABLE_PR5_STAGE_TRACE_ENABLED=false` on Render, restart worker. Traffic-free, no redeploy. Restores pre-PR5 stage provenance behaviour without reverting any code; `stage_origin` is no longer populated, the four acceptance codes are no longer emitted, and `perturbation_metric.changes_by_stage` is either absent or neutral (see PR5-specific caveat below).
2. **Revert commit 8 only** — restores default `False`. Use if the issue is specifically the default-on promotion (e.g. downstream analytics pipeline not ready for the new fields) rather than the emission logic itself. Re-deploy needed. All stage-emission plumbing stays in place and can be re-enabled via the env var.
3. **Revert the PR5 merge** — drops the entire PR5 feature set. Use only if the logic itself is wrong, not just the default.

---

## PR5-specific caveat (ChatGPT commit-8 ruling)

- Under **flag-off parity** comparisons, `changes_by_stage` is neutral/stripped — it is **not** part of the pre-PR5 contract. Consumers diffing flag-off output against master `71bf988` must normalise with `strip_pr5_fields_for_parity` before comparing; that comparator removes `perturbation_metric.changes_by_stage` and scrubs `stage_origin` / `stage_context` from trace entries.
- **`stage_origin` and `stage_context` are provenance-only fields.** They MUST NOT affect placement decisions or rooming outcomes in any downstream consumer. Any logic that reads them should be read-only (audit, reporting, debugging); routing placements through them is a category error that couples the provenance layer to the decision layer.

---

## Behavioural changes (flag-on)

- **New field on `DecisionTrace` entries:** `stage_origin: str` (defaults to `"greedy"`; set to `"sa"` / `"cpsat"` / `"chain"` / `"rooming_repair"` when a post-greedy stage claims the section) and `stage_context: dict` (empty for greedy-only entries; carries a typed `code` + stage-specific payload when a non-greedy stage claims the section).
- **Four new acceptance codes** in `stage_context["code"]`: `SA_RELOCATE_ACCEPTED`, `CPSAT_IMPROVED`, `CHAIN_ROTATED`, `ROOMING_REPAIR_REASSIGNED`. Schema is additive — existing consumers keep working unchanged.
- **New sub-dict in `perturbation_metric`:** `changes_by_stage: dict[str, int]` with keys `{greedy, sa, cpsat, chain, rooming_repair}`. Always present (schema-stable); values bucket the sections whose final `stage_origin` equals that key **and** whose section code appears in the changed-from-baseline set. Invariant enforced in tests: `sum(changes_by_stage.values()) == changes_from_baseline_count`.
- **"Last-changer-wins" semantic:** when greedy → SA → CP-SAT → rooming-repair all touch the same section, the final trace entry reflects the last stage to change that section's effective placement. V2 overlays each stage's trace dict into `result["decision_trace"]` by section-code key, so the overlay order is authoritative.
- **Rooming 2nd-pass repair path** is real behaviour now, not just trace: `assign_rooms_to_board` re-processes placements carrying the `UNASSIGNED` sentinel as repair candidates. Pre-PR5 this path silently skipped the sentinel.

---

## Acceptance

- PR5 decision-trace suite (`tests/test_pr5_decision_trace.py`) — 43 passing, 1 legitimate skip (chain fixture — greedy occasionally resolves the triple-clash pre-chain, leaving nothing for chain to rotate; emission shape covered by the flag-off companion + contract tests).
- PR3 acceptance pack (`tests/test_pr3_acceptance_pack.py`) — 21/21 stable. CI gate from PR3 unchanged.
- Rooming regression (`tests/test_pr2_room_oracle.py`, `tests/test_pr2_silent_unassigned_sites.py`) — 50/50 passing. The 2nd-pass repair widening (UNASSIGNED as candidate) did not regress buffer-rejection or heuristic-match paths.
- Pre-commit hooks all pass (ruff, bandit; mypy skipped with the usual `SKIP=mypy`).

---

## Known non-goals (out of scope for PR5, carry to PR6 or later)

- **Rejection-code emission for non-accepted stages.** Amendment 1 scoped PR5 to acceptance-only codes. `SA_RELOCATE_REJECTED` / `CPSAT_REJECTED` would double the trace volume and their diagnostic value is unclear without a separate "why rejected" study.
- **`stage_origin` on `Alternative`.** Amendment 3 scoped `stage_origin` to `DecisionTrace` only. An alternative's origin is already implicit in the parent entry's stage; adding it to each alternative duplicates state and invites drift.
- **Per-stage wallclock telemetry.** `changes_by_stage` is counts-only. Adding `stage_ms` / `stage_iterations` alongside is a clean PR6 follow-up but would muddy the PR5 contract.
- **Trace emission from the greedy generator itself.** `stage_origin="greedy"` is the dataclass default — no explicit emission is needed at greedy time. If a future study wants fine-grained greedy provenance (e.g. which strategy picked this option), that is a separate module.

---

## Memory update

`MEMORY.md` under "Session Log" records the PR5 promotion:

- `TIMETABLE_PR5_STAGE_TRACE_ENABLED` flipped to `True` default at commit 8.
- Env kill-switch live and verified.
- Test counts: 43 PR5 tests (was 0 pre-PR5) + 21 PR3 acceptance pack (stable).
- Rollback doc: `docs/PR5-PROMOTION-NOTE.md` (this file).
