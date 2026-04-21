# PR7 — DEFINITION OF READY

## Theme

**Async UX decoupling for long planner runs.**
Submit V2 optimiser runs as in-process background work with persistent status tracking, so a registrar doesn't have to hold a browser tab open for 7 minutes. The existing synchronous path is preserved behind a flag so async is strictly additive.

PR6 answered "how long each stage took" and "how much work each stage did". PR7 uses that observability to make long runs survivable without a browser window staying open.

This is a **plumbing-only** PR. No planner algorithm changes.

### What PR7 is NOT

PR7 is **not** a reliable distributed background-job system. Concretely:

- Jobs are **process-local** — they live in the memory of the single Render web worker that accepted them.
- Jobs are **not durable across deploys or restarts** — a deploy, OOM, or Render-initiated restart drops all `queued`/`running` jobs. The `PlannerJob` row survives as an audit, but its status will stay `running` forever (see commit-2 sweeper note) and must be manually reconciled.
- Cancellation is **cooperative only** — the worker checks `cancel_requested` at stage boundaries. There is no preemption mid-stage.
- This is a **single-web-process async shim**, not a distributed job system. Scaling to multiple workers, queue sharding, or cross-process recovery is explicitly out of scope and requires a separate executor (Celery/RQ) in a later PR.

That framing matters because on Render (single free-tier Starter worker, cold-starts, deploy restarts) the above are real operating conditions, not edge cases.

---

## In scope

### 1. Planner job model

New model:

- `core/models.py` → `PlannerJob`

Fields:

- `id` (UUID primary key)
- `scenario_id` (FK to `TimetableScenario`)
- `board_id` (nullable FK — board-level vs scenario-level)
- `mode` (`"optimise_current"` | `"full_rebuild"`)
- `status` (`queued` | `running` | `succeeded` | `failed` | `cancelled`)
- `submitted_by` (FK to user)
- `submitted_at` / `started_at` / `finished_at`
- `result_json` (JSONField, nullable — holds the full planner result once `succeeded`)
- `error_message` (TextField, nullable — traceback + context string when `failed`)
- `last_stage_seen` (CharField, nullable, values in `{greedy, sa, cpsat, chain, rooming_repair}`) — first-class field written by the runner as each stage boundary is crossed. Lets failure/cancel diagnostics answer "how far did it get" without inferring from PR6 telemetry after the fact. PR6 `stage_ms` is a timing signal; `last_stage_seen` is a progression signal.
- `cancel_requested` (bool — cooperative cancellation flag read at stage boundaries)
- `request_signature` (short hash of scenario + mode + flag fingerprint — used for dedup hint, not enforcement)

Indexes:

- `(scenario_id, status)`
- `(submitted_by, submitted_at desc)`

### 2. Job runner

New module:

- `core/services/planner_job_runner.py`

Responsibilities:

- `submit_planner_job(scenario_id, mode, user)` → creates `PlannerJob` in `queued`, enqueues work, returns job id
- `run_planner_job(job_id)` — worker entry; flips to `running`, invokes the existing `auto_place_scenario` / `optimise_scenario_timetable_v2`, writes `result_payload` + flips to `succeeded`, or captures exception + flips to `failed`
- `cancel_planner_job(job_id, user)` — sets `cancel_requested=True`; worker checks at stage boundaries (reusing PR6 instrumentation points)
- `get_planner_job(job_id)` — thin getter for the polling view
- No planner logic moves here. This module only orchestrates job lifecycle around the existing V2 entry points.

### 3. Queue/executor choice

**Sync-but-in-worker-thread** (Python `concurrent.futures.ThreadPoolExecutor`), **not** Celery/RQ.

Rationale:

- Adding Celery+Redis is a ~1 week infra lift and a second thing to rollback.
- A thread-based executor inside the Django worker is enough for the current volume (1–3 concurrent runs, single Render instance).
- The `PlannerJob` row is the durable audit — the executor is swap-in replaceable with Celery/RQ later without changing the API surface.
- Module exposes a pluggable `EXECUTOR` indirection so a Celery adapter can land in PR8+ without touching callers.

### 4. API surface

New views in `core/views.py` (or a dedicated `core/views_planner_jobs.py`):

- `POST /planner-jobs/` — submit a new job. Request: `{scenario_id, mode, board_id?}`. Response: `{job_id, status, submitted_at}`.
- `GET /planner-jobs/<uuid:job_id>/` — status poll. Response: `{job_id, status, submitted_at, started_at, finished_at, last_stage_seen, result_url?, error?}`. Never includes the full `result_json` in the polling response.
- `GET /planner-jobs/<uuid:job_id>/result/` — fetch the full payload when `status == succeeded`. 404 otherwise.
- `POST /planner-jobs/<uuid:job_id>/cancel/` — cooperative cancel request.

All views are RBAC-gated; job submission + cancel require the current timetable edit permission.

**No list endpoint in PR7.** Retention, pagination, and filter semantics (`?scenario_id=...`, `?status=...`) are deliberately deferred — a list view is not required to prove async execution works, and widens the API contract without proportional value in v1. A registrar-facing list can land in a follow-up PR if ops needs it; the CLI `pr7_job_report` below is sufficient for inspection.

### 5. Sync path preservation

The existing synchronous planner view stays wired and default-on. Async is opt-in via:

- Flag: `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED` (default `False` until promotion commit)
- UI: a single "Run in background" checkbox next to the existing "Optimise" / "Full rebuild" buttons

Flag-off contract:

- All five new endpoints return `404` or a clean "async planner disabled" payload
- Existing sync flow unchanged — byte-equality parity against pre-PR7 master

### 6. CLI acceptance surface

- `python manage.py pr7_job_report [--scenario <id>] [--status <state>]`

Prints a table of recent `PlannerJob` rows with status, mode, duration, `last_stage_seen`, and a `stage_telemetry` summary folded from `result_json` when present. Registrar-operational, not a test gate. The CLI is the ops substitute for the dropped list endpoint.

---

## Out of scope

- Celery / RQ / Redis
- Websocket push notifications
- Job deduplication / idempotency enforcement
- Retry/backoff policy beyond "failed stays failed"
- Multi-tenant throttling
- Distributed worker autoscaling
- Dashboard redesign
- Queue prioritisation
- Planner algorithm changes — **no** placement / scoring / rooming changes
- New telemetry payload fields
- Per-stage progress streaming (PR7 ships boundary status only — `queued → running → finished`)
- PR6 kill-switch changes
- List endpoint (`GET /planner-jobs/?...`) — deferred; `pr7_job_report` CLI covers ops inspection
- **No cross-process job recovery.** A deploy, restart, or OOM drops all in-flight jobs. Rows whose `status` is `queued` or `running` at restart time stay that way until an operator marks them `failed` — no watchdog, no rehydration, no resubmission. This is a deliberate floor, not a bug.

---

## Flag plan

Single flag:

- `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED`

Lifecycle:

- default `False` through commits 1–7
- flipped to `True` at commit 8 (promotion)

Flag-off contract:

- all PR7 endpoints return 404
- no `PlannerJob` rows created in sync flow
- existing views behave exactly as on `467a449` (post-PR6 master tip)

Kill-switch:

- env override: `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false` → live, no redeploy

---

## Acceptance bars

### 1. Flag-off parity with PR6 master tip (`467a449`)

- identical planner payload for the same scenario/mode
- identical `decision_trace`, `stage_telemetry`, `perturbation_metric`
- no new views reachable (404)
- no `PlannerJob` rows created
- verified via a `strip_pr7_fields_for_parity` helper (symmetry with PR5/PR6)

### 2. Flag-on happy path

Submit → poll → fetch:

- `submit` returns `queued` in <100ms
- `poll` transitions `queued → running → succeeded` without intermediate skips
- `result` payload is byte-equal to the payload the sync path would have produced for the same scenario (given identical flag state)
- `stage_telemetry` block populated (PR6 instrumentation active inside the async path)

### 3. Failure capture

- a run that raises inside a stage flips `status → failed`
- `error_message` carries the traceback + a short context string
- `last_stage_seen` is the authoritative "how far did it get" signal (written by the runner at each stage boundary, not inferred from telemetry afterwards)

### 4. Cooperative cancel

- `cancel_planner_job` sets `cancel_requested=True`
- worker observes the flag at the next stage boundary and flips `status → cancelled`
- `last_stage_seen` reflects the last stage completed before cancel
- partial result (if any) is **not** persisted — `result_json` stays null

### 5. Overhead ceiling

- async wrapper adds ≤ **10%** wallclock overhead over the sync path on the same scenario
- measured as p95 of `finished_at - started_at` vs sync baseline
- raised from the initial 5% aspiration: job row I/O, status transitions, cooperative-cancel checks, and per-boundary `last_stage_seen` writes are real costs and the first async cut should not be gated on a brittle 5% ceiling

### 6. Sync path preservation (behavioural)

When `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false`:

- the existing synchronous planner path is the default (unchanged from `467a449`)
- the same scenario/mode/flag state produces an identical planner payload byte-for-byte (`strip_pr7_fields_for_parity` applied to both sides where PR7 adds fields)
- all PR7 endpoints return 404
- no `PlannerJob` rows are created
- all existing planner tests (PR3/PR4/PR5/PR6 packs) pass unchanged

This is a behavioural rule, not a git-diff rule. The implementation will still be mostly additive, but compliance is proven by the payload byte-equality + test-pack stability above, not by the shape of the diff.

### 7. PR7-specific parity helper

- `core/services/timetable_pr7_parity.strip_pr7_fields_for_parity` scrubs PR7-only payload additions (job metadata when echoed back in result) for byte-equality diffs against PR6 master

---

## Commit plan

### Commit 0

- `docs/PR7-DOR.md` (this file)

### Commit 1

- failing tests + tripwires
- scenario-pack additions (PR7 fixtures reusing PR6 grammar)
- parity fixture
- migration stub test

### Commit 2

- `PlannerJob` model + migration
- `core/services/planner_job_runner.py` skeleton (submit + get_status only, no runner logic)
- flag helper
- shape tests green

### Commit 3

- runner execution path (`run_planner_job`) invoking `auto_place_scenario`
- result persistence path
- failure capture

### Commit 4

- cooperative cancellation
- stage-boundary check integrated at the same points PR6 instrumented

### Commit 5

- API views (submit / poll / result / cancel) — **no list endpoint**
- URL routing under `/planner-jobs/`
- RBAC gates

### Commit 6

- UI checkbox ("Run in background") + polling JS
- status-badge UI on the scenario detail page

### Commit 7

- `pr7_job_report` management command
- `strip_pr7_fields_for_parity`
- acceptance pack tests
- flag-off parity fixture against `467a449`

### Commit 8

- flip `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED` default to `True`
- `docs/PR7-PROMOTION-NOTE.md`
- rollback notes
- no logic changes

---

## Tests

Core tests to include:

1. `PlannerJob` model shape + indexes
2. flag-off: all endpoints 404
3. submit creates a queued row
4. poll shows `queued → running → succeeded`
5. result endpoint 404s until `succeeded`
6. async result byte-equals sync result for the same scenario
7. failure capture writes traceback + last stage
8. cooperative cancel at stage boundary
9. sync path parity against PR6 tip
10. RBAC: non-edit user cannot submit or cancel
11. CLI `pr7_job_report` schema + filters
12. parity helper strips PR7 fields correctly

---

## Rollback

Three tiers, same convention as PR3–PR6:

### 1. Env kill-switch

- `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false` → endpoints 404, UI checkbox hidden

### 2. Revert commit 8

- restores default `False`; PR7 code stays in place but nothing uses it

### 3. Revert PR7 merge

- drops the model + views entirely. **Caveat**: existing `PlannerJob` rows orphan. Migration reversal must be tested on a copy of prod DB before running in live.

---

## Non-goals / explicit deferrals

- Celery / Redis / distributed workers → PR8+
- Websocket push → later (polling is fine for current volumes)
- Queue prioritisation → later
- Retry/backoff → later
- Dashboard redesign → later
- Progress streaming within a stage → later (boundary-level is enough given PR6 telemetry)
- List endpoint for `PlannerJob` — deferred to a follow-up PR if ops needs it
- Cross-process job recovery / watchdog / rehydration on restart — explicit non-goal; PR7 is a single-web-process async shim

---

## Implementation cautions

- The threadpool executor must be **module-level singleton** so Django request handlers don't spawn one per request.
- `PlannerJob.result_json` can be large (tens of KB). Do not echo it in the polling response — force callers to hit `/result/`.
- The worker must use `timezone.now()` for `submitted_at` / `started_at` / `finished_at` (durable audit) and `time.perf_counter()` only for the overhead-ratio measurement.
- Cancel check must live at stage boundaries — never inside a stage's inner loop — to avoid corrupting partial placements.
- The async path must re-read the flag on every stage boundary so a mid-run kill-switch flip behaves cleanly (drains current stage, stops before next).
- `last_stage_seen` must be written **as each stage completes** (not retroactively from telemetry). This is the authoritative progression signal — PR6 `stage_ms` stays a timing signal.
- **Restart contract**: `PlannerJob` rows left in `queued`/`running` after a restart are stranded by design. Do NOT add a watchdog or auto-fail sweeper in PR7 — stranded rows are surfaced via `pr7_job_report` and reconciled manually. Adding implicit recovery blurs the PR7 scope floor.

---

## Gate

- PR6 must be fully promoted and deployed (done at `467a449`)
- No open PR6 rollback requests on file
- Render ops verification clean (verified locally 2026-04-21 after Render cold-start; live check deferred until next Render-warm window)

---

## Branch

`refactor/pr7-async-planner-execution` (base: master @ `467a449` = PR6 tip).

---

## Closeout

**All 8 commits landed on `refactor/pr7-async-planner-execution`.**

| # | Sha     | Subject |
|---|---------|---------|
| 0 | 963b652 | DoR — async planner execution |
| 1 | 590eab3 | Failing tests + tripwires + PR7 scenario fixtures |
| 2 | 8b8c8e0 | PlannerJob model + skeleton runner |
| 3 | b19d7a4 | Runner execution path |
| 4 | 6c295f8 | Cooperative cancellation helper |
| 5 | 44e6bba | REST endpoints (submit/poll/result/cancel) |
| 6 | b7b19f7 | ThreadPool dispatcher + UI toggle |
| 7 | d0a893e | Parity helper + pr7_job_report CLI |
| 8 | (this)  | Promotion — flag default → True + promotion note |

**Acceptance.** `tests/test_pr7_async_planner.py` — 18 passed across
14 classes (tripwire, model shape, flag helper, runner happy path,
failure capture, cooperative cancel, four endpoint tests, UI toggle,
parity helper, CLI, flag-off 404 bar, post-promotion default).
PR3 / PR4 / PR5 / PR6 regression packs unchanged.

**Env kill-switch.** `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false`
reverts the shim at runtime with no redeploy — all four endpoints
404, no PlannerJob rows created, UI toggle hidden. Exercised by
`TestFlagOffAllEndpoints404`.

**Promotion note.** See [docs/PR7-PROMOTION-NOTE.md](PR7-PROMOTION-NOTE.md)
for the three-tier rollback and scope floor / caveats.
