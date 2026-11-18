"""Save changes rewrites the opened exam timetable in place (owner, 2026-11-18).

Each Save used to add another run with the same name, so Saved timetables
filled with versions of one timetable. A Save now rewrites the run it was made
on - guarded by that run's revision, so it can never undo a Save made since -
and every other action still saves a new run.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, ExamTimetableJob, ExamTimetableRun
from core.services import exam_jobs
from core.services.exam_jobs import JobProgress
from core.services.exam_roster_view import RosterViewCache
from core.services.exam_run_store import OVERWRITE_ACTION
from tests.test_exam_export_source import export_client as export_client
from tests.test_exam_export_source import export_courses as export_courses
from tests.test_exam_fixed_evaluation import PERIODS, check, save
from tests.test_exam_fixed_evaluation import editor as editor
from tests.test_exam_jobs import _build_payload, _loaded_payload, _poll, _post, _run_as_job
from tests.test_exam_jobs import jobs as jobs
from tests.test_exam_min_change_http import _drag
from tests.test_exam_min_change_http import clashing_pair as clashing_pair

pytestmark = pytest.mark.django_db


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _board(result):
    return sorted((row["course_identity"], row["day"], row["period"]) for row in result["schedule"])


@pytest.fixture
def changed(editor):  # noqa: F811
    """The editor's opened run with every exam moved, checked and ready to save."""
    client, source, payload = editor
    for row in payload["base_schedule"]:
        row.update(day="Sun", period=PERIODS[0], slot_index=0)
    payload["run_revision"] = 0
    payload["expected_input_fingerprint"] = check(client, payload)["input_fingerprint"]
    return client, source, payload


def _overwrites():
    return [
        json.loads(row.details_json) for row in AuditLog.objects.filter(action=OVERWRITE_ACTION)
    ]


# ── the opened run is rewritten, never copied ───────────────────────


def test_save_rewrites_the_opened_timetable_and_adds_no_run(changed):
    client, source, payload = changed
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    created_at, saved_at = run.created_at, run.saved_at
    count = ExamTimetableRun.objects.count()

    saved = save(client, payload)

    assert ExamTimetableRun.objects.count() == count
    assert saved["run_id"] == source["run_id"]
    assert saved["run_revision"] == 1
    run.refresh_from_db()
    assert run.revision == 1
    assert run.label == "Manual changes"
    assert run.created_at == created_at, "Created once; only the save time moves"
    assert run.saved_at > saved_at
    assert _board(json.loads(run.result_json)) == _board(saved) != _board(source)
    # The page shows "saved" from the answer itself, as from the detail view.
    assert saved["saved_at"] == run.saved_at.isoformat()
    detail = client.get(reverse("exam_timetable_detail", args=[run.pk])).json()
    assert detail["run_revision"] == 1
    assert detail["saved_at"] == run.saved_at.isoformat()
    assert _board(detail) == _board(saved)


def test_each_save_names_the_version_it_was_made_on(changed):
    client, source, payload = changed
    first = save(client, payload)
    payload["run_revision"] = first["run_revision"]
    second = save(client, payload)
    assert (second["run_id"], second["run_revision"]) == (source["run_id"], 2)
    assert ExamTimetableRun.objects.get(pk=source["run_id"]).revision == 2


def test_a_save_made_on_an_older_version_is_refused_and_writes_nothing(changed):
    """Two pages opened the same version; the second Save must not undo the first."""
    client, source, payload = changed
    first = save(client, payload)
    after_first = ExamTimetableRun.objects.get(pk=source["run_id"])
    count = ExamTimetableRun.objects.count()

    payload["label"] = "Other page"
    refused = save(client, payload, 409)

    assert refused["error_code"] == "save_target_changed"
    assert "saved again after you opened it" in refused["error"]
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    assert (run.revision, run.label, run.result_json, run.saved_at) == (
        first["run_revision"],
        after_first.label,
        after_first.result_json,
        after_first.saved_at,
    )
    assert ExamTimetableRun.objects.count() == count
    assert len(_overwrites()) == 1


def test_a_version_saved_while_the_save_was_evaluated_is_not_overwritten(changed, monkeypatch):
    """The revision is checked at the write, not only when the request arrives."""
    client, source, payload = changed
    from core import exam_views

    real = exam_views.evaluate_exam_schedule

    def saved_meanwhile(*args, **kwargs):
        result = real(*args, **kwargs)
        ExamTimetableRun.objects.filter(pk=source["run_id"]).update(revision=1, label="Theirs")
        return result

    monkeypatch.setattr(exam_views, "evaluate_exam_schedule", saved_meanwhile)
    refused = save(client, payload, 409)
    assert refused["error_code"] == "save_target_changed"
    assert ExamTimetableRun.objects.get(pk=source["run_id"]).label == "Theirs"
    assert _overwrites() == []


def test_a_timetable_deleted_while_the_save_was_evaluated_is_not_recreated(changed, monkeypatch):
    client, source, payload = changed
    from core import exam_views

    real = exam_views.evaluate_exam_schedule

    def deleted_meanwhile(*args, **kwargs):
        result = real(*args, **kwargs)
        ExamTimetableRun.objects.filter(pk=source["run_id"]).delete()
        return result

    monkeypatch.setattr(exam_views, "evaluate_exam_schedule", deleted_meanwhile)
    count = ExamTimetableRun.objects.count()
    refused = save(client, payload, 410)
    assert refused["error_code"] == "save_target_gone"
    assert ExamTimetableRun.objects.count() == count - 1
    assert not ExamTimetableRun.objects.filter(pk=source["run_id"]).exists()


@pytest.mark.parametrize("revision", ["missing", None, True, -1, "0", 1.0])
def test_a_save_that_cannot_name_its_version_is_refused_before_any_work(
    changed, monkeypatch, revision
):
    """A page from before this release sends no revision; nothing is evaluated."""
    client, source, payload = changed
    if revision == "missing":
        del payload["run_revision"]
    else:
        payload["run_revision"] = revision
    before = ExamTimetableRun.objects.get(pk=source["run_id"]).result_json

    def forbidden(*args, **kwargs):
        raise AssertionError("A Save that cannot name its version was evaluated")

    monkeypatch.setattr("core.exam_views.evaluate_exam_schedule", forbidden)
    refused = save(client, payload, 409)
    assert refused["error_code"] == "save_target_changed"
    assert ExamTimetableRun.objects.get(pk=source["run_id"]).result_json == before


# ── the record, the history and what reads a saved run ─────────────


def test_each_overwrite_is_audited_with_the_board_it_replaced(changed):
    client, source, payload = changed
    before = ExamTimetableRun.objects.get(pk=source["run_id"])
    save(client, payload)
    after = ExamTimetableRun.objects.get(pk=source["run_id"])

    row = AuditLog.objects.get(action=OVERWRITE_ACTION)
    assert row.actor_username == "fixed-editor"
    assert row.endpoint == reverse("exam_timetable_build")
    assert json.loads(row.details_json) == {
        "run_id": source["run_id"],
        "label": "Manual changes",
        "previous_label": before.label,
        "revision": 1,
        "previous_revision": 0,
        "result_sha256": _sha(after.result_json),
        "previous_result_sha256": _sha(before.result_json),
    }


def test_the_history_shows_one_row_per_timetable_last_saved_first(changed):
    client, source, payload = changed
    later = ExamTimetableRun.objects.create(label="Built later", result_json="{}")
    listed = client.get(reverse("exam_timetable_list")).json()["runs"]
    assert [row["id"] for row in listed] == [later.pk, source["run_id"]]

    save(client, payload)
    save(client, {**payload, "run_revision": 1})

    listed = client.get(reverse("exam_timetable_list")).json()["runs"]
    assert [row["id"] for row in listed] == [source["run_id"], later.pk]
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    assert listed[0]["saved_at"] == run.saved_at.isoformat()
    assert listed[0]["created_at"] == run.created_at.isoformat()


def test_cached_student_lists_are_not_served_for_the_version_a_save_replaced(changed):
    client, source, payload = changed
    before = RosterViewCache.key(ExamTimetableRun.objects.get(pk=source["run_id"]))
    save(client, payload)
    after = RosterViewCache.key(ExamTimetableRun.objects.get(pk=source["run_id"]))
    assert after[0] == before[0]
    assert after != before


def test_every_other_action_still_saves_a_new_timetable(changed):
    """Only Save changes rewrites; Fix keeps its own new run beside the source."""
    client, source, payload = changed
    count = ExamTimetableRun.objects.count()
    repaired = client.post(
        reverse("exam_timetable_build"),
        {**payload, "mode": "minimum_change_repair"},
        content_type="application/json",
    ).json()
    assert repaired["ok"], repaired
    assert repaired["run_id"] != source["run_id"]
    assert repaired["run_revision"] == 0
    assert ExamTimetableRun.objects.count() == count + 1
    assert ExamTimetableRun.objects.get(pk=source["run_id"]).revision == 0


# ── the same, run as a background job ───────────────────────────────


def _job_save_payload(built, board, revision):
    return _loaded_payload(
        built,
        board,
        "save_loaded_changes",
        expected_input_fingerprint=built["input_fingerprint"],
        run_revision=revision,
    )


@pytest.fixture
def built(export_client, clashing_pair, jobs):  # noqa: F811
    metadata, programming, companion = clashing_pair
    jobs.EXAM_JOBS_ENABLED = False
    built = _post(export_client, _build_payload(metadata), 200)
    jobs.EXAM_JOBS_ENABLED = True
    board = _drag(built["schedule"], companion, onto=programming)
    return export_client, built, board


def test_a_save_run_as_a_job_rewrites_the_opened_run(built):
    client, source, board = built
    count = ExamTimetableRun.objects.count()
    job_id, status, body = _run_as_job(client, _job_save_payload(source, board, 0))

    assert status == 200, body
    assert (body["run_id"], body["run_revision"]) == (source["run_id"], 1)
    assert ExamTimetableRun.objects.count() == count
    assert ExamTimetableJob.objects.get(pk=job_id).result_run_id == source["run_id"]
    [audited] = _overwrites()
    assert audited["job_id"] == job_id
    assert AuditLog.objects.get(action=OVERWRITE_ACTION).actor_username == "export-source-admin"


def test_a_job_save_made_on_an_older_version_ends_refused_and_writes_nothing(built):
    client, source, board = built
    _run_as_job(client, _job_save_payload(source, board, 0))
    saved = ExamTimetableRun.objects.get(pk=source["run_id"])

    job_id, status, body = _run_as_job(client, _job_save_payload(source, source["schedule"], 0))

    assert (status, body["error_code"]) == (409, "save_target_changed")
    job = ExamTimetableJob.objects.get(pk=job_id)
    assert job.result_run_id is None
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    assert (run.revision, run.result_json) == (saved.revision, saved.result_json)


def test_a_cancel_as_a_job_saves_rolls_the_overwrite_back(built, monkeypatch):
    """The rewritten run and the job's SUCCEEDED commit together or not at all."""
    client, source, board = built
    real_stage = JobProgress.stage

    def cancel_at_save(self, key):
        result = real_stage(self, key)
        if key == "save":
            ExamTimetableJob.objects.filter(status=ExamTimetableJob.STATUS_RUNNING).update(
                cancel_requested=True
            )
        return result

    monkeypatch.setattr(JobProgress, "stage", cancel_at_save)
    before = ExamTimetableRun.objects.get(pk=source["run_id"])
    submitted = _post(client, _job_save_payload(source, board, 0), 202)

    assert _poll(client, submitted["job"]["id"])["status"] == "cancelled"
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    assert (run.revision, run.result_json, run.saved_at) == (0, before.result_json, before.saved_at)
    assert _overwrites() == []


def test_a_finished_jobs_answer_names_the_runs_current_version(built):
    """A page opening a job's result later is given the revision a Save must name."""
    client, source, board = built
    job_id, _, _ = _run_as_job(client, _job_save_payload(source, board, 0))
    ExamTimetableRun.objects.filter(pk=source["run_id"]).update(revision=5)
    status, body = exam_jobs.result(job_id, user=None)
    assert (status, body["run_revision"]) == (200, 5)
    run = ExamTimetableRun.objects.get(pk=source["run_id"])
    assert body["saved_at"] == run.saved_at.isoformat()


def test_the_previous_release_can_still_save_runs_while_a_deploy_overlaps():
    """It inserts runs without the new columns; the database fills them."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO exam_timetable_runs (label, created_at, result_json) VALUES (%s, %s, %s)",
            ["Previous release", timezone.now(), "{}"],
        )
    run = ExamTimetableRun.objects.get(label="Previous release")
    assert run.revision == 0
    assert run.saved_at is not None
