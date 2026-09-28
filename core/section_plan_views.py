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
from core.services.audit import AuditUnavailable, audit_actor, record_audit_event
from core.services.rbac import ROLE_GENERAL_ADVISOR, get_user_role
from core.services.section_limits import AUDIT_ACTION as LIMIT_AUDIT_ACTION
from core.services.section_limits import (
    LimitConflict,
    LimitRequestError,
    PlannedWrite,
    apply_limit_writes,
    parse_limit_request,
    plan_limit_writes,
    preview_token,
)
from core.services.section_plan_pipeline import (
    SizingRules,
    plan_all_programmes,
    plan_programme,
    plan_programmes,
    summarise,
)
from core.services.section_planning import (
    DEFAULT_MAX_EXTERNAL,
    DEFAULT_MAX_LOCAL_4CR,
    DEFAULT_MAX_LOCAL_OTHER,
    LOCAL_DEPARTMENTS,
    get_all_courses_with_defaults,
)
from core.services.student_helpers import normalize_code
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


def _format_export_course_name(row: dict, course_names: dict[str, str]) -> str:
    """Return the display name written into the XLSX export."""
    code = normalize_code(str(row.get("course_code", "")))
    course_name = str(row.get("course_name") or course_names.get(code, "") or "")
    programs = [str(program) for program in row.get("programs", []) if str(program).strip()]
    if programs:
        program_label = ", ".join(programs)
        return f"{program_label} - {course_name}" if course_name else program_label
    return course_name


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
        return None, JsonResponse(
            {"ok": False, "code": "invalid_json", "error": "Invalid JSON"}, status=400
        )

    try:
        year = int(body.get("year", 0))
        semester = int(body.get("semester", 0))
    except (ValueError, TypeError):
        return None, JsonResponse(
            {"ok": False, "code": "invalid_term", "error": "year and semester must be integers"},
            status=400,
        )

    if not (1400 <= year <= 1600):
        return None, JsonResponse(
            {"ok": False, "code": "invalid_term", "error": "year must be between 1400 and 1600"},
            status=400,
        )
    if semester not in (1, 2, 3):
        return None, JsonResponse(
            {"ok": False, "code": "invalid_term", "error": "semester must be 1, 2, or 3"},
            status=400,
        )

    # Support comma-separated programs  e.g. "AI,DS" → ["AI", "DS"]
    program_raw = str(body.get("program", "")).strip()
    if program_raw and "," in program_raw:
        # Each programme once: a repeated code would count its students twice.
        listed = list(dict.fromkeys(p.strip() for p in program_raw.split(",") if p.strip()))
        program: str | list[str] | None = (
            listed if len(listed) > 1 else (listed[0] if listed else None)
        )
    else:
        program = program_raw or None

    try:
        max_local_4cr = int(body.get("max_local_4cr", DEFAULT_MAX_LOCAL_4CR))
        max_local_other = int(body.get("max_local_other", DEFAULT_MAX_LOCAL_OTHER))
        max_external = int(body.get("max_external", DEFAULT_MAX_EXTERNAL))
    except (ValueError, TypeError):
        return None, JsonResponse(
            {"ok": False, "code": "invalid_capacity", "error": "Capacity limits must be integers"},
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
        "local_departments": sorted(LOCAL_DEPARTMENTS),
    }
    return render(request, "core/section_planning.html", ctx)


# ── Generate API ───────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
@throttle(max_calls=3, window_seconds=120)
def section_plan_generate_view(request: HttpRequest) -> JsonResponse:
    """Compute section demand from batch recommendations.

    Male and female students are always planned apart: every row carries M, F and
    Total (= M + F) sections, and students with no recorded gender are reported
    under ``summary.no_gender`` instead of being pooled into either cohort.
    """
    params, err = _parse_payload(request)
    if err:
        return err
    assert params is not None

    try:
        body = _plan(params)
    except Exception:
        logger.exception("section_plan_generate error")
        return JsonResponse(
            {"ok": False, "code": "generate_failed", "error": "The plan could not be computed."},
            status=500,
        )
    return JsonResponse(
        {"ok": True, "year": params["year"], "semester": params["semester"], **body}
    )


def _rules(params: dict) -> SizingRules:
    return SizingRules(
        max_local_4cr=params["max_local_4cr"],
        max_local_other=params["max_local_other"],
        max_external=params["max_external"],
        course_overrides=params.get("course_overrides") or {},
    )


def _plan(params: dict) -> dict:
    """The plan for the request's scope: one programme, several, or all of them."""
    program = params["program"]
    rules = _rules(params)
    if isinstance(program, list):
        return {
            "mode": "multi",
            **plan_programmes(params["year"], params["semester"], program, rules),
        }
    if isinstance(program, str):
        return {
            "mode": "single",
            **plan_programme(params["year"], params["semester"], program, rules),
        }
    return {"mode": "combined", **plan_all_programmes(params["year"], params["semester"], rules)}


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
    try:
        year: int | None = int(request.GET.get("year", ""))
        semester: int | None = int(request.GET.get("semester", ""))
    except (ValueError, TypeError):
        year = semester = None
    courses = get_all_courses_with_defaults(
        max_local_4cr,
        max_local_other,
        max_external,
        program=program,
        year=year,
        semester=semester,
    )
    return JsonResponse({"ok": True, "courses": courses})


# ── Save per-course seat limits (Section Planning's write path) ─


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
@throttle(max_calls=20, window_seconds=120)
def section_plan_save_limits_view(request: HttpRequest) -> JsonResponse:
    """Preview, then save, per-course seat limits for the programmes on screen.

    JSON body::

        {
          "programs": ["AI"],                       # the programmes on screen
          "changes": [
            {"course_code": "AI491", "max_capacity": 6},
            {"course_code": "CS211", "max_capacity": 30, "all_programmes": true},
            {"course_code": "CS323", "max_capacity": null}     # remove the saved limit
          ],
          "dry_run": true,                          # preview: nothing is written
          "preview_token": "…"                      # commit: the token of that preview
        }

    A preview answers the exact rows that would change (course, programme,
    old -> new, scope). A commit is refused (409 ``preview_stale``) unless its
    token matches what the same request would change now, so the user always
    confirmed exactly what is written. Every changed row is audited in the same
    transaction; if the audit write fails nothing is saved (503).
    """
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"ok": False, "code": "invalid_json", "error": "Invalid JSON"}, status=400
        )

    try:
        programs, changes = parse_limit_request(body)
        writes, unchanged = plan_limit_writes(programs, changes)
    except LimitRequestError as exc:
        return JsonResponse(exc.as_dict(), status=exc.status)

    token = preview_token(programs, writes)
    preview = {
        "programs": programs,
        "changes": [write.as_dict() for write in writes],
        "unchanged": unchanged,
        "preview_token": token,
    }
    if body.get("dry_run") is True:
        return JsonResponse({"ok": True, "dry_run": True, **preview})

    if body.get("preview_token") != token:
        return JsonResponse(
            {
                "ok": False,
                "code": "preview_stale",
                "error": "The saved limits changed since they were reviewed. Review them again.",
                **preview,
            },
            status=409,
        )

    actor, actor_role = audit_actor(request)

    def _audit(write: PlannedWrite, position: int, total: int) -> None:
        record_audit_event(
            actor_username=actor,
            actor_role=actor_role,
            action=LIMIT_AUDIT_ACTION,
            endpoint=request.path,
            method=str(request.method),
            status="success",
            details={
                **write.as_dict(),
                "requirement_id": write.requirement_id,
                "programs_on_screen": programs,
                "batch": token[:16],
                "position": position,
                "of": total,
            },
        )

    try:
        apply_limit_writes(writes, audit=_audit)
    except LimitConflict:
        return JsonResponse(
            {
                "ok": False,
                "code": "limit_changed",
                "error": "A limit changed while saving. Nothing was saved; review again.",
            },
            status=409,
        )
    except AuditUnavailable:
        return JsonResponse(
            {
                "ok": False,
                "code": "audit_unavailable",
                "error": "Couldn't record the change in the audit log, so nothing was saved.",
            },
            status=503,
        )

    logger.info(
        "section_plan_save_limits: user=%s programs=%s changed=%d unchanged=%d batch=%s",
        request.user.username,
        programs,
        len(writes),
        unchanged,
        token[:16],
    )
    return JsonResponse(
        {
            "ok": True,
            "dry_run": False,
            "programs": programs,
            "changed": [write.as_dict() for write in writes],
            "changed_count": len(writes),
            "unchanged": unchanged,
        }
    )


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
    dept_prefixes = params.get("dept_filter", [])

    def _filter_plan(plan: list[dict]) -> list[dict]:
        if not dept_prefixes:
            return plan
        return [
            entry
            for entry in plan
            if any(normalize_code(entry["course_code"]).startswith(p) for p in dept_prefixes)
        ]

    def _scoped(plan: list[dict], cohorts: dict, electives: dict) -> tuple[list[dict], dict]:
        rows = _filter_plan(plan)
        summary = summarise(rows, no_gender_students=int(cohorts.get("no_gender", 0)))
        summary["electives"] = electives
        return rows, summary

    try:
        result = _plan(params)
        if result["mode"] == "multi":
            combined, combined_summary = _scoped(
                result["combined_plan"], result["cohorts"], result["electives"]
            )
            programs_data = []
            for entry in result["programs"]:
                rows, summary = _scoped(entry["plan"], entry["cohorts"], entry["electives"])
                programs_data.append(
                    {"program": entry["program"], "plan": rows, "summary": summary}
                )
            path = _export_section_plan_xlsx(
                combined, combined_summary, params, mode="multi", programs_data=programs_data
            )
            filename = f"section_plan_{params['year']}_{params['semester']}_multi.xlsx"
        else:
            rows, summary = _scoped(result["plan"], result["cohorts"], result["electives"])
            path = _export_section_plan_xlsx(rows, summary, params, mode=result["mode"])
            suffix = f"_{program}" if isinstance(program, str) else ""
            filename = f"section_plan_{params['year']}_{params['semester']}{suffix}.xlsx"

        return FileResponse(
            path.open("rb"),
            as_attachment=True,
            filename=filename,
        )

    except Exception:
        logger.exception("section_plan_export error")
        return JsonResponse(
            {"ok": False, "code": "export_failed", "error": "The file could not be made."},
            status=500,
        )


#: The Sections sheet's columns, in order. Male and female students are sized
#: apart with the same Max/Section; Sections = Male sections + Female sections.
#: Students with no recorded gender are listed in their own column and are in
#: no section count.
EXPORT_HEADERS = [
    "#",
    "Department",
    "Course",
    "Name",
    "Credits",
    "External",
    "Male students",
    "Female students",
    "Students",
    "Max/Section",
    "Male sections",
    "Female sections",
    "Sections",
    "Avg/Section",
    "Fill %",
    "Status",
    "No gender recorded",
    "Limit from",
    "Fills slot",
]
_STATUS_TEXT = {"full": "Full", "underfilled": "Underfilled", "no_gender": "No gender recorded"}


def _limit_source_text(row: dict) -> str:
    """Where a row's Max/Section came from, in words."""
    source = row.get("limit_source") or "rule"
    slots = ", ".join(row.get("slots") or [])
    if source == "slot":
        return f"Slot {slots} limit" if slots else "Slot limit"
    if source == "draft":
        return "Draft (what-if)"
    if source == "programme":
        return "Programme limit"
    return "Rule"


_DROP_REASON_TEXT = {
    "not_published": "not published for this term",
    "invalid_mapping": "mapping is invalid",
    "no_eligible_course": "no course the student is eligible for",
    "no_term_scope": "no term to resolve it in",
}


def _export_section_plan_xlsx(
    plan: list[dict] | None = None,
    summary: dict | None = None,
    params: dict | None = None,
    *,
    mode: str = "single",
    programs_data: list[dict] | None = None,
) -> Path:
    """Build a styled XLSX workbook with Sections + Summary sheets.

    For mode="single" or "combined": one pair of sheets from plan/summary.
    For mode="multi": plan/summary are the pooled plan ("-All" sheets), then one
    pair per programme from programs_data.
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
    headers = EXPORT_HEADERS

    # Build course name lookup from Course + ProgrammeRequirement tables
    from core.models import Course as CourseModel

    _course_names: dict[str, str] = {}
    for code, desc in CourseModel.objects.values_list("course_code", "description"):
        _course_names[normalize_code(code)] = desc or ""
    # Also check ElectiveCourse for elective names
    from core.models import ElectiveCourse as EC

    for code, name in EC.objects.values_list("course_code", "course_name"):
        nc = normalize_code(code)
        if nc not in _course_names or not _course_names[nc]:
            _course_names[nc] = name or ""

    # ── Styles ──
    thin = Side(style="thin", color="D5D8DC")
    cell_border = Border(top=thin, bottom=thin, left=thin, right=thin)
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    title_fill = PatternFill(start_color="1B2631", end_color="1B2631", fill_type="solid")
    title_font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
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

    col_widths = [5, 12, 14, 30, 8, 9, 9, 9, 10, 11, 9, 9, 10, 11, 9, 14, 11, 18, 10]

    def _cell(ws, r: int, c: int, value, *, font=None, align=center):
        cell = ws.cell(row=r, column=c, value=value)
        cell.alignment = align
        cell.border = cell_border
        if font is not None:
            cell.font = font
        return cell

    def _write_sections_sheet(ws, plan_data: list[dict]) -> None:
        """Write the sections data rows with styled formatting."""
        # Title row
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        tc = ws.cell(
            row=1,
            column=1,
            value="Section Planning — male and female sections planned separately",
        )
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

            _cell(ws, r, 1, i, font=num_font)
            c = _cell(ws, r, 2, dept, font=dept_font, align=left_center)
            c.fill = _dept_fill(dept)
            _cell(ws, r, 3, row["course_code"], font=mono, align=left_center)
            _cell(
                ws,
                r,
                4,
                _format_export_course_name(row, _course_names),
                font=Font(size=9, color="566573"),
                align=left_center,
            )
            _cell(ws, r, 5, row["credit_hours"], font=num_font)
            c = _cell(ws, r, 6, "Yes" if is_ext else "")
            if is_ext:
                c.font = Font(color="2980B9", bold=True)
            _cell(ws, r, 7, int(row.get("male_students") or 0), font=num_font)
            _cell(ws, r, 8, int(row.get("female_students") or 0), font=num_font)
            # Students = Male + Female (no-gender students are in neither)
            _cell(ws, r, 9, f"=G{r}+H{r}", font=Font(bold=True, size=10))
            # Max/Section (editable — the section counts follow it)
            _cell(ws, r, 10, row["max_per_section"], font=num_font)
            _cell(ws, r, 11, f"=IF(G{r}>0,CEILING(G{r}/J{r},1),0)", font=num_font)
            _cell(ws, r, 12, f"=IF(H{r}>0,CEILING(H{r}/J{r},1),0)", font=num_font)
            _cell(ws, r, 13, f"=K{r}+L{r}", font=Font(bold=True, size=10))
            _cell(ws, r, 14, f"=IF(M{r}>0,ROUND(I{r}/M{r},0),0)", font=num_font)
            c = _cell(ws, r, 15, f'=IF(AND(J{r}>0,M{r}>0),ROUND(I{r}/(M{r}*J{r})*100,0)&"%","")')
            # Conditional formatting via static check (formulas recalculate in Excel)
            fill_pct = row.get("fill_percent", 0)
            if fill_pct >= 90:
                c.font = Font(bold=True, color="1E8449")
            elif fill_pct < 50:
                c.font = Font(bold=True, color="C0392B")
            else:
                c.font = num_font

            status = row.get("status", "")
            c = _cell(ws, r, 16, _STATUS_TEXT.get(status, ""))
            if status == "full":
                c.fill = full_fill
                c.font = status_full_font
            elif status in ("underfilled", "no_gender"):
                c.fill = under_fill
                c.font = status_under_font
            _cell(ws, r, 17, int(row.get("unknown_students") or 0) or "", font=num_font)
            _cell(ws, r, 18, _limit_source_text(row), align=left_center)
            _cell(ws, r, 19, ", ".join(row.get("slots") or []), font=mono)

            # Row fill: external gets blue tint, alternating gets grey
            row_fill = ext_fill if is_ext else (alt_fill if is_alt else None)
            if row_fill:
                for col in range(1, len(headers) + 1):
                    cell = ws.cell(row=r, column=col)
                    # Don't override dept colour (col 2) or status colour (col 16)
                    if col not in (2, 16):
                        cell.fill = row_fill

        # Column widths
        for ci, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        # Freeze panes below headers
        ws.freeze_panes = "A3"

    thin_border = Border(
        left=Side(style="thin", color="D5D8DC"),
        right=Side(style="thin", color="D5D8DC"),
        top=Side(style="thin", color="D5D8DC"),
        bottom=Side(style="thin", color="D5D8DC"),
    )
    alt_fill = PatternFill(start_color="F4F6F7", end_color="F4F6F7", fill_type="solid")

    def _write_summary_sheet(
        ws, summary_data: dict, p: dict, sec_sheet: str = "Sections", data_rows: int = 0
    ) -> None:
        """Write summary with formulas referencing the Sections sheet."""
        width = 7
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
        title_val = f"Section Plan Summary — {p.get('year', '')}/{p.get('semester', '')}"
        dept_filter = p.get("dept_filter", [])
        if dept_filter:
            title_val += f" (filtered: {', '.join(dept_filter)})"
        tc = ws.cell(row=1, column=1, value=title_val)
        tc.font = title_font
        tc.fill = title_fill
        tc.alignment = Alignment(horizontal="center", vertical="center")
        for c in range(2, width + 1):
            ws.cell(row=1, column=c).fill = title_fill

        # Sections sheet reference (quote if has spaces/hyphens)
        sq = f"'{sec_sheet}'" if "-" in sec_sheet or " " in sec_sheet else sec_sheet
        last_row = 2 + max(data_rows, 1)
        rng = lambda col: f"{sq}!{col}3:{col}{last_row}"  # noqa: E731

        kpi = Font(bold=True, size=11, color="0A8E6E")
        # KPI rows — formulas referencing the Sections sheet
        ws.cell(row=3, column=1, value="Total Courses").font = bold
        ws.cell(row=3, column=2, value=f'=COUNTIF({rng("M")},">0")').font = kpi
        ws.cell(row=3, column=3, value="Total Sections").font = bold
        ws.cell(row=3, column=4, value=f"=SUM({rng('M')})").font = kpi
        ws.cell(row=3, column=5, value="Total Students").font = bold
        ws.cell(row=3, column=6, value=f"=SUM({rng('I')})").font = kpi
        ws.cell(row=4, column=1, value="Male Sections").font = bold
        ws.cell(row=4, column=2, value=f"=SUM({rng('K')})").font = kpi
        ws.cell(row=4, column=3, value="Female Sections").font = bold
        ws.cell(row=4, column=4, value=f"=SUM({rng('L')})").font = kpi
        no_gender = (summary_data.get("no_gender") or {}).get("students", 0)
        ws.cell(row=4, column=5, value="No gender recorded").font = bold
        ws.cell(row=4, column=6, value=int(no_gender or 0)).font = kpi
        ws.cell(
            row=5,
            column=1,
            value=(
                "Male and female students are never planned together: Sections = Male + "
                "Female. Students with no recorded gender are not in any count."
            ),
        ).font = Font(italic=True, size=9, color="566573")
        electives = summary_data.get("electives") or {}
        if electives.get("dropped_total"):
            parts = [
                f"{d['program']} {d['slot']}: {_DROP_REASON_TEXT.get(d['reason'], d['reason'])}"
                f" ({d['students']})"
                for d in electives.get("dropped", [])
            ]
            ws.cell(
                row=6,
                column=1,
                value=(
                    f"Elective-slot demand that became no course: {electives['dropped_total']}"
                    f" — {'; '.join(parts)}"
                ),
            ).font = Font(italic=True, size=9, color="C0392B")

        # Department Summary table
        top = 7
        ws.cell(row=top, column=1, value="Department Summary").font = Font(bold=True, size=11)
        dept_headers = [
            "Department",
            "Courses",
            "Male sections",
            "Female sections",
            "Sections",
            "Students",
            "Total Credits",
        ]
        for col_idx, h in enumerate(dept_headers, 1):
            cell = ws.cell(row=top + 1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin_border

        depts = summary_data.get("departments", [])
        first = top + 2
        for i, dept in enumerate(depts):
            r = first + i
            dept_name = dept["department"]
            is_dept = f'({rng("B")}="{dept_name}")'
            c = ws.cell(row=r, column=1, value=dept_name)
            c.font = bold
            c.border = thin_border
            c.fill = _dept_fill(dept_name)
            formulas = [
                f'=COUNTIFS({rng("B")},"{dept_name}",{rng("M")},">0")',
                f"=SUMPRODUCT({is_dept}*{rng('K')})",
                f"=SUMPRODUCT({is_dept}*{rng('L')})",
                f"=SUMPRODUCT({is_dept}*{rng('M')})",
                f"=SUMPRODUCT({is_dept}*{rng('I')})",
                f"=SUMPRODUCT({is_dept}*{rng('M')}*{rng('E')})",
            ]
            for col, formula in enumerate(formulas, 2):
                c = ws.cell(row=r, column=col, value=formula)
                c.alignment = center
                c.border = thin_border
                if i % 2 == 1:
                    c.fill = alt_fill

        # Totals row — SUM of the formula rows above
        if depts:
            tr = first + len(depts)
            ws.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True, size=10)
            ws.cell(row=tr, column=1).border = thin_border
            for col in range(2, len(dept_headers) + 1):
                letter = get_column_letter(col)
                c = ws.cell(row=tr, column=col, value=f"=SUM({letter}{first}:{letter}{tr - 1})")
                c.font = Font(bold=True, size=10)
                c.alignment = center
                c.border = thin_border

        for col_idx in range(1, width + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 18

    if mode == "multi" and programs_data:
        # ── Pooled sheets first (all programmes, as the builder pools them) ──
        default_sheet = wb.active
        default_sheet.title = "Sections-All"
        _write_sections_sheet(default_sheet, plan or [])
        sum_all_ws = wb.create_sheet("Summary-All")
        _write_summary_sheet(
            sum_all_ws,
            summary or {},
            params or {},
            sec_sheet="Sections-All",
            data_rows=len(plan or []),
        )

        # ── Per-program sheet pairs ──
        for prog_entry in programs_data:
            prog_name = prog_entry["program"]
            sec_name = f"Sections-{prog_name}"
            sec_ws = wb.create_sheet(sec_name)
            _write_sections_sheet(sec_ws, prog_entry["plan"])
            sum_ws = wb.create_sheet(f"Summary-{prog_name}")
            _write_summary_sheet(
                sum_ws,
                prog_entry["summary"],
                params or {},
                sec_sheet=sec_name,
                data_rows=len(prog_entry["plan"]),
            )
    else:
        ws = wb.active
        ws.title = "Sections"
        _write_sections_sheet(ws, plan or [])
        ws2 = wb.create_sheet("Summary")
        _write_summary_sheet(
            ws2,
            summary or {},
            params or {},
            sec_sheet="Sections",
            data_rows=len(plan or []),
        )

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return Path(tmp.name)
