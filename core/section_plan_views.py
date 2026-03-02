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

    return {
        "year": year,
        "semester": semester,
        "program": program,
        "section": section,
        "max_local_4cr": max_local_4cr,
        "max_local_other": max_local_other,
        "max_external": max_external,
        "course_overrides": course_overrides,
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
                        "plan": plan,
                        "summary": summary,
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
            plan = compute_section_plan(aggregate, **capacity_kwargs)
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
    from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]

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
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    center = Alignment(horizontal="center")
    full_fill = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    under_fill = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")
    bold = Font(bold=True)

    def _write_sections_sheet(ws, plan_data: list[dict]) -> None:
        """Write the sections data rows into a worksheet."""
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center

        for i, row in enumerate(plan_data, 1):
            r = i + 1
            ws.cell(row=r, column=1, value=i).alignment = center
            ws.cell(row=r, column=2, value=row["department"])
            ws.cell(row=r, column=3, value=row["course_code"])
            ws.cell(row=r, column=4, value=row["credit_hours"]).alignment = center
            ws.cell(row=r, column=5, value="Yes" if row["is_external"] else "No").alignment = center
            ws.cell(row=r, column=6, value=row["total_students"]).alignment = center
            ws.cell(row=r, column=7, value=row["num_sections"]).alignment = center
            ws.cell(row=r, column=8, value=row["max_per_section"]).alignment = center
            ws.cell(row=r, column=9, value=row["avg_per_section"]).alignment = center
            ws.cell(row=r, column=10, value=f"{row['fill_percent']}%").alignment = center

            status_cell = ws.cell(
                row=r, column=11, value=row["status"].title() if row["status"] else ""
            )
            status_cell.alignment = center
            if row["status"] == "full":
                status_cell.fill = full_fill
                status_cell.font = bold
            elif row["status"] == "underfilled":
                status_cell.fill = under_fill
                status_cell.font = bold

        for col_idx in range(1, len(headers) + 1):
            ws.column_dimensions[chr(64 + col_idx) if col_idx <= 26 else "A"].width = 14

    def _write_summary_sheet(ws, summary_data: dict, p: dict) -> None:
        """Write summary metadata and department breakdown into a worksheet."""
        ws.cell(row=1, column=1, value="Year").font = bold
        ws.cell(row=1, column=2, value=p["year"])
        ws.cell(row=1, column=3, value="Semester").font = bold
        ws.cell(row=1, column=4, value=p["semester"])

        ws.cell(row=3, column=1, value="Total Courses").font = bold
        ws.cell(row=3, column=2, value=summary_data["total_courses"])
        ws.cell(row=3, column=3, value="Total Sections").font = bold
        ws.cell(row=3, column=4, value=summary_data["total_sections"])
        ws.cell(row=3, column=5, value="Total Students").font = bold
        ws.cell(row=3, column=6, value=summary_data["total_students"])

        dept_headers = ["Department", "Courses", "Sections", "Students", "Total Credits"]
        for col_idx, h in enumerate(dept_headers, 1):
            cell = ws.cell(row=5, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center

        for i, dept in enumerate(summary_data["departments"], 6):
            ws.cell(row=i, column=1, value=dept["department"])
            ws.cell(row=i, column=2, value=dept["courses"]).alignment = center
            ws.cell(row=i, column=3, value=dept["sections"]).alignment = center
            ws.cell(row=i, column=4, value=dept["students"]).alignment = center
            ws.cell(row=i, column=5, value=dept["total_credits"]).alignment = center

        for col_idx in range(1, 7):
            ws.column_dimensions[chr(64 + col_idx)].width = 16

    if mode == "multi" and programs_data:
        # Remove the default empty sheet, then create per-program sheet pairs
        default_sheet = wb.active
        for prog_entry in programs_data:
            prog_name = prog_entry["program"]
            sec_ws = wb.create_sheet(f"Sections-{prog_name}")
            _write_sections_sheet(sec_ws, prog_entry["plan"])
            sum_ws = wb.create_sheet(f"Summary-{prog_name}")
            _write_summary_sheet(sum_ws, prog_entry["summary"], params or {})
        wb.remove(default_sheet)
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
