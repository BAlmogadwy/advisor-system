from collections import Counter

import pytest
from pytest import MonkeyPatch

from core.models import Course, ElectiveCourse, ElectiveTermMapping, Student, StudentCourse
from core.services import reporting

pytestmark = pytest.mark.django_db


def test_aggregate_cache_separates_strict_and_relaxed(monkeypatch: MonkeyPatch) -> None:
    reporting.clear_aggregate_cache()
    calls: list[bool] = []

    monkeypatch.setattr(reporting, "get_student_ids", lambda **kwargs: [441000001])

    def fake_batch_recommend(
        student_ids: list[int],
        program: str,
        year: int,
        semester: int,
        *,
        strict_passed_only: bool = False,
    ) -> dict[int, list[str]]:
        calls.append(strict_passed_only)
        code = "STRICT101" if strict_passed_only else "RELAX101"
        return {student_ids[0]: [code]}

    monkeypatch.setattr(reporting, "batch_recommend", fake_batch_recommend)

    strict = reporting.build_aggregate_counts(
        1448,
        1,
        program="AI",
        strict_passed_only=True,
    )
    relaxed = reporting.build_aggregate_counts(
        1448,
        1,
        program="AI",
        strict_passed_only=False,
    )
    strict_cached = reporting.build_aggregate_counts(
        1448,
        1,
        program="AI",
        strict_passed_only=True,
    )

    assert strict == (1, Counter({"STRICT101": 1}))
    assert relaxed == (1, Counter({"RELAX101": 1}))
    assert strict_cached == strict
    assert calls == [True, False]
    reporting.clear_aggregate_cache()


def test_elective_resolution_applies_mode_to_courses_and_hour_gates() -> None:
    student = Student.objects.create(
        student_id=441000001,
        program="DS",
        total_earned_credits=60,
        current_registered_credits=30,
    )
    prerequisite = Course.objects.create(course_code="AI201", credit_hours=3)
    StudentCourse.objects.create(
        student=student,
        course=prerequisite,
        status="studying",
    )

    course_prereq_elective = ElectiveCourse.objects.create(
        programme="DS",
        course_code="DS481",
        course_name="Course prerequisite elective",
        prerequisites_csv="AI201",
    )
    hour_gate_elective = ElectiveCourse.objects.create(
        programme="DS",
        course_code="DS482",
        course_name="Hour gate elective",
        prerequisites_csv="80(HOURS)",
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="DS",
        placeholder_code="DS2",
        elective=course_prereq_elective,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="DS",
        placeholder_code="DS3",
        elective=hour_gate_elective,
    )

    relaxed = reporting.resolve_elective_recommendations(
        {student.student_id: ["DS2", "DS3"]},
        year=1448,
        semester=1,
        program="DS",
        strict_passed_only=False,
    )
    strict = reporting.resolve_elective_recommendations(
        {student.student_id: ["DS2", "DS3"]},
        year=1448,
        semester=1,
        program="DS",
        strict_passed_only=True,
    )

    assert relaxed[student.student_id] == ["DS481", "DS482"]
    assert strict[student.student_id] == []
