# Soft-seat and room recovery validation

Validated locally on 2026-10-10. This follows the initial
[instructor-priority work](TIMETABLE-INSTRUCTOR-PRIORITY-VALIDATION.md).
The objective is to recover unresolved soft-course enrolments and valid physical
rooms while preserving every instructor's campus days and idle minutes.

## Changes and acceptance rules

Classic Generate and Optimise Current now finish with a bounded recovery pass
after instructor repairs, compaction and student reassignment. It tries legal
section patterns and coordinated swaps, targeting unresolved soft courses,
their seated blockers, and congested room components. It then spends the
remaining budget on student-gap polishing without losing recovered seats or
rooms. Courses with no sufficiently large eligible room are distinguished from
recoverable congestion; unrelated room solves are skipped.

Each accepted recovery move must satisfy all of these conditions:

- No individual instructor gains a campus day or idle minute, relative to both
  the recovery entry state and the last accepted state. Aggregate metrics are
  protected too. Idle is measured from the union of physical meeting intervals;
  remote teaching does not create campus days.
- Neither unresolved soft enrolments nor physical room shortages increases.
  Hard student score components, instructor clashes and daily caps are protected.
- Meeting contact minutes, physical/remote delivery, locked sections' meeting
  times and section identity are preserved. Reserve use and student gaps can
  change when more enrolments are seated.
- Exact room checks retain existing capacity buffers, room kind, department and
  gender eligibility. A lock freezes a section's times, not its room: locked
  meetings may be re-roomed, while externally owned meetings keep both time and
  room. Count proofs are reported separately from full lexicographic room
  optimality.

Optimise Current additionally protects the instructor metrics of the original
saved board through the entire run, including mandatory repairs. A repair that
cannot satisfy those ceilings is rolled back with an explicit blocked result;
the original violation remains visible. The mandatory clash repair now tries
same-day moves, coordinated instructor moves, and one newly blocked student
course's meeting before its historical fallback. Its extra search has a shared
five-second deadline. Shared physical groups and any placement locks prevent
unsafe individual relocation.

The optional recovery runner uses an atomic savepoint. It checks that placements
and meeting metadata have not changed since search began, preserves row IDs and
creation metadata, and independently evaluates the persisted result. Missing or
ambiguous meeting matches, uniqueness collisions, room-solver failures and
failed validation discard only this pass. A whole-board rollback distinguishes
discarded candidate repairs/recovery from actual saved outcomes.

Both workspace interfaces report recovered seats and valid-room shortages. A
rejected candidate cannot display its gains as persisted improvements. Public
student score tuples retain their existing formats; actual instructor idle is
reported separately, even when the old trailing instructor-gap score is zero.

## Real-data method

The benchmark script uses SQLite's backup API with a read-only source connection
and refuses an existing or identical scratch path. Django's database path and
imported optimiser source are checked before a run. Benchmark operations ran on
copies; they did not overwrite the user's saved boards or live manual edits.
The user completed generation and edited the live timetable during this work,
so the initial snapshot and latest saved board are different inputs.

All reports contain aggregate results rather than student names. Local reports,
source freezes and SQLite copies remain under ignored `.tmp/timetable-quality/`.
The common recovery implementation is frozen in `secondary-final-source`.
The final strict current-mode gate and coordinated clash repair use the later
`secondary-guarded-final-source` freeze. Earlier completed replays had no
mandatory instructor violations and did not enter the changed repair branch.

## Completed-board comparisons

An unresolved soft count is a course enrolment, not a count of distinct students.
An instructor-day total sums working days across instructors. Instructor idle
and student gaps are total minutes across the respective populations.

| Saved input | Soft enrolments unresolved | Valid-room shortages | Instructor days | Instructor idle |
|---|---:|---:|---:|---:|
| Latest saved AI/AI2/DS/DS2, term 1 | 18 -> 2 | 3 -> 2 | 18 -> 18 | 780 -> 755 |
| Earlier instructor-compact AI/AI2/DS/DS2, term 1 | 17 -> 4 | 3 -> 2 | 20 -> 20 | 380 -> 370 |
| Saved IS/IS2, term 2 | 28 -> 1 | 16 -> 2 | No instructor links | No instructor links |

The latest saved AI board contains 258 students, 44 sections and 100 meetings.
Its existing instructor clash is repaired using three Sunday relocations:
DS432 to 16:00-17:15, AI331 to 14:30-15:45 and MGT405 to 10:45-12:25. The
original one-meeting repair would have increased instructor days 18 -> 19 and
idle 780 -> 800; that discarded run is retained as
`secondary-final-latest-live.json`, and is not an accepted comparison.

The final complete run, `secondary-guarded-latest-live.json`, removes the clash,
keeps all four hard student score components at zero and reduces instructor
134's idle 190 -> 165. All other instructors' days and idle are unchanged.
There are no mandatory-repair exceptions. It recovers 16 soft enrolments and
reaches the conditional room-shortage lower bound of two. The final recovery
stage itself reduces soft unresolved 7 -> 2 and room shortages 4 -> 2 while
preserving its entry teacher result of 18 days/755 minutes. Total elapsed time
is 45.0 seconds; the recovery search takes 6.4 seconds and 935 evaluations.
The remaining two soft enrolments are not proven unavoidable.

Student gaps increase 12,160 -> 17,215 minutes and reserve use increases 5 -> 7.
Static potential cross-board overlaps increase 20 -> 23, while actual assigned
student clashes stay zero and high-risk overlap counts remain zero. This is a
seat-coverage trade-off under the requested instructor priorities, rather than
a claim that every measure of student convenience improves.

For the instructor-compact AI board, every individual instructor's days and
idle are nonincreasing. Its hard student score remains `[0, 0, 0, 0]`, all 44
sections and 100 meeting durations are retained, and no instructor or room
clash is introduced. Total student gaps increase from 11,495 to 14,110 minutes
while 13 additional soft enrolments are recovered. The final recovery stage
itself improves soft unresolved 8 -> 4 and room shortages 3 -> 2 without changing
the preceding teacher result of 20 days and 370 idle minutes.

The IS board has 295 students, 58 sections and 131 meetings. Its hard score
remains all zero, with no actual assigned-student, instructor or room clashes.
Student gaps increase from 78,075 to 97,570 minutes, and reserve use decreases
24 -> 19. Its missing instructor links mean this case demonstrates seat and
room recovery, rather than evidence of instructor protection.

The task-start AI snapshot was captured before the user's generation had fully
finished. It is supplementary evidence, not the latest completed board:
soft unresolved 13 -> 0, true room shortages 2 -> 2, instructor days 22 -> 21
and idle 1,370 -> 990. No individual teacher worsens. High-risk unresolved
9 -> 0 and core unresolved 3 -> 1; that last core enrolment remains unresolved.
Student gaps increase 10,830 -> 16,650. The repair reports contain no detected
instructor violations.

## Generate and scheduler checks

A fresh classic Generate using the real AI/AI2/DS/DS2 male term-1 demand creates
44 sections and 100 meetings for 258 students. It finishes with zero unresolved
soft enrolments, zero high-risk unresolved, one unresolved core enrolment, two
physical room shortages, and no actual student/instructor/room clashes.
The recovery stage preserves every teacher's entry metrics: 21 total working
days and 990 idle minutes. It reduces soft unresolved 21 -> 0 and room shortages
4 -> 2. Total elapsed time is 137.1 seconds.

This fresh Generate does not uniformly beat every earlier implementation on
teacher totals: the first validation recorded a generated board with 20 days
and 485 idle minutes under a weaker per-person idle acceptance rule. The new
recovery preserves its own stricter teacher-stage result; the search is bounded
and no global teacher or timetable optimum is claimed.

Scheduler Generate is independently rerun on the unchanged real cohort
fingerprint `f2f83fc152bb3b143ba789abd37c66edb697706c4e1948ca0c05eb8387ea1dee`.
Three seeds reproduce 15 instructor days at the proven lower bound and 65 idle
minutes. Actual capacity-aware seating is `OPTIMAL`: all 259 students are fully
seated, 538 demands seated, zero unseated demands and zero actual clashes.
Only 9 of 29 sections have eligible instructor links; all nine are staffed.
Three of 50 physical meetings cannot be roomed because of capacity, matching
the conditional inventory lower bound. Total elapsed time is 122.1 seconds.
Average student idle is 106.3 minutes (earlier run: 103.2); this run does not
prove a student-idle optimum. Independent validation finds zero observed hard
violations, but certification remains `UNCERTIFIED` because blocked-slot, lock
and student-rule evidence coverage is incomplete. These room deficits also
prevent publication.

## Remaining capacity limits

The final classic assignments have no incompatible assigned rooms or room
conflicts. They reach a lower bound of two unroomed physical meetings given the
existing inventory, section demands and eligibility rules:

| Cohort/engine | Course | Room kind | Required seats | Largest eligible room |
|---|---|---|---:|---:|
| Classic AI/DS | CS323 | Lab | 26 | 25 |
| Classic AI/DS | FE2 | Lecture | 61 | 60 |
| Classic IS/IS2 | IS226 | Lab | 27 | 25 |
| Classic IS/IS2 | IS252 | Lab | 26 | 25 |
| Scheduler | CS211 | Lab | 29 | 25 |
| Scheduler | CS372 | Lab | 27 | 25 |
| Scheduler | CS424 | Lab | 26 | 25 |

Classic retains its existing 1.1 capacity buffer and rounding rule; scheduler
retains its native 1.0 buffer. Neither was reduced to hide shortages. A larger
eligible room or a justified section-plan change is required to eliminate these
deficits. These bounds concern room counts under the current inputs, not global
student seating, teacher or timetable optimality.

The shortage measurement also fixes misleading blank-only totals. The saved
AI compact board has two blank rooms plus one undersized assigned room; IS has
eleven blanks plus five incompatible assignments. The recovery room oracle
additionally detects conflicting assignments. Remote meetings needing no room
are excluded.

## Replays and rollback controls

The completed common-source reports are `secondary-final-best-term1.json`,
`secondary-final-live-is.json`, `secondary-final-live-ai.json`,
`secondary-final-generate.json` and `secondary-final-scheduler.json`.
Classic current replays use five local-search iterations and a 60-second CP-SAT
allowance. Recovery defaults are 45 seconds, 12 recovery iterations, at most
3,000 evaluations, and at most four polishing iterations within the same
remaining budget. Scheduler uses three seeds sharing a 120-second allowance.
Elapsed end-to-end times include other stages and may exceed a stage's budget.

## Verification

- Final combined timetable, instructor, constraint and scheduler regression
  suite: **889 passed, 1 skipped** in 51.1 seconds across 45 files. The skip is
  for PostgreSQL-specific locking behaviour. Before the final coordinated-repair
  additions, the wider selected integration run passed **960 tests, 1 skipped**.
- Production JavaScript result/dispatch regressions: **23 passed**; both
  workspace scripts pass `node --check`.
- Repository Ruff and `git diff --check` pass. Scoped mypy with
  `--follow-imports=silent` reports no issues in the four new policy/recovery
  modules. Broader imported dependencies still contain existing type errors;
  this is not a claim of a clean repository-wide mypy run.
- Independent JSON assertions verify every teacher's day/idle nonincrease,
  unchanged section identities and meeting duration multisets, nonincreasing
  hard/soft unresolved counts and valid-room shortages for all four current
  replay inputs. Final persisted boards have no incomplete section patterns,
  instructor clashes, incompatible assigned rooms or same-board room clashes.
- An independent adversarial subagent checked the new room proof/rollback
  handling and the coordinated repair, ran focused tests, and reproduced the
  three-move repair without writing to the live database. Separate room-domain
  inspection confirms the conditional inventory lower bounds above.

Run a new isolated replay using distinct scratch and report names:

```powershell
.venv\Scripts\python.exe scripts/benchmark_timetable_quality.py `
  --source-root .tmp/timetable-quality/secondary-guarded-final-source `
  --snapshot .tmp/timetable-quality/secondary-latest-live.sqlite3 `
  --scratch .tmp/timetable-quality/replay-new.sqlite3 `
  --output .tmp/timetable-quality/replay-new.json `
  --scenario 663 --action current --iterations 5 --seconds 60
```

`TIMETABLE_SECONDARY_RECOVERY_ENABLED=false` disables the new recovery stage.
`TIMETABLE_SECONDARY_RECOVERY_SECONDS` and
`TIMETABLE_SECONDARY_RECOVERY_ITERATIONS` adjust its budget. Disabling
`TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED` also skips recovery and the stricter
current-mode teacher gate for compatibility with the historical comparison
policy. No production release was performed. The separate Gemini and release
gates in `WORKFLOW.md` remain requirements before a production release.
