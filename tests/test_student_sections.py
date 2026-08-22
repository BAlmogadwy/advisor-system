from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
)
from core.services.timetable_snapshots import Snapshot

pytestmark = pytest.mark.django_db


def test_partial_section_mapping_keeps_unmapped_studying_courses_in_baseline() -> None:
    student = Student.objects.create(
        student_id=990846,
        registration_no="990846",
        name="Partial Mapping Student",
        program="DS",
        status="active",
    )
    mapped_course = Course.objects.create(
        course_code="MATH203",
        description="Calculus I",
        credit_hours=3,
    )
    unmapped_course = Course.objects.create(
        course_code="CS112",
        description="Programming II",
        credit_hours=4,
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="MATH203",
        course_name="Calculus I",
        programme_term=4,
        credit_hours=3,
        type="Mandatory",
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="CS112",
        course_name="Programming II",
        programme_term=4,
        credit_hours=4,
        type="Mandatory",
    )
    StudentCourse.objects.create(student=student, course=mapped_course, status="studying")
    StudentCourse.objects.create(student=student, course=unmapped_course, status="studying")

    term_section = TermSection.objects.create(
        course_code="MATH203",
        course_number="MATH203",
        course_key="MATH203",
        course_name="Calculus I",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=term_section,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1447",
        term="2",
        term_section=term_section,
        source="auto_from_studying",
    )

    baseline = append_unmapped_studying_courses(
        student.student_id,
        get_student_term_baseline(student.student_id, "1447", "2", snapshot=Snapshot.ANY),
    )

    by_code = {row["course_key"]: row for row in baseline}
    assert set(by_code) == {"MATH203", "CS112"}
    assert by_code["MATH203"]["credits"] == 3
    assert by_code["MATH203"]["day"] == "SUN"
    assert by_code["CS112"]["credits"] == 4
    assert by_code["CS112"]["source"] == "fallback_studying"
    assert by_code["CS112"]["day"] == ""


def test_scenario_owned_sections_do_not_become_registered_baseline() -> None:
    student = Student.objects.create(
        student_id=990847,
        registration_no="990847",
        name="Scenario Section Student",
        program="DS",
        status="active",
    )
    course = Course.objects.create(
        course_code="GS104",
        description="Islamic Values",
        credit_hours=2,
    )
    StudentCourse.objects.create(student=student, course=course, status="studying")
    scenario = TimetableScenario.objects.create(
        academic_year="1447",
        term="2",
        name="Generated scenario",
    )
    generated_section = TermSection.objects.create(
        scenario=scenario,
        source_tag="tw_auto",
        course_code="GS104",
        course_number="GS104",
        course_key="GS104",
        course_name="Islamic Values",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=generated_section,
        day="SUN",
        start_time="16:30",
        end_time="18:10",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1447",
        term="2",
        term_section=generated_section,
        source="auto_from_studying",
    )

    baseline = append_unmapped_studying_courses(
        student.student_id,
        get_student_term_baseline(student.student_id, "1447", "2", snapshot=Snapshot.ANY),
    )

    assert baseline == [
        {
            "course_code": "GS104",
            "course_key": "GS104",
            "course_name": "Islamic Values",
            "course_number": "",
            "section": "",
            "registered_count": None,
            "credits": 2,
            "day": "",
            "start_time": "",
            "end_time": "",
            "room": "",
            "instructor": "",
            "term_section_id": None,
            "source": "fallback_studying",
        }
    ]


def test_gender_section_filter_restricts_to_student_cohort() -> None:
    """Gender-segregated sections: a student only sees/uses their own cohort.

    Regression: the planner section catalog + build previously returned BOTH
    M and F sections for every course regardless of the student's gender.
    """
    from core.services.student_sections import (
        gender_section_filter,
        section_gender,
        student_gender,
    )

    male = Student.objects.create(
        student_id=700001, registration_no="700001", name="M", program="CS", section="M"
    )
    female = Student.objects.create(
        student_id=700002, registration_no="700002", name="F", program="CS", section="F"
    )
    for sec in ("M5", "M6", "F1", "F2", "YM4", "YF4", "ONLINE1"):
        TermSection.objects.create(
            course_code="CS999", course_number="", course_key="CS999", section=sec
        )

    def secs(gender: str) -> set[str]:
        return set(
            TermSection.objects.filter(course_key="CS999")
            .filter(gender_section_filter(gender))
            .values_list("section", flat=True)
        )

    # pure helpers
    assert section_gender("M5") == "M"
    assert section_gender("F1") == "F"
    assert section_gender("YM4") == "OTHER_BRANCH"
    assert section_gender("YF4") == "OTHER_BRANCH"
    assert section_gender("ONLINE1") == ""  # ungendered
    assert student_gender(male.student_id) == "M"
    assert student_gender(female.student_id) == "F"
    assert student_gender(999999) == ""  # unknown student

    # a male student sees only M* sections (+ ungendered), never F*
    assert secs("M") == {"M5", "M6", "ONLINE1"}
    # a female student sees only F* sections (+ ungendered), never M*
    assert secs("F") == {"F1", "F2", "ONLINE1"}
    # A staff/no-student catalogue may see both local cohorts, but never the other
    # branch. An invalid explicit cohort fails closed.
    assert secs("") == {"M5", "M6", "F1", "F2", "ONLINE1"}
    assert secs("X") == set()


def test_student_section_eligibility_refuses_unknown_student_and_other_branch() -> None:
    from core.services.student_sections import (
        UnknownStudentGender,
        section_is_available_to_student,
    )

    male = Student.objects.create(
        student_id=700003,
        registration_no="700003",
        name="M",
        program="CS",
        section="M",
    )
    other_branch = TermSection.objects.create(
        course_code="CS999",
        course_number="",
        course_key="CS999",
        section="YM4",
    )

    # Other-branch rejection occurs before programme membership can make the row
    # eligible, and an unknown student's cohort is never guessed.
    assert not section_is_available_to_student(other_branch, student_id=male.student_id)
    with pytest.raises(UnknownStudentGender):
        section_is_available_to_student(other_branch, student_id=799999)


def test_section_label_normalisers_agree_everywhere():
    """The checker's local copy and the canonical helper must never drift.

    answer_consistency deliberately avoids importing Django modules, so it
    carries a byte-for-byte local copy of this rule; this parity test is what
    makes that a copy rather than a fork.  The corpus covers the " M1" spacing
    class from issue #54 and the F01 leading-zero class from the production
    audit - the zero must SURVIVE normalisation, because F01 was an invention
    to flag, not a spelling of F1 to forgive.
    """
    from core.services.answer_consistency import _normalise_section_label
    from core.services.student_sections import normalize_section_label

    corpus = [" M1", "M 1", "m1", "F01", " f01 ", "YM4", " ym 4", "", None, "ONLINE"]
    for raw in corpus:
        assert normalize_section_label(raw) == _normalise_section_label(raw), raw

    assert normalize_section_label(" M1") == "M1"
    assert normalize_section_label("F01") == "F01"
    assert normalize_section_label("F01") != normalize_section_label("F1")
