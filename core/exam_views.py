"""
core/exam_views.py
Exam Timetable Builder — page view + API endpoints.
"""

from __future__ import annotations

import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.models import ExamTimetableRun, Student
from core.services.exam_timetable import build_enrolled_sets, build_exam_timetable
from core.services.rbac import ROLE_SUPER_ADMIN, get_user_role
from core.sidebar_context import get_sidebar_context


def _require_super_admin(request: HttpRequest) -> JsonResponse | None:
    if get_user_role(request.user) != ROLE_SUPER_ADMIN:
        return JsonResponse({"error": "SUPER_ADMIN access required"}, status=403)
    return None


@require_GET
def exam_timetable_page(request: HttpRequest) -> HttpResponse:
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
        Student.objects.exclude(program__isnull=True)
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
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

    courses = sorted(
        [{"course_code": cc, "enrolled_count": len(sids)} for cc, sids in enrolled_sets.items()],
        key=lambda c: c["course_code"],
    )

    return JsonResponse({"ok": True, "courses": courses})


@require_POST
def exam_timetable_build_view(request: HttpRequest) -> JsonResponse:
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    label = str(payload.get("label", "")).strip()
    days_raw = payload.get("days", [])
    periods_raw = payload.get("periods", [])
    max_per_day_raw = payload.get("max_per_day", 2)
    programs_raw = payload.get("programs", [])
    sections_raw = payload.get("sections", [])
    selected_courses_raw = payload.get("selected_courses", None)

    if not label:
        return JsonResponse({"ok": False, "error": "label is required"}, status=400)

    days = (
        [str(d).strip() for d in days_raw if str(d).strip()] if isinstance(days_raw, list) else []
    )
    periods = (
        [str(p).strip() for p in periods_raw if str(p).strip()]
        if isinstance(periods_raw, list)
        else []
    )

    try:
        max_per_day = int(max_per_day_raw)
        if max_per_day < 1:
            max_per_day = 1
    except (ValueError, TypeError):
        max_per_day = 2

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

    selected_courses = (
        [str(c).strip() for c in selected_courses_raw if str(c).strip()]
        if isinstance(selected_courses_raw, list)
        else None
    )

    if not days or not periods:
        return JsonResponse({"ok": False, "error": "days and periods are required"}, status=400)

    try:
        result = build_exam_timetable(
            label,
            days,
            periods,
            max_per_day=max_per_day,
            programs=programs,
            sections=sections,
            selected_courses=selected_courses,
        )
        # Check for feasibility error (bucket too large for available days)
        if result.get("feasibility_error"):
            return JsonResponse({"ok": False, **result}, status=400)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@require_GET
def exam_timetable_list_view(request: HttpRequest) -> JsonResponse:
    deny = _require_super_admin(request)
    if deny:
        return deny

    runs = list(
        ExamTimetableRun.objects.order_by("-created_at").values("id", "label", "created_at")[:20]
    )
    # Convert datetime to string for JSON serialisation
    for r in runs:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else ""

    return JsonResponse({"ok": True, "runs": runs})


@require_GET
def exam_timetable_detail_view(request: HttpRequest, run_id: int) -> JsonResponse:
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
