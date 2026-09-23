"""The exam job contract at its edges: the shared solver, what a page opening is
offered, who sees what, the limits, and the failures a long-lived job must
survive. Most of these were written after a review mutated the code and found
the first suite green.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import InterfaceError
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
    with pytest.raises(KeyError), solver_slot():
        raise KeyError
    with solver_slot(wait=False):
        pass


def test_a_caller_that_will_not_wait_is_told_at_once():
    with solver_slot(), pytest.raises(SolverBusy), solver_slot(wait=False):
        pass


def test_a_waiting_job_stops_waiting_when_it_should():
    """Asked every half second; a job cancelled or caught by a shutdown must
    not start its work the moment the slot frees."""
    with solver_slot(), pytest.raises(SolverBusy), solver_slot(give_up=lambda: True):
        pass


def test_the_planner_takes_the_same_slot(monkeypatch):
    seen = []

    def probe(job_id):
        try:
            with solver_slot(wait=False):
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
                with solver_slot(wait=False):
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

    with solver_slot():
        assert _post(export_client, payload, 503)["error_code"] == "solver_busy"
    Job.objects.create(kind=Job.KIND_BUILD, status=Job.STATUS_RUNNING, heartbeat_at=timezone.now())
    assert _post(export_client, payload, 409)["error_code"] == "job_in_progress"


# ── what a page opening is offered ──────────────────────────────────


def test_a_page_opening_sees_anyones_running_job_but_not_their_token(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
):
    running = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model),
        heartbeat_at=timezone.now(),
        client_token="theirs",
    )
    job = export_client.get(reverse("exam_timetable_job_active")).json()["job"]
    assert job["id"] == str(running.pk)
    assert job["mine"] is False
    assert "client_token" not in job
    assert job["now"]


@pytest.mark.parametrize(
    "unoffered",
    ["another_users", "too_old", "failed", "an_error_answer"],
)
def test_only_your_own_recent_saved_result_is_offered_back(
    export_client,  # noqa: F811
    jobs,
    django_user_model,
    unoffered,
):
    """An error answer - a 400 the action gave - is not a timetable to open."""
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
    elif unoffered == "failed":
        fields["status"] = Job.STATUS_FAILED
    else:
        fields.update(result_run=None, response_status=400, response_json={"ok": False})
    Job.objects.create(**fields)
    assert export_client.get(reverse("exam_timetable_job_active")).json()["job"] is None


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


def test_polling_someone_elses_job_shows_no_token(client, jobs, django_user_model):
    job = Job.objects.create(
        kind=Job.KIND_BUILD,
        status=Job.STATUS_RUNNING,
        submitted_by=_committee_member(django_user_model, "owner"),
        heartbeat_at=timezone.now(),
        client_token="tab-owner",
    )
    client.force_login(_committee_member(django_user_model, "watcher"))
    polled = client.get(reverse("exam_timetable_job", args=[job.pk])).json()["job"]
    assert polled["mine"] is False
    assert "client_token" not in polled
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


def test_a_client_token_longer_than_the_column_is_cut_not_refused(
    export_client,  # noqa: F811
    clashing_pair,  # noqa: F811
    jobs,
):
    metadata, _, _ = clashing_pair
    submitted = _post(export_client, _build_payload(metadata, client_token="t" * 200), 202)
    assert Job.objects.get(pk=submitted["job"]["id"]).client_token == "t" * 64


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
    monkeypatch.setattr(exam_views.exam_jobs, "submit", lambda payload, user: (202, {"ok": True}))
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
        client.post(url, {"label": "x"}, content_type="application/json").status_code
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
    ("status", "age_field", "age"),
    [
        (Job.STATUS_RUNNING, "started_at", timedelta(minutes=25)),
        (Job.STATUS_QUEUED, "submitted_at", timedelta(minutes=35)),
    ],
)
def test_a_job_that_is_alive_but_stuck_is_failed(export_client, jobs, status, age_field, age):  # noqa: F811
    """A heartbeat proves the process is alive, not that the job is moving; a
    job stuck with no cancellation point would hold the only lane for ever."""
    job = Job.objects.create(kind=Job.KIND_BUILD, status=status, heartbeat_at=timezone.now())
    Job.objects.filter(pk=job.pk).update(**{age_field: timezone.now() - age})
    polled = _poll(export_client, job.pk)
    assert (polled["status"], polled["error_code"]) == ("failed", "timed_out")


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
