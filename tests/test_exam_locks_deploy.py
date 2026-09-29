"""Deploying exam locks changes no saved timetable.

Render runs ``normalise_exam_runs`` before every deploy. It rewrites each saved
run whose ``schema_version`` is below ``EXAM_RUN_SCHEMA_VERSION``, and the
saved exam timetables online are the department's real work. So locks were
added WITHOUT a schema bump: ``exam_locks`` and ``qa.exam_locks`` exist only on
a run that has locks, a run without them reads as "no locks", and nothing is
ever filled in for it. These tests pin that contract.
"""

import io
import itertools
import json

import pytest
from django.core.management import call_command

from core import models
from core.models import ExamTimetableRun
from core.services.exam_evaluation import evaluate_exam_schedule
from core.services.exam_input_fingerprint import EXAM_INPUT_POLICY_VERSION
from core.services.exam_room_allocation import ROOM_ALLOCATION_POLICY_VERSION
from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    STATUS_DERIVATION_VERSION,
    derive_status_surface,
    load_normalised_run,
)
from core.services.exam_timetable import build_exam_timetable
from tests import exam_linked_parity_corpus as corpus

DAYS = corpus.POPULATION_DAYS
PERIODS = corpus.POPULATION_PERIODS


def test_no_version_that_rewrites_or_refingerprints_saved_runs_was_bumped():
    assert EXAM_RUN_SCHEMA_VERSION == 6, (
        "Bumping the run schema makes the pre-deploy normalise_exam_runs rewrite every "
        "saved exam timetable, online ones included. Locks must stay readable without it."
    )
    assert STATUS_DERIVATION_VERSION == 3, (
        "Bumping the status rules re-derives every saved run's headline on read."
    )
    assert EXAM_INPUT_POLICY_VERSION == 5, (
        "Bumping the input policy changes every saved run's input fingerprint: every "
        "loaded timetable would read as changed and need a Check before Save."
    )
    assert ROOM_ALLOCATION_POLICY_VERSION == 2, (
        "Bumping the room policy changes every saved run's input fingerprint."
    )


def _save(label: str, result: dict) -> ExamTimetableRun:
    return ExamTimetableRun.objects.create(
        label=label, result_json=json.dumps(result, ensure_ascii=False)
    )


@pytest.mark.django_db
def test_normalising_runs_with_and_without_locks_changes_no_byte():
    corpus.create_population(models)
    built = build_exam_timetable(
        "Deploy", DAYS, PERIODS, programs=["AI", "CS"], sections=["F", "M"], persist=True
    )
    source = dict(load_normalised_run(ExamTimetableRun.objects.get(pk=built["run_id"])))
    busy = next(day for day in DAYS if any(entry["day"] == day for entry in source["schedule"]))
    locked = evaluate_exam_schedule(
        days=DAYS,
        periods=PERIODS,
        max_per_day=2,
        schedule_raw=source["schedule"],
        selected_courses=[entry["course_code"] for entry in source["schedule"]],
        assign_rooms=True,
        seed=None,
        thin_conflict_threshold=0,
        programs=["AI", "CS"],
        sections=["F", "M"],
        pinned=[],
        linked_exams=[],
        exam_locks=[{"day": busy}],
        lock_source=source,
    )
    assert locked["exam_locks"] == [{"day": busy}]
    assert locked["qa"]["exam_locks"]["locked_courses"] >= 1
    # A lock report that would ask for room action, as a later Check could save.
    locked["qa"]["exam_locks"]["issues"].append({"kind": "room_unavailable", "room_code": "gone"})
    with_locks = _save("Deploy with locks", locked)
    runs = list(ExamTimetableRun.objects.order_by("id"))
    assert len(runs) == 2
    before = {run.pk: run.result_json for run in runs}
    assert all(json.loads(text)["schema_version"] == 6 for text in before.values())
    assert "exam_locks" not in json.loads(before[built["run_id"]])

    dry = io.StringIO()
    call_command("normalise_exam_runs", "--dry-run", stdout=dry)
    assert "No rows needed migration" in dry.getvalue()
    report = io.StringIO()
    call_command("normalise_exam_runs", stdout=report)
    assert "No rows needed migration" in report.getvalue()
    assert f"Skipped 2 row(s) already at v{EXAM_RUN_SCHEMA_VERSION}" in report.getvalue()
    after = {run.pk: run.result_json for run in ExamTimetableRun.objects.order_by("id")}
    assert after == before

    # And reading them adds nothing: the run without locks gains no lock key.
    plain = load_normalised_run(ExamTimetableRun.objects.get(pk=built["run_id"]))
    assert "exam_locks" not in plain and "exam_locks" not in plain["qa"]
    assert load_normalised_run(with_locks)["exam_locks"] == [{"day": busy}]


# ── the status rules, on every payload without a lock report ────────────────


def _status_payloads():
    """Every combination of the signals ``derive_status_surface`` reads."""
    for (
        overflow,
        unassigned,
        incomplete,
        manual,
        bucket,
        mapping,
        over_limit,
        heavy,
        thin,
        legacy,
    ) in itertools.product((0, 1), repeat=10):
        qa = {
            "rooms": {"unassigned_room_sections": [{"course_code": "A"}] * unassigned},
            "multi_sitting_sections": incomplete,
            "multi_sitting_details": [{"incomplete": True}] * incomplete,
            "bucket_day_violations": [{"courses": ["A", "B"]}] * bucket,
            "section_mapping": {"missing_enrollments": mapping, "ambiguous_enrollments": 0},
            "students_over_limit_per_day": over_limit,
            "heavy_day_students": heavy,
            "thin_clash_risk": [{"student_id": 1}] * thin,
        }
        if legacy:
            qa["conflict_count"] = manual
        else:
            qa["manual_override_count"] = manual
        yield {
            "status": "ok",
            "schedule": [{"day": "OVERFLOW"}] * overflow + [{"day": "Sun"}],
            "qa": qa,
        }
    for status in ("feasibility_error", "unrenderable", "future_version_unrenderable"):
        yield {"status": status}
    yield {"status": "ok", "qa": None, "schedule": None}


def _statuses() -> list:
    return [
        [*derive_status_surface(payload, source_schema_version=version)]
        for payload in _status_payloads()
        for version in (None, 5, 6)
    ]


#: ``_statuses()`` digested on origin/master (ae056bd), before locks existed,
#: under two PYTHONHASHSEED values, which agreed: 3,084 (payload, version) pairs.
MASTER_STATUSES = "143e8d85ce2d55ad5ddcb5a67c75e4cb5e69aa486042ccfc5e328941a591cbdd"


def test_every_status_without_a_lock_report_is_masters():
    assert corpus.digest(_statuses()) == MASTER_STATUSES
