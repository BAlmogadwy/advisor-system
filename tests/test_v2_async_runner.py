"""WS-E — the V2 optimiser runs through the async planner job runner.

The slow V2 pipeline is dispatched off the request thread via ``PlannerJob`` so
it is no longer SIGKILLed by gunicorn's 120s timeout mid-run. Here we assert the
two new modes route to the shared, safety-gated runner with the right mode (the
runner itself is mocked — its gate is exercised by the V2 view tests)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import DeliveryBoard, PlannerJob, TimetableScenario
from core.services.planner_job_runner import run_planner_job, submit_planner_job
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups


def _scenario():
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="v2 job")
    DeliveryBoard.objects.create(scenario=scenario, label="T1", nominal_term=1)
    return scenario


@pytest.mark.django_db
def test_planner_job_v2_current_dispatches_to_guarded_runner() -> None:
    scenario = _scenario()
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_V2_CURRENT)
    with patch(
        "core.services.timetable_v2_runner.run_v2_optimisation_guarded",
        return_value={"safety_blocked": False},
    ) as mocked:
        run_planner_job(job_id)

    mocked.assert_called_once()
    assert mocked.call_args.args[0] == scenario.id
    assert mocked.call_args.kwargs.get("mode") == "current"

    job = PlannerJob.objects.get(id=job_id)
    assert job.status == PlannerJob.STATUS_SUCCEEDED
    assert job.result_json == {"safety_blocked": False}


@pytest.mark.django_db
def test_planner_job_v2_full_dispatches_with_full_mode() -> None:
    scenario = _scenario()
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_V2_FULL)
    with patch(
        "core.services.timetable_v2_runner.run_v2_optimisation_guarded",
        return_value={"safety_blocked": False},
    ) as mocked:
        run_planner_job(job_id)

    mocked.assert_called_once()
    assert mocked.call_args.kwargs.get("mode") == "full"
    assert PlannerJob.objects.get(id=job_id).status == PlannerJob.STATUS_SUCCEEDED


@pytest.mark.django_db
def test_planner_job_v2_replays_request_params() -> None:
    """The async V2 job replays the per-request optimiser params rather than
    silently falling back to defaults (the whole point of PlannerJob.params)."""
    scenario = _scenario()
    job_id = submit_planner_job(
        scenario_id=scenario.id,
        mode=PlannerJob.MODE_OPTIMISE_V2_FULL,
        params={
            "strategies": ["compact"],
            "max_iterations": 7,
            "run_chain_search": False,
            "run_cpsat_polish": False,
            "cpsat_time_limit": 3,
            "max_chain_iterations": 2,
        },
    )
    with patch(
        "core.services.timetable_v2_runner.run_v2_optimisation_guarded",
        return_value={"safety_blocked": False},
    ) as mocked:
        run_planner_job(job_id)

    kw = mocked.call_args.kwargs
    assert kw["mode"] == "full"
    assert kw["strategies"] == ["compact"]
    assert kw["max_iterations"] == 7
    assert kw["run_chain"] is False
    assert kw["run_cpsat"] is False
    assert kw["cpsat_limit"] == 3.0
    assert kw["max_chain_iterations"] == 2


@pytest.mark.django_db
def test_optimise_v2_endpoint_async_run_queues_job() -> None:
    """The optimise endpoint with ``async_run`` returns 202 + a job id and the
    slow pipeline runs off the request thread (sync default is unchanged)."""
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="tw-v2-ep")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="v2 ep")
    DeliveryBoard.objects.create(scenario=scenario, label="T1", nominal_term=1)

    with patch(
        "core.services.timetable_v2_runner.run_v2_optimisation_guarded",
        return_value={"safety_blocked": False},
    ):
        resp = http.post(
            f"/ops/tw/scenarios/{scenario.id}/optimise-v2/",
            data=json.dumps(
                {
                    "mode": "full",
                    "async_run": True,
                    "strategies": ["compact", "balanced"],
                    "cpsat_time_limit": 9,
                }
            ),
            content_type="application/json",
        )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["async"] is True
    assert payload["mode"] == "full"
    job = PlannerJob.objects.get(id=payload["job_id"])
    assert job.mode == PlannerJob.MODE_OPTIMISE_V2_FULL
    assert job.status == PlannerJob.STATUS_SUCCEEDED
    # The view threaded the per-request params onto the job.
    assert job.params["strategies"] == ["compact", "balanced"]
    assert job.params["cpsat_time_limit"] == 9
