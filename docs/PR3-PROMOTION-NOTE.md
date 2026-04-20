# PR3 — Decision-Trace + Minimal-Perturbation Promotion Note

**Branch:** `refactor/pr3-decision-trace-minimal-perturbation`
**Base:** `master` after PR2 merge (`fdcd691`)
**Refactor-Track:** planner-refactor-q2-2026
**Rollout stance:** `TIMETABLE_PR3_DECISION_TRACE_ENABLED=True` from commit 2 (observational only). `TIMETABLE_PR3_WARM_START_ENABLED=True` from commit 8 (behavioural; env kill-switch preserved).

Read this before flipping either flag off in a live environment, or before treating the warm-start path as implicit.

---

## 1. Scope

PR3 ships two cooperating capabilities behind separate flags:

| Setting                                    | Default | Purpose                                                            |
| ------------------------------------------ | ------- | ------------------------------------------------------------------ |
| `TIMETABLE_PR3_DECISION_TRACE_ENABLED`     | `True`  | Emits per-section `decision_trace[section_code]` with the chosen option and top-2 rejected alternatives. Pure observation; no planning-decision change. |
| `TIMETABLE_PR3_WARM_START_ENABLED`         | `True`  | Enables warm-start retention: when a `baseline_placements` map is supplied, feasible baseline slots are retained instead of re-scoring cold. Behavioural — biases which slot is chosen. |

Both defaults are controlled in `config/settings.py` with env var overrides preserved:

```
TIMETABLE_PR3_DECISION_TRACE_ENABLED = os.getenv(...).lower() in {"1","true","yes","on"}
TIMETABLE_PR3_WARM_START_ENABLED     = os.getenv(...).lower() in {"1","true","yes","on"}
```

### What PR3 ships

- `core/services/timetable_decision_trace.py` — `DecisionTraceEntry` + `CandidateAlternative` dataclasses, `is_decision_trace_enabled()`, rejection-code sentinels `STUDENT_CONFLICT` / `INSTRUCTOR_CLASH`.
- `core/services/timetable_warm_start.py` — `BaselinePlacement` frozen dataclass, `is_warm_start_enabled()`, `apply_warm_start()`, `compute_perturbation_metric()` (four counters: `unchanged_count`, `changes_from_baseline_count`, `newly_placed_count`, `removed_count`).
- `auto_place_board` wiring — cold-start trace capture (commit 3–4), warm-start retention (commit 5), scenario-scoped baseline per board (commit 6).
- `optimise_scenario_timetable_v2` — `baseline_placements` kwarg fanned through to `auto_place_scenario`; scenario-level perturbation metric summed across boards; schema-stable 4-counter dict on all early-return paths.
- `tests/test_pr3_acceptance_pack.py` — authoritative CI gate across 10 fixtures, five acceptance bars (schema presence, known-alphabet, ≥0.90 trace coverage, canonical warm-start zero-change, cold-start parity).
- `core/management/commands/pr3_acceptance_report.py` — ops surface over the same shared runner.
- `docs/PR3-DOR.md` — DoR sign-off (seven amendments, commit plan).
- `snapshots/planner-refactor-2026-04-20/PR3-SCENARIO-PACK.md` — frozen scenario pack.

### What PR3 does NOT ship

- **No UI surface** for `decision_trace` or perturbation counters. Operators read them via logs, JSON payload, or the `pr3_acceptance_report` management command. UI comes in a follow-up PR.
- **No weight / objective-function tuning.** Trace visibility is the pre-condition for that work, not the work itself.
- **No full search-tree logging.** Trace records one chosen + two rejected per section; that is the ceiling for PR3.
- **No V2 lock-aware move pruning** (carried forward from PR1; still deferred pending metric evidence of churn).

---

## 2. Decision-trace flag — safe-by-default observation

`TIMETABLE_PR3_DECISION_TRACE_ENABLED=True` is load-bearing for the acceptance bars. The flag is pure observation — disabling it produces `decision_trace={}` in every payload (schema stability asserted by `tests/test_pr3_acceptance_pack.py::test_schema_presence`), and placement decisions are bit-for-bit identical whether the flag is on or off.

**Rollback path:** `TIMETABLE_PR3_DECISION_TRACE_ENABLED=false` — config change, no redeploy. The payload keeps the `decision_trace` key (empty dict); downstream consumers that match on `decision_trace.get(...)` remain correct.

---

## 3. Warm-start flag — behavioural; kill-switch live

`TIMETABLE_PR3_WARM_START_ENABLED=True` changes placement decisions when a caller supplies `baseline_placements`. The change is conservative:

- No baseline supplied → warm-start is a no-op regardless of flag state.
- Baseline supplied, flag on, baseline slot remains feasible (PR1 prayer / lock, PR2 room-oracle, student/instructor conflict) → slot is retained without re-scoring.
- Baseline supplied, flag on, baseline slot is infeasible → section falls back to cold-start scoring (nothing is placed on an illegal slot).
- Baseline supplied, flag off → same cold-start scoring path as before commit 8.

The canonical acceptance bar (`test_canonical_warm_start_zero_change`) asserts that the zero-change fixture yields `changes_from_baseline_count == 0` when the flag is on. The cold-start parity bar (`test_cold_start_parity_all_newly_placed`) asserts that placing without a baseline still reports all four counters with `newly_placed_count == placed` and the other three at zero.

### Rollback paths (fast → slow)

1. **Env kill-switch (no redeploy):** set `TIMETABLE_PR3_WARM_START_ENABLED=false` in the live environment's settings surface (Render env vars, Docker env, etc.). The next planner run reverts to cold-start behaviour. The `decision_trace` payload continues to emit normally.
2. **Code revert:** `git revert <commit-8-sha>` on `master`. This restores the `"false"` default in `config/settings.py` and the `warm_start_on=True`-absent log line. All commit 5–7 machinery stays in place — the kwarg, the retention logic, the acceptance pack are untouched. A subsequent promotion is one line.
3. **Full rollback:** revert commits 5–8 as a block. Only necessary if the warm-start *logic* turns out to be the source of a regression (rather than the default). The acceptance pack (commit 7) is the evidence gate for deciding which rollback depth is appropriate — run `python manage.py pr3_acceptance_report` and read the four counters.

### Observability

`auto_place_board` emits one info-level log per run that now includes the warm-start state:

```
auto_place_board(board=<id>): placed=<n> skipped=<n> prayer_rule_on=<bool> (rejections=<n>)
lock_rule_on=<bool> (rejections=<n>) warm_start_on=<bool> (baseline_provided=<bool>)
```

A run can be classified as warm-start on/off directly from the log line without reading settings. `baseline_provided=False` with `warm_start_on=True` is not a warning — it just means the caller did not supply a baseline, so warm-start had nothing to retain.

The scenario-wide `perturbation_metric` returned by `auto_place_scenario` is the structured counterpart: four integers summed across boards.

---

## 4. Acceptance-pack bars

The pytest module `tests/test_pr3_acceptance_pack.py` is the authoritative CI gate. All 5 bars must pass before any future behavioural change on this surface:

1. **Schema presence** — `decision_trace={}` and `perturbation_metric={unchanged_count, changes_from_baseline_count, newly_placed_count, removed_count}` are always present in the return payload, including trace-disabled and early-return paths.
2. **Known-alphabet only** — rejection codes are drawn from the frozen set `{PRAYER_OVERLAP, LOCK_RESPECT, NO_ROOM_CAPACITY, NO_ROOM_GENDER, NO_ROOM_TYPE, ROOM_OCCUPIED, ROOM_BUFFER_REJECT, ROOM_HEURISTIC_MISMATCH, STUDENT_CONFLICT, INSTRUCTOR_CLASH}`. Any new code added without this list growing is a failed build.
3. **Trace coverage floor** — aggregate `traced / placed ≥ 0.90` across fixtures where trace is enabled and placed > 0. The `>0` guard avoids divide-by-zero on pack entries with no placements.
4. **Canonical warm-start zero-change** — `pr3_canonical_warm_start.json` with warm-start on yields `changes_from_baseline_count == 0`.
5. **Cold-start parity** — `pr3_cold_start_parity.json` with no baseline yields `newly_placed_count == placed` and the other three counters at zero.

The ops wrapper `python manage.py pr3_acceptance_report` runs the same `run_pr3_fixture` runner and prints the table for a registrar — no pytest output reading required.

---

## 5. Code anchors (post-promotion)

| File:line | What |
|---|---|
| `config/settings.py:325` | `TIMETABLE_PR3_WARM_START_ENABLED` default flipped to `"true"`; env override preserved. |
| `core/services/timetable_warm_start.py:67` | `is_warm_start_enabled()` reads the setting; used by all planner sites. |
| `core/services/timetable_autoplace.py:813` | `warm_start_on = is_warm_start_enabled()` — module-level lookup at board entry. |
| `core/services/timetable_autoplace.py:1411` | Warm-start retention gate: `if warm_start_on and normalised_baseline is not None`. |
| `core/services/timetable_autoplace.py:1682` | Per-run log line now includes `warm_start_on` + `baseline_provided`. |
| `core/services/timetable_autoplace.py:1703` | Final return — `perturbation_metric` scoped to per-board baseline. |
| `core/services/timetable_optimizer_v2.py` | `baseline_placements` kwarg fanned into `auto_place_scenario`; four-counter metric seeded on all early-return paths. |
| `tests/test_pr3_acceptance_pack.py` | CI gate. 21 tests. |
| `core/management/commands/pr3_acceptance_report.py` | Ops surface. Same runner. |

---

## 6. Deferred follow-ups (not PR3)

- UI surface for `decision_trace` — operator panel showing "why this slot, not that one".
- Dashboard KPI tile for the four perturbation counters — needs design once operators have seen the data flow through for a few runs.
- Weight / objective-function tuning — separate PR once the trace surfaces which penalties dominate.
- Full search-tree logging — explicitly out-of-scope for PR3.
- V2 lock-aware move pruning — carried forward from PR1; still deferred.
- Removing deprecated `lecture_room_reject_due_to_buffer_count` — PR2 follow-up.

---

## 7. Commit list

| Commit   | Subject                                                                          |
| -------- | -------------------------------------------------------------------------------- |
| (early)  | PR3 commits 1–2: failing tests + scenario pack skeleton + flag plumbing          |
| (mid)    | PR3 commit 3: cold-start trace capture (`decision_trace={}` schema stability)    |
| be001a2  | PR3 commit 4: rejection-code alphabet + trace capture at V2 local-search site    |
| cdf075e  | PR3 commit 5: warm-start module + `BaselinePlacement` + retention wiring         |
| 3e2887c  | PR3 commit 6: scenario-level baseline + per-board scoping + metric aggregation   |
| 150aa6a  | PR3 commit 7: acceptance-bar pytest pack + `pr3_acceptance_report` command       |
| (this)   | PR3 commit 8: promote `TIMETABLE_PR3_WARM_START_ENABLED` default to True + docs  |
