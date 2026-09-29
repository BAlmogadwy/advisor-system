"""Deploying Add courses changes no saved timetable.

Render runs ``normalise_exam_runs`` before every deploy, and the saved exam
timetables online are the department's real work. Add courses bumps no version
that would rewrite or re-fingerprint a run: its report is a top-level key only
the runs it saves carry, and its migration changes a job's choices, no SQL.
"""

from __future__ import annotations

import io
import json

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import migrations
from django.urls import reverse

from core import models
from core.models import ExamTimetableJob, ExamTimetableRun
from core.services import exam_jobs
from core.services.exam_input_fingerprint import EXAM_INPUT_POLICY_VERSION
from core.services.exam_room_allocation import ROOM_ALLOCATION_POLICY_VERSION
from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    STATUS_DERIVATION_VERSION,
    load_normalised_run,
)
from core.services.exam_timetable import build_enrolled_sets_with_meta
from tests import exam_linked_parity_corpus as corpus

SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}


def test_no_version_that_rewrites_or_refingerprints_saved_runs_was_bumped():
    assert EXAM_RUN_SCHEMA_VERSION == 6
    assert STATUS_DERIVATION_VERSION == 3
    assert EXAM_INPUT_POLICY_VERSION == 5
    assert ROOM_ALLOCATION_POLICY_VERSION == 2


def test_the_migration_only_adds_a_job_kind():
    module = __import__("core.migrations.0075_exam_job_kind_add_courses", fromlist=["Migration"])
    (operation,) = module.Migration.operations
    assert isinstance(operation, migrations.AlterField)
    assert operation.model_name == "examtimetablejob" and operation.name == "kind"
    assert ("add_courses", "Add courses") in operation.field.choices
    # The model says the same: no migration is missing.
    assert list(operation.field.choices) == list(ExamTimetableJob.KIND_CHOICES)
    assert set(exam_jobs.STAGES) == {kind for kind, _ in ExamTimetableJob.KIND_CHOICES}


@pytest.mark.django_db
def test_runs_with_and_without_an_add_report_normalise_without_a_byte_changed(
    client, django_user_model, monkeypatch
):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="deploy-admin"))
    corpus.create_population(models)
    _, metadata = build_enrolled_sets_with_meta(**SCOPE)
    codes = sorted(metadata)
    built = client.post(
        reverse("exam_timetable_build"),
        {
            "label": "Deploy",
            "days": corpus.POPULATION_DAYS,
            "periods": corpus.POPULATION_PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "selected_courses": codes[:6],
            "selected_course_entries": [{"course_code": c, **metadata[c]} for c in codes[:6]],
            "pinned": [],
        },
        content_type="application/json",
    ).json()
    board = [
        {
            key: entry[key]
            for key in ("course_code", "course_name", "course_identity", "day", "period")
        }
        for entry in built["schedule"]
    ]
    added = client.post(
        reverse("exam_timetable_build"),
        {
            "label": "Deploy + rest",
            "days": corpus.POPULATION_DAYS,
            "periods": corpus.POPULATION_PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "mode": "add_courses",
            "previous_run_id": built["run_id"],
            "base_schedule": board,
            "pinned": [],
            "linked_exams": [],
            "added_courses": [
                {"course_identity": metadata[c]["course_identity"]} for c in codes[6:]
            ],
        },
        content_type="application/json",
    ).json()
    assert added["ok"] is True, added
    before = {run.pk: run.result_json for run in ExamTimetableRun.objects.order_by("id")}
    assert len(before) == 2
    assert "add_courses" not in json.loads(before[built["run_id"]])

    dry = io.StringIO()
    call_command("normalise_exam_runs", "--dry-run", stdout=dry)
    assert "No rows needed migration" in dry.getvalue()
    call_command("normalise_exam_runs", stdout=io.StringIO())
    after = {run.pk: run.result_json for run in ExamTimetableRun.objects.order_by("id")}
    assert after == before

    extended = load_normalised_run(ExamTimetableRun.objects.get(pk=added["run_id"]))
    assert extended["add_courses"] == json.loads(before[added["run_id"]])["add_courses"]
    assert extended["rebuild_mode"] == "added_courses_from_loaded"
    plain = load_normalised_run(ExamTimetableRun.objects.get(pk=built["run_id"]))
    assert "add_courses" not in plain
