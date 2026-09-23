"""Exam jobs against a real PostgreSQL, with real threads.

Two properties cannot be shown on the SQLite test database: that a job's
progress reaches the page while the job is still inside its evaluation, and that
two submits racing each other produce exactly one job. SQLite's in-memory test
database fails a contended table lock at once instead of waiting, so a test of
either there is a test of that artefact. CI runs this file on PostgreSQL 16, the
engine production runs, and fails the job if it is skipped.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from core.models import ExamTimetableJob, ExamTimetableRun
from core.services import exam_jobs
from core.services.exam_timetable import build_exam_timetable
from tests.test_exam_export_source import export_courses as export_courses
from tests.test_exam_min_change_http import DAYS, PERIODS, SCOPE

pytestmark = pytest.mark.django_db(transaction=True)
Job = ExamTimetableJob


@pytest.fixture(autouse=True)
def postgres():
    if connection.vendor != "postgresql":
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PostgreSQL exam job tests were required")
        pytest.skip("Requires PostgreSQL concurrency semantics")


def _save_payload(built):
    return {
        "label": "Visible",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "mode": "save_loaded_changes",
        "previous_run_id": built["run_id"],
        "base_schedule": built["schedule"],
        "expected_input_fingerprint": built["input_fingerprint"],
        "assign_rooms": True,
        "pinned": [],
    }


def _wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def test_progress_is_visible_to_the_page_while_the_job_is_mid_evaluation(
    export_courses,  # noqa: F811
    settings,
    monkeypatch,
    django_user_model,
):
    """A real job thread, polled from another connection while it is held inside
    the room pack, then left to save: the threaded save path - the run and the
    job's SUCCEEDED committed together while the flusher writes the same row.

    The build and the Save both have rooms on: a Save that switched them on
    would change the reviewed inputs, answer 409 inputs_changed, and a test that
    only checked the job's status would pass without anything being saved.
    """
    from core.services import exam_timetable

    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = False
    monkeypatch.setattr(exam_jobs, "FLUSH_SECONDS", 0.05)
    built = build_exam_timetable("Visible", DAYS, PERIODS, assign_rooms=True)
    runs_before = ExamTimetableRun.objects.count()
    release = threading.Event()
    real_allocate = exam_timetable.allocate_period

    def held(*args, **kwargs):
        release.wait(timeout=20)
        return real_allocate(*args, **kwargs)

    monkeypatch.setattr(exam_timetable, "allocate_period", held)
    user = django_user_model.objects.create_superuser(username="visible-admin")
    status, body = exam_jobs.submit(_save_payload(built), user=user)
    assert status == 202
    job_id = body["job"]["id"]

    def reached_rooms():
        progress = Job.objects.filter(pk=job_id).values_list("progress_json", flat=True).first()
        current = (progress or {}).get("current") or {}
        return current if current.get("key") == "assign_rooms" else None

    try:
        seen = _wait_for(reached_rooms, 15)
        assert seen, "Progress never reached the page while the job was mid-evaluation"
        assert Job.objects.get(pk=job_id).status == Job.STATUS_RUNNING
    finally:
        release.set()
    assert _wait_for(lambda: Job.objects.get(pk=job_id).status == Job.STATUS_SUCCEEDED, 30), (
        Job.objects.get(pk=job_id).status
    )
    status, body = exam_jobs.result(job_id, user=user)
    assert status == 200, body
    assert Job.objects.get(pk=job_id).result_run_id == body["run_id"]
    assert ExamTimetableRun.objects.count() == runs_before + 1


def test_two_submits_racing_each_other_start_exactly_one_job(
    export_courses,  # noqa: F811
    settings,
    monkeypatch,
    django_user_model,
):
    """The one-active-job rule is the database's, so it holds between two request
    threads - or two instances overlapping during a deploy - that both saw no
    active job an instant earlier."""
    settings.EXAM_JOBS_ENABLED = True
    monkeypatch.setattr(exam_jobs, "_dispatch", lambda job_id: None)
    user = django_user_model.objects.create_superuser(username="racing-admin")
    barrier = threading.Barrier(2)

    def submit(_):
        try:
            barrier.wait(timeout=10)
            status, _body = exam_jobs.submit({"label": "Race", "days": DAYS}, user=user)
            return status
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, range(2)))

    assert statuses == [202, 409]
    assert Job.objects.filter(status__in=Job.ACTIVE_STATUSES).count() == 1
