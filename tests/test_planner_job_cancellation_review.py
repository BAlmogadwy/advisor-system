"""Focused adversarial cancellation probe; all writes use the test database."""

import pytest
from django.utils import timezone

from core.models import DeliveryBoard, PlannerJob, SectionPlacement, TermSection, TimetableScenario
from core.services.planner_job_runner import (
    cancel_planner_job,
    run_planner_job,
    submit_planner_job,
)


@pytest.mark.django_db
def test_reconciled_worker_cannot_restore_over_later_manual_edit(monkeypatch):
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Long run")
    board = DeliveryBoard.objects.create(scenario=scenario, label="AI")
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="AI111",
        course_code="AI111",
        course_number="AI111",
        section="S1",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="BEFORE",
    )
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def long_pipeline(*args, **kwargs):
        assert cancel_planner_job(job_id)
        # Simulate an external reconciliation/ownership-loss decision. The old
        # in-process solver is still finishing, but it no longer owns the job.
        PlannerJob.objects.filter(pk=job_id).update(
            status=PlannerJob.STATUS_FAILED,
            error_message="External ownership lost",
            finished_at=timezone.now(),
        )
        # Manual writes are now allowed because there is no queued/running job.
        SectionPlacement.objects.filter(pk=placement.pk).update(room="MANUAL AFTER FAILED")
        return {"placements": 1}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", long_pipeline)
    run_planner_job(job_id)
    placement.refresh_from_db()
    assert placement.room == "MANUAL AFTER FAILED", (
        "A terminal reconciled job no longer owns the scenario"
    )
    job = PlannerJob.objects.get(pk=job_id)
    assert job.status == PlannerJob.STATUS_FAILED
    assert job.error_message == "External ownership lost"
