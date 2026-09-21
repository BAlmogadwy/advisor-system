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


#: Every table the importer could plausibly reach.  Whole rows, not chosen
#: columns: an earlier version of this test snapshotted four of the nine meeting
#: fields and compared ``CourseInstructor`` counts that were both zero, so it
#: would have passed while the import rewrote start_time, room or the section
#: itself.
_MUST_NOT_CHANGE = (
    "TermSection",
    "TermSectionMeeting",
    "TermSectionProgram",
    "CourseInstructor",
    "StudentTermSection",
)


def _snapshot_everything():
    from core import models as m

    return {
        name: list(getattr(m, name).objects.order_by("pk").values()) for name in _MUST_NOT_CHANGE
    }


@pytest.mark.django_db
def test_import_touches_nothing_else(report):
    """The inertness guarantee, and the reason this feature is safe to land.

    Writing ``TermSectionMeeting.instructor`` activates the greedy clash filter,
    the workspace conflict badge, ``validate_placement``'s critical count and the
    repair-eligibility gate — none of which has ever executed against a populated
    field (it is 0/2504 in production). So the claim is not merely "no test
    broke": it is that the import writes to exactly two tables and no others.
    """
    ts = TermSection.objects.create(
        course_code="CS", course_number="111", course_key="CS111", section="M27"
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="10:30", end_time="11:45", room="A1"
    )
    CourseInstructor.objects.create(
        program="CS",
        course_code="CS111",
        section="M",
        instructor=Instructor.objects.create(full_name="Dr Course", normalised_name="dr course"),
    )
    before = _snapshot_everything()

    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())

    after = _snapshot_everything()
    for name in _MUST_NOT_CHANGE:
        assert after[name] == before[name], f"the import modified {name}"
    # And the specific field whose population would wake the four dormant paths.
    assert set(TermSectionMeeting.objects.values_list("instructor", flat=True)) == {""}


# --------------------------------------------------------------------------
# Gaps the adversarial review proved were unpinned.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken,label",
    [
        ({"dept": "111"}, "department that is not letters"),
        ({"number": "CS"}, "number that is not digits"),
        ({"section": "oops"}, "section label with no digits"),
        ({"serial": "x"}, "serial that is not a number"),
    ],
)
def test_each_shape_check_is_load_bearing(broken, label):
    """One malformed cell at a time.

    The original test swapped dept and number, tripping two checks at once — so
    any single surviving check kept it green.
    """
    result = parse_faculty_sections(_page(_row(**broken)))
    assert result.rows == (), label
    assert result.skipped == 1, label


@pytest.mark.parametrize("section", ["M27", "F3", "YM1", "YF12", "OM2", "OF1"])
def test_two_letter_campus_sections_are_accepted(section):
    """164 of 1148 real global sections use YM/YF/OM/OF.

    A one-letter campus pattern rejected all of them — 14.3% of the estate —
    and counted each as malformed.
    """
    result = parse_faculty_sections(_page(_row(section=section)))
    assert [r.section for r in result.rows] == [section.upper()]
    assert result.skipped == 0


def test_a_row_mangled_by_colspan_is_counted_not_silently_dropped():
    twelve = "<tr>" + "".join(f"<td>{i}</td>" for i in range(12)) + "</tr>"
    result = parse_faculty_sections(_page(_row(), twelve))
    assert len(result.rows) == 1
    assert result.skipped == 1


def test_nested_tables_do_not_multiply_the_counters():
    """``find_all`` is recursive; the live report nests tables four deep.

    Iterating tables and then rows visited every row once per ancestor, inflating
    ``skipped`` and ``duplicates`` fourfold and making both useless to an operator.
    """
    inner = "<table>" + _row() + _row(dept="111") + "</table>"
    nested = ("<html><body><table><tr><td>" + inner + "</td></tr></table></body></html>").encode()
    result = parse_faculty_sections(nested)
    assert len(result.rows) == 1
    assert result.skipped == 1


def test_summary_reports_every_counter():
    page = _page(
        _row(section="M1"),
        _row(section="M1", instructor="Dr Other"),  # a real contradiction
        _row(section="M2", instructor="", serial="2"),
        _row(section="M3", dept="111", serial="3"),  # malformed
    )
    assert summarise(parse_faculty_sections(page)) == {
        "sections": 2,
        "with_instructor": 1,
        "distinct_instructors": 1,
        "distinct_courses": 1,
        "campuses": {"M": 2},
        "skipped_malformed": 1,
        "contradictory_duplicates": 1,
    }


def test_mhtml_is_decoded():
    body = _page(_row(section="M55")).decode()
    mhtml = (
        "From: <Saved by Blink>\r\n"
        "Snapshot-Content-Location: https://eas.taibahu.edu.sa/x\r\n"
        'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + body + "\r\n--B--\r\n"
    ).encode("utf-8")
    assert [r.section for r in parse_faculty_sections(mhtml).rows] == ["M55"]


def test_an_mhtml_naming_an_unknown_codec_still_parses():
    """A browser can save a charset Python has never heard of.  That is not a
    reason to abort an otherwise valid import."""
    body = _page(_row(section="M56")).decode()
    mhtml = (
        "From: <Saved by Blink>\r\n"
        "Snapshot-Content-Location: https://eas.taibahu.edu.sa/x\r\n"
        'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html; charset=not-a-real-codec\r\n\r\n" + body + "\r\n--B--\r\n"
    ).encode("utf-8")
    assert [r.section for r in parse_faculty_sections(mhtml).rows] == ["M56"]


def test_a_cp1256_page_is_decoded():
    page = _page(_row(section="M57")).decode().encode("cp1256")
    assert [r.section for r in parse_faculty_sections(page).rows] == ["M57"]


def test_a_real_capacity_disagreement_is_not_hidden_by_the_merge():
    """The merge exists for the 0-vs-real reprint.  Two genuine figures are a
    contradiction, and max() would have swallowed it."""
    result = parse_faculty_sections(_page(_row(capacity="25"), _row(capacity="40")))
    assert result.duplicates == 1


# --- model constraints the first pass left unpinned -----------------------


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["course_key", "section"])
def test_blank_identity_is_rejected(field):
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    kwargs = {"course_key": "CS111", "section": "M27", field: "   "}
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.bulk_create([SectionInstructor(instructor=a, **kwargs)])


@pytest.mark.django_db
def test_bulk_create_cannot_bypass_normalisation():
    """``save()`` is a convenience; the CHECK is the guarantee.

    The unique indexes compare raw text, so an unnormalised bulk insert would
    have sat beside the canonical row as a second primary.
    """
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    SectionInstructor.objects.create(course_key="CS111", section="M27", instructor=a)
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.bulk_create(
            [SectionInstructor(course_key="cs111", section="m27", instructor=a)]
        )


@pytest.mark.django_db
def test_an_invalid_role_is_rejected_by_the_database():
    """``role`` is matched by literal value in the one-primary partial index, so
    'Primary' would sit outside it and give the section two primaries."""
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.bulk_create(
            [SectionInstructor(course_key="CS111", section="M27", instructor=a, role="Primary")]
        )


@pytest.mark.django_db
def test_role_is_normalised_on_save():
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    link = SectionInstructor.objects.create(
        course_key="CS111", section="M27", instructor=a, role=" CO "
    )
    link.refresh_from_db()
    assert link.role == "co"


@pytest.mark.django_db
def test_one_primary_per_scenario_section():
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    b = Instructor.objects.create(full_name="Dr B", normalised_name="dr b")
    scenario = TimetableScenario.objects.create(name="s1")
    SectionInstructor.objects.create(
        scenario=scenario, course_key="CS111", section="M27", instructor=a
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.create(
            scenario=scenario, course_key="CS111", section="M27", instructor=b
        )


@pytest.mark.django_db
def test_the_same_person_cannot_be_linked_twice_to_one_scenario_section():
    a = Instructor.objects.create(full_name="Dr A", normalised_name="dr a")
    scenario = TimetableScenario.objects.create(name="s1")
    SectionInstructor.objects.create(
        scenario=scenario, course_key="CS111", section="M27", instructor=a
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        SectionInstructor.objects.create(
            scenario=scenario, course_key="CS111", section="M27", instructor=a, role="co"
        )


# --- importer paths that previously raised IntegrityError ------------------


@pytest.mark.django_db
def test_running_again_with_a_different_role_does_not_crash(report):
    """``ux_section_instructor_global`` has no role column, so the same person
    cannot hold two roles on one section.  Probing by role could not see the row
    it was about to collide with, and the whole 337-row import rolled back."""
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    call_command(
        "import_section_instructors", "--file", report, "--role", "co", stdout=io.StringIO()
    )
    link = SectionInstructor.objects.get(section="M27")
    assert link.role == "co"
    assert SectionInstructor.objects.filter(section="M27").count() == 1


@pytest.mark.django_db
def test_promoting_an_existing_co_instructor_does_not_crash(report, tmp_path):
    """Operator records a co-instructor; the registrar later makes them primary."""
    call_command("import_section_instructors", "--file", report, stdout=io.StringIO())
    newcomer = Instructor.objects.create(full_name="Dr New", normalised_name="dr new")
    SectionInstructor.objects.create(
        course_key="CS111", section="M27", instructor=newcomer, role="co"
    )
    promoted = tmp_path / "promoted.html"
    promoted.write_bytes(_page(_row(section="M27", instructor="Dr New")))
    call_command("import_section_instructors", "--file", str(promoted), stdout=io.StringIO())
    rows = SectionInstructor.objects.filter(course_key="CS111", section="M27")
    assert rows.count() == 1
    assert rows.first().instructor == newcomer
    assert rows.first().role == "primary"


@pytest.mark.django_db
def test_the_last_file_wins_and_the_disagreement_is_reported(tmp_path):
    """Silently resolving cross-file conflicts by argument order lost real
    assignments: on two snapshots of the male report, 105 of 305 shared sections
    disagreed and 22 vanished depending on --file order."""
    old = tmp_path / "old.html"
    old.write_bytes(_page(_row(section="M27", instructor="Dr Old")))
    new = tmp_path / "new.html"
    new.write_bytes(_page(_row(section="M27", instructor="Dr New")))
    out = io.StringIO()
    call_command("import_section_instructors", "--file", str(old), "--file", str(new), stdout=out)
    assert SectionInstructor.objects.get(section="M27").instructor.full_name == "Dr New"
    assert "disagree between files" in out.getvalue()
    assert "CS111/M27" in out.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize("blank_first", [True, False])
def test_a_named_instructor_beats_a_blank_whichever_order(tmp_path, blank_first):
    """A report that merely omits a section must not erase an assignment, and a
    blank is not a disagreement — reporting it as one would train operators to
    ignore the warning that matters."""
    blank = tmp_path / "blank.html"
    blank.write_bytes(_page(_row(section="M27", instructor="")))
    named = tmp_path / "named.html"
    named.write_bytes(_page(_row(section="M27", instructor="Dr Real")))
    order = [str(blank), str(named)] if blank_first else [str(named), str(blank)]
    out = io.StringIO()
    call_command("import_section_instructors", "--file", order[0], "--file", order[1], stdout=out)
    assert SectionInstructor.objects.get(section="M27").instructor.full_name == "Dr Real"
    assert "disagree between files" not in out.getvalue()


@pytest.mark.django_db
def test_a_directory_is_a_clean_error_not_a_traceback(tmp_path):
    """A saved page sits beside a "_files" directory; globbing catches it."""
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("import_section_instructors", "--file", str(tmp_path), stdout=io.StringIO())


@pytest.mark.django_db
def test_instructor_names_are_deduped_case_insensitively(tmp_path):
    """The original test used an Arabic name, where casefold() is the identity —
    so it passed even with normalise_instructor replaced by str."""
    page = tmp_path / "case.html"
    page.write_bytes(
        _page(
            _row(section="M1", instructor="  Dr. A. Smith "),
            _row(section="M2", instructor="DR. A. SMITH", serial="2"),
        )
    )
    call_command("import_section_instructors", "--file", str(page), stdout=io.StringIO())
    assert Instructor.objects.count() == 1
    people = {link.instructor_id for link in SectionInstructor.objects.all()}
    assert len(people) == 1


@pytest.mark.django_db
def test_dry_run_preview_matches_what_a_real_run_creates(tmp_path):
    """The preview counted raw names, the write counted normalised ids, so two
    spellings of one person previewed as two creations and produced one."""
    page = tmp_path / "case.html"
    page.write_bytes(
        _page(
            _row(section="M1", instructor="Dr A"),
            _row(section="M2", instructor="dr a", serial="2"),
        )
    )
    preview = io.StringIO()
    call_command("import_section_instructors", "--file", str(page), "--dry-run", stdout=preview)
    written = io.StringIO()
    call_command("import_section_instructors", "--file", str(page), stdout=written)
    assert "to be created                 : 1" in preview.getvalue()
    assert "instructors created : 1" in written.getvalue()
    assert Instructor.objects.count() == 1


def test_a_table_inside_a_cell_does_not_bleed_into_the_shape_check():
    """``find_all`` on a row is recursive, so a nested table's cells would be
    counted as the row's own and push a good row off the 13-cell shape."""
    inner = "<table><tr><td>x</td><td>y</td></tr></table>"
    # Inject into the course-name cell: its text is not shape-validated, so the
    # only thing that can break is the CELL COUNT — which is exactly what a
    # recursive cell search would get wrong (13 cells become 15, row dropped).
    poisoned = _row(course_name=f"Programming{inner}")
    result = parse_faculty_sections(_page(poisoned))
    assert len(result.rows) == 1
    assert result.rows[0].section == "M27"
    assert result.skipped == 0
