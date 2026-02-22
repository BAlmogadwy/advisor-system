from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.services.rbac import ensure_role_groups, ensure_scope_schema


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
        user = authenticate(request, username=username, password=password)
        if user is None:
            error = "Invalid username or password."
        else:
            login(request, user)
            return redirect("dashboard")

    return render(request, "core/login.html", {"error": error})


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")
