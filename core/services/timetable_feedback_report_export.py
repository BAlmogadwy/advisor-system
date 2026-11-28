"""The timetable feedback report as a workbook, for the department to keep and sort.

Each part of the report page becomes a sheet, and one more sheet holds a row per
point for anyone who wants to pivot it themselves. Everything is built from the
same `timetable_feedback_report.Report` the page draws, and the point rows from
the same narrowed points, so the file and the page never disagree.

WHAT LEAVES. A file travels further than a page, so it carries no student name
and no free text: a student is their university id, and what they wrote stays
on the case page, behind the same scope as everything else.

WRITING. Production has no lxml, and openpyxl's own writer escapes nothing an XML
part cannot hold, so every string is cleaned with the exam export's pattern. A
string that begins with "=" would be written as a formula, so every string cell
is pinned to text. An Arabic workbook reads right to left.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from django.utils import timezone

from core.models import TimetableFeedbackPoint
from core.services import timetable_feedback_report as report
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_staff_copy as scopy
from core.services.exam_student_export import XML_ILLEGAL_RE

#: openpyxl refuses a sheet name over 31 characters.
_SHEET_NAME_MAX = 31


def _clean(value: object) -> object:
    """A cell's value as the workbook may hold it."""
    if isinstance(value, str):
        return XML_ILLEGAL_RE.sub("", value)
    return value


def _course(code: str, name: str) -> str:
    return f"{code} — {name}" if name else code


def filename(filters: report.Filters, now: datetime) -> str:
    """timetable-feedback-report-1448-1-20261128.xlsx (or all-terms)."""
    # The term comes from stored data: only letters, digits, dots and hyphens may
    # reach the quoted file name in the response header.
    term = re.sub(r"[^A-Za-z0-9.-]", "-", filters.term) if filters.term else "all-terms"
    return f"timetable-feedback-report-{term}-{timezone.localtime(now):%Y%m%d}.xlsx"


def point_rows(points: Any, language: str) -> tuple[list[str], list[list[object]]]:
    """(headers, rows): one row per live point, student by id, no free text."""
    t = scopy.strings(language)
    headers = [
        t["col_reference"],
        t["case_term"],
        t["report_programme"],
        t["export_col_student_id"],
        t["col_status"],
        t["filter_reason"],
        t["report_col_course"],
        t["report_col_section"],
        t["point_requested_section"],
        t["point_requested_course"],
        t["case_registered"],
        t["case_sent"],
    ]
    rows: list[list[object]] = []
    # Streamed, a term's points need not all sit in memory as model rows; "pk"
    # last, so two points sent at the same moment keep one order.
    for point in (
        points.select_related("case")
        .order_by("case__reference", "created_at", "pk")
        .iterator(chunk_size=2000)
    ):
        case = point.case
        code, name = report.split_identity(point.section_course_identity)
        on_section = point.anchor_kind == TimetableFeedbackPoint.AnchorKind.SECTION
        rows.append(
            [
                case.reference,
                report.term_key(case.academic_year, case.term),
                str(case.program_at_submission or "").strip().upper(),
                case.student_id,
                scopy.status_label(case.status, language),
                scopy.reason_label(point.reason_code, language),
                _course(code, name) if on_section else "",
                point.section_label,
                point.requested_section_label,
                point.requested_plan_course_identity or point.requested_course_key,
                t["filter_yes"] if case.has_registration_at_submission else t["filter_no"],
                timezone.localtime(case.created_at).strftime("%Y-%m-%d"),
            ]
        )
    return headers, rows


def _summary(data: report.Report, language: str, now: datetime) -> list[list[object]]:
    t = scopy.strings(language)
    filters, totals, response = data.filters, data.totals, data.response
    window = t[f"report_window_{filters.window}"] if filters.window else t["report_window_all"]
    median = round(response.median_hours, 1) if response.median_hours is not None else ""
    return [
        [t["report_term"], filters.term or t["report_every_term"]],
        [t["report_programme"], filters.programme or t["filter_any"]],
        [
            t["report_issue"],
            scopy.reason_label(filters.reason, language) if filters.reason else t["filter_any"],
        ],
        [t["report_window"], window],
        [t["export_generated"], timezone.localtime(now).strftime("%Y-%m-%d %H:%M")],
        [],
        [t["kpi_students"], totals.students],
        [t["report_col_cases"], totals.live_cases],
        [t["report_col_points"], totals.points],
        [t["kpi_open"], totals.open],
        [t["report_col_waiting"], totals.waiting_on_staff],
        [t["tab_student"], totals.waiting_on_student],
        [t["kpi_with_department"], totals.with_department],
        [t["kpi_untouched"], totals.untouched],
        [t["export_median_hours"], median],
        [t["export_prompt"], response.prompt],
        [t["export_acted_on"], response.acted_on],
        [t["export_helpful_yes"], response.helpful_yes],
        [t["export_helpful_no"], response.helpful_no],
        [t["export_withdrawn"], totals.withdrawn],
    ]


def sheets(
    data: report.Report, points: Any, language: str, now: datetime
) -> list[tuple[str, list[str], list[list[object]]]]:
    """(sheet name, headers, rows) for every sheet, in order."""
    t = scopy.strings(language)
    reason = lambda code: scopy.reason_label(code, language)  # noqa: E731
    sheet_list: list[tuple[str, list[str], list[list[object]]]] = [
        (
            t["export_sheet_summary"],
            [t["export_col_item"], t["export_col_value"]],
            _summary(data, language, now),
        ),
        (
            t["export_sheet_issues"],
            [t["filter_reason"], t["report_col_students"], t["report_col_points"]],
            [[reason(row.code), row.students, row.points] for row in data.reasons],
        ),
        (
            t["export_sheet_sections"],
            [
                t["report_col_course"],
                t["report_col_section"],
                t["report_col_students"],
                t["report_col_points"],
                t["report_col_issues"],
                t["report_col_asked_for"],
                t["report_col_registered"],
            ],
            [
                [
                    _course(row.course_code, row.course_name),
                    row.section,
                    row.students,
                    row.points,
                    t["list_separator"].join(f"{reason(code)} ({n})" for code, n in row.reasons),
                    t["list_separator"].join(f"{label} ({n})" for label, n in row.moves),
                    row.registered_students,
                ]
                for row in data.sections
            ],
        ),
        (
            t["export_sheet_moves"],
            [
                t["report_col_course"],
                t["report_col_from"],
                t["report_col_to"],
                t["report_col_students"],
            ],
            [
                [
                    _course(row.course_code, row.course_name),
                    row.section_from,
                    row.section_to,
                    row.students,
                ]
                for row in data.moves
            ],
        ),
        (
            t["export_sheet_missing"],
            [t["report_col_course"], t["report_col_programmes"], t["report_col_students"]],
            [
                [
                    _course(row.course_code, row.course_name),
                    t["list_separator"].join(row.programmes),
                    row.students,
                ]
                for row in data.missing
            ],
        ),
        (
            t["export_sheet_programmes"],
            [
                t["report_col_programme"],
                t["report_col_students"],
                t["report_col_cases"],
                t["report_col_open"],
                t["report_col_waiting"],
                t["report_col_answered"],
                t["report_col_top_issue"],
            ],
            [
                [
                    row.programme,
                    row.students,
                    row.cases,
                    row.open,
                    row.waiting_on_staff,
                    row.answered,
                    reason(row.top_reason) if row.top_reason else "",
                ]
                for row in data.by_programme
            ],
        ),
    ]
    headers, rows = point_rows(points, language)
    sheet_list.append((t["export_sheet_points"], headers, rows))
    return sheet_list


def build_workbook(
    staff: staff_service.Staff,
    query: Mapping[str, Any],
    *,
    language: str,
    now: datetime | None = None,
) -> tuple[bytes, str, report.Report]:
    """The workbook's bytes, its file name, and the report it was built from."""
    from openpyxl import Workbook  # type: ignore[import-untyped]
    from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
    from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

    now = now or timezone.now()
    # Every row, not the page's top ones: a file is kept as the record.
    data = report.build(staff, query, now=now, full=True)
    _cases, _live, points = report.narrowed(staff, data.filters, now)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")

    book = Workbook()
    book.remove(book.active)
    for name, headers, rows in sheets(data, points, language, now):
        sheet = book.create_sheet(title=str(_clean(name))[:_SHEET_NAME_MAX])
        sheet.sheet_view.rightToLeft = language == "ar"
        widths = [len(str(text)) for text in headers]
        for row_number, values in enumerate([headers, *rows], start=1):
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=row_number, column=column, value=_clean(value))
                if isinstance(cell.value, str):
                    # Data, never a formula: a value starting "=" stays text.
                    cell.data_type = "s"
                if row_number == 1:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(vertical="center", wrap_text=True)
                if column <= len(widths):
                    widths[column - 1] = max(widths[column - 1], len(str(value)))
        for column, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(column)].width = min(max(width + 2, 10), 60)
        sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue(), filename(data.filters, now), data


def column_letters(count: int) -> Sequence[str]:
    """The letters of the first ``count`` columns, for tests that read a sheet."""
    from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

    return [get_column_letter(n) for n in range(1, count + 1)]
