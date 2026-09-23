"""Exam timetable actions as background jobs.

A job must answer exactly as the synchronous view does, save its run and
finish in one transaction or not at all, allow one active job across all users,
report stages it actually reached, and fail - never retry - a job whose process
stopped. Jobs run inline here (``EXAM_JOBS_RUN_INLINE``); the tests that need
real threads and a real database are in test_exam_jobs_postgres.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import ExamTimetableJob, ExamTimetableRun
from core.services import exam_jobs
from core.services.exam_jobs import JobProgress
from core.services.exam_progress import JobCancelled
from core.services.job_runtime import solver_slot
from core.services.rbac import ROLE_EXAM_COMMITTEE
from tests.test_exam_export_source import export_client as export_client
from tests.test_exam_export_source import export_courses as export_courses
from tests.test_exam_min_change_http import DAYS, PERIODS, SCOPE, _drag
from tests.test_exam_min_change_http import clashing_pair as clashing_pair

pytestmark = pytest.mark.django_db
Job = ExamTimetableJob


@pytest.fixture
def jobs(settings):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    return settings


def _build_payload(metadata, **extra):
    return {
        "label": "Jobs",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "assign_rooms": False,
        "selected_courses": sorted(metadata),
        "selected_course_entries": [{"course_code": c, **e} for c, e in metadata.items()],
        "pinned": [],
        **extra,
    }


def _loaded_payload(built, board, mode, **extra):
    return {
        "label": "Jobs",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "mode": mode,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": [],
        "editor_revision": 7,
        **extra,
    }


def _post(client, payload, status):
    response = client.post(
        reverse("exam_timetable_build"), payload, content_type="application/json"
    )
    assert response.status_code == status, response.content
    return response.json()


def _poll(client, job_id):
    response = client.get(reverse("exam_timetable_job", args=[job_id]))
    assert response.status_code == 200, response.content
    return response.json()["job"]


def _result(client, job_id):
    response = client.get(reverse("exam_timetable_job_result", args=[job_id]))
    return response.status_code, response.json()


def _run_as_job(client, payload):
    submitted = _post(client, payload, 202)
    job_id = submitted["job"]["id"]
    return job_id, *_result(client, job_id)


_WHEN = {"snapshot_timestamp", "created_at", "generated_at"}


def _without_run_identity(body):
    """The body as the page compares it: which run, and when, are not part of it."""

    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in _WHEN}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return stable({key: value for key, value in body.items() if key != "run_id"})


def _states(job):
    return {stage["key"]: stage["state"] for stage in job["stages"]}


def _committee_member(django_user_model, name="committee-member"):
    user = django_user_model.objects.create_user(username=name, password="unused-in-tests")
    user.groups.add(Group.objects.get_or_create(name=ROLE_EXAM_COMMITTEE)[0])
    return user


# ── the same answer, however it is run ──────────────────────────────


def test_a_build_run_as_a_job_answers_exactly_as_the_synchronous_view(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    synchronous = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    _, status, body = _run_as_job(export_client, _build_payload(metadata))

    assert status == 200
    assert body["run_id"] != synchronous["run_id"], "Each run is its own saved timetable"
    assert _without_run_identity(body) == _without_run_identity(synchronous)


@pytest.mark.parametrize(
    "mode", ["optimize_loaded", "minimum_change_repair", "save_loaded_changes"]
)
def test_a_loaded_action_run_as_a_job_answers_exactly_as_the_synchronous_view(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    mode,
):
    metadata, programming, companion = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    board = _drag(built["schedule"], companion, onto=programming)
    extra = (
        {"expected_input_fingerprint": built["input_fingerprint"]}
        if mode == "save_loaded_changes"
        else {}
    )
    synchronous = _post(export_client, _loaded_payload(built, board, mode, **extra), 200)
    jobs.EXAM_JOBS_ENABLED = True
    _, status, body = _run_as_job(export_client, _loaded_payload(built, board, mode, **extra))

    assert status == 200
    assert body["editor_revision"] == 7
    assert _without_run_identity(body) == _without_run_identity(synchronous)


def test_a_validation_error_comes_back_with_the_status_the_view_gives_it(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, programming, companion = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    board = _drag(built["schedule"], companion, onto=programming)
    bad_pin = {"pinned": [{"day": "Sun"}]}
    synchronous = _post(
        export_client, _loaded_payload(built, board, "minimum_change_repair", **bad_pin), 400
    )
    jobs.EXAM_JOBS_ENABLED = True
    before = ExamTimetableRun.objects.count()

    job_id, status, body = _run_as_job(
        export_client, _loaded_payload(built, board, "minimum_change_repair", **bad_pin)
    )

    assert (status, body) == (400, synchronous)
    assert _poll(export_client, job_id)["status"] == "succeeded", "The action answered"
    assert ExamTimetableRun.objects.count() == before


def test_an_unknown_action_is_refused_before_any_job_exists(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    body = _post(
        export_client, _loaded_payload(built, built["schedule"], "minimum_change_repiar"), 400
    )
    assert body["error"] == "Unknown timetable action: minimum_change_repiar."
    assert not Job.objects.exists()


def test_a_server_error_is_logged_not_shown(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    from core import exam_views

    def broken(*args, **kwargs):
        raise RuntimeError("secret internals: /srv/app/db.sqlite3")

    monkeypatch.setattr(exam_views, "build_exam_timetable", broken)
    metadata, _, _ = clashing_pair
    job_id, status, body = _run_as_job(export_client, _build_payload(metadata))

    assert status == 500
    assert "secret internals" not in str(body)
    job = _poll(export_client, job_id)
    assert (job["status"], job["error_code"]) == ("failed", "server_error")


# ── stages that are true ────────────────────────────────────────────


def test_stages_the_job_never_reached_are_skipped_not_done(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """Rooms are off, so neither rooming stage ever ran."""
    metadata, _, _ = clashing_pair
    job_id, status, _ = _run_as_job(export_client, _build_payload(metadata))
    job = _poll(export_client, job_id)

    assert status == 200
    assert job["status"] == "succeeded"
    assert _states(job) == {
        "enrolments": "done",
        "conflicts": "done",
        "place_exams": "done",
        "check_rules": "done",
        "assign_rooms": "skipped",
        "balance_invigilators": "skipped",
        "save": "done",
    }


def test_a_repair_with_nothing_to_move_saves_nothing_and_says_which_stages_ran(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    before = ExamTimetableRun.objects.count()

    job_id, status, body = _run_as_job(
        export_client, _loaded_payload(built, built["schedule"], "minimum_change_repair")
    )

    assert status == 200
    assert body["saved"] is False
    assert ExamTimetableRun.objects.count() == before
    assert _states(_poll(export_client, job_id)) == {
        "read_board": "done",
        "fewest_moves": "done",
        "check_rules": "skipped",
        "assign_rooms": "skipped",
        "save": "skipped",
    }


def test_counted_stages_count_the_work_actually_done(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    """Every tick is recorded. The placement count must end at the number of
    courses, and the room count at the number of packs the solver really ran."""
    from core.services import exam_timetable

    ticks: list[tuple[str, int, int]] = []
    real_counter = JobProgress.counter

    def recording_counter(self, key):
        tick = real_counter(self, key)

        def record(done, total):
            ticks.append((key, done, total))
            tick(done, total)

        return record

    packs = {"count": 0}
    real_allocate = exam_timetable.allocate_period

    def counting_allocate(*args, **kwargs):
        packs["count"] += 1
        return real_allocate(*args, **kwargs)

    monkeypatch.setattr(JobProgress, "counter", recording_counter)
    monkeypatch.setattr(exam_timetable, "allocate_period", counting_allocate)
    metadata, _, _ = clashing_pair
    job_id, status, body = _run_as_job(export_client, _build_payload(metadata, assign_rooms=True))
    assert status == 200

    courses = len(body["courses"])
    placed = [(done, total) for key, done, total in ticks if key == "place_exams"]
    assert placed == [(done, courses) for done in range(courses + 1)], "One course at a time"
    rooms = [(done, total) for key, done, total in ticks if key == "assign_rooms"]
    assert packs["count"] > 0
    assert rooms == [(done, packs["count"]) for done in range(packs["count"] + 1)], (
        "One pack at a time, ending on the number of packs the solver ran"
    )
    job = _poll(export_client, job_id)
    assert job["current"]["key"] == "save"


# ── one job at a time, and its save ─────────────────────────────────


def test_only_one_job_may_be_active_across_all_users(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    django_user_model,
):
    other = _committee_member(django_user_model)
    Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=other,
        heartbeat_at=timezone.now(),
        progress_json={"stages": [], "current": {"key": "assign_rooms", "done": 3, "total": 9}},
    )
    metadata, _, _ = clashing_pair
    body = _post(export_client, _build_payload(metadata), 409)

    assert body["error_code"] == "job_in_progress"
    holder = body["active_job"]
    assert holder["kind"] == "build"
    assert holder["stage"] == "assign_rooms"
    assert holder["mine"] is False
    assert "id" not in holder, "Another person's job is not handed to this page as its own"


def test_the_database_refuses_a_second_active_job():
    Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING)
    with pytest.raises(IntegrityError), transaction.atomic():
        Job.objects.create(kind=Job.KIND_SAVE, status=Job.STATUS_QUEUED)
    # Finished jobs do not count against it.
    Job.objects.create(kind=Job.KIND_SAVE, status=Job.STATUS_SUCCEEDED)


def test_a_job_whose_process_stopped_is_failed_and_frees_the_lane(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """A deploy or an OOM kill leaves the row RUNNING with a silent heartbeat.
    It is failed, never retried: a job that ran the process out of memory would
    do it again."""
    stale = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        heartbeat_at=timezone.now() - timedelta(minutes=5),
    )
    job = _poll(export_client, stale.pk)
    assert (job["status"], job["error_code"]) == ("failed", "server_restarted")

    metadata, _, _ = clashing_pair
    _post(export_client, _build_payload(metadata), 202)


def test_a_job_with_a_live_heartbeat_is_left_alone(export_client, jobs):  # noqa: F811
    live = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING, heartbeat_at=timezone.now()
    )
    assert _poll(export_client, live.pk)["status"] == "running"


def test_a_cancel_mid_run_saves_nothing_and_says_where_it_stopped(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    real_stage = JobProgress.stage
    target = {"job": None}

    def cancel_at_check(self, key):
        if key == "check_rules":
            Job.objects.filter(status=Job.STATUS_RUNNING).update(cancel_requested=True)
        return real_stage(self, key)

    monkeypatch.setattr(JobProgress, "stage", cancel_at_check)
    metadata, _, _ = clashing_pair
    before = ExamTimetableRun.objects.count()
    submitted = _post(export_client, _build_payload(metadata), 202)
    target["job"] = submitted["job"]["id"]

    job = _poll(export_client, target["job"])
    assert (job["status"], job["error_code"]) == ("cancelled", "cancelled")
    assert ExamTimetableRun.objects.count() == before
    states = _states(job)
    assert states["check_rules"] == "stopped"
    assert states["save"] == "pending", "A stage after the cancel never ran"
    assert _result(export_client, target["job"])[0] == 409


def test_a_cancel_that_arrives_as_it_saves_leaves_no_run_behind(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    """The run and the job's SUCCEEDED commit together or not at all."""
    real_stage = JobProgress.stage

    def cancel_at_save(self, key):
        result = real_stage(self, key)
        if key == "save":
            Job.objects.filter(status=Job.STATUS_RUNNING).update(cancel_requested=True)
        return result

    monkeypatch.setattr(JobProgress, "stage", cancel_at_save)
    metadata, _, _ = clashing_pair
    before = ExamTimetableRun.objects.count()
    submitted = _post(export_client, _build_payload(metadata), 202)

    job = _poll(export_client, submitted["job"]["id"])
    assert job["status"] == "cancelled"
    assert ExamTimetableRun.objects.count() == before
    assert Job.objects.get(pk=job["id"]).result_run_id is None


def test_a_save_can_be_cancelled_mid_evaluation_too(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    """The loaded actions unwind through their own error handling, which turns
    anything else into a 400 or a 500; a cancel must pass straight through it."""
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    real_stage = JobProgress.stage

    def cancel_at_check(self, key):
        if key == "check_rules":
            Job.objects.filter(status=Job.STATUS_RUNNING).update(cancel_requested=True)
        return real_stage(self, key)

    monkeypatch.setattr(JobProgress, "stage", cancel_at_check)
    before = ExamTimetableRun.objects.count()
    payload = _loaded_payload(
        built,
        built["schedule"],
        "save_loaded_changes",
        expected_input_fingerprint=built["input_fingerprint"],
    )
    submitted = _post(export_client, payload, 202)

    job = _poll(export_client, submitted["job"]["id"])
    assert (job["status"], job["error_code"]) == ("cancelled", "cancelled")
    assert ExamTimetableRun.objects.count() == before


def test_a_cancel_that_lands_after_an_unsaved_answer_is_still_a_cancel(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    """Fix found nothing to move and saved nothing; the registrar had pressed
    Cancel meanwhile. Nothing was saved either way, so the job says cancelled."""
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    real_finish = exam_jobs._finish

    def cancelled_just_before(job_id, *args, **kwargs):
        Job.objects.filter(pk=job_id).update(cancel_requested=True)
        return real_finish(job_id, *args, **kwargs)

    monkeypatch.setattr(exam_jobs, "_finish", cancelled_just_before)
    submitted = _post(
        export_client, _loaded_payload(built, built["schedule"], "minimum_change_repair"), 202
    )
    assert _poll(export_client, submitted["job"]["id"])["status"] == "cancelled"


def test_a_save_reports_the_evaluation_stages_it_ran(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """Rooms are on in the saved run, so the evaluation packs them. (Turning them
    on only for the Save would change the inputs the registrar reviewed, and
    Save rightly refuses that as inputs_changed.)"""
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata, assign_rooms=True), 200)
    jobs.EXAM_JOBS_ENABLED = True
    payload = _loaded_payload(
        built,
        built["schedule"],
        "save_loaded_changes",
        expected_input_fingerprint=built["input_fingerprint"],
    )
    job_id, status, _ = _run_as_job(export_client, payload)
    assert status == 200
    assert _states(_poll(export_client, job_id)) == {
        "read_board": "done",
        "check_rules": "done",
        "assign_rooms": "done",
        "save": "done",
    }


def test_only_the_person_who_started_a_job_may_cancel_it(
    client,
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    owner = _committee_member(django_user_model, "owner")
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=owner,
        heartbeat_at=timezone.now(),
    )
    other = _committee_member(django_user_model, "someone-else")
    client.force_login(other)
    assert client.post(reverse("exam_timetable_job_cancel", args=[job.pk])).status_code == 403

    client.force_login(owner)
    response = client.post(reverse("exam_timetable_job_cancel", args=[job.pk]))
    assert response.status_code == 202
    assert Job.objects.get(pk=job.pk).cancel_requested is True


def test_a_queued_job_cancels_at_once_and_a_finished_one_cannot(export_client, jobs):  # noqa: F811
    queued = Job.objects.create(kind=Job.KIND_BUILD, heartbeat_at=timezone.now())
    response = export_client.post(reverse("exam_timetable_job_cancel", args=[queued.pk]))
    assert response.status_code == 202
    assert response.json()["job"]["status"] == "cancelled"

    done = Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED)
    response = export_client.post(reverse("exam_timetable_job_cancel", args=[done.pk]))
    assert response.status_code == 409


# ── resuming a page ─────────────────────────────────────────────────


def test_a_page_opening_is_told_about_the_running_job_and_an_unseen_result(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None
    metadata, _, _ = clashing_pair
    submitted = _post(export_client, _build_payload(metadata, client_token="tab-1"), 202)
    job_id = submitted["job"]["id"]

    offered = export_client.get(reverse("exam_timetable_job_active")).json()["job"]
    assert offered["id"] == job_id, "It finished while the page was closed"
    assert offered["client_token"] == "tab-1"

    _result(export_client, job_id)
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


def test_a_result_whose_run_was_deleted_says_so(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    job_id, status, body = _run_as_job(export_client, _build_payload(metadata))
    ExamTimetableRun.objects.filter(pk=body["run_id"]).delete()
    status, body = _result(export_client, job_id)
    assert (status, body["error_code"]) == (410, "run_deleted")


def test_polling_is_never_throttled(export_client, jobs):  # noqa: F811
    job = Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED)
    for _ in range(40):
        assert export_client.get(reverse("exam_timetable_job", args=[job.pk])).status_code == 200


def test_every_job_endpoint_needs_exam_access(client, django_user_model, jobs):
    client.force_login(django_user_model.objects.create_user(username="advisor", password="x"))
    job = Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED)
    for name, args, method in [
        ("exam_timetable_job_active", [], "get"),
        ("exam_timetable_job", [job.pk], "get"),
        ("exam_timetable_job_result", [job.pk], "get"),
        ("exam_timetable_job_cancel", [job.pk], "post"),
    ]:
        assert getattr(client, method)(reverse(name, args=args)).status_code == 403, name


# ── the solver is shared, and the evaluation holds no transaction ────


def test_a_check_is_told_the_solver_is_busy_rather_than_waiting(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    check = {
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": built["schedule"],
        "pinned": [],
    }
    url = reverse("exam_timetable_draft_impact")
    with solver_slot():
        # Jobs off: Check runs as it always has.
        assert export_client.post(url, check, content_type="application/json").status_code == 200
        jobs.EXAM_JOBS_ENABLED = True
        response = export_client.post(url, check, content_type="application/json")
    assert response.status_code == 503
    assert response.json()["error_code"] == "solver_busy"
    assert response["Retry-After"]
    assert export_client.post(url, check, content_type="application/json").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_the_evaluation_holds_no_transaction(export_courses, monkeypatch):  # noqa: F811
    """On SQLite (ADR-004, IMMEDIATE) a transaction here took the database's one
    write lock for the whole evaluation, and a job's progress could not be
    written until it ended."""
    from core.services import exam_evaluation
    from core.services.exam_timetable import build_exam_timetable

    built = build_exam_timetable("Holds nothing", DAYS, PERIODS, assign_rooms=False)
    seen = []
    real = exam_evaluation.build_conflict_graph

    def spy(*args, **kwargs):
        seen.append(connection.in_atomic_block)
        return real(*args, **kwargs)

    monkeypatch.setattr(exam_evaluation, "build_conflict_graph", spy)
    exam_evaluation.evaluate_exam_schedule(
        days=DAYS,
        periods=PERIODS,
        max_per_day=2,
        schedule_raw=built["schedule"],
        selected_courses=None,
        assign_rooms=False,
        seed=None,
        thin_conflict_threshold=0,
    )
    assert seen == [False]


# ── the reporter itself ─────────────────────────────────────────────


def test_a_stage_passed_over_is_skipped_and_a_stop_leaves_the_rest_pending():
    progress = JobProgress(("a", "b", "c", "d"))
    progress.stage("a")
    progress.stage("c")
    snapshot = progress.snapshot()
    assert [s["state"] for s in snapshot["stages"]] == ["done", "skipped", "running", "pending"]
    assert progress.snapshot(finished=True)["stages"][3]["state"] == "skipped"
    assert progress.snapshot()["stages"][3]["state"] == "pending", "A preview must not commit"
    progress.finish(completed=False)
    assert [s["state"] for s in progress.snapshot()["stages"]] == [
        "done",
        "skipped",
        "stopped",
        "pending",
    ]


def test_a_count_for_a_stage_that_is_not_current_is_ignored():
    """The invigilator pass repacks rooms on every trial; those packs must not
    report themselves as the room stage."""
    progress = JobProgress(("assign_rooms", "balance_invigilators"))
    rooms = progress.counter("assign_rooms")
    progress.stage("balance_invigilators")
    rooms(5, 9)
    assert progress.snapshot()["current"] == {
        "key": "balance_invigilators",
        "done": None,
        "total": None,
    }


def test_every_report_is_a_place_the_job_can_be_cancelled():
    progress = JobProgress(("a", "b"))
    tick = progress.counter("a")
    progress.stage("a")
    progress.cancelled.set()
    with pytest.raises(JobCancelled):
        tick(1, 2)
    with pytest.raises(JobCancelled):
        progress.stage("b")
