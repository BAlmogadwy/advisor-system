"""Manual checks evaluate exact placements and guard their authoritative inputs."""

import json
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
    StudentCourse,
    StudentTermSection,
)
from core.services.exam_timetable import apply_thin_conflict_policy, build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db
DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "13:00-15:00"]


@pytest.fixture
def editor(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="fixed-editor"))
    for sid in [1, 2]:
        Student.objects.create(student_id=sid, program="AI", section="F")
    for code, credits, term in [("CS101", 4, 1), ("CS102", 4, 2), ("CS103", 2, 1)]:
        course = Course.objects.create(course_code=code, description=code, credit_hours=credits)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=code, programme_term=term
        )
        for sid in [1] if code == "CS103" else [1, 2]:
            StudentCourse.objects.create(student_id=sid, course=course, status="studying")
            scraped_exam_registration(sid, course)
    Room.objects.create(room_code="F001", capacity=10, section="F", building="B1", floor="1")
    source = build_exam_timetable(
        label="Fixed evaluation",
        days=DAYS,
        periods=PERIODS,
        programs=["AI", "DS"],
        sections=["F", "M"],
        seed=17,
    )
    payload = {
        "label": "Manual changes",
        "previous_run_id": source["run_id"],
        "base_schedule": deepcopy(source["schedule"]),
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 1,
        "selected_courses": source["courses"],
        "assign_rooms": True,
        "thin_conflict_threshold": 1,
        "pinned": [],
        "editor_revision": 5,
    }
    return client, source, payload


def check(client, payload, status=200):
    response = client.post(
        reverse("exam_timetable_draft_impact"), payload, content_type="application/json"
    )
    assert response.status_code == status, response.content
    return response.json()


def save(client, payload, status=200):
    response = client.post(
        reverse("exam_timetable_build"),
        {**payload, "mode": "save_loaded_changes"},
        content_type="application/json",
    )
    assert response.status_code == status, response.content
    return response.json()


def placements(result):
    return {
        row["course_identity"]: (row["day"], row["period"], row["slot_index"])
        for row in result["schedule"]
    }


def test_check_and_save_never_schedule_and_calculate_all_changed_cards(editor, monkeypatch):
    client, source, payload = editor
    for row in payload["base_schedule"]:
        row.update(day="Sun", period=PERIODS[0], slot_index=0)

    def forbidden(*args, **kwargs):
        raise AssertionError("A manual evaluation attempted optimization")

    monkeypatch.setattr("core.exam_views.schedule", forbidden)
    monkeypatch.setattr("core.services.exam_timetable.schedule", forbidden)
    monkeypatch.setattr("core.services.exam_timetable._rebalance_invigilators_pass", forbidden)
    # The evaluator imports the pass into its own namespace, so patching only
    # exam_timetable would leave this guard unable to see a Check that rebalanced.
    monkeypatch.setattr("core.services.exam_evaluation._rebalance_invigilators_pass", forbidden)
    count = ExamTimetableRun.objects.count()
    result = check(client, payload)
    assert ExamTimetableRun.objects.count() == count
    assert "run_id" not in result
    assert result["editor_revision"] == 5
    assert result["seed"] == source["seed"] == 17
    assert placements(result) == placements({"schedule": payload["base_schedule"]})
    qa = result["qa"]
    assert qa["conflict_count"] == 2
    assert qa["students_over_limit_per_day"] == 2
    assert qa["heavy_day_students"] == 2
    assert qa["bucket_day_violations_count"] == 1
    assert qa["schedule_violation_count"] == 3
    assert len(qa["thin_clash_risk"]) == 1
    assert qa["thin_clash_risk"][0]["student_id"] == 1
    assert len(qa["rooms"]["unassigned_room_sections"]) == 2
    assert qa["rooms"]["invigilators_total"] == 1
    assert qa["building_footprint"]["buildings_used_per_slot"]
    assert result["primary_status"] == "requires_room_action"
    payload["expected_input_fingerprint"] = result["input_fingerprint"]
    saved = save(client, payload)
    assert ExamTimetableRun.objects.count() == count + 1
    assert placements(saved) == placements(result)
    assert saved["qa"]["rooms"] == result["qa"]["rooms"]
    assert saved["input_fingerprint"] == result["input_fingerprint"]
    assert saved["pinned"] == []


def test_room_assignment_disabled_is_inherited_and_never_silently_enabled(editor, monkeypatch):
    client, source, payload = editor
    payload["assign_rooms"] = False
    payload["expected_input_fingerprint"] = check(client, payload)["input_fingerprint"]
    saved = save(client, payload)
    payload["previous_run_id"] = saved["run_id"]
    del payload["assign_rooms"]

    def forbidden():
        raise AssertionError("Disabled room assignment queried inventory")

    monkeypatch.setattr("core.services.exam_evaluation._rooms_with_metadata", forbidden)
    result = check(client, payload)
    assert result["assign_rooms"] is False
    assert result["rooms_count"] == 0
    assert result["qa"]["rooms"]["rooms_used"] == 0
    assert all(row["rooms"] == [] for row in result["schedule"])
    assert result["seed"] == source["seed"]


@pytest.mark.parametrize(
    "mutation",
    ["enrollment", "credits", "capacity", "building", "section", "program", "online", "bucket"],
)
def test_save_rejects_changed_authoritative_inputs_until_rechecked(editor, mutation):
    client, _, payload = editor
    result = check(client, payload)
    payload["expected_input_fingerprint"] = result["input_fingerprint"]
    if mutation == "enrollment":
        Student.objects.create(student_id=3, program="AI", section="F")
        StudentCourse.objects.create(
            student_id=3, course=Course.objects.get(course_code="CS101"), status="studying"
        )
        scraped_exam_registration(3, Course.objects.get(course_code="CS101"))
    elif mutation == "credits":
        Course.objects.filter(course_code="CS101").update(credit_hours=2)
    elif mutation == "capacity":
        Room.objects.update(capacity=1)
    elif mutation == "building":
        Room.objects.update(building="New building")
    elif mutation == "section":
        Student.objects.filter(student_id=1).update(section="M")
        StudentTermSection.objects.filter(student_id=1).delete()
        for course in Course.objects.all():
            scraped_exam_registration(1, course)
    elif mutation == "program":
        Student.objects.filter(student_id=1).update(program="DS")
    elif mutation == "online":
        ProgrammeRequirement.objects.filter(course_code="CS101").update(is_online=True)
    else:
        ProgrammeRequirement.objects.filter(course_code="CS101").update(programme_term=3)
    count = ExamTimetableRun.objects.count()
    rejected = save(client, payload, 409)
    assert rejected["error_code"] == "inputs_changed"
    assert ExamTimetableRun.objects.count() == count
    refreshed = check(client, payload)
    assert refreshed["input_fingerprint"] != result["input_fingerprint"]
    payload["expected_input_fingerprint"] = refreshed["input_fingerprint"]
    assert save(client, payload)["input_fingerprint"] == refreshed["input_fingerprint"]


def test_input_fingerprint_is_stable_after_moves_and_row_order_changes(editor):
    client, _, payload = editor
    initial = check(client, payload)
    payload["base_schedule"].reverse()
    for row in payload["base_schedule"]:
        row.update(day="Tue", period=PERIODS[1], slot_index=5)
    moved = check(client, payload)
    assert moved["input_fingerprint"] == initial["input_fingerprint"]
    assert moved["qa"]["conflict_count"] > initial["qa"]["conflict_count"]


def test_overflow_with_empty_period_is_preserved_until_explicitly_moved(editor):
    client, _, payload = editor
    overflow = payload["base_schedule"][0]
    overflow.update(day="OVERFLOW", period="", slot_index=len(DAYS) * len(PERIODS))
    result = check(client, payload)
    assert result["courses_count"] == 3
    assert result["primary_status"] == "contains_overflow"
    assert placements(result)[overflow["course_identity"]] == ("OVERFLOW", "", 6)
    payload["expected_input_fingerprint"] = result["input_fingerprint"]
    assert placements(save(client, payload)) == placements(result)
    overflow.update(day="Tue", period=PERIODS[1], slot_index=5)
    assert "overflow" not in check(client, payload)["status_flags"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("assign_rooms", "false"),
        ("thin_conflict_threshold", True),
        ("thin_conflict_threshold", 11),
        ("editor_revision", -1),
        ("editor_revision", True),
        ("expected_input_fingerprint", "invalid"),
        ("selected_courses", []),
        ("selected_course_entries", []),
        ("base_schedule", []),
        ("base_schedule", [None]),
        ("periods", ["25:00-26:00"]),
        ("pinned", "CS101"),
    ],
)
def test_malformed_manual_input_is_never_silently_coerced(editor, field, value):
    client, _, payload = editor
    payload[field] = value
    count = ExamTimetableRun.objects.count()
    assert not check(client, payload, 400)["ok"]
    assert ExamTimetableRun.objects.count() == count


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing_period",
        "bad_slot",
        "foreign_identity",
        "pin_disagrees",
        "pin_identity",
    ],
)
def test_invalid_course_or_pin_placements_are_explicit_errors(editor, mutation):
    client, _, payload = editor
    row = payload["base_schedule"][0]
    if mutation == "duplicate":
        payload["base_schedule"].append(deepcopy(row))
    elif mutation == "missing_period":
        row["period"] = ""
    elif mutation == "bad_slot":
        row["day"] = "Unknown day"
    elif mutation == "foreign_identity":
        row["course_identity"] = "UNKNOWN"
    else:
        pin = {key: row[key] for key in ["course_code", "day", "period", "course_identity"]}
        if mutation == "pin_disagrees":
            pin["period"] = next(period for period in PERIODS if period != pin["period"])
        else:
            pin["course_identity"] = "WRONG"
        payload["pinned"] = [pin]
    assert not check(client, payload, 400)["ok"]


def test_thin_policy_does_not_mutate_full_graph_or_lose_mutual_edges():
    graph = {"A": {"B": 1, "C": 1}, "B": {"A": 1}, "C": {"A": 1}}
    original = deepcopy(graph)
    relaxed, report = apply_thin_conflict_policy({"A": {1}, "B": {1}, "C": {1, 2}}, graph, 1)
    assert graph == original
    assert relaxed == {"A": {}, "B": {}, "C": {}}
    assert report == [
        {"course_code": "A", "total_students": 1, "dropped_edges": 2, "neighbours": ["B", "C"]},
        {"course_code": "B", "total_students": 1, "dropped_edges": 1, "neighbours": ["A"]},
    ]


def test_build_and_check_share_captured_input_baseline_and_omission_cannot_bypass_drift(editor):
    client, source, payload = editor
    payload.update(max_per_day=2, thin_conflict_threshold=0)
    baseline = check(client, payload)
    assert baseline["input_fingerprint"] == source["input_fingerprint"]
    assert baseline["source_input_fingerprint"] == source["input_fingerprint"]
    assert baseline["source_inputs_changed"] is False
    # An omitted expected hash can safely use unchanged source provenance.
    assert save(client, payload)["input_fingerprint"] == source["input_fingerprint"]
    Student.objects.create(student_id=4, program="AI", section="F")
    StudentCourse.objects.create(
        student_id=4, course=Course.objects.get(course_code="CS101"), status="studying"
    )
    scraped_exam_registration(4, Course.objects.get(course_code="CS101"))
    count = ExamTimetableRun.objects.count()
    assert save(client, payload, 409)["error_code"] == "inputs_changed"
    assert ExamTimetableRun.objects.count() == count
    refreshed = check(client, payload)
    assert refreshed["source_inputs_changed"] is True
    payload["expected_input_fingerprint"] = refreshed["input_fingerprint"]
    assert save(client, payload)["students_count"] == 3


def test_source_without_baseline_requires_explicit_reviewed_check_before_save(editor):
    client, source, payload = editor
    stored = ExamTimetableRun.objects.get(pk=source["run_id"])
    data = json.loads(stored.result_json)
    del data["input_fingerprint"]
    stored.result_json = json.dumps(data)
    stored.save(update_fields=["result_json"])
    assert save(client, payload, 409)["error_code"] == "check_required"
    checked = check(client, payload)
    assert checked["source_input_fingerprint"] is None
    assert checked["source_inputs_changed"] is None
    payload["expected_input_fingerprint"] = checked["input_fingerprint"]
    assert save(client, payload)["input_fingerprint"] == checked["input_fingerprint"]


def test_display_code_cannot_be_changed_while_reusing_valid_identity(editor):
    client, _, payload = editor
    row = next(row for row in payload["base_schedule"] if row["course_code"] == "CS101")
    row["course_code"] = "GS999"
    payload["selected_courses"] = [row["course_code"] for row in payload["base_schedule"]]
    rejected = check(client, payload, 400)
    assert "course code or identity" in rejected["error"]


@pytest.mark.parametrize("hard_kind", [None, "nonthin", "bucket"])
def test_approved_thin_clashes_are_distinct_from_hard_schedule_violations(editor, hard_kind):
    client, _, payload = editor
    # One student, two 3-credit exams in different plan buckets.
    StudentCourse.objects.filter(student_id=2).delete()
    StudentTermSection.objects.filter(student_id=2).delete()
    Course.objects.update(credit_hours=3)
    payload["base_schedule"] = [
        row for row in payload["base_schedule"] if row["course_code"] in ["CS101", "CS102"]
    ]
    payload["selected_courses"] = [row["course_code"] for row in payload["base_schedule"]]
    payload.update(max_per_day=2, assign_rooms=False)
    for row in payload["base_schedule"]:
        row.update(day="Sun", period=PERIODS[0], slot_index=0)
    if hard_kind == "nonthin":
        for code in payload["selected_courses"]:
            StudentCourse.objects.create(
                student_id=2, course=Course.objects.get(course_code=code), status="studying"
            )
            scraped_exam_registration(2, Course.objects.get(course_code=code))
    elif hard_kind == "bucket":
        ProgrammeRequirement.objects.filter(course_code="CS102").update(programme_term=1)
    checked = check(client, payload)
    qa = checked["qa"]
    assert qa["conflict_count"] == (2 if hard_kind == "nonthin" else 1)
    if hard_kind is None:
        assert checked["primary_status"] == "clean_with_approved_thin_conflicts"
        assert qa["schedule_violation_count"] == 0
        assert qa["approved_thin_conflict_count"] == 1
    else:
        assert checked["primary_status"] == "contains_manual_override"
        assert qa["schedule_violation_count"] > 0
    # The same policy applies at the fresh-build producer (fixed pins create
    # the deliberate same-slot placement without relying on scheduler luck).
    built = build_exam_timetable(
        label="Thin status",
        days=DAYS,
        periods=PERIODS,
        programs=["AI", "DS"],
        sections=["F", "M"],
        selected_courses=payload["selected_courses"],
        assign_rooms=False,
        thin_conflict_threshold=1,
        pinned=[
            {"course_code": row["course_code"], "day": "Sun", "period": PERIODS[0]}
            for row in payload["base_schedule"]
        ],
    )
    assert built["primary_status"] == checked["primary_status"]
    assert built["qa"]["schedule_violation_count"] == qa["schedule_violation_count"]
