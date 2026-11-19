"""HTTP access to the shared academic year and term settings."""

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_http_methods

from core.services.academic_settings import load_defaults, save_defaults
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope


@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
def defaults_settings_view(request: HttpRequest) -> JsonResponse:
    """
    GET  /ops/settings/defaults/  → current defaults as JSON (any logged-in user)
    POST /ops/settings/defaults/  → save new defaults (SUPER_ADMIN / GENERAL_ADVISOR only)
    """
    if request.method == "GET":
        return JsonResponse(load_defaults())

    # POST — restrict to admin roles
    scope = get_user_scope(request.user)
    role = str(scope.get("role", "")).upper()
    if role not in {ROLE_SUPER_ADMIN.upper(), ROLE_GENERAL_ADVISOR.upper()}:
        return JsonResponse(
            {"error": "Permission denied. Super admin or general advisor required."}, status=403
        )

    try:
        body = json.loads(request.body)
        yr = int(body["academic_year"])
        tm = int(body["term"])
        # Two-field clients use the original API defaults for the current term.
        c_yr = int(body.get("currentYear", 1447))
        c_tm = int(body.get("currentTerm", 1))
    except (KeyError, TypeError, ValueError) as exc:
        return JsonResponse({"error": f"Invalid payload: {exc}"}, status=400)

    if not (1400 <= yr <= 1600):
        return JsonResponse({"error": "academic_year must be between 1400 and 1600."}, status=400)
    if tm not in (0, 1, 2, 3):
        return JsonResponse({"error": "term must be 0, 1, 2, or 3."}, status=400)
    if not (1400 <= c_yr <= 1600):
        return JsonResponse({"error": "currentYear must be between 1400 and 1600."}, status=400)
    if c_tm not in (0, 1, 2, 3):
        return JsonResponse({"error": "currentTerm must be 0, 1, 2, or 3."}, status=400)

    save_defaults(yr, tm, c_yr, c_tm)
    return JsonResponse(
        {
            "message": f"Defaults saved: year={yr}, term={tm}, currentYear={c_yr}, currentTerm={c_tm}",
            "academic_year": yr,
            "term": tm,
            "currentYear": c_yr,
            "currentTerm": c_tm,
        }
    )
