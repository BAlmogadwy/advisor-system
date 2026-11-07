"""
core/services/section_roster_export.py
Section Planning's second export: who is expected to register each course.

The plan export (``Sections`` + ``Summary``) answers *how many sections* a
course needs. This one answers *which students* that demand is made of: a row
per student per course, named by student id, so the department can hand the
registrar the roster behind every section count.

It is a roster, NOT a section assignment: the plan says a course needs three
sections, and this file says who the course's students are — it never decides
which of the three a student sits in. That decision is the registrar's, taken
at registration, and inventing a split here would read as real.

The numbers tie back to the plan: a course's ``Students`` is the seat demand
that sized its sections, and ``Listed`` is the number of distinct students the
roster names. They differ only where one student's recommendations asked for
the same course twice (a direct recommendation and an elective slot resolving
to it): the plan counts both seats, the roster names the student once.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from core.models import Student
from core.services.student_helpers import normalize_code

__all__ = [
    "COURSE_HEADERS",
    "ROSTER_HEADERS",
    "build_roster_rows",
    "export_course_rosters_xlsx",
]

#: The Rosters sheet's columns, in order.
ROSTER_HEADERS = [
    "#",
    "Department",
    "Course",
    "Course Name",
    "Credits",
    "Sections",
    "Max/Section",
    "Student ID",
    "Student Name",
    "Programme",
]

#: The Courses sheet's columns, in order.
COURSE_HEADERS = [
    "#",
    "Department",
    "Course",
    "Course Name",
    "Credits",
    "External",
    "Students",
    "Listed",
    "Max/Section",
    "Sections",
    "Student IDs",
]


def _student_directory(student_ids: set[int]) -> dict[int, tuple[str, str]]:
    """(name, programme) for each student the roster names, read once."""
    if not student_ids:
        return {}
    rows = Student.objects.filter(student_id__in=sorted(student_ids)).values_list(
        "student_id", "name", "program"
    )
    return {int(sid): (str(name or ""), str(program or "")) for sid, name, program in rows}


def build_roster_rows(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (course, student), in the plan's own course order.

    ``plan`` rows are the sized plan rows carrying ``student_ids``
    (``size_demand(..., with_rosters=True)``). A row with no ``student_ids``
    key contributes nothing: a plan sized without rosters has no roster, and
    is never silently filled in with an empty one.
    """
    wanted: set[int] = set()
    for row in plan:
        wanted.update(int(sid) for sid in row.get("student_ids") or ())
    directory = _student_directory(wanted)

    rows: list[dict[str, Any]] = []
    for course in plan:
        for student_id in course.get("student_ids") or ():
            sid = int(student_id)
            name, programme = directory.get(sid, ("", ""))
            rows.append(
                {
                    "department": str(course.get("department") or ""),
                    "course_code": normalize_code(course.get("course_code")),
                    "course_name": str(course.get("course_name") or ""),
                    "credit_hours": course.get("credit_hours") or 0,
                    "num_sections": int(course.get("num_sections") or 0),
                    "max_per_section": int(course.get("max_per_section") or 0),
                    "student_id": sid,
                    "student_name": name,
                    "programme": programme,
                }
            )
    return rows


def _dept_fill(dept: str, cache: dict[str, Any]) -> Any:
    """The department's pastel fill — the plan export's colours, same hash."""
    import colorsys

    from openpyxl.styles import PatternFill  # type: ignore[import-untyped]

    if dept in cache:
        return cache[dept]
    h = 0
    for ch in str(dept):
        h = (h * 31 + ord(ch)) % 360
    r, g, b = colorsys.hls_to_rgb(h / 360.0, 0.94, 0.50)
    fill = PatternFill("solid", fgColor=f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}")
    cache[dept] = fill
    return fill


def export_course_rosters_xlsx(
    plan: list[dict[str, Any]],
    *,
    section_label: str,
    year: object = "",
    semester: object = "",
    dept_filter: list[str] | None = None,
    no_section: int = 0,
    course_names: dict[str, str] | None = None,
) -> Path:
    """Write the two-sheet roster workbook and return its temp path.

    ``Rosters`` is the data (a row per student per course, filterable);
    ``Courses`` is the index that ties each course's roster back to the seat
    demand that sized its sections.
    """
    from openpyxl import Workbook  # type: ignore[import-untyped]
    from openpyxl.styles import (  # type: ignore[import-untyped]
        Alignment,
        Border,
        Font,
        PatternFill,
        Side,
    )
    from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

    catalogue = course_names or {}
    rows = build_roster_rows(plan)

    thin = Side(style="thin", color="D5D8DC")
    cell_border = Border(top=thin, bottom=thin, left=thin, right=thin)
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    title_fill = PatternFill(start_color="1B2631", end_color="1B2631", fill_type="solid")
    title_font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_center = Alignment(horizontal="left", vertical="center")
    alt_fill = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")
    ext_fill = PatternFill(start_color="EBF5FB", end_color="EBF5FB", fill_type="solid")
    mono = Font(name="Consolas", bold=True, size=10)
    num_font = Font(name="Consolas", size=10)
    dept_font = Font(name="Calibri", bold=True, size=10, color="2E4053")
    name_font = Font(size=9, color="566573")
    note_font = Font(italic=True, size=9, color="566573")
    dept_colors: dict[str, Any] = {}

    def _as_text(cell: Any) -> Any:
        """Pin a string cell to text, so a name is never read as a formula.

        Student and course names come from the registrar's data. openpyxl
        writes a string that begins with ``=`` as a FORMULA, so a name such as
        ``=HYPERLINK(...)`` would run when the department opens the file.
        Every string this workbook writes is data, never a formula, and says
        so.
        """
        if isinstance(cell.value, str):
            cell.data_type = "s"
        return cell

    def _title(ws: Any, text: str, width: int) -> None:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
        cell = _as_text(ws.cell(row=1, column=1, value=text))
        cell.font = title_font
        cell.fill = title_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        for col in range(2, width + 1):
            ws.cell(row=1, column=col).fill = title_fill

    def _headers(ws: Any, headers: list[str], row: int) -> None:
        for col, text in enumerate(headers, 1):
            cell = _as_text(ws.cell(row=row, column=col, value=text))
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = cell_border

    def _cell(
        ws: Any, r: int, c: int, value: object, *, font: Any = None, align: Any = center
    ) -> Any:
        cell = _as_text(ws.cell(row=r, column=c, value=value))
        cell.alignment = align
        cell.border = cell_border
        if font is not None:
            cell.font = font
        return cell

    scope = f" — {year}/{semester}" if year != "" or semester != "" else ""
    filtered = f" (filtered: {', '.join(dept_filter)})" if dept_filter else ""

    wb = Workbook()

    # ── Rosters: the data, a row per student per course ──
    ws = wb.active
    ws.title = "Rosters"
    _title(
        ws,
        f"Course Rosters{scope} — Section: {section_label}{filtered}"
        " — the students expected to register each course (not a section assignment)",
        len(ROSTER_HEADERS),
    )
    _headers(ws, ROSTER_HEADERS, 2)
    for i, row in enumerate(rows, 1):
        r = i + 2
        _cell(ws, r, 1, i, font=num_font)
        cell = _cell(ws, r, 2, row["department"], font=dept_font, align=left_center)
        cell.fill = _dept_fill(row["department"], dept_colors)
        _cell(ws, r, 3, row["course_code"], font=mono, align=left_center)
        _cell(
            ws,
            r,
            4,
            row["course_name"] or catalogue.get(row["course_code"], ""),
            font=name_font,
            align=left_center,
        )
        _cell(ws, r, 5, row["credit_hours"], font=num_font)
        _cell(ws, r, 6, row["num_sections"], font=num_font)
        _cell(ws, r, 7, row["max_per_section"], font=num_font)
        # The student id as a number, so Excel sorts and filters it as one.
        _cell(ws, r, 8, row["student_id"], font=Font(name="Consolas", bold=True, size=10))
        _cell(ws, r, 9, row["student_name"], align=left_center)
        _cell(ws, r, 10, row["programme"], font=mono)
        if i % 2 == 0:
            for col in range(1, len(ROSTER_HEADERS) + 1):
                if col != 2:  # keep the department's own colour
                    ws.cell(row=r, column=col).fill = alt_fill
    for col, width in enumerate([5, 12, 14, 30, 8, 9, 11, 13, 32, 11], 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    if rows:
        ws.auto_filter.ref = f"A2:{get_column_letter(len(ROSTER_HEADERS))}{len(rows) + 2}"
    ws.freeze_panes = "A3"

    # ── Courses: the index, each roster against the demand that sized it ──
    ws2 = wb.create_sheet("Courses")
    _title(ws2, f"Courses{scope} — Section: {section_label}{filtered}", len(COURSE_HEADERS))
    note = (
        "Students = the seat demand that sized the sections; Listed = the distinct students"
        " named in the Rosters sheet. They differ only where one student's recommendations"
        " asked for the same course twice."
    )
    if no_section:
        plural = "" if no_section == 1 else "s"
        verb = "has" if no_section == 1 else "have"
        is_are = "is" if no_section == 1 else "are"
        note += (
            f" {no_section} student{plural} {verb} no recorded section"
            f" and {is_are} not in this plan."
        )
    _as_text(ws2.cell(row=2, column=1, value=note)).font = note_font
    _headers(ws2, COURSE_HEADERS, 3)
    for i, course in enumerate(plan, 1):
        r = i + 3
        dept = str(course.get("department") or "")
        code = normalize_code(course.get("course_code"))
        ids = [int(sid) for sid in course.get("student_ids") or ()]
        is_ext = bool(course.get("is_external"))
        _cell(ws2, r, 1, i, font=num_font)
        cell = _cell(ws2, r, 2, dept, font=dept_font, align=left_center)
        cell.fill = _dept_fill(dept, dept_colors)
        _cell(ws2, r, 3, code, font=mono, align=left_center)
        _cell(
            ws2,
            r,
            4,
            str(course.get("course_name") or "") or catalogue.get(code, ""),
            font=name_font,
            align=left_center,
        )
        _cell(ws2, r, 5, course.get("credit_hours") or 0, font=num_font)
        cell = _cell(ws2, r, 6, "Yes" if is_ext else "")
        if is_ext:
            cell.font = Font(color="2980B9", bold=True)
        _cell(ws2, r, 7, int(course.get("total_students") or 0), font=Font(bold=True, size=10))
        _cell(ws2, r, 8, len(ids), font=num_font)
        _cell(ws2, r, 9, int(course.get("max_per_section") or 0), font=num_font)
        _cell(ws2, r, 10, int(course.get("num_sections") or 0), font=Font(bold=True, size=10))
        _cell(ws2, r, 11, ", ".join(str(sid) for sid in ids), font=num_font, align=left_center)
        row_fill = ext_fill if is_ext else (alt_fill if i % 2 == 0 else None)
        if row_fill:
            for col in range(1, len(COURSE_HEADERS) + 1):
                if col != 2:
                    ws2.cell(row=r, column=col).fill = row_fill
    for col, width in enumerate([5, 12, 14, 30, 8, 9, 10, 9, 11, 10, 60], 1):
        ws2.column_dimensions[get_column_letter(col)].width = width
    ws2.freeze_panes = "A4"

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
