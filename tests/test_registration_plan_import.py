"""Seeding registered timetables from a registration-plan workbook.

The importer's job is to refuse everything it cannot prove. Two distinctions carry
the whole design:

  * a section resolved by TIMES, one-to-one, or not at all — the workbook labels
    sections `AI:S1` while the database holds `M1`, so nothing matches as a string,
    and a near-match would seat a student in the wrong section of the right course;
  * a course with NO sections is a reported GAP, while a course WITH sections whose
    label did not resolve is a failure. Collapsing those hides the one that matters.
"""

from __future__ import annotations

import pytest

from core.models import Student, StudentTermSection, TermSection, TermSectionMeeting
from core.services.registration_plan_import import (
    apply_plan,
    build_plan,
    check_students_exist,
    parse_meetings,
)

pytestmark = pytest.mark.django_db

YEAR, TERM = "1448", "1"


def _section(course, meetings, section="M1"):
    ts = TermSection.objects.create(
        course_code=course[:2],
        course_number=course[2:],
        course_key=course,
        course_name=course,
        section=section,
    )
    for day, start, end in meetings:
        TermSectionMeeting.objects.create(term_section=ts, day=day, start_time=start, end_time=end)
    return ts


@pytest.fixture
def world():
    """Two sections of one course at different times, plus a single-section course."""
    Student.objects.update_or_create(
        student_id=700001, defaults={"name": "A", "program": "AI", "section": "M"}
    )
    Student.objects.update_or_create(
        student_id=700002, defaults={"name": "B", "program": "AI", "section": "M"}
    )
    return {
        "s1": _section("AI331", [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")], "M1"),
        "s2": _section("AI331", [("MON", "10:30", "11:45"), ("WED", "10:30", "11:45")], "M2"),
        "solo": _section("CS323", [("TUE", "13:00", "14:15")], "M1"),
    }


def _rosters(*rows):
    return list(rows)


def _detail(*rows):
    return list(rows)


# ── sections resolve on times, one-to-one ────────────────────────


def test_a_section_is_resolved_by_its_meeting_times(world):
    """The labels cannot be matched: the workbook says `AI:S1`, the database `M1`."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "Mon 09:00-10:15", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert len(plan.links) == 1
    assert plan.links[0]["term_section_id"] == world["s1"].id


def test_the_other_section_of_the_same_course_resolves_to_the_other_row(world):
    """Proves the match is on times and not on 'first section of this course'."""
    plan = build_plan(
        _rosters(("AI331", "AI:S2", "Mon 10:30-11:45; Wed 10:30-11:45", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S2", "Mon 10:30-11:45", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.links[0]["term_section_id"] == world["s2"].id


def test_times_matching_two_sections_is_refused_not_guessed(world):
    """A duplicate makes the resolution ambiguous. Seating the student in either
    would be a coin flip they cannot see."""
    _section("AI331", [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")], "M3")
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "AMBIGUOUS_SECTION" for p in plan.problems), plan.problems


def test_a_time_mismatch_on_a_multi_section_course_is_refused(world):
    """The workbook moved some section times. Where the course has one section the
    fallback is unambiguous; where it has two, it is not."""
    plan = build_plan(
        _rosters(("AI331", "AI:S9", "Fri 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S9", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "AMBIGUOUS_AFTER_TIME_MISMATCH" for p in plan.problems)
    assert plan.time_disagreements, "the disagreement was not reported"


def test_a_time_mismatch_on_a_single_section_course_is_reported_and_linked(world):
    """Only the clock moved; the course still has exactly one section."""
    plan = build_plan(
        _rosters(("CS323", "AI:S1", "Fri 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "CS323", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok
    assert plan.links[0]["term_section_id"] == world["solo"].id
    assert len(plan.time_disagreements) == 1
    assert plan.time_disagreements[0]["course"] == "CS323"


# ── the two kinds of "cannot place" ──────────────────────────────


def test_rows_with_no_timeslot_are_skipped_not_failed(world):
    """`Project` and `Foundation retake` carry section `—` and say so themselves."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI491", "Project", "—", "no timeslot (graduation project)", "", "", ""),
            (700001, "AI", "CS111", "Foundation retake", "—", "first-year schedule", "", "", ""),
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok
    assert plan.skipped_unplaceable == 2
    assert len(plan.links) == 1


def test_a_course_with_no_sections_is_a_reported_gap_not_a_failure(world):
    """`GSE1` and `FE2` have real times in the plan and no section anywhere. The
    term is still seedable; the gap is what the operator must be told."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (
                700001,
                "AI",
                "GSE1",
                "Online elective",
                "online (evening)",
                "Sun 15:50-17:30 (online)",
                "",
                "",
                "",
            ),
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert "GSE1" in plan.uncovered
    assert plan.uncovered["GSE1"][0]["times"] == [("SUN", "15:50")], plan.uncovered
    assert len(plan.links) == 1


def test_a_course_that_HAS_sections_but_did_not_resolve_still_fails(world):
    """The distinction that matters. `AI331` exists, so an unresolved label is a
    mapping fault — seeding around it would put a student somewhere arbitrary."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "SOME:OTHER", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "UNRESOLVED_SECTION" for p in plan.problems)
    assert "AI331" not in plan.uncovered, "a mapping fault was filed as a coverage gap"


# ── writing ──────────────────────────────────────────────────────


def test_applying_replaces_only_the_students_in_the_plan(world):
    """A student the plan could not place keeps what they have. Two were blocked in
    the real workbook; an import that never considered them must not empty them."""
    StudentTermSection.objects.create(
        student_id=700002, academic_year=YEAR, term=TERM, term_section=world["solo"]
    )
    StudentTermSection.objects.create(
        student_id=700001, academic_year=YEAR, term=TERM, term_section=world["solo"]
    )
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    result = apply_plan(plan, YEAR, TERM)
    assert result["removed"] == 1 and result["written"] == 1

    mine = list(
        StudentTermSection.objects.filter(student_id=700001).values_list(
            "term_section_id", flat=True
        )
    )
    assert mine == [world["s1"].id], "the replaced student kept a stale row"
    assert StudentTermSection.objects.filter(student_id=700002).count() == 1, (
        "a student absent from the plan was emptied"
    )


def test_an_invalid_plan_cannot_be_applied(world):
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "NOPE", "", "", "", "")),
        YEAR,
        TERM,
    )
    with pytest.raises(ValueError):
        apply_plan(plan, YEAR, TERM)
    assert StudentTermSection.objects.count() == 0


def test_an_unknown_student_is_never_created(world):
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((999999, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert check_students_exist(plan) == [999999]


def test_the_same_section_twice_for_one_student_is_one_seat(world):
    """A lecture row and a lab row name the same section. That is one registration."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "Mon 09:00-10:15", "", "", ""),
            (700001, "AI", "AI331", "Core", "AI:S1", "Wed 09:00-10:15", "Sun 10:30-12:10", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert len(plan.links) == 1


def test_the_term_written_is_the_term_asked_for(world):
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        "1449",
        "2",
    )
    apply_plan(plan, "1449", "2")
    row = StudentTermSection.objects.get(student_id=700001)
    assert (row.academic_year, row.term) == ("1449", "2")


# ── the parser ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("Mon 09:00-10:15", {("MON", "09:00")}),
        ("Mon 09:00-10:15; Wed 09:00-10:15", {("MON", "09:00"), ("WED", "09:00")}),
        ("Sun 15:50-17:30 (online)", {("SUN", "15:50")}),
        ("no timeslot (graduation project)", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_meeting_times_are_parsed_from_the_workbook_prose(cell, expected):
    assert parse_meetings(cell) == expected
