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
import os
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from core.models import PlannerJob

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

_STAGE_ORDER = ("greedy", "sa", "cpsat", "chain", "rooming_repair")


def is_async_planner_enabled() -> bool:
    """Return whether the PR7 async planner is active.

    Reads ``settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED``. Default
    ``False`` until commit 8 (promotion). Env override
    ``TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false`` is the live
    kill-switch once promoted.
    """
    return bool(getattr(settings, ASYNC_PLANNER_ENABLED_SETTING, False))


def _compute_request_signature(scenario_id: int, mode: str) -> str:
    raw = f"{scenario_id}|{mode}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def submit_planner_job(
    *,
    scenario_id: int,
    mode: str,
    user: Any | None = None,
    board_id: int | None = None,
) -> uuid.UUID:
    """Create a ``PlannerJob`` row in ``queued`` and return its id."""
    job = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario_id=scenario_id,
        board_id=board_id,
        mode=mode,
        status=PlannerJob.STATUS_QUEUED,
        submitted_by=user if getattr(user, "is_authenticated", False) else None,
        request_signature=_compute_request_signature(scenario_id, mode),
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
    "run_planner_job",
    "submit_planner_job",
]
