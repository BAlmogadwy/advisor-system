# PR9 — Definition of Ready

## Theme

**Planner debt consolidation.**

After PR1–PR8 the planner stack is rule-correct, failure-visible,
explainable, stage-traced, async-capable, and UI-visible. Before the
next feature cycle, reduce maintenance risk by consolidating the
debt that has accumulated across the last eight PRs. No user-facing
feature promises.

---

## What PR9 is

A small, high-confidence cleanup pass across the planner stack:

- retire residual legacy counters / compatibility shims
- unify helper duplication and stage-vocabulary inconsistencies
- stabilise test infrastructure (document or remove test-only quirks)
- targeted mypy tightening on planner files only
- one living architecture note tying the current surface together

---

## What PR9 is NOT

- not PR9 "multi-job dashboard"
- not another planner feature
- not broad repo cleanup outside the planner area
- not a whole-repo mypy pass
- not a refactor that changes planner outputs
- not a test rewrite
- not a new flag

---

## In scope

### 1. Repo-hygiene sweep (untracked cruft)

Remove or track the untracked artefacts that have outlived their
purpose:

- `core/services.zip` — 80-file backup of `core/services/` predating
  the PR3 refactor. Branches have diverged; it is dead weight.
- `prod_seed_rooms.sh`, `rooms_F_seed.json`, `rooms_merged.json`,
  `rooms_seed.json`, `seed_f_rooms.py`, `seed_m_rooms.py` — seed
  scripts used once and never committed. If still operationally
  needed, move under `scripts/seed/`; otherwise delete.
- `snapshots/planner-refactor-2026-04-20/core/`,
  `snapshots/planner-refactor-2026-04-20/static/` — mid-refactor
  source snapshot; never referenced by tests or docs. Delete.
- `static/mockups/` — design mockups from session 21, not served by
  any view. Delete.

### 2. Flag-helper consolidation

`is_stage_telemetry_enabled` (PR6), `is_async_planner_enabled` (PR7),
`is_async_job_ui_enabled` (PR8) live in three modules. Consolidate
to a single `core/services/timetable_flags.py` so callers import
from one place. Back-compat re-exports only where a test or external
caller already imports from the old path.

### 3. Stage-vocabulary unification

The five-stage vocabulary (`greedy`, `sa`, `cpsat`, `chain`,
`rooming_repair`) is reused in `timetable_stage_telemetry`,
`timetable_pr5_parity`, `planner_job_runner`, and the PlannerJob
`STAGE_CHOICES` tuple. Pin it in one module and have all four
callers import the sequence and the membership set from there.

### 4. Provenance-only lint guard

PR5 documented the rule: `stage_origin` / `stage_context` are
provenance-only — MUST NOT affect placement or rooming decisions.
Add a single unit test asserting that planner output for a fixture
does not change when those fields are artificially mutated. Cheap
insurance.

### 5. Test-infrastructure documentation

Consolidate the PR7 `PYTEST_CURRENT_TEST` sync-dispatch quirk, the
PR6 shadow-test `Room.objects.all().delete()` teardown idiom, and
the single-worker test-pool convention into one short note:
`docs/TEST-INFRA-QUIRKS.md`. Linked from `PR7-PROMOTION-NOTE.md`
and `PR8-PROMOTION-NOTE.md` (they currently own this knowledge in
isolation).

### 6. Targeted mypy tightening

Only on files the planner-refactor touched in PR1–PR8:

- `core/services/planner_job_runner.py`
- `core/services/pr8_async_job_ui.py`
- `core/services/pr8_parity.py`
- `core/services/timetable_pr5_parity.py`
- `core/services/timetable_pr6_parity.py`
- `core/services/timetable_pr7_parity.py`
- `core/services/timetable_stage_telemetry.py`

Goal: every callable annotated, no `Any` return types that could be
tightened. Not running mypy in CI yet.

### 7. Architecture note

One living document: `docs/PLANNER-ARCHITECTURE.md`. Under 200 lines.
Covers:

- entry points (`auto_place_scenario`, `auto_place_board`, async
  `run_planner_job`, workspace page, REST endpoints)
- payload schema and the stage-ordered keys
- all planner flags and their kill-switch behaviour
- stage order + vocabulary
- rollback pointer (PR3–PR8 promotion notes)

Not a reference manual — a one-sheet future-you map.

---

## Out of scope

- new flags
- planner output changes
- scoring / heuristic changes
- PlannerJob schema changes
- endpoint additions / removals
- test rewrites
- whole-repo mypy / ruff tightening
- dependency upgrades
- frontend refactors beyond what the UI wire-in in PR8 touched

---

## Acceptance bars

### 1. Planner-output neutrality

`tests/test_pr3_acceptance_pack`, PR4, PR5, PR6, PR7, PR8 suites all
remain green with byte-identical placements / scores / telemetry on
existing fixtures. PR9 must not move a single planner output.

### 2. Import-path back-compat

After consolidating flag helpers, all existing imports from old
module paths still resolve. No caller rewritten outside the
refactored files.

### 3. Test count

Full suite count does not drop. Any new tests added under §4
increase the total; no deletions.

### 4. Docs parity

Every file removed from repo cruft that is referenced anywhere in
docs/tests is handled (either kept + tracked, or references updated).
No broken links.

---

## Commit plan

| # | Scope |
|---|-------|
| 0 | `docs/PR9-DOR.md` |
| 1 | repo-hygiene sweep (zip / seed scripts / mockups / snapshot dirs) |
| 2 | consolidate flag helpers into `core/services/timetable_flags.py` |
| 3 | stage-vocabulary single source of truth |
| 4 | provenance-only lint test for PR5 stage_origin / stage_context |
| 5 | `docs/TEST-INFRA-QUIRKS.md` consolidation |
| 6 | targeted mypy tightening on PR-touched planner files |
| 7 | `docs/PLANNER-ARCHITECTURE.md` one-sheet map |
| 8 | no-op close: test-count + regression-pack snapshot in DoR |

No promotion flag — PR9 is debt only, no runtime behaviour changes.

---

## Test plan

No new test file. Additions:

- `tests/test_pr5_stage_provenance_inert.py` — fixture-driven
  assertion that mutating `stage_origin` / `stage_context` does not
  change placement outputs.
- Any new assertions folded into existing PR5 / PR7 / PR8 suites
  where appropriate.

---

## Rollback

Debt-only PR, so rollback is straightforward:

1. Revert individual commits for any change that surprises a consumer
2. Revert the PR8 merge if needed

No flag, no env switch, no migration.

---

## Branch

`refactor/pr9-debt-consolidation` (base: master @ `9d152a4` = PR8 tip).

---

## Closeout

**8 commits landed on `refactor/pr9-debt-consolidation`.**

| # | Sha     | Subject |
|---|---------|---------|
| 0 | 579b454 | DoR — debt consolidation |
| 1 | 39b6786 | Repo-hygiene sweep |
| 2 | 796f3b8 | Consolidate flag helpers into `timetable_flags` |
| 3 | f4f9f39 | Stage-vocabulary single source of truth |
| 4 | e535737 | Provenance-only lint test for PR5 stage fields |
| 5 | 64e8dbc | Consolidate test-infra quirks into one doc |
| 6 | 7c7f476 | Targeted mypy tightening on PR-touched planner files |
| 7 | e304681 | Planner architecture one-sheet map |
| 8 | (this)  | Closeout — no-op, DoR update only |

**Test count.** 428 passed / 2 skipped (was 427 / 2 pre-PR9, +1 from
`test_pr5_stage_provenance_inert`). No regressions on PR3–PR8
acceptance packs.

**Removed from repo.** `core/services.zip`, `prod_seed_rooms.py`,
`prod_seed_rooms.sh`, `rooms_F_seed.json`, `rooms_merged.json`,
`rooms_seed.json`, `seed_f_rooms.py`, `seed_m_rooms.py`,
`snapshots/planner-refactor-2026-04-20/core/`,
`snapshots/planner-refactor-2026-04-20/static/`, `static/mockups/`.

**Newly tracked.** `core/services/timetable_flags.py`,
`tests/test_pr5_stage_provenance_inert.py`,
`docs/TEST-INFRA-QUIRKS.md`, `docs/PLANNER-ARCHITECTURE.md`.

**Architecture note.** See [PLANNER-ARCHITECTURE.md](PLANNER-ARCHITECTURE.md).
