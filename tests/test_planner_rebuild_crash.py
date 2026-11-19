"""Kill a separate planner process at deterministic database write boundaries."""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from time import sleep

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
    TimetableRepairGlobalPlan,
    TimetableRepairGlobalPlanItem,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.planner_job_runner import (
    _REBUILD_LEASE,
    _worker_lease,
    cancel_planner_job,
    reconcile_stale_planner_jobs,
    run_planner_job,
    submit_planner_job,
)
from core.services.timetable_mutation_guard import ScenarioPlannerBusy
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
    assert reconcile_stale_planner_jobs() == 1
    assert reconcile_stale_planner_jobs() == 0
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


def test_live_scheduler_cannot_be_swept_and_accepts_cancellation(
    require_postgres, board, admin_client, settings
):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    before = _saved_rows(board)
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_SCHEDULER_BUILD)
    with _worker(job_id, "solve") as (proc, pipe):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '3s'")
        try:
            assert reconcile_stale_planner_jobs() == 0
            PlannerJob.objects.filter(pk=job_id).update(
                started_at=timezone.now() - timedelta(hours=2)
            )
            assert reconcile_stale_planner_jobs() == 0
            busy = admin_client.post(
                "/planner-jobs/",
                {"scenario_id": board.scenario_id, "mode": "full_rebuild"},
                content_type="application/json",
            )
            assert busy.status_code == 409
            assert busy.json()["error"]["details"]["active_job"]["job_id"] == str(job_id)
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


def test_slow_worker_lease_survives_database_timeouts(require_postgres, board, monkeypatch):
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_FULL_REBUILD)
    # Apply server defaults to new connections only. The observer is already connected.
    options = connection.settings_dict["OPTIONS"].copy()
    timeout_options = " -c idle_in_transaction_session_timeout=200ms"
    if connection.pg_version >= 170000:
        timeout_options += " -c transaction_timeout=300ms"
    options["options"] = options.get("options", "") + timeout_options
    monkeypatch.setitem(connection.settings_dict, "OPTIONS", options)
    with _worker_lease(job_id) as acquired:
        assert acquired
        PlannerJob.objects.filter(pk=job_id).update(
            status=PlannerJob.STATUS_RUNNING,
            started_at=timezone.now(),
            params={_REBUILD_LEASE: True},
        )
        sleep(0.6)
        assert reconcile_stale_planner_jobs() == 0
    assert reconcile_stale_planner_jobs() == 1


@pytest.mark.parametrize(
    "mode",
    [
        PlannerJob.MODE_FULL_REBUILD,
        PlannerJob.MODE_OPTIMISE_CURRENT,
        PlannerJob.MODE_OPTIMISE_V2_CURRENT,
    ],
)
def test_in_place_and_older_workers_keep_the_grace_period(require_postgres, board, mode):
    job_id = submit_planner_job(
        scenario_id=board.scenario_id, mode=mode, params={_REBUILD_LEASE: True}
    )
    job = PlannerJob.objects.get(pk=job_id)
    assert _REBUILD_LEASE not in job.params
    # An older rebuild worker commits RUNNING without the new claim marker.
    # In-place modes need the grace period even if a marker exists on the row.
    if mode != PlannerJob.MODE_FULL_REBUILD:
        job.params[_REBUILD_LEASE] = True
    job.status = PlannerJob.STATUS_RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["params", "status", "started_at"])
    assert reconcile_stale_planner_jobs() == 0
    with pytest.raises(ScenarioPlannerBusy):
        submit_planner_job(scenario_id=board.scenario_id, mode=mode)
    PlannerJob.objects.filter(pk=job_id).update(started_at=timezone.now() - timedelta(hours=2))
    assert reconcile_stale_planner_jobs() == 1


def test_simultaneous_retries_after_crash_queue_one_replacement(require_postgres, board):
    job_id = submit_planner_job(scenario_id=board.scenario_id, mode=PlannerJob.MODE_FULL_REBUILD)
    with _worker(job_id, "solve"):
        pass
    start = Barrier(2)

    def retry():
        try:
            start.wait(timeout=10)
            new_job = submit_planner_job(
                scenario_id=board.scenario_id, mode=PlannerJob.MODE_FULL_REBUILD
            )
            return "queued", new_job
        except ScenarioPlannerBusy as exc:
            return "busy", exc.job.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(retry) for _ in range(2)]
        outcomes = dict(attempt.result(timeout=15) for attempt in attempts)
    replacement = PlannerJob.objects.get(
        scenario_id=board.scenario_id, status=PlannerJob.STATUS_QUEUED
    )
    assert outcomes["queued"] == replacement.pk
    # A racing request can see the prior job until reconciliation commits.
    assert outcomes["busy"] in (job_id, replacement.pk)
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_FAILED


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


@pytest.mark.parametrize("new_analysis", [False, True], ids=["existing-approval", "new-analysis"])
def test_rebuild_preserves_concurrent_repair_records(board, monkeypatch, new_analysis):
    placement = SectionPlacement.objects.get(board=board)
    section_id = placement.term_section_id
    scenario_id = board.scenario_id
    run = TimetableRepairRun.objects.create(
        scenario_id=scenario_id, target_section_id=section_id, target_placement=placement
    )
    plan = TimetableRepairGlobalPlan.objects.create(scenario_id=scenario_id)
    candidate = run.candidates.create(
        candidate_id="original", day="MON", start_time="09:00", end_time="10:00"
    )
    item = plan.items.create(sequence=1, repair_run=run, candidate=candidate, placement=placement)

    def record_repair():
        try:
            if new_analysis:
                analysis = TimetableRepairRun.objects.create(
                    scenario_id=scenario_id,
                    target_section_id=section_id,
                    target_placement_id=placement.pk,
                    summary_json={"analysis": "new"},
                )
                new_candidate = analysis.candidates.create(
                    candidate_id="new", day="TUE", start_time="09:00", end_time="10:00"
                )
                plan.items.create(
                    sequence=2,
                    repair_run=analysis,
                    candidate=new_candidate,
                    placement_id=placement.pk,
                )
                return analysis.pk
            TimetableRepairRun.objects.filter(pk=run.pk).update(
                summary_json={"analysis": "completed"}
            )
            plan.items.filter(pk=item.pk).update(
                status=TimetableRepairGlobalPlanItem.STATUS_APPROVED
            )
            return run.pk
        finally:
            connections.close_all()

    def solve(*args, **kwargs):
        if new_analysis:
            TermSection.objects.filter(pk=section_id).delete()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return {"analysis": str(pool.submit(record_repair).result(timeout=10))}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", solve)
    job_id = submit_planner_job(scenario_id=scenario_id, mode=PlannerJob.MODE_FULL_REBUILD)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_SUCCEEDED, job.error_message
    audit = TimetableRepairRun.objects.get(pk=job.result_json["analysis"])
    assert audit.target_placement_id is None
    assert audit.target_section_id == (None if new_analysis else section_id)
    assert audit.summary_json == {"analysis": "new" if new_analysis else "completed"}
    assert not plan.items.filter(placement_id__isnull=False).exists()
    if new_analysis:
        assert plan.items.filter(sequence=2, repair_run=audit).exists()
    else:
        item.refresh_from_db()
        assert item.status == TimetableRepairGlobalPlanItem.STATUS_APPROVED
