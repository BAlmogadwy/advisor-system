import logging

from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from core.services.rbac import ensure_role_groups, ensure_scope_schema

logger = logging.getLogger(__name__)

_LOGIN_MAX_FAILS = 5
_LOGIN_LOCKOUT_SECONDS = 300


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    # RBAC bootstrap — log failures but never block login. authenticate()
    # works even if the role groups don't exist, and subsequent requests
    # will retry the bootstrap.
    try:
        ensure_role_groups()
    except Exception:
        logger.exception("ensure_role_groups failed; continuing without RBAC bootstrap")
    try:
        ensure_scope_schema()
    except Exception:
        logger.exception("ensure_scope_schema failed; continuing")

    if request.user.is_authenticated:
        return redirect("dashboard")

    error = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""

        # Cache-based rate limiting (keyed by username, not session)
        fail_key = f"login_fail:{username}"
        fails = cache.get(fail_key, 0)

        if fails >= _LOGIN_MAX_FAILS:
            error = "Too many failed attempts. Please try again later."
            return render(request, "core/login.html", {"error": error})

        user = authenticate(request, username=username, password=password)
        if user is None:
            cache.set(fail_key, fails + 1, _LOGIN_LOCKOUT_SECONDS)
            error = "Invalid username or password."
        else:
            cache.delete(fail_key)
            login(request, user)
            return redirect("dashboard")

    return render(request, "core/login.html", {"error": error})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")
