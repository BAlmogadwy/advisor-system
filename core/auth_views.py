import time

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from core.services.rbac import ensure_role_groups, ensure_scope_schema

_LOGIN_MAX_FAILS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    ensure_role_groups()
    ensure_scope_schema()

    if request.user.is_authenticated:
        return redirect("dashboard")

    error = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""

        # --- session-based rate limiting ---
        fail_key = f"login_fails_{username}"
        lock_key = f"login_lockout_{username}"
        fails = request.session.get(fail_key, 0)
        lockout_ts = request.session.get(lock_key, 0)

        if fails >= _LOGIN_MAX_FAILS:
            if time.time() - lockout_ts < _LOGIN_LOCKOUT_SECONDS:
                error = "Too many failed attempts. Please try again later."
                return render(request, "core/login.html", {"error": error})
            # lockout expired -- reset counters
            request.session[fail_key] = 0
            request.session.pop(lock_key, None)
            fails = 0

        user = authenticate(request, username=username, password=password)
        if user is None:
            fails += 1
            request.session[fail_key] = fails
            if fails >= _LOGIN_MAX_FAILS:
                request.session[lock_key] = time.time()
            error = "Invalid username or password."
        else:
            # clear rate-limit counters on success
            request.session.pop(fail_key, None)
            request.session.pop(lock_key, None)
            login(request, user)
            return redirect("dashboard")

    return render(request, "core/login.html", {"error": error})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")
