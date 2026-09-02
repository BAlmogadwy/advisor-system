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
