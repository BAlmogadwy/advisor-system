# PR7 Promotion Note — async planner execution

_Status:_ PR7 commit 8 flips `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED` default
to `True`. The full async execution path (PlannerJob audit row +
ThreadPool dispatcher + submit/poll/result/cancel endpoints + UI
toggle) is now live.

## Scope

PR7 is an **async UX shim** — it lets the timetable optimiser run off
the request thread so long planner runs don't freeze the browser. It
is **not** a distributed job system.

### What PR7 IS

- Process-local `PlannerJob` rows (status / last_stage_seen /
  cancel_requested / result_json / error_message).
- Single-worker `ThreadPoolExecutor` that runs `run_planner_job`
  against the existing V2 pipeline (unchanged semantics).
- Flag-gated REST surface: `POST /planner-jobs/`, `GET /planner-jobs/<id>/`,
  `GET /planner-jobs/<id>/result/`, `POST /planner-jobs/<id>/cancel/`.
- Minimal "Run in background" toggle on the scenario detail page.
- `pr7_job_report` read-only CLI over the PlannerJob table.

### What PR7 IS NOT (scope floor)

- **Not durable across process restarts.** A running job whose worker
  thread dies with its web process transitions to no terminal state
  until the next manual sweep. No Celery, no Redis, no external queue.
- **Single-worker only.** Concurrent submits queue behind each other.
- **Cooperative cancellation only.** `cancel_requested=True` is
  observed at each stage boundary; a runaway C-extension stage cannot
  be pre-empted.
- **No cross-process recovery.** If Gunicorn cycles the worker mid-run,
  the PlannerJob row may stay in `running` status indefinitely.

## Rollback (three tiers)

1. **Env kill-switch (runtime, no redeploy).** Set
   `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false` in the Render env and
   restart. All four `/planner-jobs/*` endpoints revert to 404, no
   PlannerJob rows created, UI toggle hides. Pre-PR7 behaviour is
   byte-identical.
2. **Revert commit 8.** `git revert <c8-sha>` flips the settings
   default back to `False`. Code paths remain in the tree but dormant.
3. **Revert the PR7 merge.** `git revert -m 1 <merge-sha>` removes
   every PR7 touch site. Migration `0023_add_plannerjob` becomes
   reversible via `python manage.py migrate core 0022`.

## Caveats

- **`PYTEST_CURRENT_TEST` sync-dispatch is a test-harness quirk only.**
  In-memory SQLite cannot be flushed across test teardown while a
  background thread holds a connection. When `PYTEST_CURRENT_TEST` is
  set in the environment, `dispatch_planner_job` runs synchronously
  and returns an immediate `Future`. This branch is never taken in
  production.
- **`last_stage_seen` is the authoritative progression signal**,
  distinct from PR6 `stage_ms` timing. Derived from stage boundaries
  only; never inferred from missing / zero telemetry defaults.
- **`error_message`** stores a compact summary + last six traceback
  lines, capped at 4000 characters. Full tracebacks are not persisted.

## Acceptance coverage

- `tests/test_pr7_async_planner.py` — 18 tests across 14 classes:
  tripwire, model shape, flag helper, runner happy path, failure
  capture, cooperative cancel, four endpoint tests, UI toggle, parity
  helper, CLI, flag-off endpoint 404 bar, post-promotion default
  assertion.
- CLI: `python manage.py pr7_job_report --limit 20`.
- Regression packs (PR3 / PR4 / PR5 / PR6) unchanged.
