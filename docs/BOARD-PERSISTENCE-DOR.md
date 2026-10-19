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
- Defect (c) (greedy clash/cap map inert on rebuild) is a constraint-plumbing
  bug, addressed by the ConstraintEngine PR-2, not persistence.

## PR-1b — success-path meeting sync (stacked on PR-1)

`persist_section_states_to_scenario` (`timetable_optimizer_v2.py:506-602`)
moves placements and `assign_rooms_to_board` assigns their rooms — verified that
**neither writes `TermSectionMeeting`** (rooming.py has zero TSM references). So
after a successful optimise every changed section's meeting rows are stale. A
live check on scenario 638 found **69 of 70 sections drifted** (166 meeting-slots
vs 168 placement-slots).

Rather than thread TSM writes through the hot per-section persist (which runs
*before* rooms are assigned), PR-1b adds one **projection** step:
`sync_meetings_from_placements(scenario_id)` — called once after the rooming loop
and *before* the instructor cap/clash/compaction repairs, in both optimise flows.
It rewrites each scenario-owned section's meetings to the distinct
`(day, start, end, room)` tuples of its placements, carrying the instructor
forward then re-fanning the link primary via `reconcile_scenario_instructors`.

- Runs before the repairs so their `_relocate` (matches TSM by
  `(term_section, day, start)`) sees meetings that already match placements.
- Dedup collapses a cross-board shared session into the single row the TSM
  unique constraint allows — clearing the duplicate-meeting drift behind the
  phantom clashes (e.g. GSE1).
- Global sections untouched (same scope rule as the rest of the module).

Still deferred (PR-2+): migrating the greedy/solver/SA/rebalance persists onto
this projection (they already maintain TSM via delete-recreate + re-fan, so they
don't drift — consolidation, not a bug) and the `tw_planned` `instructor=""`
manual-placement gap.

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

## Lock contract correction — 2026-10-18

The earlier per-placement reset behavior could silently truncate an ordinary
section. A real-data Full Rebuild with one locked meeting each for MATH105 and
PHYS103 kept those rows but removed three unlocked companion meetings: their
weekly contact time fell from 150/250 minutes to 75/75 minutes. Counting the
surviving section as already placed prevented the generator from restoring the
missing meetings, and course-presence checks did not detect that loss.

For a lock-preserving rebuild, any locked meeting therefore protects the
complete physical section's existing time pattern, including copies on other
boards in the same scenario. Reset preserves all companion placements and
canonical meetings; generation occupancy and later solver/persistence stages
must honor that same section boundary. This does not set additional database
lock flags. Sections without locks remain available for rebuilding. An explicit
`keep_locked=False` reset remains a hard wipe.

Canonical cleanup still removes orphan meeting rows that have no retained
placement. Protecting a complete section preserves its valid meeting identities
and metadata without reintroducing phantom instructor occupancy.

This protects meeting days, start/end times and complete weekly contact hours.
The user accepts room reassignment; this correction adds no room immobility
rule. ENG101/ENG102 retain their indivisible four-day policy.

The workspace actions have distinct scopes:

| Action | Scenario affected | Saved locks |
| --- | --- | --- |
| Generate | Creates a new scenario from the generation inputs | Existing scenario stays intact; its locks are not copied |
| Optimise Current | Loaded scenario, starting from its existing timetable | Protected sections retain their time patterns |
| Full Rebuild | Loaded scenario, rebuilding the other sections | Protected sections retain their time patterns and all meetings |

At this checkpoint Generate still used fixed search seeds. The subsequent
per-run seed change below replaces that behavior without changing the scoring
or lock contract. Current optimisation searches for admissible improvements to
the saved timetable; an unchanged result does not establish global optimality.

Verification on a fresh SQLite copy of the real 258-student scenario used all
seven Full Rebuild strategies. Two individual placement locks protected the
complete five-meeting MATH105/PHYS103 patterns: their original IDs, days,
start/end times and flags matched exactly after the run, retaining 150 and 250
weekly contact minutes. The corrected run retained 100 placements, whereas the
same pre-fix probe retained 97. The guarded run succeeded without rollback.
This was a bounded replay (zero local iterations, chain/global CP-SAT polish
off, candidate budget 0.1 seconds per board, short compaction/recovery budgets),
not a measurement of production search quality. Separate real solver and
repair regressions cover protected companion occupancy, shared board copies,
cap/clash repairs, flag-off behavior and canonical orphan cleanup.

A Generate scaffold on that private copy created a new scenario with five
boards and no inherited placements or locks; the original scenario's placement
and canonical meeting rows matched their pre-call snapshots. No saved user
database was modified. Local replay evidence remains under the ignored
`.tmp/lock-audit-20261018/` directory.

Final local validation passed 1,469 related Python tests (one PostgreSQL-only
skip) and 87 timetable frontend tests. Ruff lint/format, JavaScript parsing,
Django system checks, whitespace/control-character checks, scoped Bandit on the
six changed services, and scoped mypy on board persistence passed. Independent
Codex review approved the frozen changes. This is local correctness validation,
not the specifically required Gemini or full-project type release gate.

The split-view lock control also passed browser verification against the copied
real timetable: a native double-click produced one save, a later Unlock still
worked, and the meeting retained its time. Twelve permanent full-script DOM
regressions cover slow/fast replies, repeated keys, shared panes, failed replies,
failed refreshes and genuine later unlocks; three fail against the original
script. The user database's original 100 scenario placements were compared
read-only with the pre-audit snapshot and remained identical.

## Per-run search seeds — 2026-10-18

Each accepted Generate, Full Rebuild, or Optimise Current request now receives
a fresh nonnegative 31-bit seed. The server stores it in the planner job before
dispatch and includes it in polling and result data. Both workspace result
dialogs display the seed, including zero. Generate still creates a new scenario;
both optimisation options retain the loaded scenario's protected time patterns.

API callers may supply `seed` as an integer from 0 through 2147483647 to reuse
the starting choices. Omission or null requests a fresh seed. Invalid values,
including booleans and numeric strings, are rejected before scenario creation
or stale-job reconciliation. Retries of an existing worker use its stored seed.

The classic strategy portfolio derives independent streams from that root.
Greedy placement varies only options tied on the complete placement score;
candidate evaluation and final placement use the same strategy stream. Current
optimisation varies eligible move order while retaining strict improvement
acceptance, instructor priorities, safety checks and locks. CP-SAT and annealing
receive derived seeds. The alternative scheduler receives the root explicitly
and derives its portfolio seeds at its existing isolation boundary. No
process-global random state is changed, and context cleanup covers failures
and cancellation. Direct legacy service calls without a seeded run retain
their previous defaults.

A fresh seed explores different choices; it does not require a different final
timetable. Constrained data may admit one acceptable result, and Current may
find no safe improvement. Reusing a seed helps reproduce starting choices;
parallel workers and time budgets can still affect the final result. No
placement or acceptance constraint is relaxed to force visible variety.

Permanent tests exercise actual greedy layout variety, same-seed replay across
new database IDs, different acceptable Current improvements, equal-quality
Current rejection, complete locked patterns under all seven strategies,
English bundles, concurrent context isolation, API validation and seed-zero
handling. The two variety regressions fail against the preceding checkpoint.
Bounded real-data replay evidence is stored locally under the ignored
`.tmp/run-seed-audit/` directory; all replay databases are fresh read-only-source
SQLite backups.

The bounded real 258-student Generate replays with seeds 73011 and 73012 each
built 100 placements and produced different time layouts. Current replays with
seeds 101 and 202 also produced different layouts, preserved the five original
meetings in two partly locked sections, kept each instructor's days and idle
within the saved ceilings, and reduced total idle from 780 to 755 minutes with
18 instructor-days in both runs. A 405-student Term 2 Generate replay with
explicit seed zero built 187 placements; all four English sections retained
their complete allowed pattern and empty room assignments. These short replay
budgets demonstrate seed routing and invariants, not production quality maxima.

A seeded Full Rebuild retained all 100 placements and all five protected
meeting identities/times/flags. Its saved-week instructor comparison was worse
(18 to 22 days; 780 to 1320 idle minutes). Full's existing gate compares its
generated candidates and subsequent passes, rather than imposing Current's
saved-week instructor ceilings. Independent review confirmed that the runner,
ranking and policy modules are unchanged by this seed feature. The initial
probe's unconditional saved-week assertion failed for Full and was corrected
to apply to Current only; the original evidence remains available. This seed
change does not change Full Rebuild's acceptance contract.

Final local checks passed 1,587 related Python tests (one PostgreSQL-only skip),
101 frontend tests, Ruff lint/format, JavaScript parsing, Django system checks,
control-character/whitespace checks, scoped Bandit, and mypy on the new pure
seed helper. Independent Codex review found no actionable issues. Full-import
mypy still has existing project annotation errors; named Gemini release review
has not been completed. No production release is represented by these checks.

## Independent scenario copies — 2026-10-18

Both workspace views provide Copy scenario beside the scenario selector. The
optional name defaults to a unique copy name within the source year and term.
A published or draft source produces an editable draft; copying does not run
Generate or Optimise. General Advisors and Super Admins use the authenticated
POST `/ops/tw/scenarios/<id>/copy/` endpoint. Existing edit and planner controls
retain their permissions and behaviour.

The copy transaction creates new boards and physical sections, including
unplaced sections and sections referenced only through visibility. Shared
sections within the source remain shared within the copy, but their identifiers
are private to it. Placements, canonical meetings, rooms, locks, instructor and
programme allocations, board visibility, student demand, board membership,
section budgets and scenario settings are remapped to the new parents.
ENG101/ENG102 retain their exact four-day, roomless meeting bundles.

Student, instructor and room catalogues remain shared. StudentTermSection
registration evidence is deliberately excluded: unscoped availability readers
could treat duplicated registration rows as additional commitments. Planner
jobs, repair/approval records and client undo history are also excluded. Student
requests and membership are copied so that normal draft evaluation can rebuild
derived assignment results.

Related scenario and section locks protect the snapshot, and a queued or
running planner on a related resource returns the existing busy response.
Visibility is rechecked after acquiring the locks. Any late write failure rolls
back the entire copy. Explicit name collisions and ambiguous physical-section
or instructor identities return a conflict rather than silently overwriting,
merging or renaming records. No database migration is required.

Tests cover the complete private graph, source immutability, real HTTP editing,
deleting either side, late rollback, shared-resource jobs, role checks, name
validation and identity conflicts. An isolated runtime fault that dropped the
copied lock flags made the permanent graph regression fail on the changed lock;
the production source was never modified for that probe. Private backups of the
real 258-student and 405-student scenarios copied 100 and 187 placements across
five boards each; their timetable fingerprints and safety summaries matched exactly. The latter
included all four valid roomless English sections. Both browser views also
copied the published real scenario with its two locks intact. Native browser
checks confirmed unique repeat names, refresh of a non-first board, and Split
Close returning to the current copy after refresh. The original user
database was checked read-only and remained unchanged. Replay databases and
screenshots stay in the ignored `.tmp/scenario-copy-audit/` directory.

Final local validation passed 1,618 related Python tests (one PostgreSQL-only
skip), all 132 frontend regressions and a repeated 31-case copy backend suite.
The permanent browser-script tests cover stale list responses, repeated
submission, Unicode-safe names, saved-but-unloaded copies, read-only retries,
refresh selection and Split Close navigation. Ruff lint/format, scoped mypy and
Bandit, JavaScript parsing, Django system checks and whitespace/control-character
checks passed. Independent Codex review approved the frozen changes. These are
local feature checks; this entry does not represent deployment of the copy UI.

## Locks freeze times, not rooms — 2026-10-19

The lock contract above accepts room reassignment, but five room paths still
treated a lock as a room freeze. Production scenario 26 showed the cost: CS112
S1 and S3 were both locked into one room at SUN/TUE 09:00 while compatible rooms
were free, and every Optimise Current kept the double-booking. The recovery room
oracle pinned the locked rooms (`LOCKED_ROOM_CONFLICT`), its writer and meeting
sync refused any locked change, board rooming skipped locked unroomed rows, and
the compaction and classic persist guards rolled back a locked room change.

A lock now freezes only the section's days, times and contact pattern on the
Optimise and repair room paths (recovery, cap/clash repair, compaction, board
rooming and the classic SA/load-balanced persist). Build's auto-placer still
seeds a locked meeting's existing room as occupied, which the contract allows.
Locked meetings are re-roomed like any other; the writer and meeting sync still
reject a locked time change and keep an unroomable locked meeting's empty
representation. Externally owned sections (also placed by another scenario)
remain fully frozen, room included. The compaction gate compares locked rows
without `room`/`updated_at` and locked meetings without `room`; the classic
persist guard compares locked rows without `room`.

Room reassignment never costs a locked meeting its room for nothing. The exact
room solve first minimises and pins the number of unroomed meetings (its count
proof comes from that phase alone); a further phase then keeps every locked
meeting that already holds a valid room roomed, in any compatible room. A locked
meeting therefore yields its room only when that seats strictly more meetings
overall. The greedy warm start, which is also the timeout/size fallback, seats
those locked meetings in their current rooms first only when that seats no
fewer meetings than the plain greedy. Unlocked meetings get no tie-break.

`tests/test_timetable_locked_room_reassignment.py` reproduces scenario 26
end to end through `run_v2_optimisation_guarded(mode="current")` and covers the
oracle, writer, meeting sync and an externally owned control.

The manual paths follow the same contract. `POST /ops/tw/placements/<id>/move/`
accepts a room-only change (same day/start/end, no `pattern_id`) on a locked
placement without `override`, keeping the lock; any time change still returns
`PLACEMENT_LOCKED` unless overridden. A room-only change moves every
same-scenario copy at that slot that was in the same room, plus a
scenario-owned section's meeting rows in that room (merging a row the new room
would duplicate); a copy already in another room is left alone, so undo
restores every row exactly unless the new room was one such a copy already
held. Bulk clean rooms now rooms locked unroomed rows in place and rooms the
unroomed copies of a shared class together, in one room whose department suits
every board, so undo and redo stay exact. `shared_sections --apply` gives a
locked copy that already meets at the canonical times the canonical room (never
an unroomed one). Externally owned sections stay fully frozen on every manual
path: the move and pattern-switch endpoints return 409
`PLACEMENT_EXTERNALLY_OWNED`, bulk clean rooms and bulk safe time moves skip
them, and the canonicaliser skips the whole section and says so in its dry run
and summary.
The split and classic lock buttons explain the rule in English and Arabic.
`tests/test_timetable_manual_locked_room_edit.py` covers the endpoint.
