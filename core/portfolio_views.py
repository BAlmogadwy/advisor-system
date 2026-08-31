"""
core/portfolio_views.py
Standalone Advisor Portfolio page view.
"""

import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.models import Student
from core.services.advisor_graduation_optimization import (
    OPTIMIZED_CURRENT_OFFERINGS,
    OptimizedGraduationUnavailable,
    build_optimized_current_offerings_report,
)
from core.services.advisor_presentations import graduation_presentation_from_tool_results
from core.services.graduation_export import build_graduation_xlsx
from core.services.policy import require_student_scope
from core.services.rbac import ROLE_ADVISOR
from core.services.student_graduation import (
    PLANNING_BASELINE_KINDS,
    REGISTERED_TIMETABLE,
    build_graduation_report,
)
from core.services.student_sections import prefer_arabic_course_names_in_payload
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)

ADVISOR_GRADUATION_BASELINE_KINDS = frozenset(
    {*PLANNING_BASELINE_KINDS, OPTIMIZED_CURRENT_OFFERINGS}
)


def _request_prefers_arabic(request: HttpRequest) -> bool:
    return str(getattr(request, "LANGUAGE_CODE", "") or "").lower().startswith("ar")


@role_required(ROLE_ADVISOR)  # ADVISOR, GENERAL_ACADEMIC_ADVISOR, SUPER_ADMIN
@require_GET
def advisor_portfolio_page(request: HttpRequest) -> HttpResponse:
    context = get_sidebar_context(request)
    # ONE condition for the picker, computed here and handed to both the template
    # and (via the template) the JS. The template used to hide the bar on
    # `role == 'ADVISOR'` while the JS decided to skip it on
    # `role === 'ADVISOR' && USER_ADVISOR_ID`. Two conditions, one of them wrong
    # whenever the id was blank, and no way for either to notice.
    context["hide_advisor_picker"] = bool(
        context.get("role") == ROLE_ADVISOR and str(context.get("user_advisor_id") or "").strip()
    )
    return render(request, "core/advisor_portfolio.html", context)


def _advisor_student_graduation_payload(
    request: HttpRequest, student_id: int
) -> tuple[dict | None, dict | None, int]:
    """Build the shared page/API payload without weakening either entry point."""
    requested_baseline = request.GET.get("baseline")
    baseline_kind = (
        REGISTERED_TIMETABLE if requested_baseline is None else str(requested_baseline).strip()
    )
    if baseline_kind not in ADVISOR_GRADUATION_BASELINE_KINDS:
        return (
            None,
            {
                "error": (
                    "baseline must be registered_timetable, recommended_current_term, "
                    "or optimized_current_offerings"
                ),
                "code": "INVALID_GRADUATION_BASELINE",
                "allowed_baselines": sorted(ADVISOR_GRADUATION_BASELINE_KINDS),
            },
            400,
        )

    try:
        defaults = load_defaults()
        academic_year = int(defaults["currentYear"])
        term = int(defaults["currentTerm"])
    except (KeyError, OSError, TypeError, ValueError):
        return (
            None,
            {
                "error": "The global current academic year and term are not configured.",
                "code": "GRADUATION_TERM_NOT_CONFIGURED",
            },
            503,
        )

    # The simulator advances main terms by odd/even plan parity. Treating a
    # summer/third term as either one would produce a confident but false plan.
    if term not in {1, 2}:
        return (
            None,
            {
                "error": "Graduation planning supports global current terms 1 and 2 only.",
                "code": "UNSUPPORTED_GRADUATION_TERM",
                "academic_year": academic_year,
                "term": term,
                "supported_terms": [1, 2],
            },
            400,
        )

    try:
        if baseline_kind == OPTIMIZED_CURRENT_OFFERINGS:
            try:
                section_snapshot_academic_year = int(defaults["academic_year"])
                section_snapshot_term = int(defaults["term"])
            except (KeyError, TypeError, ValueError):
                raise OptimizedGraduationUnavailable(
                    "The recorded section-snapshot term is not configured.",
                    code="SECTION_SNAPSHOT_TERM_MISMATCH",
                ) from None
            report = build_optimized_current_offerings_report(
                student_id,
                academic_year,
                term,
                section_snapshot_academic_year=section_snapshot_academic_year,
                section_snapshot_term=section_snapshot_term,
            )
        else:
            report = build_graduation_report(
                student_id,
                academic_year,
                term,
                planning_baseline_kind=baseline_kind,
            )
        if report and _request_prefers_arabic(request):
            report = prefer_arabic_course_names_in_payload(report)
    except OptimizedGraduationUnavailable as exc:
        return (
            None,
            {
                "error": str(exc),
                "code": exc.code,
                **exc.details,
            },
            exc.status,
        )
    except Exception:  # noqa: BLE001 - API must fail closed without leaking internals
        logger.exception("portfolio graduation report failed for student %s", student_id)
        return (
            None,
            {
                "error": "The graduation plan could not be generated.",
                "code": "GRADUATION_REPORT_FAILED",
            },
            500,
        )

    if not report:
        return (
            None,
            {
                "error": "No graduation-plan data is available for this student.",
                "code": "GRADUATION_REPORT_UNAVAILABLE",
            },
            422,
        )

    presentation = graduation_presentation_from_tool_results(
        [
            {
                **report,
                "tool": "graduation_progress",
                "ok": True,
                "scenario_academic_year": academic_year,
                "scenario_term": term,
            }
        ]
    )
    return (
        {
            "student_id": student_id,
            "academic_year": academic_year,
            "term": term,
            "baseline": baseline_kind,
            # Keep the report intact: the table needs waiting terms and blockers
            # that are deliberately compacted out of the graph presentation.
            "report": report,
            "presentation": presentation,
        },
        None,
        200,
    )


@never_cache
@role_required(ROLE_ADVISOR)  # ADVISOR, GENERAL_ACADEMIC_ADVISOR, SUPER_ADMIN
@require_GET
def advisor_portfolio_student_graduation_page(
    request: HttpRequest, student_id: int
) -> HttpResponse:
    """Render one advisee's graduation forecast as a full, read-only workspace."""
    scope_error = require_student_scope(request, student_id)
    if scope_error:
        return scope_error

    payload, error, status = _advisor_student_graduation_payload(request, student_id)
    requested_baseline = str(request.GET.get("baseline") or REGISTERED_TIMETABLE).strip()
    active_baseline = (
        requested_baseline
        if requested_baseline in ADVISOR_GRADUATION_BASELINE_KINDS
        else REGISTERED_TIMETABLE
    )
    error_payload = error or {}
    context = {
        **get_sidebar_context(request),
        "student": Student.objects.filter(student_id=student_id).first(),
        "student_id": student_id,
        "academic_year": (
            payload.get("academic_year") if payload else error_payload.get("academic_year")
        ),
        "term": payload.get("term") if payload else error_payload.get("term"),
        "baseline_kind": payload.get("baseline") if payload else active_baseline,
        "grad": payload.get("report") if payload else None,
        "graduation_presentation": payload.get("presentation") if payload else {},
        "graduation_error": error,
    }
    return render(
        request,
        "core/advisor_student_graduation.html",
        context,
        status=status,
    )


@never_cache
@role_required(ROLE_ADVISOR)  # ADVISOR, GENERAL_ACADEMIC_ADVISOR, SUPER_ADMIN
@require_GET
def advisor_portfolio_student_graduation_view(
    request: HttpRequest, student_id: int
) -> JsonResponse:
    """Return the same scoped graduation scenario for API consumers."""
    scope_error = require_student_scope(request, student_id)
    if scope_error:
        return scope_error

    payload, error, status = _advisor_student_graduation_payload(request, student_id)
    return JsonResponse(payload if payload is not None else error, status=status)


@never_cache
@role_required(ROLE_ADVISOR)  # ADVISOR, GENERAL_ACADEMIC_ADVISOR, SUPER_ADMIN
@require_GET
def advisor_portfolio_student_graduation_export_xlsx(
    request: HttpRequest, student_id: int
) -> HttpResponse:
    """Download the same scoped graduation scenario shown on the page."""
    scope_error = require_student_scope(request, student_id)
    if scope_error:
        return scope_error

    payload, error, status = _advisor_student_graduation_payload(request, student_id)
    if payload is None:
        return JsonResponse(error, status=status)

    student = Student.objects.filter(student_id=student_id).first()
    if student is None:  # Defensive only; the scope check already returns 404.
        return JsonResponse(
            {"error": "Student not found.", "code": "STUDENT_NOT_FOUND"},
            status=404,
        )

    content = build_graduation_xlsx(
        student=student,
        academic_year=int(payload["academic_year"]),
        term=int(payload["term"]),
        baseline_kind=str(payload["baseline"]),
        report=payload["report"],
        presentation=payload["presentation"],
        language_code=str(getattr(request, "LANGUAGE_CODE", "en") or "en"),
    )
    baseline_slug = {
        REGISTERED_TIMETABLE: "registered",
        "recommended_current_term": "recommended",
        OPTIMIZED_CURRENT_OFFERINGS: "optimized_offerings",
    }[str(payload["baseline"])]
    filename = (
        f"graduation_plan_{student_id}_{baseline_slug}_"
        f"{payload['academic_year']}_T{payload['term']}.xlsx"
    )
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
