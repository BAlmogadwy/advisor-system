# MIGRATION_ANALYSIS.md

Date: 2026-02-12

## Objective
Migrate functional logic from `import_old/` into the new Django project while preserving existing behavior baselines and enforcing the max-safety workflow.

## Source Snapshot (imported)
- `import_old/phase_1_scraper/*` (portal scraping + HTML parsing)
- `import_old/phase_2_recommender/*` (classification + recommendations)
- `import_old/database/*` (SQLite schema + inserts)
- `import_old/utils/student_helpers.py` (normalization + prereq helpers)
- `import_old/BASELINE.md` (behavior invariants)
- `import_old/ACCEPTANCE.md` (acceptance checks)

## Critical Findings
1. **Hardcoded credentials present** in `import_old/config/settings.py`:
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD`
   - Must be moved to environment variables; rotate credentials if these are real.
2. Old app is largely script-based and DB-coupled to `database/advisor.db`.
3. Baseline behavior is clearly documented and should be treated as non-regression contract.

## Behavior Contracts to Preserve
- Course code normalization semantics (`normalize_code`)
- Parser output structure and timetable parsing heuristics
- Course status classification logic (passed/studying/not_taken)
- Two prerequisite semantics:
  - tolerant normalized (`get_prerequisites`)
  - visualizer exact (`get_prerequisites_visualizer_style`)
- Recommender rules:
  - prereq satisfied by passed OR studying
  - max credits 18
  - ranking/tie-break policy order
  - student term calculation formula

## Proposed Target Architecture (Django)
- `core/services/normalization.py` → `normalize_code`
- `core/services/parser.py` → `parse_study_plan`, `parse_timetable`
- `core/services/classifier.py` → `classify_courses`
- `core/services/recommender.py` → recommendation engine
- `core/repositories/sqlite_repo.py` → DB access wrappers (initially keep sqlite direct for parity)
- `core/management/commands/*` → wrappers for batch scraper/recommender
- `core/api/views.py` → minimal endpoints:
  - `/health/`
  - `/recommend/<student_id>/`

## Migration Strategy (Low-Risk)
### Milestone 1: Lift-and-stabilize (no behavior change)
- Copy old logic into namespaced Django service modules.
- Keep SQLite schema compatibility with existing DB file.
- Add regression tests mirroring `BASELINE.md` contracts.

### Milestone 2: Deterministic test harness
- Add fixture DB (small deterministic graph).
- Golden tests for parser/classifier/recommender/helper functions.
- Ensure output order and values exactly match baseline.

### Milestone 3: Django integration
- Add API endpoint(s) calling service layer.
- Add management commands for batch flows.
- Keep old scripts callable as compatibility wrappers.

### Milestone 4: Security + config hardening
- Remove hardcoded secrets.
- Use `.env` / environment variables.
- Add deployment-safe settings split (dev/prod).

## Immediate Next Actions
1. Confirm DB strategy:
   - Reuse `import_old/database/advisor.db` as authoritative source for now? (recommended for parity)
2. Confirm portal creds handling:
   - Move to env vars and rotate old creds.
3. Start Milestone 1 implementation in small commits.

## Risks
- Hidden behavior in data-dependent scripts may diverge if DB changes.
- Parser robustness depends on portal HTML structure stability.
- Existing tests are weak; stronger deterministic tests are required before refactors.
