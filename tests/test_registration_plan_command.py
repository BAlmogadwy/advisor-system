"""The command's own guards — the ones that are not in the plan.

`tests/test_registration_plan_import.py` proves the plan is right. Two refusals
live in the command instead, because they are about the FILE and the OPERATOR
rather than about the data:

  * a workbook whose columns have moved must fail, not be read through positional
    coincidence;
  * an apply that leaves students with an incomplete week must leave a record of
    which students, because nothing in the database distinguishes "no section
    exists for this course" from "not registered".
"""

from __future__ import annotations

import json
from io import StringIO

import openpyxl
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import Student, StudentTermSection, TermSection, TermSectionMeeting

pytestmark = pytest.mark.django_db

ROSTER_HEADER = ("course", "section", "lectures", "lab period", "cap", "students", "student_ids")
DETAIL_HEADER = (
    "student_id",
    "program",
    "course",
    "kind",
    "section",
    "lectures",
    "lab period",
    "room(s)",
    "instructor",
)


def _workbook(tmp_path, *, roster_header=ROSTER_HEADER, detail_rows=(), roster_rows=()):
    book = openpyxl.Workbook()
    rosters = book.active
    rosters.title = "Section Rosters"
    rosters.append(list(roster_header))
    for row in roster_rows:
        rosters.append(list(row))
    detail = book.create_sheet("Student Courses (detail)")
    detail.append(list(DETAIL_HEADER))
    for row in detail_rows:
        detail.append(list(row))
    path = tmp_path / "plan.xlsx"
    book.save(path)
    return path


@pytest.fixture
def world():
    Student.objects.update_or_create(
        student_id=800001, defaults={"name": "A", "program": "AI", "section": "M"}
    )
    section = TermSection.objects.create(
        course_code="AI",
        course_number="331",
        course_key="AI331",
        course_name="AI331",
        section="M1",
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="MON", start_time="09:00", end_time="10:15"
    )
    return section


def test_a_reordered_sheet_fails_instead_of_being_read_positionally(tmp_path, world):
    """Every column this importer reads is positional. A workbook whose author
    swapped two columns would otherwise seat students from whichever column landed
    in slot 4 — and the file is an approved fixed-format artefact, so its headers
    are part of the contract."""
    swapped = ("section", "course", "lectures", "lab period", "cap", "students", "student_ids")
    path = _workbook(tmp_path, roster_header=swapped)
    with pytest.raises(CommandError, match="UNEXPECTED_HEADER"):
        call_command("import_registration_plan", str(path), "--year", "1448", "--term", "1")


def test_the_expected_headers_pass(tmp_path, world):
    path = _workbook(
        tmp_path,
        roster_rows=[("AI331", "AI:S1", "Mon 09:00-10:15", "", "-", 1, "")],
        detail_rows=[(800001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")],
    )
    out = StringIO()
    call_command("import_registration_plan", str(path), "--year", "1448", "--term", "1", stdout=out)
    assert "1 section links for 1 students" in out.getvalue()


def test_applying_with_uncovered_rows_and_no_report_writes_nothing(tmp_path, world):
    """A warning scrolls away, which is the exact failure the report was added to
    close. If the import leaves students with an incomplete week, the record of
    WHICH students is a precondition of writing.

    No default path is chosen: the file carries student identifiers, so the
    operator names somewhere restricted rather than having one picked for them."""
    path = _workbook(
        tmp_path,
        roster_rows=[("AI331", "AI:S1", "Mon 09:00-10:15", "", "-", 1, "")],
        detail_rows=[
            (800001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
            (800001, "AI", "GSE1", "Online elective", "online", "Sun 15:50-17:30", "", "", ""),
        ],
    )
    with pytest.raises(CommandError, match="--report is required"):
        call_command(
            "import_registration_plan",
            str(path),
            "--year",
            "1448",
            "--term",
            "1",
            "--apply",
        )
    assert StudentTermSection.objects.count() == 0, "the refusal still wrote rows"


def test_the_report_records_the_gap_the_database_cannot(tmp_path, world):
    """The gap is the difference between "nothing scheduled" and "we do not hold
    it", and no column anywhere carries it."""
    report = tmp_path / "plan.json"
    path = _workbook(
        tmp_path,
        roster_rows=[("AI331", "AI:S1", "Mon 09:00-10:15", "", "-", 1, "")],
        detail_rows=[
            (800001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
            (800001, "AI", "GSE1", "Online elective", "online", "Sun 15:50-17:30", "", "", ""),
        ],
    )
    call_command(
        "import_registration_plan",
        str(path),
        "--year",
        "1448",
        "--term",
        "1",
        "--apply",
        "--report",
        str(report),
    )
    assert StudentTermSection.objects.count() == 1

    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["sha256"], "the workbook was not fingerprinted"
    assert saved["uncovered"]["GSE1"][0]["student_id"] == 800001
    assert saved["counts"]["links"] == 1
    assert saved["counts"]["replaces"] == 0


def test_an_apply_with_no_gaps_needs_no_report(tmp_path, world):
    """The requirement is scoped to the failure it exists for. A clean import is
    fully described by the rows it wrote."""
    path = _workbook(
        tmp_path,
        roster_rows=[("AI331", "AI:S1", "Mon 09:00-10:15", "", "-", 1, "")],
        detail_rows=[(800001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")],
    )
    call_command("import_registration_plan", str(path), "--year", "1448", "--term", "1", "--apply")
    assert StudentTermSection.objects.count() == 1
