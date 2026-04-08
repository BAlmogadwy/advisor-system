"""
core/services/timetable_export.py
XLSX export for the Timetable Workspace.

Exports all boards in a scenario to a styled workbook:
- One sheet per term level with timetable grid + course info sidebar
- Summary sheet with budget, student counts, conflict overview
- Conflict Matrix sheet showing student overlap between courses
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_autoplace import WEEKDAYS, get_meeting_pattern
from core.services.timetable_workspace import compute_scenario_budget, detect_board_conflicts


def export_scenario_xlsx(scenario_id: int) -> Path:
    """Export a scenario's timetable to a styled XLSX workbook."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX export") from exc

    scenario = TimetableScenario.objects.get(id=scenario_id)
    boards = list(
        DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order", "label")
    )

    # ── Styles ───────────────────────────────────────────────────
    hdr_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    hdr_font = Font(name="Consolas", size=9, bold=True, color="FFFFFF")
    hdr_align = Alignment(horizontal="center", vertical="center")
    thin_side = Side(style="thin", color="CCCCCC")
    thick_side = Side(style="medium", color="333333")
    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    def _apply_outer_border(ws, min_row, min_col, max_row, max_col):
        """Apply thick outer border around a rectangular range."""
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                cell = ws.cell(row=r, column=c)
                existing = cell.border
                left = thick_side if c == min_col else existing.left
                right = thick_side if c == max_col else existing.right
                top = thick_side if r == min_row else existing.top
                bottom = thick_side if r == max_row else existing.bottom
                cell.border = Border(left=left, right=right, top=top, bottom=bottom)
    bold_font = Font(bold=True, size=9)
    normal_font = Font(name="Consolas", size=9)
    center_align = Alignment(horizontal="center", vertical="center")
    conflict_fill = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")
    board_hdr_fill = PatternFill(start_color="111144", end_color="111144", fill_type="solid")
    board_hdr_font = Font(name="Consolas", size=10, bold=True, color="FFFFFF")
    slot_fill = PatternFill(start_color="E8F5F0", end_color="E8F5F0", fill_type="solid")
    info_hdr_fill = PatternFill(start_color="4056E3", end_color="4056E3", fill_type="solid")
    info_hdr_font = Font(name="Consolas", size=9, bold=True, color="FFFFFF")

    DAY_LABELS = ["SUN", "MON", "TUE", "WED", "THU"]
    # Course info sidebar starts at column H (8)
    INFO_START_COL = 8

    # ── Distinct pastel colors for courses ───────────────────────
    # 20 visually distinct pastels with good contrast for dark text
    COURSE_COLORS = [
        "D4E6F1",  # light blue
        "D5F5E3",  # light green
        "FADBD8",  # light pink
        "FCF3CF",  # light yellow
        "D7BDE2",  # light purple
        "A9DFBF",  # mint
        "F9E79F",  # gold
        "AED6F1",  # sky blue
        "F5CBA7",  # peach
        "A3E4D7",  # teal light
        "E8DAEF",  # lavender
        "FDEBD0",  # cream
        "ABB2B9",  # silver
        "A2D9CE",  # sea green
        "F5B7B1",  # salmon
        "D6DBDF",  # light gray
        "ABEBC6",  # pale green
        "FAD7A0",  # light orange
        "D2B4DE",  # orchid
        "AEB6BF",  # cool gray
    ]

    def _course_fill(course_code: str, color_map: dict[str, str]) -> PatternFill:
        """Get or assign a distinct pastel fill for a course."""
        if course_code not in color_map:
            idx = len(color_map) % len(COURSE_COLORS)
            color_map[course_code] = COURSE_COLORS[idx]
        hex_c = color_map[course_code]
        return PatternFill(start_color=hex_c, end_color=hex_c, fill_type="solid")

    wb = Workbook()
    wb.remove(wb.active)

    # Pre-load budget for all terms
    budget_all = list(ScenarioSectionBudget.objects.filter(scenario=scenario))
    budget_by_term: dict[int, list] = defaultdict(list)
    for b in budget_all:
        budget_by_term[b.programme_term or 0].append(b)

    # Pre-load course names from Course table
    from core.models import Course
    course_names: dict[str, str] = {}
    for c in Course.objects.all():
        course_names[c.course_code.upper()] = c.description or c.course_code

    # ── Group boards by nominal_term ─────────────────────────────
    boards_by_term: dict[int, list[DeliveryBoard]] = defaultdict(list)
    for b in boards:
        boards_by_term[b.nominal_term or 0].append(b)

    # ── One sheet per term level ─────────────────────────────────
    for term_num in sorted(boards_by_term.keys()):
        term_boards = boards_by_term[term_num]
        ws = wb.create_sheet(title=f"Term {term_num}")

        row = 1

        for board_idx, board in enumerate(term_boards):
            if board_idx > 0:
                row += 2

            # Board header
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            cell = ws.cell(row=row, column=1)
            primary_count = BoardStudentLink.objects.filter(
                board=board, link_type="primary"
            ).count()
            visitor_count = BoardStudentLink.objects.filter(
                board=board, link_type="visitor"
            ).count()
            cell.value = f"{board.label} — {primary_count} primary + {visitor_count} visitors"
            cell.fill = board_hdr_fill
            cell.font = board_hdr_font
            cell.alignment = center_align
            for c in range(1, 7):
                ws.cell(row=row, column=c).fill = board_hdr_fill
            row += 1

            # Slot config
            slot_config = scenario.slot_config or []
            if not slot_config:
                from core.services.timetable_autoplace import DEFAULT_SLOTS
                slot_config = DEFAULT_SLOTS

            # Timetable grid header
            headers = ["Time"] + DAY_LABELS
            for col_idx, hdr in enumerate(headers, start=1):
                cell = ws.cell(row=row, column=col_idx)
                cell.value = hdr
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.alignment = hdr_align
                cell.border = thin_border

            grid_header_row = row
            row += 1

            # Load placements
            placements = list(
                SectionPlacement.objects.filter(board=board)
                .select_related("term_section")
                .order_by("day", "start_time")
            )

            # ── Split placements into student groups ─────────────
            # Group 1 = S1 of each course, Group 2 = S2, etc.
            # Find max section number to determine group count
            from collections import defaultdict as _dd
            course_sections: dict[str, list] = _dd(list)
            for p in placements:
                course_sections[p.term_section.course_code].append(p)

            max_groups = max((len(secs) for secs in course_sections.values()), default=1)
            # Normalize: for each course, sort sections by label (S1, S2...)
            for code in course_sections:
                course_sections[code].sort(key=lambda p: p.term_section.section)

            # Assign distinct pastel color per course (shared across all groups)
            course_color_map: dict[str, str] = {}

            for group_idx in range(max_groups):
                if group_idx > 0:
                    row += 1  # gap between groups

                # Group header
                group_label = f"Group {group_idx + 1}" if max_groups > 1 else ""
                if group_label:
                    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
                    gcell = ws.cell(row=row, column=1)
                    # Count courses in this group
                    group_course_count = sum(
                        1 for secs in course_sections.values() if len(secs) > group_idx
                    )
                    gcell.value = f"Group {group_idx + 1} — {group_course_count} courses"
                    gcell.font = Font(bold=True, size=9, color="4056E3")
                    gcell.alignment = center_align
                    row += 1

                # Grid header
                for col_idx, hdr in enumerate(["Time"] + DAY_LABELS, start=1):
                    cell = ws.cell(row=row, column=col_idx)
                    cell.value = hdr
                    cell.fill = hdr_fill
                    cell.font = hdr_font
                    cell.alignment = hdr_align
                    cell.border = thin_border
                grid_header_row = row
                row += 1

                # Get placements for this group: pick section[group_idx] from each course
                group_placements = []
                for code, secs in course_sections.items():
                    if group_idx < len(secs):
                        group_placements.append(secs[group_idx])

                # Build grid for this group
                grid: dict[str, dict[str, dict]] = {}
                for slot in slot_config:
                    slot_key = f"{slot['start']}-{slot['end']}"
                    grid[slot_key] = {d: {"text": "", "courses": []} for d in DAY_LABELS}

                # Collect ALL meeting placements for each section in this group
                for p in group_placements:
                    # Get all placements for this term_section on this board
                    ts_placements = [
                        pp for pp in placements
                        if pp.term_section_id == p.term_section_id
                    ]
                    for pp in ts_placements:
                        slot_key = f"{pp.start_time}-{pp.end_time}"
                        day = pp.day.upper()[:3]
                        if slot_key not in grid:
                            for i, slot in enumerate(slot_config):
                                if slot["start"] == pp.start_time:
                                    if i + 1 < len(slot_config) and slot_config[i + 1]["end"] == pp.end_time:
                                        slot_key = f"{slot['start']}-{slot_config[i + 1]['end']}"
                                        if slot_key not in grid:
                                            grid[slot_key] = {d: {"text": "", "courses": []} for d in DAY_LABELS}
                                    break

                        ts = pp.term_section
                        meeting = TermSectionMeeting.objects.filter(term_section=ts, day=day).first()
                        instructor = meeting.instructor if meeting else ""
                        room = meeting.room if meeting else pp.room

                        text = f"{ts.course_code} {ts.section}"
                        if instructor:
                            text += f"\n{instructor}"
                        if room:
                            text += f"\n{room}"

                        if slot_key in grid and day in grid[slot_key]:
                            cd = grid[slot_key][day]
                            if cd["text"]:
                                cd["text"] += f"\n---\n{text}"
                            else:
                                cd["text"] = text
                            cd["courses"].append(ts.course_code)

                # Write grid rows
                for slot_key in sorted(grid.keys()):
                    cell = ws.cell(row=row, column=1)
                    cell.value = slot_key
                    cell.font = bold_font
                    cell.fill = slot_fill
                    cell.border = thin_border
                    cell.alignment = center_align

                    for day_idx, day in enumerate(DAY_LABELS):
                        cell = ws.cell(row=row, column=day_idx + 2)
                        cd = grid[slot_key].get(day, {"text": "", "courses": []})
                        cell.value = cd["text"]
                        cell.font = normal_font
                        cell.border = thin_border
                        cell.alignment = Alignment(
                            horizontal="center", vertical="center", wrap_text=True
                        )
                        if len(cd["courses"]) > 1:
                            cell.fill = conflict_fill
                        elif len(cd["courses"]) == 1:
                            cell.fill = _course_fill(cd["courses"][0], course_color_map)

                    row += 1

                # Outer border for this group's grid
                _apply_outer_border(ws, grid_header_row, 1, row - 1, 6)

            # Conflicts below grid
            conflicts = detect_board_conflicts(board.id)
            summary = conflicts["summary"]
            if summary["critical"] > 0 or summary["warning"] > 0:
                row += 1
                ws.cell(row=row, column=1, value="Conflicts:").font = Font(
                    bold=True, color="C03030", size=9
                )
                row += 1
                for o in conflicts.get("overlaps", []):
                    ws.cell(row=row, column=1, value=f"OVERLAP: {' vs '.join(o.get('sections', []))}").font = Font(color="C03030", size=8)
                    ws.cell(row=row, column=3, value=o.get("detail", ""))
                    row += 1
                for c in conflicts.get("instructor_clashes", []):
                    ws.cell(row=row, column=1, value=f"INSTRUCTOR: {c.get('instructor', '')}").font = Font(color="C03030", size=8)
                    ws.cell(row=row, column=3, value=" vs ".join(c.get("sections", [])))
                    row += 1
                for c in conflicts.get("room_clashes", []):
                    ws.cell(row=row, column=1, value=f"ROOM: {c.get('room', '')}").font = Font(color="D97706", size=8)
                    ws.cell(row=row, column=3, value=" vs ".join(c.get("sections", [])))
                    row += 1

        # ── Course Info Sidebar (right side of each sheet) ───────
        term_budget = budget_by_term.get(term_num, [])
        if term_budget:
            info_row = 1
            # Header
            info_headers = ["Course", "Name", "Cr", "Sec.", "Students"]
            for ci, hdr in enumerate(info_headers):
                cell = ws.cell(row=info_row, column=INFO_START_COL + ci)
                cell.value = hdr
                cell.fill = info_hdr_fill
                cell.font = info_hdr_font
                cell.alignment = hdr_align
                cell.border = thin_border
            info_row += 1

            total_sections = 0
            total_students = 0
            for b in sorted(term_budget, key=lambda x: x.course_code):
                cr = b.credit_hours or 0
                pattern = get_meeting_pattern(cr)
                c_cell = ws.cell(row=info_row, column=INFO_START_COL, value=b.course_code)
                c_cell.font = Font(bold=True, size=9)
                c_cell.border = thin_border
                # Match the timetable grid color for this course
                c_cell.fill = _course_fill(b.course_code, course_color_map)
                name = course_names.get(b.course_code.upper(), b.course_code)
                ws.cell(row=info_row, column=INFO_START_COL + 1, value=name).font = normal_font
                ws.cell(row=info_row, column=INFO_START_COL + 1).border = thin_border
                ws.cell(row=info_row, column=INFO_START_COL + 2, value=cr).font = normal_font
                ws.cell(row=info_row, column=INFO_START_COL + 2).border = thin_border
                ws.cell(row=info_row, column=INFO_START_COL + 2).alignment = center_align
                ws.cell(row=info_row, column=INFO_START_COL + 3, value=b.planned_sections).font = normal_font
                ws.cell(row=info_row, column=INFO_START_COL + 3).border = thin_border
                ws.cell(row=info_row, column=INFO_START_COL + 3).alignment = center_align
                ws.cell(row=info_row, column=INFO_START_COL + 4, value=b.total_demand).font = normal_font
                ws.cell(row=info_row, column=INFO_START_COL + 4).border = thin_border
                ws.cell(row=info_row, column=INFO_START_COL + 4).alignment = center_align
                total_sections += b.planned_sections
                total_students += b.total_demand
                info_row += 1

            # Totals row
            ws.cell(row=info_row, column=INFO_START_COL, value="TOTAL").font = Font(bold=True, size=9)
            ws.cell(row=info_row, column=INFO_START_COL).border = thin_border
            ws.cell(row=info_row, column=INFO_START_COL + 1).border = thin_border
            ws.cell(row=info_row, column=INFO_START_COL + 2).border = thin_border
            ws.cell(row=info_row, column=INFO_START_COL + 3, value=total_sections).font = Font(bold=True, size=9)
            ws.cell(row=info_row, column=INFO_START_COL + 3).border = thin_border
            ws.cell(row=info_row, column=INFO_START_COL + 3).alignment = center_align
            ws.cell(row=info_row, column=INFO_START_COL + 4, value=total_students).font = Font(bold=True, size=9)
            ws.cell(row=info_row, column=INFO_START_COL + 4).border = thin_border
            ws.cell(row=info_row, column=INFO_START_COL + 4).alignment = center_align

            # Outer border for course info table
            _apply_outer_border(ws, 1, INFO_START_COL, info_row, INFO_START_COL + 4)

            # ── Per-Term Conflict Matrix (right of course info table) ─
            # Course info ends at column L (INFO_START_COL + 4 = col 12)
            # Gap column M (13), matrix starts at column N (14)
            MATRIX_START_COL = INFO_START_COL + 6  # col 14 = N
            term_courses = {b.course_code for b in term_budget}
            _write_mini_conflict_matrix(
                ws, 1, MATRIX_START_COL, scenario, term_num, term_courses,
                hdr_fill, hdr_font, hdr_align, thin_border, normal_font, center_align,
            )

        # Column widths
        ws.column_dimensions["A"].width = 16
        for col_letter in ["B", "C", "D", "E", "F"]:
            ws.column_dimensions[col_letter].width = 22
        ws.column_dimensions["G"].width = 3  # gap
        ws.column_dimensions["H"].width = 12  # Course
        ws.column_dimensions["I"].width = 30  # Name
        ws.column_dimensions["J"].width = 6   # Cr
        ws.column_dimensions["K"].width = 6   # Sec
        ws.column_dimensions["L"].width = 10  # Students

    # ── Conflict Matrix Sheet ────────────────────────────────────
    _build_conflict_matrix_sheet(wb, scenario, hdr_fill, hdr_font, hdr_align, thin_border,
                                 normal_font, bold_font, center_align)

    # ── Summary Sheet ────────────────────────────────────────────
    ws_sum = wb.create_sheet(title="Summary")

    ws_sum.cell(row=1, column=1, value="Scenario").font = bold_font
    ws_sum.cell(row=1, column=2, value=scenario.name)
    ws_sum.cell(row=2, column=1, value="Year").font = bold_font
    ws_sum.cell(row=2, column=2, value=scenario.academic_year)
    ws_sum.cell(row=2, column=3, value="Term").font = bold_font
    ws_sum.cell(row=2, column=4, value=scenario.term)
    ws_sum.cell(row=3, column=1, value="Status").font = bold_font
    ws_sum.cell(row=3, column=2, value=scenario.status)

    # Board summary
    row = 5
    ws_sum.cell(row=row, column=1, value="Boards").font = Font(bold=True, size=11)
    row += 1
    for col_idx, hdr in enumerate(
        ["Board", "Term", "Primary", "Visitors", "Sections", "Critical", "Warnings"], start=1
    ):
        cell = ws_sum.cell(row=row, column=col_idx)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border
    row += 1

    for board in boards:
        primary = BoardStudentLink.objects.filter(board=board, link_type="primary").count()
        visitor = BoardStudentLink.objects.filter(board=board, link_type="visitor").count()
        placed = SectionPlacement.objects.filter(board=board).count()
        conflicts = detect_board_conflicts(board.id)
        ws_sum.cell(row=row, column=1, value=board.label).border = thin_border
        ws_sum.cell(row=row, column=2, value=board.nominal_term).border = thin_border
        ws_sum.cell(row=row, column=3, value=primary).border = thin_border
        ws_sum.cell(row=row, column=4, value=visitor).border = thin_border
        ws_sum.cell(row=row, column=5, value=placed).border = thin_border
        c_cell = ws_sum.cell(row=row, column=6, value=conflicts["summary"]["critical"])
        c_cell.border = thin_border
        if conflicts["summary"]["critical"] > 0:
            c_cell.fill = conflict_fill
        w_cell = ws_sum.cell(row=row, column=7, value=conflicts["summary"]["warning"])
        w_cell.border = thin_border
        row += 1

    # Outer border for boards table (header row to last data row)
    _apply_outer_border(ws_sum, 6, 1, row - 1, 7)

    # Section budget
    row += 2
    ws_sum.cell(row=row, column=1, value="Section Budget").font = Font(bold=True, size=11)
    row += 1
    for col_idx, hdr in enumerate(
        ["Course", "Term", "Credits", "Meetings/wk", "Planned", "Used", "Remaining", "Demand"],
        start=1,
    ):
        cell = ws_sum.cell(row=row, column=col_idx)
        cell.value = hdr
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border
    row += 1

    budget = compute_scenario_budget(scenario_id)
    for b in budget:
        cr = b.get("credit_hours", 3)
        meetings = len(get_meeting_pattern(cr))
        ws_sum.cell(row=row, column=1, value=b["course_code"]).border = thin_border
        ws_sum.cell(row=row, column=2, value=b.get("programme_term")).border = thin_border
        ws_sum.cell(row=row, column=3, value=cr).border = thin_border
        ws_sum.cell(row=row, column=4, value=f"{meetings}x").border = thin_border
        ws_sum.cell(row=row, column=5, value=b["planned_sections"]).border = thin_border
        ws_sum.cell(row=row, column=6, value=b["used_sections"]).border = thin_border
        rem_cell = ws_sum.cell(row=row, column=7, value=b["remaining_sections"])
        rem_cell.border = thin_border
        if b["remaining_sections"] > 0:
            rem_cell.fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
        ws_sum.cell(row=row, column=8, value=b["total_demand"]).border = thin_border
        row += 1

    # Outer border for budget table
    budget_hdr_row = row - len(budget) - 1  # header row of budget table
    _apply_outer_border(ws_sum, budget_hdr_row, 1, row - 1, 8)

    ws_sum.column_dimensions["A"].width = 16
    for col_letter in ["B", "C", "D", "E", "F", "G", "H"]:
        ws_sum.column_dimensions[col_letter].width = 14
    ws_sum.freeze_panes = "A2"

    # ── Save ─────────────────────────────────────────────────────
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)


def _write_mini_conflict_matrix(
    ws, start_row, start_col, scenario, term_num, term_courses,
    hdr_fill, hdr_font, hdr_align, thin_border, normal_font, center_align,
):
    """Write a conflict matrix for one term level on an existing sheet.

    Includes ALL courses that the board's students need (not just the term's
    curriculum courses). Cross-term courses are marked with an asterisk.
    This is critical because a Term 3 student repeating MATH105 (Term 1) must
    not have it overlap with their Term 3 courses.
    """
    from openpyxl.styles import Border, Font, PatternFill, Side

    # Get students relevant to this term (primary + visitors)
    from core.models import BoardStudentLink
    board_ids = list(
        DeliveryBoard.objects.filter(
            scenario=scenario, nominal_term=term_num
        ).values_list("id", flat=True)
    )
    student_ids = set(
        BoardStudentLink.objects.filter(
            board_id__in=board_ids
        ).values_list("student_id", flat=True)
    )

    # Get ALL recommended courses for these students (not filtered by term)
    student_maps = ScenarioStudentMap.objects.filter(
        scenario=scenario, student_id__in=student_ids
    )
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    courses = sorted(course_students.keys())
    n = len(courses)
    if n == 0:
        return

    # Identify which courses are cross-term (not in this term's curriculum)
    cross_term_courses = {c for c in courses if c not in term_courses}
    native_count = n - len(cross_term_courses)

    # Title
    r = start_row
    ws.cell(row=r, column=start_col, value=f"Conflict Matrix — Term {term_num}").font = Font(
        bold=True, size=10
    )
    subtitle = f"{len(student_ids)} students, {native_count} courses"
    if cross_term_courses:
        subtitle += f" + {len(cross_term_courses)} cross-term (*)"
    ws.cell(row=r + 1, column=start_col, value=subtitle).font = Font(size=8, color="666666")
    r += 3

    # Build matrix
    sets = [course_students[c] for c in courses]
    matrix = [[0] * n for _ in range(n)]
    max_val = 0
    for i in range(n):
        matrix[i][i] = len(sets[i])
        for j in range(i + 1, n):
            shared = len(sets[i] & sets[j])
            matrix[i][j] = shared
            matrix[j][i] = shared
            if shared > max_val:
                max_val = shared

    # Header row
    cross_hdr_fill = PatternFill(start_color="4056E3", end_color="4056E3", fill_type="solid")
    ws.cell(row=r, column=start_col).border = thin_border
    for j, code in enumerate(courses):
        cell = ws.cell(row=r, column=start_col + 1 + j)
        is_cross = code in cross_term_courses
        cell.value = f"*{code}" if is_cross else code
        cell.fill = cross_hdr_fill if is_cross else hdr_fill
        cell.font = Font(name="Consolas", size=8, bold=True, color="FFFFFF")
        cell.alignment = center_align
        cell.border = thin_border
    r += 1

    diag_fill = PatternFill(start_color="E8F5F0", end_color="E8F5F0", fill_type="solid")
    zero_font = Font(name="Consolas", size=8, color="CCCCCC")

    for i, code in enumerate(courses):
        is_cross = code in cross_term_courses
        cell = ws.cell(row=r, column=start_col)
        cell.value = f"*{code}" if is_cross else code
        cell.fill = cross_hdr_fill if is_cross else hdr_fill
        cell.font = Font(name="Consolas", size=8, bold=True, color="FFFFFF")
        cell.border = thin_border

        for j in range(n):
            cell = ws.cell(row=r, column=start_col + 1 + j)
            val = matrix[i][j]
            cell.value = val
            cell.border = thin_border
            cell.alignment = center_align

            if i == j:
                cell.fill = diag_fill
                cell.font = Font(name="Consolas", size=8, bold=True, color="0A8E6E")
            elif val == 0:
                cell.font = zero_font
            elif max_val > 0:
                t = min(val / max_val, 1.0)
                rc = int(200 - 150 * t)
                gc = int(230 - 140 * t)
                bc = int(220 - 10 * t)
                cell.fill = PatternFill(
                    start_color=f"{rc:02X}{gc:02X}{bc:02X}",
                    end_color=f"{rc:02X}{gc:02X}{bc:02X}",
                    fill_type="solid",
                )
                cell.font = Font(name="Consolas", size=8, bold=True)

        r += 1

    # Outer border for mini conflict matrix (header row to last data row)
    matrix_hdr_row = start_row + 3
    matrix_end_row = r - 1
    matrix_end_col = start_col + n
    if matrix_end_row >= matrix_hdr_row:
        thick = Side(style="medium", color="333333")
        for mr in range(matrix_hdr_row, matrix_end_row + 1):
            for mc in range(start_col, matrix_end_col + 1):
                cl = ws.cell(row=mr, column=mc)
                ex = cl.border
                cl.border = Border(
                    left=thick if mc == start_col else ex.left,
                    right=thick if mc == matrix_end_col else ex.right,
                    top=thick if mr == matrix_hdr_row else ex.top,
                    bottom=thick if mr == matrix_end_row else ex.bottom,
                )


def _build_conflict_matrix_sheet(wb, scenario, hdr_fill, hdr_font, hdr_align,
                                  thin_border, normal_font, bold_font, center_align):
    """Build a Conflict Matrix sheet showing student overlap between courses."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    ws = wb.create_sheet(title="Conflicts (All)")

    # Load student-course data from ScenarioStudentMap (all students, all courses)
    student_maps = ScenarioStudentMap.objects.filter(scenario=scenario)
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    courses = sorted(course_students.keys())
    n = len(courses)

    if n == 0:
        ws.cell(row=1, column=1, value="No student data available")
        return

    # Build NxN matrix
    sets = [course_students[c] for c in courses]
    matrix = [[0] * n for _ in range(n)]
    max_val = 0
    for i in range(n):
        matrix[i][i] = len(sets[i])
        for j in range(i + 1, n):
            shared = len(sets[i] & sets[j])
            matrix[i][j] = shared
            matrix[j][i] = shared
            if shared > max_val:
                max_val = shared

    # Title
    ws.cell(row=1, column=1, value="Student Conflict Matrix").font = Font(bold=True, size=12)
    ws.cell(row=2, column=1, value=f"{n} courses, {len(student_maps)} students").font = Font(
        size=9, color="666666"
    )

    start_row = 4

    # Header row (course codes rotated)
    ws.cell(row=start_row, column=1, value="").border = thin_border
    for j, code in enumerate(courses):
        cell = ws.cell(row=start_row, column=j + 2)
        cell.value = code
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="bottom", text_rotation=90)
        cell.border = thin_border

    # Data rows
    diag_fill = PatternFill(start_color="E8F5F0", end_color="E8F5F0", fill_type="solid")
    zero_font = Font(name="Consolas", size=9, color="CCCCCC")

    for i, code in enumerate(courses):
        r = start_row + 1 + i
        # Row header
        cell = ws.cell(row=r, column=1)
        cell.value = code
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.border = thin_border

        for j in range(n):
            cell = ws.cell(row=r, column=j + 2)
            val = matrix[i][j]
            cell.value = val
            cell.border = thin_border
            cell.alignment = center_align

            if i == j:
                # Diagonal: total students for this course
                cell.fill = diag_fill
                cell.font = Font(name="Consolas", size=9, bold=True, color="0A8E6E")
            elif val == 0:
                cell.font = zero_font
            else:
                # Heat-map: teal → royal gradient based on overlap count
                if max_val > 0:
                    t = min(val / max_val, 1.0)
                    r_c = int(200 - 150 * t)
                    g_c = int(230 - 140 * t)
                    b_c = int(220 - 10 * t)
                    hex_color = f"{r_c:02X}{g_c:02X}{b_c:02X}"
                    cell.fill = PatternFill(
                        start_color=hex_color, end_color=hex_color, fill_type="solid"
                    )
                    cell.font = Font(name="Consolas", size=9, bold=True)

    # Outer border for global conflict matrix
    if n > 0:
        thick = Side(style="medium", color="333333")
        end_row = start_row + n
        end_col = 1 + n
        for mr in range(start_row, end_row + 1):
            for mc in range(1, end_col + 1):
                cl = ws.cell(row=mr, column=mc)
                ex = cl.border
                cl.border = Border(
                    left=thick if mc == 1 else ex.left,
                    right=thick if mc == end_col else ex.right,
                    top=thick if mr == start_row else ex.top,
                    bottom=thick if mr == end_row else ex.bottom,
                )

    # Column widths
    ws.column_dimensions["A"].width = 12
    for j in range(n):
        col_letter = chr(ord("B") + j) if j < 25 else None
        if col_letter:
            ws.column_dimensions[col_letter].width = 7

    ws.freeze_panes = "B5"
