"""
Tests for core.services.exam_timetable

Fixture: 3 students, 4 courses, overlapping enrolments.
  s1: EX101, EX102            → EX101-EX102 conflict (weight 2)
  s2: EX101, EX102, EX201     → +EX101-EX201, +EX102-EX201 (weight 1 each)
  s3: EX201, EX301            → EX201-EX301 (weight 1)
"""

import json

import pytest

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)
from core.services.exam_timetable import (
    build_conflict_graph,
    build_enrolled_sets,
    build_exam_timetable,
    build_plan_term_buckets,
    check_bucket_feasibility,
    schedule,
)

pytestmark = pytest.mark.django_db


def _setup_fixture() -> None:
    """Create 3 students, 4 courses, overlapping studying enrolments."""
    s1 = Student.objects.create(student_id=990001, program="TESTPROG")
    s2 = Student.objects.create(student_id=990002, program="TESTPROG")
    s3 = Student.objects.create(student_id=990003, program="TESTPROG")

    c1 = Course.objects.create(course_code="EX101", credit_hours=3)
    c2 = Course.objects.create(course_code="EX102", credit_hours=3)
    c3 = Course.objects.create(course_code="EX201", credit_hours=3)
    c4 = Course.objects.create(course_code="EX301", credit_hours=2)

    StudentCourse.objects.create(student=s1, course=c1, status="studying", actual_term="")
    StudentCourse.objects.create(student=s1, course=c2, status="studying", actual_term="")
    StudentCourse.objects.create(student=s2, course=c1, status="studying", actual_term="")
    StudentCourse.objects.create(student=s2, course=c2, status="studying", actual_term="")
    StudentCourse.objects.create(student=s2, course=c3, status="studying", actual_term="")
    StudentCourse.objects.create(student=s3, course=c3, status="studying", actual_term="")
    StudentCourse.objects.create(student=s3, course=c4, status="studying", actual_term="")


def test_build_enrolled_sets() -> None:
    _setup_fixture()
    enrolled = build_enrolled_sets()

    assert len(enrolled) == 4
    assert enrolled["EX101"] == {990001, 990002}
    assert enrolled["EX102"] == {990001, 990002}
    assert enrolled["EX201"] == {990002, 990003}
    assert enrolled["EX301"] == {990003}


def test_build_conflict_graph() -> None:
    _setup_fixture()
    enrolled = build_enrolled_sets()
    conflicts, adj = build_conflict_graph(enrolled)

    # EX101-EX102: shared by s1 + s2 → weight 2
    assert adj["EX101"]["EX102"] == 2
    assert adj["EX102"]["EX101"] == 2

    # EX101-EX201: shared by s2 → weight 1
    assert adj["EX101"]["EX201"] == 1

    # EX102-EX201: shared by s2 → weight 1
    assert adj["EX102"]["EX201"] == 1

    # EX201-EX301: shared by s3 → weight 1
    assert adj["EX201"]["EX301"] == 1

    # EX101-EX301: no overlap
    assert "EX301" not in adj.get("EX101", {})

    # Conflict list should have 4 edges total
    assert len(conflicts) == 4


def test_schedule_no_conflicts() -> None:
    _setup_fixture()
    enrolled = build_enrolled_sets()
    _conflicts, adj = build_conflict_graph(enrolled)

    slots = [
        {"index": 0, "day": "Sun", "period": "08:00-10:00"},
        {"index": 1, "day": "Sun", "period": "10:30-12:30"},
        {"index": 2, "day": "Mon", "period": "08:00-10:00"},
        {"index": 3, "day": "Mon", "period": "10:30-12:30"},
        {"index": 4, "day": "Tue", "period": "08:00-10:00"},
        {"index": 5, "day": "Tue", "period": "10:30-12:30"},
    ]

    course_list = sorted(enrolled.keys())
    result = schedule(course_list, adj, slots)

    assert len(result) == 4

    # Verify no two conflicting courses share the same slot
    course_slot = {e["course_code"]: e["slot_index"] for e in result}
    for a, neighbours in adj.items():
        for b in neighbours:
            assert course_slot.get(a) != course_slot.get(b), (
                f"Conflict: {a} and {b} share slot {course_slot.get(a)}"
            )


def test_build_exam_timetable_full() -> None:
    """End-to-end: build, persist, and validate."""
    _setup_fixture()

    result = build_exam_timetable(
        label="Test Run",
        days=["Sun", "Mon", "Tue"],
        periods=["08:00-10:00", "10:30-12:30"],
    )

    # Basic structure
    assert result["courses_count"] == 4
    assert result["students_count"] == 3
    assert result["conflicts_count"] == 4
    assert len(result["slots"]) == 6  # 3 days × 2 periods
    assert len(result["schedule"]) == 4

    # QA: zero same-slot conflicts
    assert result["qa"]["conflict_count"] == 0
    assert result["qa"]["same_slot_conflicts"] == []
    assert result["qa"]["max_per_day"] == 2
    assert "students_over_limit_per_day" in result["qa"]

    # Persisted in DB
    run_id = result["run_id"]
    run = ExamTimetableRun.objects.get(id=run_id)
    assert run.label == "Test Run"
    stored = json.loads(run.result_json)
    assert stored["courses_count"] == 4
    assert stored["qa"]["conflict_count"] == 0


# ── Programme-plan term bucket tests ──────────────────────────


def _setup_bucket_fixture() -> None:
    """Create fixture with ProgrammeRequirement mappings.

    Programme "AI":
      Term 1: EX101, EX102       (2 courses)
      Term 2: EX201              (1 course)
    Programme "DS":
      Term 1: EX101              (shared with AI term 1)
      Term 2: EX301              (1 course)

    So EX101 belongs to buckets (AI, 1) and (DS, 1) — multi-bucket.
    """
    _setup_fixture()

    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="EX101",
        programme_term=1,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="EX102",
        programme_term=1,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="EX201",
        programme_term=2,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="EX101",
        programme_term=1,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="EX301",
        programme_term=2,
        credit_hours=2,
    )


def test_build_plan_term_buckets() -> None:
    _setup_bucket_fixture()

    running = {"EX101", "EX102", "EX201", "EX301"}
    buckets, course_buckets = build_plan_term_buckets(running)

    # Bucket (AI, 1) → {EX101, EX102}
    assert buckets[("AI", 1)] == {"EX101", "EX102"}
    # Bucket (AI, 2) → {EX201}
    assert buckets[("AI", 2)] == {"EX201"}
    # Bucket (DS, 1) → {EX101}
    assert buckets[("DS", 1)] == {"EX101"}
    # Bucket (DS, 2) → {EX301}
    assert buckets[("DS", 2)] == {"EX301"}

    # EX101 is in two buckets: (AI, 1) and (DS, 1)
    assert set(course_buckets["EX101"]) == {("AI", 1), ("DS", 1)}

    # EX301 is in one bucket: (DS, 2)
    assert course_buckets["EX301"] == [("DS", 2)]


def test_bucket_feasibility_pass() -> None:
    """3 courses in a bucket, 5 days → feasible."""
    buckets = {("AI", 1): {"C1", "C2", "C3"}}
    violations = check_bucket_feasibility(buckets, num_days=5)
    assert violations == []


def test_bucket_feasibility_fail() -> None:
    """6 courses in a bucket, 5 days → infeasible."""
    buckets = {("AI", 1): {"C1", "C2", "C3", "C4", "C5", "C6"}}
    violations = check_bucket_feasibility(buckets, num_days=5)
    assert len(violations) == 1
    assert violations[0]["program"] == "AI"
    assert violations[0]["programme_term"] == 1
    assert violations[0]["bucket_size"] == 6
    assert violations[0]["num_days"] == 5


def test_schedule_bucket_day_rule() -> None:
    """Courses in the same (program, term) bucket must be on different days."""
    _setup_bucket_fixture()
    enrolled = build_enrolled_sets()
    _conflicts, adj = build_conflict_graph(enrolled)
    running = set(enrolled.keys())
    ptb, cb = build_plan_term_buckets(running)

    slots = [
        {"index": 0, "day": "Sun", "period": "08:00-10:00"},
        {"index": 1, "day": "Sun", "period": "10:30-12:30"},
        {"index": 2, "day": "Mon", "period": "08:00-10:00"},
        {"index": 3, "day": "Mon", "period": "10:30-12:30"},
        {"index": 4, "day": "Tue", "period": "08:00-10:00"},
        {"index": 5, "day": "Tue", "period": "10:30-12:30"},
    ]

    course_list = sorted(enrolled.keys())
    result = schedule(
        course_list,
        adj,
        slots,
        enrolled_sets=enrolled,
        max_per_day=2,
        plan_term_buckets=ptb,
        course_buckets=cb,
    )

    # Build course→day map
    course_day = {e["course_code"]: e["day"] for e in result}

    # Bucket (AI, 1) has EX101, EX102 → must be on different days
    assert course_day["EX101"] != course_day["EX102"], (
        f"EX101 and EX102 (AI/Term1) should not share day: {course_day['EX101']}"
    )


def test_schedule_multi_bucket_course() -> None:
    """EX101 is in (AI,1) and (DS,1) — must satisfy both bucket day rules."""
    _setup_bucket_fixture()
    enrolled = build_enrolled_sets()
    _conflicts, adj = build_conflict_graph(enrolled)
    running = set(enrolled.keys())
    ptb, cb = build_plan_term_buckets(running)

    # EX101 is in (AI,1) with EX102, and in (DS,1) alone.
    # EX101 and EX102 must be on different days.
    assert ("AI", 1) in cb["EX101"]
    assert ("DS", 1) in cb["EX101"]
    assert "EX102" in ptb[("AI", 1)]

    slots = [
        {"index": 0, "day": "Sun", "period": "08:00-10:00"},
        {"index": 1, "day": "Sun", "period": "10:30-12:30"},
        {"index": 2, "day": "Mon", "period": "08:00-10:00"},
        {"index": 3, "day": "Mon", "period": "10:30-12:30"},
        {"index": 4, "day": "Tue", "period": "08:00-10:00"},
        {"index": 5, "day": "Tue", "period": "10:30-12:30"},
    ]

    course_list = sorted(enrolled.keys())
    result = schedule(
        course_list,
        adj,
        slots,
        enrolled_sets=enrolled,
        max_per_day=2,
        plan_term_buckets=ptb,
        course_buckets=cb,
    )

    course_day = {e["course_code"]: e["day"] for e in result}

    # EX101 and EX102 must be on different days (bucket AI/1)
    assert course_day["EX101"] != course_day["EX102"]

    # All 4 courses scheduled (no overflow)
    assert len(result) == 4
    for e in result:
        assert e["day"] != "OVERFLOW"


def test_full_pipeline_with_buckets() -> None:
    """End-to-end build_exam_timetable with ProgrammeRequirement data."""
    _setup_bucket_fixture()

    result = build_exam_timetable(
        label="Bucket Test Run",
        days=["Sun", "Mon", "Tue"],
        periods=["08:00-10:00", "10:30-12:30"],
    )

    # Should not be a feasibility error
    assert "feasibility_error" not in result

    assert result["courses_count"] == 4
    assert result["students_count"] == 3
    assert result["qa"]["conflict_count"] == 0

    # Bucket QA: zero day violations
    assert result["qa"]["bucket_count"] == 4  # AI/1, AI/2, DS/1, DS/2
    assert result["qa"]["bucket_day_violations_count"] == 0
    assert result["qa"]["bucket_day_violations"] == []

    # Buckets summary included
    assert "buckets_summary" in result
    assert result["bucket_count"] == 4

    # Verify the schedule: EX101 and EX102 (bucket AI/1) on different days
    course_day = {e["course_code"]: e["day"] for e in result["schedule"]}
    assert course_day["EX101"] != course_day["EX102"]

    # Persisted in DB
    run = ExamTimetableRun.objects.get(id=result["run_id"])
    stored = json.loads(run.result_json)
    assert stored["qa"]["bucket_day_violations_count"] == 0


# ── Selected-courses (2-step flow) tests ─────────────────────


def test_preview_courses() -> None:
    """build_enrolled_sets returns all studying courses with student sets."""
    _setup_fixture()
    enrolled = build_enrolled_sets()

    # Should return 4 courses with correct enrolled counts
    assert len(enrolled) == 4
    courses = sorted(
        [{"course_code": cc, "enrolled_count": len(sids)} for cc, sids in enrolled.items()],
        key=lambda c: c["course_code"],
    )
    assert courses[0] == {"course_code": "EX101", "enrolled_count": 2}
    assert courses[1] == {"course_code": "EX102", "enrolled_count": 2}
    assert courses[2] == {"course_code": "EX201", "enrolled_count": 2}
    assert courses[3] == {"course_code": "EX301", "enrolled_count": 1}


def test_build_with_selected_courses() -> None:
    """Build with only 2 of 4 courses selected → result has only those 2."""
    _setup_fixture()

    result = build_exam_timetable(
        label="Selected Courses Run",
        days=["Sun", "Mon", "Tue"],
        periods=["08:00-10:00", "10:30-12:30"],
        selected_courses=["EX101", "EX201"],
    )

    assert result["courses_count"] == 2
    assert len(result["schedule"]) == 2

    scheduled_codes = {e["course_code"] for e in result["schedule"]}
    assert scheduled_codes == {"EX101", "EX201"}

    # Only students enrolled in selected courses should be counted
    # s1: EX101 only (EX102 excluded), s2: EX101+EX201, s3: EX201 only (EX301 excluded)
    assert result["students_count"] == 3

    # QA: zero conflicts
    assert result["qa"]["conflict_count"] == 0

    # Persisted in DB
    run = ExamTimetableRun.objects.get(id=result["run_id"])
    stored = json.loads(run.result_json)
    assert stored["courses_count"] == 2


def test_build_without_selection_uses_all() -> None:
    """Build without selected_courses param → backward compatible, uses all."""
    _setup_fixture()

    result = build_exam_timetable(
        label="All Courses Run",
        days=["Sun", "Mon", "Tue"],
        periods=["08:00-10:00", "10:30-12:30"],
    )

    # All 4 studying courses included
    assert result["courses_count"] == 4
    assert len(result["schedule"]) == 4

    scheduled_codes = {e["course_code"] for e in result["schedule"]}
    assert scheduled_codes == {"EX101", "EX102", "EX201", "EX301"}
