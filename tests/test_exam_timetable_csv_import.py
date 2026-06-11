from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from core.models import Course, ExamTimetableRun, Student, StudentCourse

pytestmark = pytest.mark.django_db


def test_import_exam_timetable_csv_creates_saved_run(tmp_path) -> None:
    course_a = Course.objects.create(course_code="IMP101", credit_hours=3)
    course_b = Course.objects.create(course_code="IMP102", credit_hours=3)
    student = Student.objects.create(student_id=771001, program="IMPORT")
    StudentCourse.objects.create(student=student, course=course_a, status="studying")
    StudentCourse.objects.create(student=student, course=course_b, status="studying")

    csv_path = tmp_path / "manual_exam.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Day,Date,Period,Time,Course Name,Course Code",
                "Tuesday,16/12/1447 H - 02/06/2026,Period 1,08:30 AM - 10:30 AM,Intro A,IMP101",
                "Tuesday,16/12/1447 H - 02/06/2026,Period 1,08:30 AM - 10:30 AM,Intro B,IMP102",
            ]
        ),
        encoding="utf-8",
    )

    call_command(
        "import_exam_timetable_csv",
        str(csv_path),
        label="Manual Import",
        no_rooms=True,
    )

    run = ExamTimetableRun.objects.get(label="Manual Import")
    payload = json.loads(run.result_json)

    assert payload["schema_version"] >= 1
    assert payload["rebuild_mode"] == "manual_csv_import"
    assert payload["courses"] == ["IMP101", "IMP102"]
    assert payload["schedule"][0]["day"] == "Tuesday 02/06/2026"
    assert payload["schedule"][0]["period"] == "Period 1 (08:30 AM - 10:30 AM)"
    assert payload["qa"]["conflict_count"] == 1
    assert payload["primary_status"] == "contains_manual_override"


def test_import_exam_timetable_csv_splits_same_code_different_names(tmp_path) -> None:
    csv_path = tmp_path / "manual_duplicate.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Day,Date,Period,Time,Course Name,Course Code",
                "Monday,22/12/1447 H - 08/06/2026,Period 1,08:30 AM - 10:30 AM,Programming 1,CS112",
                "Monday,22/12/1447 H - 08/06/2026,Period 2,11:00 AM - 01:00 PM,Programming 2,CS112",
            ]
        ),
        encoding="utf-8",
    )

    call_command(
        "import_exam_timetable_csv",
        str(csv_path),
        label="Duplicate Import",
        no_rooms=True,
    )

    payload = json.loads(ExamTimetableRun.objects.get(label="Duplicate Import").result_json)

    assert payload["courses"] == ["CS112 (1)", "CS112 (2)"]
    assert [entry["source_course_code"] for entry in payload["schedule"]] == ["CS112", "CS112"]
    assert [entry["course_code"] for entry in payload["schedule"]] == ["CS112 (1)", "CS112 (2)"]
