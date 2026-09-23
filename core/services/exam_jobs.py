"""Exam timetable actions as background jobs: submit, run, report, cancel.

Build, Optimize, Fix and Save used to be one synchronous request that held a
request thread for up to a minute or two on a 0.5-CPU host and told the page
nothing until it ended. A job is submitted in one request (202), runs on its
own thread, and the page polls it for the stage it has reached.

The rules that make this safe on one small web process:

* **One active job, across all users.** A database constraint enforces it, so
  it holds while an old and a new instance overlap during a deploy. A second
  submit is refused with the holder's name and stage, not queued: a queue in
  memory is started by the interpreter's exit hook during a shutdown and then
  killed, and a queue in the database is a worker we do not run.
* **A job owns its save.** The run is inserted and the job marked SUCCEEDED in
  one short transaction, conditional on the job still being wanted. A cancel,
  a crash or a restart can therefore never leave a run in the history without
  a job that says it finished.
* **Progress is written from its own thread and connection**, so the page sees
  it while the job's own thread is busy, and a lost connection costs one
  heartbeat rather than the job.
* **A stale job is failed, never retried.** A job whose heartbeat stops was
  killed - by a deploy, or by running out of memory, in which case a retry
  would take every in-flight request down with it again.

Run synchronously (``EXAM_JOBS_ENABLED`` off), the view calls the same
``execute_exam_action`` and returns the same status and body, so the switch is
a rollback that the page does not notice.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import socket
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone

from core.models import ExamTimetableJob, ExamTimetableRun
from core.services.exam_progress import ExamProgress, JobCancelled, reporting
from core.services.job_runtime import HOLDER_EXAM_JOB, SolverBusy, solver_holder, solver_slot
from core.services.job_runtime import shutting_down as _shutting_down

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)

Job = ExamTimetableJob

#: A running or waiting job writes a heartbeat about every second; one that has
#: been silent this long belonged to a process that no longer exists.
STALE_AFTER = timedelta(seconds=90)
#: A heartbeat proves the process is alive, not that the job is moving. A build
#: takes a minute or two on production; one still running after this is stuck
#: somewhere with no cancellation point, and it holds the only lane.
MAX_RUNNING = timedelta(minutes=20)
#: A job waits while a timetable-planner job holds the solver, which can take
#: twelve to fifteen minutes; past this it will not be wanted any more.
MAX_QUEUED = timedelta(minutes=30)
FLUSH_SECONDS = 1.0
#: Finished jobs are deleted after this; the runs they produced are not.
RETENTION = timedelta(days=7)
#: A result that finished while its page was closed is offered back this long.
UNSEEN_RESULT_WINDOW = timedelta(hours=1)
#: The fields a request thread may load. The board itself (``request_payload``,
#: about 1 MB) is only ever loaded by the job's own thread.
_LIGHT_FIELDS = (
    "id",
    "kind",
    "status",
    "submitted_by",
    "result_run",
    "progress_json",
    "error_code",
    "submitted_at",
    "started_at",
    "finished_at",
    "acknowledged_at",
    "response_status",
    "cancel_requested",
    "submitted_by__username",
    "submitted_by__first_name",
    "submitted_by__last_name",
    "cancelled_by",
    "cancelled_by__username",
    "cancelled_by__first_name",
    "cancelled_by__last_name",
)


def _light() -> QuerySet[ExamTimetableJob]:
    """Jobs as a request thread may load them: never the board, always the names."""
    return Job.objects.select_related("submitted_by", "cancelled_by").only(*_LIGHT_FIELDS)


#: The stages each kind of job reports, in order. A stage the job passes over
#: - rooms turned off, nothing to repair - is shown as skipped, never as done.
STAGES: dict[str, tuple[str, ...]] = {
    Job.KIND_BUILD: (
        "enrolments",
        "conflicts",
        "place_exams",
        "check_rules",
        "assign_rooms",
        "balance_invigilators",
        "save",
    ),
    Job.KIND_OPTIMIZE: (
        "read_board",
        "place_exams",
        "check_rules",
        "assign_rooms",
        "balance_invigilators",
        "save",
    ),
    Job.KIND_REPAIR: ("read_board", "fewest_moves", "check_rules", "assign_rooms", "save"),
    Job.KIND_SAVE: ("read_board", "check_rules", "assign_rooms", "save"),
}
_LOADED_KINDS = {Job.KIND_OPTIMIZE, Job.KIND_REPAIR, Job.KIND_SAVE}


def jobs_enabled() -> bool:
    return bool(getattr(settings, "EXAM_JOBS_ENABLED", False))


def _run_inline() -> bool:
    return bool(getattr(settings, "EXAM_JOBS_RUN_INLINE", False))


def resolve_seed(payload: dict) -> int:
    """The randomised build's seed: drawn when the job was submitted, if it was.

    Only a job's stored payload carries ``_seed``; the view strips it from what
    the browser sends, so a client cannot choose one.
    """
    seed = payload.get("_seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and seed > 0:
        return seed
    return random.randint(1, 2**31 - 1)  # nosec B311 - tie-breaking, not security


def is_multistart(payload: dict) -> bool:
    """A multistart build, which persists several candidate runs outside one
    transaction and keeps each candidate in memory: never a background job."""
    from core.services.exam_multistart import is_multistart_enabled

    return (
        "base_schedule" not in payload
        and bool(payload.get("multistart"))
        and is_multistart_enabled()
    )


# ── progress ─────────────────────────────────────────────────────────────────


class JobProgress(ExamProgress):
    """What one job has reached, held in memory and written out by a flusher.

    The pipelines call ``stage`` and the counters from the job's thread; the
    flusher reads ``snapshot`` from its own. A lock keeps the two consistent.
    Every call is also a cancellation point.
    """

    def __init__(self, plan: tuple[str, ...]) -> None:
        self._lock = threading.Lock()
        self._plan = plan
        self._states = dict.fromkeys(plan, "pending")
        self._current: dict[str, Any] | None = None
        self._waiting_for: dict | None = None
        self._stage_started: dict[str, float] = {}
        self.stage_ms: dict[str, int] = {}
        self.cancelled = threading.Event()
        self.on_change: Any = None  # the inline flusher's hook

    def stage(self, key: str) -> None:
        self.check_cancelled()
        with self._lock:
            if key not in self._states:
                return
            now = time.monotonic()
            for earlier in self._plan[: self._plan.index(key)]:
                if self._states[earlier] == "running":
                    self._close(earlier, "done", now)
                elif self._states[earlier] == "pending":
                    self._states[earlier] = "skipped"
            self._states[key] = "running"
            self._stage_started[key] = now
            self._current = {"key": key, "done": None, "total": None}
        self._changed(force=True)

    def counter(self, key: str):
        def tick(done: int, total: int) -> None:
            self.check_cancelled()
            with self._lock:
                if self._current is None or self._current["key"] != key:
                    return
                self._current = {"key": key, "done": int(done), "total": int(total)}
            self._changed(force=False)

        return tick

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled

    def waiting_for(self, holder: dict | None) -> None:
        """What a queued job is waiting on, so its page can say it."""
        with self._lock:
            changed = (holder or {}).get("kind") != (self._waiting_for or {}).get("kind")
            self._waiting_for = holder
        if changed:
            self._changed(force=True)

    def finish(self, *, completed: bool) -> None:
        """Close the stages: a finished job skips what it never reached; a job
        that stopped marks where it stopped and leaves the rest pending."""
        with self._lock:
            now = time.monotonic()
            for key in self._plan:
                if self._states[key] == "running":
                    self._close(key, "done" if completed else "stopped", now)
                elif completed and self._states[key] == "pending":
                    self._states[key] = "skipped"

    def snapshot(self, *, finished: bool | None = None) -> dict:
        """The stages as they stand, or - with ``finished`` - as they will stand
        once the job ends that way, without committing to it yet."""
        with self._lock:
            states = dict(self._states)
            if finished is not None:
                for key in self._plan:
                    if states[key] == "running":
                        states[key] = "done" if finished else "stopped"
                    elif finished and states[key] == "pending":
                        states[key] = "skipped"
            snapshot = {
                "stages": [{"key": key, "state": states[key]} for key in self._plan],
                "current": dict(self._current) if self._current else None,
            }
            if self._waiting_for is not None:
                snapshot["waiting_for"] = dict(self._waiting_for)
            return snapshot

    def _close(self, key: str, state: str, now: float) -> None:
        self._states[key] = state
        started = self._stage_started.get(key)
        if started is not None:
            self.stage_ms[key] = int((now - started) * 1000)

    def _changed(self, *, force: bool) -> None:
        if self.on_change is not None:
            self.on_change(force=force)


def _initial_progress(kind: str) -> dict:
    return JobProgress(STAGES[kind]).snapshot()


class _Flusher:
    """Writes a job's progress and heartbeat, and reads its cancel flag.

    In production it is a thread with its own database connection, writing
    about once a second, so progress is visible while the job's own thread is
    busy. Run inline (tests), it writes from the calling thread at each change
    instead, at most twice a second within a stage.

    It must outlive any one database error: a connection the server dropped
    raises one error and then another on every later use, and a flusher that
    died of it would stop the heartbeat, and the sweep would fail a live job.
    """

    def __init__(self, job_id, progress: JobProgress, *, threaded: bool) -> None:
        self.job_id = job_id
        self.progress = progress
        self.threaded = threaded
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = 0.0

    def start(self) -> None:
        if self.threaded:
            self._thread = threading.Thread(
                target=self._loop, name=f"exam-job-progress-{self.job_id}", daemon=True
            )
            self._thread.start()
        else:
            self.progress.on_change = self._inline

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.progress.on_change = None

    def _loop(self) -> None:
        try:
            while not self._stop.wait(FLUSH_SECONDS):
                self.flush()
        finally:
            connection.close()

    def _inline(self, *, force: bool) -> None:
        now = time.monotonic()
        if force or now - self._last >= 0.5:
            self._last = now
            self.flush()

    def flush(self) -> None:
        try:
            alive = Job.objects.filter(pk=self.job_id, status__in=Job.ACTIVE_STATUSES).update(
                progress_json=self.progress.snapshot(), heartbeat_at=timezone.now()
            )
            if not alive or Job.objects.filter(pk=self.job_id, cancel_requested=True).exists():
                # Cancelled, or swept as stale: either way nobody wants the
                # result, and a job running on regardless would hold the solver
                # while the next one waits for it.
                self.progress.cancelled.set()
        except Exception:
            # Never fail the build it reports on. SQLite can lose a lock race;
            # PostgreSQL can drop the connection, which the next tick reopens.
            logger.warning("exam job %s: progress write failed", self.job_id, exc_info=True)
            if self.threaded:
                connection.close()


# ── submit ───────────────────────────────────────────────────────────────────


def submit(payload: dict, *, user, is_superadmin: bool = False) -> tuple[int, dict]:
    """Start one action in the background, or say why it cannot start now."""
    mode = str(payload.get("mode") or "").strip()
    if "base_schedule" in payload:
        kind = mode if mode in _LOADED_KINDS else None
        if kind is None:
            # The same answer the synchronous path gives, before anything is stored.
            return 400, {"ok": False, "error": f"Unknown timetable action: {mode or 'none'}."}
    else:
        kind = Job.KIND_BUILD
    if not str(payload.get("label", "")).strip():
        return 400, {"ok": False, "error": "label is required"}

    sweep_stale()
    _purge_finished()
    payload = dict(payload)
    if payload.get("randomize"):
        payload["_seed"] = resolve_seed({})
    editor_revision = payload.get("editor_revision", 0)
    try:
        with transaction.atomic():
            job = Job.objects.create(
                kind=kind,
                submitted_by=user if getattr(user, "is_authenticated", False) else None,
                editor_revision=editor_revision if isinstance(editor_revision, int) else 0,
                request_payload=payload,
                progress_json=_initial_progress(kind),
                heartbeat_at=timezone.now(),
            )
    except IntegrityError:
        return 409, _busy_body(user)
    _log(job.id, kind, "submitted", user_id=getattr(user, "pk", None))
    try:
        _dispatch(job.id)
    except Exception:
        # A thread that cannot start (memory or thread pressure) must not leave
        # a queued row holding the only lane until the sweep finds it.
        logger.exception("exam job %s could not start", job.id)
        _end(job.id, None, Job.STATUS_FAILED, error_code="server_error")
        # Its owner is told in this answer, so it is not news on the next page open.
        Job.objects.filter(pk=job.id).update(acknowledged_at=timezone.now())
        return 503, {
            "ok": False,
            "error_code": "job_not_started",
            "error": "The server could not start this action. Nothing was saved; try again.",
        }
    fresh = _light().get(pk=job.pk)
    return 202, {"ok": True, "job": serialize(fresh, user=user, is_superadmin=is_superadmin)}


def lane_busy() -> bool:
    """Is a job queued or running? Swept first, so a dead one does not count."""
    sweep_stale()
    return Job.objects.filter(lane="exam", status__in=Job.ACTIVE_STATUSES).exists()


def busy_body(user) -> dict:
    return _busy_body(user)


def _busy_body(user) -> dict:
    active = _light().filter(lane="exam", status__in=Job.ACTIVE_STATUSES).first()
    holder: dict[str, Any] = {}
    if active is not None:
        mine = active.submitted_by_id is not None and active.submitted_by_id == getattr(
            user, "pk", None
        )
        current = (active.progress_json or {}).get("current") or {}
        holder = {
            "kind": active.kind,
            "mine": mine,
            "owner": _display_name(active.submitted_by),
            "stage": current.get("key"),
            "status": active.status,
        }
        if mine:
            holder["id"] = str(active.id)
    return {
        "ok": False,
        "error_code": "job_in_progress",
        "error": "Another timetable action is running. Try again when it has finished.",
        "active_job": holder,
    }


def _display_name(user) -> str:
    if user is None:
        return ""
    full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return full or getattr(user, "username", "") or ""


def _dispatch(job_id) -> None:
    if _run_inline():
        run_job(job_id, threaded=False)
        return
    # One thread per job, not a pool: a pool's queue is drained by the
    # interpreter's exit hook, starting queued work in the middle of a shutdown.
    threading.Thread(
        target=run_job, args=(job_id,), name=f"exam-job-{job_id}", daemon=False
    ).start()


# ── run ──────────────────────────────────────────────────────────────────────


def run_job(job_id, *, threaded: bool = True) -> None:
    """Run one submitted job to an end state. Never raises."""
    from core.exam_views import execute_exam_action

    if threaded:
        close_old_connections()
    job = None
    progress: JobProgress | None = None
    flusher: _Flusher | None = None
    started = time.monotonic()
    outcome = "gone"
    try:
        job = Job.objects.filter(pk=job_id, status=Job.STATUS_QUEUED).first()
        if job is None:
            return
        progress = JobProgress(STAGES[job.kind])
        # The heartbeat runs while the job waits its turn for the solver, so a
        # long wait is not mistaken for a dead process.
        flusher = _Flusher(job.id, progress, threaded=threaded)
        flusher.start()
        waiting = progress

        def give_up() -> bool:
            waiting.waiting_for(solver_holder())
            return waiting.cancelled.is_set() or _shutting_down()

        try:
            with solver_slot(holder=HOLDER_EXAM_JOB, give_up=give_up):
                waiting.waiting_for(None)
                if _shutting_down() or not _claim(job.id):
                    outcome = _end_unstarted(job.id)
                    return
                with reporting(progress):
                    status, body = execute_exam_action(
                        job.request_payload,
                        save=functools.partial(_commit_run, job.id, progress),
                    )
                outcome = _finish(job.id, progress, status, body)
        except SolverBusy:
            # It gave up waiting: cancelled, swept, or the process is exiting.
            outcome = _end_unstarted(job.id)
    except JobCancelled:
        outcome = _end_safely(job_id, progress, Job.STATUS_CANCELLED, "cancelled", threaded)
    except Exception:
        logger.exception("exam job %s failed", job_id)
        outcome = _end_safely(job_id, progress, Job.STATUS_FAILED, "server_error", threaded)
    finally:
        if flusher is not None:
            flusher.stop()
        if job is not None:
            _log(
                job.id,
                job.kind,
                outcome,
                seconds=round(time.monotonic() - started, 2),
                stage_ms=progress.stage_ms if progress else {},
                max_rss_mb=_max_rss_mb(),
            )
        if threaded:
            connection.close()


def _end_unstarted(job_id) -> str:
    """A job that never got to run: cancelled while it waited, swept, or caught
    by a shutdown. Only the last still needs its row closed."""
    if _shutting_down():
        Job.objects.filter(pk=job_id, status__in=Job.ACTIVE_STATUSES).update(
            status=Job.STATUS_FAILED,
            error_code="server_restarted",
            request_payload={},
            finished_at=timezone.now(),
        )
    return Job.objects.filter(pk=job_id).values_list("status", flat=True).first() or "gone"


def _end_safely(job_id, progress, status: str, error_code: str, threaded: bool) -> str:
    """Record how a job ended, even if the error that ended it was the database's."""
    if threaded:
        close_old_connections()
    try:
        return _end(job_id, progress, status, error_code=error_code)
    except Exception:
        # The sweep will fail it once the heartbeat stops.
        logger.exception("exam job %s: could not record its end", job_id)
        return status


def _claim(job_id) -> bool:
    now = timezone.now()
    return bool(
        Job.objects.filter(pk=job_id, status=Job.STATUS_QUEUED, cancel_requested=False).update(
            status=Job.STATUS_RUNNING, started_at=now, heartbeat_at=now, worker=_worker_id()
        )
    )


def _commit_run(job_id, progress: JobProgress, label: str, result: dict) -> int:
    """Save the run and finish the job together, or neither.

    Past this point a cancel is too late to honour partially: either the job
    is still wanted and both rows commit, or it is not and nothing does.
    """
    progress.stage("save")
    with transaction.atomic():
        run = ExamTimetableRun.objects.create(
            label=label, result_json=json.dumps(result, ensure_ascii=False)
        )
        finished = Job.objects.filter(
            pk=job_id, status=Job.STATUS_RUNNING, cancel_requested=False
        ).update(
            status=Job.STATUS_SUCCEEDED,
            result_run=run,
            response_status=200,
            request_payload={},
            progress_json=progress.snapshot(finished=True),
            heartbeat_at=timezone.now(),
            finished_at=timezone.now(),
        )
        if not finished:
            raise JobCancelled
    progress.finish(completed=True)
    return run.id


def _finish(job_id, progress: JobProgress, status: int, body: dict) -> str:
    """Record an action that ended without saving a run: an error, or Fix with
    nothing to move. A saved run already finished the job in ``_commit_run``."""
    if Job.objects.filter(pk=job_id, status=Job.STATUS_SUCCEEDED).exists():
        return Job.STATUS_SUCCEEDED
    if status >= 500:
        return _end(
            job_id,
            progress,
            Job.STATUS_FAILED,
            error_code="server_error",
            response_status=status,
            body=body,
        )
    ended = Job.objects.filter(pk=job_id, status=Job.STATUS_RUNNING, cancel_requested=False).update(
        status=Job.STATUS_SUCCEEDED,
        response_status=status,
        response_json=body,
        request_payload={},
        progress_json=progress.snapshot(finished=True),
        heartbeat_at=timezone.now(),
        finished_at=timezone.now(),
    )
    if ended:
        progress.finish(completed=True)
        return Job.STATUS_SUCCEEDED
    # Cancelled while it ran, and nothing was saved: say so.
    return _end(job_id, progress, Job.STATUS_CANCELLED, error_code="cancelled")


def _end(
    job_id,
    progress: JobProgress | None,
    status: str,
    *,
    error_code: str,
    body: dict | None = None,
    response_status: int | None = None,
) -> str:
    """Close an active job that stopped; a job already closed stays as it is."""
    fields: dict[str, Any] = {
        "status": status,
        "error_code": error_code,
        "request_payload": {},
        "finished_at": timezone.now(),
    }
    if progress is not None:
        fields["progress_json"] = progress.snapshot(finished=False)
    if body is not None:
        fields["response_json"] = body
        fields["response_status"] = response_status
    if Job.objects.filter(pk=job_id, status__in=Job.ACTIVE_STATUSES).update(**fields):
        if progress is not None:
            progress.finish(completed=False)
        return status
    return Job.objects.filter(pk=job_id).values_list("status", flat=True).first() or status


# ── read, cancel, sweep ──────────────────────────────────────────────────────


def serialize(job: Job, *, user, is_superadmin: bool = False) -> dict:
    progress = job.progress_json or {}
    mine = job.submitted_by_id is not None and job.submitted_by_id == getattr(user, "pk", None)
    stopped_by_other = (
        job.cancelled_by_id is not None and job.cancelled_by_id != job.submitted_by_id
    )
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "stages": progress.get("stages") or [],
        "current": progress.get("current"),
        "waiting_for": progress.get("waiting_for"),
        "error_code": job.error_code,
        "mine": mine,
        # Runs are shared by the committee, so who is building is not private;
        # a registrar waiting on a colleague's build should know whose it is.
        "owner": _display_name(job.submitted_by),
        "can_cancel": mine or is_superadmin,
        "cancelled_by": _display_name(job.cancelled_by) if stopped_by_other else "",
        "has_run": job.result_run_id is not None,
        # It ended with the action's own refusal (a 4xx it answered), not a result.
        "refused": (job.response_status or 0) >= 400,
        # Asked to stop and not stopped yet: a second Stop would change nothing.
        "stopping": job.cancel_requested and job.status in Job.ACTIVE_STATUSES,
        "result_run_id": job.result_run_id,
        "submitted_at": _iso(job.submitted_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "now": _iso(timezone.now()),
    }


def poll(job_id, *, user, is_superadmin: bool = False) -> tuple[int, dict]:
    sweep_stale()
    job = _light().filter(pk=job_id).first()
    if job is None:
        return 404, {"ok": False, "error_code": "job_not_found", "error": "Job not found"}
    return 200, {"ok": True, "job": serialize(job, user=user, is_superadmin=is_superadmin)}


def seen(job_id, *, user) -> tuple[int, dict]:
    """The owner's page has shown how it ended: do not show it again."""
    owner = Job.objects.filter(pk=job_id).values_list("submitted_by_id", flat=True).first()
    if owner is None and not Job.objects.filter(pk=job_id).exists():
        return 404, {"ok": False, "error_code": "job_not_found", "error": "Job not found"}
    if owner is None or owner != getattr(user, "pk", None):
        return 403, {"ok": False, "error": "Only the person who started it can mark it seen."}
    marked = Job.objects.filter(pk=job_id, acknowledged_at__isnull=True).update(
        acknowledged_at=timezone.now()
    )
    return 200, {"ok": True, "marked": bool(marked)}


def result(job_id, *, user) -> tuple[int, dict]:
    """The action's own status and body, exactly as the synchronous view gives them."""
    job = Job.objects.filter(pk=job_id).defer("request_payload").first()
    if job is None:
        return 404, {"ok": False, "error_code": "job_not_found", "error": "Job not found"}
    if job.status == Job.STATUS_FAILED:
        return job.response_status or 500, job.response_json or {
            "ok": False,
            "error_code": job.error_code or "server_error",
            "error": "The timetable action stopped before it finished. Nothing was saved.",
        }
    if job.status != Job.STATUS_SUCCEEDED:
        return 409, {
            "ok": False,
            "error_code": f"job_{job.status}",
            "error": "This timetable action has no result.",
        }
    if job.submitted_by_id is not None and job.submitted_by_id == getattr(user, "pk", None):
        Job.objects.filter(pk=job.pk, acknowledged_at__isnull=True).update(
            acknowledged_at=timezone.now()
        )
    if job.result_run_id is None:
        if job.response_status == 200 and not job.response_json:
            # The run this job saved has since been deleted from the history.
            return 410, {"ok": False, "error_code": "run_deleted", "error": "That run was deleted."}
        return job.response_status or 200, job.response_json
    run = ExamTimetableRun.objects.filter(pk=job.result_run_id).first()
    if run is None:
        return 410, {"ok": False, "error_code": "run_deleted", "error": "That run was deleted."}
    body: dict[str, Any] = {"ok": True}
    if job.kind in _LOADED_KINDS:
        body["editor_revision"] = job.editor_revision
    body.update(json.loads(run.result_json))
    body["run_id"] = run.id
    return 200, body


def active(*, user, is_superadmin: bool = False, owed: str | None = None) -> dict:
    """What a page opening now should know: the job running, and how the
    caller's own latest job ended if it ended while their page was closed.

    Both, when both are true: "the outcome will be shown here" must hold even
    when a colleague's job is running as the registrar comes back.

    ``owed``: the id of an ending a page was given beside a running job and is
    now asking about again. It was news inside the window when the page got
    it, so the window no longer applies to it; seen, superseded or deleted, it
    is still not returned."""
    sweep_stale()
    running = _light().filter(lane="exam", status__in=Job.ACTIVE_STATUSES).first()
    ending = None
    if getattr(user, "is_authenticated", False):
        # Only the latest: an older result the registrar has since built past
        # is not news, even if nobody ever opened it.
        latest = _light().filter(submitted_by=user).order_by("-submitted_at").first()
        if latest is not None and _unseen_ending(latest, windowed=str(latest.pk) != owed):
            ending = serialize(latest, user=user, is_superadmin=is_superadmin)
    if running is not None:
        job = serialize(running, user=user, is_superadmin=is_superadmin)
        return {"ok": True, "job": job, "ending": ending}
    return {"ok": True, "job": ending}


def _unseen_ending(job: Job, *, windowed: bool = True) -> bool:
    if job.status in Job.ACTIVE_STATUSES or job.acknowledged_at is not None:
        return False
    if job.finished_at is None:
        return False
    if windowed and job.finished_at < timezone.now() - UNSEEN_RESULT_WINDOW:
        return False
    if (
        job.status == Job.STATUS_SUCCEEDED
        and job.result_run_id is None
        and job.response_status == 200
    ):
        # Saved, then deleted from Saved timetables (its run link was nulled):
        # nothing is left to show, and "nothing was built" would be untrue. A
        # Fix that moved nothing keeps its answer, and is still news.
        return Job.objects.filter(pk=job.pk).exclude(response_json={}).exists()
    return True


def cancel(job_id, *, user, is_superadmin: bool) -> tuple[int, dict]:
    job = _light().filter(pk=job_id).first()
    if job is None:
        return 404, {"ok": False, "error_code": "job_not_found", "error": "Job not found"}
    if not is_superadmin and job.submitted_by_id != getattr(user, "pk", None):
        return 403, {"ok": False, "error": "Only the person who started it can cancel it."}
    now = timezone.now()
    stopped_by = user if getattr(user, "is_authenticated", False) else None
    stopped_here = True
    if Job.objects.filter(pk=job.pk, status=Job.STATUS_QUEUED).update(
        status=Job.STATUS_CANCELLED,
        cancel_requested=True,
        cancelled_by=stopped_by,
        error_code="cancelled",
        request_payload={},
        finished_at=now,
    ):
        _log(job.pk, job.kind, "cancelled")
    elif not Job.objects.filter(
        pk=job.pk, status=Job.STATUS_RUNNING, cancel_requested=False
    ).update(cancel_requested=True, cancelled_by=stopped_by):
        # Not stopped by this call. A Stop already on its way to the job keeps
        # whoever sent it first; a job that has ended cannot be stopped at all.
        if not Job.objects.filter(
            pk=job.pk, status=Job.STATUS_RUNNING, cancel_requested=True
        ).exists():
            job = _light().get(pk=job.pk)
            return 409, {
                "ok": False,
                "error_code": "job_finished",
                "error": "It had already finished.",
                "job": serialize(job, user=user, is_superadmin=is_superadmin),
            }
        stopped_here = False
    job = _light().get(pk=job.pk)
    return 202, {
        "ok": True,
        "stopped_here": stopped_here,
        "job": serialize(job, user=user, is_superadmin=is_superadmin),
    }


def sweep_stale() -> int:
    """Fail every job whose process stopped, or that has run far too long.

    It reads before it writes: on SQLite even an UPDATE that matches nothing
    takes the database's one write lock, and every poll calls this.
    """
    now = timezone.now()
    stale = (
        Job.objects.filter(status__in=Job.ACTIVE_STATUSES, heartbeat_at__lt=now - STALE_AFTER)
        | Job.objects.filter(status=Job.STATUS_RUNNING, started_at__lt=now - MAX_RUNNING)
        | Job.objects.filter(status=Job.STATUS_QUEUED, submitted_at__lt=now - MAX_QUEUED)
    )
    rows = list(
        stale.values(
            "id", "kind", "status", "worker", "heartbeat_at", "started_at", "progress_json"
        )
    )
    swept = 0
    for row in rows:
        silent = row["heartbeat_at"] is None or row["heartbeat_at"] < now - STALE_AFTER
        if silent:
            code = "server_restarted"
        elif row["status"] == Job.STATUS_QUEUED:
            code = "never_started"
        else:
            code = "timed_out"
        # Only as it was read: a queued job that started meanwhile is not one
        # that never started.
        if Job.objects.filter(pk=row["id"], status=row["status"]).update(
            status=Job.STATUS_FAILED, error_code=code, request_payload={}, finished_at=now
        ):
            swept += 1
            # A silent heartbeat means a deploy or - the one to look for - an OOM kill.
            logger.warning(
                "exam job swept %s",
                json.dumps(
                    {
                        "id": str(row["id"]),
                        "kind": row["kind"],
                        "was": row["status"],
                        "reason": code,
                        "worker": row["worker"],
                        "stage": ((row["progress_json"] or {}).get("current") or {}).get("key"),
                        "heartbeat_at": _iso(row["heartbeat_at"]),
                        "started_at": _iso(row["started_at"]),
                    }
                ),
            )
    return swept


def _purge_finished() -> None:
    cutoff = timezone.now() - RETENTION
    stale_ids = list(
        Job.objects.filter(finished_at__lt=cutoff)
        .exclude(status__in=Job.ACTIVE_STATUSES)
        .values_list("pk", flat=True)[:200]
    )
    if stale_ids:
        Job.objects.filter(pk__in=stale_ids).delete()


# ── helpers ──────────────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def _max_rss_mb() -> float | None:
    """The process's peak resident memory so far - a ceiling, not this job's own."""
    try:
        import resource
    except ImportError:  # Windows development machines
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _log(job_id, kind: str, event: str, **fields) -> None:
    # The fields go in the message itself: the console formatter prints no
    # ``extra``, and these lines are the only record of how long each stage took.
    logger.info(
        "exam job %s %s",
        event,
        json.dumps({"id": str(job_id), "kind": kind, **fields}, default=str),
    )
