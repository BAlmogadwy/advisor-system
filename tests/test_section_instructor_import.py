"""Per-section instructor assignment — parser, model constraints, and importer.

``CourseInstructor`` records who *may* teach a course; it cannot say which of
CS113's thirty-one sections a person holds.  ``SectionInstructor`` records the
registrar's actual per-section decision, parsed from
``facultySectionsAvilableSeats.do``.

The load-bearing test here is ``test_import_touches_nothing_else``: this feature
is additive, and the moment it writes ``TermSectionMeeting.instructor`` it wakes
four enforcement paths that have never run against real data.  That test is what
makes "no behaviour change" checkable rather than asserted.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from core.models import (
    CourseInstructor,
    Instructor,
    SectionInstructor,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.faculty_sections_import import (
    parse_faculty_sections,
    summarise,
)

# --------------------------------------------------------------------------
# Fixtures — a minimal stand-in for the registrar's report.
# --------------------------------------------------------------------------

_HEADER_NOISE = "Someone Name SOMEONE@taibahu.edu.sa 21 Sept 2026 12.21:10"


def _row(
    *,
    registered="24",
    capacity="24",
    thu="",
    wed="10:30-11:45",
    tue="",
    mon="10:15-11:55",
    sun="10:30-11:45",
    instructor="عبدالله أحمد حويمد الصاعدي",
    section="M27",
    course_name="أساسيات البرمجة",
    number="111",
    dept="CS",
    serial="1",
):
    """Build one report row.  Cells are in the report's right-to-left order."""
    cells = [
        registered,
        capacity,
        thu,
        wed,
        tue,
        mon,
        sun,
        instructor,
        section,
        course_name,
        number,
        dept,
        serial,
    ]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _page(*rows: str) -> bytes:
    return ("<html><body><table>" + "".join(rows) + "</table></body></html>").encode("utf-8")


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def test_parses_a_row_in_right_to_left_column_order():
    result = parse_faculty_sections(_page(_row()))
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.course_key == "CS111"
    assert row.section == "M27"
    assert row.instructor == "عبدالله أحمد حويمد الصاعدي"
    assert row.capacity == 24
    assert row.registered == 24
    assert row.meetings == {"sun": "10:30-11:45", "mon": "10:15-11:55", "wed": "10:30-11:45"}


def test_malformed_rows_are_counted_not_silently_dropped():
    """A transposed report must surface as ``skipped``, never as bad data.

    If the registrar reorders columns, the department cell stops looking like a
    department.  That has to be visible.
    """
    good = _row()
    transposed = _row(dept="111", number="CS")  # the two swapped
    result = parse_faculty_sections(_page(good, transposed))
    assert len(result.rows) == 1
    assert result.skipped == 1


def test_print_header_rows_are_ignored():
    result = parse_faculty_sections(_page(_row(course_name=_HEADER_NOISE), _row(section="M28")))
    assert [r.section for r in result.rows] == ["M28"]


def test_missing_instructor_is_empty_not_a_dash():
    result = parse_faculty_sections(_page(_row(instructor="-", wed="-", mon="-", sun="-")))
    row = result.rows[0]
    assert row.instructor == ""
    assert row.has_instructor is False
    assert row.meetings == {}


def test_repeated_section_merges_capacity_and_is_not_a_contradiction():
    """The report prints each section twice, once with capacity 0.

    Merging those keeps ``duplicates`` meaningful; if the benign repeat counted,
    the alarm would always be ringing.
    """
    result = parse_faculty_sections(_page(_row(capacity="0"), _row(capacity="16")))
    assert len(result.rows) == 1
    assert result.rows[0].capacity == 16
    assert result.duplicates == 0


def test_conflicting_instructor_for_one_section_is_reported():
    result = parse_faculty_sections(_page(_row(instructor="Dr A"), _row(instructor="Dr B")))
    assert result.duplicates == 1
    assert len(result.rows) == 1  # first wins, but the caller is told


def test_summary_counts_are_per_section_not_per_row():
    page = _page(_row(section="M1"), _row(section="M1"), _row(section="M2", instructor=""))
    summary = summarise(parse_faculty_sections(page))
    assert summary["sections"] == 2
    assert summary["with_instructor"] == 1
    assert summary["campuses"] == {"M": 2}


# --------------------------------------------------------------------------
# Model constraints
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_course_key_and_section_are_normalised_on_write():
    person = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    link = SectionInstructor.objects.create(
        course_key=" cs111 ", section=" m27 ", instructor=person
    )
    link.refresh_from_db()
    assert link.course_key == "CS111"
    assert link.section == "M27"


@pytest.mark.django_db
def test_one_primary_per_global_section():
    """Without this, 'primary' would mean 'whichever row was inserted first' —
    the defect that migration 0035 fixed at course level."""
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    b = Instructor.objects.create(full_name="Dr B", normalised_name="dr b")
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=b)


@pytest.mark.django_db
def test_a_second_non_primary_instructor_is_allowed():
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    b = Instructor.objects.create(full_name="Dr B", normalised_name="dr b")
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=b, role="co")
    assert SectionInstructor.objects.filter(course_key="CS111", section="M27").count() == 2


@pytest.mark.django_db
def test_the_same_person_cannot_be_linked_twice_to_one_global_section():
    """A plain unique constraint would NOT enforce this: ``scenario`` is nullable
    and SQL treats NULLs as distinct.  The partial index is what makes it hold."""
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a, role="co")


@pytest.mark.django_db
def test_a_scenario_section_is_independent_of_the_global_one():
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    scenario = TimetableScenario.objects.create(name="s1")
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    SectionInstructor.objects.create(
        scenario=scenario, course_key="CS111", section="M27", instructor=a
    )
    assert SectionInstructor.objects.count() == 2


@pytest.mark.django_db
def test_an_assignment_survives_deletion_of_the_section_row():
    """The whole point of the natural key.  Five paths delete ``TermSection``
    rows; an assignment must not be collateral damage of any of them."""
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    ts = TermSection.objects.create(
        course_code="CS", course_number="111", course_key="CS111", section="M27"
    )
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    ts.delete()
    assert SectionInstructor.objects.filter(course_key="CS111", section="M27").exists()


# --------------------------------------------------------------------------
# Importer
# --------------------------------------------------------------------------


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "sections.html"
    path.write_bytes(
        _page(
            _row(section="M27", instructor="أحمد سليمان داود مايت"),
            _row(section="M28", instructor="رجاء محمد رجاء الحجيلي", serial="2"),
            _row(section="M29", instructor="", serial="3"),
        )
    )
    return str(path)


@pytest.mark.django_db
def test_import_creates_people_and_assignments(report):
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    assert Instructor.objects.count() == 2
    assert SectionInstructor.objects.count() == 2
    link = SectionInstructor.objects.get(section="M28")
    assert link.instructor.full_name == "رجاء محمد رجاء الحجيلي"
    assert link.role == "primary"
    assert link.source == "registrar_faculty_sections"
    # A section with no instructor produces no row at all.
    assert not SectionInstructor.objects.filter(section="M29").exists()


@pytest.mark.django_db
def test_import_is_idempotent(report):
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    assert Instructor.objects.count() == 2
    assert SectionInstructor.objects.count() == 2


@pytest.mark.django_db
def test_reassignment_replaces_the_primary_rather_than_adding_one(report, tmp_path):
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    changed = tmp_path / "changed.html"
    changed.write_bytes(_page(_row(section="M27", instructor="شخص آخر")))
    call_command("import_section_instructors", "--file", str(changed), stdout=io.StringIO())
    links = SectionInstructor.objects.filter(section="M27")
    assert links.count() == 1
    assert links.first().instructor.full_name == "شخص آخر"


@pytest.mark.django_db
def test_import_reuses_an_existing_instructor_by_normalised_name(report):
    Instructor.objects.create(
        full_name="أحمد سليمان داود مايت", normalised_name="أحمد سليمان داود مايت".casefold()
    )
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    assert Instructor.objects.filter(full_name="أحمد سليمان داود مايت").count() == 1


@pytest.mark.django_db
def test_dry_run_writes_nothing(report):
    call_command("import_section_instructors", "--file", report, "--dry-run", stdout=io.StringIO())
    assert Instructor.objects.count() == 0
    assert SectionInstructor.objects.count() == 0


@pytest.mark.django_db
def test_import_keeps_an_assignment_whose_section_we_do_not_have(report):
    """Resolution is a join, not a dependency: the registrar may publish a
    section before we scrape it."""
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    assert TermSection.objects.count() == 0
    assert SectionInstructor.objects.count() == 2


@pytest.mark.django_db
def test_import_touches_nothing_else(report):
    """The inertness guarantee.

    Writing ``TermSectionMeeting.instructor`` activates the greedy clash filter,
    the workspace conflict badge, ``validate_placement``'s critical count and the
    repair-eligibility gate — none of which has ever run against a populated
    field.  This import must leave that field, and ``CourseInstructor``, exactly
    as it found them.
    """
    ts = TermSection.objects.create(
        course_code="CS", course_number="111", course_key="CS111", section="M27"
    )
    meeting = TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="10:30", end_time="11:45", room="A1"
    )
    before_meetings = list(
        TermSectionMeeting.objects.values_list("id", "instructor", "room", "day")
    )
    before_courses = CourseInstructor.objects.count()

    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())

    meeting.refresh_from_db()
    assert meeting.instructor == ""
    assert list(TermSectionMeeting.objects.values_list("id", "instructor", "room", "day")) == (
        before_meetings
    )
    assert CourseInstructor.objects.count() == before_courses
