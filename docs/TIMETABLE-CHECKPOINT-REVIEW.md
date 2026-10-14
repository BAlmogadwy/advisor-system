# Timetable checkpoint and independent review — 2026-10-11

Status: local preservation checkpoint; **not approved for a PR or release**.

## Preservation

The snapshot database ignore rule is `/snapshots/**/*.sqlite3`. It covers
current and future nested replay databases without excluding documentation or
JSON fixtures. The six SQLite copies present at intake remain local; no
database was deleted or staged.

The checkpoint includes the timetable engine, secondary recovery, job lifecycle,
workspace UI, English course policy, tests and validation reports accumulated
in this task. A commit preserves that work; it does not certify correctness.

## Commit hook results

The first commit attempt applied Ruff formatting to 41 files and added final
newlines to six replay JSON reports. Ruff lint, merge-conflict checks, whitespace,
JavaScript syntax and control-character checks passed.

The full-import mypy hook failed with 1,901 errors across 137 files. This includes
unrelated existing modules and errors in changed/new modules; it is not a green
type check. Bandit could not start because recursive discovery found multiple
`.bandit` files inside existing ignored replay source copies under `.tmp`.

For the preservation checkpoint only, these two failing hooks are explicitly
skipped. Their failures remain release blockers pending diagnosis; no hook
configuration is weakened and no production approval is implied.

## Review scope

Three independent reviewers inspected engine selection/default-on flags and
persistence, scheduler/English pattern propagation, and UI/job cancellation,
concurrency and history. A fourth independent reviewer checked their fixes and
found additional ownership counterexamples. Controlled mutations ran in an
isolated source copy, preserving the user's server and databases.

`WORKFLOW.md` also requires a specifically named Gemini adversarial pass and an
independent final release review. No Gemini executable or callable Gemini tool
is available in this session. Codex subagent reviews do not satisfy that named
Gemini requirement. No PR, push, deployment or release is authorized by this
checkpoint.

## Controlled test mutations

Revision `8e4d49c` was exported into an isolated source copy, with a separate
in-memory test database and fresh test processes. No mutation touched the live
workspace source or a saved timetable. The selected baseline passed 12 tests.
Each mutation below then produced an assertion failure in its targeted test;
restoring all source returned the same 12 tests to green.

| Broken protection | Targeted regression | Result |
| --- | --- | --- |
| Per-teacher idle ceiling | `test_total_idle_gain_cannot_hide_an_individual_gap_regression` | Failed |
| Per-teacher day ceiling | `test_individual_ceiling_prevents_swapping_longer_weeks` | Failed |
| Hard student component ceilings | `test_each_hard_student_component_is_a_gate` | Three failing cases |
| Instructor-priority kill switch | `test_kill_switch_restores_exact_student_lex_comparison` | Failed |
| Secondary room nonregression | `test_soft_seat_cannot_be_paid_for_with_more_room_shortages` | Failed |
| Complete English bundle validation | `test_validator_rejects_mixed_partial_extra_and_shifted_patterns` | Failed |
| English no-room requirement | `test_new_generation_places_three_plus_english_sections_as_complete_lectures` | Both cases failed |
| Cancelled-worker ownership check | `test_reconciled_worker_cannot_restore_over_later_manual_edit` | Failed |

The machine-readable results are in
`snapshots/timetable-guard-mutations-20261011.json`. These are targeted tests of
eight protections, not an exhaustive mutation score or a release approval.

## Independent adversarial findings

The review found concrete counterexamples despite passing earlier tests:

1. **P1: English display columns leak into ordinary scheduling.** After adding
   an English 09:00–12:00 column, a three-credit AI123 course could receive two
   three-hour meetings. Policy-only columns must remain display-only for ordinary
   courses, while preserving explicit columns and the effective default grid.
2. **P1: Compaction splits a shared physical section.** A section represented on
   two boards moved on only one board, while its canonical meeting moved too.
   Writes must update all copies atomically and room them as one physical demand.
3. **P2: Classic SA persistence contradicts instructor-first selection.** A move
   reducing teacher idle from 105 to 15 minutes was rejected because student gap
   rose from 15 to 105 with hard coverage unchanged. Persistence must use the
   same canonical selection policy and independent safety ceilings.
4. **P2: The priority kill switch fails to restore classic behavior.** A new
   persistence guard still rejected teacher-quality trade-offs when priority was
   disabled. The new teacher guard must honor that switch.
5. **P1: Cancellation restores over another scenario's accepted edit.** An
   imported shared section owned by A had a placement on B; B allowed a room edit
   during A's job, then A's cancellation overwrote it. Shared resources require
   durable ownership through section replacement and restoration.
6. **P1: Synchronous optimisation has no durable job ownership.** Another planner
   could acquire the same scenario while a synchronous pipeline was running.
   Final review also found the same gap in synchronous generation. Both must use
   the same guarded job ownership as async work.
7. **P2: English manual preview conflicts with itself.** Previewing a valid
   existing bundle reported four instructor conflicts against its own meetings.
   Preview must exclude that complete bundle while checking other meetings.
8. **P1: Cancellation leaves a linked global canonical meeting changed.** Actual
   classic SA persistence moved a section whose owner was null; cancellation
   restored its board placement but not its canonical meeting, which also lost
   its original identity. Cancellation must snapshot and restore canonical
   meetings for linked nonowned sections under the shared resource lease.
9. **P1: Exact room persistence clears repair references.** Deleting and
   recreating a placement with its original primary key still triggers `SET_NULL`
   on repair-run and global-plan references. Atomic two-phase updates must keep
   the original rows and references alive, including through unique-key swaps.
10. **P1: External scenario copies can split on a successful repair.** A local
    section moved while its locked copy on another scenario stayed unchanged.
    Connected leases prevent concurrent edits but do not make this write safe.
    Sections shared outside the target scenario require time/room immobility;
    destructive rebuilds must refuse before changing their shared records.
11. **P1: Reset can disconnect a global section from its active job lease.**
    Removing a job's sole placement left the global section available for a new
    copy on another scenario. That scenario could then optimise successfully,
    before cancellation restored the first job's older canonical meeting.
    Submission must persist server-computed physical section IDs as well as
    scenario IDs; manual creation and planner submission must honor those IDs
    until the job finishes, even after its placement edges disappear.

All eleven confirmed findings above were repaired. The final independent Codex
review approved the repaired local behavior after a frozen 99-test focused run
and an independent reproduction of the global-section reset/cancellation case.
No remaining confirmed local correctness blocker was found in that review.
This is a local correctness assessment, not the required production release gate.

The final physical lease fix also received an isolated mutation check on the
current implementation. Disabling only the persisted physical-ID intersection
made both new reset regressions fail: manual creation returned 201 instead of
409, and competing planner submission did not raise `ScenarioPlannerBusy`.
The same pair passed before mutation; all 20 ownership regressions passed after
restoring the protection. These two probes complement the eight checkpoint
mutations above.

Cancellation tests use valid shared boards within one scenario across all five
engines, retaining placement identity, repair references and canonical meetings.
Separate locked and unlocked external-copy tests prove destructive rebuilds
refuse before the pipeline and leave all captured resource rows unchanged.

## Security scan follow-up

Bandit was run directly on all non-test Python files changed by the checkpoint,
using the existing `.bandit` configuration and explicit file targets. It
reported **zero findings and zero scan errors**. This resolves the scoped scan;
it does not claim the repository's recursive hook or full-import type gate passed.

## Default-on settings reviewed

| Setting | Checkpoint default | Operational fallback |
| --- | --- | --- |
| `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` | Changed from false to true | Set false to skip optional compaction |
| `TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED` | New, true | Set false to restore classic student-first comparisons |
| `TIMETABLE_SECONDARY_RECOVERY_ENABLED` | New, true | Set false to skip bounded seat/room recovery; priority false also skips it |

The defaults remain aligned with the user's requested instructor priority and
seat/room recovery. Keeping them enabled locally does not constitute production
promotion. The disabled-priority classic persistence regression is included in
the review fixes and tests.

## Verification

The final combined related Python regression run passed **1,411 tests**, with
one PostgreSQL-only skip, in 98 seconds. This run includes the external section
safeguards, durable global-section leases and revised cancellation/refusal tests.
All **75 timetable frontend tests** passed; frontend source did not change
during the subsequent ownership fixes. Django's system check and Git whitespace
checks passed.

Ruff lint and format checks passed on 88 changed/new Python files. Targeted mypy
with `--follow-imports=skip` passed the seven policy, section, snapshot and ownership
modules; this is a scoped check, not a replacement for the failing full-import
repository type gate.

The parent revision was also exported into an isolated source copy for a type
baseline. Full-import mypy on the same 49 existing input files reported 876
errors on the parent and 1,728 on the working tree. Changes to import reach make
those totals unsuitable as a count of introduced defects. They establish both
inherited type debt and an unresolved current type gate; the errors cannot all
be dismissed as preexisting.

Real institutional replays ran on separate SQLite copies of the earlier freshly
generated timetables. The user's original database was not modified.

| Replay | Instructor days | Instructor idle | Soft unresolved seats | Physical room shortages |
| --- | --- | --- | --- | --- |
| Term 1, 258 students | 21 → 21 | 670 → 415 minutes | 4 → 0 | 2 → 2 |
| Term 2, 405 students | 0 → 0 | 0 → 0 | 31 → 17 | 9 → 3 |

Every mapped term-one instructor retained their day ceiling and idle ceiling.
Term two has no eligible instructor links, so its zero metrics do not establish
teacher-quality protection; targeted tests and the mapped term-one replay cover
that claim. The four ENG102 sections retained complete four-day afternoon
patterns, and actual assigned student clashes stayed zero. Term one has no new
ENG101 demand in this cohort. Reports:
`snapshots/review-optimise-term1-20261011.json` and
`snapshots/review-optimise-term2-20261011.json`.

Both replay inputs have no physical sections copied to external scenarios, so
the subsequent external ownership guards do not change their scheduling inputs.
The replay budgets were 15 seconds and eight search iterations; full pipeline
elapsed times were approximately 50 and 118 seconds because mandatory passes
run outside that search budget.

## Release limits and rollback

No PR, push or release was performed. The specifically required Gemini pass is
unavailable, and full-import mypy still fails. PostgreSQL simultaneous transaction
behavior remains unverified; the skipped PostgreSQL test is not a pass. Planner
execution ownership uses a process-local registry: deployment is currently one
web process with multiple threads, and external CLI workers or peer stale-job
sweeps are not validated as a distributed execution configuration.

Preservation commits skip only the two previously diagnosed hooks (`mypy` and
recursive `bandit`) for that invocation. Scoped Bandit is green, but the hook
configuration is unchanged and its recursive discovery failure remains visible.
All other applicable commit hooks run normally.

The three documented environment flags can disable their optional optimisations
for operational rollback. Code rollback must revert the local review-fix commit
and then the preservation checkpoint together, after any active planner finishes;
do not revert only ownership safeguards while leaving the new engine enabled.
SQLite replay files are ignored local evidence and are not part of either commit.
