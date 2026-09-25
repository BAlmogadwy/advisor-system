"""Portal defaults and access boundaries for restricted roles."""

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import translation

from core.services.rbac import ROLE_EXAM_COMMITTEE, get_user_role


class ExamCommitteeAccessMiddleware:
    """Keep committee accounts inside exams, including direct API requests.

    Match fully qualified resolved view names: e.g. ``admin:login`` must not
    inherit permission from the public ``login`` route. New routes are denied
    until explicitly included here.
    """

    allowed_views = frozenset(
        {
            "login",
            "logout",
            "set_language",
            "profile_page",
            "profile_me",
            "profile_change_username",
            "profile_change_password",
            "exam_timetable_page",
            "exam_timetable_filters",
            "exam_timetable_preview_courses",
            "exam_timetable_build",
            "exam_timetable_job_active",
            "exam_timetable_job",
            "exam_timetable_job_result",
            "exam_timetable_job_cancel",
            "exam_timetable_job_seen",
            "exam_timetable_draft_impact",
            "exam_timetable_list",
            "exam_timetable_detail",
            "exam_timetable_copy",
            "exam_timetable_export",
            "exam_department_options",
            "exam_department_export",
            "exam_student_export_preflight",
            "exam_student_export",
        }
    )

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponse | None:
        if not request.user.is_authenticated or get_user_role(request.user) != ROLE_EXAM_COMMITTEE:
            return None
        view_name = request.resolver_match.view_name if request.resolver_match else None
        if view_name == "dashboard" and request.method in {"GET", "HEAD"}:
            return redirect("exam_timetable_page")
        if view_name in self.allowed_views:
            return None
        return JsonResponse(
            {"error": "Exam Committee access is limited to exam timetables and your account."},
            status=403,
        )


class StudentPortalDefaultsMiddleware:
    """Start the student login flow in Arabic until the user chooses a language.

    ``LocaleMiddleware`` still owns explicit language selection through Django's
    language cookie.  The login entry point supplies and persists the Arabic
    default when that cookie is absent, so the choice follows the student into
    every portal page. Staff pages and locale-aware APIs retain their existing
    behaviour, and students can still deliberately switch to English.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        language_cookie = settings.LANGUAGE_COOKIE_NAME
        if not request.path.startswith("/student/login/") or language_cookie in request.COOKIES:
            return self.get_response(request)

        request.LANGUAGE_CODE = "ar"
        with translation.override("ar"):
            response = self.get_response(request)
        response.set_cookie(
            language_cookie,
            "ar",
            max_age=settings.LANGUAGE_COOKIE_AGE,
            path=settings.LANGUAGE_COOKIE_PATH,
            domain=settings.LANGUAGE_COOKIE_DOMAIN,
            secure=settings.LANGUAGE_COOKIE_SECURE,
            httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
            samesite=settings.LANGUAGE_COOKIE_SAMESITE,
        )
        return response
