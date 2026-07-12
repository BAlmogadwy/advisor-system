"""Stale planner-job reconciliation — orphaned RUNNING/QUEUED jobs from a dead
server are swept to FAILED so a polling UI never spins forever."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone


def _scenario():
    from core.models import TimetableScenario

    return TimetableScenario.objects.create(academic_year="1448", term="1", name="x")


def _job(scenario, status, *, started_minutes_ago=None):
    from core.models import PlannerJob

    started = (
        timezone.now() - timedelta(minutes=started_minutes_ago)
        if started_minutes_ago is not None
        else None
    )
    return PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_V2_FULL,
        status=status,
        started_at=started,
    )


@pytest.mark.django_db
def test_stale_running_job_is_failed() -> None:
    from core.models import PlannerJob
    from core.services.planner_job_runner import reconcile_stale_planner_jobs

    scn = _scenario()
    stale = _job(scn, PlannerJob.STATUS_RUNNING, started_minutes_ago=90)
    fresh = _job(scn, PlannerJob.STATUS_RUNNING, started_minutes_ago=2)
    done = _job(scn, PlannerJob.STATUS_SUCCEEDED, started_minutes_ago=120)

    n = reconcile_stale_planner_jobs()  # default 45-min window

    assert n == 1
    stale.refresh_from_db()
    fresh.refresh_from_db()
    done.refresh_from_db()
    assert stale.status == PlannerJob.STATUS_FAILED
    assert stale.finished_at is not None
    assert "server stopped" in (stale.error_message or "")
    assert fresh.status == PlannerJob.STATUS_RUNNING  # recent → untouched
    assert done.status == PlannerJob.STATUS_SUCCEEDED  # terminal → untouched


@pytest.mark.django_db
def test_stale_queued_never_dispatched_is_failed() -> None:
    from core.models import PlannerJob
    from core.services.planner_job_runner import reconcile_stale_planner_jobs

    scn = _scenario()
    queued = _job(scn, PlannerJob.STATUS_QUEUED)  # started_at = None
    # submitted_at is auto_now_add — backdate it past the window.
    PlannerJob.objects.filter(id=queued.id).update(
        submitted_at=timezone.now() - timedelta(minutes=90)
    )

    n = reconcile_stale_planner_jobs()

    assert n == 1
    queued.refresh_from_db()
    assert queued.status == PlannerJob.STATUS_FAILED


@pytest.mark.django_db
def test_reconcile_is_idempotent_and_noop_when_clean() -> None:
    from core.models import PlannerJob
    from core.services.planner_job_runner import reconcile_stale_planner_jobs

    scn = _scenario()
    _job(scn, PlannerJob.STATUS_RUNNING, started_minutes_ago=1)
    assert reconcile_stale_planner_jobs() == 0
    assert reconcile_stale_planner_jobs() == 0
