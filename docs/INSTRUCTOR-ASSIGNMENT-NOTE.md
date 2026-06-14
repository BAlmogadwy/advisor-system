# Instructor Assignment — feature note

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
- **Editable workspace-drawer control** — the split-workspace placement drawer shows instructor
  chips and an inline add/remove editor (`drawerSetInstructors` → `tw_section_instructors_set_view`),
  reloading panes so clash badges recompute. Disabled on published scenarios.
- **Parity readers migrated** — `detect_board_conflicts` (`timetable_workspace.py`) and
  `_section_instructor_policy` (`timetable_repair_eligibility.py`) now group by the linked
  instructor ids when `TIMETABLE_INSTRUCTOR_LINKS_ENABLED` is on (per section: links-or-free-text,
  matching the planner). Flag OFF = byte-identical; both have the existing pair-dedupe so a
  multi-instructor section never double-reports.
