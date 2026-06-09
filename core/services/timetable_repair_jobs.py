"""Durable job orchestration for timetable repair analysis.

Repair jobs are evidence-only. They may create analysis/simulation
``TimetableRepairRun`` rows, but they never approve or apply changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from datetime import timedelta
from time import sleep
from typing import Any
from uuid import UUID

from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import SectionPlacement, TimetableRepairJob, TimetableScenario
from core.services.timetable_repair import (
    REPAIR_CACHE_VERSION,
    analyse_timetable_repair,
    repair_run_detail,
    simulate_timetable_repair_scope,
)

TERMINAL_STATUSES = {
    TimetableRepairJob.STATUS_SUCCEEDED,
    TimetableRepairJob.STATUS_FAILED,
    TimetableRepairJob.STATUS_CANCELLED,
}
REPAIR_JOB_API_CONTRACT_VERSION = "repair-job-api-contract-v1"
REPAIR_JOB_REUSE_STAGE = "reused_completed_result"
DEFAULT_STALE_RUNNING_SECONDS = 30 * 60
DEFAULT_MAX_JOB_ATTEMPTS = 3


def _running_under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _json_signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_signature(kind: str, payload: dict[str, Any]) -> str:
    return _json_signature(
        {
            "kind": kind,
            "cache_version": REPAIR_CACHE_VERSION,
            "payload": payload,
        }
    )


def _normalise_student_ids(values: list[Any]) -> list[int]:
    clean: list[int] = []
    seen: set[int] = set()
    for raw in values:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            clean.append(value)
            seen.add(value)
    return clean


def _normalise_limits(limits: dict[str, Any] | None) -> dict[str, int]:
    clean: dict[str, int] = {}
    for key, value in (limits or {}).items():
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        clean[str(key)] = parsed
    return clean


def submit_repair_analysis_job(
    *,
    placement_id: int,
    blocked_student_ids: list[Any] | None = None,
    blocked_requests: list[dict[str, Any]] | None = None,
    mode: str = "conservative",
    limits: dict[str, Any] | None = None,
    requested_by=None,
    active_plan_filter: str | None = None,
    dispatch_inline: bool | None = None,
) -> TimetableRepairJob:
    """Create a queued repair-analysis job and optionally execute it inline."""

    placement = SectionPlacement.objects.select_related("board__scenario").get(id=int(placement_id))
    payload = {
        "placement_id": int(placement_id),
        "blocked_student_ids": _normalise_student_ids(blocked_student_ids or []),
        "blocked_requests": list(blocked_requests or []),
        "mode": str(mode or "conservative"),
        "active_plan_filter": str(active_plan_filter or "ALL"),
        "limits": _normalise_limits(limits),
    }
    request_signature = _request_signature(TimetableRepairJob.KIND_ANALYSIS, payload)
    cache_fingerprint = _json_signature(
        {"scenario_id": placement.board.scenario_id, "payload": payload}
    )
    reusable_job = _find_reusable_completed_job(
        kind=TimetableRepairJob.KIND_ANALYSIS,
        scenario_id=placement.board.scenario_id,
        request_signature=request_signature,
    )
    if reusable_job is not None:
        return _create_reused_completed_job(
            kind=TimetableRepairJob.KIND_ANALYSIS,
            scenario=placement.board.scenario,
            requested_by=requested_by,
            request_signature=request_signature,
            cache_fingerprint=cache_fingerprint,
            request_payload=payload,
            reusable_job=reusable_job,
        )

    job = TimetableRepairJob.objects.create(
        kind=TimetableRepairJob.KIND_ANALYSIS,
        scenario=placement.board.scenario,
        submitted_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        request_signature=request_signature,
        cache_fingerprint=cache_fingerprint,
        request_payload=payload,
        progress_json={"stage": "queued", "percent": 0},
    )
    if dispatch_inline if dispatch_inline is not None else _running_under_pytest():
        run_repair_job(job.id, worker_id="inline")
        job.refresh_from_db()
    return job


def submit_repair_simulation_job(
    *,
    scenario_id: int,
    program: str = "",
    nominal_term: int | None = None,
    course_keys: list[Any] | None = None,
    limits: dict[str, Any] | None = None,
    max_placements: int = 8,
    requested_by=None,
    dispatch_inline: bool | None = None,
) -> TimetableRepairJob:
    """Create a queued scope-simulation job and optionally execute it inline."""

    scenario = TimetableScenario.objects.get(id=int(scenario_id))
    payload = {
        "scenario_id": int(scenario_id),
        "program": str(program or ""),
        "nominal_term": int(nominal_term) if nominal_term not in {None, ""} else None,
        "course_keys": [str(course) for course in (course_keys or [])],
        "limits": _normalise_limits(limits),
        "max_placements": max(1, int(max_placements or 8)),
    }
    request_signature = _request_signature(TimetableRepairJob.KIND_SIMULATION, payload)
    cache_fingerprint = _json_signature({"scenario_id": scenario.id, "payload": payload})
    reusable_job = _find_reusable_completed_job(
        kind=TimetableRepairJob.KIND_SIMULATION,
        scenario_id=scenario.id,
        request_signature=request_signature,
    )
    if reusable_job is not None:
        return _create_reused_completed_job(
            kind=TimetableRepairJob.KIND_SIMULATION,
            scenario=scenario,
            requested_by=requested_by,
            request_signature=request_signature,
            cache_fingerprint=cache_fingerprint,
            request_payload=payload,
            reusable_job=reusable_job,
        )

    job = TimetableRepairJob.objects.create(
        kind=TimetableRepairJob.KIND_SIMULATION,
        scenario=scenario,
        submitted_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        request_signature=request_signature,
        cache_fingerprint=cache_fingerprint,
        request_payload=payload,
        progress_json={"stage": "queued", "percent": 0},
    )
    if dispatch_inline if dispatch_inline is not None else _running_under_pytest():
        run_repair_job(job.id, worker_id="inline")
        job.refresh_from_db()
    return job


def get_repair_job(job_id: UUID | str) -> TimetableRepairJob | None:
    return (
        TimetableRepairJob.objects.select_related(
            "scenario",
            "repair_run",
            "submitted_by",
        )
        .filter(id=job_id)
        .first()
    )


def cancel_repair_job(job_id: UUID | str, *, requested_by=None) -> bool:
    job = TimetableRepairJob.objects.filter(id=job_id).first()
    if job is None or job.status in TERMINAL_STATUSES:
        return False
    job.cancel_requested = True
    job.progress_json = {
        **(job.progress_json or {}),
        "cancel_requested": True,
        "cancel_requested_at": timezone.now().isoformat(),
    }
    job.save(update_fields=["cancel_requested", "progress_json"])
    return True


def retry_repair_job(
    job_id: UUID | str,
    *,
    requested_by=None,
    max_attempts: int = DEFAULT_MAX_JOB_ATTEMPTS,
    dispatch_inline: bool | None = None,
) -> TimetableRepairJob | None:
    """Create a fresh queued retry for a failed/cancelled evidence job."""

    source = get_repair_job(job_id)
    if source is None or source.status not in {
        TimetableRepairJob.STATUS_FAILED,
        TimetableRepairJob.STATUS_CANCELLED,
    }:
        return None
    if int(source.attempt_count or 0) >= max(1, int(max_attempts or DEFAULT_MAX_JOB_ATTEMPTS)):
        return None

    job = TimetableRepairJob.objects.create(
        kind=source.kind,
        scenario=source.scenario,
        submitted_by=requested_by
        if getattr(requested_by, "is_authenticated", False)
        else source.submitted_by,
        request_signature=source.request_signature,
        cache_fingerprint=source.cache_fingerprint,
        request_payload=source.request_payload or {},
        progress_json={
            "stage": "queued_retry",
            "percent": 0,
            "retry_of_job_id": str(source.id),
            "previous_status": source.status,
            "previous_attempt_count": int(source.attempt_count or 0),
        },
    )
    if dispatch_inline if dispatch_inline is not None else _running_under_pytest():
        run_repair_job(job.id, worker_id="inline-retry")
        job.refresh_from_db()
    return job


def recover_stale_repair_jobs(
    *,
    stale_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
    max_attempts: int = DEFAULT_MAX_JOB_ATTEMPTS,
    limit: int = 50,
    worker_id: str = "repair-worker",
) -> list[TimetableRepairJob]:
    """Recover running jobs whose worker heartbeat is stale.

    A stale job is requeued when it still has retry attempts left. If it has
    already reached the attempt cap, it is failed with explicit diagnostics.
    """

    cutoff = timezone.now() - timedelta(seconds=max(1, int(stale_after_seconds or 1)))
    stale_ids = list(
        TimetableRepairJob.objects.filter(status=TimetableRepairJob.STATUS_RUNNING)
        .filter(Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff))
        .order_by("started_at", "submitted_at", "id")
        .values_list("id", flat=True)[: max(1, int(limit or 1))]
    )
    recovered: list[TimetableRepairJob] = []
    for job_id in stale_ids:
        with transaction.atomic():
            qs = TimetableRepairJob.objects.filter(id=job_id)
            if connection.features.has_select_for_update:
                qs = qs.select_for_update()
            job = qs.first()
            if job is None or not _repair_job_is_stale(job, cutoff):
                continue
            recovered.append(
                _recover_one_stale_job_locked(
                    job,
                    max_attempts=max_attempts,
                    worker_id=worker_id,
                )
            )
    return recovered


def run_repair_job(job_id: UUID | str, *, worker_id: str = "manual") -> TimetableRepairJob | None:
    """Claim and execute one queued repair job."""

    with transaction.atomic():
        job_qs = TimetableRepairJob.objects.filter(id=job_id)
        if connection.features.has_select_for_update:
            job_qs = job_qs.select_for_update()
        job = job_qs.first()
        if job is None:
            return None
        if job.status != TimetableRepairJob.STATUS_QUEUED:
            return job
        now = timezone.now()
        if job.cancel_requested:
            job.status = TimetableRepairJob.STATUS_CANCELLED
            job.started_at = now
            job.finished_at = now
            job.progress_json = {
                **(job.progress_json or {}),
                "stage": "cancelled_before_start",
                "percent": 100,
            }
            job.save(update_fields=["status", "started_at", "finished_at", "progress_json"])
            return job
        previous_progress = dict(job.progress_json or {})
        job.status = TimetableRepairJob.STATUS_RUNNING
        job.started_at = now
        job.locked_by = worker_id[:128]
        job.locked_at = now
        job.heartbeat_at = now
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.progress_json = {**previous_progress, "stage": "running", "percent": 10}
        job.save(
            update_fields=[
                "status",
                "started_at",
                "locked_by",
                "locked_at",
                "heartbeat_at",
                "attempt_count",
                "progress_json",
            ]
        )

    try:
        result = _execute_repair_job_payload(job)
    except Exception as exc:  # noqa: BLE001 - store structured failure for admin review.
        job = TimetableRepairJob.objects.get(id=job_id)
        job.status = TimetableRepairJob.STATUS_FAILED
        job.error_message = _format_exception(exc)
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        job.progress_json = {
            **(job.progress_json or {}),
            "stage": "failed",
            "percent": 100,
        }
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "progress_json",
            ]
        )
        return job

    job = TimetableRepairJob.objects.get(id=job_id)
    job.status = TimetableRepairJob.STATUS_SUCCEEDED
    job.result_json = result
    job.error_message = ""
    job.finished_at = timezone.now()
    job.heartbeat_at = job.finished_at
    job.progress_json = {
        **(job.progress_json or {}),
        "stage": "succeeded",
        "percent": 100,
        "result_kind": job.kind,
    }
    run_id = ((result.get("analysis") or {}).get("run") or {}).get("id")
    if run_id:
        job.repair_run_id = run_id
    job.save(
        update_fields=[
            "status",
            "result_json",
            "error_message",
            "finished_at",
            "heartbeat_at",
            "progress_json",
            "repair_run",
        ]
    )
    return job


def _execute_repair_job_payload(job: TimetableRepairJob) -> dict[str, Any]:
    payload = job.request_payload or {}
    if job.kind == TimetableRepairJob.KIND_ANALYSIS:
        detail = analyse_timetable_repair(
            placement_id=int(payload["placement_id"]),
            blocked_student_ids=list(payload.get("blocked_student_ids") or []),
            blocked_requests=list(payload.get("blocked_requests") or []),
            mode=str(payload.get("mode") or "conservative"),
            requested_by=job.submitted_by,
            limits=dict(payload.get("limits") or {}),
            active_plan_filter=str(payload.get("active_plan_filter") or "ALL"),
        )
        return {
            "job_result_version": "repair-job-result-v1",
            "analysis": detail,
        }
    if job.kind == TimetableRepairJob.KIND_SIMULATION:
        result = simulate_timetable_repair_scope(
            scenario_id=int(payload["scenario_id"]),
            program=str(payload.get("program") or ""),
            nominal_term=payload.get("nominal_term"),
            course_keys=list(payload.get("course_keys") or []),
            requested_by=job.submitted_by,
            limits=dict(payload.get("limits") or {}),
            max_placements=int(payload.get("max_placements") or 8),
        )
        return {
            "job_result_version": "repair-job-result-v1",
            "simulation": result,
        }
    raise ValueError(f"Unsupported repair job kind: {job.kind}")


def _find_reusable_completed_job(
    *,
    kind: str,
    scenario_id: int,
    request_signature: str,
) -> TimetableRepairJob | None:
    """Return a completed job whose stored repair result is still scenario-current."""

    candidates = (
        TimetableRepairJob.objects.select_related("repair_run")
        .filter(
            kind=kind,
            scenario_id=scenario_id,
            request_signature=request_signature,
            status=TimetableRepairJob.STATUS_SUCCEEDED,
        )
        .order_by("-finished_at", "-submitted_at", "-id")[:20]
    )
    for job in candidates:
        if _completed_job_result_is_current(job):
            return job
    return None


def _create_reused_completed_job(
    *,
    kind: str,
    scenario: TimetableScenario,
    requested_by,
    request_signature: str,
    cache_fingerprint: str,
    request_payload: dict[str, Any],
    reusable_job: TimetableRepairJob,
) -> TimetableRepairJob:
    """Record a new submission that reuses a fresh completed result without solver work."""

    now = timezone.now()
    job = TimetableRepairJob.objects.create(
        kind=kind,
        scenario=scenario,
        repair_run=reusable_job.repair_run,
        submitted_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        status=TimetableRepairJob.STATUS_SUCCEEDED,
        request_signature=request_signature,
        cache_fingerprint=cache_fingerprint,
        request_payload=request_payload,
        progress_json={
            "stage": REPAIR_JOB_REUSE_STAGE,
            "percent": 100,
            "result_kind": kind,
            "reused_from_job_id": str(reusable_job.id),
            "reuse_policy": "completed_job_result_reused_only_when_repair_runs_are_current",
        },
        result_json=reusable_job.result_json or {},
        started_at=now,
        finished_at=now,
        heartbeat_at=now,
    )
    return job


def _completed_job_result_is_current(job: TimetableRepairJob) -> bool:
    if job.status != TimetableRepairJob.STATUS_SUCCEEDED or not job.result_json:
        return False
    if job.kind == TimetableRepairJob.KIND_ANALYSIS:
        run_id = job.repair_run_id or (
            ((job.result_json.get("analysis") or {}).get("run") or {}).get("id")
        )
        return _repair_run_is_current(run_id)
    if job.kind == TimetableRepairJob.KIND_SIMULATION:
        run_ids = [
            row.get("run_id")
            for row in ((job.result_json.get("simulation") or {}).get("runs") or [])
            if row.get("run_id")
        ]
        if not run_ids:
            return False
        return all(_repair_run_is_current(run_id) for run_id in run_ids)
    return False


def _repair_run_is_current(run_id: UUID | str | None) -> bool:
    if not run_id:
        return False
    try:
        freshness = repair_run_detail(run_id).get("run_freshness") or {}
    except Exception:  # noqa: BLE001 - stale or deleted audit rows must never be reused.
        return False
    return bool(freshness.get("recommendation_current"))


def run_next_repair_job(*, worker_id: str = "repair-worker") -> TimetableRepairJob | None:
    job = _next_queued_job()
    if job is None:
        return None
    return run_repair_job(job.id, worker_id=worker_id)


def run_repair_worker_loop(
    *,
    worker_id: str = "repair-worker",
    once: bool = False,
    idle_sleep_seconds: float = 2.0,
    recover_stale: bool = True,
    stale_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
    max_attempts: int = DEFAULT_MAX_JOB_ATTEMPTS,
) -> int:
    """Run queued repair jobs until stopped. Returns executed job count."""

    executed = 0
    while True:
        close_old_connections()
        try:
            if recover_stale:
                recover_stale_repair_jobs(
                    worker_id=worker_id,
                    stale_after_seconds=stale_after_seconds,
                    max_attempts=max_attempts,
                )
            job = run_next_repair_job(worker_id=worker_id)
        finally:
            close_old_connections()
        if job is None:
            if once:
                return executed
            sleep(max(0.1, float(idle_sleep_seconds)))
            continue
        executed += 1
        if once:
            return executed


def _next_queued_job() -> TimetableRepairJob | None:
    with transaction.atomic():
        qs = TimetableRepairJob.objects.filter(
            status=TimetableRepairJob.STATUS_QUEUED,
        ).order_by("submitted_at", "id")
        if connection.features.has_select_for_update:
            qs = qs.select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked
            )
        return qs.first()


def _repair_job_is_stale(job: TimetableRepairJob, cutoff) -> bool:
    if job.status != TimetableRepairJob.STATUS_RUNNING:
        return False
    reference = job.heartbeat_at or job.started_at
    return reference is not None and reference < cutoff


def _recover_one_stale_job_locked(
    job: TimetableRepairJob,
    *,
    max_attempts: int,
    worker_id: str,
) -> TimetableRepairJob:
    now = timezone.now()
    previous_progress = dict(job.progress_json or {})
    previous_locked_by = job.locked_by
    previous_heartbeat = job.heartbeat_at.isoformat() if job.heartbeat_at else ""
    previous_started = job.started_at.isoformat() if job.started_at else ""
    recovery_count = int(previous_progress.get("stale_recovery_count") or 0) + 1
    recovery_payload = {
        **previous_progress,
        "stale_recovery_count": recovery_count,
        "recovered_at": now.isoformat(),
        "recovered_by": worker_id[:128],
        "previous_locked_by": previous_locked_by,
        "previous_heartbeat_at": previous_heartbeat,
        "previous_started_at": previous_started,
        "max_attempts": max(1, int(max_attempts or DEFAULT_MAX_JOB_ATTEMPTS)),
    }

    if job.cancel_requested:
        job.status = TimetableRepairJob.STATUS_CANCELLED
        job.finished_at = now
        job.heartbeat_at = now
        job.locked_by = ""
        job.locked_at = None
        job.progress_json = {
            **recovery_payload,
            "stage": "cancelled_after_stale_worker",
            "percent": 100,
        }
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "heartbeat_at",
                "locked_by",
                "locked_at",
                "progress_json",
            ]
        )
        return job

    if int(job.attempt_count or 0) >= max(1, int(max_attempts or DEFAULT_MAX_JOB_ATTEMPTS)):
        job.status = TimetableRepairJob.STATUS_FAILED
        job.error_message = (
            "Repair job exceeded the maximum worker attempts after stale heartbeat recovery."
        )
        job.finished_at = now
        job.heartbeat_at = now
        job.locked_by = ""
        job.locked_at = None
        job.progress_json = {
            **recovery_payload,
            "stage": "failed_stale_max_attempts",
            "percent": 100,
        }
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "locked_by",
                "locked_at",
                "progress_json",
            ]
        )
        return job

    job.status = TimetableRepairJob.STATUS_QUEUED
    job.error_message = ""
    job.locked_by = ""
    job.locked_at = None
    job.heartbeat_at = None
    job.progress_json = {
        **recovery_payload,
        "stage": "requeued_after_stale_worker",
        "percent": 0,
    }
    job.save(
        update_fields=[
            "status",
            "error_message",
            "locked_by",
            "locked_at",
            "heartbeat_at",
            "progress_json",
        ]
    )
    return job


def list_repair_jobs(
    *,
    scenario_id: int | None = None,
    kind: str = "",
    status: str = "",
    submitted_by_id: int | None = None,
    limit: int = 50,
) -> tuple[list[TimetableRepairJob], dict[str, Any]]:
    """Return recent repair jobs for an operations view."""

    page_limit = max(1, min(int(limit or 50), 100))
    qs = TimetableRepairJob.objects.select_related(
        "scenario",
        "repair_run",
        "submitted_by",
    )
    filters: dict[str, Any] = {"limit": page_limit}
    if scenario_id is not None:
        qs = qs.filter(scenario_id=int(scenario_id))
        filters["scenario_id"] = int(scenario_id)
    if kind:
        qs = qs.filter(kind=kind)
        filters["kind"] = kind
    if status:
        qs = qs.filter(status=status)
        filters["status"] = status
    if submitted_by_id is not None:
        qs = qs.filter(submitted_by_id=int(submitted_by_id))
        filters["submitted_by_id"] = int(submitted_by_id)

    rows = list(qs.order_by("-submitted_at", "-id")[: page_limit + 1])
    has_more = len(rows) > page_limit
    return rows[:page_limit], {"filters": filters, "has_more": has_more}


def serialize_repair_job(
    job: TimetableRepairJob,
    *,
    include_result: bool = False,
) -> dict[str, Any]:
    payload = {
        "api_contract": _repair_job_api_contract(job),
        "job_id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "scenario_id": job.scenario_id,
        "repair_run_id": str(job.repair_run_id) if job.repair_run_id else "",
        "submitted_by": getattr(job.submitted_by, "username", "") if job.submitted_by_id else "",
        "submitted_at": job.submitted_at.isoformat() if job.submitted_at else "",
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
        "cancel_requested": job.cancel_requested,
        "request_signature": job.request_signature,
        "cache_fingerprint": job.cache_fingerprint,
        "attempt_count": int(job.attempt_count or 0),
        "locked_by": job.locked_by,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else "",
        "progress": job.progress_json or {},
        "reuse": _repair_job_reuse_payload(job),
        "recovery": _repair_job_recovery_payload(job),
        "error_message": job.error_message,
    }
    if include_result:
        payload["result"] = job.result_json or {}
    return payload


def _repair_job_api_contract(job: TimetableRepairJob) -> dict[str, Any]:
    job_id = str(job.id)
    return {
        "version": REPAIR_JOB_API_CONTRACT_VERSION,
        "execution_policy": "queued_job_is_evidence_only_and_never_applies_repair_changes",
        "result_policy": "poll_job_then_fetch_result_when_terminal",
        "reuse_policy": "completed_results_may_be_reused_only_when_underlying_repair_runs_are_current",
        "recovery_policy": "stale_running_jobs_are_requeued_until_attempt_cap_then_failed",
        "endpoint_templates": {
            "submit": "/ops/tw/repair/jobs/",
            "list": "/ops/tw/repair/jobs/list/",
            "poll": "/ops/tw/repair/jobs/{job_id}/",
            "result": "/ops/tw/repair/jobs/{job_id}/result/",
            "cancel": "/ops/tw/repair/jobs/{job_id}/cancel/",
            "retry": "/ops/tw/repair/jobs/{job_id}/retry/",
            "recover_stale": "/ops/tw/repair/jobs/recover-stale/",
        },
        "endpoints": {
            "list": "/ops/tw/repair/jobs/list/",
            "poll": f"/ops/tw/repair/jobs/{job_id}/",
            "result": f"/ops/tw/repair/jobs/{job_id}/result/",
            "cancel": f"/ops/tw/repair/jobs/{job_id}/cancel/",
            "retry": f"/ops/tw/repair/jobs/{job_id}/retry/",
            "recover_stale": "/ops/tw/repair/jobs/recover-stale/",
        },
    }


def _repair_job_reuse_payload(job: TimetableRepairJob) -> dict[str, Any]:
    progress = job.progress_json or {}
    reused_from_job_id = str(progress.get("reused_from_job_id") or "")
    return {
        "reused": bool(reused_from_job_id),
        "reused_from_job_id": reused_from_job_id,
        "policy": "completed_result_reuse_requires_matching_request_signature_and_current_repair_run_freshness",
    }


def _repair_job_recovery_payload(job: TimetableRepairJob) -> dict[str, Any]:
    progress = job.progress_json or {}
    return {
        "stale_recovery_count": int(progress.get("stale_recovery_count") or 0),
        "retry_of_job_id": str(progress.get("retry_of_job_id") or ""),
        "recovered_at": str(progress.get("recovered_at") or ""),
        "policy": "running_jobs_with_stale_heartbeat_are_requeued_until_attempt_cap",
    }


def repair_job_collection_api_contract() -> dict[str, Any]:
    """Return the collection-level job API contract for empty list responses."""

    return {
        "version": REPAIR_JOB_API_CONTRACT_VERSION,
        "execution_policy": "queued_job_is_evidence_only_and_never_applies_repair_changes",
        "result_policy": "poll_job_then_fetch_result_when_terminal",
        "list_policy": "recent_jobs_are_returned_newest_first_with_explicit_filters",
        "endpoint_templates": {
            "submit": "/ops/tw/repair/jobs/",
            "list": "/ops/tw/repair/jobs/list/",
            "poll": "/ops/tw/repair/jobs/{job_id}/",
            "result": "/ops/tw/repair/jobs/{job_id}/result/",
            "cancel": "/ops/tw/repair/jobs/{job_id}/cancel/",
            "retry": "/ops/tw/repair/jobs/{job_id}/retry/",
            "recover_stale": "/ops/tw/repair/jobs/recover-stale/",
        },
        "endpoints": {
            "submit": "/ops/tw/repair/jobs/",
            "list": "/ops/tw/repair/jobs/list/",
            "recover_stale": "/ops/tw/repair/jobs/recover-stale/",
        },
    }


def _format_exception(exc: Exception) -> str:
    summary = f"{type(exc).__name__}: {exc}"
    tb_tail = "\n".join(traceback.format_exc().splitlines()[-8:])
    return f"{summary}\n...\n{tb_tail}"[:4000]


__all__ = [
    "TERMINAL_STATUSES",
    "cancel_repair_job",
    "get_repair_job",
    "list_repair_jobs",
    "repair_job_collection_api_contract",
    "recover_stale_repair_jobs",
    "retry_repair_job",
    "run_next_repair_job",
    "run_repair_job",
    "run_repair_worker_loop",
    "serialize_repair_job",
    "submit_repair_analysis_job",
    "submit_repair_simulation_job",
]
