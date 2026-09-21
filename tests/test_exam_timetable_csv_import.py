from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.course_identity import planner_course_key

pytestmark = pytest.mark.django_db


def test_import_exam_timetable_csv_creates_saved_run(tmp_path) -> None:
    course_a = Course.objects.create(course_code="IMP101", credit_hours=3)
    course_b = Course.objects.create(course_code="IMP102", credit_hours=3)
    student = Student.objects.create(student_id=771001, program="IMPORT", section="M")
    StudentCourse.objects.create(student=student, course=course_a, status="studying")
    StudentCourse.objects.create(student=student, course=course_b, status="studying")
    for course in [course_a, course_b]:
        section = TermSection.objects.create(
            course_key=course.course_code,
            course_code=course.course_code,
            course_number=course.course_code[-3:],
            section="M1",
        )
        StudentTermSection.objects.create(
            student_id=student.student_id,
            term_section=section,
            academic_year="1447",
            term="2",
            source="scraper_timetable",
        )

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
    assert payload["enrollment_source"] == "scraper_timetable"
    assert payload["enrollment_scope"] == {"programs": [], "sections": []}
    assert all(entry["enrolled_count"] == 1 for entry in payload["schedule"])


def _duplicate_scraped_courses():
    Course.objects.create(course_code="CS112", credit_hours=4)
    for index, (name, term_rank) in enumerate([("Programming 1", 2), ("Programming 2", 1)], 1):
        student = Student.objects.create(
            student_id=772000 + index, program=f"P{index}", section="F"
        )
        ProgrammeRequirement.objects.create(
            program=student.program,
            course_code="CS112",
            course_name=name,
            programme_term=term_rank,
        )
        section = TermSection.objects.create(
            course_code="CS",
            course_number="112",
            course_key="CS112",
            course_name=name,
            section=f"F{index}",
        )
        StudentTermSection.objects.create(
            student_id=student.student_id,
            term_section=section,
            academic_year="1447",
            term="2",
            source="scraper_timetable",
        )


def test_import_exam_timetable_csv_matches_duplicate_names_not_display_suffixes(tmp_path) -> None:
    _duplicate_scraped_courses()
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
    assert [entry["course_name"] for entry in payload["schedule"]] == [
        "Programming 1",
        "Programming 2",
    ]
    assert [entry["course_identity"] for entry in payload["schedule"]] == [
        planner_course_key("CS112", name) for name in ["Programming 1", "Programming 2"]
    ]
    assert [entry["programs"] for entry in payload["schedule"]] == [["P1"], ["P2"]]
    assert [
        payload["section_enrollment"][entry["course_code"]][0]["section"]
        for entry in payload["schedule"]
    ] == ["F1", "F2"]
    assert payload["credit_map"] == {"CS112 (1)": 4, "CS112 (2)": 4}
    assert payload["students_count"] == 2
    assert payload["enrollment_source"] == "scraper_timetable"


def _write_one_course_csv(tmp_path, code, name):
    path = tmp_path / "strict-source.csv"
    path.write_text(
        "Day,Date,Period,Time,Course Name,Course Code\n"
        f"Monday,08/06/2026,Period 1,08:30 AM - 10:30 AM,{name},{code}\n",
        encoding="utf-8",
    )
    return path


def test_csv_import_rejects_studying_course_without_scraped_enrollments(tmp_path):
    course = Course.objects.create(course_code="ONLY101", description="Academic record only")
    student = Student.objects.create(student_id=773001, program="IMPORT", section="M")
    StudentCourse.objects.create(student=student, course=course, status="studying")
    path = _write_one_course_csv(tmp_path, "ONLY101", "Academic record only")
    with pytest.raises(CommandError, match="no actual scraped timetable enrollments"):
        call_command("import_exam_timetable_csv", str(path), no_rooms=True)
    assert not ExamTimetableRun.objects.exists()


def test_csv_import_rejects_ambiguous_variant_even_with_known_display_suffix(tmp_path):
    _duplicate_scraped_courses()
    path = _write_one_course_csv(tmp_path, "CS112 (1)", "Unclear programming name")
    with pytest.raises(CommandError, match="multiple scraped course identities"):
        call_command("import_exam_timetable_csv", str(path), no_rooms=True)
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize("section_label", ["F01", "ALL"])
def test_csv_single_identity_alias_uses_authoritative_name_and_metadata(tmp_path, section_label):
    course = Course.objects.create(
        course_code="ONE101", description="Recorded course name", credit_hours=4
    )
    student = Student.objects.create(student_id=774001, program="IMPORT", section="F")
    section = TermSection.objects.create(
        course_code="ONE",
        course_number="101",
        course_key="ONE101",
        section=section_label,
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        term_section=section,
        academic_year="1447",
        term="2",
        source="scraper_timetable",
    )
    path = _write_one_course_csv(tmp_path, "ONE101", "An import spelling alias")
    call_command("import_exam_timetable_csv", str(path), no_rooms=True)
    payload = json.loads(ExamTimetableRun.objects.get().result_json)
    entry = payload["schedule"][0]
    assert entry["course_name"] == course.description
    assert entry["course_identity"] == planner_course_key(course.course_code, course.description)
    assert entry["enrolled_count"] == 1
    assert entry["programs"] == ["IMPORT"]
    assert payload["credit_map"]["ONE101"] == 4
    assert payload["section_enrollment"]["ONE101"][0]["section"] == section_label
    assert payload["qa"]["section_mapping"]["mapped_enrollments"] == 1
    snapshot = payload["qa"]["enrolment_snapshot"]
    assert snapshot["fallback_used"] is False
    assert snapshot["synthetic_all_sections_count"] == 0


def test_csv_rejects_two_aliases_resolving_to_one_actual_exam(tmp_path):
    _duplicate_scraped_courses()
    StudentTermSection.objects.filter(term_section__section="F2").delete()
    path = tmp_path / "duplicate-identities.csv"
    path.write_text(
        "Day,Date,Period,Time,Course Name,Course Code\n"
        "Monday,08/06/2026,Period 1,08:30 AM - 10:30 AM,Alias One,CS112\n"
        "Monday,08/06/2026,Period 2,11:00 AM - 01:00 PM,Alias Two,CS112\n",
        encoding="utf-8",
    )
    with pytest.raises(CommandError, match="selected more than once"):
        call_command("import_exam_timetable_csv", str(path), no_rooms=True)
    assert not ExamTimetableRun.objects.exists()
