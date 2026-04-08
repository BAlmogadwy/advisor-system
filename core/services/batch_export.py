"""
core/services/batch_export.py
XLSX export for the Batch Recommender (aggregate course demand).

Creates a styled workbook with:
  - Demand sheet: title with program/section, ranked courses with demand count,
    and per-course prerequisite studying breakdown
  - Summary sheet: filters, total students, top courses
"""

from __future__ import annotations

import tempfile
from collections import Counter, defaultdict
from pathlib import Path


def export_batch_recommender_xlsx(
    year: int,
    semester: int,
    program: str | None,
    section: str | None,
    student_count: int,
    aggregate: dict[str, int],
) -> Path:
    """Export batch recommender results as styled XLSX."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError as exc:
        raise RuntimeError("openpyxl is required") from exc

    from django.db.models import Count as DjCount

    from core.models import Course, Prerequisite, StudentCourse
    from core.services.student_helpers import normalize_code

    # Styles
    hdr_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    hdr_font = Font(name="Consolas", size=9, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center")
    title_fill = PatternFill(start_color="111144", end_color="111144", fill_type="solid")
    title_font = Font(name="Consolas", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    thick_side = Side(style="medium", color="333333")
    bold_font = Font(bold=True, size=9)
    normal_font = Font(name="Consolas", size=9)
    center_align = Alignment(horizontal="center", vertical="center")
    wrap_align = Alignment(horizontal="left", vertical="top", wrap_text=True)
    studying_font = Font(size=8, color="C03030")
    studying_fill = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")

    # ── Pre-load prerequisite + studying data ────────────────────
    # For each recommended course, find its prerequisites and count
    # how many students are currently studying each prerequisite.

    sorted_courses = sorted(aggregate.items(), key=lambda x: -x[1])
    course_codes = [code for code, _ in sorted_courses]

    # Load course names
    course_names: dict[str, str] = {}
    for c in Course.objects.filter(course_code__in=course_codes):
        course_names[c.course_code.upper()] = c.description or c.course_code

    # Load prerequisites for all recommended courses
    # Determine which programs to check
    programs_to_check = []
    if program:
        programs_to_check = [p.strip() for p in program.split(",") if p.strip()]

    prereq_map: dict[str, list[str]] = defaultdict(list)  # course -> [prereq_codes]
    if programs_to_check:
        for prog in programs_to_check:
            for cc, prereq_cc in Prerequisite.objects.filter(
                program=prog, course_code__in=course_codes
            ).values_list("course_code", "prerequisite_course_code"):
                cc_n = normalize_code(cc)
                for part in str(prereq_cc).split(","):
                    p = normalize_code(part)
                    if p and p not in prereq_map[cc_n]:
                        prereq_map[cc_n].append(p)

    # Count students currently studying each prerequisite
    all_prereq_codes = set()
    for prereqs in prereq_map.values():
        all_prereq_codes.update(prereqs)

    prereq_studying_count: dict[str, int] = {}
    if all_prereq_codes:
        studying_qs = (
            StudentCourse.objects.filter(
                course__course_code__in=all_prereq_codes,
                status="studying",
            )
            .values("course__course_code")
            .annotate(cnt=DjCount("id"))
        )
        for row in studying_qs:
            prereq_studying_count[normalize_code(row["course__course_code"])] = row["cnt"]

    wb = Workbook()
    wb.remove(wb.active)

    # ── Demand Sheet ─────────────────────────────────────────────
    ws = wb.create_sheet(title="Course Demand")

    # Title row with program and section
    prog_label = program or "All Programs"
    sec_label = f"Section {section}" if section else "All Sections"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
    title_cell = ws.cell(row=1, column=1)
    title_cell.value = f"Batch Recommender — {prog_label} | {sec_label} | {year}/T{semester} | {student_count} students"
    title_cell.fill = title_fill
    title_cell.font = title_font
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(1, 5):
        ws.cell(row=1, column=c).fill = title_fill

    # Headers
    headers = ["#", "Course", "Name", "Students Needing", "Prerequisite Status"]
    row = 2
    for col, hdr in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border

    row = 3
    for rank, (code, count) in enumerate(sorted_courses, start=1):
        # Rank
        ws.cell(row=row, column=1, value=rank).font = Font(size=9, color="999999")
        ws.cell(row=row, column=1).border = thin_border
        ws.cell(row=row, column=1).alignment = center_align

        # Course code
        ws.cell(row=row, column=2, value=code).font = bold_font
        ws.cell(row=row, column=2).border = thin_border

        # Course name
        name = course_names.get(code.upper(), "")
        ws.cell(row=row, column=3, value=name).font = Font(size=8, color="666666")
        ws.cell(row=row, column=3).border = thin_border

        # Count
        cell = ws.cell(row=row, column=4, value=count)
        cell.font = Font(name="Consolas", size=10, bold=True)
        cell.border = thin_border
        cell.alignment = center_align
        # Color by demand level
        pct = round(count / max(student_count, 1) * 100)
        if pct >= 50:
            cell.fill = PatternFill(start_color="A9DFBF", end_color="A9DFBF", fill_type="solid")
        elif pct >= 25:
            cell.fill = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")

        # Prerequisite studying breakdown
        prereqs = prereq_map.get(normalize_code(code), [])
        if prereqs:
            lines = []
            has_studying = False
            for pr in prereqs:
                studying_cnt = prereq_studying_count.get(pr, 0)
                if studying_cnt > 0:
                    lines.append(f"* {studying_cnt} students studying {pr}")
                    has_studying = True
                else:
                    lines.append(f"  {pr}: all passed")

            cell = ws.cell(row=row, column=5, value="\n".join(lines))
            cell.border = thin_border
            cell.alignment = wrap_align
            if has_studying:
                cell.font = studying_font
                cell.fill = studying_fill
            else:
                cell.font = Font(size=8, color="0A8E6E")
                cell.fill = PatternFill(start_color="E8F5F0", end_color="E8F5F0", fill_type="solid")
        else:
            cell = ws.cell(row=row, column=5, value="No prerequisites")
            cell.font = Font(size=8, color="999999")
            cell.border = thin_border

        row += 1

    # Totals
    total_demand = sum(aggregate.values())
    ws.cell(row=row, column=1).border = thin_border
    ws.cell(row=row, column=2, value="TOTAL").font = Font(bold=True, size=9)
    ws.cell(row=row, column=2).border = thin_border
    ws.cell(row=row, column=3, value=f"{len(sorted_courses)} courses").font = Font(size=8, color="666666")
    ws.cell(row=row, column=3).border = thin_border
    ws.cell(row=row, column=4, value=total_demand).font = Font(bold=True, size=9)
    ws.cell(row=row, column=4).border = thin_border
    ws.cell(row=row, column=4).alignment = center_align
    ws.cell(row=row, column=5).border = thin_border

    # Outer border
    for r in range(2, row + 1):
        for c in range(1, 6):
            cl = ws.cell(row=r, column=c)
            ex = cl.border
            cl.border = Border(
                left=thick_side if c == 1 else ex.left,
                right=thick_side if c == 5 else ex.right,
                top=thick_side if r == 2 else ex.top,
                bottom=thick_side if r == row else ex.bottom,
            )

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 35
    ws.freeze_panes = "A3"

    # ── Summary Sheet ────────────────────────────────────────────
    ws_sum = wb.create_sheet(title="Summary")

    ws_sum.cell(row=1, column=1, value="Batch Recommender Report").font = Font(bold=True, size=12)
    ws_sum.cell(row=3, column=1, value="Year").font = bold_font
    ws_sum.cell(row=3, column=2, value=year)
    ws_sum.cell(row=3, column=3, value="Semester").font = bold_font
    ws_sum.cell(row=3, column=4, value=semester)
    ws_sum.cell(row=4, column=1, value="Program").font = bold_font
    ws_sum.cell(row=4, column=2, value=program or "All")
    ws_sum.cell(row=4, column=3, value="Section").font = bold_font
    ws_sum.cell(row=4, column=4, value=section or "All")

    ws_sum.cell(row=6, column=1, value="Students Scanned").font = bold_font
    ws_sum.cell(row=6, column=2, value=student_count)
    ws_sum.cell(row=6, column=2).font = Font(bold=True, size=14, color="0A8E6E")
    ws_sum.cell(row=7, column=1, value="Courses Found").font = bold_font
    ws_sum.cell(row=7, column=2, value=len(sorted_courses))
    ws_sum.cell(row=8, column=1, value="Total Demand").font = bold_font
    ws_sum.cell(row=8, column=2, value=total_demand)

    # Top 10
    ws_sum.cell(row=10, column=1, value="Top 10 Courses").font = Font(bold=True, size=11)
    for col, hdr in enumerate(["Course", "Name", "Count"], start=1):
        cell = ws_sum.cell(row=11, column=col)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border

    for i, (code, count) in enumerate(sorted_courses[:10]):
        r = 12 + i
        ws_sum.cell(row=r, column=1, value=code).font = bold_font
        ws_sum.cell(row=r, column=1).border = thin_border
        name = course_names.get(code.upper(), "")
        ws_sum.cell(row=r, column=2, value=name).font = Font(size=8)
        ws_sum.cell(row=r, column=2).border = thin_border
        ws_sum.cell(row=r, column=3, value=count).border = thin_border
        ws_sum.cell(row=r, column=3).alignment = center_align

    ws_sum.column_dimensions["A"].width = 18
    ws_sum.column_dimensions["B"].width = 30
    ws_sum.column_dimensions["C"].width = 14
    ws_sum.column_dimensions["D"].width = 14

    # Save
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
