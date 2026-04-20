# PR0 — Scenario pack freeze

Track: `planner-refactor-q2-2026`
PR: PR0 — externalise capacity buffer

## Baseline

- `baseline_commit`: `47549c86a1feff33239363e99886f4af62a5e2b8`
- `baseline_tag`: `baseline/pre-planner-refactor-2026-04-20`

## Flags

- `flags_on`: (none — PR0 introduces no runtime flag)
- `flags_off`: (none)

Setting introduced: `TIMETABLE_CAPACITY_BUFFER` — a numeric config, not a boolean
flag. Default `1.1` matches the hardcoded value it replaces, so behaviour is
identical to baseline unless an operator explicitly overrides it.

## Comparison basis

Goal: prove **no algorithmic behaviour change** at the default value.

- Full existing test suite (162 tests) must still pass.
- `assign_rooms_to_board()` output `{assigned, unassigned}` must be identical
  to baseline for any board when `TIMETABLE_CAPACITY_BUFFER = 1.1`.
- `auto_place_board()` placements must be identical to baseline when
  `TIMETABLE_CAPACITY_BUFFER = 1.1`.

## New observable surface

- `assign_rooms_to_board()` returns additionally:
  - `capacity_buffer: float`
  - `room_reject_due_to_buffer_count: int` — rooms that would have fit at
    `buffer = 1.00` but were rejected at the current buffer.
- `auto_place_board()` returns additionally:
  - `capacity_buffer: float`

## Offline comparison helper

Management command `timetable_buffer_compare --board-id N` runs a read-only
simulation of `assign_rooms_to_board` at buffer `1.00` and at the current
setting, printing per-board deltas in assigned / unassigned counts. Does
NOT persist any changes to the database.

## Rollback

`git revert` the PR0 commit; `TIMETABLE_CAPACITY_BUFFER` setting read
falls back to `1.1` default even if the setting is missing, so a partial
revert remains functional.
