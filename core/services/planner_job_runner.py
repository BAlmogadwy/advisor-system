"""PR7 commit 3 — planner job runner execution path.

Thin orchestration layer around ``PlannerJob`` rows. Commits 2–3 land
the submit / get / run helpers; cooperative cancellation and API views
arrive in commits 4–5.

This is an **async UX shim**, not a distributed job system. See
``docs/PR7-DOR.md`` §"What PR7 is NOT" for the scope floor
(process-local, not durable across restarts, cooperative cancel only).
"""

from __future__ import annotations

import hashlib
import logging
import os
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from core.models import PlannerJob

logger = logging.getLogger(__name__)

# A planner job stuck in RUNNING/QUEUED past this window almost certainly belongs
# to a server process that died mid-run (the in-process worker can't update its
# own row once the process is gone). Generous default — the slowest observed real
# build is ~12-15 min — so a legitimately long job is never swept by mistake.
PLANNER_JOB_STALE_SETTING = "TIMETABLE_PLANNER_JOB_STALE_MINUTES"
PLANNER_JOB_STALE_DEFAULT_MIN = 45


def reconcile_stale_planner_jobs(stale_minutes: int | None = None) -> int:
    """Mark orphaned planner jobs (RUNNING past the stale window, or QUEUED but
    never dispatched) as FAILED. PR7 jobs are process-local and not durable
    across restarts, so a server stop/reap leaves rows stranded in RUNNING
    forever — a polling UI would spin indefinitely. Idempotent; returns the count
    reconciled."""
    if stale_minutes is None:
        stale_minutes = int(
            getattr(settings, PLANNER_JOB_STALE_SETTING, PLANNER_JOB_STALE_DEFAULT_MIN)
        )
    cutoff = timezone.now() - timedelta(minutes=max(1, stale_minutes))
    stale = PlannerJob.objects.filter(
        status__in=[PlannerJob.STATUS_RUNNING, PlannerJob.STATUS_QUEUED],
    ).filter(Q(started_at__lt=cutoff) | Q(started_at__isnull=True, submitted_at__lt=cutoff))
    updated = stale.update(
        status=PlannerJob.STATUS_FAILED,
        error_message="reconciled: the server stopped before the job finished (stale job swept)",
        finished_at=timezone.now(),
    )
    if updated:
        logger.warning("Reconciled %d stale planner job(s) -> failed", updated)
    return updated


_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pr7-planner")
    return _EXECUTOR


def _worker(job_id: str) -> None:
    close_old_connections()
    try:
        run_planner_job(job_id)
    finally:
        close_old_connections()


def _dispatch_sync() -> bool:
    """True when running under pytest (in-memory SQLite can't be shared
    with a background thread) or when an explicit override asks for it."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(getattr(settings, "TIMETABLE_PR7_DISPATCH_SYNC", False))


def dispatch_planner_job(job_id: uuid.UUID | str) -> Future:
    if _dispatch_sync():
        fut: Future = Future()
        try:
            run_planner_job(job_id)
            fut.set_result(None)
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut
    return _get_executor().submit(_worker, str(job_id))


ASYNC_PLANNER_ENABLED_SETTING = "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED"

from core.services.timetable_stage_telemetry import STAGE_KEYS as _STAGE_ORDER  # noqa: E402


def is_async_planner_enabled() -> bool:  # noqa: D401 — back-compat re-export
    """Back-compat re-export. Canonical home: ``core.services.timetable_flags``."""
    from core.services.timetable_flags import is_async_planner_enabled as _impl

    return _impl()


def _compute_request_signature(scenario_id: int, mode: str) -> str:
    raw = f"{scenario_id}|{mode}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def submit_planner_job(
    *,
    scenario_id: int,
    mode: str,
    user: Any | None = None,
    board_id: int | None = None,
    params: dict | None = None,
) -> uuid.UUID:
    """Create a ``PlannerJob`` row in ``queued`` and return its id.

    ``params`` carries per-request optimiser tuning (strategies, CP-SAT budget,
    iteration caps) so an async V2 job replays the same configuration the
    synchronous path would have used.
    """
    # Self-heal: sweep any jobs stranded RUNNING by a prior server death before
    # queueing a new one, so the table never accumulates ghost "running" rows.
    reconcile_stale_planner_jobs()
    job = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario_id=scenario_id,
        board_id=board_id,
        mode=mode,
        status=PlannerJob.STATUS_QUEUED,
        submitted_by=user if getattr(user, "is_authenticated", False) else None,
        request_signature=_compute_request_signature(scenario_id, mode),
        params=params or {},
    )
    return job.id


def get_planner_job(job_id: uuid.UUID | str) -> PlannerJob | None:
    return PlannerJob.objects.filter(id=job_id).first()


def cancel_planner_job(
    job_id: uuid.UUID | str,
    *,
    user: Any | None = None,
) -> bool:
    """Cooperatively request cancellation of a planner job.

    Sets ``cancel_requested=True``. The runner checks the flag at each
    stage boundary (and before starting) and transitions to
    ``cancelled`` if seen. Returns ``True`` when the flag was set,
    ``False`` when the job does not exist or is already terminal.
    """
    job = PlannerJob.objects.filter(id=job_id).first()
    if job is None:
        return False
    if job.status in {
        PlannerJob.STATUS_SUCCEEDED,
        PlannerJob.STATUS_FAILED,
        PlannerJob.STATUS_CANCELLED,
    }:
        return False
    job.cancel_requested = True
    job.save(update_fields=["cancel_requested"])
    return True


def _clear_scenario_placements(scenario_id: int) -> int:
    """Delete every ``SectionPlacement`` attached to any board of the
    given scenario. Returns the number of rows removed.

    Used when ``mode=full_rebuild`` — PR7 commits 2-5 ignored mode
    entirely and always ran fill-unplaced. Calling this before
    ``auto_place_scenario`` restores the expected "rebuild from
    scratch" semantic.
    """
    from core.models import SectionPlacement

    deleted, _ = SectionPlacement.objects.filter(
        board__scenario_id=scenario_id,
    ).delete()
    return deleted


def _derive_last_stage(result: dict | None) -> str | None:
    if not isinstance(result, dict):
        return None
    telemetry = result.get("stage_telemetry") or {}
    stage_ms = telemetry.get("stage_ms") or {}
    last = None
    for stage in _STAGE_ORDER:
        if stage_ms.get(stage, 0) > 0:
            last = stage
    return last or "greedy"


def run_planner_job(job_id: uuid.UUID | str) -> None:
    """Execute a queued ``PlannerJob`` synchronously in-process.

    Commit 3 — coarse happy/failure path. Cooperative cancellation at
    stage boundaries lands in commit 4; commit 5 wires the ThreadPool
    dispatcher + REST views.
    """
    from core.services.timetable_autoplace import auto_place_scenario

    job = PlannerJob.objects.filter(id=job_id).first()
    if job is None:
        return

    if job.status != PlannerJob.STATUS_QUEUED:
        return

    if job.cancel_requested and job.status == PlannerJob.STATUS_QUEUED:
        job.status = PlannerJob.STATUS_CANCELLED
        job.started_at = timezone.now()
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "started_at", "finished_at"])
        return

    job.status = PlannerJob.STATUS_RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    try:
        if job.mode in (
            PlannerJob.MODE_OPTIMISE_V2_FULL,
            PlannerJob.MODE_OPTIMISE_V2_CURRENT,
        ):
            # WS-E — run the V2 optimiser off the request thread, behind the
            # shared snapshot → regression → rollback safety gate. This is the
            # path that fixes the audit's P1 (a sync V2 run exceeding gunicorn's
            # 120s timeout was SIGKILLed before the view-level rollback fired).
            from core.services.timetable_v2_runner import run_v2_optimisation_guarded

            v2_mode = "full" if job.mode == PlannerJob.MODE_OPTIMISE_V2_FULL else "current"
            # Replay the per-request tuning the synchronous view would have used,
            # so async 'full' keeps its strategy sweep + CP-SAT budget (same
            # kwargs/coercions as tw_optimise_v2_view's sync call).
            p = job.params or {}
            result = run_v2_optimisation_guarded(
                job.scenario_id,
                mode=v2_mode,
                max_iterations=int(p.get("max_iterations", 50)),
                run_chain=bool(p.get("run_chain_search", True)),
                run_cpsat=bool(p.get("run_cpsat_polish", True)),
                cpsat_limit=float(p.get("cpsat_time_limit", 60)),
                strategies=p.get("strategies") or None,
                max_chain_iterations=int(p.get("max_chain_iterations", 10)),
            )
        elif job.mode == PlannerJob.MODE_FULL_REBUILD:
            _clear_scenario_placements(job.scenario_id)
            result = auto_place_scenario(job.scenario_id, strategy="adaptive")
        else:
            result = auto_place_scenario(job.scenario_id)
    except Exception as exc:
        job.status = PlannerJob.STATUS_FAILED
        summary = f"{type(exc).__name__}: {exc}"
        tb_tail = "\n".join(traceback.format_exc().splitlines()[-6:])
        job.error_message = f"{summary}\n...\n{tb_tail}"[:4000]
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at"])
        return

    job.status = PlannerJob.STATUS_SUCCEEDED
    job.result_json = result
    job.last_stage_seen = _derive_last_stage(result)
    job.finished_at = timezone.now()
    job.save(
        update_fields=["status", "result_json", "last_stage_seen", "finished_at"],
    )


__all__ = [
    "ASYNC_PLANNER_ENABLED_SETTING",
    "cancel_planner_job",
    "dispatch_planner_job",
    "get_planner_job",
    "is_async_planner_enabled",
    "reconcile_stale_planner_jobs",
    "run_planner_job",
    "submit_planner_job",
]
