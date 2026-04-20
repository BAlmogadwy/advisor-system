# PR1 — Prayer / Lock Enforcement Promotion Note

**Branch:** `refactor/pr1-prayer-lock-enforcement`
**Base:** `master @ d0c6739`
**Refactor-Track:** planner-refactor-q2-2026
**Rollout stance:** flags default OFF → behaviourally a no-op until operators flip them per scenario.

This note captures scope, the honest limits of the current implementation, and
the follow-up work that PR1 explicitly defers. Read before enabling either
flag in a live environment.

---

## 1. Scope

PR1 introduces two validator-gated rules plus the planner wiring that enforces
them. Both rules are **default OFF** and both are read from Django settings:

| Setting                                   | Default | Purpose                                                        |
| ----------------------------------------- | ------- | -------------------------------------------------------------- |
| `TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE`   | `False` | Enable configurable prayer-window overlap rejection.           |
| `TIMETABLE_ENFORCE_LOCKS`                 | `False` | Enable structural respect for locked `SectionPlacement` rows.  |
| `TIMETABLE_PRAYER_WINDOWS`                | `[]`    | List of `{day, start_time, end_time}` entries (half-open).     |

### What PR1 ships

- `core/services/timetable_validation.py` — pure, flag-gated helpers:
  `prayer_overlap_rejection`, `lock_rejection`, `validate_candidate`,
  plus `RejectionReason`, sentinels `PRAYER_OVERLAP` / `LOCK_RESPECT`,
  `get_prayer_windows`, `_same_day`, a guarded `_to_min`.
- `auto_place_board` wiring:
  - **Prayer:** rejects any option whose meeting overlaps a configured
    prayer window (half-open intervals, case-insensitive day compare).
  - **Lock:** structural preload. Locked rows are seeded into
    `placed_masks`, `placed_schedule`, `all_placed_masks`,
    `slot_density`, and `room_tracker.usage` **before** round-robin
    placement begins. The existing `already = count()` path then skips
    those section indices automatically — no parallel "locked cells"
    branch.
- V2 optimiser candidate-gen filter in `timetable_local_search_v2`:
  drops repattern/swap moves whose target pattern overlaps any configured
  prayer window, reusing **the same helpers** as `auto_place_board` (no
  forked rule, no forked day-normalisation, no forked interval
  semantics).
- New payload surface on `auto_place_board` return:
  `pr1_prayer_rejections: list[dict]` and `pr1_lock_rejections: list[dict]`.
- Single per-run info log at `auto_place_board` return that records
  `board_id`, `placed`, `skipped`, both flags' on/off state, and both
  rejection counts.
- Unit tests: 13 prayer, 7 lock. E2E tests: 5 (parity + behaviour paths).

### What PR1 does NOT ship

- **No retirement of the legacy `_start_is_blocked` 11:35–12:59 filter.**
  Coexistence is intentional: retiring the legacy filter is a behavioural
  change and belongs in its own PR where the diff can be evaluated on its
  own. See §3 below for the implication.
- **No V2-side lock filter.** See §4.
- **No UI toggle** to flip the flags. Operators set them via Django
  settings (or the runtime settings module appropriate to the
  deployment). UI/admin surface comes in PR3.
- **No dashboard surface** for `pr1_*_rejections`. The fields land in
  the return payload and the log line; downstream rendering is a
  follow-up once operators have confirmed the data is useful.

---

## 2. Flags: default OFF is the whole rollout story

PR1 is explicitly designed so that enabling the code path with both flags
off produces **byte-identical planner output** to the pre-PR1 baseline.
The parity test in `tests/test_pr1_end_to_end.py::test_parity_flags_off_matches_baseline`
locks this in: the return payload's baseline keys (`placed`, `skipped`,
`placements`, `capacity_buffer`) are preserved, and both new rejection
lists are asserted to be empty.

Operators flip flags per environment. Recommended sequence:

1. Enable `TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE` first, with
   `TIMETABLE_PRAYER_WINDOWS` populated. Watch the `pr1_prayer_rejections`
   count in logs across runs. If the list is empty across the
   representative scenario set, verify the configured windows are
   actually being loaded — a typo in the setting name yields a silent
   empty list, not an error.
2. Then enable `TIMETABLE_ENFORCE_LOCKS`. The payload's
   `pr1_lock_rejections` count after the first enforced run should equal
   the number of locked `SectionPlacement` rows for that board. This is
   the fastest way to confirm the preload actually ran.

---

## 3. Prayer telemetry is post-legacy — under-observes

`pr1_prayer_rejections` records overlaps discovered by the new rule
**after** the legacy hardcoded prayer-break filter (`_start_is_blocked`,
any start in 11:35–12:59) has already suppressed many candidates.

So with both flags on, the new rule catches a strict subset:

- Meetings that start **before** 11:35 but **end** inside the configured
  window (e.g. an 11:15–12:30 slot overlapping a 12:00–12:15 window) are
  visible to the new rule and rejected.
- Meetings that start **inside** 11:35–12:59 never reach the new rule —
  they are already eliminated by `_start_is_blocked`.

This is explicitly documented in `auto_place_board`'s prayer block. It is
**not a bug**; it is the intentional co-existence semantics for PR1.

The legacy filter retires in a subsequent PR. At that point
`pr1_prayer_rejections` becomes the complete record of configured-window
violations, and the two semantics can be compared directly on the same
scenario pack.

---

## 4. Lock enforcement is structural — not per-candidate

### Why the payload entries look different from the prayer ones

`pr1_lock_rejections` in PR1 is **one entry per preloaded locked row**,
emitted when the preload loop runs. It is **not** a count of
per-candidate collisions during round-robin placement. The structural
skip (via `already = count()` over locked rows) makes those collisions
unreachable — there is nothing to count.

This is a genuinely different shape of data from the prayer list. The
prayer list grows when the planner **tried** a placement and was
rejected. The lock list grows when the planner **respected** a
pre-existing lock by seeding it into occupancy. Read the logs
accordingly: `pr1_lock_rejections=N` after an enforced run means "N
locks were respected", not "N candidates collided with locks".

### Pre-conditions for lock enforcement

Lock enforcement depends on locked rows **existing in
`SectionPlacement` storage before the run starts**. The preload reads
`SectionPlacement.objects.filter(board=board, is_locked=True)`; it does
not introspect UI state or in-flight transactions. If the caller is
about to commit a new lock and then trigger a run, the commit must land
before the run is invoked.

### V2 lock-filter omission is intentional

There is no V2-side lock filter in PR1. This is **not** an oversight and
**not** a scope cut. The reasoning:

- Prayer overlap is a pure property of the target pattern (day ×
  time). Filtering candidate moves against the prayer rule is
  well-defined at move-generation time because each
  `to_pattern_id_{a,b}` fully determines the meetings.
- Lock respect, in contrast, is a resource-occupancy property
  (day × start × room). V2 resolves rooms **later**, via
  `try_repair_rooms_locally` against `room_occupancies`, and the
  auto-placer's commit-4 preload already seeds the locked-room
  occupancy when the V2 candidate is evaluated through the
  `auto_place_scenario` path.
- A V2-side lock filter at move-generation time would either (a)
  duplicate information already present in `room_occupancies`, or (b)
  produce a forked, half-true lock model that disagrees with the
  structural model in `auto_place_board`. Neither is worth shipping in
  PR1.

If shadow runs under `TIMETABLE_ENFORCE_LOCKS=True` show V2 proposing
repattern or swap moves that are only killed later because of locked
resource state, **that** is the signal to design an explicit lock-aware
move-pruning step — probably in PR2 or PR3. Until then, the omission
is deliberate and documented.

---

## 5. Rejection-code stability

The two sentinel strings — `PRAYER_OVERLAP` and `LOCK_RESPECT` — are
stable payload consumers' contract. Any downstream surface (dashboard,
CSV export, audit log) can match on those strings. Rejection context
keys (`prayer_start`, `prayer_end`, `locked_room`) are also stable.

Additional sentinels are expected in later PRs (e.g. room-type
mismatch, instructor conflict, exam-window overlap). Additions are
backwards-compatible; no existing code is changed when a new code is
introduced.

---

## 6. Test coverage snapshot

| File                                         | Coverage                                         |
| -------------------------------------------- | ------------------------------------------------ |
| `tests/test_pr1_prayer_enforcement.py`       | 13 unit tests: strict-before/after, boundary touch, engulf, straddle, multi-window, empty schedule, different day, context. |
| `tests/test_pr1_lock_enforcement.py`         | 7 unit tests: direct collision, different room, different slot, multi-lock, empty locks, context. |
| `tests/test_pr1_end_to_end.py`               | 3 E2E tests: parity (flags OFF), behaviour (both ON), prayer-only (locks OFF does not preload). |

Totals: **208/208 tests pass** at branch tip, including the 205
pre-PR1 baseline tests.

---

## 7. Deferred to later PRs

- Retire legacy `_start_is_blocked` 11:35–12:59 filter. Required before
  `pr1_prayer_rejections` becomes a complete record.
- V2-side lock-aware move pruning (only if shadow runs demonstrate the
  need — see §4).
- Admin/UI toggle for both flags (probably PR3).
- Dashboard surface for `pr1_*_rejections` (probably PR3 or PR4).
- Prayer-window schema — the current `TIMETABLE_PRAYER_WINDOWS` list of
  dicts is a fine interim but a database-backed schema makes more sense
  once admin editing lands.

---

## 8. Commit list

| Commit   | Subject                                                                  |
| -------- | ------------------------------------------------------------------------ |
| 1aeb525  | PR1 commit 1/8: failing tests + fixtures + scenario pack skeleton        |
| 622151d  | PR1 commit 2/8: add timetable_validation module with prayer + lock rule helpers |
| e551f01  | PR1 commit 3/8: wire prayer rule into auto_place_board + fold review nits |
| 6642eee  | PR1 commit 4/8: lock preload + occupancy seeding + structural skip       |
| 0f02780  | PR1 commit 5/8: V2 optimiser candidate-gen prayer-overlap filter         |
| 740fd53  | PR1 commit 6/8: surface flag state in logs + telemetry-honesty comments  |
| (this)   | PR1 commit 7/7: promotion note + comment fix (docs-only)                 |

Original plan called 8 commits; commits 4 + 5 were consolidated into a
single structural lock-enforcement commit (the preload + seed + skip
must land together, per "three things together" review guidance). So the
branch ships 7 commits, not 8.
