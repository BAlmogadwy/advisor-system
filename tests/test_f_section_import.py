"""Regression tests for the female expected-timetable importer.

The importer consumes optimiser workbook rows, but these tests deliberately stop
at its row-dictionary boundary.  No production workbook or developer database is
needed to prove the important contract: an F plan is forecast data, never
registrar evidence, and replacing it is scoped to its exact source and students.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models import Student, StudentTermSection, TermSection, TermSectionMeeting
from core.services.f_section_import import apply_plan, build_plan, parse_days
from core.services.timetable_snapshots import SnapshotClass, classify_source

pytestmark = pytest.mark.django_db

YEAR = "1448"
TERM = "1"
SOURCE = "registration_plan_1448_t1"


def _schedule(
    course: str,
    section: str,
    *,
    days: object = "Monday + Wednesday",
    start: object = "09:00:00",
    end: object = "10:15:00",
    room: object = "F-101",
    units: object = 3,
) -> dict[str, Any]:
    letters = "".join(char for char in course if char.isalpha())
    number = "".join(char for char in course if char.isdigit())
    return {
        "Course": course,
        "Code": letters,
        "No.": number,
        "Course Name": f"Course {course}",
        "Section": section,
        "Units": units,
        "Days (EN)": days,
        "From": start,
        "To": end,
        "Room": room,
    }


def _roster(student_id: object, program: str, course: str, section: str) -> dict[str, Any]:
    return {
        "Student ID": student_id,
        "Program": program,
        "Course": course,
        "Section": section,
    }


def _student(student_id: int, *, program: str = "AI") -> Student:
    return Student.objects.create(
        student_id=student_id,
        name=f"Student {student_id}",
        program=program,
        section="F",
    )


def _term_section(course: str, section: str) -> TermSection:
    return TermSection.objects.create(
        source_tag="fixture",
        course_name=course,
        course_code="".join(char for char in course if char.isalpha()),
        course_number="".join(char for char in course if char.isdigit()),
        course_key=course,
        section=section,
    )


def _link(
    student_id: int,
    term_section: TermSection,
    source: str,
    *,
    year: str = YEAR,
    term: str = TERM,
) -> StudentTermSection:
    return StudentTermSection.objects.create(
        student_id=student_id,
        academic_year=year,
        term=term,
        term_section=term_section,
        source=source,
    )


def test_build_plan_parses_normalises_and_preserves_exact_section_identity() -> None:
    schedule = [
        _schedule("CS 372", "F1", days="Monday + Wednesday", room="F-101"),
        _schedule("CS 372", "F1", days="Tuesday", start="13:00", end="14:15", room="F-102"),
        _schedule("AI491", "F32", days="—", start=None, end=None, room=None, units="1.0"),
    ]
    roster = [
        _roster("4500001", "ai", "CS372", "F1"),
        _roster(4500001, "AI", "CS 372", "F1"),  # an exact duplicate roster row
        _roster(4500001, "AI", "AI491", "F32"),
    ]

    plan = build_plan(schedule, roster)

    assert plan.ok, [str(problem) for problem in plan.problems]
    assert list(plan.sections) == ["CS372|F1", "AI491|F32"]
    assert plan.sections["CS372|F1"].meetings == [
        ("MON", "09:00", "10:15", "F-101"),
        ("WED", "09:00", "10:15", "F-101"),
        ("TUE", "13:00", "14:15", "F-102"),
    ]
    assert plan.sections["AI491|F32"].meetings == []
    assert plan.sections["AI491|F32"].credits == 1
    assert plan.links == [(4500001, "CS372|F1"), (4500001, "AI491|F32")]
    assert plan.students == {4500001}
    assert plan.roster_rows_read == 3
    assert plan.duplicate_rows == 1


@pytest.mark.parametrize(
    ("schedule", "roster", "problem_code"),
    [
        (
            [_schedule("CS372", "M1")],
            [_roster(4500001, "AI", "CS372", "M1")],
            "NOT_A_FEMALE_SECTION",
        ),
        (
            [_schedule("CS372", "F1", days="Funday")],
            [_roster(4500001, "AI", "CS372", "F1")],
            "UNKNOWN_DAY",
        ),
        (
            [_schedule("CS372", "F1", start=None)],
            [_roster(4500001, "AI", "CS372", "F1")],
            "MISSING_TIME",
        ),
        (
            [_schedule("CS372", "F1", start="10:15", end="09:00")],
            [_roster(4500001, "AI", "CS372", "F1")],
            "BAD_TIME",
        ),
        (
            [_schedule("CS372", "F1")],
            [_roster(4500001, "SE", "CS372", "F1")],
            "PROGRAM_OUT_OF_SCOPE",
        ),
        (
            [_schedule("CS372", "F1")],
            [_roster(4500001, "AI", "CS372", "F9")],
            "SECTION_NOT_SCHEDULED",
        ),
    ],
    ids=[
        "male-section",
        "unknown-day",
        "missing-time",
        "reversed-time",
        "programme-out-of-scope",
        "roster-section-not-scheduled",
    ],
)
def test_build_plan_refuses_unsafe_or_unresolvable_rows(
    schedule: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    problem_code: str,
) -> None:
    plan = build_plan(schedule, roster)

    assert not plan.ok
    assert problem_code in {problem.code for problem in plan.problems}


def test_build_plan_refuses_two_sections_of_one_course_for_one_student() -> None:
    plan = build_plan(
        [_schedule("CS372", "F1"), _schedule("CS372", "F2", start="10:30", end="11:45")],
        [_roster(4500001, "AI", "CS372", "F1"), _roster(4500001, "AI", "CS372", "F2")],
    )

    assert not plan.ok
    assert "TWO_SECTIONS_ONE_COURSE" in {problem.code for problem in plan.problems}


@pytest.mark.parametrize("source", ["scraper_timetable", "planner", "manual", ""])
def test_apply_rejects_every_non_expected_source_before_writing(source: str) -> None:
    _student(4500001)
    plan = build_plan(
        [_schedule("CS372", "F1")],
        [_roster(4500001, "AI", "CS372", "F1")],
    )

    with pytest.raises(ValueError, match="not an expected-plan source"):
        apply_plan(plan, YEAR, TERM, source)

    assert TermSection.objects.count() == 0
    assert StudentTermSection.objects.count() == 0


def test_apply_replaces_only_same_expected_source_and_preserves_registrar_rows() -> None:
    first = _student(4500001)
    second = _student(4500002, program="DS")
    old_expected = _term_section("OLD101", "F9")
    registrar_section = _term_section("REG101", "F8")
    other_expected = _term_section("OTHER101", "F7")

    stale_for_target_student = _link(first.student_id, old_expected, SOURCE)
    registrar = _link(first.student_id, registrar_section, "scraper_timetable")
    other_source = _link(first.student_id, other_expected, "registration_plan_1448_t2")
    other_term = _link(first.student_id, old_expected, SOURCE, term="2")
    other_student = _link(second.student_id, old_expected, SOURCE)

    plan = build_plan(
        [_schedule("CS372", "F1")],
        [_roster(first.student_id, "AI", "CS372", "F1")],
    )
    result = apply_plan(plan, YEAR, TERM, SOURCE)

    assert result == {
        "sections_created": 1,
        "meetings_written": 2,
        "sections_total": 1,
        "removed": 1,
        "written": 1,
        "students": 1,
    }
    assert not StudentTermSection.objects.filter(pk=stale_for_target_student.pk).exists()
    for preserved in (registrar, other_source, other_term, other_student):
        assert StudentTermSection.objects.filter(pk=preserved.pk).exists(), preserved.pk

    replacement = StudentTermSection.objects.get(
        student_id=first.student_id,
        academic_year=YEAR,
        term=TERM,
        source=SOURCE,
    )
    assert replacement.term_section.course_key == "CS372"
    assert replacement.term_section.section == "F1"
    assert classify_source(replacement.source) is SnapshotClass.EXPECTED
    assert (
        StudentTermSection.objects.filter(
            student_id=first.student_id,
            academic_year=YEAR,
            term=TERM,
            source="scraper_timetable",
        ).count()
        == 1
    )


def test_apply_writes_the_exact_planned_links_source_and_meetings() -> None:
    _student(4500001, program="AI2")
    _student(4500002, program="DS2")
    plan = build_plan(
        [
            _schedule("CS372", "F1", days="Sunday / Tuesday", room="B12"),
            _schedule("AI491", "F32", days="بدون موعد", start=None, end=None, room=None),
        ],
        [
            _roster(4500001, "AI2", "CS372", "F1"),
            _roster(4500001, "AI2", "AI491", "F32"),
            _roster(4500002, "DS2", "CS372", "F1"),
        ],
    )

    result = apply_plan(plan, YEAR, TERM, SOURCE)

    assert result["written"] == len(plan.links) == 3
    actual = set(
        StudentTermSection.objects.filter(academic_year=YEAR, term=TERM).values_list(
            "student_id",
            "term_section__course_key",
            "term_section__section",
            "source",
        )
    )
    assert actual == {
        (4500001, "CS372", "F1", SOURCE),
        (4500001, "AI491", "F32", SOURCE),
        (4500002, "CS372", "F1", SOURCE),
    }
    assert set(
        TermSectionMeeting.objects.filter(term_section__course_key="CS372").values_list(
            "day", "start_time", "end_time", "room"
        )
    ) == {
        ("SUN", "09:00", "10:15", "B12"),
        ("TUE", "09:00", "10:15", "B12"),
    }
    assert not TermSectionMeeting.objects.filter(term_section__course_key="AI491").exists()
    assert {classify_source(source) for *_identity, source in actual} == {SnapshotClass.EXPECTED}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Monday + Wednesday", ["MON", "WED"]),
        ("Sunday / Tuesday", ["SUN", "TUE"]),
        ("Thursday and Saturday", ["THU", "SAT"]),
        ("—", []),
        (None, []),
    ],
)
def test_parse_days_accepts_the_workbook_forms(raw: object, expected: list[str]) -> None:
    assert parse_days(raw) == expected
