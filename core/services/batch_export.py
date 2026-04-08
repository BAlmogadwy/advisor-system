"""
core/services/batch_export.py
XLSX export for the Batch Recommender (aggregate course demand).

Layout: each course gets a bold header row with demand count.
Prerequisites are listed as indented sub-rows below each course,
showing how many students are currently studying each prerequisite.
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
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

    # ── Styles ───────────────────────────────────────────────────
    hdr_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    hdr_font = Font(name="Consolas", size=9, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center")
    title_fill = PatternFill(start_color="111144", end_color="111144", fill_type="solid")
    title_font = Font(name="Consolas", size=11, bold=True, color="FFFFFF")
    thick_side = Side(style="medium", color="333333")
    thin_side = Side(style="thin", color="CCCCCC")
    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    course_fill = PatternFill(start_color="EBF5FB", end_color="EBF5FB", fill_type="solid")
    course_font = Font(name="Consolas", size=10, bold=True, color="111144")
    count_font = Font(name="Consolas", size=11, bold=True, color="0A8E6E")
    prereq_indent_font = Font(size=9, color="666666")
    studying_font = Font(size=9, bold=True, color="C03030")
    studying_fill = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")
    passed_font = Font(size=9, color="0A8E6E")
    passed_fill = PatternFill(start_color="E8F5F0", end_color="E8F5F0", fill_type="solid")
    no_prereq_font = Font(size=8, italic=True, color="AAAAAA")
    center = Alignment(horizontal="center", vertical="center")
    left_indent = Alignment(horizontal="left", vertical="center", indent=2)

    # ── Pre-load data ────────────────────────────────────────────
    sorted_courses = sorted(aggregate.items(), key=lambda x: -x[1])
    course_codes = [code for code, _ in sorted_courses]

    # Course names
    course_names: dict[str, str] = {}
    for c in Course.objects.filter(course_code__in=course_codes):
        course_names[c.course_code.upper()] = c.description or ""

    # Prerequisites
    programs_to_check = [p.strip() for p in (program or "").split(",") if p.strip()]
    prereq_map: dict[str, list[str]] = defaultdict(list)
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

    # Studying counts per prerequisite
    all_prereq_codes = set()
    for prereqs in prereq_map.values():
        all_prereq_codes.update(prereqs)

    prereq_studying_count: dict[str, int] = {}
    if all_prereq_codes:
        for row in (
            StudentCourse.objects.filter(
                course__course_code__in=all_prereq_codes, status="studying",
            )
            .values("course__course_code")
            .annotate(cnt=DjCount("id"))
        ):
            prereq_studying_count[normalize_code(row["course__course_code"])] = row["cnt"]

    # Prereq course names
    prereq_names: dict[str, str] = {}
    if all_prereq_codes:
        for c in Course.objects.filter(course_code__in=all_prereq_codes):
            prereq_names[c.course_code.upper()] = c.description or ""

    wb = Workbook()
    wb.remove(wb.active)

    # ── Demand Sheet ─────────────────────────────────────────────
    ws = wb.create_sheet(title="Course Demand")

    # Title
    prog_label = program or "All Programs"
    sec_label = f"Section {section}" if section else "All Sections"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    tc = ws.cell(row=1, column=1)
    tc.value = f"Batch Recommender — {prog_label} | {sec_label} | {year}/T{semester} | {student_count} students"
    tc.fill = title_fill
    tc.font = title_font
    tc.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(1, 6):
        ws.cell(row=1, column=c).fill = title_fill

    # Headers
    row = 2
    for col, (hdr, w) in enumerate([
        ("#", 5), ("Course", 14), ("Name", 32), ("Students", 12), ("Prerequisite Status", 40),
    ], start=1):
        cell = ws.cell(row=row, column=col)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border

    # Data rows
    row = 3
    for rank, (code, count) in enumerate(sorted_courses, start=1):
        prereqs = prereq_map.get(normalize_code(code), [])
        name = course_names.get(code.upper(), "")

        # ── Course header row ────────────────────────────────────
        # Rank
        ws.cell(row=row, column=1, value=rank).font = Font(size=9, bold=True, color="999999")
        ws.cell(row=row, column=1).border = thin_border
        ws.cell(row=row, column=1).alignment = center

        # Course code (bold, colored background)
        cell = ws.cell(row=row, column=2, value=code)
        cell.font = course_font
        cell.fill = course_fill
        cell.border = thin_border

        # Name
        cell = ws.cell(row=row, column=3, value=name)
        cell.font = Font(size=8, color="444444")
        cell.fill = course_fill
        cell.border = thin_border

        # Student count
        cell = ws.cell(row=row, column=4, value=count)
        cell.font = count_font
        cell.alignment = center
        cell.fill = course_fill
        cell.border = thin_border
        # Demand gradient
        pct = round(count / max(student_count, 1) * 100)
        if pct >= 50:
            cell.fill = PatternFill(start_color="A9DFBF", end_color="A9DFBF", fill_type="solid")
        elif pct >= 30:
            cell.fill = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")

        # Prereq summary in course row
        if not prereqs:
            cell = ws.cell(row=row, column=5, value="No prerequisites")
            cell.font = no_prereq_font
            cell.fill = course_fill
            cell.border = thin_border
        else:
            studying_total = sum(prereq_studying_count.get(p, 0) for p in prereqs)
            if studying_total > 0:
                cell = ws.cell(row=row, column=5, value=f"{len(prereqs)} prerequisites ({studying_total} students still studying)")
                cell.font = Font(size=8, bold=True, color="C03030")
                cell.fill = studying_fill
            else:
                cell = ws.cell(row=row, column=5, value=f"{len(prereqs)} prerequisites (all passed)")
                cell.font = Font(size=8, color="0A8E6E")
                cell.fill = passed_fill
            cell.border = thin_border

        row += 1

        # ── Prerequisite sub-rows ────────────────────────────────
        for pr in prereqs:
            studying_cnt = prereq_studying_count.get(pr, 0)
            pr_name = prereq_names.get(pr.upper(), "")

            ws.cell(row=row, column=1).border = thin_border
            # Indented prereq code
            cell = ws.cell(row=row, column=2, value=f"  └ {pr}")
            cell.font = prereq_indent_font
            cell.border = thin_border

            # Prereq name
            cell = ws.cell(row=row, column=3, value=pr_name)
            cell.font = Font(size=8, color="888888")
            cell.border = thin_border

            ws.cell(row=row, column=4).border = thin_border

            # Studying count
            if studying_cnt > 0:
                cell = ws.cell(row=row, column=5, value=f"{studying_cnt} students currently studying")
                cell.font = studying_font
                cell.fill = studying_fill
            else:
                cell = ws.cell(row=row, column=5, value="All students passed")
                cell.font = passed_font
                cell.fill = passed_fill
            cell.border = thin_border

            row += 1

    # Totals
    total_demand = sum(aggregate.values())
    for c in range(1, 6):
        ws.cell(row=row, column=c).border = Border(
            left=thin_side, right=thin_side, top=thick_side, bottom=thick_side,
        )
    ws.cell(row=row, column=2, value="TOTAL").font = Font(bold=True, size=10)
    ws.cell(row=row, column=3, value=f"{len(sorted_courses)} courses").font = Font(size=9, color="666666")
    ws.cell(row=row, column=4, value=total_demand).font = Font(bold=True, size=10, color="0A8E6E")
    ws.cell(row=row, column=4).alignment = center

    # Column widths
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 34
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 40
    ws.freeze_panes = "A3"

    # ── Summary Sheet ────────────────────────────────────────────
    ws_sum = wb.create_sheet(title="Summary")

    ws_sum.cell(row=1, column=1, value="Batch Recommender Report").font = Font(bold=True, size=12)
    ws_sum.cell(row=3, column=1, value="Year").font = Font(bold=True, size=9)
    ws_sum.cell(row=3, column=2, value=year)
    ws_sum.cell(row=3, column=3, value="Semester").font = Font(bold=True, size=9)
    ws_sum.cell(row=3, column=4, value=semester)
    ws_sum.cell(row=4, column=1, value="Program").font = Font(bold=True, size=9)
    ws_sum.cell(row=4, column=2, value=program or "All")
    ws_sum.cell(row=4, column=3, value="Section").font = Font(bold=True, size=9)
    ws_sum.cell(row=4, column=4, value=section or "All")

    ws_sum.cell(row=6, column=1, value="Students Scanned").font = Font(bold=True, size=9)
    ws_sum.cell(row=6, column=2, value=student_count).font = Font(bold=True, size=14, color="0A8E6E")
    ws_sum.cell(row=7, column=1, value="Courses Found").font = Font(bold=True, size=9)
    ws_sum.cell(row=7, column=2, value=len(sorted_courses))
    ws_sum.cell(row=8, column=1, value="Total Demand").font = Font(bold=True, size=9)
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
        ws_sum.cell(row=r, column=1, value=code).font = Font(bold=True, size=9)
        ws_sum.cell(row=r, column=1).border = thin_border
        ws_sum.cell(row=r, column=2, value=course_names.get(code.upper(), "")).font = Font(size=8)
        ws_sum.cell(row=r, column=2).border = thin_border
        ws_sum.cell(row=r, column=3, value=count).border = thin_border
        ws_sum.cell(row=r, column=3).alignment = center

    ws_sum.column_dimensions["A"].width = 18
    ws_sum.column_dimensions["B"].width = 32
    ws_sum.column_dimensions["C"].width = 14

    # Save
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
