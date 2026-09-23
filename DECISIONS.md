# DECISIONS.md

## ADR-001: Delivery workflow
- **Decision:** Use Max-Safety Production Mode (Codex + Gemini + final review pass)
- **Status:** Accepted
- **Date:** 2026-02-12
- **Rationale:** Higher reliability and lower production risk through adversarial review and independent release gate.

## ADR-002: Backend framework
- **Decision:** Django
- **Status:** Accepted
- **Date:** 2026-02-12
- **Rationale:** User-selected framework for this project.

## ADR-003: Promote the V2.1 student adviser in production
- **Decision:** Enable `STUDENT_ADVISOR_V21_ENABLED` in the Render Blueprint while
  retaining the V2 dispatcher as the single-flag rollback target.
- **Status:** Accepted
- **Date:** 2026-09-02
- **Rationale:** The versioned semantic-plan, privacy, rendering, browser, and
  regression gates now cover the human-adviser voice and deterministic grounded
  answers. Keeping V2 enabled preserves an immediate fail-closed rollback path.

## ADR-004: Acquire SQLite write transactions eagerly
- **Decision:** Configure writable SQLite connections with Django's `IMMEDIATE`
  transaction mode and a 30-second busy timeout; leave PostgreSQL and read-only
  frozen fixture connections unchanged.
- **Status:** Accepted
- **Date:** 2026-09-02
- **Rationale:** Local adviser and scraper operations can otherwise form a stale
  read snapshot that cannot be promoted to a writer. Acquiring the SQLite writer
  slot at atomic-block entry makes the configured timeout effective and prevents
  intermittent lock failures without changing production PostgreSQL semantics.

## ADR-005: Synchronize registrar timetable data with a guarded natural-key delta
- **Decision:** Publish local timetable changes through the versioned
  `academic_timetable_delta.v1` export/import workflow. The importer is fixed to
  academic year `1448`, term `1`, and source `scraper_timetable`; it replaces the
  complete registrar-section set only for explicitly named, roster-matched
  students and never deletes global sections.
- **Status:** Accepted
- **Date:** 2026-09-02
- **Rationale:** A production database contains accounts, sessions, conversations,
  planner state, OTPs, and production-only students that do not exist in a local
  SQLite snapshot. Replacing or loading that snapshot would destroy live state.
  A SHA-pinned, count-pinned, natural-key delta can update the intended registrar
  slice atomically while preserving production-only and non-registrar data.
- **Consequences:** The importer fails closed when the production logical state,
  touched-student roster fields, migrations, or expected operation counts differ
  from the frozen baseline. New roster records, roster-field changes, orphan
  sections, empty meeting extracts, and non-registrar relationship changes are
  excluded and require a separate reviewed workflow.

## ADR-006: A prerequisite naming a course outside its programme's plan is a defect
- **Decision:** Treat any `Prerequisite` row whose `course_code` or
  `prerequisite_course_code` is absent from that programme's plan — its
  `ProgrammeRequirement` rows plus its `ElectiveCourse` catalogue, excluding
  `N(HOURS)` pseudo-prerequisites — as a data defect.
  `core.services.curriculum_integrity` is the authority; it is surfaced by
  `run_integrity_checks` on the DB-admin screen and by the read-only
  `curriculum_integrity_report` command.
- **Status:** Accepted
- **Date:** 2026-09-06
- **Rationale:** Nothing in the schema ties a prerequisite's codes to the plan it
  governs, so such a row can never be satisfied and blocks its course for every
  student in the programme — silently, because an unsatisfiable prerequisite
  raises nothing and the graduation forecast merely returns no estimate.
  `DS2/MATH471 -> MATH204` cost all 191 DS2 students their forecast and was found
  only by tracing back from the missing number. It was the sole such row in 403.
- **Consequences:** `course_priority.build_program_dependency_graph` deliberately
  retains both endpoints of such a row so importance scoring never drops an edge.
  That tolerance is a scoring decision and is **not** an endorsement; its
  docstring now says so and points here. The check is an audit, not a write-path
  guard: the Oracle plan importer inserts a course's prerequisites immediately
  after its own requirement row, so forward references within one import are
  legitimate and momentary. It is also not wired into CI, where every job runs
  against an empty database and the check would prove nothing; `tests/
  test_curriculum_integrity.py` builds a plan first so it cannot pass vacuously.

## ADR-007: Exam timetable actions run as one-at-a-time background jobs in the web process
- **Decision:** Build, Optimize, Fix and Save of the exam timetable run as an
  `ExamTimetableJob` on a dedicated thread inside the web process, behind
  `EXAM_JOBS_ENABLED`. The page submits (202), polls the job's named stages, and
  fetches the action's own status and body when it ends. One job may be active
  at a time across all users, enforced by a partial unique constraint; a second
  submit is refused (409) with the holder and stage, never queued. The job saves
  its run and marks itself SUCCEEDED in one transaction. A job whose heartbeat
  stops is failed, never retried. CP-SAT work in the process — exam jobs, the
  planner's jobs and, with jobs on, the exam Check — takes turns on one solver
  slot (`core.services.job_runtime`).
- **Status:** Accepted
- **Date:** 2026-09-23
- **Rationale:** The registrar asked to see progress. The actions held a request
  thread for up to two minutes on a 0.5-CPU, 512 MB instance and reported nothing
  until they ended. A worker service would redeploy on the same push, cost a
  second instance, and share the Telegram worker's memory if co-located; a
  progress side-channel would still pin a request thread and lose the build on
  reload. Three reviews (architecture, backend, frontend) chose the in-process
  job and found the traps it must avoid: a thread pool's exit hook starts queued
  work during shutdown, so there is no queue; a progress write from inside the
  evaluation's transaction was invisible until commit, so progress is written
  from its own thread and connection and the read-only evaluation no longer
  opens a transaction; an OOM-killed job leaves its row RUNNING, so staleness is
  judged by heartbeat.
- **Consequences:** `execute_exam_action` is the single implementation both paths
  call, so the synchronous view is a rollback the page cannot tell apart. A job
  survives a deploy only if it finishes within gunicorn's graceful window after
  SIGTERM (~150 s); otherwise the sweep reports it honestly. A second registrar
  waits for the first to finish. Multistart stays synchronous: it persists
  several candidate runs outside one transaction.
- **Rollout (2026-09-23):** jobs are on by default; `EXAM_JOBS_ENABLED=false` in
  the service's environment is the rollback, and the page handles both answers.
  Only a request carrying `X-Exam-Jobs: 1` - the page that can follow a job -
  is answered with one. A tab still running the previous release's script, or
  any other client, has the action run inside its request as before, but it
  now takes turns: 409 `job_in_progress` while a job holds the lane, 503
  `solver_busy` with `Retry-After` while a Check, a job or a planner run holds
  the solver. The previous release's script shows those as the server's English
  error text. The solver
  slot records what holds it, so a turned-away Check or a queued job can say it
  is waiting on a timetable-planner run (twelve to fifteen minutes) rather than
  another exam action. With jobs on, a planner run therefore also holds up the
  exam Check and Save for its whole length: the memory trade this decision
  exists for.
