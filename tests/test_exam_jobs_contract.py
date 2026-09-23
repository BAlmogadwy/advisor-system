"""The exam job contract at its edges: the shared solver, what a page opening is
offered, who sees what, the limits, and the failures a long-lived job must
survive. Most of these were written after a review mutated the code and found
the first suite green.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import InterfaceError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, ExamTimetableJob, Room
from core.services import exam_jobs, planner_job_runner
from core.services.exam_jobs import STAGES, JobProgress
from core.services.job_runtime import SolverBusy, solver_slot
from tests.test_exam_export_source import export_client as export_client
from tests.test_exam_export_source import export_courses as export_courses
from tests.test_exam_jobs import (
    _build_payload,
    _committee_member,
    _loaded_payload,
    _poll,
    _post,
    _result,
    _run_as_job,
    _states,
)
from tests.test_exam_min_change_http import DAYS, PERIODS, SCOPE
from tests.test_exam_min_change_http import clashing_pair as clashing_pair

pytestmark = pytest.mark.django_db
Job = ExamTimetableJob


@pytest.fixture
def jobs(settings):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    return settings


def _me(django_user_model):
    return django_user_model.objects.get(username="export-source-admin")


# ── the shared solver ───────────────────────────────────────────────


def test_the_slot_is_released_when_its_block_raises():
    with pytest.raises(KeyError), solver_slot(holder="test"):
        raise KeyError
    with solver_slot(holder="test", wait=False):
        pass


def test_a_caller_that_will_not_wait_is_told_at_once():
    with (
        solver_slot(holder="test"),
        pytest.raises(SolverBusy),
        solver_slot(holder="test", wait=False),
    ):
        pass


def test_a_waiting_job_stops_waiting_when_it_should():
    """Asked every half second; a job cancelled or caught by a shutdown must
    not start its work the moment the slot frees."""
    with (
        solver_slot(holder="test"),
        pytest.raises(SolverBusy),
        solver_slot(holder="test", give_up=lambda: True),
    ):
        pass


def test_the_planner_takes_the_same_slot(monkeypatch):
    seen = []

    def probe(job_id):
        try:
            with solver_slot(holder="test", wait=False):
                seen.append("free")
        except SolverBusy:
            seen.append("held")

    monkeypatch.setattr(planner_job_runner, "close_old_connections", lambda: None)
    monkeypatch.setattr(planner_job_runner, "run_planner_job", probe)
    planner_job_runner._worker("x")
    assert seen == ["held"]


def test_a_running_job_holds_the_slot(export_client, clashing_pair, jobs, monkeypatch):  # noqa: F811
    seen = []
    real_stage = JobProgress.stage

    def probe(self, key):
        if key == "check_rules":
            try:
                with solver_slot(holder="test", wait=False):
                    seen.append("free")
            except SolverBusy:
                seen.append("held")
        return real_stage(self, key)

    monkeypatch.setattr(JobProgress, "stage", probe)
    metadata, _, _ = clashing_pair
    _run_as_job(export_client, _build_payload(metadata))
    assert seen == ["held"]


def test_a_job_caught_by_a_shutdown_never_starts(export_client, clashing_pair, jobs, monkeypatch):  # noqa: F811
    """The interpreter's exit hooks join the planner's pool first; a job waiting
    behind it would otherwise claim the slot mid-shutdown and be killed."""
    monkeypatch.setattr(exam_jobs, "_shutting_down", lambda: True)
    metadata, _, _ = clashing_pair
    submitted = _post(export_client, _build_payload(metadata), 202)
    job = _poll(export_client, submitted["job"]["id"])
    assert (job["status"], job["error_code"]) == ("failed", "server_restarted")
    assert job["started_at"] is None


def test_multistart_stays_synchronous_but_still_takes_turns(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    from core import exam_views

    jobs.TIMETABLE_EXAM_MULTISTART_ENABLED = True
    monkeypatch.setattr(
        exam_views,
        "execute_exam_action",
        lambda payload, **kw: (200, {"ok": True, "mode": "multistart"}),
    )
    metadata, _, _ = clashing_pair
    payload = _build_payload(metadata, multistart=True)
    assert _post(export_client, payload, 200)["mode"] == "multistart"
    assert not Job.objects.exists(), "Multistart is never a background job"

    with solver_slot(holder="test"):
        assert _post(export_client, payload, 503)["error_code"] == "solver_busy"
    Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING, heartbeat_at=timezone.now())
    assert _post(export_client, payload, 409)["error_code"] == "job_in_progress"


# ── what a page opening is offered ──────────────────────────────────


def test_a_page_opening_sees_anyones_running_job_and_whose_it_is(
    client,
    jobs,
    django_user_model,
):
    owner = _committee_member(django_user_model, "owner")
    owner.first_name, owner.last_name = "Huda", "Saleh"
    owner.save()
    running = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=owner,
        heartbeat_at=timezone.now(),
    )
    client.force_login(_committee_member(django_user_model, "watcher"))
    job = client.get(reverse("exam_timetable_job_active")).json()["job"]
    assert job["id"] == str(running.pk)
    assert job["mine"] is False
    assert job["owner"] == "Huda Saleh"
    assert job["can_cancel"] is False, "Only the owner or a SUPER_ADMIN may stop it"
    assert job["has_run"] is False
    assert job["now"]


def test_a_superadmin_is_told_they_may_cancel_anyones_job(export_client, jobs, django_user_model):  # noqa: F811
    running = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model),
        heartbeat_at=timezone.now(),
    )
    polled = _poll(export_client, running.pk)
    assert (polled["mine"], polled["can_cancel"]) == (False, True)


@pytest.mark.parametrize(
    "unoffered",
    ["another_users", "too_old", "seen"],
)
def test_only_your_own_recent_saved_result_is_offered_back(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
    unoffered,
):
    from core.models import ExamTimetableRun

    run = ExamTimetableRun.objects.create(label="saved", result_json="{}")
    fields = {
        "kind": Job.KIND_BUILD,
        "status": Job.STATUS_SUCCEEDED,
        "submitted_by": _me(django_user_model),
        "finished_at": timezone.now(),
        "result_run": run,
    }
    if unoffered == "another_users":
        fields["submitted_by"] = _committee_member(django_user_model)
    elif unoffered == "too_old":
        fields["finished_at"] = timezone.now() - timedelta(hours=2)
    elif unoffered == "seen":
        fields["acknowledged_at"] = timezone.now()
    Job.objects.create(**fields)
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


@pytest.mark.parametrize(
    ("kind", "response_status", "refused"),
    [
        (Job.KIND_SAVE, 409, True),
        (Job.KIND_BUILD, 400, True),
        (Job.KIND_REPAIR, 200, False),
    ],
)
def test_an_action_that_saved_nothing_while_you_were_away_is_shown(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
    kind,
    response_status,
    refused,
):
    """A Save refused because the inputs changed while it waited, or a Fix
    with nothing to move: "the outcome will be shown here" covers these too."""
    job = Job.objects.create(
        kind=kind,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now(),
        response_status=response_status,
        response_json={"ok": response_status < 400, "error": "Inputs changed."},
    )
    offered = export_client.get(reverse("exam_timetable_job_active")).json()["job"]
    assert (offered["id"], offered["has_run"], offered["refused"]) == (str(job.pk), False, refused)


@pytest.mark.parametrize("status", [Job.STATUS_FAILED, Job.STATUS_CANCELLED])
def test_a_job_that_ended_badly_while_you_were_away_is_shown_once(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
    status,
):
    """ "You can leave this page" is a promise about failures too: a build that
    failed while its registrar was away is shown when they come back, once."""
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=status,
        error_code="server_error",
        submitted_by=_me(django_user_model),
        finished_at=timezone.now(),
    )
    offered = export_client.get(reverse("exam_timetable_job_active")).json()["job"]
    assert (offered["id"], offered["status"]) == (str(job.pk), status)
    assert export_client.post(reverse("exam_timetable_job_seen", args=[job.pk])).status_code == 200
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


def test_only_the_latest_of_your_jobs_is_offered(export_client, jobs, django_user_model):  # noqa: F811
    """An older result the registrar has since built past is not news, even
    if nobody ever opened it."""
    from core.models import ExamTimetableRun

    me = _me(django_user_model)
    older = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=me,
        finished_at=timezone.now(),
        result_run=ExamTimetableRun.objects.create(label="older", result_json="{}"),
    )
    newer = Job.objects.create(
        kind=Job.KIND_REPAIR,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=me,
        finished_at=timezone.now(),
        acknowledged_at=timezone.now(),
        result_run=ExamTimetableRun.objects.create(label="newer", result_json="{}"),
    )
    Job.objects.filter(pk=older.pk).update(submitted_at=timezone.now() - timedelta(minutes=5))
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None
    Job.objects.filter(pk=newer.pk).update(acknowledged_at=None)
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"]["id"] == str(
        newer.pk
    )


def test_a_dead_job_is_swept_before_a_page_is_told_about_it(export_client, jobs):  # noqa: F811
    Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        heartbeat_at=timezone.now() - timedelta(minutes=5),
    )
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


def test_reading_someone_elses_result_does_not_mark_it_seen(client, jobs, django_user_model):
    owner = _committee_member(django_user_model, "owner")
    job = Job.objects.create(
        kind=Job.KIND_REPAIR,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=owner,
        finished_at=timezone.now(),
        response_status=200,
        response_json={"ok": True, "saved": False},
    )
    client.force_login(_committee_member(django_user_model, "reader"))
    assert client.get(reverse("exam_timetable_job_result", args=[job.pk])).status_code == 200
    assert Job.objects.get(pk=job.pk).acknowledged_at is None


# ── bodies ──────────────────────────────────────────────────────────


def test_the_409_for_your_own_running_job_hands_back_its_id(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    django_user_model,
):
    mine = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_me(django_user_model),
        heartbeat_at=timezone.now(),
    )
    metadata, _, _ = clashing_pair
    holder = _post(export_client, _build_payload(metadata), 409)["active_job"]
    assert (holder["mine"], holder.get("id")) == (True, str(mine.pk))


def test_polling_someone_elses_job_does_not_offer_to_cancel_it(client, jobs, django_user_model):
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model, "owner"),
        heartbeat_at=timezone.now(),
    )
    client.force_login(_committee_member(django_user_model, "watcher"))
    polled = client.get(reverse("exam_timetable_job", args=[job.pk])).json()["job"]
    assert (polled["mine"], polled["can_cancel"]) == (False, False)
    assert polled["now"]


def test_a_swept_job_result_is_a_500_that_says_nothing_was_saved(export_client, jobs):  # noqa: F811
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        heartbeat_at=timezone.now() - timedelta(minutes=5),
    )
    _poll(export_client, job.pk)
    status, body = _result(export_client, job.pk)
    assert (status, body["ok"], body["error_code"]) == (500, False, "server_restarted")


@pytest.mark.parametrize("state", [Job.STATUS_QUEUED, Job.STATUS_RUNNING])
def test_an_unfinished_job_has_no_result(export_client, jobs, state):  # noqa: F811
    job = Job.objects.create(kind=Job.KIND_BUILD, status=state, heartbeat_at=timezone.now())
    status, body = _result(export_client, job.pk)
    assert (status, body["error_code"]) == (409, f"job_{state}")


def test_a_run_deleted_while_its_result_is_read_says_so(export_client, jobs, monkeypatch):  # noqa: F811
    """The run can vanish between reading the job and reading the run; the
    answer is still "deleted", never an empty success."""
    from core.models import ExamTimetableRun

    run = ExamTimetableRun.objects.create(label="gone", result_json="{}")
    job = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED, result_run=run, response_status=200
    )
    monkeypatch.setattr(
        exam_jobs.ExamTimetableRun.objects, "filter", lambda **kw: ExamTimetableRun.objects.none()
    )
    status, body = _result(export_client, job.pk)
    assert (status, body["error_code"]) == (410, "run_deleted")


def test_a_check_that_fails_on_the_server_shows_nothing_of_the_failure(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    from core import exam_views

    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)

    def broken(**kwargs):
        raise RuntimeError("secret internals: /srv/app/db.sqlite3")

    monkeypatch.setattr(exam_views, "evaluate_exam_schedule", broken)
    response = export_client.post(
        reverse("exam_timetable_draft_impact"),
        {
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "previous_run_id": built["run_id"],
            "base_schedule": built["schedule"],
            "pinned": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 500
    assert "secret internals" not in response.content.decode()


def test_a_late_cancel_never_relabels_a_swept_failure():
    job = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_FAILED, error_code="server_restarted"
    )
    exam_jobs._end(
        job.pk, JobProgress(STAGES["build"]), Job.STATUS_CANCELLED, error_code="cancelled"
    )
    job.refresh_from_db()
    assert (job.status, job.error_code) == ("failed", "server_restarted")


def test_an_offered_result_is_withdrawn_once_its_owner_has_seen_it(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """Marking it seen withdraws the offer (Close does this without fetching
    the result; fetching it marks it seen too, tested with the result view)."""
    metadata, _, _ = clashing_pair
    submitted = _post(export_client, _build_payload(metadata), 202)
    job_id = submitted["job"]["id"]
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"]["id"] == job_id
    response = export_client.post(reverse("exam_timetable_job_seen", args=[job_id]))
    assert response.status_code == 200 and response.json()["marked"] is True
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


def test_only_the_owner_can_mark_a_result_seen(client, jobs, django_user_model):
    owner = _committee_member(django_user_model, "owner")
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=owner,
        finished_at=timezone.now(),
    )
    client.force_login(_committee_member(django_user_model, "someone-else"))
    response = client.post(reverse("exam_timetable_job_seen", args=[job.pk]))
    assert response.status_code == 403
    assert Job.objects.get(pk=job.pk).acknowledged_at is None
    missing = client.post(reverse("exam_timetable_job_seen", args=[uuid.uuid4()]))
    assert (missing.status_code, missing.json()["error_code"]) == (404, "job_not_found")


def test_a_finished_job_keeps_no_copy_of_the_board(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """The submitted board is about 1 MB; a week of jobs kept it all."""
    metadata, _, _ = clashing_pair
    job_id, status, body = _run_as_job(export_client, _build_payload(metadata))
    assert status == 200
    assert Job.objects.get(pk=job_id).request_payload == {}


def test_a_superadmin_stopping_someone_elses_job_is_audited(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model),
        heartbeat_at=timezone.now(),
    )
    response = export_client.post(reverse("exam_timetable_job_cancel", args=[job.pk]))
    assert response.status_code == 202
    assert AuditLog.objects.filter(action="exam_timetable.cancel_others_job").exists()


@pytest.mark.parametrize("first", ["owner", "superadmin"])
def test_the_first_stop_is_the_one_recorded(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
    first,
):
    """A job notices a Stop only at its next checkpoint, which can be seconds
    away in a solve. A second Stop meanwhile must not rewrite who stopped it, nor
    record a superadmin's stop of a colleague's job that its owner stopped."""
    owner = _committee_member(django_user_model, "owner")
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=owner,
        heartbeat_at=timezone.now(),
    )
    # Two browsers: the test client fixtures share one session.
    owner_client = Client()
    owner_client.force_login(owner)
    url = reverse("exam_timetable_job_cancel", args=[job.pk])
    stoppers = [owner_client, export_client] if first == "owner" else [export_client, owner_client]
    first_answer = stoppers[0].post(url).json()
    second_answer = stoppers[1].post(url).json()
    assert (first_answer["stopped_here"], second_answer["stopped_here"]) == (True, False)
    expected = owner if first == "owner" else _me(django_user_model)
    assert Job.objects.get(pk=job.pk).cancelled_by == expected
    audited = AuditLog.objects.filter(action="exam_timetable.cancel_others_job").count()
    assert audited == (0 if first == "owner" else 1)
    assert _poll(owner_client, job.pk)["stopping"] is True, "Asked to stop: no second Stop offered"


# ── retention, seed and limits ──────────────────────────────────────


def test_finished_jobs_are_purged_after_a_week_not_before(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    now = timezone.now()
    old = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED, finished_at=now - timedelta(days=8)
    )
    recent = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED, finished_at=now - timedelta(days=6)
    )
    metadata, _, _ = clashing_pair
    _post(export_client, _build_payload(metadata), 202)
    assert not Job.objects.filter(pk=old.pk).exists()
    assert Job.objects.filter(pk=recent.pk).exists()


def test_a_randomised_job_runs_with_the_seed_drawn_at_submit_not_one_sent_in(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    metadata, _, _ = clashing_pair
    draws = iter([424242, 999, 998])
    monkeypatch.setattr(exam_jobs.random, "randint", lambda a, b: next(draws))
    _, status, body = _run_as_job(export_client, _build_payload(metadata, randomize=True, _seed=7))
    assert status == 200
    assert body["seed"] == 424242


def test_a_seed_sent_to_the_synchronous_path_is_ignored(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    jobs.EXAM_JOBS_ENABLED = False
    monkeypatch.setattr(exam_jobs.random, "randint", lambda a, b: 31337)
    metadata, _, _ = clashing_pair
    body = _post(export_client, _build_payload(metadata, randomize=True, _seed=7), 200)
    assert body["seed"] == 31337


def test_resolve_seed_accepts_only_a_positive_integer(monkeypatch):
    monkeypatch.setattr(exam_jobs.random, "randint", lambda a, b: 7)
    assert exam_jobs.resolve_seed({"_seed": 5}) == 5
    for bad in (True, 0, -3, "5", None):
        assert exam_jobs.resolve_seed({"_seed": bad}) == 7


def test_the_synchronous_limit_still_applies_and_the_job_limit_is_looser(
    client, jobs, django_user_model, monkeypatch
):
    from core import exam_views

    monkeypatch.setattr("core.authz._rate_buckets", {})
    monkeypatch.setattr(
        exam_views, "execute_exam_action", lambda payload, **kw: (200, {"ok": True})
    )
    monkeypatch.setattr(exam_views.exam_jobs, "submit", lambda payload, **kw: (202, {"ok": True}))
    client.force_login(_committee_member(django_user_model))
    url = reverse("exam_timetable_build")
    jobs.EXAM_JOBS_ENABLED = False
    # 3 in production; 20 if the module was first imported with DEBUG on.
    limit = exam_views._BUILD_MAX_CALLS
    codes = [
        client.post(url, {"label": "x"}, content_type="application/json").status_code
        for _ in range(limit + 1)
    ]
    assert codes == [200] * limit + [429]
    assert exam_views._JOB_SUBMIT_MAX_CALLS == 20
    jobs.EXAM_JOBS_ENABLED = True
    codes = [
        client.post(
            url, {"label": "x"}, content_type="application/json", HTTP_X_EXAM_JOBS="1"
        ).status_code
        for _ in range(21)
    ]
    assert codes == [202] * 20 + [429]


def test_a_committee_member_may_poll_without_limit(client, jobs, django_user_model):
    job = Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_SUCCEEDED)
    client.force_login(_committee_member(django_user_model))
    for _ in range(40):
        assert client.get(reverse("exam_timetable_job", args=[job.pk])).status_code == 200


# ── the sweep ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "age_field", "age", "code"),
    [
        (Job.STATUS_RUNNING, "started_at", timedelta(minutes=25), "timed_out"),
        (Job.STATUS_QUEUED, "submitted_at", timedelta(minutes=35), "never_started"),
    ],
)
def test_a_job_that_is_alive_but_stuck_is_failed(export_client, jobs, status, age_field, age, code):  # noqa: F811
    """A heartbeat proves the process is alive, not that the job is moving; a
    job stuck with no cancellation point would hold the only lane for ever. A
    job that never started is told apart from one that ran too long: the page
    says different things about them."""
    job = Job.objects.create(kind=Job.KIND_BUILD, status=status, heartbeat_at=timezone.now())
    Job.objects.filter(pk=job.pk).update(**{age_field: timezone.now() - age})
    polled = _poll(export_client, job.pk)
    assert (polled["status"], polled["error_code"]) == ("failed", code)


def test_a_sweep_with_nothing_to_do_writes_nothing(jobs, django_assert_num_queries):
    """On SQLite even an UPDATE that matches nothing takes the one write lock,
    and every poll sweeps."""
    Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING, heartbeat_at=timezone.now())
    with django_assert_num_queries(1):
        assert exam_jobs.sweep_stale() == 0


def test_a_swept_job_is_logged_with_what_it_was_doing(export_client, jobs, caplog):  # noqa: F811
    job = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_RUNNING,
        heartbeat_at=timezone.now() - timedelta(minutes=5),
        worker="web-1:42",
        progress_json={"stages": [], "current": {"key": "assign_rooms", "done": 1, "total": 9}},
    )
    with caplog.at_level(logging.WARNING, logger="core.services.exam_jobs"):
        exam_jobs.sweep_stale()
    line = next(r.getMessage() for r in caplog.records if "swept" in r.getMessage())
    for fact in (str(job.pk), "optimize_loaded", "web-1:42", "assign_rooms", "server_restarted"):
        assert fact in line


def test_job_lines_reach_the_console_in_production_logging():
    """gunicorn configures no root logger; without an entry for these the INFO
    lines - every stage timing - were silently dropped."""
    job_logger = logging.getLogger("core.services.exam_jobs")
    assert job_logger.isEnabledFor(logging.INFO)
    handlers, node = [], job_logger
    while node is not None:
        handlers += node.handlers
        node = node.parent if node.propagate else None
    assert any(isinstance(h, logging.StreamHandler) for h in handlers), "No console reaches it"


# ── a flusher that outlives a database error ────────────────────────


def test_the_flusher_survives_a_dropped_connection(monkeypatch):
    """A dropped PostgreSQL connection raises one error, then InterfaceError -
    not a DatabaseError - on every later use. A flusher that died of it stopped
    the heartbeat, and the sweep then failed a live job."""
    job = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING, heartbeat_at=timezone.now()
    )
    progress = JobProgress(STAGES["build"])
    flusher = exam_jobs._Flusher(job.pk, progress, threaded=False)
    real_filter = Job.objects.filter
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterfaceError("connection already closed")
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(Job.objects, "filter", flaky)
    flusher.flush()  # must not raise
    flusher.flush()
    assert Job.objects.get(pk=job.pk).progress_json["stages"], "The next tick wrote again"


def test_a_job_that_was_swept_is_told_to_stop():
    """A swept job that ran on would hold the solver while the next one waits."""
    job = Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_FAILED)
    progress = JobProgress(STAGES["build"])
    exam_jobs._Flusher(job.pk, progress, threaded=False).flush()
    assert progress.cancelled.is_set()


# ── stages that are wired, not just declared ────────────────────────


def test_every_stage_and_counter_key_in_the_source_is_in_a_plan():
    """A stage the reporter does not know is silently ignored, so a typo in one
    would leave the page stuck on the stage before it."""
    services = Path(exam_jobs.__file__).resolve().parent
    planned = {key for plan in STAGES.values() for key in plan}
    used: set[str] = set()
    for path in [services.parent / "exam_views.py", *services.glob("exam_*.py")]:
        used |= set(
            re.findall(r'\.(?:stage|counter)\("([a-z_]+)"\)', path.read_text(encoding="utf-8"))
        )
    assert used and used <= planned, used - planned


def _recording(ticks):
    real_counter = JobProgress.counter

    def recording(self, key):
        tick = real_counter(self, key)

        def record(done, total):
            # Only ticks the page would actually show: the stage must be current.
            if self._current and self._current["key"] == key:
                ticks.append(key)
            tick(done, total)

        return record

    return recording


def test_an_optimise_with_rooms_reports_its_counted_stages(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    ticks: list[str] = []
    monkeypatch.setattr(JobProgress, "counter", _recording(ticks))
    metadata, _, _ = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata, assign_rooms=True), 200)
    jobs.EXAM_JOBS_ENABLED = True
    job_id, status, _ = _run_as_job(
        export_client, _loaded_payload(built, built["schedule"], "optimize_loaded")
    )
    assert status == 200
    states = _states(_poll(export_client, job_id))
    assert states["place_exams"] == "done" and states["assign_rooms"] == "done", states
    assert {"place_exams", "assign_rooms"} <= set(ticks)


def test_a_build_with_real_rooms_balances_its_invigilators(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    """Without Room rows the invigilator pass never runs, and its stage would
    read "skipped" whatever the code did."""
    Room.objects.create(room_code="F001", capacity=40, section="F", building="B1", floor="1")
    Room.objects.create(room_code="M001", capacity=40, section="M", building="B1", floor="1")
    ticks: list[str] = []
    monkeypatch.setattr(JobProgress, "counter", _recording(ticks))
    metadata, _, _ = clashing_pair
    job_id, status, _ = _run_as_job(export_client, _build_payload(metadata, assign_rooms=True))
    assert status == 200
    states = _states(_poll(export_client, job_id))
    assert states["assign_rooms"] == "done" and states["balance_invigilators"] == "done", states
    assert "assign_rooms" in ticks


# ── the page opts in; busy answers say who; a job that cannot start ──


def test_a_client_that_does_not_ask_for_a_job_gets_the_old_answer(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    """A tab still running the previous release's script cannot follow a job:
    answering it with one would blank its board on the day jobs are turned on."""
    metadata, _, _ = clashing_pair
    response = export_client.post(
        reverse("exam_timetable_build"), _build_payload(metadata), content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["run_id"]
    assert not Job.objects.exists()


def test_the_solver_says_what_holds_it():
    from core.services.job_runtime import solver_holder

    assert solver_holder() is None
    with solver_slot(holder="planner"):
        assert solver_holder()["kind"] == "planner"
        with pytest.raises(SolverBusy) as busy:
            with solver_slot(holder="check", wait=False):
                pass
        assert busy.value.holder["kind"] == "planner"
    assert solver_holder() is None


def test_a_queued_job_says_what_it_is_waiting_for(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
    django_user_model,
):
    """Behind a timetable-planner run a job can wait a quarter of an hour; its
    page must say that, not that another exam action is running."""
    seen = []

    def shutting_down():
        # Called each half second while it waits; the first look is while queued.
        if not seen:
            job = Job.objects.get()
            seen.append(exam_jobs.poll(job.pk, user=_me(django_user_model))[1]["job"])
            return False
        return True

    monkeypatch.setattr(exam_jobs, "_shutting_down", shutting_down)
    metadata, _, _ = clashing_pair
    with solver_slot(holder="planner"):
        _post(export_client, _build_payload(metadata), 202)
    assert seen[0]["status"] == "queued"
    assert seen[0]["waiting_for"]["kind"] == "planner"


def test_a_job_whose_thread_cannot_start_frees_the_lane(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    real_dispatch = exam_jobs._dispatch

    def no_thread(job_id):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(exam_jobs, "_dispatch", no_thread)
    metadata, _, _ = clashing_pair
    refused = _post(export_client, _build_payload(metadata), 503)
    assert refused["error_code"] == "job_not_started"
    assert Job.objects.get().status == Job.STATUS_FAILED
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None, (
        "Already told, in the 503: not shown again on the next page open"
    )
    monkeypatch.setattr(exam_jobs, "_dispatch", real_dispatch)
    assert _post(export_client, _build_payload(metadata), 202)["job"]["id"]


def test_the_owner_is_told_who_stopped_their_job(client, export_client, jobs, django_user_model):  # noqa: F811
    owner = _committee_member(django_user_model, "owner")
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=owner,
        heartbeat_at=timezone.now(),
    )
    admin = _me(django_user_model)
    admin.first_name, admin.last_name = "Sara", "Admin"
    admin.save()
    assert (
        export_client.post(reverse("exam_timetable_job_cancel", args=[job.pk])).status_code == 202
    )
    client.force_login(owner)
    assert _poll(client, job.pk)["cancelled_by"] == "Sara Admin"


def test_stopping_your_own_job_names_no_one(export_client, jobs, django_user_model):  # noqa: F811
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_QUEUED,
        submitted_by=_me(django_user_model),
        heartbeat_at=timezone.now(),
    )
    assert (
        export_client.post(reverse("exam_timetable_job_cancel", args=[job.pk])).status_code == 202
    )
    assert _poll(export_client, job.pk)["cancelled_by"] == ""
    assert Job.objects.get(pk=job.pk).cancelled_by == _me(django_user_model), (
        "Recorded all the same"
    )


def test_a_poll_never_loads_the_board(export_client, jobs, django_user_model):  # noqa: F811
    """The board is about 1 MB and stays in the row for the whole run; the page
    polls every second and a half."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_me(django_user_model),
        heartbeat_at=timezone.now(),
        request_payload={"base_schedule": ["x" * 1000] * 50},
    )
    _poll(export_client, job.pk)
    with CaptureQueriesContext(connection) as captured:
        _poll(export_client, job.pk)
    sql = " ".join(query["sql"] for query in captured.captured_queries)
    assert "request_payload" not in sql
    assert "response_json" not in sql
    job_selects = [
        q
        for q in captured.captured_queries
        if 'FROM "exam_timetable_jobs"' in q["sql"]
        and q["sql"].lstrip().upper().startswith("SELECT")
    ]
    # The sweep's read, and the job itself with both names joined.
    assert len(job_selects) <= 2, [q["sql"] for q in job_selects]


def test_a_sweep_does_not_relabel_a_job_that_started_meanwhile(export_client, jobs, monkeypatch):  # noqa: F811
    """The sweep reads, then writes. A queued job that claims the solver in
    between is running, not one that never started."""
    job = Job.objects.create(
        kind=Job.KIND_BUILD, status=Job.STATUS_QUEUED, heartbeat_at=timezone.now()
    )
    Job.objects.filter(pk=job.pk).update(submitted_at=timezone.now() - timedelta(minutes=35))

    def read_then_start(rows):
        rows = [*rows]
        Job.objects.filter(pk=job.pk).update(status=Job.STATUS_RUNNING, started_at=timezone.now())
        return rows

    monkeypatch.setattr(exam_jobs, "list", read_then_start, raising=False)
    exam_jobs.sweep_stale()
    assert Job.objects.get(pk=job.pk).status == Job.STATUS_RUNNING


def test_jobs_are_on_unless_turned_off(monkeypatch):
    import importlib

    import config.settings

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("EXAM_JOBS_ENABLED", raising=False)
    try:
        assert importlib.reload(config.settings).EXAM_JOBS_ENABLED is True
        monkeypatch.setenv("EXAM_JOBS_ENABLED", "false")
        assert importlib.reload(config.settings).EXAM_JOBS_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(config.settings)


def test_a_planner_job_never_starts_during_a_shutdown(monkeypatch):
    """Behind an exam job, a planner run can get the solver only as the process
    exits; a full rebuild clears the board first and would be killed half-way."""
    from core.models import PlannerJob
    from tests.test_planner_job_reconcile import _job, _scenario

    started = []
    job = _job(_scenario(), PlannerJob.STATUS_QUEUED)
    monkeypatch.setattr(planner_job_runner, "close_old_connections", lambda: None)
    monkeypatch.setattr(planner_job_runner, "run_planner_job", started.append)
    monkeypatch.setattr(planner_job_runner, "shutting_down", lambda: True)
    planner_job_runner._worker(str(job.id))
    assert started == []
    assert PlannerJob.objects.get(pk=job.id).status == PlannerJob.STATUS_FAILED


def test_a_client_without_the_header_says_what_it_holds_the_solver_for(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
):
    from core import exam_views
    from core.services.job_runtime import solver_holder

    held = []

    def run(payload, **kwargs):
        held.append(solver_holder()["kind"])
        return 200, {"ok": True}

    monkeypatch.setattr(exam_views, "execute_exam_action", run)
    metadata, _, _ = clashing_pair
    response = export_client.post(
        reverse("exam_timetable_build"), _build_payload(metadata), content_type="application/json"
    )
    assert response.status_code == 200
    assert held == ["exam_sync"], "An action run in its request, not multistart"


@pytest.mark.parametrize(
    ("holder", "retry"), [("planner", "30"), ("exam_job", "15"), ("check", "5")]
)
def test_a_check_is_told_busy_before_the_board_is_read(
    export_client,  # noqa: F811
    jobs,
    monkeypatch,
    holder,
    retry,
):
    """A refused Check must not take CPU from the job it waits for."""
    from core import exam_views

    def never(*args, **kwargs):
        raise AssertionError("The saved board was read for a Check the solver would refuse")

    monkeypatch.setattr(exam_views, "_loaded_request_context", never)
    with solver_slot(holder=holder):
        response = export_client.post(
            reverse("exam_timetable_draft_impact"),
            {"base_schedule": []},
            content_type="application/json",
        )
    assert response.status_code == 503
    assert response.json()["holder"]["kind"] == holder
    assert response["Retry-After"] == retry


def test_a_planner_job_waiting_for_the_solver_gives_up_when_the_process_exits(monkeypatch):
    """Queued behind an exam job as the process exits: it must stop waiting and
    fail, not start its rebuild the moment the exam job lets go."""
    import threading

    from core.services.job_runtime import HOLDER_EXAM_JOB

    started, failed = [], []
    done = threading.Event()
    monkeypatch.setattr(planner_job_runner, "close_old_connections", lambda: None)
    monkeypatch.setattr(
        planner_job_runner,
        "run_planner_job",
        lambda job_id: done.is_set() or started.append(job_id),
    )
    monkeypatch.setattr(
        planner_job_runner, "_fail_unstarted", lambda job_id: done.is_set() or failed.append(job_id)
    )
    monkeypatch.setattr(planner_job_runner, "shutting_down", lambda: True)
    try:
        with solver_slot(holder=HOLDER_EXAM_JOB):
            worker = threading.Thread(
                target=planner_job_runner._worker, args=("job-1",), daemon=True
            )
            worker.start()
            worker.join(timeout=3)
            assert not worker.is_alive(), "It stopped waiting for the solver"
    finally:
        # A worker still stuck (the defect) must do nothing once the test ends.
        done.set()
    assert (started, failed) == ([], ["job-1"])


def test_a_job_that_waited_says_nothing_about_waiting_once_it_runs(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
    monkeypatch,
    caplog,
):
    """The page says what a queued job waits for; once it has its turn, the
    reason must not linger over a job that is running or has finished."""
    import time

    from core.services import job_runtime

    job_runtime._SOLVER.acquire()
    job_runtime._holder = ("planner", time.monotonic())
    released = []

    def planner_finishes():
        # The first look while it waits: the planner run ends, the slot frees.
        # Only the planner's hold, once - never the exam job's own.
        holder = job_runtime._holder
        if not released and holder is not None and holder[0] == "planner":
            released.append(True)
            job_runtime._holder = None
            job_runtime._SOLVER.release()
        return False

    monkeypatch.setattr(exam_jobs, "_shutting_down", planner_finishes)
    metadata, _, _ = clashing_pair
    submitted = _post(export_client, _build_payload(metadata), 202)
    job = Job.objects.get(pk=submitted["job"]["id"])
    assert job.status == Job.STATUS_SUCCEEDED
    assert "waiting_for" not in job.progress_json
    # It ran holding the slot, and gave it back: nothing released it twice. A
    # double release would end the job through its failure handler, which logs.
    assert job_runtime._SOLVER.acquire(blocking=False), "The slot is free again"
    job_runtime._SOLVER.release()
    failures = [
        r
        for r in caplog.records
        if r.name == "core.services.exam_jobs" and r.levelno >= logging.ERROR
    ]
    assert failures == [], [r.getMessage() for r in failures]


def test_marking_seen_is_a_post_never_a_get(export_client, jobs, django_user_model):  # noqa: F811
    """A link prefetch must not acknowledge an outcome its owner has not seen."""
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_FAILED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now(),
    )
    url = reverse("exam_timetable_job_seen", args=[job.pk])
    assert export_client.get(url).status_code == 405
    assert export_client.head(url).status_code == 405
    assert Job.objects.get(pk=job.pk).acknowledged_at is None
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"]["id"] == str(
        job.pk
    )


def test_my_ending_is_reported_beside_a_colleagues_running_job(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    """Back while a colleague's job runs: the page follows theirs, and must
    still learn how the registrar's own job ended."""
    mine = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_FAILED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now(),
    )
    Job.objects.filter(pk=mine.pk).update(submitted_at=timezone.now() - timedelta(minutes=3))
    running = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model),
        heartbeat_at=timezone.now(),
    )
    answer = export_client.get(reverse("exam_timetable_job_active")).json()
    assert answer["job"]["id"] == str(running.pk)
    assert answer["ending"]["id"] == str(mine.pk)


def test_a_saved_run_deleted_since_is_not_reported_as_nothing_built(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    """Saved, then deleted from Saved timetables while its owner was away:
    there is nothing to show, and "No timetable was built" would be untrue."""
    from core.models import ExamTimetableRun

    run = ExamTimetableRun.objects.create(label="saved", result_json="{}")
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now(),
        result_run=run,
        response_status=200,
    )
    run.delete()
    assert Job.objects.get(pk=job.pk).result_run_id is None
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


def test_an_owed_ending_is_kept_past_the_hour_it_was_offered_in(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    """Given beside a colleague's job at 10:50, the registrar's own ending is
    asked for again when that job is closed - which may be past the hour. The
    page was promised it inside the hour; asked about by id, it is still shown."""
    mine = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_FAILED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now() - exam_jobs.UNSEEN_RESULT_WINDOW - timedelta(minutes=1),
    )
    url = reverse("exam_timetable_job_active")
    assert export_client.get(url).json()["job"] is None, "Past the hour, unasked"
    assert export_client.get(url, {"owed": str(mine.pk)}).json()["job"]["id"] == str(mine.pk)
    # Asked about another id, the window still holds.
    assert export_client.get(url, {"owed": str(uuid.uuid4())}).json()["job"] is None
    # Not an id at all: ignored, not an error.
    response = export_client.get(url, {"owed": "not-a-uuid"})
    assert (response.status_code, response.json()["job"]) == (200, None)


def test_an_owed_ending_seen_superseded_or_deleted_is_not_returned(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    """Asked about by id, the ending skips only the window: the server still
    decides whether it is news."""
    from core.models import ExamTimetableRun

    me = _me(django_user_model)
    url = reverse("exam_timetable_job_active")
    seen = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_FAILED,
        submitted_by=me,
        finished_at=timezone.now(),
        acknowledged_at=timezone.now(),
    )
    assert export_client.get(url, {"owed": str(seen.pk)}).json()["job"] is None, "Seen elsewhere"

    run = ExamTimetableRun.objects.create(label="saved", result_json="{}")
    deleted = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=me,
        finished_at=timezone.now(),
        result_run=run,
        response_status=200,
    )
    run.delete()
    assert export_client.get(url, {"owed": str(deleted.pk)}).json()["job"] is None, "Run deleted"

    owed = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_FAILED,
        submitted_by=me,
        finished_at=timezone.now(),
    )
    Job.objects.filter(pk=owed.pk).update(submitted_at=timezone.now() - timedelta(minutes=5))
    newer = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_SUCCEEDED,
        submitted_by=me,
        finished_at=timezone.now(),
        acknowledged_at=timezone.now(),
    )
    assert newer.pk != owed.pk
    assert export_client.get(url, {"owed": str(owed.pk)}).json()["job"] is None, "Superseded"


def test_a_missing_run_says_so_with_a_code(export_client, jobs):  # noqa: F811
    """A page offering a run someone has since deleted says it was deleted, in
    its own language: the answer carries a code, not only English."""
    response = export_client.get(reverse("exam_timetable_detail", args=[987654]))
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_an_owed_ending_past_the_hour_comes_back_beside_a_running_job(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    """Asked about by id while another colleague's job has started meanwhile:
    the page follows that job and must still be given the ending it is owed."""
    mine = Job.objects.create(
        kind=Job.KIND_OPTIMIZE,
        status=Job.STATUS_FAILED,
        submitted_by=_me(django_user_model),
        finished_at=timezone.now() - exam_jobs.UNSEEN_RESULT_WINDOW - timedelta(minutes=1),
    )
    running = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model),
        heartbeat_at=timezone.now(),
    )
    url = reverse("exam_timetable_job_active")
    plain = export_client.get(url).json()
    assert (plain["job"]["id"], plain["ending"]) == (str(running.pk), None), "Past the hour"
    owed = export_client.get(url, {"owed": str(mine.pk)}).json()
    assert owed["job"]["id"] == str(running.pk)
    assert owed["ending"]["id"] == str(mine.pk)
