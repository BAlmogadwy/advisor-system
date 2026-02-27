import os
from typing import Any, cast

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.models import ProgrammeRequirement
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_scope,
    set_user_scope,
)
from core.services.recommender import recommend_next_courses
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context


def health(request: HttpRequest) -> JsonResponse:
    try:
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error", "db": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required(login_url="login")
@require_POST
def dev_role_switch_view(request: HttpRequest) -> JsonResponse:
    # Double-guard: require both DEBUG=True AND an explicit opt-in env var.
    # This prevents accidental exposure if DEBUG is ever left on in production.
    if not settings.DEBUG or os.getenv("ALLOW_DEV_ROLE_SWITCH", "").lower() != "true":
        return JsonResponse({"error": "Not available outside DEBUG mode."}, status=403)

    role = (request.POST.get("role") or "").strip()
    advisor_id = (request.POST.get("advisor_id") or "").strip()
    departments = (request.POST.get("departments") or "").strip()

    if role not in {ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR}:
        return JsonResponse({"error": "Invalid role."}, status=400)

    ensure_role_groups()
    ensure_scope_schema()
    user = request.user
    if not user.is_authenticated or user.id is None:
        return JsonResponse({"error": "Authentication required."}, status=401)
    groups_manager = cast(Any, user.groups)
    groups_manager.clear()
    groups_manager.add(Group.objects.get(name=role))
    set_user_scope(int(user.id), advisor_id=advisor_id, departments=departments)

    return JsonResponse(
        {
            "ok": True,
            "role": role,
            "advisor_id": advisor_id,
            "departments": departments,
            "message": "Role switched for current session (dev mode). Refresh dashboard.",
        }
    )


@login_required(login_url="login")
def dashboard(request: HttpRequest) -> HttpResponse:
    student_id_raw = request.GET.get("student_id", "").strip()

    # Fall back to saved global defaults when year/semester not in GET params
    _defaults = load_defaults()
    year_raw = request.GET.get("year", "").strip() or str(_defaults["academic_year"])
    semester_raw = request.GET.get("semester", "").strip() or str(_defaults["term"])

    programs = list(
        ProgrammeRequirement.objects.exclude(program__isnull=True)
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
        .order_by("program")
    )

    ensure_role_groups()
    ensure_scope_schema()
    scope = get_user_scope(request.user)
    role = str(scope.get("role", ROLE_ADVISOR))

    advisor_scope = str(scope.get("advisor_id", "")).strip()
    department_scope = [str(x).upper() for x in scope.get("departments", []) if str(x).strip()]
    if role == ROLE_SUPER_ADMIN:
        role_scope_hint = "Full access across all programs and advisors."
    elif role == ROLE_GENERAL_ADVISOR:
        role_scope_hint = f"Department scope: {', '.join(department_scope) if department_scope else 'none assigned'}"
    else:
        role_scope_hint = f"Advisor scope: {advisor_scope or 'none assigned'}"

    context: dict[str, object] = {
        **get_sidebar_context(request),
        "student_id": student_id_raw,
        "year": year_raw,
        "semester": semester_raw,
        "recommendations": None,
        "error": "",
        "programs": programs,
        "role_scope_hint": role_scope_hint,
        "scope_departments": department_scope,
        "scope_advisor_id": advisor_scope,
        "debug_mode": bool(settings.DEBUG),
    }

    if student_id_raw:
        try:
            student_id = int(student_id_raw)
            year = int(year_raw)
            semester = int(semester_raw)
        except ValueError:
            context["error"] = "student_id, year, and semester must all be integers."
            return render(request, "core/dashboard.html", context)

        recommendations = recommend_next_courses(
            student_id=student_id,
            current_academic_year=year,
            current_semester=semester,
        )
        context["recommendations"] = recommendations

    return render(request, "core/dashboard.html", context)
