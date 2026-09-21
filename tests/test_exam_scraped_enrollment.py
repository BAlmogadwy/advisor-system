"""Only actual scraped registrations define exam population and term selection."""

import pytest
from django.urls import reverse

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.exam_sections import resolve_exam_section_enrollment
from core.services.exam_timetable import build_enrolled_sets_with_meta, build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration
from tests.test_exam_export_source import export_client as export_client

pytestmark = pytest.mark.django_db


def _student(sid, program="AI", cohort="F"):
    return Student.objects.create(student_id=sid, program=program, section=cohort)


@pytest.mark.parametrize(
    "source",
    [
        "registration_plan_1448_t1",
        "manual",
        "planner",
        "auto_from_studying",
        "fallback_studying",
        "SCRAPER_TIMETABLE",
        " scraper_timetable ",
        "",
    ],
)
def test_only_exact_scraper_source_is_enrollment(source):
    actual, excluded = _student(1), _student(2)
    scraped_exam_registration(actual, "CS101")
    link = scraped_exam_registration(excluded, "CS102")
    link.source = source
    link.save(update_fields=["source"])
    course = Course.objects.create(course_code="STUDY999", description="Studying-only")
    StudentCourse.objects.create(student=actual, course=course, status="studying")
    enrolled, _ = build_enrolled_sets_with_meta()
    assert enrolled == {"CS101": {1}}
    resolved = resolve_exam_section_enrollment({"CS102": {2}})
    assert resolved["CS102"][0]["mapping_status"] == "missing"


@pytest.mark.parametrize(
    "newer_source", ["registration_plan_1449_t1", "manual", "fallback_studying"]
)
def test_newer_non_scraped_term_cannot_replace_current_scraped_term(newer_source):
    student = _student(1)
    current = scraped_exam_registration(student, "CS101", year="1448", term="2")
    future = scraped_exam_registration(student, "CS102", year="1449", term="1")
    future.source = newer_source
    future.save(update_fields=["source"])
    old = scraped_exam_registration(student, "CS103", year="1448", term="1")
    enrolled, _ = build_enrolled_sets_with_meta()
    assert enrolled == {"CS101": {1}}
    groups = resolve_exam_section_enrollment(enrolled)["CS101"]
    assert groups[0]["term_section_id"] == current.term_section_id
    assert (groups[0]["academic_year"], groups[0]["term"]) == ("1448", "2")
    assert StudentTermSection.objects.filter(pk=old.pk).exists()


def test_scraper_registrations_work_without_any_student_course_rows_and_deduplicate_meetings():
    student = _student(1)
    first = scraped_exam_registration(student, "CS101", section_label="F01")
    scraped_exam_registration(student, "CS101", section_label="F1")
    for day in ["Sun", "Mon", "Tue"]:
        TermSectionMeeting.objects.create(
            term_section=first.term_section,
            day=day,
            start_time="08:00",
            end_time="09:00",
            room="FROOM",
        )
    assert not StudentCourse.objects.exists()
    enrolled, _ = build_enrolled_sets_with_meta()
    assert enrolled == {"CS101": {1}}
    groups = resolve_exam_section_enrollment(enrolled)["CS101"]
    assert len(groups) == 1
    assert groups[0]["mapping_status"] == "ambiguous"
    assert groups[0]["student_count"] == 1


@pytest.mark.parametrize(
    "source", [None, "registration_plan_1448_t1", "manual", "fallback_studying"]
)
def test_no_scraped_population_is_empty_and_build_endpoint_does_not_save(export_client, source):  # noqa: F811
    student = _student(1)
    course = Course.objects.create(course_code="CS101", description="CS101")
    StudentCourse.objects.create(student=student, course=course, status="studying")
    if source is not None:
        link = scraped_exam_registration(student, course)
        link.source = source
        link.save(update_fields=["source"])
    assert build_enrolled_sets_with_meta() == ({}, {})
    response = export_client.post(
        reverse("exam_timetable_build"),
        {
            "label": "No scraped registrations",
            "days": ["Sun"],
            "periods": ["08:00-10:00"],
            "programs": ["AI"],
            "sections": ["F"],
            "assign_rooms": False,
        },
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


def test_plan_specific_identities_and_population_scope_are_preserved():
    for program, name in [("AI", "Programming I"), ("AI2", "Fundamentals of Programming")]:
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS111", course_name=name, programme_term=1
        )
    people = [_student(1, "AI", "F"), _student(2, "AI", "M"), _student(3, "AI2", "F")]
    for student in people:
        scraped_exam_registration(
            student, "CS111", section_label=f"{student.section}{student.student_id}"
        )
    excluded = _student(4, "AI2", "F")
    course = Course.objects.create(course_code="CS111", description="Unused studying name")
    StudentCourse.objects.create(student=excluded, course=course, status="studying")
    enrolled, metadata = build_enrolled_sets_with_meta()
    by_identity = {
        metadata[code]["course_identity"]: students for code, students in enrolled.items()
    }
    assert by_identity == {
        planner_course_key("CS111", "Programming I"): {1, 2},
        planner_course_key("CS111", "Fundamentals of Programming"): {3},
    }
    scoped, scoped_meta = build_enrolled_sets_with_meta(programs=["AI"], sections=["F"])
    assert scoped == {"CS111": {1}}
    assert scoped_meta["CS111"]["course_name"] == "Programming I"
    assert build_enrolled_sets_with_meta(programs=["NONE"]) == ({}, {})


def test_scraped_population_is_not_removed_by_student_course_status():
    student = _student(1)
    course = Course.objects.create(course_code="CS101", description="CS101")
    StudentCourse.objects.create(student=student, course=course, status="passed")
    scraped_exam_registration(student, course)
    result = build_exam_timetable(
        label="Actual retake", days=["Sun"], periods=["08:00-10:00"], assign_rooms=False
    )
    assert result["students_count"] == 1
    assert result["schedule"][0]["enrolled_count"] == 1
    assert result["enrollment_source"] == "scraper_timetable"


def test_catalogue_source_tag_does_not_override_actual_registration_source():
    student = _student(1)
    actual = scraped_exam_registration(student, "CS101")
    actual.term_section.source_tag = "registration_plan_1448_t1"
    actual.term_section.save(update_fields=["source_tag"])
    forecast = scraped_exam_registration(student, "CS102")
    forecast.source = "registration_plan_1448_t1"
    forecast.save(update_fields=["source"])

    enrolled, _ = build_enrolled_sets_with_meta()

    assert enrolled == {"CS101": {1}}
    mapped = resolve_exam_section_enrollment(enrolled)["CS101"][0]
    assert mapped["mapping_source"] == "scraper_timetable"
    assert mapped["term_section_id"] == actual.term_section_id


def test_newer_scenario_scrape_cannot_displace_global_scraped_term():
    student = _student(1)
    current = scraped_exam_registration(student, "CS101", year="1448", term="1")
    draft = scraped_exam_registration(student, "CS102", year="1449", term="1")
    draft.term_section.scenario = TimetableScenario.objects.create(
        academic_year="1449", term="1", name="Future working scenario"
    )
    draft.term_section.save(update_fields=["scenario"])

    enrolled, metadata = build_enrolled_sets_with_meta()

    assert enrolled == {"CS101": {1}}
    mapped = resolve_exam_section_enrollment(enrolled, course_meta=metadata)["CS101"][0]
    assert mapped["term_section_id"] == current.term_section_id
    assert (mapped["academic_year"], mapped["term"]) == ("1448", "1")


@pytest.mark.parametrize("action", ["build", "check", "save", "optimize"])
def test_disappeared_scraped_course_never_falls_back_to_studying_or_saves(export_client, action):  # noqa: F811
    student = _student(1)
    course = Course.objects.create(course_code="CS101", description="CS101")
    StudentCourse.objects.create(student=student, course=course, status="studying")
    registration = scraped_exam_registration(student, course)
    source = build_exam_timetable(
        label="Previously scraped", days=["Sun"], periods=["08:00-10:00"], assign_rooms=False
    )
    registration.delete()
    payload = {
        "label": "Do not resurrect studying enrollment",
        "days": ["Sun"],
        "periods": ["08:00-10:00"],
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": False,
        "selected_courses": source["courses"],
        "selected_course_entries": source["schedule"],
    }
    if action != "build":
        payload.update(previous_run_id=source["run_id"], base_schedule=source["schedule"])
    if action in {"save", "optimize"}:
        payload["mode"] = "save_loaded_changes" if action == "save" else "optimize_loaded"
    response = export_client.post(
        reverse("exam_timetable_draft_impact" if action == "check" else "exam_timetable_build"),
        payload,
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "courses_unavailable"
    assert response.json()["unavailable_courses"] == ["CS101"]
    assert ExamTimetableRun.objects.count() == 1


def test_recorded_all_label_is_preserved_without_claiming_synthetic_fallback(export_client):  # noqa: F811
    student = _student(1)
    registration = scraped_exam_registration(student, "CS101", section_label="ALL")
    built = build_exam_timetable(
        label="Official ALL section", days=["Sun"], periods=["08:00-10:00"], assign_rooms=False
    )
    response = export_client.post(
        reverse("exam_timetable_draft_impact"),
        {
            "previous_run_id": built["run_id"],
            "base_schedule": built["schedule"],
            "selected_courses": built["courses"],
            "assign_rooms": False,
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    checked = response.json()
    for result in (built, checked):
        section = result["section_enrollment"]["CS101"][0]
        assert section["section"] == "ALL"
        assert section["term_section_id"] == registration.term_section_id
        assert section["mapping_status"] == "mapped"
        assert result["qa"]["enrolment_snapshot"]["fallback_used"] is False
        assert result["qa"]["enrolment_snapshot"]["synthetic_all_sections_count"] == 0
    assert checked["input_fingerprint"] == built["input_fingerprint"]
    assert ExamTimetableRun.objects.count() == 1
