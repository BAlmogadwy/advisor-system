"""
core/section_plan_views.py
Next Semester Section Planning — page view + API endpoints.

Endpoints:
    GET  section_plan_page          – render the section planning page
    POST section_plan_generate_view – compute section demand from recommendations
    POST section_plan_export_view   – download section plan as styled .xlsx
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required, throttle
from core.models import ProgrammeRequirement
from core.services.rbac import ROLE_GENERAL_ADVISOR, get_user_role
from core.services.reporting import build_aggregate_counts
from core.services.section_planning import (
    DEFAULT_MAX_EXTERNAL,
    DEFAULT_MAX_LOCAL_4CR,
    DEFAULT_MAX_LOCAL_OTHER,
    compute_plan_summary,
    compute_section_plan,
    get_all_courses_with_defaults,
    load_programme_capacities,
)
from core.services.student_helpers import normalize_code
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


def _require_general_advisor(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 JsonResponse if user is below GENERAL_ADVISOR, else None."""
    from core.services.rbac import ROLE_SUPER_ADMIN

    role = get_user_role(request.user)
    if role not in {ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN}:
        return JsonResponse({"error": "General Advisor access required"}, status=403)
    return None


def _parse_payload(request: HttpRequest) -> tuple[dict | None, JsonResponse | None]:
    """Parse JSON body and extract validated parameters.

    Returns (params_dict, None) on success or (None, error_response) on failure.
    """
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    try:
        year = int(body.get("year", 0))
        semester = int(body.get("semester", 0))
    except (ValueError, TypeError):
        return None, JsonResponse(
            {"ok": False, "error": "year and semester must be integers"},
            status=400,
        )

    if not (1400 <= year <= 1600):
        return None, JsonResponse(
            {"ok": False, "error": "year must be between 1400 and 1600"},
            status=400,
        )
    if semester not in (1, 2, 3):
        return None, JsonResponse(
            {"ok": False, "error": "semester must be 1, 2, or 3"},
            status=400,
        )

    # Support comma-separated programs  e.g. "AI,DS" → ["AI", "DS"]
    program_raw = str(body.get("program", "")).strip()
    if program_raw and "," in program_raw:
        program: str | list[str] | None = [p.strip() for p in program_raw.split(",") if p.strip()]
        if not program:
            program = None
    else:
        program = program_raw or None

    section = str(body.get("section", "")).strip() or None

    try:
        max_local_4cr = int(body.get("max_local_4cr", DEFAULT_MAX_LOCAL_4CR))
        max_local_other = int(body.get("max_local_other", DEFAULT_MAX_LOCAL_OTHER))
        max_external = int(body.get("max_external", DEFAULT_MAX_EXTERNAL))
    except (ValueError, TypeError):
        return None, JsonResponse(
            {"ok": False, "error": "Capacity limits must be integers"},
            status=400,
        )

    # Clamp to reasonable range
    max_local_4cr = max(5, min(max_local_4cr, 200))
    max_local_other = max(5, min(max_local_other, 200))
    max_external = max(5, min(max_external, 200))

    # Per-course capacity overrides  { "CS101": 30, "AI201": 20, ... }
    raw_overrides = body.get("course_overrides") or {}
    course_overrides: dict[str, int] = {}
    if isinstance(raw_overrides, dict):
        for k, v in raw_overrides.items():
            try:
                val = int(v)
                if val >= 1:
                    course_overrides[normalize_code(str(k))] = min(val, 500)
            except (ValueError, TypeError):
                pass

    # Department prefix filter for export  e.g. "CS,AI"
    dept_filter_raw = str(body.get("dept_filter", "")).strip().upper()
    dept_prefixes = (
        [p.strip() for p in dept_filter_raw.split(",") if p.strip()] if dept_filter_raw else []
    )

    return {
        "year": year,
        "semester": semester,
        "program": program,
        "section": section,
        "max_local_4cr": max_local_4cr,
        "max_local_other": max_local_other,
        "max_external": max_external,
        "course_overrides": course_overrides,
        "dept_filter": dept_prefixes,
    }, None


# ── Page view ──────────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def section_plan_page(request: HttpRequest) -> HttpResponse:
    """Render the section planning page."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    ctx = {
        **get_sidebar_context(request),
        "default_year": defaults["academic_year"],
        "default_term": defaults["term"],
    }
    return render(request, "core/section_planning.html", ctx)


# ── Generate API ───────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
@throttle(max_calls=3, window_seconds=120)
def section_plan_generate_view(request: HttpRequest) -> JsonResponse:
    """Compute section demand from batch recommendations."""
    params, err = _parse_payload(request)
    if err:
        return err
    assert params is not None

    program = params["program"]
    capacity_kwargs = {
        "max_local_4cr": params["max_local_4cr"],
        "max_local_other": params["max_local_other"],
        "max_external": params["max_external"],
        "course_overrides": params.get("course_overrides"),
    }

    try:
        if isinstance(program, list):
            # ── Multi-program mode ──
            from collections import Counter

            result_programs: list[dict] = []
            total_student_count = 0
            combined_agg: Counter[str] = Counter()
            for prog in program:
                sc, agg = build_aggregate_counts(
                    params["year"],
                    params["semester"],
                    program=prog,
                    section=params["section"],
                )
                combined_agg += agg
                pr_caps = load_programme_capacities(prog, list(agg.keys()))
                plan = compute_section_plan(
                    agg,
                    **capacity_kwargs,
                    programme_capacities=pr_caps,
                )
                summary = compute_plan_summary(plan)
                result_programs.append(
                    {
                        "program": prog,
                        "student_count": sc,
                        "plan": plan,
                        "summary": summary,
                    }
                )
                total_student_count += sc

            # Build combined union plan using min capacities across programs
            combined_codes = list(combined_agg.keys())
            combined_pr_caps: dict[str, int] = {}
            for prog in program:
                caps = load_programme_capacities(prog, combined_codes)
                for code, cap in caps.items():
                    if code in combined_pr_caps:
                        combined_pr_caps[code] = min(combined_pr_caps[code], cap)
                    else:
                        combined_pr_caps[code] = cap
            combined_plan = compute_section_plan(
                combined_agg,
                **capacity_kwargs,
                programme_capacities=combined_pr_caps,
            )
            combined_summary = compute_plan_summary(combined_plan)

            # Build additive department summary (sum per-program dept stats)
            # This reflects actual teaching load across programs, not recomputed
            additive_dept: dict[str, dict[str, int]] = {}
            for prog_data in result_programs:
                for dept in (prog_data.get("summary") or {}).get("departments", []):
                    d = dept["department"]
                    if d not in additive_dept:
                        additive_dept[d] = {
                            "courses": 0,
                            "sections": 0,
                            "students": 0,
                            "total_credits": 0,
                        }
                    additive_dept[d]["courses"] += dept["courses"]
                    additive_dept[d]["sections"] += dept["sections"]
                    additive_dept[d]["students"] += dept["students"]
                    additive_dept[d]["total_credits"] += dept["total_credits"]
            combined_summary["departments"] = [
                {"department": d, **v} for d, v in sorted(additive_dept.items())
            ]

            return JsonResponse(
                {
                    "ok": True,
                    "mode": "multi",
                    "year": params["year"],
                    "semester": params["semester"],
                    "student_count": total_student_count,
                    "combined_plan": combined_plan,
                    "combined_summary": combined_summary,
                    "programs": result_programs,
                }
            )

        elif isinstance(program, str):
            # ── Single-program mode ──
            student_count, aggregate = build_aggregate_counts(
                params["year"],
                params["semester"],
                program=program,
                section=params["section"],
            )
            pr_caps = load_programme_capacities(program, list(aggregate.keys()))
            plan = compute_section_plan(
                aggregate,
                **capacity_kwargs,
                programme_capacities=pr_caps,
            )
            summary = compute_plan_summary(plan)

            return JsonResponse(
                {
                    "ok": True,
                    "mode": "single",
                    "year": params["year"],
                    "semester": params["semester"],
                    "student_count": student_count,
                    "plan": plan,
                    "summary": summary,
                }
            )

        else:
            # ── Combined mode (no program filter) ──
            student_count, aggregate = build_aggregate_counts(
                params["year"],
                params["semester"],
                program=None,
                section=params["section"],
            )
            plan = compute_section_plan(aggregate, **capacity_kwargs)
            summary = compute_plan_summary(plan)

            return JsonResponse(
                {
                    "ok": True,
                    "mode": "combined",
                    "year": params["year"],
                    "semester": params["semester"],
                    "student_count": student_count,
                    "plan": plan,
                    "summary": summary,
                }
            )

    except Exception as exc:
        logger.exception("section_plan_generate error")
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


# ── Courses list API (for advanced per-course settings) ───────


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def section_plan_courses_view(request: HttpRequest) -> JsonResponse:
    """Return all courses with their computed default capacities.

    Query params (optional): max_local_4cr, max_local_other, max_external
    — so the list reflects the current global settings.
    """
    try:
        max_local_4cr = int(request.GET.get("max_local_4cr", DEFAULT_MAX_LOCAL_4CR))
        max_local_other = int(request.GET.get("max_local_other", DEFAULT_MAX_LOCAL_OTHER))
        max_external = int(request.GET.get("max_external", DEFAULT_MAX_EXTERNAL))
    except (ValueError, TypeError):
        max_local_4cr = DEFAULT_MAX_LOCAL_4CR
        max_local_other = DEFAULT_MAX_LOCAL_OTHER
        max_external = DEFAULT_MAX_EXTERNAL

    program_raw = request.GET.get("program", "").strip()
    if program_raw and "," in program_raw:
        program: str | list[str] | None = [p.strip() for p in program_raw.split(",") if p.strip()]
    else:
        program = program_raw or None
    courses = get_all_courses_with_defaults(
        max_local_4cr, max_local_other, max_external, program=program
    )
    return JsonResponse({"ok": True, "courses": courses})


# ── Save per-course capacity API ──────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def section_plan_save_capacity_view(request: HttpRequest) -> JsonResponse:
    """Persist a per-course max_capacity override to ProgrammeRequirement rows.

    Accepts JSON body:
        {
            "programs": ["AI", "DS"],
            "course_code": "CS211",
            "max_capacity": 30        // or null to clear
        }

    Updates ProgrammeRequirement.max_capacity for every row matching
    (program IN programs) AND (course_code = course_code).
    Returns {"ok": True, "updated": <count>}.
    """
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    # ── Validate programs ──
    programs = body.get("programs")
    if not programs or not isinstance(programs, list):
        return JsonResponse(
            {"ok": False, "error": "'programs' must be a non-empty list of program codes"},
            status=400,
        )
    programs = [str(p).strip() for p in programs if str(p).strip()]
    if not programs:
        return JsonResponse(
            {"ok": False, "error": "'programs' must be a non-empty list of program codes"},
            status=400,
        )

    # ── Validate course_code ──
    course_code = str(body.get("course_code", "")).strip()
    if not course_code:
        return JsonResponse(
            {"ok": False, "error": "'course_code' is required"},
            status=400,
        )
    course_code = normalize_code(course_code)

    # ── Validate max_capacity ──
    raw_cap = body.get("max_capacity")
    if raw_cap is None or raw_cap == "" or raw_cap == "null":
        max_capacity = None
    else:
        try:
            max_capacity = int(raw_cap)
            if max_capacity < 1:
                return JsonResponse(
                    {"ok": False, "error": "'max_capacity' must be a positive integer or null"},
                    status=400,
                )
            max_capacity = min(max_capacity, 500)  # reasonable upper bound
        except (ValueError, TypeError):
            return JsonResponse(
                {"ok": False, "error": "'max_capacity' must be a positive integer or null"},
                status=400,
            )

    # ── Perform update ──
    updated = ProgrammeRequirement.objects.filter(
        program__in=programs,
        course_code=course_code,
    ).update(max_capacity=max_capacity)

    logger.info(
        "save_capacity: user=%s programs=%s course=%s max_capacity=%s updated=%d",
        request.user.username,
        programs,
        course_code,
        max_capacity,
        updated,
    )

    return JsonResponse({"ok": True, "updated": updated})


# ── Export XLSX API ────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
@throttle(max_calls=5, window_seconds=120)
def section_plan_export_view(request: HttpRequest) -> HttpResponseBase:
    """Generate and download section plan as styled XLSX."""
    params, err = _parse_payload(request)
    if err:
        return err
    assert params is not None

    program = params["program"]
    capacity_kwargs = {
        "max_local_4cr": params["max_local_4cr"],
        "max_local_other": params["max_local_other"],
        "max_external": params["max_external"],
        "course_overrides": params.get("course_overrides"),
    }

    dept_prefixes = params.get("dept_filter", [])

    def _filter_plan(plan: list[dict]) -> list[dict]:
        if not dept_prefixes:
            return plan
        return [
            entry
            for entry in plan
            if any(normalize_code(entry["course_code"]).startswith(p) for p in dept_prefixes)
        ]

    try:
        if isinstance(program, list):
            # ── Multi-program export ──
            programs_data: list[dict] = []
            for prog in program:
                _sc, agg = build_aggregate_counts(
                    params["year"],
                    params["semester"],
                    program=prog,
                    section=params["section"],
                )
                pr_caps = load_programme_capacities(prog, list(agg.keys()))
                plan = compute_section_plan(
                    agg,
                    **capacity_kwargs,
                    programme_capacities=pr_caps,
                )
                summary = compute_plan_summary(plan)
                programs_data.append(
                    {
                        "program": prog,
                        "plan": _filter_plan(plan),
                        "summary": compute_plan_summary(_filter_plan(plan)),
                    }
                )
            path = _export_section_plan_xlsx(
                params=params,
                mode="multi",
                programs_data=programs_data,
            )
            filename = f"section_plan_{params['year']}_{params['semester']}_multi.xlsx"

        elif isinstance(program, str):
            # ── Single-program export ──
            _sc, aggregate = build_aggregate_counts(
                params["year"],
                params["semester"],
                program=program,
                section=params["section"],
            )
            pr_caps = load_programme_capacities(program, list(aggregate.keys()))
            plan = compute_section_plan(
                aggregate,
                **capacity_kwargs,
                programme_capacities=pr_caps,
            )
            plan = _filter_plan(plan)
            summary = compute_plan_summary(plan)
            path = _export_section_plan_xlsx(plan, summary, params, mode="single")
            filename = f"section_plan_{params['year']}_{params['semester']}_{program}.xlsx"

        else:
            # ── Combined export (no program filter) ──
            _sc, aggregate = build_aggregate_counts(
                params["year"],
                params["semester"],
                program=None,
                section=params["section"],
            )
            plan = _filter_plan(compute_section_plan(aggregate, **capacity_kwargs))
            summary = compute_plan_summary(plan)
            path = _export_section_plan_xlsx(plan, summary, params, mode="combined")
            filename = f"section_plan_{params['year']}_{params['semester']}.xlsx"

        return FileResponse(
            path.open("rb"),
            as_attachment=True,
            filename=filename,
        )

    except Exception as exc:
        logger.exception("section_plan_export error")
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


def _export_section_plan_xlsx(
    plan: list[dict] | None = None,
    summary: dict | None = None,
    params: dict | None = None,
    *,
    mode: str = "single",
    programs_data: list[dict] | None = None,
) -> Path:
    """Build a styled XLSX workbook with Sections + Summary sheets.

    For mode="single" or "combined": uses plan/summary directly (one pair of sheets).
    For mode="multi": iterates programs_data, creating per-program sheet pairs.
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

    wb = Workbook()

    headers = [
        "#",
        "Department",
        "Course",
        "Credits",
        "External",
        "Students",
        "Sections",
        "Max/Section",
        "Avg/Section",
        "Fill %",
        "Status",
    ]

    # ── Styles ──
    thin = Side(style="thin", color="D5D8DC")
    cell_border = Border(top=thin, bottom=thin, left=thin, right=thin)
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    title_fill = PatternFill(start_color="1B2631", end_color="1B2631", fill_type="solid")
    title_font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    center = Alignment(horizontal="center", vertical="center")
    left_center = Alignment(horizontal="left", vertical="center")
    alt_fill = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")
    full_fill = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")
    under_fill = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")
    ext_fill = PatternFill(start_color="EBF5FB", end_color="EBF5FB", fill_type="solid")
    bold = Font(bold=True)
    mono = Font(name="Consolas", bold=True, size=10)
    dept_font = Font(name="Calibri", bold=True, size=10, color="2E4053")
    num_font = Font(name="Consolas", size=10)
    status_full_font = Font(bold=True, color="1E8449")
    status_under_font = Font(bold=True, color="C0392B")

    # Department colour map (hash-based pastel, same idea as exam export)
    _dept_colors: dict[str, PatternFill] = {}

    def _dept_fill(dept: str) -> PatternFill:
        if dept in _dept_colors:
            return _dept_colors[dept]
        import colorsys

        h = 0
        for ch in str(dept):
            h = (h * 31 + ord(ch)) % 360
        r, g, b = colorsys.hls_to_rgb(h / 360.0, 0.94, 0.50)
        hex_c = f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"
        fill = PatternFill("solid", fgColor=hex_c)
        _dept_colors[dept] = fill
        return fill

    col_widths = [5, 12, 14, 8, 9, 10, 10, 12, 12, 9, 12]

    def _write_sections_sheet(ws, plan_data: list[dict]) -> None:
        """Write the sections data rows with styled formatting."""
        # Title row
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        tc = ws.cell(row=1, column=1, value="Section Planning")
        tc.font = title_font
        tc.fill = title_fill
        tc.alignment = Alignment(horizontal="center", vertical="center")
        for c in range(2, len(headers) + 1):
            ws.cell(row=1, column=c).fill = title_fill

        # Header row
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=2, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = cell_border

        # Data rows
        for i, row in enumerate(plan_data, 1):
            r = i + 2
            is_alt = i % 2 == 0
            is_ext = row.get("is_external", False)
            dept = row.get("department", "")

            # Row number
            c = ws.cell(row=r, column=1, value=i)
            c.alignment = center
            c.font = num_font
            c.border = cell_border

            # Department — with colour band
            c = ws.cell(row=r, column=2, value=dept)
            c.font = dept_font
            c.alignment = left_center
            c.border = cell_border
            c.fill = _dept_fill(dept)

            # Course code
            c = ws.cell(row=r, column=3, value=row["course_code"])
            c.font = mono
            c.alignment = left_center
            c.border = cell_border

            # Credits
            c = ws.cell(row=r, column=4, value=row["credit_hours"])
            c.alignment = center
            c.font = num_font
            c.border = cell_border

            # External
            c = ws.cell(row=r, column=5, value="Yes" if is_ext else "")
            c.alignment = center
            c.border = cell_border
            if is_ext:
                c.font = Font(color="2980B9", bold=True)

            # Students
            c = ws.cell(row=r, column=6, value=row["total_students"])
            c.alignment = center
            c.font = Font(bold=True, size=10)
            c.border = cell_border

            # Sections
            c = ws.cell(row=r, column=7, value=row["num_sections"])
            c.alignment = center
            c.font = num_font
            c.border = cell_border

            # Max/Section
            c = ws.cell(row=r, column=8, value=row["max_per_section"])
            c.alignment = center
            c.font = num_font
            c.border = cell_border

            # Avg/Section
            c = ws.cell(row=r, column=9, value=row["avg_per_section"])
            c.alignment = center
            c.font = num_font
            c.border = cell_border

            # Fill %
            fill_pct = row.get("fill_percent", 0)
            c = ws.cell(row=r, column=10, value=f"{fill_pct}%")
            c.alignment = center
            c.border = cell_border
            if fill_pct >= 90:
                c.font = Font(bold=True, color="1E8449")
            elif fill_pct < 50:
                c.font = Font(bold=True, color="C0392B")
            else:
                c.font = num_font

            # Status
            status = row.get("status", "")
            c = ws.cell(row=r, column=11, value=status.title() if status else "")
            c.alignment = center
            c.border = cell_border
            if status == "full":
                c.fill = full_fill
                c.font = status_full_font
            elif status == "underfilled":
                c.fill = under_fill
                c.font = status_under_font

            # Row fill: external gets blue tint, alternating gets grey
            row_fill = ext_fill if is_ext else (alt_fill if is_alt else None)
            if row_fill:
                for col in range(1, len(headers) + 1):
                    cell = ws.cell(row=r, column=col)
                    # Don't override dept colour (col 2) or status colour (col 11)
                    if (
                        col not in (2, 11)
                        and not cell.fill.fgColor
                        or cell.fill.fgColor.rgb == "00000000"
                    ):
                        cell.fill = row_fill

        # Column widths
        for ci, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        # Freeze panes below headers
        ws.freeze_panes = "A3"

    from openpyxl.styles import Border, Side  # type: ignore[import-untyped]

    thin_border = Border(
        left=Side(style="thin", color="D5D8DC"),
        right=Side(style="thin", color="D5D8DC"),
        top=Side(style="thin", color="D5D8DC"),
        bottom=Side(style="thin", color="D5D8DC"),
    )
    title_fill = PatternFill(start_color="1B2631", end_color="1B2631", fill_type="solid")
    title_font = Font(bold=True, color="FFFFFF", size=12)
    alt_fill = PatternFill(start_color="F4F6F7", end_color="F4F6F7", fill_type="solid")

    def _write_summary_sheet(ws, summary_data: dict, p: dict) -> None:
        """Write summary metadata and department breakdown into a worksheet."""
        # Title row
        ws.merge_cells("A1:F1")
        title_val = f"Section Plan Summary — {p.get('year', '')}/{p.get('semester', '')}"
        dept_filter = p.get("dept_filter", [])
        if dept_filter:
            title_val += f" (filtered: {', '.join(dept_filter)})"
        tc = ws.cell(row=1, column=1, value=title_val)
        tc.font = title_font
        tc.fill = title_fill
        tc.alignment = Alignment(horizontal="center", vertical="center")
        for c in range(2, 7):
            ws.cell(row=1, column=c).fill = title_fill

        # KPI row
        kpi_labels = ["Total Courses", "Total Sections", "Total Students", "Avg Fill %"]
        kpi_values = [
            summary_data.get("total_courses", 0),
            summary_data.get("total_sections", 0),
            summary_data.get("total_students", 0),
            f"{summary_data.get('avg_fill_percent', 0)}%",
        ]
        for ci, label in enumerate(kpi_labels):
            ws.cell(row=3, column=1 + ci * 2, value=label).font = bold
            ws.cell(row=3, column=2 + ci * 2, value=kpi_values[ci])

        # Department Summary table
        ws.cell(row=5, column=1, value="Department Summary").font = Font(bold=True, size=11)
        dept_headers = ["Department", "Courses", "Sections", "Students", "Total Credits"]
        for col_idx, h in enumerate(dept_headers, 1):
            cell = ws.cell(row=6, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin_border

        depts = summary_data.get("departments", [])
        for i, dept in enumerate(depts):
            r = 7 + i
            ws.cell(row=r, column=1, value=dept["department"]).font = bold
            ws.cell(row=r, column=1).border = thin_border
            ws.cell(row=r, column=2, value=dept["courses"]).alignment = center
            ws.cell(row=r, column=2).border = thin_border
            ws.cell(row=r, column=3, value=dept["sections"]).alignment = center
            ws.cell(row=r, column=3).border = thin_border
            ws.cell(row=r, column=4, value=dept["students"]).alignment = center
            ws.cell(row=r, column=4).border = thin_border
            ws.cell(row=r, column=5, value=dept["total_credits"]).alignment = center
            ws.cell(row=r, column=5).border = thin_border
            if i % 2 == 1:
                for c in range(1, 6):
                    ws.cell(row=r, column=c).fill = alt_fill

        # Totals row
        if depts:
            tr = 7 + len(depts)
            ws.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True, size=10)
            ws.cell(row=tr, column=1).border = thin_border
            ws.cell(row=tr, column=2, value=sum(d["courses"] for d in depts)).font = bold
            ws.cell(row=tr, column=2).alignment = center
            ws.cell(row=tr, column=2).border = thin_border
            ws.cell(row=tr, column=3, value=sum(d["sections"] for d in depts)).font = bold
            ws.cell(row=tr, column=3).alignment = center
            ws.cell(row=tr, column=3).border = thin_border
            ws.cell(row=tr, column=4, value=sum(d["students"] for d in depts)).font = bold
            ws.cell(row=tr, column=4).alignment = center
            ws.cell(row=tr, column=4).border = thin_border
            ws.cell(row=tr, column=5, value=sum(d["total_credits"] for d in depts)).font = bold
            ws.cell(row=tr, column=5).alignment = center
            ws.cell(row=tr, column=5).border = thin_border

        for col_idx in range(1, 7):
            ws.column_dimensions[chr(64 + col_idx)].width = 18

    if mode == "multi" and programs_data:
        # ── Combined sheet first (all programs merged) ──
        combined_plan: list[dict] = []
        for prog_entry in programs_data:
            combined_plan.extend(prog_entry["plan"])
        combined_plan.sort(key=lambda r: (r.get("department", ""), r.get("course_code", "")))
        combined_summary = compute_plan_summary(combined_plan)

        default_sheet = wb.active
        default_sheet.title = "Sections-All"
        _write_sections_sheet(default_sheet, combined_plan)
        sum_all_ws = wb.create_sheet("Summary-All")
        _write_summary_sheet(sum_all_ws, combined_summary, params or {})

        # ── Per-program sheet pairs ──
        for prog_entry in programs_data:
            prog_name = prog_entry["program"]
            sec_ws = wb.create_sheet(f"Sections-{prog_name}")
            _write_sections_sheet(sec_ws, prog_entry["plan"])
            sum_ws = wb.create_sheet(f"Summary-{prog_name}")
            _write_summary_sheet(sum_ws, prog_entry["summary"], params or {})
    else:
        # Single or combined — keep existing behaviour exactly
        ws = wb.active
        ws.title = "Sections"
        _write_sections_sheet(ws, plan or [])
        ws2 = wb.create_sheet("Summary")
        _write_summary_sheet(ws2, summary or {}, params or {})

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
