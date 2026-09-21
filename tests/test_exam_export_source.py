"""Every persisted workflow supplies scoped enrollment and honest QA to exports."""

import json

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Student, StudentCourse
from core.services.exam_multistart import run_multistart
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import build_enrolled_sets_with_meta, build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db
PERIOD = "08:00-10:00"
PROGRAMMING = "Programming I"
FUNDAMENTALS = "Fundamentals of Programming"


@pytest.fixture
def export_courses():
    shared = Course.objects.create(
        course_code="CS111", description="Registrar code", credit_hours=3
    )
    for program, name in [("AI", PROGRAMMING), ("AI2", FUNDAMENTALS)]:
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS111", course_name=name, programme_term=1
        )
    for sid, program, section in [
        (901, "AI", "F"),
        (902, "AI", "F"),
        (903, "AI", "M"),
        (904, "AI2", "F"),
        (905, "AI2", "M"),
        (906, "AI2", "M"),
    ]:
        student = Student.objects.create(student_id=sid, program=program, section=section)
        StudentCourse.objects.create(student=student, course=shared, status="studying")
        scraped_exam_registration(student, shared)


@pytest.fixture
def export_client(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="export-source-admin"))
    return client


def _entry(name):
    _, metadata = build_enrolled_sets_with_meta()
    return next(
        {"course_code": code, **meta}
        for code, meta in metadata.items()
        if meta["course_name"] == name
    )


def _assert_persisted_snapshot(result, expected, *, schedule_counts=True):
    assert {
        code: {section["gender"]: section["student_count"] for section in sections}
        for code, sections in result["section_enrollment"].items()
    } == expected
    assert result["qa"]["enrolment_snapshot"]["sections_count"] == sum(
        len(sections) for sections in expected.values()
    )
    # Each population is explicitly backed by one scraped section per cohort.
    for groups in result["section_enrollment"].values():
        for group in groups:
            assert group["section"] == f"{group['gender']}1"
            assert group["section_key"] == f"term-section:{group['term_section_id']}"
            assert group["term_section_id"] is not None
            assert group["mapping_status"] == "mapped"
            assert len(group["membership_fingerprint"]) == 64
    mapping = result["qa"]["section_mapping"]
    assert mapping["mapped_sections"] == sum(len(genders) for genders in expected.values())
    assert mapping["ambiguous_enrollments"] == mapping["missing_enrollments"] == 0
    assert mapping["mapped_enrollments"] == sum(
        sum(genders.values()) for genders in expected.values()
    )
    saved = load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))
    assert saved["section_enrollment"] == result["section_enrollment"]
    if schedule_counts:
        for entry in saved["schedule"]:
            assert sum(expected[entry["course_code"]].values()) == entry["enrolled_count"]


@pytest.mark.parametrize("assign_rooms", [False, True])
def test_duplicate_code_variants_keep_separate_gender_demand_without_available_rooms(
    export_courses, assign_rooms
):
    result = build_exam_timetable(
        label="Export source variants",
        days=["Sun", "Mon"],
        periods=[PERIOD],
        assign_rooms=assign_rooms,
    )
    _assert_persisted_snapshot(
        result,
        {
            _entry(PROGRAMMING)["course_code"]: {"F": 2, "M": 1},
            _entry(FUNDAMENTALS)["course_code"]: {"F": 1, "M": 2},
        },
    )
    if assign_rooms:
        assert sum(len(entry["rooms"]) for entry in result["schedule"]) == 4
        assert all(
            room["room_code"] == "UNASSIGNED"
            for entry in result["schedule"]
            for room in entry["rooms"]
        )


@pytest.mark.parametrize("assign_rooms", [False, True])
def test_missing_inventory_is_a_room_problem_only_when_allocation_was_requested(
    export_courses, export_client, assign_rooms
):
    payload = {
        "label": "Room inventory export source",
        "days": ["Sun"],
        "periods": [PERIOD],
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": assign_rooms,
    }
    result = None
    for mode in [None, "save_loaded_changes", "optimize_loaded"]:
        if mode:
            assert result is not None
            payload.update(
                mode=mode, previous_run_id=result["run_id"], base_schedule=result["schedule"]
            )
        response = export_client.post(
            reverse("exam_timetable_build"), payload, content_type="application/json"
        )
        assert response.status_code == 200, response.content
        result = response.json()
        _assert_persisted_snapshot(result, {"CS111": {"F": 2}})
        if assign_rooms:
            assert result["qa"]["rooms"]["rooms_available"] == 0
            assert result["qa"]["rooms"]["rooms_used"] == 0
            unassigned = result["qa"]["rooms"]["unassigned_room_sections"]
            assert len(unassigned) == 1
            assert {
                key: unassigned[0][key]
                for key in (
                    "course_code",
                    "day",
                    "period",
                    "section",
                    "student_count",
                    "gender",
                    "mapping_status",
                    "room_group",
                )
            } == {
                "course_code": "CS111",
                "day": "Sun",
                "period": PERIOD,
                "section": "F1",
                "student_count": 2,
                "gender": "F",
                "mapping_status": "mapped",
                "room_group": "",
            }
            assert unassigned[0]["section_parts"][0]["section_key"].startswith("term-section:")
            assert result["primary_status"] == "requires_room_action"
            assert "room_action_required" in result["status_flags"]
        else:
            assert not result["qa"].get("rooms", {}).get("unassigned_room_sections")
            assert "room_action_required" not in result["status_flags"]
            assert result["primary_status"] == "clean"


@pytest.mark.parametrize("sections,expected", [(["F"], {"F": 2}), (["M"], {"M": 1})])
def test_scope_and_selected_identity_snapshot_survives_build_save_optimize(
    export_courses, export_client, sections, expected
):
    selected = _entry(PROGRAMMING)
    payload = {
        "label": "Export source scope",
        "days": ["Sun", "Mon"],
        "periods": [PERIOD],
        "programs": ["AI"],
        "sections": sections,
        "assign_rooms": False,
        "selected_courses": [selected["course_code"]],
        "selected_course_entries": [selected],
    }
    response = export_client.post(
        reverse("exam_timetable_build"), payload, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    result = response.json()
    _assert_persisted_snapshot(result, {selected["course_code"]: expected})
    for mode in ["save_loaded_changes", "optimize_loaded"]:
        response = export_client.post(
            reverse("exam_timetable_build"),
            {
                **payload,
                "mode": mode,
                "previous_run_id": result["run_id"],
                "base_schedule": result["schedule"],
                "programs": [],
                "sections": [],
            },
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        result = response.json()
        _assert_persisted_snapshot(result, {selected["course_code"]: expected})
        assert result["enrollment_scope"] == {"programs": ["AI"], "sections": sections}


@pytest.mark.parametrize("assign_rooms", [False, True])
def test_overflow_course_keeps_enrollment_snapshot(export_courses, assign_rooms):
    companion = Course.objects.create(course_code="EX222", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="EX222", course_name="Second exam", programme_term=2
    )
    for student in Student.objects.filter(program="AI"):
        StudentCourse.objects.create(student=student, course=companion, status="studying")
        scraped_exam_registration(student, companion)
    result = build_exam_timetable(
        label="Overflow export source",
        days=["Sun"],
        periods=[PERIOD],
        programs=["AI"],
        assign_rooms=assign_rooms,
    )
    assert any(entry["day"] == "OVERFLOW" for entry in result["schedule"])
    _assert_persisted_snapshot(result, {"CS111": {"F": 2, "M": 1}, "EX222": {"F": 2, "M": 1}})


def test_multistart_candidates_persist_enrollment_without_rooms(export_courses):
    selected = _entry(PROGRAMMING)
    report = run_multistart(
        label="Candidate export sources",
        days=["Sun", "Mon"],
        periods=[PERIOD],
        programs=["AI"],
        sections=["F"],
        selected_courses=[selected["course_code"]],
        selected_course_entries=[selected],
        seeds=[1, 2],
        assign_rooms=False,
    )
    assert report.candidates_by_role
    for run in ExamTimetableRun.objects.all():
        result = load_normalised_run(run)
        _assert_persisted_snapshot(
            {**result, "run_id": run.pk}, {selected["course_code"]: {"F": 2}}
        )


def test_bucket_override_without_student_slot_clash_is_honest_through_saved_workflows(
    export_courses, export_client
):
    companion = Course.objects.create(course_code="EX222", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="EX222", course_name="Second exam", programme_term=1
    )
    for student in Student.objects.filter(program="AI"):
        StudentCourse.objects.create(student=student, course=companion, status="studying")
        scraped_exam_registration(student, companion)
    second_period = "13:00-15:00"
    payload = {
        "label": "Bucket override export",
        "days": ["Sun"],
        "periods": [PERIOD, second_period],
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": False,
        "pinned": [
            {"course_code": "CS111", "day": "Sun", "period": PERIOD},
            {"course_code": "EX222", "day": "Sun", "period": second_period},
        ],
    }
    result = None
    for mode in [None, "save_loaded_changes", "optimize_loaded"]:
        if mode:
            assert result is not None
            payload.update(
                mode=mode, previous_run_id=result["run_id"], base_schedule=result["schedule"]
            )
        response = export_client.post(
            reverse("exam_timetable_build"), payload, content_type="application/json"
        )
        assert response.status_code == 200, response.content
        result = response.json()
        assert result["qa"]["conflict_count"] == 0
        assert result["qa"]["manual_override_count"] == 1
        assert result["qa"]["manual_override_details"] == [
            {
                "kind": "bucket_day",
                "program": "AI",
                "programme_term": 1,
                "day": "Sun",
                "courses": ["CS111", "EX222"],
            }
        ]
        assert result["primary_status"] == "contains_manual_override"
        assert "manual_override" in result["status_flags"]
        saved = load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))
        assert saved["qa"]["manual_override_details"] == result["qa"]["manual_override_details"]


def test_csv_import_without_rooms_also_keeps_gender_enrollment(export_courses, tmp_path):
    csv_path = tmp_path / "exam-source.csv"
    csv_path.write_text(
        "Day,Date,Period,Time,Course Name,Course Code\n"
        "Monday,08/06/2026,Period 1,08:00 AM - 10:00 AM,Programming I,CS111 (2)\n",
        encoding="utf-8",
    )
    call_command(
        "import_exam_timetable_csv", str(csv_path), label="CSV export source", no_rooms=True
    )
    run = ExamTimetableRun.objects.get(label="CSV export source")
    result = json.loads(run.result_json)
    _assert_persisted_snapshot(
        {**result, "run_id": run.pk}, {"CS111 (2)": {"F": 2, "M": 1}}, schedule_counts=False
    )
