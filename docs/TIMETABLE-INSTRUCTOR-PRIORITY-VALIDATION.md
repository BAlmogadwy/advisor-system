# Timetable generation and optimisation validation

Validated locally on 2026-10-10 against baseline commit `d6b4134`. The requested
policy is instructor campus working days first, then instructor idle minutes,
within hard student and operational safeguards.

This records the first implementation. The follow-up
[secondary recovery validation](TIMETABLE-SECONDARY-RECOVERY-VALIDATION.md)
adds individual instructor idle protection and room measurements that include
incompatible assigned rooms. Its later results supersede the soft-seat and room
trade-offs documented here.

## Implemented behaviour

- Classic candidate selection, single moves, coordinated moves, CP-SAT
  acceptance and persistence use the same instructor policy. Student score
  tuples retain their existing 6/7/9-element formats; teacher metrics are
  reported separately. Hard coverage components cannot compensate for one
  another. Optional moves cannot add a campus day for another instructor.
- A stalled focused search expands immediately. Chain search includes actual
  shared-instructor relationships and supports coordinated improvements even
  without shared student demand. Scans and generation obey their deadlines and
  preserve the best completed trial.
- Generated split course codes resolve to the complete institutional code for
  instructor and online links. Course occupancy retains the full offering
  identity so different courses in one department remain distinct.
- All improvement stages preserve meeting durations and physical delivery.
  Remote sections stay fixed in optional campus-domain searches. Mandatory
  remote repairs require an explicit online slot of matching duration; if none
  exists, the original meeting and an unresolved-violation report remain.
- Optional instructor compaction is enabled by default with a 20-second search
  budget. It protects each teacher's day ceiling, student feasibility, reserve,
  soft coverage and bounded student-gap impact. A local transaction rolls back
  only this pass if room coverage or locked metadata worsens, preserving any
  preceding mandatory repairs.
- Returned optimiser errors restore the original board, just like exceptions.
- Scheduler Generate uses three seeds by default, ranks strict instructor gaps
  after instructor days and operational checks, retains the roomed selected
  board, and confirms actual capacity-aware student seating among finalists.
  Unknown seating is reported as unknown; feasible and optimal are distinct.
- Both workspace interfaces show teacher improvements, seating evidence and
  rollbacks accurately. Synchronous Generate honours the requested engine and
  runs the same policy as the background path.

Kill switches: `TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=false` restores the
previous classic comparison policy;
`TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED=false` disables the optional pass.
The two flags are independent.

## Real-data method

Every replay uses SQLite's backup API to create a new scratch database from a
read-only source snapshot. Django's database path is checked against the scratch
path before solving. The live `db.sqlite3` and saved timetables are not edited.
Original source is frozen under `.tmp/timetable-quality/baseline-source`.
Detailed local reports and scratch databases remain in ignored `.tmp` storage.
After validation, read-only row comparisons confirmed that nine live timetable,
meeting, instructor, budget and student-link tables still match the source
snapshot exactly.

The database contains one saved timetable: scenario 662, academic year 1448,
term 2, IS/IS2, male. It has 295 student profiles, 58 sections and 131 meetings,
with no matching instructor assignments. To exercise instructor priorities,
additional boards were generated from the actual AI/AI2/DS/DS2 male term-1
curriculum, demand, instructor links and room inventory. These are institutional
cohort replays, rather than additional previously saved timetables.

Classic runs use five local-search iterations, a 120-second CP-SAT allowance
and a 20-second chain allowance. Changed local search has a 30-second benchmark
allowance; the original implementation hardcodes 180 seconds. Elapsed-time
comparisons therefore describe these configured runs and are not speed claims
at identical local-search limits. Scheduler comparisons use the same input
fingerprint and 120 seconds of nominal planning allowance: one original seed
versus three changed seeds sharing that allowance, plus seating confirmation.
Default UI `seconds` remains a **per-seed** allowance; `total_seconds` includes
the shared confirmation allowance.

## Results

### Saved IS/IS2 timetable

Both versions improved the same saved scenario 662. The final changed replay
retains all 58 sections, all 131 meetings and every meeting-duration multiset.
There are no resolved instructor links in this scenario, so it cannot establish
an instructor improvement.

| Measure after Optimise Current | Original | Changed |
|---|---:|---:|
| Core/excess-foundation/high-risk unresolved components | 0 / 0 / 0 | 0 / 0 / 0 |
| Soft-course unresolved seats | 39 | 25 |
| Student gap minutes | 66,880 | 72,080 |
| Physical meetings without rooms | 13 | 10 |
| Assigned student / instructor / room clashes | 0 / 0 / 0 | 0 / 0 / 0 |
| Elapsed seconds | 150.3 | 178.0 |

The changed replay seats 14 more soft-course demands and leaves three fewer
physical meetings without rooms, with higher student gaps and elapsed time.
Its own starting-board gaps fall from 78,075 to 72,080 and soft unresolved
demands from 28 to 25. It is not uniformly better than the original algorithm
on every metric. Earlier intermediate runs are superseded by this final replay.

### Instructor-linked classic cohort

The term-1 classic cohort contains 258 students, 44 sections, 100 meetings and
five resolved instructors. Working days below sum each instructor's campus days.

| Measure | Original full generation | Changed full generation | Changed Optimise Current, starting from original generated board |
|---|---:|---:|---:|
| Instructor working days | 22 | 20 | 20 |
| Instructor idle minutes | 1,125 | 485 | 380 |
| Student gap minutes | 12,820 | 12,090 | 11,495 |
| Soft-course unresolved seats | 2 | 20 | 17 |
| High-risk/core/excess-foundation unresolved components | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Assigned student clashes | 0 | 0 | 0 |
| Physical meetings without rooms | 3 | 4 | 2 |
| Elapsed seconds | 105.1 | 130.9 | 51.5 |

Optimise Current saved two campus days and 745 instructor idle minutes (66.2%).
Every instructor's day count was nonincreasing. Section identities and meeting
duration multisets were preserved, with no instructor or room double-bookings
and no incomplete section patterns. Its compaction pass saved a further 185
idle minutes while reducing student gaps by 395 minutes, without increasing
any student's gap or the existing room shortage.

The teacher-first policy has a visible cost: more soft-course unresolved seats.
Core coverage stayed protected. Fresh generation also leaves one additional
physical meeting without a room compared with the original build. Building
permits room shortages, but they still block publication. The compaction pass
itself did not worsen its entry board's shortage. Total idle can improve while
an individual instructor's idle grows; the individual non-regression guarantee
applies to campus days, not to idle minutes.

### Scheduler cohort

The scheduler cohort contains 259 students, 29 sections and five instructors;
its intake and section-sizing rules differ from classic, so these figures are
compared only against the same scheduler input. Both versions have fingerprint
`f2f83fc152bb3b143ba789abd37c66edb697706c4e1948ca0c05eb8387ea1dee`.

| Measure | Original one-seed planner | Changed three-seed portfolio |
|---|---:|---:|
| Instructor working days | 15 | 15 |
| Instructor idle minutes | 65 | 65 |
| Fully seated students | 259 | 259 |
| Actual student clashes | 0 | 0 |
| Unseated demands | 0 | 0 |
| Average student idle minutes | 124.6 | 103.2 |
| Unroomed physical meetings | 3 | 3 |
| Independent hard-rule violations | 0 | 0 |
| Elapsed seconds | 120.8 | 121.5 |

Both reach the model's instructor-day floor of 15; neither proves globally
minimum instructor gaps. Seating was optimal in both runs. Three lab meetings
require 26, 27 and 29 seats while the largest eligible lab has 25, so those
shortages cannot be solved by rearranging this room inventory. Only 9 of 29
sections have instructor links; all staffable sections are assigned. The
validator still reports `UNCERTIFIED` because blocked slots, persisted locks
and some student-rule evidence are not represented in this fresh-build snapshot.

## Verification and practical limits

Targeted regressions cover the reported bugs, teacher-only coordinated moves,
all weekdays and adjacent fixed intervals, custom durations, remote repairs,
generated course identities, exact rollback of persisted metadata, room loss,
locked assignments, unknown seating, portfolio selection, dispatch and both
frontend result flows. An independent adversarial agent reviewed the final
changes and found no remaining material blocker.

Final integration command selected 48 timetable, instructor, scheduler,
rooming, lock, dispatch and objective test files: **897 passed, 1 skipped** in
54.6 seconds. The skip requires PostgreSQL. The frontend wrapper also exercises
19 Node.js interaction regressions. Repository-wide Ruff, both workspace
JavaScript syntax checks, Django system checks and `git diff --check` passed.
The new comparison policy, course identity helper and student seating module
pass scoped mypy. The broader existing advisory type-check backlog remains
(203 errors in the selected legacy modules, down from 220 at baseline); this is
not a clean repository-wide type-check claim.

Bounded local search and CP-SAT do not establish global optimality. Search
timers exclude some setup, model construction and rooming work. Mandatory hard
repairs may require a reported campus-day exception or retain an unresolved
violation when no legal domain exists. No deployment or production release was
performed; the repository's separate Gemini/release gate remains applicable.

Replays are reproducible with `scripts/benchmark_timetable_quality.py`; use
absolute output paths and a fresh scratch path for every invocation. The script
supports saved-board improvement, full generation, scheduler portfolios and
measurement, with optional institutional program, gender and term selection.
