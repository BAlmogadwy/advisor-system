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
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from threading import Lock
from typing import Any

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import PlannerJob
from core.services.job_runtime import HOLDER_PLANNER, SolverBusy, shutting_down, solver_slot
from core.services.timetable_flags import is_one_teacher_active
from core.services.timetable_run_seed import parse_run_seed, resolve_run_seed, seeded_timetable_run

logger = logging.getLogger(__name__)

# A planner job stuck in RUNNING/QUEUED past this window almost certainly belongs
# to a server process that died mid-run (the in-process worker can't update its
# own row once the process is gone). Generous default — the slowest observed real
# build is ~12-15 min — so a legitimately long job is never swept by mistake.
PLANNER_JOB_STALE_SETTING = "TIMETABLE_PLANNER_JOB_STALE_MINUTES"
PLANNER_JOB_STALE_DEFAULT_MIN = 45

# The planner is process-local. A live solver may exceed the orphan timeout;
# polling must not relinquish its ownership while its worker can still persist.
# Reference counts also protect against duplicate direct run invocations.
_EXECUTING_JOBS: Counter[str] = Counter()
_EXECUTING_JOBS_LOCK = Lock()


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
    with _EXECUTING_JOBS_LOCK:
        executing_ids = tuple(_EXECUTING_JOBS)
    stale = stale.exclude(pk__in=executing_ids)
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
        # Its CP-SAT solves take turns with the exam jobs' and the exam Check's:
        # two at once on this 0.5-CPU, 512 MB host split the CPU and can OOM it.
        # A job still waiting when the process starts to exit must not start: a
        # full rebuild clears the board first and would be killed half-way.
        with solver_slot(holder=HOLDER_PLANNER, give_up=shutting_down):
            if shutting_down():
                raise SolverBusy
            run_planner_job(job_id)
    except SolverBusy:
        _fail_unstarted(job_id)
    finally:
        close_old_connections()


def _fail_unstarted(job_id: str) -> None:
    PlannerJob.objects.filter(
        id=job_id, status__in=[PlannerJob.STATUS_QUEUED, PlannerJob.STATUS_RUNNING]
    ).update(
        status=PlannerJob.STATUS_FAILED,
        error_message="the server restarted before the job could start; nothing was changed",
        finished_at=timezone.now(),
    )


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
    # Validate before even orphan reconciliation, which can itself write rows.
    supplied_seed = parse_run_seed((params or {}).get("seed"))
    # Self-heal: sweep any jobs stranded RUNNING by a prior server death before
    # queueing a new one, so the table never accumulates ghost "running" rows.
    reconcile_stale_planner_jobs()
    from core.models import TimetableScenario
    from core.services.timetable_mutation_guard import (
        ScenarioPlannerBusy,
        ScenarioPublished,
        conflicting_planner_job,
        scenario_resource_ids,
        scenario_write_scope,
    )

    with scenario_write_scope(scenario_id) as (scenarios, owned_scenario_ids):
        scenario = next((row for row in scenarios if row.pk == scenario_id), None)
        if scenario is None:
            raise TimetableScenario.DoesNotExist
        if scenario.status == "published":
            raise ScenarioPublished("Cannot modify published scenario")
        owned_section_ids = scenario_resource_ids(scenario_id)[1]
        active = conflicting_planner_job(owned_scenario_ids, owned_section_ids)
        if active is not None:
            raise ScenarioPlannerBusy(active)
        job = PlannerJob.objects.create(
            id=uuid.uuid4(),
            scenario_id=scenario_id,
            board_id=board_id,
            mode=mode,
            status=PlannerJob.STATUS_QUEUED,
            submitted_by=user if getattr(user, "is_authenticated", False) else None,
            request_signature=_compute_request_signature(scenario_id, mode),
            params={
                **(params or {}),
                "seed": resolve_run_seed(supplied_seed),
                "owned_scenario_ids": sorted(owned_scenario_ids),
                "owned_term_section_ids": sorted(owned_section_ids),
            },
        )
    return job.id


def get_planner_job(job_id: uuid.UUID | str) -> PlannerJob | None:
    return PlannerJob.objects.filter(id=job_id).first()


def serialize_planner_job(job: PlannerJob) -> dict[str, Any]:
    """Read-only metadata shared by job polling and scenario reload discovery."""
    return {
        "job_id": str(job.id),
        "scenario_id": job.scenario_id,
        "status": job.status,
        "mode": job.mode,
        "seed": (job.params or {}).get("seed"),
        "last_stage_seen": job.last_stage_seen,
        "submitted_at": job.submitted_at.isoformat() if job.submitted_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "cancel_requested": job.cancel_requested,
        "error_message": job.error_message,
    }


def run_planner_job_synchronously(
    *, scenario_id: int, mode: str, user: Any | None = None, params: dict | None = None
) -> dict[str, Any]:
    """Execute with a durable lease without requiring async polling endpoints."""
    job_id = submit_planner_job(scenario_id=scenario_id, mode=mode, user=user, params=params)
    run_planner_job(job_id)
    job = get_planner_job(job_id)
    if job is not None and isinstance(job.result_json, dict):
        return job.result_json
    return {
        "error": f"Optimiser error: {job.error_message if job else 'planner result missing'}",
        "seed": (job.params or {}).get("seed") if job else None,
    }


def cancel_planner_job(
    job_id: uuid.UUID | str,
    *,
    user: Any | None = None,
) -> bool:
    """Cooperatively request cancellation of a planner job.

    Sets ``cancel_requested=True``. The runner checks before starting, at
    scheduler progress boundaries and after a bounded pipeline finishes. It
    restores the pre-job schedule before transitioning to ``cancelled``.
    Returns ``True`` when the flag was set,
    ``False`` when the job does not exist or is already terminal.
    """
    # One conditional write competes with the terminal decision's row lock.
    # A caller cannot acknowledge a cancel after success has already committed.
    return bool(
        PlannerJob.objects.filter(
            id=job_id,
            status__in=(PlannerJob.STATUS_QUEUED, PlannerJob.STATUS_RUNNING),
        ).update(cancel_requested=True)
    )


def _clear_scenario_placements(scenario_id: int) -> int:
    """Reset a scenario's board(s) for a ``mode=full_rebuild`` run.

    Delegates to ``timetable_board_persistence.reset_scenario`` so the rebuild
    starts from the same clean slate the split-workspace rebuild uses: unlocked
    placements **and their meeting rows** are cleared, while locked placements
    (the user's hard constraints) survive. Previously this deleted every
    placement — including locked ones — and left ``TermSectionMeeting`` rows
    stranded, so the adaptive rebuild inherited stale meetings and silently
    dropped the user's locks. Returns the number of placements removed.
    """
    from core.services.timetable_board_persistence import reset_scenario

    return reset_scenario(scenario_id, keep_locked=True).placements_deleted


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


class _PlannerCancelled(Exception):
    """Cooperative boundary signal; finalisation owns the snapshot restore."""


def _check_planner_cancel(job_id: uuid.UUID | str) -> None:
    # A lost claim must stop future phases too; finalisation deliberately leaves
    # that terminal job and any subsequent manual edits untouched.
    if not PlannerJob.objects.filter(
        pk=job_id, status=PlannerJob.STATUS_RUNNING, cancel_requested=False
    ).exists():
        raise _PlannerCancelled


def run_planner_job(job_id: uuid.UUID | str) -> None:
    """Execute a queued ``PlannerJob`` synchronously in-process.

    Cancellation is cooperative: bounded solver calls finish, then all of this
    job's persisted changes are discarded before its terminal state is saved.
    """
    key = str(job_id)
    with _EXECUTING_JOBS_LOCK:
        _EXECUTING_JOBS[key] += 1
    try:
        job = get_planner_job(job_id)
        # Never draw here: retries and legacy persisted jobs must not acquire a
        # different starting point merely by entering this worker again.
        with seeded_timetable_run((job.params or {}).get("seed") if job else None):
            _run_planner_job(job_id)
    finally:
        with _EXECUTING_JOBS_LOCK:
            _EXECUTING_JOBS[key] -= 1
            if not _EXECUTING_JOBS[key]:
                del _EXECUTING_JOBS[key]


def _run_planner_job(job_id: uuid.UUID | str) -> None:
    from core.services.planner_job_snapshot import restore_planner_job, snapshot_planner_job
    from core.services.timetable_autoplace import auto_place_scenario

    with transaction.atomic():
        job = PlannerJob.objects.select_for_update().filter(id=job_id).first()
        if job is None or job.status != PlannerJob.STATUS_QUEUED:
            return
        job.started_at = timezone.now()
        if job.cancel_requested:
            job.status = PlannerJob.STATUS_CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "started_at", "finished_at"])
            return
        job.status = PlannerJob.STATUS_RUNNING
        job.save(update_fields=["status", "started_at"])

    baseline = None
    result = None
    failure = None
    try:
        # Capture before the legacy full rebuild clears anything, and before a
        # scheduler replaces sections or adds grid columns.
        baseline = snapshot_planner_job(job.scenario_id)
        _check_planner_cancel(job.id)
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
                checkpoint=lambda: _check_planner_cancel(job.id),
            )
        elif job.mode == PlannerJob.MODE_SCHEDULER_BUILD:
            # The alternative engine. Imported inside the branch so the original
            # planner never carries the dependency: if the subsystem is broken or
            # absent, every existing mode above still runs untouched, and this
            # one fails as a failed job rather than as an import error.
            from scheduler.bridge import build_into_scenario

            p = job.params or {}

            def _stage(name: str) -> None:
                _check_planner_cancel(job.id)
                job.last_stage_seen = name
                job.save(update_fields=["last_stage_seen"])

            result = build_into_scenario(
                job.scenario_id,
                academic_year=str(p.get("academic_year", "")),
                term=int(p.get("term", 1)),
                programs=list(p.get("programs") or []),
                gender=str(p.get("gender", "M")),
                seconds=float(p.get("seconds", 120)),
                runs=int(p.get("runs", 3)),
                clash_tolerance=float(p.get("clash_tolerance", 0.20)),
                default_capacity=int(p.get("default_capacity", 25)),
                min_demand=int(p.get("min_demand", 5)),
                confirmation_seconds=float(p.get("confirmation_seconds", 20)),
                seed=p.get("seed"),
                progress=_stage,
            )
        elif job.mode == PlannerJob.MODE_FULL_REBUILD:
            _clear_scenario_placements(job.scenario_id)
            if is_one_teacher_active():
                from core.services.section_teacher_assignment import run_placer

                result = run_placer(
                    job.scenario_id,
                    lambda: auto_place_scenario(job.scenario_id, strategy="adaptive"),
                    rebuild=True,
                )
            else:
                result = auto_place_scenario(job.scenario_id, strategy="adaptive")
        elif is_one_teacher_active():
            # Places only what is missing and keeps the board, so the teachers
            # already there stay; only new sections get one.
            from core.services.section_teacher_assignment import run_placer

            result = run_placer(
                job.scenario_id, lambda: auto_place_scenario(job.scenario_id), rebuild=False
            )
        else:
            result = auto_place_scenario(job.scenario_id)
    except _PlannerCancelled:
        pass
    except Exception as exc:
        summary = f"{type(exc).__name__}: {exc}"
        tb_tail = "\n".join(traceback.format_exc().splitlines()[-6:])
        failure = f"{summary}\n...\n{tb_tail}"[:4000]

    # The V2 guarded runner catches optimiser failures internally and returns
    # ``{"error": ...}`` after rolling back, rather than raising — so a non-empty
    # ``error`` here is a real failure and must NOT be reported as succeeded (the
    # sync view maps the same dict to HTTP 500). Persist the result either way so
    # the payload/telemetry is inspectable.
    try:
        with transaction.atomic():
            # Read the authoritative flag while holding the row lock through
            # both restoration and the terminal write. Never use the stale job
            # instance that was loaded before the solver ran.
            terminal = PlannerJob.objects.select_for_update().get(pk=job.id)
            if terminal.status != PlannerJob.STATUS_RUNNING:
                logger.warning("Planner %s lost running ownership; skipping finalisation", job.id)
                return
            if terminal.cancel_requested:
                if baseline is not None:
                    restore_planner_job(baseline)
                terminal.status = PlannerJob.STATUS_CANCELLED
                terminal.result_json = None
                terminal.error_message = None
            else:
                terminal.result_json = (
                    {**result, "seed": (terminal.params or {}).get("seed")}
                    if isinstance(result, dict)
                    else result
                )
                error = result.get("error") if isinstance(result, dict) else None
                if failure or error:
                    terminal.status = PlannerJob.STATUS_FAILED
                    terminal.error_message = failure or str(error)[:4000]
                else:
                    terminal.status = PlannerJob.STATUS_SUCCEEDED
                    terminal.last_stage_seen = _derive_last_stage(result)
            terminal.finished_at = timezone.now()
            terminal.save(
                update_fields=[
                    "status",
                    "result_json",
                    "error_message",
                    "last_stage_seen",
                    "finished_at",
                ]
            )
    except Exception as exc:
        # A failed restore rolls back atomically. It must never be called a
        # successful cancellation while the modified timetable remains saved.
        PlannerJob.objects.filter(pk=job.id, status=PlannerJob.STATUS_RUNNING).update(
            status=PlannerJob.STATUS_FAILED,
            result_json=None,
            finished_at=timezone.now(),
            error_message=f"Cancellation/finalisation failed: {type(exc).__name__}: {exc}"[:4000],
        )


__all__ = [
    "ASYNC_PLANNER_ENABLED_SETTING",
    "cancel_planner_job",
    "dispatch_planner_job",
    "get_planner_job",
    "is_async_planner_enabled",
    "reconcile_stale_planner_jobs",
    "run_planner_job",
    "run_planner_job_synchronously",
    "submit_planner_job",
]
