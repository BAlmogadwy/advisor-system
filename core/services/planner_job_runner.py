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
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Lock
from typing import Any

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import PlannerJob
from core.services.job_runtime import HOLDER_PLANNER, SolverBusy, shutting_down, solver_slot
from core.services.timetable_flags import is_one_teacher_active
from core.services.timetable_run_seed import parse_run_seed, resolve_run_seed, seeded_timetable_run

logger = logging.getLogger(__name__)

# Grace period for undispatched jobs and backends without a worker lease.
PLANNER_JOB_STALE_SETTING = "TIMETABLE_PLANNER_JOB_STALE_MINUTES"
PLANNER_JOB_STALE_DEFAULT_MIN = 45

# The planner is process-local. A live solver may exceed the orphan timeout;
# polling must not relinquish its ownership while its worker can still persist.
# Reference counts also protect against duplicate direct run invocations.
_EXECUTING_JOBS: Counter[str] = Counter()
_EXECUTING_JOBS_LOCK = Lock()


def _try_worker_lock(cursor, job_id) -> bool:
    # The UUID's low 64 bits identify this worker to other database sessions.
    key = int.from_bytes(uuid.UUID(str(job_id)).bytes[-8:], "big", signed=True)
    cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
    return cursor.fetchone()[0]


@contextmanager
def _worker_lease(job_id):
    if connection.vendor != "postgresql":
        yield True
        return
    # A separate transaction keeps the lease across the claim, solve and publish.
    # PostgreSQL releases it on disconnect, including an abrupt process exit.
    lease = connection.copy()
    try:
        lease.set_autocommit(False)
        with lease.cursor() as cursor:
            # The solver works on another connection. Its lease is idle by design.
            cursor.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
            if lease.pg_version >= 170000:
                cursor.execute("SET LOCAL transaction_timeout = 0")
            acquired = _try_worker_lock(cursor, job_id)
        yield acquired
    finally:
        lease.close()


def reconcile_stale_planner_jobs(stale_minutes: int | None = None) -> int:
    """Fail orphaned jobs and return the number newly reconciled.

    PostgreSQL workers hold a lease from before claiming a job through its
    terminal commit, so an unlocked running job can be retried immediately.
    Queued jobs and jobs on other backends use the configured grace period.
    """
    if stale_minutes is None:
        stale_minutes = int(
            getattr(settings, PLANNER_JOB_STALE_SETTING, PLANNER_JOB_STALE_DEFAULT_MIN)
        )
    cutoff = timezone.now() - timedelta(minutes=max(1, stale_minutes))
    eligible = Q(started_at__lt=cutoff) | Q(started_at__isnull=True, submitted_at__lt=cutoff)
    if connection.vendor == "postgresql":
        eligible |= Q(status=PlannerJob.STATUS_RUNNING)
    stale = PlannerJob.objects.filter(
        eligible,
        status__in=[PlannerJob.STATUS_RUNNING, PlannerJob.STATUS_QUEUED],
    )
    with _EXECUTING_JOBS_LOCK:
        executing_ids = tuple(_EXECUTING_JOBS)
    stale = stale.exclude(pk__in=executing_ids)
    fields = {
        "status": PlannerJob.STATUS_FAILED,
        "error_message": "reconciled: the server stopped before the job finished (stale job swept)",
        "finished_at": timezone.now(),
    }
    if connection.vendor == "postgresql":
        updated = 0
        with transaction.atomic(), connection.cursor() as cursor:
            for job_id in list(stale.values_list("pk", flat=True)):
                if _try_worker_lock(cursor, job_id):
                    updated += stale.filter(pk=job_id).update(**fields)
    else:
        updated = stale.update(**fields)
    if updated:
        logger.warning("Reconciled %d stale planner job(s) -> failed", updated)
    return updated


_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pr7-planner")
    return _EXECUTOR


@contextmanager
def _planner_slot(*, wait: bool) -> Iterator[None]:
    with solver_slot(holder=HOLDER_PLANNER, wait=wait, give_up=shutting_down):
        if shutting_down():
            raise SolverBusy
        yield


def run_planner_job(job_id: uuid.UUID | str, *, wait: bool = False) -> None:
    """Run a submitted job under the solver limit. Only background workers wait."""
    try:
        with _planner_slot(wait=wait):
            _run_seeded_planner_job(job_id)
    except SolverBusy:
        _fail_unstarted(job_id)


def _worker(job_id: str) -> None:
    close_old_connections()
    try:
        run_planner_job(job_id, wait=True)
    finally:
        close_old_connections()


def _fail_unstarted(job_id: uuid.UUID | str) -> None:
    reason = "the server restarted" if shutting_down() else "the solver was busy"
    PlannerJob.objects.filter(id=job_id, status=PlannerJob.STATUS_QUEUED).update(
        status=PlannerJob.STATUS_FAILED,
        error_message=f"{reason} before the job could start; nothing was changed",
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
    with synchronous_planner() as run:
        return run(scenario_id=scenario_id, mode=mode, user=user, params=params)


@contextmanager
def synchronous_planner() -> Iterator[Callable[..., dict[str, Any]]]:
    """Reserve capacity before a request creates a scenario or a planner job."""
    with _planner_slot(wait=False):
        yield _submit_and_run_planner_job


def _submit_and_run_planner_job(
    *, scenario_id: int, mode: str, user: Any | None = None, params: dict | None = None
) -> dict[str, Any]:
    job_id = submit_planner_job(scenario_id=scenario_id, mode=mode, user=user, params=params)
    _run_seeded_planner_job(job_id)
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


def _error_message(value: object) -> str:
    """Keep even malformed engine diagnostics storable in a UTF-8 text column."""
    try:
        text = str(value)
    except Exception:
        text = f"Unprintable {type(value).__name__} diagnostic"
    escaped = text[:4000].replace("\x00", r"\x00")
    return escaped.encode("utf-8", errors="backslashreplace").decode("utf-8")[:4000]


def _check_planner_cancel(job_id: uuid.UUID | str) -> None:
    # A lost claim must stop future phases too; finalisation deliberately leaves
    # that terminal job and any subsequent manual edits untouched.
    if not PlannerJob.objects.filter(
        pk=job_id, status=PlannerJob.STATUS_RUNNING, cancel_requested=False
    ).exists():
        raise _PlannerCancelled


def _run_seeded_planner_job(job_id: uuid.UUID | str) -> None:
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
        with _worker_lease(job_id) as acquired:
            if acquired and job:
                with seeded_timetable_run((job.params or {}).get("seed")):
                    _run_planner_job(job_id)
    finally:
        with _EXECUTING_JOBS_LOCK:
            _EXECUTING_JOBS[key] -= 1
            if not _EXECUTING_JOBS[key]:
                del _EXECUTING_JOBS[key]


def _report_stage(job_id, stage: str, separate_connection: bool) -> None:
    if separate_connection:
        # Stage metadata must commit promptly so polling and cancellation can
        # use the job row while the private solver transaction is in progress.
        reporter = connection.copy()
        try:
            with reporter.cursor() as cursor:
                cursor.execute(
                    "UPDATE planner_jobs SET last_stage_seen=%s WHERE id=%s AND status=%s AND NOT cancel_requested",
                    [stage, job_id, PlannerJob.STATUS_RUNNING],
                )
                updated = cursor.rowcount
        finally:
            reporter.close()
    else:
        updated = PlannerJob.objects.filter(
            pk=job_id, status=PlannerJob.STATUS_RUNNING, cancel_requested=False
        ).update(last_stage_seen=stage)
    if not updated:
        raise _PlannerCancelled


def _execute_engine(job, progress=None):
    from core.services.timetable_autoplace import auto_place_scenario

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
            progress=progress,
            # Generate's weekly meetings per course, copied when the job was
            # submitted (absent on older jobs: the engine then decides).
            meeting_patterns=p.get("meeting_patterns"),
        )
    elif job.mode == PlannerJob.MODE_FULL_REBUILD:
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
    return result


def _run_planner_job(job_id: uuid.UUID | str) -> None:
    from core.services.planner_job_snapshot import restore_planner_job, snapshot_planner_job
    from core.services.planner_rebuild_workspace import rebuild_workspace

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
    proposal = None
    engine_error = None
    result = None
    failure = None
    try:
        _check_planner_cancel(job.id)
        if job.mode in (
            PlannerJob.MODE_FULL_REBUILD,
            PlannerJob.MODE_OPTIMISE_V2_FULL,
            PlannerJob.MODE_SCHEDULER_BUILD,
        ):
            separate_progress = connection.vendor == "postgresql" and not connection.in_atomic_block
            with rebuild_workspace() as proposal:
                # Keep an in-process cancellation request and last progress visible even
                # when a solver raises. Only the proposal is discarded on failure.
                try:
                    if job.mode == PlannerJob.MODE_FULL_REBUILD:
                        _clear_scenario_placements(job.scenario_id)
                    result = _execute_engine(
                        job, lambda stage: _report_stage(job.id, stage, separate_progress)
                    )
                    if not (isinstance(result, dict) and result.get("error")):
                        proposal.capture()
                except Exception as exc:
                    engine_error = exc
            if engine_error is not None:
                raise engine_error
        else:
            baseline = snapshot_planner_job(job.scenario_id)
            result = _execute_engine(job)
    except _PlannerCancelled:
        pass
    except Exception as exc:
        summary = f"{type(exc).__name__}: {_error_message(exc)}"
        tb_tail = "\n".join(traceback.format_exc().splitlines()[-6:])
        failure = _error_message(f"{summary}\n...\n{tb_tail}")

    try:
        with transaction.atomic():
            # Read the authoritative flag while holding the row lock through
            # both restoration and the terminal write. Never use the stale job
            # instance that was loaded before the solver ran.
            terminal = PlannerJob.objects.select_for_update().get(pk=job.id)
            if terminal.status != PlannerJob.STATUS_RUNNING:
                logger.warning("Planner %s lost running ownership; skipping finalisation", job.id)
                return
            # Some engines return an error after writing intermediate results.
            error = result.get("error") if isinstance(result, dict) else None
            if error:
                failure = _error_message(error)
            if baseline is not None and (terminal.cancel_requested or failure):
                restore_planner_job(baseline)
            if terminal.cancel_requested:
                terminal.status = PlannerJob.STATUS_CANCELLED
                terminal.result_json = None
                terminal.error_message = None
            else:
                terminal.result_json = (
                    {**result, "seed": (terminal.params or {}).get("seed")}
                    if isinstance(result, dict)
                    else result
                )
                if failure:
                    terminal.status = PlannerJob.STATUS_FAILED
                    terminal.error_message = failure
                else:
                    if proposal is not None:
                        proposal.publish()
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
        # A failed terminal save (for example, non-JSON solver telemetry) also
        # rolled back any restoration above. Release ownership and restore in
        # one transaction, using only fields we know can be persisted.
        reason = f"Planner finalisation failed: {type(exc).__name__}: {_error_message(exc)}"
        if failure:
            reason += f"\nSolver error: {failure}"
        running = PlannerJob.objects.filter(pk=job.id, status=PlannerJob.STATUS_RUNNING)
        failed_fields = {
            "status": PlannerJob.STATUS_FAILED,
            "result_json": None,
            "finished_at": timezone.now(),
        }
        try:
            with transaction.atomic():
                if (
                    running.update(**failed_fields, error_message=_error_message(reason))
                    and baseline is not None
                ):
                    restore_planner_job(baseline)
        except Exception as restore_exc:
            logger.exception("Could not restore timetable for planner job %s", job.id)
            reason = f"Timetable restore failed: {type(restore_exc).__name__}: {_error_message(restore_exc)}\n{reason}"
            running.update(**failed_fields, error_message=_error_message(reason))


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
    "synchronous_planner",
]
