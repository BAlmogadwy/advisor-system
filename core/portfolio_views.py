"""
core/portfolio_views.py
Standalone Advisor Portfolio page view.
"""

import logging
import re
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.models import ProgrammeRequirement, Student
from core.services.advisor_graduation_optimization import (
    OPTIMIZED_CURRENT_OFFERINGS,
    OptimizedGraduationUnavailable,
    build_optimized_current_offerings_report,
)
from core.services.advisor_presentations import graduation_presentation_from_tool_results
from core.services.graduation_export import build_graduation_xlsx
from core.services.policy import require_student_scope
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN
from core.services.student_graduation import (
    PLANNING_BASELINE_KINDS,
    REGISTERED_TIMETABLE,
    build_graduation_must_have_scenario,
    build_graduation_report,
)
from core.services.student_helpers import is_elective_slot, normalize_code
from core.services.student_sections import prefer_arabic_course_names_in_payload
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)

ADVISOR_GRADUATION_BASELINE_KINDS = frozenset(
    {*PLANNING_BASELINE_KINDS, OPTIMIZED_CURRENT_OFFERINGS}
)
MAX_STUDENT_ID = 2_147_483_647
MAX_ADMIN_MUST_HAVE_COURSES = 10
MAX_ADMIN_COURSE_CODE_LENGTH = 32
_ADMIN_COURSE_CODE_RE = re.compile(r"^[A-Z0-9_-]+$")


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


def _graduation_workspace_context(
    request: HttpRequest,
    *,
    student: Student,
    student_id: int,
    payload: dict | None,
    error: dict | None,
    requested_baseline: str,
    admin_mode: bool,
) -> dict:
    """Build one template contract for portfolio and admin graduation workspaces."""
    active_baseline = (
        requested_baseline
        if requested_baseline in ADVISOR_GRADUATION_BASELINE_KINDS
        else REGISTERED_TIMETABLE
    )
    error_payload = error or {}
    if admin_mode:
        page_url = reverse("admin_graduation_student_page", kwargs={"student_id": student_id})
        export_url = reverse(
            "admin_graduation_student_export_xlsx",
            kwargs={"student_id": student_id},
        )
        back_url = reverse("admin_graduation_planning_page")
    else:
        page_url = reverse(
            "advisor_portfolio_student_graduation_page",
            kwargs={"student_id": student_id},
        )
        export_url = reverse(
            "advisor_portfolio_student_graduation_export_xlsx",
            kwargs={"student_id": student_id},
        )
        back_url = reverse("advisor_portfolio_page")

    navigation = _graduation_navigation_context(
        page_url=page_url,
        export_url=export_url,
        baseline_kind=active_baseline,
    )

    return {
        **get_sidebar_context(request),
        "student": student,
        "student_id": student_id,
        "student_id_query": str(student_id),
        "academic_year": (
            payload.get("academic_year") if payload else error_payload.get("academic_year")
        ),
        "term": payload.get("term") if payload else error_payload.get("term"),
        "baseline_kind": payload.get("baseline") if payload else active_baseline,
        "grad": payload.get("report") if payload else None,
        "graduation_presentation": payload.get("presentation") if payload else {},
        "graduation_error": error,
        "graduation_admin_mode": admin_mode,
        "graduation_page_url": page_url,
        "graduation_export_url": export_url,
        "graduation_back_url": back_url,
        "graduation_search_url": reverse("admin_graduation_planning_page"),
        "graduation_can_export": True,
        **navigation,
    }


def _admin_graduation_search_context(
    request: HttpRequest,
    *,
    student_id_query: str = "",
    error_code: str | None = None,
) -> dict:
    return {
        **get_sidebar_context(request),
        "student": None,
        "student_id": None,
        "student_id_query": student_id_query,
        "academic_year": None,
        "term": None,
        "baseline_kind": REGISTERED_TIMETABLE,
        "grad": None,
        "graduation_presentation": {},
        "graduation_error": None,
        "graduation_admin_mode": True,
        "graduation_page_url": "",
        "graduation_export_url": "",
        "graduation_back_url": reverse("admin_graduation_planning_page"),
        "graduation_search_url": reverse("admin_graduation_planning_page"),
        "graduation_search_error_code": error_code,
        "graduation_export_href": "",
    }


def _parse_admin_student_id(raw_value: str) -> int | None:
    value = raw_value.strip()
    if not value or not value.isascii() or not value.isdecimal():
        return None
    student_id = int(value)
    if student_id <= 0 or student_id > MAX_STUDENT_ID:
        return None
    return student_id


def _normalise_admin_must_have_courses(request: HttpRequest) -> tuple[list[str], list[dict]]:
    """Parse the non-mutating admin scenario parameters without guessing codes."""
    courses: list[str] = []
    errors: list[dict] = []
    for raw_value in request.GET.getlist("must_have"):
        for value in re.split(r"[,;\r\n]+", str(raw_value or "")):
            if not value.strip():
                continue
            code = normalize_code(value)
            if (
                not code
                or len(code) > MAX_ADMIN_COURSE_CODE_LENGTH
                or _ADMIN_COURSE_CODE_RE.fullmatch(code) is None
            ):
                errors.append(
                    {
                        "kind": "INVALID_MUST_HAVE_COURSE_CODE",
                        "course_code": str(value).strip()[:MAX_ADMIN_COURSE_CODE_LENGTH],
                    }
                )
                continue
            if code not in courses:
                courses.append(code)
    if len(courses) > MAX_ADMIN_MUST_HAVE_COURSES:
        errors.append(
            {
                "kind": "TOO_MANY_MUST_HAVE_COURSES",
                "maximum": MAX_ADMIN_MUST_HAVE_COURSES,
            }
        )
    return courses[:MAX_ADMIN_MUST_HAVE_COURSES], errors


def _graduation_scenario_query(
    baseline_kind: str,
    must_have_courses: list[str] | tuple[str, ...] = (),
    allow_same_term_prerequisites: bool = False,
) -> str:
    pairs: list[tuple[str, str]] = [("baseline", baseline_kind)]
    pairs.extend(("must_have", code) for code in must_have_courses)
    if allow_same_term_prerequisites:
        pairs.append(("allow_same_term_prerequisites", "1"))
    return urlencode(pairs)


def _url_with_query(path: str, query: str) -> str:
    return f"{path}?{query}" if query else path


def _graduation_navigation_context(
    *,
    page_url: str,
    export_url: str,
    baseline_kind: str,
    must_have_courses: list[str] | tuple[str, ...] = (),
    allow_same_term_prerequisites: bool = False,
) -> dict:
    tab_urls = {
        kind: _url_with_query(
            page_url,
            _graduation_scenario_query(
                kind,
                must_have_courses,
                allow_same_term_prerequisites,
            ),
        )
        for kind in sorted(ADVISOR_GRADUATION_BASELINE_KINDS)
    }
    active_query = _graduation_scenario_query(
        baseline_kind,
        must_have_courses,
        allow_same_term_prerequisites,
    )
    return {
        "graduation_registered_url": tab_urls[REGISTERED_TIMETABLE],
        "graduation_recommended_url": tab_urls["recommended_current_term"],
        "graduation_optimized_url": tab_urls[OPTIMIZED_CURRENT_OFFERINGS],
        "graduation_retry_url": _url_with_query(page_url, active_query),
        "graduation_export_href": _url_with_query(export_url, active_query),
        "graduation_scenario_clear_url": _url_with_query(
            page_url,
            _graduation_scenario_query(baseline_kind),
        ),
    }


def _admin_graduation_plan_options(
    student: Student,
    baseline_report: dict | None,
    must_have_courses: list[str],
) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """Return authoritative programme-plan choices plus request validation."""
    requirements = list(
        ProgrammeRequirement.objects.filter(program=student.program)
        .order_by("programme_term", "course_code")
        .values("course_code", "course_name", "programme_term", "credit_hours", "type")
    )
    passed_codes = {
        normalize_code(code)
        for code in student.student_courses.filter(status__iexact="passed").values_list(
            "course__course_code", flat=True
        )
        if normalize_code(code)
    }
    baseline_codes = {
        normalize_code(course.get("code") or course.get("offered_course_code") or "")
        for course in (baseline_report or {}).get("planning_baseline_courses_assumed_passed", [])
        if isinstance(course, dict)
    }
    options: list[dict] = []
    by_code: dict[str, dict] = {}
    for row in requirements:
        code = normalize_code(row.get("course_code"))
        if not code:
            continue
        placeholder = is_elective_slot(row.get("type"))
        if code in passed_codes:
            status = "passed"
        elif code in baseline_codes:
            status = "starting_source"
        elif placeholder:
            status = "elective_placeholder"
        else:
            status = "available"
        option = {
            "code": code,
            "name": str(row.get("course_name") or "").strip(),
            "term": int(row.get("programme_term") or 0),
            "credits": int(row.get("credit_hours") or 0),
            "type": str(row.get("type") or "").strip(),
            "status": status,
            "selected": code in must_have_courses,
            "selectable": status == "available",
        }
        options.append(option)
        by_code[code] = option

    errors: list[dict] = []
    for code in must_have_courses:
        option = by_code.get(code)
        if option is None:
            errors.append({"kind": "MUST_HAVE_COURSE_NOT_IN_PLAN", "course_code": code})
        elif option["status"] == "elective_placeholder":
            errors.append({"kind": "ELECTIVE_PLACEHOLDER_NOT_A_COURSE", "course_code": code})
    return options, by_code, errors


def _admin_same_term_prerequisite_setting(request: HttpRequest) -> tuple[bool, list[dict]]:
    values = request.GET.getlist("allow_same_term_prerequisites")
    if not values:
        return False, []
    if any(str(value) != "1" for value in values):
        return False, [{"kind": "INVALID_SAME_TERM_PREREQUISITE_SETTING"}]
    return True, []


def _scenario_presentation(request: HttpRequest, report: dict, year: int, term: int) -> dict:
    return graduation_presentation_from_tool_results(
        [
            {
                **report,
                "tool": "graduation_progress",
                "ok": True,
                "scenario_academic_year": year,
                "scenario_term": term,
            }
        ]
    )


def _admin_graduation_scenario(
    request: HttpRequest,
    *,
    student: Student,
    payload: dict | None,
) -> tuple[dict | None, dict, int]:
    """Apply one admin-only, read-only current-term constraint scenario."""
    must_have_courses, parse_errors = _normalise_admin_must_have_courses(request)
    allow_same_term_prerequisites, setting_errors = _admin_same_term_prerequisite_setting(request)
    baseline_report = payload.get("report") if payload else None
    options, option_by_code, plan_errors = _admin_graduation_plan_options(
        student,
        baseline_report,
        must_have_courses,
    )
    errors = [*parse_errors, *setting_errors, *plan_errors]
    raw_must_have_requested = any(
        str(value or "").strip() for value in request.GET.getlist("must_have")
    )
    if raw_must_have_requested and not must_have_courses and not parse_errors:
        errors.append({"kind": "NO_MUST_HAVE_COURSES"})
    if allow_same_term_prerequisites and not must_have_courses:
        errors.append({"kind": "SAME_TERM_PREREQUISITES_REQUIRE_MUST_HAVE_COURSE"})

    scenario_requested = bool(
        raw_must_have_requested or request.GET.getlist("allow_same_term_prerequisites")
    )
    scenario_report: dict | None = None
    what_if: dict = {}
    result_payload = payload
    result_status = 400 if errors else 200

    if payload and must_have_courses and not errors:
        try:
            scenario_report = build_graduation_must_have_scenario(
                int(student.student_id),
                int(payload["academic_year"]),
                int(payload["term"]),
                baseline_report=baseline_report,
                must_have_courses=must_have_courses,
                allow_same_term_direct_prerequisites=allow_same_term_prerequisites,
            )
            if scenario_report and _request_prefers_arabic(request):
                scenario_report = prefer_arabic_course_names_in_payload(scenario_report)
            what_if = (
                scenario_report.get("what_if")
                if isinstance(scenario_report, dict)
                and isinstance(scenario_report.get("what_if"), dict)
                else {}
            )
            if what_if.get("valid") is True:
                result_payload = {
                    **payload,
                    "report": scenario_report,
                    "presentation": _scenario_presentation(
                        request,
                        scenario_report,
                        int(payload["academic_year"]),
                        int(payload["term"]),
                    ),
                }
                result_status = 200
            else:
                errors.extend(list(what_if.get("validation_errors") or []))
                result_status = 400
        except (TypeError, ValueError) as exc:
            logger.warning(
                "invalid admin graduation scenario for student %s: %s",
                student.student_id,
                exc,
            )
            errors.append({"kind": "INVALID_MUST_HAVE_SCENARIO"})
            result_status = 400
        except Exception:  # noqa: BLE001 - keep the factual baseline visible
            logger.exception(
                "admin graduation scenario failed for student %s",
                student.student_id,
            )
            errors.append({"kind": "MUST_HAVE_SCENARIO_FAILED"})
            result_status = 500

    selected_rows = [
        option_by_code.get(code, {"code": code, "name": "", "term": 0, "credits": 0})
        for code in must_have_courses
    ]
    context = {
        "graduation_scenario_requested": scenario_requested,
        "graduation_scenario_valid": bool(what_if.get("valid") is True),
        "graduation_scenario_errors": errors,
        "graduation_scenario_what_if": what_if,
        "graduation_must_have_courses": must_have_courses,
        "graduation_must_have_csv": ", ".join(must_have_courses),
        "graduation_selected_plan_courses": selected_rows,
        "graduation_allow_same_term_prerequisites": allow_same_term_prerequisites,
        "graduation_plan_options": options,
        "graduation_max_must_have_courses": MAX_ADMIN_MUST_HAVE_COURSES,
        "graduation_can_export": not scenario_requested or bool(what_if.get("valid") is True),
    }
    return result_payload, context, result_status


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
    student = Student.objects.get(student_id=student_id)
    context = _graduation_workspace_context(
        request,
        student=student,
        student_id=student_id,
        payload=payload,
        error=error,
        requested_baseline=requested_baseline,
        admin_mode=False,
    )
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
@role_required(ROLE_SUPER_ADMIN)
@require_GET
def admin_graduation_planning_page(request: HttpRequest) -> HttpResponse:
    """Render the admin Student-ID launcher for the graduation planner."""
    raw_student_id = request.GET.get("student_id")
    if raw_student_id is None:
        return render(
            request,
            "core/advisor_student_graduation.html",
            _admin_graduation_search_context(request),
        )

    student_id_query = str(raw_student_id).strip()
    student_id = _parse_admin_student_id(student_id_query)
    if student_id is None:
        return render(
            request,
            "core/advisor_student_graduation.html",
            _admin_graduation_search_context(
                request,
                student_id_query=student_id_query,
                error_code="INVALID_STUDENT_ID",
            ),
            status=400,
        )
    if not Student.objects.filter(student_id=student_id).exists():
        return render(
            request,
            "core/advisor_student_graduation.html",
            _admin_graduation_search_context(
                request,
                student_id_query=student_id_query,
                error_code="STUDENT_NOT_FOUND",
            ),
            status=404,
        )
    return redirect("admin_graduation_student_page", student_id=student_id)


@never_cache
@role_required(ROLE_SUPER_ADMIN)
@require_GET
def admin_graduation_student_page(request: HttpRequest, student_id: int) -> HttpResponse:
    """Render an unrestricted, admin-only graduation planning workspace."""
    if student_id <= 0 or student_id > MAX_STUDENT_ID:
        return render(
            request,
            "core/advisor_student_graduation.html",
            _admin_graduation_search_context(
                request,
                student_id_query=str(student_id),
                error_code="INVALID_STUDENT_ID",
            ),
            status=400,
        )
    student = Student.objects.filter(student_id=student_id).first()
    if student is None:
        return render(
            request,
            "core/advisor_student_graduation.html",
            _admin_graduation_search_context(
                request,
                student_id_query=str(student_id),
                error_code="STUDENT_NOT_FOUND",
            ),
            status=404,
        )

    scope_error = require_student_scope(request, student_id)
    if scope_error:
        return scope_error
    payload, error, status = _advisor_student_graduation_payload(request, student_id)
    requested_baseline = str(request.GET.get("baseline") or REGISTERED_TIMETABLE).strip()
    scenario_payload, scenario_context, scenario_status = _admin_graduation_scenario(
        request,
        student=student,
        payload=payload,
    )
    if scenario_context["graduation_scenario_requested"]:
        status = scenario_status if payload is not None else status
    payload = scenario_payload
    context = _graduation_workspace_context(
        request,
        student=student,
        student_id=student_id,
        payload=payload,
        error=error,
        requested_baseline=requested_baseline,
        admin_mode=True,
    )
    context.update(scenario_context)
    context.update(
        _graduation_navigation_context(
            page_url=context["graduation_page_url"],
            export_url=context["graduation_export_url"],
            baseline_kind=context["baseline_kind"],
            must_have_courses=scenario_context["graduation_must_have_courses"],
            allow_same_term_prerequisites=scenario_context[
                "graduation_allow_same_term_prerequisites"
            ],
        )
    )
    return render(
        request,
        "core/advisor_student_graduation.html",
        context,
        status=status,
    )


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

    return _graduation_export_response(request, student_id)


@never_cache
@role_required(ROLE_SUPER_ADMIN)
@require_GET
def admin_graduation_student_export_xlsx(request: HttpRequest, student_id: int) -> HttpResponse:
    """Download the active scenario from the admin graduation workspace."""
    if student_id <= 0 or student_id > MAX_STUDENT_ID:
        return JsonResponse(
            {"error": "Student ID must be a positive integer.", "code": "INVALID_STUDENT_ID"},
            status=400,
        )
    scope_error = require_student_scope(request, student_id)
    if scope_error:
        return scope_error

    return _graduation_export_response(request, student_id, admin_scenario=True)


def _graduation_export_response(
    request: HttpRequest,
    student_id: int,
    *,
    admin_scenario: bool = False,
) -> HttpResponse:
    """Build the workbook response after the caller has enforced its own scope."""

    payload, error, status = _advisor_student_graduation_payload(request, student_id)
    if payload is None:
        return JsonResponse(error, status=status)

    student = Student.objects.filter(student_id=student_id).first()
    if student is None:  # Defensive only; the scope check already returns 404.
        return JsonResponse(
            {"error": "Student not found.", "code": "STUDENT_NOT_FOUND"},
            status=404,
        )

    scenario_suffix = ""
    if admin_scenario:
        scenario_payload, scenario_context, scenario_status = _admin_graduation_scenario(
            request,
            student=student,
            payload=payload,
        )
        if scenario_context["graduation_scenario_requested"]:
            if (
                scenario_status != 200
                or scenario_payload is None
                or not scenario_context["graduation_scenario_valid"]
            ):
                return JsonResponse(
                    {
                        "error": "The must-have graduation scenario is invalid.",
                        "code": "INVALID_MUST_HAVE_SCENARIO",
                        "validation_errors": scenario_context["graduation_scenario_errors"],
                    },
                    status=scenario_status if scenario_status >= 400 else 400,
                )
            payload = scenario_payload
            scenario_suffix = "_scenario"

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
        f"graduation_plan_{student_id}_{baseline_slug}{scenario_suffix}_"
        f"{payload['academic_year']}_T{payload['term']}.xlsx"
    )
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
