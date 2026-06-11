# Project Memory

This file captures durable context from the current development session. Use it
as the first local reference when continuing timetable optimisation work.

## Current Product North Star

The timetable builder is being optimised for actual student outcomes.

Primary metric:

- Minimise the number of unresolved students.
- The goal is to register all scenario students where feasible.

Secondary metrics:

- Protect already registered students from losing courses.
- Minimise moved students and section changes.
- Keep hard timetable constraints valid.
- Treat cross-board conflict counts as diagnostic/safety signals, not the main
  optimisation target.

## Timetable Repair / Move Optimisation Direction

The selected-section slot workflow should answer:

> If section X moves to a new slot, can the system rearrange students across
> available sections, possibly across multiple courses, so blocked students can
> register while existing registrations are protected as much as possible?

This is not a simple empty-slot check and not only a direct unresolved-count
preview. Candidate slots should be evaluated after the best available student
reallocation is solved.

## Graph Usage Clarification

The current selected-section move optimiser does not use Neo4j or the timetable
graph twin as its decision engine.

What is used now:

- An in-memory, graph-shaped affected-component expansion:
  students -> current/requested courses -> sections -> neighbouring students.
- Solver-native indexes such as `sections_by_course`, `students_by_section`,
  `courses_by_student`, and `requested_courses_by_student`.
- CP-SAT/min-cost-flow/LNS repair solving on the bounded component.
- Student time-conflict edges are converted into `AddAtMostOne` and pairwise
  CP-SAT constraints.

What exists separately:

- `core/services/timetable_graph_twin.py` builds a disposable scenario graph
  and can sync/view Neo4j data for relationship analysis and explanation.
- That graph twin is useful for diagnostics, visual explanation, and future
  cascade visualization, but it is not currently wired into
  `SectionMoveOptimisationEngine`.

## Naming Decision

Public/service concept:

- `SectionMoveOptimisationEngine`

Reason:

- The feature is broader than "repair"; it evaluates section movement options
  by actual student-registration outcomes.

Important implementation detail:

- `core/services/section_move_optimisation.py` is the public facade for the
  selected-section workflow.
- `core/services/timetable_repair.py` remains the audited implementation layer
  for candidate generation, solving, approval, apply, rollback, reporting,
  jobs, and global plans.
- Do not delete `timetable_repair.py` just because the UI wording changed. It
  is still active infrastructure.

## Recently Completed

- Refactored the selected-section repair flow into blueprint-shaped component
  boundaries in `core/services/section_move_optimisation_components.py`.
- `analyse_timetable_repair` now builds the affected component through
  `AffectedComponentBuilder`.
- `evaluate_repair_candidates` now routes candidate generation, candidate
  solving, objective ranking, impact scoring, and audit persistence through the
  blueprint components instead of direct inline orchestration.
- Added focused component tests in
  `tests/test_section_move_optimisation_components.py`.
- Added `SectionMoveOptimisationEngine` facade.
- Routed selected-section analyse/detail/approve/apply/rollback views through
  the new facade.
- Kept audited repair functions as the underlying implementation for jobs,
  global plans, rollback, reports, and tests.
- Changed selected-section visible wording toward "Move optimisation".
- Removed obsolete second-click exact-repair UI branch. Repair/optimisation
  slots now use the direct apply-ready flow.
- Added generated-scenario baseline materialisation so an applied candidate can
  write audited `StudentTermSection` rows when a generated scenario did not
  already have persistent student-section assignments.
- Changed exact repair candidate preselection so scanned slots are ordered by
  actual student-outcome preview first. The CP-SAT budget now goes to slots
  that reduce unresolved students/courses before visually clean board slots.
- Changed global unresolved-student scan target selection to cover one
  placement from each unresolved-course hotspot before spending remaining scan
  budget on extra sections of the same course.
- Fixed simulation run rows so unsolved/no-exact candidates no longer appear
  as `unresolved_blocked = 0`; they carry the target unresolved count and rank
  behind real recovery.
- Added explicit selected-section move scopes:
  `single_session`, `all_sessions`, and `lectures_only`.
- Scoped moves are carried as a candidate move-set through slot generation,
  actual student-outcome preview, CP-SAT meeting replacement, apply preflight,
  apply, and rollback. `single_session` remains the default.
- The first multi-session implementation preserves the selected section's
  existing relative day/time pattern when evaluating companion lecture/lab
  sessions.
- Bumped the repair cache version to avoid reusing old candidate ordering
  results.
- Verified the direct slot apply path in the browser on a generated scenario:
  approval and apply succeeded, placement moved, student changes were written,
  and rollback remained available.

## Current UX Expectations

- The first quick board-level slot badges should not be treated as final
  student-outcome evidence.
- During actual section-move optimisation, show the UI as actively calculating
  rather than showing misleading fast results.
- A valid apply-ready slot should allow direct click to approve/apply the exact
  audited candidate.
- Rejected slots must show why they were rejected.
- Filtered panes should be treated as filtered views for slot availability.
  Do not reject a filtered AI pane slot just because a non-filtered hidden
  section occupies the same visual pane slot.

## Data/Domain Notes

- `StudentTermSection` is the persistent student-section assignment table.
- Generated scenarios may initially have no `StudentTermSection` rows; the
  evaluator baseline can be materialised at apply time when needed.
- Scenario-level student demand is represented through scenario demand/request
  structures. Avoid adding duplicate request models unless the app truly needs
  a new normalized source of truth.

## Useful Verification Commands

```powershell
python manage.py check
python -m py_compile core\services\section_move_optimisation.py core\timetable_workspace_views.py core\services\timetable_repair.py core\services\timetable_repair_domain.py
node --check static\js\page-timetable-workspace-split.js
```

## Local Entry Points

- Planner: `/planner/`
- Timetable workspace: `/timetable-workspace/`
- Split timetable workspace: `/timetable-workspace/split/`
- Backend API namespace used by the split workspace: `/ops/tw/`
