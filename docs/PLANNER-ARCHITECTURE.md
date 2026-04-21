# Planner Architecture — one-sheet map

A live snapshot of the planner stack after PR1–PR9 (planner-refactor
track, 2026-04-20/21). Under 200 lines by design — this is a map,
not a reference manual.

## Entry points

| Entry | Path | Purpose |
|-------|------|---------|
| Board placement | `core.services.timetable_autoplace.auto_place_board(...)` | Place all sections on a single `DeliveryBoard` |
| Scenario placement | `core.services.timetable_autoplace.auto_place_scenario(...)` | Fan out across every `DeliveryBoard` in a `TimetableScenario`, merge per-board results |
| V2 pipeline | `core.services.timetable_optimizer_v2.run_v2_pipeline(...)` | 5-stage pipeline: generate → rank → local search → chain search → CP-SAT polish |
| Async runner | `core.services.planner_job_runner.run_planner_job(job_id)` | In-process synchronous wrapper that records timing + last_stage_seen on a `PlannerJob` row |
| Async dispatcher | `core.services.planner_job_runner.dispatch_planner_job(job_id)` | Submits `_worker(job_id)` to a single-worker `ThreadPoolExecutor`; tests override to sync (see [TEST-INFRA-QUIRKS.md](TEST-INFRA-QUIRKS.md)) |
| REST submit | `POST /planner-jobs/` | Creates `PlannerJob`, dispatches, returns 201 `{job_id, status: "queued"}` |
| REST poll | `GET /planner-jobs/<id>/` | Status + metadata; `result_json` not echoed |
| REST result | `GET /planner-jobs/<id>/result/` | Full payload; 404 until `succeeded` |
| REST cancel | `POST /planner-jobs/<id>/cancel/` | Sets `cancel_requested=True` (cooperative, not pre-emptive) |
| Workspace page | `timetable_workspace_page` → `core/templates/core/timetable_workspace.html` | Renders PR8 status card when `is_async_job_ui_effective()` is True |

## Stage vocabulary

The five stages of the V2 pipeline — frozen, canonical source at
`core.services.timetable_stage_telemetry.STAGE_KEYS`:

1. `greedy` — initial board/scenario placement path
2. `sa` — SA / local-search pass
3. `cpsat` — CP-SAT polish call
4. `chain` — chain-search pass
5. `rooming_repair` — room assignment repair / recovery path

Every module that needs this tuple imports from `STAGE_KEYS`. Never
re-declare inline.

## Payload schema (schema-stable, every key always present)

```
{
    "boards": {
        "<board_label>": {
            "placements": [...],
            "final_score": <int>,
            "decision_trace": {...},
            "stage_telemetry": {"stage_ms": {5}, "stage_iterations": {5}},
            "perturbation_metric": {unchanged, changes_from_baseline, newly_placed, removed, changes_by_stage{5}}
        },
        ...
    },
    "total_placed": <int>,
    "total_skipped": <int>,
    "decision_trace": {...},
    "perturbation_metric": {...},
    "stage_telemetry": {...}
}
```

Provenance-only fields inside each `DecisionTrace`:

- `stage_origin` — last stage that touched this placement
- `stage_context` — code-specific detail bag

These are **read-only** for planner decisions (PR9 c4 test locks
this in).

## Flags — single source: `core.services.timetable_flags`

| Flag | Intro | Default (prod) | What turning off does |
|------|-------|----------------|-----------------------|
| `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED` | PR6 | `True` | `stage_telemetry` block zeroed; schema preserved |
| `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED` | PR7 | `True` | All `/planner-jobs/*` endpoints 404; no rows created |
| `TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED` | PR8 | `True` | PR8 card hides; no polling; sync controls unchanged |
| `TIMETABLE_PR5_STAGE_TRACE_ENABLED` | PR5 | `True` | `stage_origin` / `stage_context` stripped from trace |
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | PR4 | `True` | Instructor-clash emission skipped |
| `TIMETABLE_LAB_HEURISTIC_UNIFIED` | PR4 | `True` | Reverts to scattered heuristic call sites |
| `TIMETABLE_PR3_WARM_START_ENABLED` | PR3 | `True` | Cold-start (no baseline reuse) |

Every flag is env-overridable — set e.g. `TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false`
in the Render dashboard and restart to revert at runtime.

## Rollback hierarchy

Each PR has a promotion note with a three-tier rollback (env switch
→ revert c8 → revert merge). The notes live in `docs/`:

- [PR3-PROMOTION-NOTE.md](PR3-PROMOTION-NOTE.md) — warm start
- [PR4-PROMOTION-NOTE.md](PR4-PROMOTION-NOTE.md) — instructor clash + lab heuristic unification
- [PR5-PROMOTION-NOTE.md](PR5-PROMOTION-NOTE.md) — stage trace
- [PR6-PROMOTION-NOTE.md](PR6-PROMOTION-NOTE.md) — stage telemetry
- [PR7-PROMOTION-NOTE.md](PR7-PROMOTION-NOTE.md) — async planner
- [PR8-PROMOTION-NOTE.md](PR8-PROMOTION-NOTE.md) — async job UX

## Parity helpers (flag-off byte equality)

Each PR ships a `strip_*_for_parity` helper so consumers can compare
against pre-PR snapshots:

- `core.services.timetable_pr5_parity.strip_pr5_fields_for_parity`
- `core.services.timetable_pr6_parity.strip_pr6_fields_for_parity`
- `core.services.timetable_pr7_parity.strip_pr7_fields_for_parity`
- `core.services.pr8_parity.strip_pr8_ui_context`

Used by: acceptance packs, shadow validation, regression tests.

## Acceptance packs (CLI)

| Command | Purpose |
|---------|---------|
| `python manage.py pr3_acceptance_report` | Warm-start feasibility + wallclock bars over 21 tests |
| `python manage.py pr5_acceptance_report` | Stage-trace tallies over four fixtures |
| `python manage.py pr6_telemetry_report` | Stage telemetry totals + flag state |
| `python manage.py pr7_job_report --limit 20` | Recent PlannerJob rows |

## Cross-cutting conventions

- **Single source** for the stage tuple → `STAGE_KEYS`
- **Single source** for flag helpers → `timetable_flags`
- **Provenance-only** → PR5 stage_origin / stage_context (lint test in PR9 c4)
- **Sync-dispatch test fallback** → `PYTEST_CURRENT_TEST` env (see [TEST-INFRA-QUIRKS.md](TEST-INFRA-QUIRKS.md))
- **Commit flow** → `SKIP=mypy,bandit git commit`

## What's NOT in the planner stack

- No Celery / Redis / RQ
- No WebSocket / SSE
- No multi-worker pool
- No durable job queue across process restarts
- No cross-process cancellation
- No multi-job dashboard

These are explicit scope-floor items from PR7 and PR8 DoRs, preserved
intentionally.

## Next steps (if picking back up)

Planner stack is currently stable. Debt has been consolidated
(PR9). Candidate future themes:

- PR10 — multi-job history / audit dashboard
- Registrar-facing run-compare UI (before/after side-by-side)
- Stage-by-stage progress events (SSE, if ever wanted)
- Broader mypy pass beyond the planner files

No open blockers as of PR9 merge.
