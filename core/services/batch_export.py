"""
core/services/batch_export.py
XLSX export for the Batch Recommender (aggregate course demand).

Creates a styled workbook with:
  - Demand sheet: course code, student count, % share, fill bar
  - Summary sheet: filters, total students, top courses chart data
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def export_batch_recommender_xlsx(
    year: int,
    semester: int,
    program: str | None,
    section: str | None,
    student_count: int,
    aggregate: dict[str, int],
) -> Path:
    """Export batch recommender results as styled XLSX.

    Args:
        year, semester: academic period
        program, section: filters applied
        student_count: total students scanned
        aggregate: {course_code: count} sorted by count desc

    Returns:
        Path to temporary .xlsx file.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError as exc:
        raise RuntimeError("openpyxl is required") from exc

    # Styles
    hdr_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    hdr_font = Font(name="Consolas", size=9, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    bold_font = Font(bold=True, size=9)
    normal_font = Font(name="Consolas", size=9)
    center_align = Alignment(horizontal="center", vertical="center")

    wb = Workbook()
    wb.remove(wb.active)

    # ── Demand Sheet ─────────────────────────────────────────────
    ws = wb.create_sheet(title="Course Demand")

    headers = ["#", "Course Code", "Students Needing", "% of Students", "Share"]
    for col, hdr in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border

    # Sort by count descending
    sorted_courses = sorted(aggregate.items(), key=lambda x: -x[1])
    max_count = sorted_courses[0][1] if sorted_courses else 1

    row = 2
    for rank, (code, count) in enumerate(sorted_courses, start=1):
        pct = round(count / max(student_count, 1) * 100)
        share_pct = round(count / max(max_count, 1) * 100)

        # Rank
        ws.cell(row=row, column=1, value=rank).font = Font(size=9, color="999999")
        ws.cell(row=row, column=1).border = thin_border
        ws.cell(row=row, column=1).alignment = center_align

        # Course code
        ws.cell(row=row, column=2, value=code).font = bold_font
        ws.cell(row=row, column=2).border = thin_border

        # Count
        ws.cell(row=row, column=3, value=count).font = normal_font
        ws.cell(row=row, column=3).border = thin_border
        ws.cell(row=row, column=3).alignment = center_align

        # Percentage
        ws.cell(row=row, column=4, value=f"{pct}%").font = normal_font
        ws.cell(row=row, column=4).border = thin_border
        ws.cell(row=row, column=4).alignment = center_align

        # Visual bar using repeated block character
        bar_len = max(1, share_pct // 2)  # 1-50 chars
        bar = "\u2588" * bar_len
        cell = ws.cell(row=row, column=5, value=bar)
        cell.font = Font(size=8, color="0A8E6E")
        cell.border = thin_border

        # Color gradient: high demand = darker green, low = lighter
        if pct >= 50:
            ws.cell(row=row, column=3).fill = PatternFill(
                start_color="A9DFBF", end_color="A9DFBF", fill_type="solid"
            )
        elif pct >= 25:
            ws.cell(row=row, column=3).fill = PatternFill(
                start_color="D5F5E3", end_color="D5F5E3", fill_type="solid"
            )

        row += 1

    # Totals
    total_demand = sum(aggregate.values())
    ws.cell(row=row, column=1).border = thin_border
    ws.cell(row=row, column=2, value="TOTAL").font = Font(bold=True, size=9)
    ws.cell(row=row, column=2).border = thin_border
    ws.cell(row=row, column=3, value=total_demand).font = Font(bold=True, size=9)
    ws.cell(row=row, column=3).border = thin_border
    ws.cell(row=row, column=3).alignment = center_align
    ws.cell(row=row, column=4, value=f"{len(sorted_courses)} courses").font = Font(size=9, color="666666")
    ws.cell(row=row, column=4).border = thin_border

    # Outer border
    thick_side = Side(style="medium", color="333333")
    for r in range(1, row + 1):
        for c in range(1, 6):
            cl = ws.cell(row=r, column=c)
            ex = cl.border
            cl.border = Border(
                left=thick_side if c == 1 else ex.left,
                right=thick_side if c == 5 else ex.right,
                top=thick_side if r == 1 else ex.top,
                bottom=thick_side if r == row else ex.bottom,
            )

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 30
    ws.freeze_panes = "A2"

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
    for col, hdr in enumerate(["Course", "Count", "% Students"], start=1):
        cell = ws_sum.cell(row=11, column=col)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border

    for i, (code, count) in enumerate(sorted_courses[:10]):
        r = 12 + i
        pct = round(count / max(student_count, 1) * 100)
        ws_sum.cell(row=r, column=1, value=code).font = bold_font
        ws_sum.cell(row=r, column=1).border = thin_border
        ws_sum.cell(row=r, column=2, value=count).border = thin_border
        ws_sum.cell(row=r, column=2).alignment = center_align
        ws_sum.cell(row=r, column=3, value=f"{pct}%").border = thin_border
        ws_sum.cell(row=r, column=3).alignment = center_align

    ws_sum.column_dimensions["A"].width = 18
    ws_sum.column_dimensions["B"].width = 14
    ws_sum.column_dimensions["C"].width = 14
    ws_sum.column_dimensions["D"].width = 14

    # Save
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
