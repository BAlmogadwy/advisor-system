from __future__ import annotations

import pytest

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TimetableScenario,
)
from core.services.elective_resolver import (
    _get_timetable_courses,
    resolve_elective_placeholders,
)

pytestmark = pytest.mark.django_db


def _section(course_key: str, section: str) -> TermSection:
    return TermSection.objects.create(
        course_code=course_key[:2],
        course_number=course_key[2:],
        course_key=course_key,
        course_name=course_key,
        section=section,
    )


def test_timetable_courses_uses_only_the_latest_verified_snapshot() -> None:
    student_id = 990_303
    previous = _section("OLD101", "M1")
    current_a = _section("NEW201", "M1")
    current_b = _section("NEW202", "M2")

    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1447",
        term="2",
        term_section=previous,
        source="scraper_timetable",
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        term_section=current_a,
        source="scraper_timetable",
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        term_section=current_b,
        source="scraper_timetable",
    )

    planned = _section("PLAN301", "M3")
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1449",
        term="1",
        term_section=planned,
        source="planner",
    )
    scenario = TimetableScenario.objects.create(
        academic_year="1450",
        term="1",
        name="Future scenario",
    )
    scenario_section = TermSection.objects.create(
        scenario=scenario,
        course_code="SC",
        course_number="401",
        course_key="SC401",
        course_name="Scenario-only course",
        section="M4",
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1450",
        term="1",
        term_section=scenario_section,
        source="scraper_timetable",
    )

    assert _get_timetable_courses(student_id) == {"NEW201", "NEW202"}
    assert _get_timetable_courses(
        student_id,
        academic_year="1448",
        term="1",
    ) == {"NEW201", "NEW202"}


def test_timetable_courses_is_empty_after_current_snapshot_is_cleared() -> None:
    assert _get_timetable_courses(990_304) == set()


def test_unmapped_training_course_does_not_fulfil_elective_placeholders() -> None:
    student_id = 990_305
    Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Summer trainee",
        program="AI",
        section="M",
    )
    for code in ("AI1", "GSE1"):
        Course.objects.create(course_code=code, department=code.rstrip("1"))
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            programme_term=8,
            credit_hours=3,
        )
        StudentCourse.objects.create(
            student_id=student_id,
            course=Course.objects.get(course_code=code),
            status="studying",
            mark=49 if code == "GSE1" else None,
        )

    training = Course.objects.create(
        course_code="AI490",
        department="AI",
        description="Field Training",
        credit_hours=2,
    )
    StudentCourse.objects.create(
        student_id=student_id,
        course=training,
        status="studying",
    )
    training_section = _section("AI490", "M1")
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1447",
        term="3",
        term_section=training_section,
        source="scraper_timetable",
    )

    preview = resolve_elective_placeholders(
        "AI",
        student_ids=[student_id],
        student_snapshots={student_id: ("1447", "3")},
        dry_run=True,
    )

    assert preview["reconciled_count"] == 2
    assert set(
        StudentCourse.objects.filter(student_id=student_id).values_list("status", flat=True)
    ) == {"studying"}

    result = resolve_elective_placeholders(
        "AI",
        student_ids=[student_id],
        student_snapshots={student_id: ("1447", "3")},
    )

    assert result["total_updates"] == 0
    assert result["reconciled_count"] == 2
    statuses = dict(
        StudentCourse.objects.filter(
            student_id=student_id,
            course__course_code__in={"AI1", "GSE1"},
        ).values_list("course__course_code", "status")
    )
    assert statuses == {"AI1": "not_taken", "GSE1": "failed"}


def test_exact_term_mapping_resolves_versioned_programme_placeholder() -> None:
    student_id = 990_306
    Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Mapped elective",
        program="CS2",
        section="M",
    )
    placeholder = Course.objects.create(course_code="CS1", department="CS")
    ProgrammeRequirement.objects.create(
        program="CS2",
        course_code="CS1",
        programme_term=7,
        credit_hours=3,
    )
    StudentCourse.objects.create(
        student_id=student_id,
        course=placeholder,
        status="not_taken",
    )
    elective = ElectiveCourse.objects.create(
        course_code="CS403",
        course_name="Mapped CS Elective",
        programme="CS",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="CS",
        placeholder_code="CS1",
        elective=elective,
    )
    actual = Course.objects.create(course_code="CS403", department="CS")
    StudentCourse.objects.create(
        student_id=student_id,
        course=actual,
        status="studying",
    )
    actual_section = _section("CS403", "M2")
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        term_section=actual_section,
        source="scraper_timetable",
    )

    result = resolve_elective_placeholders(
        "CS2",
        student_ids=[student_id],
        student_snapshots={student_id: ("1448", "1")},
    )

    assert result["updates"] == [
        {
            "student_id": student_id,
            "placeholder": "CS1",
            "placeholder_type": "program_elective",
            "term": 7,
            "resolved_with": "CS403",
        }
    ]
    assert (
        StudentCourse.objects.get(
            student_id=student_id,
            course__course_code="CS1",
        ).status
        == "studying"
    )
