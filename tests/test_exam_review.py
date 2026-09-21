"""Review colours and overlap highlights must describe the actual exam population."""

from copy import deepcopy

import pytest

from core.management.commands.import_exam_timetable_csv import _import_payload
from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Student, StudentCourse
from core.services.course_identity import planner_course_key
from core.services.exam_evaluation import evaluate_exam_schedule
from core.services.exam_run_schema import EXAM_RUN_SCHEMA_VERSION, normalise_exam_run_payload
from core.services.exam_timetable import build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db
DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "13:00-15:00"]


def _student(sid, program="AI", section="F"):
    return Student.objects.create(student_id=sid, program=program, section=section)


def _build(**kwargs):
    return build_exam_timetable(
        label="Review metadata",
        days=DAYS,
        periods=PERIODS,
        assign_rooms=False,
        persist=False,
        **kwargs,
    )


def _check(result, **kwargs):
    return evaluate_exam_schedule(
        days=DAYS,
        periods=PERIODS,
        max_per_day=2,
        schedule_raw=deepcopy(result["schedule"]),
        selected_courses=result["courses"],
        assign_rooms=False,
        seed=result["seed"],
        **kwargs,
    )


def test_review_retains_tiny_course_students_and_is_identical_after_fixed_check(monkeypatch):
    students = [_student(9000001), _student(9000002), _student(9000003)]
    for student in students:
        scraped_exam_registration(student, "CS101")
    scraped_exam_registration(students[0], "CS102")
    captured = {}
    from core.services import exam_timetable

    original = exam_timetable.schedule

    def capture_adjacency(courses, adj, *args, **kwargs):
        captured["adj"] = deepcopy(adj)
        return original(courses, adj, *args, **kwargs)

    monkeypatch.setattr(exam_timetable, "schedule", capture_adjacency)
    result = _build(thin_conflict_threshold=1, seed=47)
    expected = {
        "version": 1,
        "enrollment_source": "scraper_timetable",
        "student_overlaps": [{"course_a": "CS101", "course_b": "CS102", "shared_students": 1}],
    }
    assert all("CS102" not in neighbours for neighbours in captured["adj"].values())
    assert result["exam_review"] == expected
    checked = _check(result, thin_conflict_threshold=1)
    assert checked["exam_review"] == expected
    assert checked["input_fingerprint"] == result["input_fingerprint"]
    assert [(e["course_identity"], e["day"], e["period"]) for e in checked["schedule"]] == [
        (e["course_identity"], e["day"], e["period"])
        for e in sorted(result["schedule"], key=lambda e: (e["slot_index"], e["course_code"]))
    ]
    assert not ExamTimetableRun.objects.exists()


def test_review_respects_program_section_and_selected_course_scope():
    for student in [_student(1), _student(2), _student(3, section="M"), _student(4, program="DS")]:
        for code in ["CS101", "CS102", "CS103"]:
            scraped_exam_registration(student, code)
    result = _build(programs=["AI"], sections=["F"], selected_courses=["CS101", "CS102"])
    assert result["exam_review"]["student_overlaps"] == [
        {"course_a": "CS101", "course_b": "CS102", "shared_students": 2}
    ]
    checked = _check(result, programs=["AI"], sections=["F"], thin_conflict_threshold=0)
    assert checked["exam_review"] == result["exam_review"]
    assert checked["enrollment_scope"] == {"programs": ["AI"], "sections": ["F"]}


def test_review_keeps_same_code_variants_separate_and_shared_cross_plan_course_complete():
    for sid, program, name, level in [
        (1, "AI", "Programming I", 1),
        (2, "DS", "Programming Fundamentals", 2),
    ]:
        student = _student(sid, program=program)
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS111", course_name=name, programme_term=level
        )
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="GS101",
            course_name="Shared General Course",
            programme_term=level + 1,
        )
        scraped_exam_registration(student, "CS111", section_label=f"F{sid}")
        scraped_exam_registration(student, "GS101")
    result = _build()
    identity_to_display = {
        entry["course_identity"]: entry["course_code"] for entry in result["schedule"]
    }
    variants = [
        identity_to_display[planner_course_key("CS111", name)]
        for name in ["Programming I", "Programming Fundamentals"]
    ]
    shared = identity_to_display[planner_course_key("GS101", "Shared General Course")]
    expected = sorted(
        [{"course_a": variant, "course_b": shared, "shared_students": 1} for variant in variants],
        key=lambda edge: (edge["course_a"], edge["course_b"]),
    )
    assert variants[0] != variants[1]
    assert result["exam_review"]["student_overlaps"] == expected
    assert [
        (b["program"], b["programme_term"])
        for b in result["buckets_summary"]
        if shared in b["courses"]
    ] == [("AI", 2), ("DS", 3)]
    assert _check(result, thin_conflict_threshold=0)["exam_review"] == result["exam_review"]


def test_planned_and_studying_membership_never_creates_review_overlap():
    first, second = _student(1), _student(2)
    scraped_exam_registration(first, "CS101")
    scraped_exam_registration(second, "CS102")
    planned = scraped_exam_registration(first, "CS102")
    planned.source = "registration_plan_1448_t1"
    planned.save(update_fields=["source"])
    course = Course.objects.create(course_code="CS102", description="CS102")
    StudentCourse.objects.create(student=first, course=course, status="studying")
    for code in ["CS101", "CS102", "PLAN999"]:
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=code, programme_term=1
        )
    result = _build()
    assert result["courses"] == ["CS101", "CS102"]
    assert result["exam_review"]["student_overlaps"] == []


def test_csv_import_review_uses_same_authoritative_overlap_contract():
    student = _student(1)
    rows = []
    for code in ["CS101", "CS102"]:
        scraped_exam_registration(student, code)
        rows.append(
            {
                "Day": "Sun",
                "Date": "2026-09-20",
                "Period": "P1",
                "Time": "08:00-10:00",
                "Course Name": "",
                "Course Code": code,
            }
        )
    imported = _import_payload(rows=rows, label="Review CSV", assign_rooms=False)
    built = _build()
    assert imported["exam_review"] == built["exam_review"]
    assert normalise_exam_run_payload(imported)["exam_review"] == imported["exam_review"]
    assert not ExamTimetableRun.objects.exists()


def test_old_snapshots_are_unavailable_and_new_snapshots_keep_review_measurements():
    legacy = {
        "schema_version": 3,
        "status": "ok",
        "conflicts": [{"course_a": "CS101", "course_b": "CS102", "shared": 99}],
    }
    migrated = normalise_exam_run_payload(legacy)
    assert migrated["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert migrated["exam_review"] is None
    assert "exam_review" not in legacy
    assert normalise_exam_run_payload(migrated) == migrated

    student = _student(1)
    scraped_exam_registration(student, "CS101")
    built = _build()
    assert built["exam_review"]["student_overlaps"] == []
    normalized = normalise_exam_run_payload(built)
    assert normalized["exam_review"] == built["exam_review"]


@pytest.mark.parametrize("mode", ["save", "optimize"])
def test_saved_and_optimized_review_preserves_scope_identity_and_pins(mode):
    from core.exam_views import _optimise_loaded_schedule, _rebuild_loaded_schedule

    for student in [_student(1), _student(2, program="DS")]:
        for code in ["CS101", "CS102"]:
            scraped_exam_registration(student, code)
    pins = [{"course_code": "CS101", "day": DAYS[0], "period": PERIODS[0]}]
    built = _build(programs=["AI"], sections=["F"], pinned=pins, thin_conflict_threshold=1)
    common = {
        "label": "Review persisted",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        "schedule_raw": built["schedule"],
        "selected_courses": built["courses"],
        "assign_rooms": False,
        "seed": None,
        "thin_conflict_threshold": 1,
        "programs": ["AI"],
        "sections": ["F"],
        "pinned": pins,
    }
    if mode == "save":
        saved = _rebuild_loaded_schedule(
            expected_input_fingerprint=built["input_fingerprint"], **common
        )
    else:
        saved = _optimise_loaded_schedule(**common)
    assert saved["exam_review"] == built["exam_review"]
    assert saved["exam_review"]["student_overlaps"] == [
        {"course_a": "CS101", "course_b": "CS102", "shared_students": 1}
    ]
    pinned_entry = next(entry for entry in saved["schedule"] if entry["course_code"] == "CS101")
    assert (pinned_entry["day"], pinned_entry["period"]) == (DAYS[0], PERIODS[0])
    assert saved["enrollment_scope"] == {"programs": ["AI"], "sections": ["F"]}
    assert ExamTimetableRun.objects.count() == 1
