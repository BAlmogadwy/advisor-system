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
    # Current creation APIs reject this ownership mismatch, but historical
    # imported references must also survive a section's cascade/recreation.
    external_scenario = TimetableScenario.objects.create(name="Historical board owner")
    external_board = DeliveryBoard.objects.create(scenario=external_scenario, label="Historical")
    external_placement = SectionPlacement.objects.create(
        board=external_board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="OLD",
        is_locked=True,
    )
    BoardSectionVisibility.objects.create(board=external_board, term_section=section)
    external_run = TimetableRepairRun.objects.create(
        scenario=external_scenario,
        target_section=section,
        target_placement=external_placement,
    )
    external_plan = TimetableRepairGlobalPlan.objects.create(
        scenario=external_scenario, request_signature="historical"
    )
    TimetableRepairGlobalPlanItem.objects.create(
        plan=external_plan,
        sequence=1,
        repair_run=external_run,
        candidate=candidate,
        placement=external_placement,
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
@pytest.mark.parametrize("outcome", ["success", "error_result", "exception"])
def test_running_cancel_restores_whole_scenario_for_every_engine(
    baseline, monkeypatch, mode, outcome
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
        assert cancel_planner_job(job_id)
        if outcome == "exception":
            raise RuntimeError("solver fault after cancel")
        if outcome == "error_result":
            return {"error": "guarded solver failure after cancel"}
        return {"placements": 1}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    monkeypatch.setattr("scheduler.bridge.build_into_scenario", pipeline)
    run_planner_job(job_id)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == "cancelled"
    assert job.cancel_requested and job.finished_at is not None
    assert job.result_json is None and job.error_message is None
    assert rows() == before
    assert not cancel_planner_job(job_id)


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
