# PR2 — Scenario pack freeze

Track: `planner-refactor-q2-2026`
PR: PR2 — room oracle + typed infeasibility reporting
DoR: [`docs/PR2-DOR.md`](../../docs/PR2-DOR.md) (signed off 2026-04-20)

## Base

- `base_commit`: `0e5eab1` — master after PR1 ships (prayer + lock enforcement gates live, both flags default False).
- Branch: `refactor/pr2-room-oracle-infeasibility-reporting`.

## Flags

- `TIMETABLE_PR2_ROOM_ORACLE_ENABLED` — default False. Gates the staged feasibility oracle's pre-placement checks.
- `TIMETABLE_PR2_CAPACITY_SIM` — default False. Gates Stage 3 (sorted-capacity simulation). Rolled on separately once Stage 1+2 are clean.

With both flags False, room-selection decisions are unchanged. The observable change is that the four silent-UNASSIGNED call-sites emit structured `RoomFailureReason` records instead of bare `"UNASSIGNED"` strings — a reporting-only behaviour delta.

## Reason-code alphabet

Six stable sentinels exposed from `core.services.timetable_room_oracle`:

| Code | Meaning |
|---|---|
| `NO_ROOM_CAPACITY` | No room in the eligible pool has enough raw capacity for the section (demand × buffer) |
| `NO_ROOM_GENDER` | No room matches the section's required gender |
| `NO_ROOM_TYPE` | No room of the required type (lecture / lab) is eligible |
| `ROOM_OCCUPIED` | Eligible rooms exist but every one is busy at (day, start_time) |
| `ROOM_BUFFER_REJECT` | A room fits raw capacity but fails the configured buffer multiplier |
| `ROOM_HEURISTIC_MISMATCH` | The `duration > 80` / `cr == 4` lab-classification heuristic and the `is_lab_course` heuristic disagree on this section. Observation only — no placement change in PR2. |

## Reason-code semantics

Each `RoomFailureReason` carries `code`, `day`, `start_time`, `end_time`, `course_code`, `section_code`, and a `context` dict with code-specific detail (e.g. `{needed, best_capacity, buffer}` for `ROOM_BUFFER_REJECT`).

## Call-site swap targets (PR2 commit 3)

Four silent-UNASSIGNED sites swapped to structured emission:

| # | File:line | Function |
|---|---|---|
| 1 | `core/services/timetable_autoplace.py:1102-1104` | `auto_place_board` — `best_option is None` |
| 2 | `core/services/timetable_autoplace.py:1162` | `auto_place_board` — fallback `assigned_room = "UNASSIGNED"` |
| 3 | `core/services/timetable_rooming.py:324` | `assign_rooms_to_board` — `tracker.assign_best_fit` returned None |
| 4 | `core/services/timetable_room_repair.py:118-119` | `try_repair_rooms_locally` — section unable to place |

## Staged oracle (PR2 commit 4)

Pre-placement check wired into `assign_rooms_to_board` and `auto_place_board` before their main loops:

1. **Stage 1 — metadata existence.** Does at least one room match gender AND type AND capacity-with-buffer? Otherwise emit the matching `NO_ROOM_*` reason and skip.
2. **Stage 2 — buffer-aware rejection accounting.** If a room fits raw but fails buffer, emit `ROOM_BUFFER_REJECT` and bump `buffer_only_rejects`.
3. **Stage 3 (flag-gated).** Sorted-capacity simulation — off by default behind `TIMETABLE_PR2_CAPACITY_SIM`.

`RoomProfile` dataclass gains a `gender` field in the same commit (decision (i) from DoR sign-off).

## New observable payload (PR2 commit 5)

Planner return dicts (`auto_place_board`, `assign_rooms_to_board`) gain:

- `unplaced_count: int`
- `room_failures: list[dict]` — each entry a `RoomFailureReason.to_dict()`
- `buffer_only_rejects: int` — carved out of today's `lecture_room_reject_due_to_buffer_count` counter
- `heuristic_mismatch_count: int`
- `suggested_relaxation: list[dict]` — optional hints (e.g. `{placement_id, hint: "fits at buffer=1.0"}`)

## Reporting surface (PR2 commit 5)

A single Django management command reads the latest persisted planner result and summarises:

- total failures
- counts by `reason_code`
- `buffer_only_rejects`
- `heuristic_mismatch_count`
- sample affected placements (first N)

No admin panel, no CSV export, no exam-panel coupling. UI is deferred to a later PR on top of this command's payload shape.

## Fixtures (one JSON per reason, plus parity)

Order matches the seven scenario tests in `tests/test_pr2_room_oracle.py`:

1. [`fixtures/pr2_buffer_reject.json`](fixtures/pr2_buffer_reject.json) — `ROOM_BUFFER_REJECT`
2. [`fixtures/pr2_wrong_gender.json`](fixtures/pr2_wrong_gender.json) — `NO_ROOM_GENDER`
3. [`fixtures/pr2_wrong_type.json`](fixtures/pr2_wrong_type.json) — `NO_ROOM_TYPE`
4. [`fixtures/pr2_all_rooms_occupied.json`](fixtures/pr2_all_rooms_occupied.json) — `ROOM_OCCUPIED`
5. [`fixtures/pr2_no_feasible_room.json`](fixtures/pr2_no_feasible_room.json) — `NO_ROOM_CAPACITY`
6. [`fixtures/pr2_parity.json`](fixtures/pr2_parity.json) — obvious feasibility; every oracle helper returns `None`
7. [`fixtures/pr2_heuristic_mismatch.json`](fixtures/pr2_heuristic_mismatch.json) — `ROOM_HEURISTIC_MISMATCH` (observational)

Tests construct the data programmatically; the JSON files document the semantic shape for traceability (same convention as PR1).

## Test files

- [`tests/test_pr2_room_oracle.py`](../../tests/test_pr2_room_oracle.py) — API-shape tests (Section A, unlock at commit 2) + seven scenario tests (Section B, unlock at commit 4).
- [`tests/test_pr2_silent_unassigned_sites.py`](../../tests/test_pr2_silent_unassigned_sites.py) — one test per silent-UNASSIGNED site (DoR acceptance criterion #1).

Both files import from `core.services.timetable_room_oracle`; commit 1 leaves them failing at collection (`ModuleNotFoundError`). Commit 2 creates the module and Section A turns green. Commits 3–5 progressively unlock the remaining tests.

## Acceptance gates (DoR criteria, verbatim)

1. **Zero silent UNASSIGNED at the four call-sites** — targeted per-site tests in `test_pr2_silent_unassigned_sites.py`. No grep test.
2. **Every unassigned placement carries a `reason_code`** in `room_failures`.
3. **Buffer-only rejects separately counted** — `buffer_only_rejects == 1` on `pr2_buffer_reject.json`.
4. **Parity preserved for feasible rooming** — `pr2_parity.json` stays at `unplaced_count == 0`, `room_failures == []`.
5. **p95 wallclock ≤ 1.3× master baseline** — measured via a lightweight benchmark harness added around commit 5 (not in commit 1).
6. **Feasible-rate ≥ 99% of baseline** unless every new failure is explained by a newly-surfaced typed reason.

## Rollback

- Flags → False restores pre-PR2 oracle behaviour exactly.
- The silent-to-typed swap (commit 3) has no flag — it's pure payload enrichment. Revert that single commit if rollback of the swap is ever needed.
- `git revert` on the PR2 merge commit is always available as a full rollback.
