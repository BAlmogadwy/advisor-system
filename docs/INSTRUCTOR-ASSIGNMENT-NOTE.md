# Instructor Assignment — feature note

> ## ⚠️ HISTORICAL — parts of this document describe code that no longer exists
>
> Read this section before trusting anything below it. Three claims in this note are
> **false as of 2026-09-21**, and they are the kind that cost hours:
>
> 1. **`SectionInstructor` as described here was deleted** two and a half hours after it
>    landed (`2ba5bae`, migration `0035`, `DeleteModel` with no data migration). It was
>    replaced by course-level `CourseInstructor(program, course_code, section M/F)`. The
>    reason was a product rejection of scenario-scoped assignment and of the UI, not a
>    defect. A **new, differently-keyed** `SectionInstructor` exists again since migration
>    `0067` — see *Per-section assignment (2026-09)* below. It is not this one.
> 2. **`TIMETABLE_INSTRUCTOR_LINKS_ENABLED` defaults to `true`**, not `false` — changed
>    2026-07-16 (`config/settings.py`). The flag table below is wrong. There is no
>    "flag-OFF by default" safety margin, and **no test compares a flag-ON board against a
>    flag-OFF one**, so every "byte-identical" claim in this note is asserted rather than
>    measured.
> 3. **`tests/test_instructor_assignment.py` does not exist** (deleted in `2ba5bae`). The
>    current tests are `tests/test_course_instructor_assignment.py` and
>    `tests/test_section_instructor_import.py`.
>
> The endpoint list is likewise stale: `ops/instructors/{sections,assign,unassign}` and the
> `ops/tw/...` drawer routes were removed. Current routes are
> `ops/instructors/course-assignments/{,set,clear,assign-bulk,reconcile}`.

A real instructor-assignment feature: a global `Instructor` entity, scenario-scoped
section links, an assignment UI, a teaching-load report, and an opt-in multi-instructor
planner clash. Replaces the 100 %-blank free-text `TermSectionMeeting.instructor`.

## Data model (`core/models.py`, migration `0034`)

- **`Instructor`** — global person (reused across scenarios/terms), mirroring
  `AcademicAdvisor`. `full_name` + `normalised_name` (strip+casefold dedupe key, unique),
  `full_name_ar`, `email` (unique-when-present), `employee_no`, `department`,
  `max_weekly_hours` (advisory), `is_active`.
- **`SectionInstructor`** — scenario-scoped link (FK `term_section` CASCADE, FK `instructor`
  PROTECT, `role` = primary|co|lab). Scenario is derived from `term_section.scenario_id`
  (no scenario FK ⇒ cannot drift). Unique `(term_section, instructor)`.

### Source of truth + write-through cache

`SectionInstructor` links are the **source of truth**. On every assign, the
single write path `core.services.instructor_assignment.set_section_instructors`
**also writes the primary instructor's name into every `TermSectionMeeting.instructor`
of the section** (a display cache). This means the existing free-text-based clash/conflict
readers keep working the moment a section gets an instructor — the structured links then
power the load report and the opt-in multi-instructor clash.

## Flags (`config/settings.py`)

| Flag | Default | Effect |
|---|---|---|
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | **true** | whether the planner runs the instructor-clash filter at all |
| `TIMETABLE_INSTRUCTOR_LINKS_ENABLED` | **false** | where clash ids come from: structured links (multi-instructor) vs the single free-text name |

Roll-out is two-stage and fully reversible:

- **Land state (links flag OFF):** the planner clash reads the free-text name exactly as
  before — byte-identical output. Because assignment write-throughs the primary name, the
  existing single-instructor clash already lights up after an assignment, with no solver
  change.
- **Links flag ON:** a section's meeting clashes if **any** of its assigned instructors is
  double-booked elsewhere ("Dr A / Dr B" → two independently-checked people). Per section it
  is links-or-free-text, never both; sections without links always fall back to the free-text
  path. Recommended after a shadow ON-vs-OFF comparison on a multi-instructor scenario. Env
  override is the kill-switch.

## Endpoints

- Page: `GET /instructor-management/` (sidebar entry under the timetabling block).
- Roster: `ops/instructors/{list,create,update,set-active}/`.
- Assignment: `ops/instructors/{sections,assign,unassign,assign-bulk}/`.
- Load report: `ops/instructors/load-report/?scenario_id=`.
- Workspace drawer: `ops/tw/instructors/`, `ops/tw/sections/<id>/instructors/`.

All writes require General Advisor / Super Admin, are audited, and are blocked on published
scenarios.

## UI

- **Management page** (`core/templates/core/instructor_management.html` +
  `static/js/page-instructor-management.js`): two tabs — *Roster & Assignments* (CRUD +
  scenario-scoped assignment grid + bulk assign) and *Load Report*. Bilingual EN/AR.
- **Load report**: per instructor — sections, distinct courses, total credit hours, weekly
  contact hours, teaching days, time clashes, and a load-status pill vs `max_weekly_hours`.

## Tests

`tests/test_instructor_assignment.py` — model constraints (unique / PROTECT / CASCADE), the
assign service (write-through, clear, name dedupe), endpoints (create+409, assign,
published-block, RBAC), load-report aggregation, and the links helper (active-only). Full
suite green.

## Deferred (clearly-scoped follow-ups)

1. **`seed_instructors` backfill command** — a manual one-off to resolve any future imported
   free-text instructor strings to `Instructor` rows. No-op today (all rows blank); not wired
   into `preDeployCommand`.

### Done since first cut
- **Seed-from-advisor** — the create form pre-fills name/email/department from an existing
  `AcademicAdvisor` (`GET /ops/instructors/advisors/`).
- **Workspace-drawer control (dead)** — the split-workspace placement drawer has an inline
  add/remove editor (`drawerSetInstructors` → `tw_section_instructors_set_view`), but the board
  payload never carried `instructors`, so it always showed "None" and edited nothing. With one
  teacher per section on it is hidden; deleting it is a later clean-up.

## One teacher per section (`TIMETABLE_ONE_TEACHER_PER_SECTION`, default off)

`CourseInstructor` still says who *may* teach a course. With the flag on (it needs
`TIMETABLE_INSTRUCTOR_LINKS_ENABLED`), each section is taught by exactly **one** of those
teachers, or by nobody yet ("teacher to be decided", reported as missing coverage), and the
planner charges clashes and the daily cap to that teacher only.

- **Where the choice lives.** Scenario-scoped `SectionInstructor` rows. The engine writes rows
  with source `engine`; any other primary row (`department`, `registrar`, ...) is a pin it never
  overwrites, and `copied_frozen` is a copy's pin for a section the source did not own. A
  section another scenario also places is frozen: it keeps its owner's teacher.
- **Who picks it.** `core/services/section_teacher_assignment.py`, a staged CP-SAT solve
  (fewest sections without a teacher, then the soft 12-session load, then fewer campus days,
  then less idle, then stability). Hard: no clash, the daily cap, at most 15 sessions a week, and
  D10 (a course with more than 3 sections gives one teacher at most 2 of them). A section not
  placed yet goes only to a teacher who keeps one of its placer options free of clash and the
  cap, so the required-section retry can still place it; a section no teacher can keep a time
  for is "to be decided" (reason clash or cap). The placer keeps the same rule while it places:
  it takes an option that would use up the last free time of another section the same teacher
  still has to place, or a time at which none of a to-be-decided section's teachers could take
  it, only when every option is like that. Such a section is still placed (the required-section
  guarantee); the retry lists it under `teacher_guard_ranked_last`, and the coverage's
  `tbd_sections` say what stops each eligible teacher (`blocked_by`). A map
  in force that already left a missing section no time keeps it (the kept rule), and the run
  results list it under `unplaced_teacher_busy`. Builds re-pick with the board; Optimise Current
  starts from a re-pick bounded per teacher and keeps its end re-pick only when it is no worse.
  Manual moves and repairs never re-pick.
- **With the day-off policy** (`TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY`, see
  `TIMETABLE-CONSTRAINTS.md` §3.1) the solve's third stage counts teachers left with **no
  day off** (5 working days) instead of the soft 12-session load; the greedy hint, the
  fallback and the read path keep the 12-session rank. Optimise Current also re-picks once
  between the search and the post passes (`after_search` in the run's `section_teachers`),
  so the repairs, compaction, recovery and the end re-pick charge that map. Every re-pick
  is bounded per teacher by the board in hand (a day given up for students earlier is
  kept) and judged against the run's start: coverage first, no new clash or over-cap
  session, no teacher's days rising (a swap buys students nothing, so no teacher gives up a
  day for another's day off), and a no-worse (teachers with no day off, idle net of
  allowances, days, idle); refused re-picks report `reason="day_off_policy"`.
- **Link edits.** Adding, removing or deactivating a link writes nothing: a stored row that no
  longer holds is ignored when the map is read, and the section gets a teacher it can have.
- **Display.** The drawer shows the section's teacher (or "to be decided" with the reason); the
  exports name it in the grid and the Instructors sheet; the run results and the quality panel
  show how many sections have a teacher.
- **Reconcile** (`course_assignment_reconcile_view`) writes the map's names into the meetings.
  With the flag on it is refused (409) while a planner job holds the scenario or once it is
  published.
- **Ops note.** After switching the flag on, press **Reconcile** once per scenario, so the
  readers that still use the meeting names (the split workspace's protected-instructor
  hints and search, the grid's instructor line) catch up. The drawer, the badges, the
  exports and the engine already read the map.
- **Parity readers migrated** — `detect_board_conflicts` (`timetable_workspace.py`) and
  `_section_instructor_policy` (`timetable_repair_eligibility.py`) now group by the linked
  instructor ids when `TIMETABLE_INSTRUCTOR_LINKS_ENABLED` is on (per section: links-or-free-text,
  matching the planner). Flag OFF = byte-identical; both have the existing pair-dedupe so a
  multi-instructor section never double-reports.
