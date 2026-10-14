# ENG101 / ENG102 weekly patterns

Local implementation and validation on 2026-10-10. This applies to new
generation; no saved timetable was migrated or deleted.

## Contract

Only exact normalized ENG101 and ENG102 registrar identities use this rule.
Every placed section has exactly four continuous lecture meetings, on Sunday,
Monday, Wednesday and Thursday. The whole section selects either 09:00–12:00
on all four days or 13:00–16:00 on all four days. Tuesday, mixed periods,
partial sections and shortened sessions are invalid.

Neither course needs a room assignment. They do not consume shared-room
inventory or produce room shortages, room clashes or missing-room repair
actions, including when optional room text exists on a meeting. Their teaching
times still participate in instructor clashes, working days, gaps, daily caps
and student feasibility. This room exemption does not mark them as online.

## Implementation

- `timetable_course_policy.py` owns the immutable patterns. Both scheduling
  engines, classic solvers, pattern catalogs, editors and certifiers use it.
  Registrar prefix/number and full offering keys resolve the same policy.
- Generate selects a complete weekly bundle. Parallel sections are permitted;
  the actual shared instructor remains a hard constraint. Three-hour English
  meetings stay lectures and use course-scoped windows rather than lab slots.
- Optimise, simulated annealing, load balancing, CP-SAT polishing, instructor
  compaction and repairs either switch the whole bundle or retain it. Optional
  changes protect every instructor's days and idle time, hard student coverage,
  room quality and contact time. Invalid persistence rolls back both board
  placements and canonical meeting metadata.
- Manual create/switch/remove and repair apply act atomically. A lock on any
  constituent meeting protects the bundle, including across board copies.
  Consistent copies count as four teaching meetings; partial/inconsistent copies
  are rejected. Room and instructor metadata, locks and placement identities
  survive valid switches. Undo/Redo also remaps IDs after recreation.
- Main and Split show continuous English grids and exactly two pattern choices.
  The inspector and XLSX exports say **No room required**. Ordinary course
  grids, room editing, optimisation options and exports remain available.
- Workspace pages initialize from a valid explicit scenario's year and term;
  Split also honors a newly created scenario instead of opening an unrelated
  default. Scope values are escaped in JavaScript so Unicode or blank fields
  cannot stop the page from loading.

## Automated checks

- Related Python regression checks: **989 passed, 1 skipped**. The skipped
  PostgreSQL-specific import test requires a PostgreSQL environment.
- Timetable frontend behavior tests: **75 passed**, including complete bundle
  history sequences through create, switch, remove, Undo and Redo.
- All modified/untracked Python source passes Ruff. Django system checks and
  main/Split/shared-policy JavaScript syntax checks pass.
- Combined mypy checks pass for the three new policy, fixed-pattern search and
  manual section-policy modules.
- Independent subagent final review: no remaining concrete blocker; **74**
  targeted English Python tests and **10** English frontend tests passed.

## Fresh real-data checks

SQLite read-only backups of the institutional database were used for isolated
new scenarios. Browser writes were confined to the disposable term-2 copy.
The original saved scenarios and database were not changed by these tests.

| Check | Result |
| --- | --- |
| Classic Generate, term 2, AI/AI2/DS/DS2 | 405 students, 82 sections, 187 meetings; four ENG102 sections: one AM, three PM; all four satisfy the policy |
| Optimise that fresh term-2 scenario | All four ENG102 sections remain valid, now PM; actual assigned student clashes remain 0; soft unresolved seats 31 → 17; ordinary-course room shortages 9 → 3 |
| Alternative scheduler, fresh term 2 | 404 students, 72 sections; zero hard rule violations, including complete weekly patterns; 11 ordinary-course room shortages remain, so the whole board is not certified |
| Classic Generate, fresh term 1 | 258 students, 44 sections, 100 meetings; no new ENG101 demand in this cohort |
| Optimise that fresh term-1 scenario | Instructor teaching days 21 → 21; idle minutes 670 → 395; every instructor's days and idle time nonincreasing; soft unresolved seats 4 → 0; room shortages remain 2; assigned clashes remain 0 |

Term 2 has no eligible instructor links in this source data, so its zero
instructor metrics are not evidence of teacher scheduling quality. Targeted
instructor fixtures cover both English codes, real full-window clashes, the
two-bundle capacity of one instructor, whole-bundle gap improvements, locks and
rollback. The term-1 real-data run independently checks teacher non-regression.

Reports: `snapshots/eng-final-term2-20261010.json`,
`eng-final-scheduler-term2-20261010.json`,
`eng-final-optimise-term1-20261010.json` and
`eng-final-optimise-term2-20261010.json`. The earlier fresh term-1 generation is
recorded in `snapshots/eng-classic-term1-20261010.json`.

Browser verification on the fresh term-2 copy covers main/Split pattern changes,
full-section Undo/Redo, lock protection, continuous four-day cells and the
no-room inspector. Split opens the correct explicit term-2 scenario. The local
user server at `http://127.0.0.1:8001` was restarted with the final code.
Screenshot: `snapshots/eng-pattern-browser-20261010.png`.

## Local readiness and release

The build and independent final local review pass. No deployment, commit or
production release was performed. `WORKFLOW.md` separately requires a Gemini
adversarial pass for production release; Gemini was unavailable in this session,
so that release gate is not claimed complete.
