"""Persisted run seeds across HTTP admission, dispatch and worker cleanup."""

import json
import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import DeliveryBoard, PlannerJob, TimetableScenario
from core.services.planner_job_runner import (
    cancel_planner_job,
    run_planner_job,
    serialize_planner_job,
    submit_planner_job,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.timetable_run_seed import MAX_RUN_SEED, current_run_seed, seeded_timetable_run

pytestmark = pytest.mark.django_db
MODES = [
    PlannerJob.MODE_OPTIMISE_CURRENT,
    PlannerJob.MODE_FULL_REBUILD,
    PlannerJob.MODE_OPTIMISE_V2_CURRENT,
    PlannerJob.MODE_OPTIMISE_V2_FULL,
    PlannerJob.MODE_SCHEDULER_BUILD,
]
BAD_SEEDS = [True, False, -1, MAX_RUN_SEED + 1, 1.0, 1.25, "42", [], {}]


@pytest.fixture
def http(settings):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    user = User.objects.create_user(username="seed-boundary-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def scenario():
    scenario = TimetableScenario.objects.create(
        name="Seed boundary", academic_year="1448", term="2"
    )
    DeliveryBoard.objects.create(scenario=scenario, label="Board", program="AI")
    return scenario


def post(http, path, body):
    return http.post(path, json.dumps(body), content_type="application/json")


def patch_engines(monkeypatch, pipeline):
    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    monkeypatch.setattr("scheduler.bridge.build_into_scenario", pipeline)


def test_new_jobs_draw_once_each_and_execution_never_redraws(scenario, monkeypatch):
    entropy = Mock(side_effect=[12001, 12002])
    monkeypatch.setattr("core.services.timetable_run_seed.secrets.randbelow", entropy)
    seeds = []
    patch_engines(monkeypatch, lambda *args, **kwargs: {"witness": current_run_seed()})
    for _ in range(2):
        job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        before = PlannerJob.objects.get(pk=job_id).params["seed"]
        run_planner_job(job_id)
        run_planner_job(job_id)  # A duplicate worker entry does not redraw or execute again.
        job = PlannerJob.objects.get(pk=job_id)
        assert job.params["seed"] == before == job.result_json["seed"] == job.result_json["witness"]
        seeds.append(before)
    assert seeds == [12001, 12002]
    assert entropy.call_count == 2
    assert current_run_seed() is None


@pytest.mark.parametrize("endpoint", ["generate", "current", "full", "generic"])
@pytest.mark.parametrize("null_seed", [False, True])
def test_repeated_missing_or_null_http_seeds_draw_once_per_accepted_click(
    http, scenario, monkeypatch, endpoint, null_seed
):
    entropy = Mock(side_effect=[12011, 12012])
    monkeypatch.setattr("core.services.timetable_run_seed.secrets.randbelow", entropy)
    patch_engines(monkeypatch, lambda *args, **kwargs: {"witness": current_run_seed()})
    monkeypatch.setattr(
        "core.timetable_workspace_views.generate_workspace_scenario",
        lambda **kwargs: {
            "scenario": {"id": scenario.id},
            "boards": [],
            "section_budget": [],
            "student_summary": {"classified": 1},
        },
    )
    if endpoint == "generate":
        path, body = (
            "/ops/tw/generate-workspace/",
            {"year": 1448, "semester": 2, "program": "AI", "defer_build": True},
        )
    elif endpoint == "generic":
        path, body = "/planner-jobs/", {"scenario_id": scenario.id, "mode": "full_rebuild"}
    else:
        path, body = (
            f"/ops/tw/scenarios/{scenario.id}/optimise-v2/",
            {"mode": endpoint, "async_run": True},
        )
    if null_seed:
        body["seed"] = None
    observed = []
    for _ in range(2):
        response = post(http, path, body)
        assert response.status_code in (201, 202), response.content
        observed.append(response.json()["seed"])
    assert observed == [12011, 12012]
    assert entropy.call_count == 2
    assert set(PlannerJob.objects.values_list("params__seed", flat=True)) == set(observed)


@pytest.mark.parametrize("seed", [0, MAX_RUN_SEED])
def test_explicit_replay_seed_is_retained_without_entropy(scenario, monkeypatch, seed):
    entropy = Mock(side_effect=AssertionError("Explicit replay must not draw a seed"))
    monkeypatch.setattr("core.services.timetable_run_seed.secrets.randbelow", entropy)
    patch_engines(monkeypatch, lambda *args, **kwargs: {"witness": current_run_seed()})
    job_id = submit_planner_job(
        scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT, params={"seed": seed}
    )
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.result_json["seed"] == job.result_json["witness"] == seed
    assert serialize_planner_job(job)["seed"] == seed
    entropy.assert_not_called()


@pytest.mark.parametrize("seed", BAD_SEEDS)
def test_invalid_service_seed_cannot_even_reconcile_stale_jobs(scenario, monkeypatch, seed):
    stale = PlannerJob.objects.create(
        id=uuid.uuid4(), scenario=scenario, mode=PlannerJob.MODE_OPTIMISE_CURRENT
    )
    PlannerJob.objects.filter(pk=stale.pk).update(
        submitted_at=timezone.now() - timedelta(minutes=90)
    )
    reconcile = Mock(side_effect=AssertionError("Invalid seeds must fail before stale-job writes"))
    monkeypatch.setattr("core.services.planner_job_runner.reconcile_stale_planner_jobs", reconcile)
    with CaptureQueriesContext(connection) as queries, pytest.raises(ValueError):
        submit_planner_job(
            scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT, params={"seed": seed}
        )
    assert not any(
        query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in queries
    )
    stale.refresh_from_db()
    assert stale.status == PlannerJob.STATUS_QUEUED
    reconcile.assert_not_called()


@pytest.mark.parametrize("endpoint", ["generate", "optimise", "generic"])
@pytest.mark.parametrize("seed", BAD_SEEDS)
def test_invalid_http_seed_is_rejected_before_scaffold_or_job_writes(
    http, scenario, monkeypatch, endpoint, seed
):
    scaffold = Mock(side_effect=AssertionError("Invalid seed reached Generate"))
    monkeypatch.setattr("core.timetable_workspace_views.generate_workspace_scenario", scaffold)
    if endpoint == "generate":
        path, body = (
            "/ops/tw/generate-workspace/",
            {"year": 1448, "semester": 2, "program": "AI", "engine": "classic"},
        )
    elif endpoint == "optimise":
        path, body = (
            f"/ops/tw/scenarios/{scenario.id}/optimise-v2/",
            {"mode": "full", "async_run": True},
        )
    else:
        path, body = "/planner-jobs/", {"scenario_id": scenario.id, "mode": "full_rebuild"}
    with CaptureQueriesContext(connection) as queries:
        response = post(http, path, {**body, "seed": seed})
    assert response.status_code == 400, response.content
    assert not PlannerJob.objects.exists()
    scaffold.assert_not_called()
    assert not any(
        query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in queries
    )


@pytest.mark.parametrize("mode", ["current", "full"])
@pytest.mark.parametrize("async_enabled", [False, True])
def test_sync_and_async_current_full_preserve_zero_seed(
    http, scenario, settings, monkeypatch, mode, async_enabled
):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = async_enabled
    patch_engines(
        monkeypatch,
        lambda *args, **kwargs: {"mode": kwargs.get("mode"), "witness": current_run_seed()},
    )
    with seeded_timetable_run(99):
        response = post(
            http,
            f"/ops/tw/scenarios/{scenario.id}/optimise-v2/",
            {"mode": mode, "async_run": True, "seed": 0},
        )
        assert current_run_seed() == 99
    assert response.status_code == (202 if async_enabled else 200), response.content
    job = PlannerJob.objects.get(scenario=scenario)
    assert job.params["seed"] == job.result_json["seed"] == job.result_json["witness"] == 0
    if async_enabled:
        assert response.json()["seed"] == 0
        assert http.get(f"/planner-jobs/{job.id}/").json()["seed"] == 0
        result = http.get(f"/planner-jobs/{job.id}/result/").json()
        assert result["seed"] == result["result"]["seed"] == 0
    else:
        assert response.json()["optimisation"]["seed"] == 0
    assert current_run_seed() is None


@pytest.mark.parametrize("engine", ["classic", "scheduler"])
@pytest.mark.parametrize("async_enabled", [False, True])
@pytest.mark.parametrize("supplied", [0, None])
def test_generate_both_engines_use_one_persisted_seed(
    http, scenario, settings, monkeypatch, engine, async_enabled, supplied
):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = async_enabled
    entropy = Mock(return_value=14001)
    monkeypatch.setattr("core.services.timetable_run_seed.secrets.randbelow", entropy)
    monkeypatch.setattr(
        "core.timetable_workspace_views.generate_workspace_scenario",
        lambda **kwargs: {
            "scenario": {"id": scenario.id},
            "boards": [],
            "section_budget": [],
            "student_summary": {"classified": 1},
        },
    )
    seen = []

    def pipeline(*args, **kwargs):
        seen.append(current_run_seed())
        if engine == "scheduler":
            assert kwargs["seed"] == current_run_seed()
        return {"engine": engine, "witness": current_run_seed()}

    patch_engines(monkeypatch, pipeline)
    response = post(
        http,
        "/ops/tw/generate-workspace/",
        {
            "year": 1448,
            "semester": 2,
            "program": "AI",
            "section": "M",
            "engine": engine,
            "defer_build": True,
            "seed": supplied,
        },
    )
    assert response.status_code == 201, response.content
    expected = 0 if supplied == 0 else 14001
    job = PlannerJob.objects.get(scenario=scenario)
    assert seen == [expected]
    assert response.json()["seed"] == job.params["seed"] == job.result_json["seed"] == expected
    assert entropy.call_count == (1 if supplied is None else 0)
    assert current_run_seed() is None


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("outcome", ["success", "error_result", "exception", "cancel"])
def test_all_worker_paths_bind_and_restore_context(scenario, monkeypatch, mode, outcome):
    job_id = submit_planner_job(scenario_id=scenario.id, mode=mode, params={"seed": 314})

    def pipeline(*args, **kwargs):
        assert current_run_seed() == 314
        if outcome == "exception":
            raise RuntimeError("seeded solver failure")
        if outcome == "cancel":
            assert cancel_planner_job(job_id)
        return (
            {"error": "seeded error"}
            if outcome == "error_result"
            else {"witness": current_run_seed()}
        )

    patch_engines(monkeypatch, pipeline)
    with seeded_timetable_run(99):
        run_planner_job(job_id)
        assert current_run_seed() == 99
    job = PlannerJob.objects.get(pk=job_id)
    expected = {
        "success": "succeeded",
        "error_result": "failed",
        "exception": "failed",
        "cancel": "cancelled",
    }[outcome]
    assert job.status == expected
    assert serialize_planner_job(job)["seed"] == 314
    if isinstance(job.result_json, dict):
        assert job.result_json["seed"] == 314
    assert current_run_seed() is None


def test_legacy_job_without_seed_keeps_unseeded_behavior_and_does_not_draw(scenario, monkeypatch):
    entropy = Mock(side_effect=AssertionError("Legacy worker may not invent a new seed"))
    monkeypatch.setattr("core.services.timetable_run_seed.secrets.randbelow", entropy)

    def pipeline(*args, **kwargs):
        assert current_run_seed() is None
        return {"legacy": True}

    patch_engines(monkeypatch, pipeline)
    job = PlannerJob.objects.create(
        id=uuid.uuid4(), scenario=scenario, mode=PlannerJob.MODE_OPTIMISE_CURRENT, params={}
    )
    with seeded_timetable_run(99):
        run_planner_job(job.id)
        assert current_run_seed() == 99
    job.refresh_from_db()
    assert job.params == {}
    assert job.result_json == {"legacy": True, "seed": None}
    entropy.assert_not_called()


def test_cancel_before_start_and_restore_failure_both_reset_seed_context(scenario, monkeypatch):
    job_id = submit_planner_job(
        scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT, params={"seed": 315}
    )
    cancel_planner_job(job_id)
    with seeded_timetable_run(99):
        run_planner_job(job_id)
        assert current_run_seed() == 99

    def pipeline(*args, **kwargs):
        cancel_planner_job(second)
        return {}

    def failed_restore(snapshot):
        assert current_run_seed() == 316
        raise RuntimeError("restore failed")

    patch_engines(monkeypatch, pipeline)
    monkeypatch.setattr("core.services.planner_job_snapshot.restore_planner_job", failed_restore)
    second = submit_planner_job(
        scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT, params={"seed": 316}
    )
    with seeded_timetable_run(99):
        run_planner_job(second)
        assert current_run_seed() == 99
    assert PlannerJob.objects.get(pk=second).status == "failed"
    assert current_run_seed() is None


def test_generic_job_submit_replays_zero_and_failure_poll_retains_seed(http, scenario, monkeypatch):
    def pipeline(*args, **kwargs):
        assert current_run_seed() == 0
        raise RuntimeError("solver failed")

    patch_engines(monkeypatch, pipeline)
    response = post(
        http, "/planner-jobs/", {"scenario_id": scenario.id, "mode": "optimise_current", "seed": 0}
    )
    assert response.status_code == 201 and response.json()["seed"] == 0
    job_id = response.json()["job_id"]
    poll = http.get(f"/planner-jobs/{job_id}/").json()
    assert poll["status"] == "failed" and poll["seed"] == 0
    result = http.get(f"/planner-jobs/{job_id}/result/")
    assert result.status_code == 404 and result.json()["seed"] == 0


def test_sync_error_response_retains_zero_seed(http, scenario, settings, monkeypatch):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = False
    patch_engines(monkeypatch, lambda *args, **kwargs: {"error": "No student profiles"})
    response = post(http, f"/ops/tw/scenarios/{scenario.id}/optimise-v2/", {"seed": 0})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["seed"] == 0
