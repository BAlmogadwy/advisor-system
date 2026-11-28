"""The timetable feedback report as a workbook: what it holds, over whose cases,
and what it never holds.

Built on the same cases as test_timetable_feedback_report.py, and read back with
openpyxl, so each sheet is checked by its values, not its bytes.
"""

from __future__ import annotations

import io
import json
from datetime import datetime

import pytest
from django.test import Client
from django.utils import timezone
from openpyxl import load_workbook

from core.models import AuditLog, Student, TimetableFeedbackPoint
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_report as report
from core.services import timetable_feedback_report_export as export
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_staff_copy as scopy
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN
from tests.test_timetable_feedback_report import TERM, YEAR, _case, _point
from tests.test_timetable_feedback_staff import (
    ADV,
    S_MESSY,
    S_MINE,
    S_OTHER_ADVISOR,
    make_students,
    staff_user,
)

pytestmark = pytest.mark.django_db


# Fixtures of this module's own: one imported from another module would be
# redefined by its use as a parameter (ruff F811).
@pytest.fixture(autouse=True)
def _feedback_on(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))


@pytest.fixture
def people(db):
    make_students()
    return {
        "advisor": staff_user("export-advisor", ROLE_ADVISOR, advisor_id=ADV),
        "admin": staff_user("export-admin", ROLE_SUPER_ADMIN),
    }


EXPORT = "/ops/timetable-feedback/report/export/"
REPORT = "/ops/timetable-feedback/report/"


def _book(user, language="en", **query):
    client = Client()
    client.force_login(user)
    response = client.get(EXPORT, query, headers={"accept-language": language})
    assert response.status_code == 200, response.status_code
    return response, load_workbook(io.BytesIO(response.content))


def _rows(sheet):
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def _all_text(book):
    return " ".join(
        str(value) for sheet in book.worksheets for row in _rows(sheet) for value in row if value
    )


def test_the_workbook_holds_each_part_of_the_report_and_agrees_with_it(people):
    case = _case(S_MINE, has_registration_at_submission=True)
    _point(case, "PREFER_OTHER_SECTION", course="CS113::Programming", section="M33", asked="M31")
    _point(_case(S_MESSY), "TIME_CLASH", course="CS113::Programming", section="M33")

    response, book = _book(people["admin"])
    t = scopy.strings("en")
    assert book.sheetnames == [
        t["export_sheet_summary"],
        t["export_sheet_issues"],
        t["export_sheet_sections"],
        t["export_sheet_moves"],
        t["export_sheet_missing"],
        t["export_sheet_programmes"],
        t["export_sheet_points"],
    ]
    data = report.build(staff_service.staff_of(people["admin"]), {})
    summary = {row[0]: row[1] for row in _rows(book[t["export_sheet_summary"]]) if row and row[0]}
    assert summary[t["kpi_students"]] == data.totals.students == 2
    assert summary[t["report_col_points"]] == data.totals.points == 2
    assert summary[t["report_term"]] == data.filters.term
    (section,) = _rows(book[t["export_sheet_sections"]])[1:]
    assert section[:4] == ["CS113 — Programming", "M33", 2, 2]
    assert _rows(book[t["export_sheet_moves"]])[1:] == [["CS113 — Programming", "M33", "M31", 1]]
    points = _rows(book[t["export_sheet_points"]])
    assert len(points) == 1 + data.totals.points
    assert case.reference in {row[0] for row in points[1:]}
    # The student is their university id, a number to sort by.
    assert S_MINE in {row[3] for row in points[1:]}
    assert response["Content-Disposition"] == (
        f'attachment; filename="timetable-feedback-report-1448-1-'
        f'{timezone.localtime():%Y%m%d}.xlsx"'
    )


def test_an_advisors_workbook_holds_only_their_cases(people):
    mine = _case(S_MINE)
    _point(mine, "TIME_CLASH", course="ZZ100::Mine Only", section="Q1")
    theirs = _case(S_OTHER_ADVISOR)
    _point(theirs, "TIME_CLASH", course="ZZ900::Not Theirs", section="Q9")
    _response, book = _book(people["advisor"])
    text = _all_text(book)
    assert mine.reference in text and "ZZ100" in text
    assert theirs.reference not in text and "ZZ900" not in text


def test_no_name_and_no_free_text_leaves_in_the_workbook(people):
    Student.objects.filter(student_id=S_MINE).update(name="Very Private Name")
    case = _case(S_MINE)
    _point(case, "OTHER", section="M1")
    TimetableFeedbackPoint.objects.filter(case=case).update(note="A private note")
    _response, book = _book(people["admin"])
    text = _all_text(book)
    assert str(S_MINE) in text
    assert "Very Private Name" not in text and "A private note" not in text


def test_a_value_starting_with_equals_stays_text_never_a_formula(people):
    _point(_case(S_MINE), "TIME_CLASH", course='=HYPERLINK("http://x")::Name', section="=1+1")
    _response, book = _book(people["admin"])
    sheet = book[scopy.strings("en")["export_sheet_points"]]
    cells = [cell for row in sheet.iter_rows(min_row=2) for cell in row]
    labels = [cell for cell in cells if cell.value == "=1+1"]
    assert labels and all(cell.data_type == "s" for cell in labels)
    assert all(cell.data_type != "f" for cell in cells)


def test_an_arabic_workbook_reads_right_to_left_in_arabic(people):
    _point(_case(S_MINE))
    _response, book = _book(people["admin"], "ar")
    t = scopy.strings("ar")
    assert book.sheetnames[0] == t["export_sheet_summary"]
    assert all(sheet.sheet_view.rightToLeft for sheet in book.worksheets)
    _response, english = _book(people["admin"], "en")
    assert not any(sheet.sheet_view.rightToLeft for sheet in english.worksheets)


def test_what_an_xml_part_cannot_hold_is_dropped_not_written(people):
    _point(_case(S_MINE), "TIME_CLASH", course="CS113::Bad" + chr(0xFFFF) + "Name", section="M1")
    _response, book = _book(people["admin"])
    assert "CS113 — BadName" in _all_text(book)
    assert export._clean("a" + chr(0xFFFE) + "b" + chr(0x0B) + "c") == "abc"


def test_the_workbook_follows_the_pages_narrowing(people):
    _point(_case(S_MINE, term="1"), "TIME_CLASH")
    _point(_case(S_MESSY, term="2"), "LOAD_TOO_HIGH")
    client = Client()
    client.force_login(people["admin"])
    page = client.get(REPORT, {"term": "1448/2"}).content.decode()
    link = page.split('id="ttfrExport" href="')[1].split('"')[0].replace("&amp;", "&")
    assert link.startswith(EXPORT) and "term=1448%2F2" in link
    book = load_workbook(io.BytesIO(client.get(link).content))
    points = _rows(book[scopy.strings("en")["export_sheet_points"]])[1:]
    assert [row[1] for row in points] == ["1448/2"]
    # Every term stays every term in the file too.
    every = client.get(REPORT, {"term": report.EVERY_TERM}).content.decode()
    assert f"term={report.EVERY_TERM}" in every.split('id="ttfrExport" href="')[1].split('"')[0]


def test_every_download_is_audited_with_its_narrowing(people):
    _point(_case(S_MINE))
    _book(people["admin"], reason="TIME_CLASH")
    entry = AuditLog.objects.filter(action="timetable_feedback.report_export").latest("pk")
    assert entry.status == "success"
    assert entry.actor_username == people["admin"].username
    details = json.loads(entry.details_json)
    assert (details["reason"], details["cases"]) == ("TIME_CLASH", 1)


def test_the_file_is_named_after_its_term_and_day():
    filters = report.Filters(term="1448/1", programme="", reason="", window="")
    day = timezone.make_aware(datetime(2026, 11, 28, 9, 0))
    assert export.filename(filters, day) == "timetable-feedback-report-1448-1-20261128.xlsx"
    every = report.Filters(term="", programme="", reason="", window="")
    assert export.filename(every, day) == "timetable-feedback-report-all-terms-20261128.xlsx"
