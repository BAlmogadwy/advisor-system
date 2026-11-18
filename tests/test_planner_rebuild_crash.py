"""Kill a separate planner process at deterministic database write boundaries."""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from core.models import (
    DeliveryBoard,
    PlannerJob,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.planner_job_runner import (
    cancel_planner_job,
    reconcile_stale_planner_jobs,
    run_planner_job,
    submit_planner_job,
)
from tests.planner_crash_worker import run_worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def require_postgres():
    if connection.vendor != "postgresql":
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PostgreSQL was required")
        pytest.skip("Requires separate PostgreSQL sessions")


@pytest.fixture
def board():
    scenario = TimetableScenario.objects.create(name="Crash test")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Board")
    section = TermSection.objects.create(
        scenario=scenario, course_code="AI1", course_key="AI1", section="S1"
    )
    for model, refs in [
        (SectionPlacement, {"board": board}),
        (TermSectionMeeting, {"instructor": "Saved teacher"}),
    ]:
        model.objects.create(
            term_section=section,
            day="MON",
            start_time="09:00",
            end_time="10:00",
            room="SAVED",
            **refs,
        )
    return board


def _saved_rows(board):
    return (
        list(SectionPlacement.objects.filter(board=board).order_by("pk").values()),
        list(
            TermSectionMeeting.objects.filter(term_section__scenario=board.scenario)
            .order_by("pk")
            .values()
        ),
    )


@contextmanager
def _worker(job_id, phase):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=run_worker, args=(connection.settings_dict, job_id, phase, child))
    proc.start()
    child.close()
    try:
        assert parent.poll(45), f"worker exit code {proc.exitcode}"
        assert parent.recv() == "ready", PlannerJob.objects.get(pk=job_id).error_message
        yield proc, parent
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        parent.close()
        assert not proc.is_alive()


@pytest.mark.parametrize(
    "mode",
    [
        PlannerJob.MODE_FULL_REBUILD,
        PlannerJob.MODE_OPTIMISE_V2_FULL,
        PlannerJob.MODE_SCHEDULER_BUILD,
    ],
)
@pytest.mark.parametrize("phase", ["solve", "commit"])
def test_process_death_preserves_the_committed_board(require_postgres, board, mode, phase):
    before = _saved_rows(board)
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=mode)
    with _worker(job_id, phase):
        assert _saved_rows(board) == before
    assert _saved_rows(board) == before
    PlannerJob.objects.filter(pk=job_id).update(started_at=timezone.now() - timedelta(hours=2))
    assert reconcile_stale_planner_jobs() == 1
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_FAILED
    assert _saved_rows(board) == before

    retry_id = submit_planner_job(scenario_id=board.scenario_id, mode=mode)
    with _worker(retry_id, "solve") as (proc, pipe):
        pipe.send("continue")
        proc.join(30)
        assert proc.exitcode == 0
    assert PlannerJob.objects.get(pk=retry_id).status == PlannerJob.STATUS_SUCCEEDED
    assert SectionPlacement.objects.get(board=board).room == "NEW"
    assert TermSectionMeeting.objects.get(term_section__scenario=board.scenario).room == "NEW"


def test_live_scheduler_cannot_be_swept_and_accepts_cancellation(require_postgres, board):
    before = _saved_rows(board)
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_SCHEDULER_BUILD)
    with _worker(job_id, "solve") as (proc, pipe):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '3s'")
        try:
            PlannerJob.objects.filter(pk=job_id).update(
                started_at=timezone.now() - timedelta(hours=2)
            )
            assert reconcile_stale_planner_jobs() == 0
            assert PlannerJob.objects.get(pk=job_id).last_stage_seen == "persist"
            assert cancel_planner_job(job_id)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET lock_timeout")
        pipe.send("continue")
        proc.join(30)
        assert proc.exitcode == 0
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_CANCELLED
    assert _saved_rows(board) == before


def test_worker_that_lost_ownership_cannot_publish(require_postgres, board):
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_FULL_REBUILD)
    with _worker(job_id, "solve") as (proc, pipe):
        PlannerJob.objects.filter(pk=job_id).update(status=PlannerJob.STATUS_FAILED)
        SectionPlacement.objects.filter(board=board).update(room="Manual change")
        saved = _saved_rows(board)
        pipe.send("continue")
        proc.join(30)
        assert proc.exitcode == 0
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_FAILED
    assert _saved_rows(board) == saved


def test_real_v2_candidate_crash_and_retry_from_workspace(require_postgres, admin_client):
    from core.services.timetable_autoplace import auto_place_scenario
    from tests.test_timetable_seeded_classic import _symmetric_scenario

    scenario = _symmetric_scenario()
    board = DeliveryBoard.objects.get(scenario=scenario)
    ScenarioStudentMap.objects.create(
        scenario=scenario, student_id=1, primary_term=1, recommended_courses=["AI101"]
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario, student_id=1, course_key="AI101", course_code="AI101", primary_term=1
    )
    auto_place_scenario(scenario.pk, strategy="compact")
    placement = SectionPlacement.objects.filter(board=board).earliest("pk")
    SectionPlacement.objects.filter(pk=placement.pk).update(room="Saved room")
    repair = TimetableRepairRun.objects.create(
        scenario=scenario, target_placement=placement, target_section=placement.term_section
    )
    before = _saved_rows(board)
    options = {
        "seed": 0,
        "strategies": ["compact"],
        "run_cpsat_polish": False,
        "run_chain_search": False,
        "max_iterations": 1,
    }
    job_id = submit_planner_job(
        scenario_id=scenario.pk, mode=PlannerJob.MODE_OPTIMISE_V2_FULL, params=options
    )
    with _worker(job_id, "v2_candidate"):
        assert _saved_rows(board) == before
    assert _saved_rows(board) == before
    repair.refresh_from_db()
    assert repair.target_placement_id == placement.pk

    PlannerJob.objects.filter(pk=job_id).update(started_at=timezone.now() - timedelta(hours=2))
    response = admin_client.post(
        f"/ops/tw/scenarios/{scenario.pk}/optimise-v2/",
        {**options, "mode": "full", "async_run": True},
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    retry = PlannerJob.objects.get(pk=response.json()["job_id"])
    assert retry.status == PlannerJob.STATUS_SUCCEEDED, retry.error_message
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_FAILED
    assert SectionPlacement.objects.filter(board=board).exists()
    assert TermSectionMeeting.objects.filter(term_section__scenario=scenario).exists()


def test_unrelated_inserts_survive_successful_publication(board, monkeypatch):
    other = TimetableScenario.objects.create(name="Other draft")
    other_board = DeliveryBoard.objects.create(scenario=other, label="Other")
    section = TermSection.objects.get(scenario=board.scenario)
    other_section = TermSection.objects.create(
        scenario=other, course_code="Q1", course_key="Q1", section="A"
    )

    def external_save():
        try:
            return SectionPlacement.objects.create(
                board=other_board,
                term_section=other_section,
                day="WED",
                start_time="11:00",
                end_time="12:00",
                room="Unrelated",
            ).pk
        finally:
            connections.close_all()

    external_ids = []

    def solve(*args, **kwargs):
        SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day="TUE",
            start_time="12:00",
            end_time="13:00",
            room="New",
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            external_ids.append(pool.submit(external_save).result(timeout=10))
        return {"placed": 1}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", solve)
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_FULL_REBUILD)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_SUCCEEDED, job.error_message
    assert SectionPlacement.objects.get(board=board).room == "New"
    assert SectionPlacement.objects.get(pk=external_ids[0], board=other_board).room == "Unrelated"
