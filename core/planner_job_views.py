"""PR7 commit 5 — REST views for async planner jobs.

Four endpoints, flag-gated. When
``TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=False`` each returns 404 (no
row creation, no side effects) — preserves pre-PR7 behaviour exactly.

POST /planner-jobs/              submit
GET  /planner-jobs/<id>/         poll (status + lightweight metadata, no result_json)
GET  /planner-jobs/<id>/result/  result (404 until succeeded)
POST /planner-jobs/<id>/cancel/  cancel (idempotent)

Execution is synchronous in-process for commits 3-5. The
ThreadPoolExecutor dispatcher that runs ``run_planner_job`` off the
request thread lands alongside the UI toggle in commit 6.
"""

from __future__ import annotations

import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.services.planner_job_runner import (
    cancel_planner_job,
    dispatch_planner_job,
    get_planner_job,
    is_async_planner_enabled,
    submit_planner_job,
)


def _flag_off_404() -> HttpResponse:
    return JsonResponse({"detail": "PR7 async planner disabled"}, status=404)


@csrf_exempt
@require_http_methods(["POST"])
def planner_job_submit(request: HttpRequest) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid JSON"}, status=400)
    scenario_id = payload.get("scenario_id")
    mode = payload.get("mode")
    if not isinstance(scenario_id, int) or mode not in {"optimise_current", "full_rebuild"}:
        return JsonResponse({"detail": "bad scenario_id/mode"}, status=400)
    user = request.user if getattr(request, "user", None) else None
    job_id = submit_planner_job(scenario_id=scenario_id, mode=mode, user=user)
    dispatch_planner_job(job_id)
    return JsonResponse({"job_id": str(job_id), "status": "queued"}, status=201)


@require_http_methods(["GET"])
def planner_job_poll(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    job = get_planner_job(job_id)
    if job is None:
        return JsonResponse({"detail": "not found"}, status=404)
    return JsonResponse(
        {
            "job_id": str(job.id),
            "status": job.status,
            "mode": job.mode,
            "last_stage_seen": job.last_stage_seen,
            "submitted_at": job.submitted_at.isoformat() if job.submitted_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "cancel_requested": job.cancel_requested,
            "error_message": job.error_message,
        }
    )


@require_http_methods(["GET"])
def planner_job_result(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    job = get_planner_job(job_id)
    if job is None or job.status != "succeeded":
        return JsonResponse({"detail": "result not ready"}, status=404)
    return JsonResponse({"job_id": str(job.id), "result": job.result_json})


@csrf_exempt
@require_http_methods(["POST"])
def planner_job_cancel(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    ok = cancel_planner_job(job_id)
    if not ok:
        return JsonResponse({"detail": "cannot cancel"}, status=404)
    return JsonResponse({"job_id": job_id, "cancel_requested": True}, status=202)
