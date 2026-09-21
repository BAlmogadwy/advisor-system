"""Department exports receive saved evidence, never invented room membership."""

import json
from copy import deepcopy

import pytest
from django.core.management import call_command
from django.urls import reverse

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
    TermSection,
    TermSectionMeeting,
)
from core.services.exam_run_schema import load_normalised_run, normalise_exam_run_payload
from core.services.exam_sections import resolve_exam_section_enrollment
from core.services.exam_timetable import build_exam_timetable
from tests import test_exam_export_source as source_fixtures
from tests.exam_source_factory import scraped_exam_registration

export_client = source_fixtures.export_client
export_courses = source_fixtures.export_courses

pytestmark = pytest.mark.django_db
DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "13:00-15:00"]


@pytest.fixture
def population(export_courses):
    course = Course.objects.get(course_code="CS111")
    for sid, program in [(907, "DS"), (908, "CS")]:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="CS111",
            course_name="Programming I",
            programme_term=1,
        )
        student = Student.objects.create(
            student_id=sid,
            program=program,
            section="F",
            name="PRIVATE STUDENT NAME",
        )
        scraped_exam_registration(student, course)
    section = TermSection.objects.get(course_key="CS111", section="F1")
    for day, instructor in [("Sun", "Dr Recorded"), ("Mon", "Dr Recorded"), ("Tue", "Dr Second")]:
        TermSectionMeeting.objects.create(
            term_section=section,
            day=day,
            start_time="08:00",
            end_time="09:00",
            instructor=instructor,
        )


def _build(**kwargs):
    return build_exam_timetable(
        label="Operations snapshot",
        days=DAYS,
        periods=PERIODS,
        seed=17,
        assign_rooms=kwargs.pop("assign_rooms", False),
        **kwargs,
    )


def _assert_reconciled(result):
    snapshot = result["operations_snapshot"]
    assert snapshot["version"] == 1
    assert snapshot["enrollment_source"] == "scraper_timetable"
    assert set(snapshot["courses"]) == set(result["courses"])
    for entry in result["schedule"]:
        course = snapshot["courses"][entry["course_code"]]
        assert course["course_identity"] == entry["course_identity"]
        assert sum(s["student_count"] for s in course["sections"]) == entry["enrolled_count"]
        assert sum(r["student_count"] for r in course["program_counts"]) == entry["enrolled_count"]
        for section in course["sections"]:
            assert sum(section["program_counts"].values()) == section["student_count"]
    encoded = json.dumps(snapshot)
    assert "PRIVATE STUDENT NAME" not in encoded
    assert "student_id" not in encoded
    assert "membership_fingerprint" not in encoded


def test_build_separates_canonical_identities_and_preserves_shared_section_counts(population):
    result = _build()
    _assert_reconciled(result)
    courses = {row["course_name"]: row for row in result["operations_snapshot"]["courses"].values()}
    first = next(s for s in courses["Programming I"]["sections"] if s["gender"] == "F")
    second = next(
        s for s in courses["Fundamentals of Programming"]["sections"] if s["gender"] == "F"
    )
    assert first["section_key"] == second["section_key"]
    assert first["program_counts"] == {"AI": 2, "CS": 1, "DS": 1}
    assert second["program_counts"] == {"AI2": 1}
    assert first["instructors"] == ["Dr Recorded", "Dr Second"]
    assert first["instructor_source"] == "recorded_section_meetings"
    male = next(s for s in courses["Programming I"]["sections"] if s["gender"] == "M")
    assert male["instructors"] == []
    assert male["instructor_status"] == "missing"
    assert all(
        "program_counts" not in section and "instructors" not in section
        for groups in result["section_enrollment"].values()
        for section in groups
    )


def test_unknown_program_and_ambiguous_section_never_drop_or_guess_students():
    course = Course.objects.create(course_code="CS113")
    first = Student.objects.create(student_id=91, section="F", program="AI")
    unknown = Student.objects.create(student_id=92, section="F", program="")
    missing = Student.objects.create(student_id=93, section="F", program="DS")
    scraped_exam_registration(first, course, section_label="F1")
    scraped_exam_registration(first, course, section_label="F2")
    scraped_exam_registration(unknown, course, section_label="F1")
    for section in TermSection.objects.all():
        TermSectionMeeting.objects.create(
            term_section=section,
            day="Sun",
            start_time="08:00",
            end_time="09:00",
            instructor="Must not attribute to ambiguous students",
        )
    captured = {}
    ordinary = resolve_exam_section_enrollment(
        {"CS113": {first.pk, unknown.pk, missing.pk}},
        operations_sections=captured,
    )
    assert sum(s["student_count"] for s in ordinary["CS113"]) == 3
    sections = {s["mapping_status"]: s for s in captured["CS113"]}
    assert sections["mapped"]["program_counts"] == {"": 1}
    assert sections["ambiguous"]["program_counts"] == {"AI": 1}
    assert sections["missing"]["program_counts"] == {"DS": 1}
    for status in ("ambiguous", "missing"):
        assert sections[status]["instructors"] == []
        assert sections[status]["instructor_status"] == "unavailable"


def test_source_scope_counts_only_selected_programmes_and_gender(population):
    result = _build(programs=["AI", "DS"], sections=["F"])
    _assert_reconciled(result)
    course = next(iter(result["operations_snapshot"]["courses"].values()))
    assert course["program_counts"] == [
        {"program": "AI", "gender": "F", "student_count": 2},
        {"program": "DS", "gender": "F", "student_count": 1},
    ]
    assert course["sections"][0]["program_counts"] == {"AI": 2, "DS": 1}


def test_split_rooms_keep_programme_counts_only_at_teaching_section_level(population):
    for code in ("F01", "F02", "F03"):
        Room.objects.create(room_code=code, capacity=2, section="F")
    Room.objects.create(room_code="M01", capacity=4, section="M")
    result = _build(assign_rooms=True)
    _assert_reconciled(result)
    programming = next(e for e in result["schedule"] if e["course_name"] == "Programming I")
    assert len([room for room in programming["rooms"] if room["gender"] == "F"]) == 2
    for entry in result["schedule"]:
        for room in entry["rooms"]:
            assert "program_counts" not in room
            assert all("program_counts" not in part for part in room["section_parts"])


def test_saved_snapshot_survives_source_edits_without_live_queries(
    population, django_assert_num_queries
):
    result = _build()
    run = ExamTimetableRun.objects.get(pk=result["run_id"])
    Student.objects.update(program="CHANGED")
    TermSectionMeeting.objects.update(instructor="Changed later")
    with django_assert_num_queries(0):
        loaded = load_normalised_run(run)
    assert loaded["operations_snapshot"] == result["operations_snapshot"]


@pytest.mark.parametrize("legacy", [False, True])
def test_check_save_and_loaded_optimize_capture_same_snapshot(population, export_client, legacy):
    source = _build()
    if legacy:
        old = deepcopy(source)
        old["schema_version"] = 4
        old.pop("operations_snapshot")
        ExamTimetableRun.objects.filter(pk=source["run_id"]).update(result_json=json.dumps(old))
    payload = {
        "label": "Operations lifecycle",
        "previous_run_id": source["run_id"],
        "base_schedule": deepcopy(source["schedule"]),
        "days": DAYS,
        "periods": PERIODS,
        "selected_courses": source["courses"],
        "assign_rooms": False,
    }
    response = export_client.post(
        reverse("exam_timetable_draft_impact"),
        payload,
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    checked = response.json()
    assert checked["operations_snapshot"] == source["operations_snapshot"]
    payload["expected_input_fingerprint"] = checked["input_fingerprint"]
    for mode in ("save_loaded_changes", "optimize_loaded"):
        response = export_client.post(
            reverse("exam_timetable_build"),
            {**payload, "mode": mode},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        saved = response.json()
        _assert_reconciled(saved)
        assert saved["operations_snapshot"] == source["operations_snapshot"]
        assert (
            load_normalised_run(ExamTimetableRun.objects.get(pk=saved["run_id"]))[
                "operations_snapshot"
            ]
            == source["operations_snapshot"]
        )


def test_instructor_source_change_invalidates_checked_save(population, export_client):
    source = _build()
    payload = {
        "label": "Stale instructor",
        "previous_run_id": source["run_id"],
        "base_schedule": source["schedule"],
        "days": DAYS,
        "periods": PERIODS,
        "selected_courses": source["courses"],
        "assign_rooms": False,
    }
    checked = export_client.post(
        reverse("exam_timetable_draft_impact"),
        payload,
        content_type="application/json",
    ).json()
    TermSectionMeeting.objects.filter(instructor="Dr Recorded").update(instructor="New instructor")
    response = export_client.post(
        reverse("exam_timetable_build"),
        {
            **payload,
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
        content_type="application/json",
    )
    assert response.status_code == 409, response.content
    assert response.json()["error_code"] == "inputs_changed"


def test_csv_import_captures_department_evidence_for_correct_named_variant(population, tmp_path):
    path = tmp_path / "operations.csv"
    path.write_text(
        "Day,Date,Period,Time,Course Name,Course Code\n"
        "Sunday,21/09/2026,Period 1,08:00-10:00,Programming I,CS111\n",
        encoding="utf-8",
    )
    call_command("import_exam_timetable_csv", str(path), label="Operations CSV", no_rooms=True)
    result = load_normalised_run(ExamTimetableRun.objects.get(label="Operations CSV"))
    _assert_reconciled(result)
    course = next(iter(result["operations_snapshot"]["courses"].values()))
    assert course["course_name"] == "Programming I"
    assert {r["program"] for r in course["program_counts"]} == {"AI", "CS", "DS"}


def test_legacy_snapshot_is_unavailable_without_reconstruction(django_assert_num_queries):
    with django_assert_num_queries(0):
        result = normalise_exam_run_payload(
            {
                "schema_version": 4,
                "status": "ok",
                "schedule": [],
                "section_enrollment": {},
            }
        )
    assert result["schema_version"] == 5
    assert result["operations_snapshot"] is None
    assert normalise_exam_run_payload(result) == result
