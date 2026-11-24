"""Compare actual registered and proposed sections using the graduation engine."""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.student_sections import get_student_term_baseline
from core.services.timetable_snapshots import Snapshot

pytestmark = pytest.mark.django_db
STUDENT_ID = 4_699_871
YEAR, TERM = 1448, 1


def _seed_curriculum():
    # The student is in programme term 5. ML leads to Applied ML, then to Capstone.
    # A separate final lab first comes due in programme term 8.
    student = Student.objects.create(
        student_id=STUDENT_ID,
        registration_no=str(STUDENT_ID),
        name="Planner Sample Student",
        program="PLN",
        section="M",
        status="ACTIVE",
        current_registered_credits=3,
    )
    courses = [
        ("CS301", "Algorithms", 5, None),
        ("AI331", "Machine Learning", 5, None),
        ("AI451", "Applied Machine Learning", 6, "AI331"),
        ("AI491", "Machine Learning Capstone", 7, "AI451"),
        ("AI480", "AI Systems Lab", 8, None),
        ("STAT305", "Applied Statistics", 5, None),
    ]
    for code, name, due, prerequisite in courses:
        Course.objects.create(course_code=code, description=name, credit_hours=3)
        ProgrammeRequirement.objects.create(
            program=student.program,
            course_code=code,
            course_name=name,
            programme_term=due,
            credit_hours=3,
            type="Mandatory",
        )
        if prerequisite:
            Prerequisite.objects.create(
                program=student.program,
                course_code=code,
                prerequisite_course_code=prerequisite,
            )
    sections = {}
    for code, label, day in [
        ("CS301", "M1", "SUN"),
        ("AI331", "M1", "MON"),
        ("AI331", "M2", "WED"),
        ("STAT305", "M1", "TUE"),
    ]:
        section = TermSection.objects.create(
            course_key=code,
            course_code=code[:-3],
            course_number=code[-3:],
            course_name=next(row[1] for row in courses if row[0] == code),
            section=label,
            available_capacity=30,
            registered_count=3,
        )
        TermSectionProgram.objects.create(term_section=section, program=student.program)
        TermSectionMeeting.objects.create(
            term_section=section,
            day=day,
            start_time="09:00",
            end_time="10:15",
        )
        sections[(code, label)] = section
    StudentTermSection.objects.create(
        student_id=STUDENT_ID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=sections["CS301", "M1"],
        source="scraper_timetable",
    )
    return sections


def _option(name, *sections):
    return {
        "name": name,
        "mappings": [{"term_section_id": s.pk, "course_key": s.course_key} for s in sections],
    }


def _compare(options, keep=True):
    from core.services.planner_graduation import compare_planner_graduation

    retained = (
        get_student_term_baseline(STUDENT_ID, str(YEAR), str(TERM), snapshot=Snapshot.EFFECTIVE)
        if keep
        else None
    )
    return compare_planner_graduation(
        student_id=STUDENT_ID,
        year=YEAR,
        term=TERM,
        options=options,
        retained_baseline=retained,
    )


def test_gateway_moves_graduation_earlier_but_section_time_does_not():
    sections = _seed_curriculum()
    result = _compare(
        [
            _option("A1", sections["AI331", "M1"]),
            _option("A2", sections["AI331", "M2"]),
            _option("B1", sections["STAT305", "M1"]),
        ]
    )
    assert result["baseline"]["terms_including_planning_term"] == 5
    assert result["baseline"]["completion_academic_year"] == 1450
    assert result["baseline"]["completion_term"] == 1
    for label in ("A1", "A2"):
        row = result["options"][label]
        assert row["terms_including_planning_term"] == 4
        assert (row["completion_academic_year"], row["completion_term"]) == (1449, 2)
        assert row["delta_terms"] == -1
        assert any(
            "AI451" in (r.get("course_code"), r.get("related_course_code")) for r in row["reasons"]
        )
    assert result["options"]["B1"]["delta_terms"] == 0


def test_ignore_registered_gateway_can_delay_graduation():
    sections = _seed_curriculum()
    StudentTermSection.objects.create(
        student_id=STUDENT_ID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=sections["AI331", "M1"],
        source="scraper_timetable",
    )
    result = _compare([_option("C1", sections["STAT305", "M1"])], keep=False)
    assert result["baseline"]["terms_including_planning_term"] == 4
    assert result["options"]["C1"]["delta_terms"] == 1


def test_unfinished_forecast_has_no_precise_difference():
    sections = _seed_curriculum()
    Prerequisite.objects.create(
        program="PLN",
        course_code="AI480",
        prerequisite_course_code="NEVER100",
    )
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert result["baseline"]["status"] == "unresolved"
    assert result["options"]["A1"]["status"] == "unresolved"
    assert result["options"]["A1"]["delta_terms"] is None


def test_working_or_expected_timetable_cannot_stand_in_for_registration():
    sections = _seed_curriculum()
    StudentTermSection.objects.filter(student_id=STUDENT_ID).update(source="planner")
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert result["baseline"]["reason_code"] == "no_registered_timetable"
    assert result["options"]["A1"]["terms_including_planning_term"] == 4
    assert result["options"]["A1"]["delta_terms"] is None
