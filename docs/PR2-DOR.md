# PR2 — Definition of Ready

**Branch:** `refactor/pr2-room-oracle-infeasibility-reporting`
**Base:** master @ `0e5eab1` (post-PR1 prayer/lock enforcement)
**Theme:** Room-feasibility visibility. Kill silent UNASSIGNED, surface typed failure reasons, add cheap staged feasibility checks.

This document is the pre-code agreement on scope, non-scope, acceptance bar, and commit sequence. No code lands on this branch until the DoR is signed off.

---

## Why this PR, why now

PR1 added two placement-legality gates (prayer, lock) with explicit `RejectionReason` payloads. The rooming code downstream still uses the old pattern — a section that fails to get a room is marked `"UNASSIGNED"` with no typed reason, and diagnostics degrade to a single counter (`lecture_room_reject_due_to_buffer_count`). Today a planner run can report e.g. "skipped: 7" without telling an operator whether those 7 sections failed on gender, capacity, buffer, type, or the brittle `duration > 80 and cr == 4` lab-classification heuristic.

PR2 surfaces that lost signal without changing the planner's algorithmic shape. Nothing in the objective function, strategy selection, or overall placement loop changes. We replace silent fallthrough with structured emission, add a cheap pre-placement oracle so obvious "no feasible room exists anywhere" cases fail fast with a named reason, and expose `room_failures` in the result payload so downstream reporting can group/count/explain.

---

## In scope

### 1. Typed rooming failure reasons

Introduce a `RoomFailureReason` dataclass (frozen, JSON-serialisable, same shape as PR1's `RejectionReason`) with the following stable code sentinels:

| Code | Meaning |
|---|---|
| `NO_ROOM_CAPACITY` | No room in the eligible set has capacity >= demand (with buffer applied) |
| `NO_ROOM_GENDER` | No room matches the section's required gender (M/F) |
| `NO_ROOM_TYPE` | No room of the required type (lecture / lab) is eligible — e.g. lab-only sections in a lecture-only programme zone |
| `ROOM_OCCUPIED` | Rooms of the right gender/type/capacity exist but all are occupied at the required (day, start_time) |
| `ROOM_BUFFER_REJECT` | A room fits raw capacity but fails the configured buffer multiplier (e.g. enrolment 30, capacity 32, buffer 1.1 → needs 33) |
| `ROOM_HEURISTIC_MISMATCH` | The current `duration > 80 and cr == 4` lab-classification heuristic rejected a room that would otherwise fit (2-credit long-lecture case). Surfaces the heuristic's blast radius before anyone replaces it. |

Each reason carries: `code`, `day`, `start_time`, `end_time`, `course_code`, `section_code`, and a `context` dict for failure-specific detail (e.g. `{"needed": 33, "best_capacity": 32, "buffer": 1.1}` on `ROOM_BUFFER_REJECT`).

### 2. Cheap staged room-feasibility oracle

Pre-placement check (before the main placement loop tries candidates). Lightweight — existence-level only:

- **Stage 1 — existence by metadata.** Is there at least one room in the scope with matching gender AND type AND capacity >= demand (buffer-scaled)? If no room matches on metadata alone, emit the appropriate `NO_ROOM_*` reason and skip the section without entering the placement loop.
- **Stage 2 — buffer-aware rejection accounting.** When a room fits raw but fails buffer, increment a separate counter AND emit `ROOM_BUFFER_REJECT`. Today buffer-only rejects are lumped into a single counter; the oracle splits them out per placement.
- **Stage 3 (optional, flag-gated).** Sorted-capacity simulation — walk candidate rooms smallest-first to verify at least one ordering places the section. Pure optimism check; does not replace the real assigner. Off by default; behind `TIMETABLE_PR2_CAPACITY_SIM` flag.

**Explicit non-goal:** no bipartite matching, no Hopcroft-Karp, no global room solver. Those belong in a future PR.

### 3. Structured infeasibility payload

Planner result (`auto_place_board`, `assign_rooms_to_board`) gains:

```python
{
    # existing keys preserved — placed, skipped, placements, capacity_buffer,
    # pr1_prayer_rejections, pr1_lock_rejections …

    "unplaced_count": int,
    "room_failures": list[dict],          # list[RoomFailureReason.to_dict()]
    "buffer_only_rejects": int,            # carved out of today's lumped counter
    "heuristic_mismatch_count": int,       # how often cr==4/duration>80 heuristic blocked a fit
    "suggested_relaxation": list[dict],    # optional hints — e.g. {"placement_id": …, "hint": "fits at buffer=1.0"}
}
```

Every section that ends unplaced carries a `reason_code` in its placement dict (or in `room_failures` keyed by `(course_code, section_code)`). **Zero silent UNASSIGNED paths by the end of this PR.**

### 4. Management / reporting surface

Extend an existing diagnostics surface — no new UI, no new admin views. Acceptable shapes:

- A management command or diagnostics endpoint that answers:
  - "how many placements failed by each room reason"
  - "how many were buffer-only rejects"
  - "how many were gender/type shortages vs true capacity shortages"
- Minimum bar: the payload fields above are read by existing diagnostics so a future UI PR can render them without back-fitting.

### 5. Tests

Fixture-backed, isolated, and exercising each reason code at least once:

1. **Fits raw but fails buffer** — section 30 students, only room 32 capacity, buffer 1.1 → `ROOM_BUFFER_REJECT`
2. **Wrong gender only** — female section, only male rooms at slot → `NO_ROOM_GENDER`
3. **Wrong room type only** — lab required, only lecture rooms available → `NO_ROOM_TYPE`
4. **All rooms occupied at slot** — eligible rooms exist, but every one busy at (day, start_time) → `ROOM_OCCUPIED`
5. **No feasible room at all** — demand exceeds every room's capacity with buffer → `NO_ROOM_CAPACITY`
6. **Parity case** — obviously feasible rooming assigns every placement; `unplaced_count == 0`, `room_failures == []`
7. **Heuristic mismatch** — 2-credit 100-min meeting that the current `cr == 4` guard rejects; assert `ROOM_HEURISTIC_MISMATCH` surfaces without the section actually moving to a different room (observation only in this PR)

Existing rooming tests (`test_timetable_capacity_buffer.py`, `test_exam_room_assignment.py`, `test_timetable_strategies.py`) must continue to pass unmodified.

---

## Out of scope

- **CP-SAT room subproblem.** No OR-Tools model for rooming in this PR.
- **Room UI redesign.** No admin changes, no new views. Diagnostics use existing payload surfaces.
- **Objective-function weight changes.** Room penalties in the strategy scorer are untouched.
- **Retiring the `duration > 80 and cr == 4` heuristic.** PR2 *surfaces* its blast radius via `ROOM_HEURISTIC_MISMATCH`. The replacement (a proper room-type resolver) belongs in a later PR where the measured mismatch rate justifies the design.
- **Room matching optimiser.** No bipartite matching, no capacity-sort-then-pack global solver.
- **Capacity-buffer flag redesign.** The current `capacity_buffer` parameter keeps its semantics.
- **Cross-cutting typecheck cleanup.** The mypy top-6 backlog PR is separate.
- **pytest 9.x upgrade.** Separate PR (GHSA-6w46-j5rx-g56g ignored until then).

---

## Acceptance bar (measurable, CI-enforceable)

Before merge:

1. **Zero silent UNASSIGNED paths.** Every `"UNASSIGNED"` emission site today has a typed `reason_code` by the end of this PR. Enforced by a dedicated test that greps the codebase.
2. **Every unassigned placement carries a `reason_code`** in `room_failures`. Test asserts this on a synthetic-infeasible scenario.
3. **Buffer-only rejects separately counted.** `buffer_only_rejects` == expected count on the buffer-only fixture.
4. **Parity preserved for obviously feasible rooming.** PR1's parity scenario pack still places exactly as before; `unplaced_count == 0`, `room_failures == []`.
5. **Performance bar.** Planner wallclock p95 on the PR2 scenario pack ≤ 1.3× current master baseline. Measured via an existing perf harness or a new lightweight one if none fits.
6. **Feasible-rate floor.** Overall feasible-rate ≥ 99% of current baseline on the scenario pack, UNLESS every new failure is explained by a newly surfaced typed room reason (i.e. the oracle is correctly catching infeasibilities that today go silent).

---

## Internal commit order

ChatGPT's recommended sequence, adopted verbatim:

| # | Commit | What lands |
|---|---|---|
| 1 | Failing tests + scenario pack | The 7 test cases above as `test_pr2_*.py`, fixture JSONs under `snapshots/planner-refactor-2026-04-20/fixtures/pr2_*.json`, skeleton `RoomFailureReason` import that doesn't exist yet (tests fail by design with ImportError — documents the contract) |
| 2 | Typed `RoomFailureReason` structure | `core/services/timetable_room_oracle.py` with dataclass, code sentinels, `.to_dict()`, unit tests for each code, flag read helper (`is_room_oracle_enabled()`), no wiring yet |
| 3 | Replace silent UNASSIGNED with structured emission | Edit the 4 silent sites (autoplace.py:1102–1104, autoplace.py:1162, rooming.py:324, room_repair.py:118–119) to emit `RoomFailureReason`; accumulate into `room_failures` in the return payload |
| 4 | Cheap staged room oracle | Add Stage 1 existence check + Stage 2 buffer-aware split; wire into `assign_rooms_to_board` and `auto_place_board` before their main loops |
| 5 | Payload / reporting surface | Extend return dicts with `unplaced_count`, `room_failures`, `buffer_only_rejects`, `heuristic_mismatch_count`, `suggested_relaxation`; wire into existing diagnostics |
| 6 | Promotion note | `docs/PR2-PROMOTION-NOTE.md` — scope recap, flag state, acceptance-bar numbers measured on the scenario pack, pre-condition checklist for rollout, deferred follow-ups |

Commits 1–2 are green-behind-a-flag (default OFF). Commit 3 does the behavioural swap for the silent sites. Commits 4–5 are additive. Commit 6 is docs only.

---

## Code anchors (for commits 3–5)

Grounded findings from the pre-DoR survey, for the implementer:

### Silent UNASSIGNED sites (commit 3)

| File:line | Context |
|---|---|
| [core/services/timetable_autoplace.py:1102-1104](core/services/timetable_autoplace.py) | `best_option is None` after scoring; increments `total_skipped`, no reason |
| [core/services/timetable_autoplace.py:1162](core/services/timetable_autoplace.py) | Fallback sets `assigned_room = "UNASSIGNED"` with no record |
| [core/services/timetable_rooming.py:324](core/services/timetable_rooming.py) | `tracker.assign_best_fit()` returned None; sets `p.room = "UNASSIGNED"`, bumps only `lecture_room_reject_due_to_buffer_count` |
| [core/services/timetable_room_repair.py:118-119](core/services/timetable_room_repair.py) | Section unable to place; returns `False` with no per-section detail |

### The `cr == 4 and duration > 80` heuristic (commit 3 emits `ROOM_HEURISTIC_MISMATCH` observationally)

| File:line | Function | Code |
|---|---|---|
| [core/services/timetable_autoplace.py:1008](core/services/timetable_autoplace.py) | `auto_place_board` scoring | `rtype = "lab" if (duration > 80 and is_lab_course) else "lecture"` |
| [core/services/timetable_autoplace.py:1131](core/services/timetable_autoplace.py) | `auto_place_board` persistence | same |
| [core/services/timetable_rooming.py:305](core/services/timetable_rooming.py) | `assign_rooms_to_board` | `room_type = "lab" if (duration > 80 and cr == 4) else "lecture"` |
| [core/services/timetable_room_repair.py:97](core/services/timetable_room_repair.py) | `try_repair_rooms_locally` | counts lectures (`≤80 min`) then picks by majority — inverse of the above |

`cr` = credit rating (1–4). The brittleness: 2-credit 100-minute long lectures get misclassified as labs by the bare `duration > 80` check; the `cr == 4` guard in `rooming.py:305` papers over it, but `autoplace.py:1008/1131` use `is_lab_course` instead — divergent rules for the same question.

### Room metadata available to the oracle (commit 4)

Room dict (from `get_programme_rooms()`): `room_code, capacity, room_type, section (M/F), wing, building`.
`RoomProfile` dataclass: `room_id, capacity, room_type` only — PR2 may need to extend if gender is consulted directly. Deferred design question for commit 4.

---

## Flag plan

| Flag | Default | Controls |
|---|---|---|
| `TIMETABLE_PR2_ROOM_ORACLE_ENABLED` | `False` | Gates the staged oracle's pre-placement checks. When off, only the silent-UNASSIGNED-to-typed-reason replacement (commit 3) runs — observation only. |
| `TIMETABLE_PR2_CAPACITY_SIM` | `False` | Gates Stage 3 (sorted-capacity simulation). Off for the PR; rolled on separately once Stage 1+2 run clean on real data. |

When **both** flags are off, the only observable change is that `room_failures` is populated and `"UNASSIGNED"` string literals are replaced by structured reasons. No placement decision changes.

---

## Pre-condition / rollback

- **Pre-condition for merge:** scenario pack runs clean against the acceptance bar (all 6 criteria).
- **Rollback:** flags to False restores pre-PR behaviour for the oracle. The silent-to-typed swap (commit 3) has no flag — it's pure payload enrichment and should never need rollback. If it did, git revert the single commit cleanly.

---

## Deferred follow-ups (not PR2)

- Replace the `duration > 80 and cr == 4` heuristic with a proper room-type resolver, informed by the measured `ROOM_HEURISTIC_MISMATCH` rate PR2 surfaces.
- Bipartite-matching or CP-SAT room assigner (the "global oracle" ChatGPT explicitly ruled out of PR2 scope).
- UI surface for `room_failures` (admin panel, operator view). Once the payload is stable, a UI PR can render it.
- `RoomProfile` schema extension (gender field) if commit 4 finds the current shape insufficient — handled as an intra-PR design call when it comes up.

---

## Sign-off

- [ ] ChatGPT reviews this DoR and approves scope / non-scope / acceptance bar.
- [ ] Commit 1 (failing tests + scenario pack) lands only after sign-off.
