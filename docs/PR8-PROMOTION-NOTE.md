# PR8 Promotion Note — async job UX

_Status:_ PR8 commit 8 flips `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED`
default to `True`. The async-job status card now renders on the
Timetable Workspace page and drives the PR7 endpoint surface.

## Scope

PR8 is a **UI shim** around PR7's async execution substrate. It does
not change planner semantics; it only surfaces job lifecycle.

### What PR8 IS

- Compact status card on the workspace page: status pill, timing
  fields, `last_stage_seen`, error summary on failure.
- Four controls: submit (disabled while active), cancel (visible only
  when active), view-result (visible when succeeded), rerun (visible
  when failed or cancelled).
- 2-second polling while `queued`/`running`; stops on terminal.
- Thin `fetch`-based JS adapter at `core/static/core/js/pr8_async_job_adapter.js`.
- Graceful hide when PR7 backend flag is off.

### What PR8 IS NOT (scope floor)

- No SSE / websocket push.
- No multi-job history browser.
- No progress percentages, no stage-by-stage progress bars.
- No retries / auto-restart.
- No cross-tab synchronisation.
- No notifications / toasts beyond the inline card state.

## Rollback (three tiers)

1. **Env kill-switch (runtime, no redeploy).** Set
   `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=false` in the Render env and
   restart. The card stops rendering; pre-PR8 byte parity restored on
   the workspace page. PR7 backend endpoints unaffected.
2. **Revert commit 8.** `git revert <c8-sha>` flips the settings
   default back to `False`. The partial + adapter remain dormant.
3. **Revert the PR8 merge.** `git revert -m 1 <merge-sha>` removes
   every PR8 touch site. No data migration required — PR8 added no
   schema.

## Caveats

- **Single-job scope per page session.** Submitting a new run replaces
  the visible card state. No history browser.
- **Polling respects backend availability.** If PR7 is disabled
  mid-session, the card hides; the user can reload to see the sync
  path.
- **CSRF token is fetched via `django.middleware.csrf.get_token`** at
  view-render time and embedded in `pr8_config_json`. Downgrading to
  flag-off zeroes the token in the serialised config.

## Acceptance coverage

- `tests/test_pr8_async_job_ui.py` — 14 classes, ~25 tests:
  tripwires, adapter shim, card rendering, status-pill classes,
  polling cadence + terminal-state helper, controls, duplicate-submit
  block, failed/cancelled states, backend-disabled graceful hide,
  three-path acceptance pack, parity helper, post-promotion default.
- Test flag-off path (`TestBackendDisabledHides`) verifies that either
  PR7-off or PR8-off collapses `is_async_job_ui_effective` to `False`.
