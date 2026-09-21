"""Local launcher for exercising the real student-adviser HTTP surface.

The lab does not implement a second chat endpoint.  It switches an explicitly
authorised local superuser session into an ordinary provisioned student session,
then redirects to the production student adviser page.  Consequently ownership,
rate limiting, persistence, idempotency, evidence handling, and the V2/V2.1
dispatcher are all the same code a student uses.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.models import Student
from core.services.rbac import ROLE_STUDENT, get_user_scope
from core.services.student_identity import normalize_student_id
from core.services.student_otp import (
    OTPError,
    mark_student_authentication,
    provision_student_user,
)
from core.sidebar_context import get_sidebar_context

_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def student_advisor_lab_request_allowed(request: HttpRequest) -> bool:
    """Return whether this request originates from the explicitly enabled lab.

    ``REMOTE_ADDR`` is server-owned.  Do not consult forwarding headers here: the
    launcher is intentionally usable only by a browser connected over loopback,
    even if a development reverse proxy happens to be configured.
    """

    if not settings.DEBUG or not getattr(settings, "ALLOW_DEV_STUDENT_ADVISOR_LAB", False):
        return False
    try:
        return ipaddress.ip_address(str(request.META.get("REMOTE_ADDR") or "")).is_loopback
    except ValueError:
        return False


def _local_lab_only(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not student_advisor_lab_request_allowed(request):
            # Conceal the development-only surface when any deployment guard is
            # absent.  A production instance should look as though this route does
            # not exist, rather than advertise a disabled impersonation endpoint.
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


@_local_lab_only
@login_required(login_url="login")
@never_cache
@require_http_methods(["GET", "POST"])
def dev_student_advisor_v21_lab_view(request: HttpRequest) -> HttpResponse:
    """Select a local student and enter the genuine student V2.1 chat page."""

    if not request.user.is_superuser:
        # The launcher deliberately replaces the superuser session with the
        # selected student.  Reopening the same local lab URL should therefore
        # return that linked student to the real prompt page, not strand them on
        # a 403/dashboard loop.  Other non-superusers remain forbidden.
        try:
            scope = get_user_scope(request.user)
        except Exception:
            scope = {}
        if scope.get("role") == ROLE_STUDENT and scope.get("student_id"):
            return redirect(f"{reverse('student_advisor')}?lab=1")
        return HttpResponseForbidden("This local lab requires a superuser.")

    context: dict[str, Any] = {
        **get_sidebar_context(request),
        "v2_enabled": bool(getattr(settings, "STUDENT_ADVISOR_V2_ENABLED", False)),
        "v21_enabled": bool(getattr(settings, "STUDENT_ADVISOR_V21_ENABLED", False)),
        "llm_backend": str(getattr(settings, "LLM_BACKEND", "")),
    }
    if request.method == "GET":
        return render(request, "core/dev_student_advisor_v21_lab.html", context)

    raw_student_id = str(request.POST.get("student_id") or "").strip()
    try:
        student_id = normalize_student_id(raw_student_id)
    except ValueError:
        context["error"] = "Enter a valid university ID."
        context["student_id"] = raw_student_id
        return render(
            request,
            "core/dev_student_advisor_v21_lab.html",
            context,
            status=400,
        )

    if not Student.objects.filter(student_id=student_id).exists():
        context["error"] = "No local student record has that university ID."
        context["student_id"] = raw_student_id
        return render(
            request,
            "core/dev_student_advisor_v21_lab.html",
            context,
            status=404,
        )

    try:
        student_user = provision_student_user(student_id)
    except OTPError:
        context["error"] = "That student identity cannot be provisioned safely."
        context["student_id"] = raw_student_id
        return render(
            request,
            "core/dev_student_advisor_v21_lab.html",
            context,
            status=409,
        )

    # This deliberately becomes a normal student session.  No subject id travels
    # to the chat API, and every downstream principal is resolved from UserScope.
    login(request, student_user, backend=_MODEL_BACKEND)
    mark_student_authentication(request)
    return redirect(f"{reverse('student_advisor')}?lab=1")


__all__ = [
    "dev_student_advisor_v21_lab_view",
    "student_advisor_lab_request_allowed",
]
