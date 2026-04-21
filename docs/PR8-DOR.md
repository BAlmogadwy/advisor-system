# PR8 — Definition of Ready

## Theme

**Async job UX for planner runs.**

PR7 shipped the execution substrate:

- background job model
- submit / poll / result / cancel endpoints
- cooperative cancellation
- kill-switch
- async default-on

PR8 makes that usable from the scenario page without manual endpoint
polling.

---

## What PR8 is

A **minimal, single-job user experience** on the scenario page that
lets a user:

- submit a planner run in background
- see current job status
- poll automatically while active
- cancel while cancellable
- open the final result when ready
- see a clear failure state when the job fails

This is a **UI + thin client orchestration** PR, not a planner-logic PR.

---

## What PR8 is NOT

PR8 is **not**:

- websocket / SSE streaming
- multi-job history browser
- cross-page job dashboard
- retries / auto-restart
- progress percentages
- stage timing charts
- planner algorithm changes
- new planner payload semantics
- cross-process recovery
- admin analytics panel
- bulk job operations
- notifications / email / toasts beyond minimal inline status

Single-job scope only, on the scenario page.

---

## In scope

### 1. Single-job status card on scenario page

A compact planner job card on the scenario detail page, showing one
of: no active job / queued / running / succeeded / failed / cancelled.

Display fields:

- status pill
- `submitted_at`
- `started_at`
- `finished_at`
- `last_stage_seen`
- `error_message` summary when failed

### 2. Polling loop

- while status is `queued` or `running`: poll every **2 seconds**
- once terminal (`succeeded` / `failed` / `cancelled`): stop polling
- if polling request itself fails: show non-blocking inline error,
  allow manual retry

No exponential backoff. No aggressive retry logic.

### 3. Submit / cancel / fetch-result controls

- "Run in background" uses PR7 async submit path
- while active: disable duplicate submit, show Cancel when cancellable
- on success: show "View result"
- on failure: show failure summary + "Run again"
- on cancelled: show cancelled state + "Run again"

### 4. Single active job per scenario-page session

Care only about the **most recent / current job for the page session**.
New submit replaces the card state. Old jobs are not listed in PR8.

### 5. Client-side state model

Minimal client state: `jobId`, `status`, `submittedAt`, `startedAt`,
`finishedAt`, `lastStageSeen`, `errorMessage`, `resultReady`,
`pollingTimerId`. No heavy framework.

### 6. Thin API adapter

A small JS adapter for PR7 endpoints: submit / fetch status / fetch
result / cancel. No duplicate business logic in templates.

---

## Out of scope

Explicitly deferred:

- multi-job list on the scenario page
- global "my jobs" page
- websocket / SSE live push
- optimistic progress percentages
- stage-by-stage progress bars
- background result diff viewer
- resume / retry failed job
- stale-job reconciler
- cross-tab synchronisation
- access control redesign
- polling from pages other than the scenario page

---

## Flag plan

`TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED`

- default `False` through commits 2–7
- flipped to `True` in commit 8

Interaction with PR7:

- if PR7 async planner is disabled, PR8 UI hides or degrades gracefully
- PR8 must not expose dead controls when backend async is off
- existing sync planner path remains intact

---

## Acceptance bars

### 1. Flag-off parity

With `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=false`:

- scenario page behaviour matches current master
- no async job card rendered
- existing sync controls unchanged
- no extra polling requests

### 2. Happy-path UX

Submit creates job; status card moves queued → running → succeeded;
polling stops at terminal; "View result" available; no duplicate
submit while active.

### 3. Cancel UX

Cancel request updates UI through cancelling/cancelled; rerun control
available after cancellation.

### 4. Failure UX

Failed pill visible; compact error summary shown; rerun control
available; polling stops.

### 5. Polling discipline

- active cadence = 2s
- no polling once terminal
- no more than one in-flight poll at a time per page

### 6. Backend neutrality

PR8 must not change:

- planner results
- planner payload semantics
- job state machine semantics from PR7

### 7. Overhead / noise ceiling

- only active pages poll
- terminal pages stop polling
- no background polling on pages without an active job

---

## Fixture / test surface

At minimum, PR8 must ship fixtures or mocked responses for:

1. **happy path** — queued → running → succeeded
2. **cancelled path** — queued / running → cancelled
3. **failed path** — queued / running → failed

Optional additions: flag-off path, PR7-backend-disabled path.

---

## Commit plan

| # | Scope |
|---|-------|
| 0 | `docs/PR8-DOR.md` |
| 1 | failing UI tests + fixture / mocked-response set, tripwires |
| 2 | thin JS API adapter for PR7 endpoints (no UI wiring) |
| 3 | status card markup + status-pill rendering (no polling) |
| 4 | polling loop (2s active, stop on terminal), one in-flight poll guard |
| 5 | submit / cancel / result controls, duplicate-submit suppression |
| 6 | failure / cancelled terminal UX polish; backend-disabled graceful hide |
| 7 | acceptance pack + parity checks + helper utilities |
| 8 | flip `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED` default True + `docs/PR8-PROMOTION-NOTE.md` |

---

## Test plan

### New test files

- `tests/test_pr8_async_job_ui.py`
- `tests/test_pr8_acceptance_pack.py`

### Core assertions

1. flag-off renders no async job card
2. backend-disabled hides async controls cleanly
3. happy-path status progression renders correctly
4. cancelled-path status progression renders correctly
5. failed-path status progression renders correctly
6. polling stops at terminal states
7. duplicate submit blocked while active
8. view-result button shown only when succeeded
9. cancel button shown only while cancellable
10. no extra polling on pages without active job

---

## Implementation cautions

1. **Single active poller** — no overlapping intervals / duplicate fetches.
2. **Terminal-state stop** — stop immediately once terminal state is seen.
3. **Graceful degradation** — if PR7 backend disabled, hide async UI
   rather than showing broken controls.
4. **No state explosion** — state local to scenario page; no global event bus.
5. **Accessibility** — status pill text readable without colour alone.

---

## Rollback

Three tiers, consistent with PR3–PR7:

1. **Env / UI kill-switch** — `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=false`
2. **Revert commit 8** — if only the default-on promotion is problematic
3. **Revert PR8 merge** — if UI logic itself is causing regressions

PR7 backend remains intact regardless.

---

## Branch

`refactor/pr8-async-job-ui` (base: master @ `dcea2e0` = PR7 tip).

---

## Closeout

To be filled at commit 8 with:

- final 8-commit summary
- promotion commit hash
- acceptance snapshot (test counts)
- link to `docs/PR8-PROMOTION-NOTE.md`
