import pytest

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.recommender import recommend_next_courses
from core.services.student_helpers import get_prerequisites, get_student_passed_and_studying

pytestmark = pytest.mark.django_db

# Use a unique program name to avoid collision with migration data
_PROGRAM = "TESTCS"


def _setup_fixture_data() -> None:
    Student.objects.update_or_create(
        student_id=441234,
        defaults={"program": _PROGRAM, "section": "M"},
    )

    cs101, _ = Course.objects.get_or_create(course_code="TCS101", defaults={"credit_hours": 3})
    cs102, _ = Course.objects.get_or_create(course_code="TCS102", defaults={"credit_hours": 3})
    Course.objects.get_or_create(course_code="TCS201", defaults={"credit_hours": 3})
    Course.objects.get_or_create(course_code="TGS101", defaults={"credit_hours": 2})

    StudentCourse.objects.get_or_create(
        student_id=441234, course=cs101, defaults={"status": "passed"}
    )
    StudentCourse.objects.get_or_create(
        student_id=441234, course=cs102, defaults={"status": "studying"}
    )

    ProgrammeRequirement.objects.update_or_create(
        program=_PROGRAM,
        course_code="TCS101",
        defaults={"type": "core", "programme_term": 1, "credit_hours": 3},
    )
    ProgrammeRequirement.objects.update_or_create(
        program=_PROGRAM,
        course_code="TCS102",
        defaults={"type": "core", "programme_term": 1, "credit_hours": 3},
    )
    ProgrammeRequirement.objects.update_or_create(
        program=_PROGRAM,
        course_code="TCS201",
        defaults={"type": "core", "programme_term": 2, "credit_hours": 3},
    )
    ProgrammeRequirement.objects.update_or_create(
        program=_PROGRAM,
        course_code="TGS101",
        defaults={"type": "gs", "programme_term": 2, "credit_hours": 2},
    )

    Prerequisite.objects.get_or_create(
        program=_PROGRAM,
        course_code="TCS201",
        prerequisite_course_code="TCS102",
    )


def test_recommender_uses_passed_or_studying_prereq() -> None:
    _setup_fixture_data()

    passed, studying = get_student_passed_and_studying(441234)
    assert "TCS101" in passed
    assert "TCS102" in studying

    prereqs = get_prerequisites("TCS201", _PROGRAM)
    assert prereqs == ["TCS102"]

    recs = recommend_next_courses(441234, current_academic_year=1445, current_semester=0)
    assert "TCS201" in recs
