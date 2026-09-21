"""Soft workload limits remain scheduling preferences and visible QA warnings."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    STATUS_DERIVATION_VERSION,
    derive_status_surface,
    load_normalised_run,
    normalise_exam_run_payload,
)


def _payload(**qa):
    return {
        "schema_version": EXAM_RUN_SCHEMA_VERSION,
        "status": "ok",
        "primary_status": "clean",
        "status_flags": [],
        "status_derivation_version": 1,
        "schedule": [{"course_code": "CS111", "slot_index": 0, "day": "Sun", "period": "P1"}],
        "qa": {
            "manual_override_count": 0,
            "students_over_limit_per_day": 0,
            "heavy_day_students": 0,
            **qa,
        },
    }


@pytest.mark.parametrize(
    "qa, expected_flags",
    [
        ({"students_over_limit_per_day": 15}, {"daily_limit_exceeded"}),
        ({"heavy_day_students": 2}, {"heavy_credit_day"}),
        (
            {"students_over_limit_per_day": 15, "heavy_day_students": 2},
            {"daily_limit_exceeded", "heavy_credit_day"},
        ),
    ],
)
def test_measured_soft_limit_violations_are_workload_warnings(qa, expected_flags):
    payload = _payload(max_per_day=2, max_exams_per_day_per_student=3, **qa)
    original = deepcopy(payload)
    primary, flags = derive_status_surface(payload)
    assert primary == "contains_workload_warnings"
    assert set(flags) == expected_flags
    assert payload == original


def test_no_workload_warnings_when_counts_are_zero():
    assert derive_status_surface(_payload()) == ("clean", [])


def test_workload_warning_precedes_approved_thin_clashes():
    primary, flags = derive_status_surface(
        _payload(students_over_limit_per_day=1, thin_clash_risk=[{"student_id": 1}])
    )
    assert primary == "contains_workload_warnings"
    assert set(flags) == {"daily_limit_exceeded", "approved_thin_conflicts"}


def test_section_review_precedes_workload_and_thin_clashes_without_hiding_their_flags():
    primary, flags = derive_status_surface(
        _payload(
            students_over_limit_per_day=1,
            heavy_day_students=1,
            thin_clash_risk=[{"student_id": 1}],
            section_mapping={"missing_enrollments": 1},
        )
    )
    assert primary == "requires_section_review"
    assert set(flags) == {
        "section_mapping_incomplete",
        "daily_limit_exceeded",
        "heavy_credit_day",
        "approved_thin_conflicts",
    }


@pytest.mark.parametrize(
    "issue, expected_primary, expected_flag",
    [
        ({"overflow": True}, "contains_overflow", "overflow"),
        (
            {"rooms": {"unassigned_room_sections": [{}]}},
            "requires_room_action",
            "room_action_required",
        ),
        ({"manual_override_count": 1}, "contains_manual_override", "manual_override"),
    ],
)
def test_hard_problems_keep_precedence_and_workload_flags(issue, expected_primary, expected_flag):
    payload = _payload(students_over_limit_per_day=1, heavy_day_students=1, **issue)
    if issue.get("overflow"):
        payload["schedule"][0]["day"] = "OVERFLOW"
    primary, flags = derive_status_surface(payload)
    assert primary == expected_primary
    assert {expected_flag, "daily_limit_exceeded", "heavy_credit_day"} <= set(flags)


@pytest.mark.parametrize("stored_version", [None, 0, 1, 2])
def test_loading_saved_run_rederives_outdated_headline_from_existing_qa(stored_version):
    payload = _payload(students_over_limit_per_day=15)
    if stored_version is None:
        payload.pop("status_derivation_version")
    else:
        payload["status_derivation_version"] = stored_version
    run = SimpleNamespace(result_json=json.dumps(payload))
    stored_json = run.result_json
    loaded = load_normalised_run(run)
    assert loaded["primary_status"] == "contains_workload_warnings"
    assert loaded["status_flags"] == ["daily_limit_exceeded"]
    assert loaded["status_derivation_version"] == STATUS_DERIVATION_VERSION
    assert loaded["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert loaded["schedule"] == payload["schedule"]
    assert run.result_json == stored_json
    assert normalise_exam_run_payload(loaded) == loaded


@pytest.mark.parametrize("gap", ["missing_enrollments", "ambiguous_enrollments"])
def test_current_saved_qa_rederives_section_review_without_rewriting_the_run(gap):
    payload = _payload(section_mapping={gap: 100})
    payload["status_derivation_version"] = 2
    run = SimpleNamespace(result_json=json.dumps(payload))
    original_json = run.result_json
    loaded = load_normalised_run(run)
    assert loaded["primary_status"] == "requires_section_review"
    assert loaded["status_flags"] == ["section_mapping_incomplete"]
    assert loaded["status_derivation_version"] == STATUS_DERIVATION_VERSION
    assert loaded["schedule"] == payload["schedule"]
    assert loaded["qa"]["section_mapping"] == {gap: 100}
    assert run.result_json == original_json
    assert normalise_exam_run_payload(loaded) == loaded


def test_newer_status_derivation_is_not_downgraded():
    payload = _payload(students_over_limit_per_day=1)
    payload["primary_status"] = "future_policy"
    payload["status_flags"] = ["future_flag"]
    payload["status_derivation_version"] = STATUS_DERIVATION_VERSION + 1
    loaded = normalise_exam_run_payload(payload)
    assert loaded["primary_status"] == "future_policy"
    assert loaded["status_flags"] == ["future_flag"]
    assert loaded["status_derivation_version"] == STATUS_DERIVATION_VERSION + 1
