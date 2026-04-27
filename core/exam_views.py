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

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import throttle
from core.models import ExamTimetableRun, Student
from core.services.audit import log_audit_event
from core.services.exam_multistart import (
    is_multistart_enabled,
    report_to_dict,
    run_multistart,
)
from core.services.exam_run_schema import load_normalised_run
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
    course_codes = list(enrolled_sets.keys())
    credit_map = build_credit_map(course_codes)

    # Online flag per course: True iff at least one matching ProgrammeRequirement
    # row marks it as is_online, OR the course code is in the GS / GSE general
    # studies family (institutional convention — those are delivered online).
    from core.models import ProgrammeRequirement

    pr_qs = ProgrammeRequirement.objects.filter(course_code__in=course_codes, is_online=True)
    if programs:
        pr_qs = pr_qs.filter(program__in=programs)
    online_set = set(pr_qs.values_list("course_code", flat=True))
    online_set.update(cc for cc in course_codes if cc.startswith(("GS", "GSE")))

    courses = sorted(
        [
            {
                "course_code": cc,
                "enrolled_count": len(sids),
                "credit_hours": credit_map.get(cc, 3),
                "is_online": cc in online_set,
            }
            for cc, sids in enrolled_sets.items()
        ],
        key=lambda c: str(c["course_code"]),
    )

    return JsonResponse({"ok": True, "courses": courses})


# Throttle: looser in development for fast tuning, tighter in production
# to keep this expensive endpoint from being hammered.
_BUILD_MAX_CALLS = 20 if settings.DEBUG else 3


@require_POST
@throttle(max_calls=_BUILD_MAX_CALLS, window_seconds=120)
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
    assign_rooms = bool(payload.get("assign_rooms", True))
    thin_threshold_raw = payload.get("thin_conflict_threshold", 0)

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

    # Thin-conflict threshold: courses with total enrolment <= this value
    # are dropped from the conflict graph. 0 = current behaviour.
    # Clamped to [0, 10] to prevent the registrar from inadvertently
    # ignoring real-sized courses' conflicts. Booleans rejected (Python
    # treats True as int 1, but a JSON `true` here is almost certainly
    # a client bug, not "use threshold 1").
    if isinstance(thin_threshold_raw, bool):
        thin_conflict_threshold = 0
    else:
        try:
            thin_conflict_threshold = max(0, min(10, int(thin_threshold_raw)))
        except (ValueError, TypeError):
            thin_conflict_threshold = 0

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

    # Multi-start is feature-flagged. When TIMETABLE_EXAM_MULTISTART_ENABLED
    # is set and the request opts in (``multistart=True``), the runner
    # explores N seeded builds and returns the 4 mechanically-defined
    # Pareto candidates ("recommended" / "lowest_overflow" /
    # "lowest_overload" / "best_room_feasibility") in a single response.
    # The single-run path remains the default; existing client code is
    # unaffected.
    multistart_requested = bool(payload.get("multistart", False))
    if multistart_requested and is_multistart_enabled():
        # Optional inputs; sensible defaults match the peer-review plan.
        try:
            n_runs = max(1, min(50, int(payload.get("n_runs", 20))))
        except (ValueError, TypeError):
            n_runs = 20
        try:
            time_budget_s = max(0.5, min(60.0, float(payload.get("time_budget_s", 12.0))))
        except (ValueError, TypeError):
            time_budget_s = 12.0
        previous_run_id_raw = payload.get("previous_run_id")
        try:
            previous_run_id = int(previous_run_id_raw) if previous_run_id_raw is not None else None
        except (ValueError, TypeError):
            previous_run_id = None

        try:
            report = run_multistart(
                label=label,
                days=days,
                periods=periods,
                max_per_day=max_per_day,
                programs=programs,
                sections=sections,
                selected_courses=selected_courses,
                pinned=pinned,
                n_runs=n_runs,
                time_budget_s=time_budget_s,
                assign_rooms=assign_rooms,
                thin_conflict_threshold=thin_conflict_threshold,
                previous_run_id=previous_run_id,
            )
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=500)

        if report.feasibility_error is not None and not report.candidates_by_role:
            return JsonResponse(
                {"ok": False, "multistart": report_to_dict(report)},
                status=400,
            )
        return JsonResponse(
            {"ok": True, "mode": "multistart", "multistart": report_to_dict(report)}
        )

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
            assign_rooms=assign_rooms,
            thin_conflict_threshold=thin_conflict_threshold,
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

    # Single read path: the normaliser handles legacy / corrupt /
    # missing-key payloads gracefully (returns ``status="unrenderable"``
    # rather than 500), so the UI receives a render-safe payload for
    # any historic row regardless of when it was stored.
    result = dict(load_normalised_run(run))
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

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
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
