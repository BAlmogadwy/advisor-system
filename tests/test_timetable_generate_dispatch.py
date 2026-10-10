from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import DeliveryBoard, PlannerJob, TimetableScenario
from core.services.planner_job_runner import submit_planner_job as real_submit_planner_job
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups


@pytest.fixture
def generation_client(monkeypatch):
    ensure_role_groups()
    user = User.objects.create_user(username="generation-policy-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"]
    )
    DeliveryBoard.objects.create(scenario=scenario, label="T2", nominal_term=2)
    scaffold = Mock(
        return_value={
            "scenario": {"id": scenario.pk},
            "boards": [],
            "section_budget": [],
            "student_summary": {"classified": 1},
        }
    )
    submit, dispatch = Mock(return_value="test-job"), Mock()
    monkeypatch.setattr("core.timetable_workspace_views.generate_workspace_scenario", scaffold)
    monkeypatch.setattr("core.services.planner_job_runner.submit_planner_job", submit)
    monkeypatch.setattr("core.services.planner_job_runner.dispatch_planner_job", dispatch)
    monkeypatch.setattr("core.services.planner_job_runner.is_async_planner_enabled", lambda: True)
    return http, scenario, scaffold, submit, dispatch


def post(http, **overrides):
    return http.post(
        "/ops/tw/generate-workspace/",
        content_type="application/json",
        data=json.dumps(
            {
                "year": 1448,
                "semester": 2,
                "program": "AI",
                "section": "M",
                "engine": "scheduler",
                "defer_build": True,
                **overrides,
            }
        ),
    )


@pytest.mark.django_db
def test_scheduler_generate_runs_three_attempts_and_forwards_confirmation(generation_client):
    http, _, scaffold, submit, _ = generation_client
    assert post(http).status_code == 201
    params = submit.call_args.kwargs["params"]
    assert submit.call_args.kwargs["mode"] == PlannerJob.MODE_SCHEDULER_BUILD
    assert params["runs"] == 3
    assert params["seconds"] == 120
    assert params["confirmation_seconds"] == 20
    assert scaffold.call_args.kwargs["run_autoplace"] is False


@pytest.mark.django_db
def test_scheduler_total_budget_includes_shared_confirmation(generation_client):
    http, _, _, submit, _ = generation_client
    assert post(http, runs=3, total_seconds=90, confirmation_seconds=15).status_code == 201
    params = submit.call_args.kwargs["params"]
    assert params["seconds"] * params["runs"] + params["confirmation_seconds"] == 90


@pytest.mark.parametrize(
    "tuning",
    [
        {"runs": 0},
        {"runs": 9},
        {"runs": 1.5},
        {"runs": True},
        {"seconds": -1},
        {"seconds": float("inf")},
        {"seconds": True},
        {"confirmation_seconds": 0},
        {"clash_tolerance": -0.1},
        {"clash_tolerance": 1.1},
        {"total_seconds": 10},
        {"engine": "unknown"},
        {"section": ""},
    ],
)
@pytest.mark.django_db
def test_invalid_generation_tuning_does_not_create_a_scaffold(generation_client, tuning):
    http, _, scaffold, submit, _ = generation_client
    assert post(http, **tuning).status_code == 400
    scaffold.assert_not_called()
    submit.assert_not_called()


@pytest.mark.django_db
def test_scheduler_sync_fallback_honours_engine_and_policy(generation_client, monkeypatch):
    http, scenario, scaffold, submit, _ = generation_client
    build = Mock(return_value={"engine": "scheduler", "instructor_days": 2})
    classic = Mock()
    monkeypatch.setattr("scheduler.bridge.build_into_scenario", build)
    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", classic)
    monkeypatch.setattr(
        "core.services.planner_job_runner.submit_planner_job", real_submit_planner_job
    )
    monkeypatch.setattr("core.services.planner_job_runner.is_async_planner_enabled", lambda: False)
    response = post(http)
    assert response.status_code == 201
    assert response.json()["build_summary"]["instructor_days"] == 2
    assert build.call_args.args == (scenario.pk,)
    assert build.call_args.kwargs["runs"] == 3
    assert scaffold.call_args.kwargs["run_autoplace"] is False
    classic.assert_not_called()
    submit.assert_not_called()
    assert PlannerJob.objects.get(scenario=scenario).status == PlannerJob.STATUS_SUCCEEDED


@pytest.mark.django_db
def test_classic_sync_generation_uses_full_guarded_portfolio(generation_client, monkeypatch):
    http, scenario, scaffold, submit, _ = generation_client
    classic = Mock(return_value={"optimisation_policy": "instructor_first"})
    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", classic)
    monkeypatch.setattr(
        "core.services.planner_job_runner.submit_planner_job", real_submit_planner_job
    )
    response = post(http, engine="classic", defer_build=False)
    assert response.status_code == 201
    assert response.json()["build_summary"]["optimisation_policy"] == "instructor_first"
    assert classic.call_args.args == (scenario.pk,)
    assert classic.call_args.kwargs["mode"] == "full"
    assert scaffold.call_args.kwargs["run_autoplace"] is False
    submit.assert_not_called()
    assert PlannerJob.objects.get(scenario=scenario).status == PlannerJob.STATUS_SUCCEEDED
