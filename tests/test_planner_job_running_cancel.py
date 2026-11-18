from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import (
    BoardSectionVisibility,
    DeliveryBoard,
    PlannerJob,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableRepairCandidate,
    TimetableRepairGlobalPlan,
    TimetableRepairGlobalPlanItem,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.planner_job_runner import (
    cancel_planner_job,
    reconcile_stale_planner_jobs,
    run_planner_job,
    submit_planner_job,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def baseline():
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Cancellation baseline",
        slot_config=[{"start": "09:00", "end": "10:15"}],
        lab_slot_config=[{"start": "11:00", "end": "12:40"}],
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="AI", program="AI")
    for index in range(2):
        section = TermSection.objects.create(
            scenario=scenario,
            source_tag="scheduler",
            course_key=f"AI10{index}",
            course_code=f"AI10{index}",
            course_number=f"AI10{index}",
            section="S1",
            course_name="Original",
            available_capacity=30,
            registered_count=12,
            created_at="original section created",
            updated_at="original section updated",
        )
        placement = SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="OLD",
            is_locked=index == 0,
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="OLD",
            building="Original building",
            floor_wing="Original floor",
            instructor="Original Teacher",
            created_at="created",
            updated_at="updated",
        )
        StudentTermSection.objects.create(
            student_id=1000 + index,
            academic_year="1448",
            term="1",
            term_section=section,
            source="scraper_timetable",
            created_at="original student created",
        )
        TermSectionProgram.objects.create(
            term_section=section, program="AI", assignment_source="manual"
        )
        BoardSectionVisibility.objects.create(board=board, term_section=section)
    run = TimetableRepairRun.objects.create(
        scenario=scenario,
        target_section=section,
        target_placement=placement,
    )
    candidate = TimetableRepairCandidate.objects.create(
        run=run,
        candidate_id="original",
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    plan = TimetableRepairGlobalPlan.objects.create(scenario=scenario, request_signature="original")
    TimetableRepairGlobalPlanItem.objects.create(
        plan=plan,
        sequence=1,
        repair_run=run,
        candidate=candidate,
        placement=placement,
    )
    # A second board owned by the same scenario keeps physical copies and
    # repair-plan references in the valid rebuild scope. Cancellation must
    # restore them after section cascades as well as the primary board.
    shared_scenario = scenario
    shared_board = DeliveryBoard.objects.create(
        scenario=shared_scenario, label="Shared owned board"
    )
    shared_placement = SectionPlacement.objects.create(
        board=shared_board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="OLD",
        is_locked=True,
    )
    BoardSectionVisibility.objects.create(board=shared_board, term_section=section)
    shared_run = TimetableRepairRun.objects.create(
        scenario=shared_scenario,
        target_section=section,
        target_placement=shared_placement,
    )
    shared_plan = TimetableRepairGlobalPlan.objects.create(
        scenario=shared_scenario, request_signature="owned-shared"
    )
    TimetableRepairGlobalPlanItem.objects.create(
        plan=shared_plan,
        sequence=1,
        repair_run=shared_run,
        candidate=candidate,
        placement=shared_placement,
    )
    # Imported global sections and registrar assignments must survive every
    # cancellation exactly; the scenario rollback has no ownership over them.
    global_section = TermSection.objects.create(
        course_key="GLOBAL",
        course_code="GLOBAL",
        course_number="GLOBAL",
        section="S1",
    )
    StudentTermSection.objects.create(
        student_id=9999,
        academic_year="1448",
        term="1",
        term_section=global_section,
        source="scraper_timetable",
    )
    TermSectionMeeting.objects.create(
        term_section=global_section,
        day="THU",
        start_time="17:00",
        end_time="18:15",
        room="GLOBAL",
    )
    return scenario, board


def rows():
    return {
        model.__name__: list(model.objects.order_by("pk").values())
        for model in (
            TimetableScenario,
            TermSection,
            SectionPlacement,
            TermSectionMeeting,
            StudentTermSection,
            TermSectionProgram,
            BoardSectionVisibility,
            TimetableRepairRun,
            TimetableRepairGlobalPlanItem,
        )
    }


@pytest.mark.parametrize(
    "mode",
    [
        PlannerJob.MODE_OPTIMISE_V2_CURRENT,
        PlannerJob.MODE_OPTIMISE_V2_FULL,
        PlannerJob.MODE_SCHEDULER_BUILD,
        PlannerJob.MODE_OPTIMISE_CURRENT,
        PlannerJob.MODE_FULL_REBUILD,
    ],
)
@pytest.mark.parametrize(
    ("cancel", "outcome"),
    [
        (True, "success"),
        (True, "error_result"),
        (True, "exception"),
        (False, "error_result"),
        (False, "exception"),
    ],
)
def test_cancel_or_failure_restores_whole_scenario_for_every_engine(
    baseline, monkeypatch, mode, cancel, outcome
):
    scenario, board = baseline
    before = rows()
    job_id = submit_planner_job(scenario_id=scenario.id, mode=mode)

    def pipeline(*args, **kwargs):
        # Simulate scheduler-owned section replacement, including FK cascades,
        # or a classic rebuild's persisted intermediate output.
        TermSection.objects.filter(scenario=scenario).delete()
        fresh = TermSection.objects.create(
            scenario=scenario,
            course_key="NEW",
            course_code="NEW",
            course_number="NEW",
            section="S1",
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=fresh,
            day="MON",
            start_time="13:00",
            end_time="14:15",
            room="NEW",
        )
        TermSectionMeeting.objects.create(
            term_section=fresh,
            day="MON",
            start_time="13:00",
            end_time="14:15",
            instructor="New Teacher",
        )
        StudentTermSection.objects.create(
            student_id=1000, academic_year="1448", term="1", term_section=fresh
        )
        TermSectionProgram.objects.create(term_section=fresh, program="DS")
        BoardSectionVisibility.objects.create(board=board, term_section=fresh)
        TimetableScenario.objects.filter(pk=scenario.id).update(slot_config=[], lab_slot_config=[])
        if cancel:
            assert cancel_planner_job(job_id)
        if outcome == "exception":
            raise RuntimeError("solver fault after writing")
        if outcome == "error_result":
            return {"error": "solver fault after writing", "stage": "persist"}
        return {"placements": 1}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    monkeypatch.setattr("scheduler.bridge.build_into_scenario", pipeline)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.finished_at is not None
    assert job.cancel_requested == cancel
    if cancel:
        assert job.status == PlannerJob.STATUS_CANCELLED
        assert job.result_json is None and job.error_message is None
    else:
        assert job.status == PlannerJob.STATUS_FAILED
        assert "solver fault after writing" in job.error_message
        if outcome == "error_result":
            assert job.result_json["stage"] == "persist"
            assert job.result_json["seed"] == job.params["seed"]
        else:
            assert job.result_json is None
    assert rows() == before
    assert not cancel_planner_job(job_id)


@pytest.mark.django_db(transaction=True)
def test_failed_full_rebuild_restores_saved_timetable_then_real_retry_succeeds(
    admin_client, monkeypatch, settings
):
    from tests.one_teacher_support import place, section
    from tests.pr7_fixture_loader import load_pr7_fixture

    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    settings.TIMETABLE_ENFORCE_LOCKS = True
    scenario, board, _ = load_pr7_fixture("pr7_async_happy_path.json")
    locked = section(scenario, "CS101", "S1")
    unlocked = section(scenario, "CS102", "S1")
    place(board, locked, [("SUN", "08:00", "09:15")], locked=True, room="A101")
    place(board, unlocked, [("MON", "08:00", "09:15")], room="A102", instructor_name="Dr Saved")
    before = rows()
    request = {"scenario_id": scenario.id, "mode": PlannerJob.MODE_FULL_REBUILD, "seed": 17}

    def fail_after_reset(*args, **kwargs):
        assert not SectionPlacement.objects.filter(term_section=unlocked).exists()
        assert not TermSectionMeeting.objects.filter(term_section=unlocked).exists()
        assert SectionPlacement.objects.filter(term_section=locked, is_locked=True).exists()
        raise RuntimeError("solver failed after clearing the draft")

    with monkeypatch.context() as patch:
        patch.setattr("core.services.timetable_autoplace.auto_place_scenario", fail_after_reset)
        first = admin_client.post("/planner-jobs/", request, content_type="application/json")
    assert first.status_code == 201
    failed_id = first.json()["job_id"]
    failed = admin_client.get(f"/planner-jobs/{failed_id}/").json()
    assert failed["status"] == PlannerJob.STATUS_FAILED
    assert "solver failed after clearing the draft" in failed["error_message"]
    assert rows() == before

    retry = admin_client.post("/planner-jobs/", request, content_type="application/json")
    assert retry.status_code == 201
    retry_id = retry.json()["job_id"]
    assert retry_id != failed_id
    assert (
        admin_client.get(f"/planner-jobs/{retry_id}/").json()["status"]
        == PlannerJob.STATUS_SUCCEEDED
    )
    assert admin_client.get(f"/planner-jobs/{retry_id}/result/").status_code == 200
    assert SectionPlacement.objects.filter(term_section=locked, is_locked=True).exists()
    assert SectionPlacement.objects.filter(board=board, term_section__course_code="CS102").exists()
    assert TermSectionMeeting.objects.filter(
        term_section__scenario=scenario, term_section__course_code="CS102"
    ).exists()


def test_scheduler_progress_boundary_stops_and_restores_changed_grid(baseline, monkeypatch):
    scenario, _ = baseline
    before = rows()
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_SCHEDULER_BUILD)

    def pipeline(*args, progress, **kwargs):
        progress("snapshot")
        TimetableScenario.objects.filter(pk=scenario.id).update(slot_config=[])
        assert cancel_planner_job(job_id)
        progress("persist")
        raise AssertionError("cancelled scheduler must not proceed past its progress boundary")

    monkeypatch.setattr("scheduler.bridge.build_into_scenario", pipeline)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "cancelled" and job.last_stage_seen == "snapshot"
    assert rows() == before


class _UnprintableDiagnostic:
    def __str__(self):
        raise ValueError("Cannot render engine diagnostic")


@pytest.mark.parametrize(
    "result",
    [
        {"error": "persist failed", "details": {1}},
        {"stage_telemetry": {"stage_ms": "invalid"}},
        {"error": _UnprintableDiagnostic()},
    ],
    ids=["unserializable-error", "malformed-success", "unprintable-error"],
)
def test_failed_finalisation_restores_draft(baseline, monkeypatch, result):
    scenario, board = baseline
    before = rows()
    job_id = submit_planner_job(scenario_id=scenario.pk, mode=PlannerJob.MODE_FULL_REBUILD)

    def pipeline(*args, **kwargs):
        SectionPlacement.objects.filter(board=board).update(room="PARTIAL")
        return result

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_FAILED
    assert job.result_json is None
    assert "finalisation failed" in job.error_message
    if isinstance(result.get("error"), str):
        assert result["error"] in job.error_message
    if isinstance(result.get("error"), _UnprintableDiagnostic):
        assert "Unprintable" in job.error_message
    assert rows() == before


@pytest.mark.parametrize("raises", [False, True])
def test_unencodable_solver_diagnostic_cannot_prevent_rollback(baseline, monkeypatch, raises):
    scenario, board = baseline
    before = rows()
    job_id = submit_planner_job(scenario_id=scenario.pk, mode=PlannerJob.MODE_FULL_REBUILD)

    def pipeline(*args, **kwargs):
        SectionPlacement.objects.filter(board=board).update(room="PARTIAL")
        message = "solver diagnostic: \ud800\x00"
        if raises:
            raise RuntimeError(message)
        return {"error": message}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_FAILED
    assert "solver diagnostic" in job.error_message
    assert "\x00" not in job.error_message
    assert job.error_message.encode("utf-8")
    assert rows() == before


def test_restore_failure_never_claims_cancelled_or_succeeded(baseline, monkeypatch):
    scenario, _ = baseline
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def pipeline(*args, **kwargs):
        assert cancel_planner_job(job_id)
        return {"placements": 1}

    def failing_restore(snapshot):
        SectionPlacement.objects.filter(board__scenario_id=scenario.id).delete()
        raise RuntimeError("restore failed")

    before = rows()
    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    monkeypatch.setattr("core.services.planner_job_snapshot.restore_planner_job", failing_restore)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "failed" and "restore failed" in job.error_message
    assert job.result_json is None
    assert rows() == before  # failed restoration was rolled back atomically


def test_terminal_success_rejects_late_cancel(baseline, monkeypatch):
    scenario, _ = baseline
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", lambda *a, **k: {})
    run_planner_job(job_id)
    assert not cancel_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "succeeded" and not job.cancel_requested


def test_executing_job_is_not_reconciled_or_released_past_orphan_timeout(baseline, monkeypatch):
    from core.services.timetable_mutation_guard import active_planner_job

    scenario, _ = baseline
    before = rows()
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def pipeline(*args, **kwargs):
        assert cancel_planner_job(job_id)
        PlannerJob.objects.filter(pk=job_id).update(
            started_at=timezone.now() - timedelta(minutes=90)
        )
        assert reconcile_stale_planner_jobs() == 0
        assert active_planner_job(scenario.id).id == job_id
        return {}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    assert PlannerJob.objects.get(pk=job_id).status == "cancelled"
    assert rows() == before


def test_failed_worker_clears_live_registration_for_future_orphan_sweeps(baseline, monkeypatch):
    scenario, _ = baseline
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def pipeline(*args, **kwargs):
        raise RuntimeError("solver exception")

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    # A subsequent orphan row has no executing worker: the process-local
    # registration must be removed even when the original pipeline failed.
    PlannerJob.objects.filter(pk=job_id).update(
        status=PlannerJob.STATUS_RUNNING,
        started_at=timezone.now() - timedelta(minutes=90),
    )
    assert reconcile_stale_planner_jobs() == 1


def test_v2_cancel_after_local_search_skips_chain_and_restores_baseline(baseline, monkeypatch):
    from unittest.mock import Mock

    scenario, _ = baseline
    Student.objects.create(student_id=7001, registration_no="7001", name="Student", program="AI")
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario, student_id=7001, course_key="AI101", course_code="AI101"
    )
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_V2_CURRENT)
    before = rows()

    def local_search(*args, best_candidate, **kwargs):
        assert cancel_planner_job(job_id)
        return best_candidate

    chain = Mock(side_effect=AssertionError("cancelled job must not start chain search"))
    monkeypatch.setattr(
        "core.services.timetable_local_search_v2.diagnostic_driven_local_search", local_search
    )
    monkeypatch.setattr("core.services.timetable_local_search_chains.chain_local_search", chain)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "cancelled" and job.error_message is None
    chain.assert_not_called()
    assert rows() == before


def test_v2_full_cancel_after_candidates_skips_ranking_and_restores_baseline(baseline, monkeypatch):
    from unittest.mock import Mock

    scenario, _ = baseline
    Student.objects.create(student_id=7001, registration_no="7001", name="Student", program="AI")
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario, student_id=7001, course_key="AI101", course_code="AI101"
    )
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_V2_FULL)
    before = rows()

    def candidates(*args, **kwargs):
        assert cancel_planner_job(job_id)
        SectionPlacement.objects.filter(board__scenario_id=scenario.id).update(room="TEMPORARY")
        return [{"id": "generated", "sections": []}]

    ranking = Mock(side_effect=AssertionError("cancelled job must not start ranking"))
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2._generate_candidates_for_scenario", candidates
    )
    monkeypatch.setattr("core.services.timetable_optimizer_v2.rank_timetable_candidates", ranking)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "cancelled" and job.error_message is None
    ranking.assert_not_called()
    assert rows() == before


@pytest.mark.parametrize("locked", [False, True])
def test_external_shared_rebuild_fails_before_pipeline_without_cancellation(
    baseline, monkeypatch, locked
):
    from unittest.mock import Mock

    scenario, board = baseline
    section = TermSection.objects.filter(scenario=scenario).order_by("pk").last()
    external_scenario = TimetableScenario.objects.create(name="External physical owner")
    external_board = DeliveryBoard.objects.create(scenario=external_scenario, label="External")
    SectionPlacement.objects.create(
        board=external_board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="OLD",
        is_locked=locked,
    )
    before = rows()
    pipeline = Mock(side_effect=AssertionError("external rebuild must stop before the pipeline"))
    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_FULL_REBUILD)
    run_planner_job(job_id)

    pipeline.assert_not_called()
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_FAILED
    assert not job.cancel_requested
    assert "resolve shared section ownership" in job.error_message
    assert job.result_json is None
    assert rows() == before
