# PR-1 — Board Persistence: Definition of Ready

Part of the 2026-07 timetable-engine strengthening plan (see the engine
review in agent memory). This is the first structural PR: a single module
that owns writing the two on-disk representations of a board together, so
they can never drift, and one snapshot/restore that covers both.

## Problem (verified against current code, 2026-07-12)

A board's schedule is stored **twice**:

- `SectionPlacement` — board-scoped, lockable (`is_locked`), **no instructor
  field**. Unique on `(board, term_section, day, start_time)`.
- `TermSectionMeeting` (TSM) — section-scoped, carries `instructor`, `room`,
  `building`, `floor_wing`. The instructor lives *only* here. Unique on
  `(term_section, day, start_time, end_time, room, instructor)`.

TSM times/instructor are read by conflict masks (`timetable_workspace.py:261`),
exports (`timetable_export.py`, `timetable_per_plan_export.py`), the exam room
lookup, repair eligibility, and the greedy clash preload
(`timetable_autoplace.py:1299`). So any path that mutates one representation
without the other silently corrupts conflict detection and exports.

### Confirmed defects this PR closes

| # | Defect | Site (current code) |
|---|--------|---------------------|
| a | Scenario rollback restores placements only; TSM left at the failed run's state | `timetable_v2_runner.py:24-57` (`_PLACEMENT_SNAPSHOT_FIELDS`, `restore_scenario_placements`), rollback at `:209`, `:227-229` |
| b | `full_rebuild` reset deletes **locked** placements and never clears TSM | `planner_job_runner.py:179-193` (`_clear_scenario_placements`) — asymmetric with the correct `timetable_optimizer_v2.py:619-644` |
| d | Errored optimise runs are marked `SUCCEEDED` | `planner_job_runner.py:275` — set unconditionally though `run_v2_optimisation_guarded` returns `{"error": ...}` (`timetable_v2_runner.py:211,214`) |

Empirical corroboration: scenario 639 carries two duplicate `GSE1` meeting
rows (a cross-board elective persisted twice) — a live instance of the drift
class, and the source of the "70 clashes" the evaluator reports on real runs.

## Scope

**In (PR-1):**
1. New module `core/services/timetable_board_persistence.py`:
   - `reset_scenario(scenario_id, *, keep_locked=True) -> ResetResult`
     — delete placements + their meetings for a fresh run; locked sections'
     rows survive when `keep_locked` (their meetings too). This is exactly the
     current *correct* `_reset_unlocked_placements`, promoted to the module and
     generalised (`keep_locked=False` = hard wipe, used by nothing yet).
   - `snapshot_scenario(scenario_id) -> ScenarioSnapshot` — capture placements
     **and** meetings (incl. `instructor`, `room`, `building`, `floor_wing`).
   - `restore_scenario(scenario_id, snapshot) -> None` — atomically restore
     both tables (delete-all + bulk-recreate inside one `transaction.atomic`).
2. Point `timetable_v2_runner` snapshot/restore at the module → fix (a).
3. Point `planner_job_runner._clear_scenario_placements` at
   `reset_scenario(keep_locked=True)` → fix (b): stop destroying locks, clear
   TSM so the adaptive rebuild starts clean.
4. `run_planner_job`: when the result dict carries `error`, set `FAILED` with
   the message instead of `SUCCEEDED` → fix (d).
5. Redirect `timetable_optimizer_v2._reset_unlocked_placements` to the module
   (single reset implementation; behaviour identical).

**Out (explicitly deferred, next PR):**
- The success-path TSM drift in `persist_section_states_to_scenario`
  (`timetable_optimizer_v2.py:506-602` never writes TSM) and migrating the
  greedy/solver/SA/rebalance/cap-repair/compaction persists onto one shared
  write-through. These are the hottest paths and get their own parity-tested
  PR-1b. PR-1's snapshot/reset already make that drift *recoverable* (rollback
  restores TSM) and *transient* (reset clears TSM before each rebuild), so
  deferring is safe.
- Defect (c) (greedy clash/cap map inert on rebuild) is a constraint-plumbing
  bug, addressed by the ConstraintEngine PR-2, not persistence.

## Invariant (the module's contract)

> After any `reset_scenario` / `restore_scenario`, for every scenario-owned
> `TermSection`, the set of its `TermSectionMeeting` `(day, start, end)` tuples
> equals the set of its `SectionPlacement` `(day, start)` slots on the
> scenario's boards, and each meeting retains the `instructor` it had before.

`restore_scenario(snapshot_scenario(s))` is an exact round-trip (identity) for
both tables including `instructor`.

## Design decisions

- **Update-in-place, not delete-recreate, for future write-throughs.**
  `apply_primary_instructor` only re-fans *link-based* instructors (no-op for
  free-text), so recreating TSM rows would drop free-text instructors. Snapshot
  therefore captures `instructor` explicitly; restore recreates rows verbatim
  (lossless). The deferred write-through (PR-1b) will `.update()` matched rows
  like `timetable_instructor_cap_repair._relocate` does.
- **Scope symmetry.** Placements snapshot/reset by `board__scenario_id`;
  meetings by `term_section__scenario_id`. Global (scenario-null) sections are
  never mutated by the optimiser, so they are intentionally out of scope for
  both — matching the current reset's scope.
- **No feature flag.** These are correctness fixes; a flag defaulting to the
  buggy path would only preserve corruption. Safety comes from tests + the fact
  that `reset_scenario(keep_locked=True)` is byte-for-byte the current correct
  reset. The one behaviour change users can observe — `full_rebuild` now
  preserves locks — is a fix that makes the main workspace consistent with the
  split workspace (which already preserves them).
- **Atomicity.** Restore and reset run inside `transaction.atomic()` so a crash
  mid-write cannot leave a half-updated board.

## Test plan (TDD — tests written to fail against current code first)

`tests/test_board_persistence.py`:
1. `reset_scenario(keep_locked=True)` deletes unlocked placements + their TSM,
   preserves locked placements **and** their TSM.
2. `reset_scenario` parity: same DB end-state as the current
   `_reset_unlocked_placements` on a mixed locked/unlocked scenario.
3. `snapshot`→mutate→`restore` round-trips placements **and** TSM instructor
   (the (a) regression: prove TSM is restored, not just placements).
4. `restore_scenario` is atomic (a raising bulk-create leaves the pre-restore
   state — exercised via a forced failure).
5. Global-section TSM is untouched by reset/restore.

`tests/test_pr7_async_planner.py` (extend):
6. `full_rebuild` preserves locked placements (the (b) regression).
7. A job whose runner returns `{"error": ...}` ends `FAILED`, not `SUCCEEDED`,
   with the message surfaced (the (d) regression).

Gate: full suite green (currently 883/2), ruff + bandit clean (`SKIP=mypy` per
project convention), `manage.py check` clean.

## Rollback

Pure additive module + four call-site redirects. Revert = `git revert` the
merge; no migration, no schema change, no data backfill.
