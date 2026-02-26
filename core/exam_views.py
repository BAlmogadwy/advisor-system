"""
core/exam_views.py
Exam Timetable Builder — page view + API endpoints.

All endpoints require SUPER_ADMIN role (enforced by ``_require_super_admin``).

Endpoints:
    GET  exam_timetable_page          – render the single-page builder UI
    GET  exam_timetable_filters_view  – return programs/sections for filter dropdowns
    POST exam_timetable_preview_courses_view – return running courses matching filters
    POST exam_timetable_build_view    – build (or rebuild) the exam timetable
    GET  exam_timetable_list_view     – paginated list of saved runs
    GET  exam_timetable_detail_view   – load a specific saved run
    GET  exam_timetable_export_view   – download a run as .xlsx
    POST exam_timetable_delete_view   – delete a saved run (requires confirm=DELETE)
"""

from __future__ import annotations

import json

from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.models import ExamTimetableRun, Student
from core.services.audit import log_audit_event
from core.services.exam_timetable import (
    build_credit_map,
    build_enrolled_sets,
    build_exam_timetable,
    export_exam_timetable_xlsx,
)
from core.services.rbac import ROLE_SUPER_ADMIN, get_user_role
from core.sidebar_context import get_sidebar_context


def _require_super_admin(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 JsonResponse if user is not SUPER_ADMIN, else None."""
    if get_user_role(request.user) != ROLE_SUPER_ADMIN:
        return JsonResponse({"error": "SUPER_ADMIN access required"}, status=403)
    return None


@require_GET
def exam_timetable_page(request: HttpRequest) -> HttpResponse:
    """Render the exam timetable builder page (all logic is client-side JS)."""
    deny = _require_super_admin(request)
    if deny:
        return deny
    return render(request, "core/exam_timetable.html", get_sidebar_context(request))


@require_GET
def exam_timetable_filters_view(request: HttpRequest) -> JsonResponse:
    """Return distinct programs and sections for the filter dropdowns."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    programs = sorted(
        p
        for p in Student.objects.exclude(program__isnull=True)
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
        if p is not None
    )
    sections = sorted(
        Student.objects.exclude(section="").values_list("section", flat=True).distinct()
    )
    return JsonResponse({"ok": True, "programs": programs, "sections": sections})


@require_POST
def exam_timetable_preview_courses_view(request: HttpRequest) -> JsonResponse:
    """Return running courses (studying) matching the program/section filters."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    programs_raw = payload.get("programs", [])
    sections_raw = payload.get("sections", [])

    # Normalise filter lists: strip whitespace, drop empties.
    # `or None` converts an empty list to None so build_enrolled_sets
    # treats it as "no filter" (include all students).
    programs = (
        [str(p).strip() for p in programs_raw if str(p).strip()]
        if isinstance(programs_raw, list)
        else []
    ) or None
    sections = (
        [str(s).strip() for s in sections_raw if str(s).strip()]
        if isinstance(sections_raw, list)
        else []
    ) or None

    enrolled_sets = build_enrolled_sets(programs=programs, sections=sections)

    # Fetch credit hours for all preview courses
    credit_map = build_credit_map(list(enrolled_sets.keys()))

    courses = sorted(
        [
            {
                "course_code": cc,
                "enrolled_count": len(sids),
                "credit_hours": credit_map.get(cc, 3),
            }
            for cc, sids in enrolled_sets.items()
        ],
        key=lambda c: str(c["course_code"]),
    )

    return JsonResponse({"ok": True, "courses": courses})


@require_POST
def exam_timetable_build_view(request: HttpRequest) -> JsonResponse:
    """Build (or rebuild) the exam timetable.

    Accepts JSON body with: label, days, periods, max_per_day,
    programs, sections, selected_courses, pinned overrides,
    and optional randomize flag for varied timetable generation.
    """
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    # ── Extract raw values from JSON payload ──
    label = str(payload.get("label", "")).strip()
    days_raw = payload.get("days", [])
    periods_raw = payload.get("periods", [])
    max_per_day_raw = payload.get("max_per_day", 2)
    programs_raw = payload.get("programs", [])
    sections_raw = payload.get("sections", [])
    selected_courses_raw = payload.get("selected_courses", None)
    pinned_raw = payload.get("pinned", None)
    randomize = payload.get("randomize", False)

    if not label:
        return JsonResponse({"ok": False, "error": "label is required"}, status=400)

    # ── Normalise list inputs: strip whitespace, drop empties ──
    days = (
        [str(d).strip() for d in days_raw if str(d).strip()] if isinstance(days_raw, list) else []
    )
    periods = (
        [str(p).strip() for p in periods_raw if str(p).strip()]
        if isinstance(periods_raw, list)
        else []
    )

    # Soft cap on exams per student per day (default 2, minimum 1)
    try:
        max_per_day = int(max_per_day_raw)
        if max_per_day < 1:
            max_per_day = 1
    except (ValueError, TypeError):
        max_per_day = 2

    # `or None` → treat empty list as "no filter" (all students)
    programs = (
        [str(p).strip() for p in programs_raw if str(p).strip()]
        if isinstance(programs_raw, list)
        else []
    ) or None
    sections = (
        [str(s).strip() for s in sections_raw if str(s).strip()]
        if isinstance(sections_raw, list)
        else []
    ) or None

    # User-curated course list from the preview step (None = use all)
    selected_courses = (
        [str(c).strip() for c in selected_courses_raw if str(c).strip()]
        if isinstance(selected_courses_raw, list)
        else None
    )

    # Pinned overrides: courses the user dragged to a specific slot.
    # Each entry must have course_code, day, and period; skip invalid ones.
    pinned: list[dict[str, str]] | None = None
    if isinstance(pinned_raw, list):
        pinned_items: list[dict[str, str]] = []
        for p in pinned_raw:
            if isinstance(p, dict):
                cc = str(p.get("course_code", "")).strip()
                d = str(p.get("day", "")).strip()
                pr = str(p.get("period", "")).strip()
                if cc and d and pr:
                    pinned_items.append({"course_code": cc, "day": d, "period": pr})
        pinned = pinned_items if pinned_items else None

    if not days or not periods:
        return JsonResponse({"ok": False, "error": "days and periods are required"}, status=400)

    # Generate a random seed when the user enables randomised tie-breaking.
    # Each build produces a different timetable variant; the seed is stored
    # in the result so it can be reproduced if needed.
    import random as _rnd

    seed = _rnd.randint(1, 2**31 - 1) if randomize else None

    try:
        result = build_exam_timetable(
            label,
            days,
            periods,
            max_per_day=max_per_day,
            programs=programs,
            sections=sections,
            selected_courses=selected_courses,
            pinned=pinned,
            seed=seed,
        )
        # Check for feasibility error (bucket too large for available days)
        if result.get("feasibility_error"):
            return JsonResponse({"ok": False, **result}, status=400)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@require_GET
def exam_timetable_list_view(request: HttpRequest) -> JsonResponse:
    """Return a paginated list of saved exam-timetable runs (newest first)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    PAGE_SIZE = 10
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    qs = ExamTimetableRun.objects.order_by("-created_at").values("id", "label", "created_at")
    total = qs.count()
    total_pages = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = min(page, total_pages)

    start = (page - 1) * PAGE_SIZE
    runs = list(qs[start : start + PAGE_SIZE])
    for r in runs:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else ""  # type: ignore[typeddict-item]

    return JsonResponse(
        {
            "ok": True,
            "runs": runs,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        }
    )


@require_GET
def exam_timetable_detail_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Load a previously saved exam-timetable run by ID (for the history panel)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        run = ExamTimetableRun.objects.get(id=run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)

    result = json.loads(run.result_json)
    result["run_id"] = run.id
    result["label"] = run.label
    result["created_at"] = run.created_at.isoformat() if run.created_at else ""

    return JsonResponse({"ok": True, **result})


@require_GET
def exam_timetable_export_view(request: HttpRequest, run_id: int) -> HttpResponseBase:
    """Download the exam timetable for a saved run as a styled .xlsx workbook."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        path = export_exam_timetable_xlsx(run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=f"exam_timetable_{run_id}.xlsx",
    )


@require_POST
def exam_timetable_delete_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Delete a saved exam-timetable run (requires confirm=DELETE)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="exam_timetable.delete_run",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse(
            {"ok": False, "error": "Confirmation required: send confirm=DELETE"},
            status=400,
        )

    try:
        run = ExamTimetableRun.objects.get(id=run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)

    label = run.label
    run.delete()

    log_audit_event(
        request,
        action="exam_timetable.delete_run",
        status="success",
        details={"run_id": run_id, "label": label},
    )
    return JsonResponse({"ok": True})
