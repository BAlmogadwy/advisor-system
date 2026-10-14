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
from django.views.decorators.http import require_http_methods

from core.services.planner_job_runner import (
    cancel_planner_job,
    dispatch_planner_job,
    get_planner_job,
    reconcile_stale_planner_jobs,
    serialize_planner_job,
    submit_planner_job,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.timetable_flags import is_async_planner_enabled
from core.services.timetable_mutation_guard import (
    ScenarioPlannerBusy,
    ScenarioPublished,
    reject_active_planner_job,
    scenario_busy_response,
)


def _flag_off_404() -> HttpResponse:
    return JsonResponse({"detail": "PR7 async planner disabled"}, status=404)


def _deny_unless_general_advisor(request: HttpRequest) -> HttpResponse | None:
    """Guard: 401 for anonymous callers, 403 below General Advisor.

    Mirrors ``timetable_workspace_views._require_general_advisor`` — these
    endpoints mutate scenario placements, so they carry the same role floor
    as every ``tw_*`` view. JSON statuses (not a login redirect) because the
    only legitimate callers are ``fetch()`` clients that send X-CSRFToken.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return JsonResponse({"detail": "authentication required"}, status=401)
    if get_user_role(user) not in {ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN}:
        return JsonResponse({"detail": "General Advisor access required"}, status=403)
    return None


@require_http_methods(["POST"])
def planner_job_submit(request: HttpRequest) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    deny = _deny_unless_general_advisor(request)
    if deny is not None:
        return deny
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid JSON"}, status=400)
    scenario_id = payload.get("scenario_id")
    mode = payload.get("mode")
    if not isinstance(scenario_id, int) or mode not in {"optimise_current", "full_rebuild"}:
        return JsonResponse({"detail": "bad scenario_id/mode"}, status=400)
    from core.models import TimetableScenario

    scenario = TimetableScenario.objects.filter(pk=scenario_id).first()
    if scenario is None:
        return JsonResponse({"detail": "Scenario not found"}, status=404)
    if scenario.status == "published":
        return JsonResponse({"detail": "Cannot modify published scenario"}, status=400)
    busy = reject_active_planner_job(scenario_id)
    if busy is not None:
        return busy
    try:
        job_id = submit_planner_job(scenario_id=scenario_id, mode=mode, user=request.user)
    except ScenarioPlannerBusy as exc:
        return scenario_busy_response(exc.job)
    except ScenarioPublished:
        return JsonResponse({"detail": "Cannot modify published scenario"}, status=400)
    dispatch_planner_job(job_id)
    return JsonResponse({"job_id": str(job_id), "status": "queued"}, status=201)


@require_http_methods(["GET"])
def planner_job_poll(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    deny = _deny_unless_general_advisor(request)
    if deny is not None:
        return deny
    # Sweep ghost RUNNING jobs from a dead server so a spinning UI sees the
    # orphaned job flip to failed instead of polling forever.
    reconcile_stale_planner_jobs()
    job = get_planner_job(job_id)
    if job is None:
        return JsonResponse({"detail": "not found"}, status=404)
    return JsonResponse(serialize_planner_job(job))


@require_http_methods(["GET"])
def planner_job_result(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    deny = _deny_unless_general_advisor(request)
    if deny is not None:
        return deny
    job = get_planner_job(job_id)
    if job is None or job.status != "succeeded":
        return JsonResponse({"detail": "result not ready"}, status=404)
    return JsonResponse({"job_id": str(job.id), "result": job.result_json})


@require_http_methods(["POST"])
def planner_job_cancel(request: HttpRequest, job_id: str) -> HttpResponse:
    if not is_async_planner_enabled():
        return _flag_off_404()
    deny = _deny_unless_general_advisor(request)
    if deny is not None:
        return deny
    ok = cancel_planner_job(job_id)
    if not ok:
        return JsonResponse({"detail": "cannot cancel"}, status=404)
    return JsonResponse({"job_id": job_id, "cancel_requested": True}, status=202)
