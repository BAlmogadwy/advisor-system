# PR1 — Scenario pack freeze

Track: `planner-refactor-q2-2026`
PR: PR1 — prayer/lock enforcement

## Baseline

- `baseline_commit`: `d0c6739` (PR0 merged — externalised capacity buffer)
- `baseline_description`: PR0 preserved planner behaviour at default settings
  (TIMETABLE_CAPACITY_BUFFER = 1.1); parity for PR1 is measured against
  d0c6739, not against pre-PR0 47549c8.

## Flags

- `flags_on` (feature being introduced, default OFF in config):
  - `TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE`
  - `TIMETABLE_ENFORCE_LOCKS`
- `flags_off` (baseline comparison run):
  - Both of the above set to False.

Both flags default to False on merge so prod behaviour is unchanged.

## Interval semantics

PR1 treats meeting and prayer windows as half-open intervals `[start, end)`.
Overlap is defined as `a.start < b.end AND a.end > b.start`. Exact boundary
touch (meeting ends 12:00, prayer starts 12:00) is legal.

## Rule semantics

### Prayer rule (gated on `TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE`)

When enabled, any candidate placement whose meeting window overlaps any
prayer window on that day (per the half-open-interval rule) is rejected
with code `PRAYER_OVERLAP`. An empty prayer schedule is a no-op.

### Lock rule (gated on `TIMETABLE_ENFORCE_LOCKS`)

When enabled:

1. **Preload**: on planner entry, ingest all `SectionPlacement` rows with
   `is_locked=True` for the board/scenario into the `RoomTracker` usage
   map and into any internal cell-occupied / course-already-placed
   structures.
2. **Skip**: the auto-placement iteration must not attempt to re-place or
   overwrite a locked placement.
3. **Telemetry**: a candidate that would have collided with a locked cell
   emits a `LOCK_RESPECT` rejection on the return payload. This is a
   telemetry signal, not the enforcement mechanism.

## Call sites

Validator wired into:

- `auto_place_board()` — invoked in the regular/overflow/pin placement
  paths, before finalising a slot.
- `build_section_states_for_scenario()` (V2 optimiser) — candidate-gen
  filter that short-circuits infeasible states early.

Validator explicitly NOT wired into `assign_rooms_to_board()` — by the
time rooming runs, placement legality has already been decided. Rooming
may carry forward rejection metadata from upstream but does not enforce
these rules.

## New observable surface

Planner return dict gains:

- `pr1_prayer_rejections: list[dict]` — each entry
  `{day, start_time, end_time, course_code, reason, context}`.
- `pr1_lock_rejections: list[dict]` — same shape.

Both lists are empty when the corresponding flag is False. No UI surface
in PR1 (KPI tiles + drilldown move to PR4 decision trace).

## Scope deliberately excluded

- No dashboard UI — moved to PR4.
- No CP-SAT constraint additions — PR1 filters at candidate-gen only; solver
  constraints come later if needed.
- No typed rooming failure reasons (`NO_ROOM_CAPACITY` etc.) — PR2.

## Safety claim

PR1 ships both flags defaulted to False. On merge, default production
semantics are identical to `d0c6739`. Operators enable the flags per
scenario to evaluate behaviour before wider rollout.

## Rollback

`git revert` the PR1 merge commit. Because both flags are config-gated
and default False, leaving the PR1 code in place (without revert) is
also a safe fallback so long as the flags stay False.

## Fixtures

- [`fixtures/pr1_prayer_basic.json`](fixtures/pr1_prayer_basic.json) —
  shape reference for the prayer-straddling scenario.
- [`fixtures/pr1_lock_respect.json`](fixtures/pr1_lock_respect.json) —
  shape reference for the locked-placement + candidate-collision scenario.
- [`fixtures/pr1_parity_baseline.json`](fixtures/pr1_parity_baseline.json) —
  parity assertions against baseline `d0c6739`.

Tests construct the data programmatically; the JSON files document the
semantic shape for traceability.

## Acceptance gates (for the PR1 promotion note)

- `feasible_rate` ≥ 97% of baseline across the validation pack.
- `planner_wallclock_p95` ≤ 1.5× baseline.
- `hard_lock_respect_rate` ≥ 99.5% (no locked placement moved).
- `parity_when_flags_off` = 100% (identical placement set vs baseline).
