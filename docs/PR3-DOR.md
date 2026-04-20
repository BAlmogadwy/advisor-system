# PR3 — Definition of Ready

**Branch:** `refactor/pr3-decision-trace-minimal-perturbation`
**Base:** master @ `fdcd691` (post-PR2 room-oracle + infeasibility reporting)
**Theme:** Explainability + minimal perturbation. Answer the two questions PR2 didn't — *"Why did it place there?"* and *"When I re-run, don't destroy everything."*

This document is the pre-code agreement on scope, non-scope, acceptance bar, and commit sequence. No code lands on this branch until the DoR is signed off by ChatGPT.

---

## Why this PR, why now

PR1 made placement rules explicit (prayer, lock rejection with `RejectionReason`).
PR2 made placement failures typed (`RoomFailureReason` with six reason codes).
A registrar now knows *that* a section failed and *what kind of failure* it was — but still can't see *why a placed section is where it is* or *why the obvious alternative was rejected*. And every planner re-run today reshuffles placements from scratch, destroying a week of manual tweaks because the solver has no notion of "keep what already works."

PR3 closes that loop by adding:

1. **Decision trace.** For each placed section, record the chosen slot plus 2–3 alternatives that were considered, each with a typed rejection reason (reusing PR1 + PR2 codes — no invented labels).
2. **Warm-start / minimal perturbation.** Accept existing placements as a soft-fixed initial state; move a section only when conflict forces it. Locks stay hard (PR1); this is a new *softer* "prefer to keep" layer.
3. **Perturbation metric.** Emit `changes_from_baseline_count` and `unchanged_count` so the registrar can see at a glance how disruptive a re-run was.

No new hard constraints. No solver rewrite. No weight tuning. No CP-SAT expansion.

---

## In scope

### 1. `DecisionTrace` dataclass

Frozen, JSON-serialisable, PR1-shape-aligned:

```python
@dataclass(frozen=True)
class DecisionTrace:
    section_code: str
    course_code: str
    chosen: ChosenSlot                 # day / start / end / room / pattern_id
    alternatives: tuple[Alternative, ...]   # up to 3

@dataclass(frozen=True)
class Alternative:
    day: str
    start_time: str
    end_time: str
    room: str | None
    rejection_code: str                # PR1 / PR2 code (no new sentinels)
    rejection_context: dict[str, object]    # optional detail (e.g. {"clashing_section": "..."})
```

**Rejection codes reused verbatim** — no invented labels:

| Source PR | Code | Meaning (inherited) |
|---|---|---|
| PR1 | `PRAYER_WINDOW_CLASH` | Candidate overlaps a prayer window |
| PR1 | `LOCK_VIOLATION` | Candidate moves a locked placement |
| PR3 | `INSTRUCTOR_CLASH` | Instructor already teaches another section at this slot |
| PR3 | `STUDENT_CONFLICT` | ≥1 student enrolled in another section already at this slot (cohort conflict in planner semantics; deliberately named *conflict* rather than *overlap* because this is about who is in the cohort, not geometric time-overlap) |
| PR2 | `NO_ROOM_TYPE` / `NO_ROOM_GENDER` / `NO_ROOM_CAPACITY` / `ROOM_OCCUPIED` / `ROOM_BUFFER_REJECT` / `ROOM_HEURISTIC_MISMATCH` | As per PR2-DOR.md |

**New for PR3:** two codes that didn't exist before — `INSTRUCTOR_CLASH` and `STUDENT_CONFLICT`. These surface reasons that today are implicit inside scoring (a candidate with overlap gets a worse score and gets beaten; PR3 makes the rejection explicit in the trace).

### 2. Decision-trace capture

**Required vs. best-effort:**

| Stage | Trace capture |
|---|---|
| `auto_place_board` (greedy placer) | **MUST** capture chosen + up to 3 alternatives per placed section |
| V2 local-search stage — when a move is *accepted* | **MAY** append/update trace best-effort (the new chosen slot plus the 2 strongest rejected neighbours of that move) |
| V2 chain-search | **SHOULD NOT** capture — trace would require full search-tree instrumentation |
| V2 CP-SAT polish | **SHOULD NOT** capture — internal to OR-Tools; instrumentation would blow up complexity |
| V2 ranking / generation | **SHOULD NOT** capture — these enumerate strategies, not per-section decisions |

**How.** After scoring a section's candidate slots, emit the top-scoring candidate as `chosen`, then the next 2 candidates (best-scored but rejected) with their rejection reason. If fewer than 2 alternatives exist (tight scenarios), emit whatever does. Alternative count is **fixed at 3 max** — not configurable.

**Approximation rule.** DecisionTrace is *approximate, not perfect*. We do NOT:

- capture the full search tree
- log every rejected candidate
- record every alternative for every pass of V2's 5-stage pipeline

The rule (per ChatGPT): **enough signal to answer "why not *this* obvious alternative?"**. No more.

### 3. Warm-start / minimal perturbation

**Baseline source.** In-memory / caller-supplied only. `baseline_placements` comes from the planner's caller (typically a previous run's result payload). **No DB persistence. No new model fields. No migrations.** Persisting baselines across server restarts is explicitly a future-PR concern.

**Input.** A new `baseline_placements: dict[section_code, BaselinePlacement]` argument accepted by `auto_place_board` (default `None` → cold start, identical to today's behaviour).

**Semantics.** For each section the planner is about to place:

- If a baseline entry exists AND the baseline placement is still feasible (no prayer conflict, no lock conflict, instructor free, students free, room fits): **retain it** — the existing placement is kept as-is, and `DecisionTrace` records the retention (`chosen=baseline_placement`, plus the top other scoring candidates as alternatives for transparency, each with a context note that the alternative had a better/worse score but the baseline was retained to minimise perturbation).
- If a baseline entry exists but is now infeasible: retention is skipped, the planner scores candidates normally, and the baseline's own rejection reason (e.g. `PRAYER_WINDOW_CLASH`) is recorded as one of the alternatives in the resulting trace.
- If no baseline entry exists: cold-start placement for that section, trace captured as usual.

**Framing:** "retention from baseline", not "scoring bypass." The scoring machinery itself is unchanged; warm-start is a pre-decision layer that can short-circuit with the existing slot when nothing forces a move.

**Locks stay hard.** A locked placement is still unmovable via PR1's `LOCK_VIOLATION`. The warm-start layer sits *below* locks in priority — if a baseline entry conflicts with a lock, the lock wins and `LOCK_VIOLATION` appears in the trace.

### 4. Perturbation metric

Planner result (`auto_place_board`, `run_v2_pipeline`, `run_full_optimiser`) gains:

```python
{
    # existing keys preserved (placed, skipped, placements, room_failures, …)

    "changes_from_baseline_count": int,       # sections whose final placement differs from baseline
    "unchanged_count": int,                   # sections whose final placement matches baseline exactly
    "newly_placed_count": int,                # sections not in baseline at all
    "removed_count": int,                     # sections in baseline but not in final result (rare — means feasibility lost)
    "decision_trace": dict[str, DecisionTrace],   # keyed by section_code, one entry per placed section
}
```

When `baseline_placements=None`, `changes_from_baseline_count` and `unchanged_count` are both `0` and `newly_placed_count == placed`. This is the parity case — cold starts are indistinguishable from today.

**Schema stability under disabled trace.** When `TIMETABLE_PR3_DECISION_TRACE_ENABLED=False`, the planner result still includes the `decision_trace` key with an empty `{}` — the key is always present so downstream consumers can count on a stable payload shape regardless of flag state.

### 5. Management / reporting surface

**A Django management command** — `python manage.py explain_timetable --scenario-id N` or `--board-id N`. Read-only (savepoint-rolled-back, same pattern as `report_room_failures`). Prints:

- per-section chosen slot (course / section / day / start / room)
- up to 3 alternatives per section (day / start / room / rejection code)
- perturbation summary block at the end (`unchanged=X  changed=Y  newly_placed=Z`)

No admin panel, no UI, no CSV. The command's payload shape is the next-PR's UI-layer starting point.

### 6. Tests

Fixture-backed, isolated, exercising each new code path at least once:

1. **`DecisionTrace` shape parity** — build one, round-trip `.to_dict()`, assert PR1-shape compatibility (code / day / start_time / end_time / course_code / section_code / context)
2. **Chosen + alternatives captured on cold start** — synthetic 5-section board, assert every placed section has ≥1 alternative in its trace
3. **Warm-start preserves feasible baseline** — baseline with all 5 placed, re-run → `unchanged_count == 5`, `changes_from_baseline_count == 0`, decision_trace shows `chosen=baseline` for each
4. **Warm-start falls back when infeasible** — flip one baseline entry to a prayer-clashing slot, re-run → that section moves, `changes_from_baseline_count == 1`, its trace shows `PRAYER_WINDOW_CLASH` as the rejection reason for the baseline
5. **Locks still beat warm-start** — baseline tries to move a locked section; `LOCK_VIOLATION` emitted, lock wins
6. **`INSTRUCTOR_CLASH` surfaces** — two sections same instructor overlapping slot; weaker section's trace shows `INSTRUCTOR_CLASH` for that candidate
7. **`STUDENT_CONFLICT` surfaces** — two sections share ≥1 student at the same slot; trace shows `STUDENT_CONFLICT`
8. **Perturbation metric totals** — on a re-run with 3 unchanged + 1 moved + 1 newly_placed, metric block reflects those exact counts
9. **Cold-start parity** — `baseline_placements=None` → `changes_from_baseline_count == 0`, `unchanged_count == 0`, `newly_placed_count == placed` (no behavioural change vs today)
10. **Trace-disabled schema stability** — with `TIMETABLE_PR3_DECISION_TRACE_ENABLED=False`, payload still contains `decision_trace` key equal to `{}`
11. **Canonical warm-start fixture** — a specific fixture re-run under warm-start produces `changes_from_baseline_count == 0` exactly; this is the hard invariant. Broader-pack scenarios may drift by a small amount provided each drift maps to an explainable cause.

Existing PR1 + PR2 tests (`test_timetable_prayer_rule.py`, `test_pr2_*.py`, etc.) must continue to pass unmodified.

---

## Out of scope

- **Weight / objective-function tuning.** DecisionTrace records *why*, not *how well*. No changes to spacing / day_load / slot_load weights.
- **CP-SAT expansion.** No OR-Tools changes. V2's existing CP-SAT polisher stays as-is.
- **Global optimisation redesign.** No bipartite matching, no whole-scenario re-optimiser.
- **UI dashboards.** Management command only. UI for decision_trace is a later PR.
- **Removing the legacy prayer filter.** Separate PR whenever we decide to retire `is_in_prayer_window()` in favour of the PR1 rejection path.
- **V2 lock-aware pruning.** Only if PR2 logs or this PR's metrics surface a concrete need.
- **Full search-tree logging.** Explicitly excluded per ChatGPT's "approximate, not perfect" constraint.
- **Removing deprecated `lecture_room_reject_due_to_buffer_count`.** That's a separate follow-up PR per PR2-DOR.md.
- **DB persistence of baseline placements.** `baseline_placements` is caller-supplied / in-memory only in PR3. No new model fields, no migrations. Persisting baselines (e.g. so a server restart keeps the registrar's last-saved timetable as a warm-start source) is a later PR.
- **Configurable alternative count.** Top-3 is fixed, not parameterised. A config knob adds test-branching for little gain.
- **V2 deep-phase trace capture.** Chain-search, CP-SAT polish, ranking and generation phases are explicitly excluded from trace capture — instrumenting them would require full search-tree bookkeeping.

---

## Acceptance bar (measurable, CI-enforceable)

Before merge:

1. **Trace coverage ≥ 90%.** On the PR3 scenario pack, at least 90% of placed sections have ≥1 alternative recorded in their `decision_trace` entry. Asserted by a test that runs the scenario pack and counts.
2. **Rejection codes map to known sentinels only.** Test asserts every `rejection_code` in the trace appears in the union of PR1 + PR2 + PR3-new codes. No invented vague labels.
3. **Cold-start parity.** `baseline_placements=None` produces a result byte-for-byte identical to the PR2 baseline on the parity scenario (minus the new additive payload keys, which default to `0` / `{}`).
4. **Warm-start minimal-change.** On a *canonical warm-start fixture set* (re-run with the same scenario data and no underlying change) `changes_from_baseline_count == 0` and `unchanged_count == placed` — the exact-zero invariant is asserted as a test on this specific fixture. On the *broader scenario pack*, `changes_from_baseline_count` must be low and *explainable* — every non-zero entry traceable to a concrete cause (e.g. baseline infeasibility, rare tie-break drift). No universal zero-change requirement across every scenario — that's too brittle for minor nondeterminism.
5. **Feasible-rate floor.** ≥ 99% of PR2 baseline feasible-rate on the scenario pack.
6. **Performance.** Planner wallclock p95 ≤ 1.3× PR2 baseline. Measured using the PR2 perf harness extended with a warm-start case; harness update lands around commit 6 or 7.

---

## Internal commit order

| # | Commit | What lands |
|---|---|---|
| 1 | Failing tests + scenario pack | The 9 test cases above as `test_pr3_*.py`; fixture JSONs under `snapshots/planner-refactor-2026-04-20/fixtures/pr3_*.json`; skeleton `DecisionTrace` import that doesn't exist yet (ImportError documents the contract). |
| 2 | `DecisionTrace` + `Alternative` dataclasses + sentinels | `core/services/timetable_decision_trace.py` with both dataclasses, `INSTRUCTOR_CLASH` + `STUDENT_CONFLICT` sentinels, `.to_dict()`, unit tests. No wiring. |
| 3 | Integrate trace into `auto_place_board` | Capture chosen + top-2 alternatives in the greedy placer; attach to return payload as `decision_trace`. Cold-start only. |
| 4 | Integrate trace into V2 (best-effort) | Capture chosen + alternatives in V2's local-search stage. Best-effort — stages where it's expensive to capture (CP-SAT polish) can skip. |
| 5 | Warm-start logic | Accept `baseline_placements` argument; prefer feasible baseline entries; fall back on infeasibility. Emit `unchanged_count`, `changes_from_baseline_count`, `newly_placed_count`, `removed_count`. |
| 6 | Perturbation metric wiring | Extend `run_v2_pipeline` / `run_full_optimiser` return dicts to propagate the metrics. Unit tests. |
| 7 | `explain_timetable` management command | Mirrors `report_room_failures` pattern (savepoint rollback, scenario-id / board-id mutex group). |
| 8 | Promotion note | Close `docs/PR3-DOR.md` with promotion block + acceptance-bar numbers. Mark deferred follow-ups. |

Commits 1–2 are green-behind-a-flag (new structures unused). Commit 3 wires cold-start trace capture (additive, no behavioural change to placement decisions). Commits 4–7 are additive. Commit 8 is docs only.

---

## Flag plan

| Flag | Default | Controls |
|---|---|---|
| `TIMETABLE_PR3_DECISION_TRACE_ENABLED` | `True` | Gates trace capture. Off = return empty `decision_trace={}`; warm-start metrics still work. Env var override preserved for emergency disable. |
| `TIMETABLE_PR3_WARM_START_ENABLED` | `False` → flip to `True` in commit 8 | Gates the warm-start preference layer. Off = ignore `baseline_placements` even when provided (cold-start parity). |

Rationale for split flags: trace capture is pure observation (zero planning-decision change); warm-start is a behavioural change (it biases which slot gets chosen). They roll out independently so a warm-start regression doesn't force the trace capture off.

---

## Code anchors

### Decision-trace capture sites (commit 3)

| File:line | Context |
|---|---|
| `core/services/timetable_autoplace.py` — the scoring+placement loop that currently keeps a `best_option` | After loop: emit chosen from best_option, top-2 non-chosen candidates from scored list |
| `core/services/timetable_v2_pipeline.py` — local-search stage | After local-search accepts a move: record the move + 2 rejected neighbours |

### Warm-start preference logic (commit 5)

| File:line | Context |
|---|---|
| `core/services/timetable_autoplace.py` — section loop entry | Before scoring: check `baseline_placements.get(section_code)` → if feasible, skip scoring and use baseline |

### Instructor / student overlap detection (commit 3)

Reuses existing fields already maintained by the placer:
- `instructor_schedule: dict[instructor_id, set[(day, start)]]` — already populated during scoring
- `student_schedule: dict[student_id, set[(day, start)]]` — already populated (used by scoring's overlap penalty)

PR3 only *observes* these structures to emit a trace — no new bookkeeping.

---

## Pre-condition / rollback

- **Pre-condition for merge:** scenario pack runs clean against the acceptance bar (all 6 criteria).
- **Rollback:** `TIMETABLE_PR3_DECISION_TRACE_ENABLED=false` returns empty `decision_trace={}` without changing placement decisions. `TIMETABLE_PR3_WARM_START_ENABLED=false` reverts to cold-start behaviour. Both flags respond to env vars so rollback is a config change, not a deploy.

---

## Deferred follow-ups (not PR3)

- UI surface for `decision_trace` (operator panel showing "why this slot, not that one"). Built on the management-command payload shape.
- Full search-tree logging (explicitly out-of-scope here).
- Per-student timetable generation (already deferred from earlier PRs).
- Weight / objective-function tuning — separate PR once the trace surfaces which penalties dominate.
- V2 lock-aware pruning — deferred until metric evidence of churn.
- Removing deprecated `lecture_room_reject_due_to_buffer_count` — separate PR (PR2 follow-up).

---

## Sign-off

- [ ] ChatGPT reviews this DoR and approves scope / non-scope / acceptance bar. **Revision 1 sent 2026-04-20** with seven amendments applied: (1) `STUDENT_OVERLAP` renamed to `STUDENT_CONFLICT` (cohort framing, not geometric). (2) Trace-disabled schema stability clause added — `decision_trace: {}` always present. (3) Baseline source restricted to in-memory / caller-supplied; DB persistence explicitly deferred. (4) Top-3 alternatives fixed, not parameterised. (5) V2 trace-capture scope tightened: greedy MUST, local-search MAY, deeper phases SHOULD NOT. (6) Warm-start semantics reworded as retention, not scoring bypass. (7) Acceptance bar #4 relaxed — exact-zero asserted on canonical fixture only; broader pack must be "low and explainable."
- [ ] Commit 1 (failing tests + scenario pack) lands next.
