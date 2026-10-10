"""Reject interactive writes while a planner owns a scenario.

Manual edits hold the scenario lock through their write. Planner submission
holds the same lock through job creation, then dispatches after committing.
SQLite writable transactions use BEGIN IMMEDIATE in the project settings;
PostgreSQL uses the scenario row lock. Internal worker services bypass this UI
guard, so cooperative execution and cancellation keep their existing behavior.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import (
    DeliveryBoard,
    PlannerJob,
    SectionPlacement,
    TimetableRepairGlobalPlan,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role


class ScenarioPlannerBusy(Exception):
    def __init__(self, job: PlannerJob) -> None:
        self.job = job
        super().__init__("This scenario is being updated by a planner job. Wait for it to finish.")


class ScenarioPublished(Exception):
    """Submission must re-check published status under the same scenario lock."""


def active_planner_job(scenario_id: int) -> PlannerJob | None:
    return (
        PlannerJob.objects.filter(
            scenario_id=scenario_id,
            status__in=(PlannerJob.STATUS_QUEUED, PlannerJob.STATUS_RUNNING),
        )
        .order_by("-submitted_at", "-id")
        .first()
    )


def scenario_busy_response(job: PlannerJob) -> JsonResponse:
    from core.services.planner_job_runner import serialize_planner_job

    return JsonResponse(
        {
            "ok": False,
            "error": {
                "code": "SCENARIO_BUSY",
                "message": str(ScenarioPlannerBusy(job)),
                "details": {
                    "scenario_id": job.scenario_id,
                    "active_job": serialize_planner_job(job),
                },
            },
        },
        status=409,
    )


def reject_active_planner_job(scenario_id: int) -> JsonResponse | None:
    job = active_planner_job(scenario_id)
    return scenario_busy_response(job) if job else None


def _scenario_id(
    request: HttpRequest, arguments: dict[str, Any], *, payload_scope: str
) -> int | None:
    try:
        if arguments.get("scenario_id") is not None:
            return int(arguments["scenario_id"])
        if arguments.get("placement_id") is not None:
            value = (
                SectionPlacement.objects.filter(pk=arguments["placement_id"])
                .values_list(
                    "board__scenario_id",
                    flat=True,
                )
                .first()
            )
            return int(value) if value is not None else None
        if arguments.get("run_id") is not None:
            value = (
                TimetableRepairRun.objects.filter(pk=arguments["run_id"])
                .values_list(
                    "scenario_id",
                    flat=True,
                )
                .first()
            )
            return int(value) if value is not None else None
        if arguments.get("plan_id") is not None:
            value = (
                TimetableRepairGlobalPlan.objects.filter(pk=arguments["plan_id"])
                .values_list(
                    "scenario_id",
                    flat=True,
                )
                .first()
            )
            return int(value) if value is not None else None
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload, dict):
            return None
        if payload_scope == "scenario" and payload.get("scenario_id") is not None:
            return int(payload["scenario_id"])
        if payload_scope == "board" and payload.get("board_id") is not None:
            value = (
                DeliveryBoard.objects.filter(pk=int(payload["board_id"]))
                .values_list(
                    "scenario_id",
                    flat=True,
                )
                .first()
            )
            return int(value) if value is not None else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        # Invalid input retains the endpoint's existing validation response.
        return None
    return None


def guard_manual_scenario_write(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    signature = inspect.signature(view)
    payload_scope = "scenario" if view.__name__ == "tw_board_create_view" else "board"

    @wraps(view)
    def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Leave authentication, role and method responses to the endpoint.
        if request.method != "POST" or get_user_role(request.user) not in {
            ROLE_GENERAL_ADVISOR,
            ROLE_SUPER_ADMIN,
        }:
            return view(request, *args, **kwargs)
        arguments = dict(signature.bind_partial(request, *args, **kwargs).arguments)
        scenario_id = _scenario_id(request, arguments, payload_scope=payload_scope)
        if scenario_id is None:
            return view(request, *args, **kwargs)
        with transaction.atomic():
            scenario = TimetableScenario.objects.select_for_update().filter(pk=scenario_id).first()
            # Preserve the endpoint's existing missing/published response.
            if scenario is not None and scenario.status != "published":
                deny = reject_active_planner_job(scenario_id)
                if deny is not None:
                    return deny
            return view(request, *args, **kwargs)

    return guarded
