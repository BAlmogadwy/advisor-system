"""
core/settings_views.py
Global site defaults (academic year + term).
Stored in site_defaults.json next to manage.py — no migrations needed.
"""
import json
import os
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope

# ── Storage ───────────────────────────────────────────────────
_DEFAULTS_PATH = Path(
    os.environ.get("SITE_DEFAULTS_PATH", "")
    or Path(__file__).resolve().parent.parent / "site_defaults.json"
)

_BUILTIN_DEFAULTS: dict = {"academic_year": 1447, "term": 1}


def load_defaults() -> dict:
    """Return saved defaults. Falls back to built-ins if file missing or corrupt."""
    try:
        if _DEFAULTS_PATH.exists():
            data = json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
            return {
                "academic_year": int(data.get("academic_year", _BUILTIN_DEFAULTS["academic_year"])),
                "term":          int(data.get("term",          _BUILTIN_DEFAULTS["term"])),
            }
    except (json.JSONDecodeError, ValueError, OSError):
        pass
    return dict(_BUILTIN_DEFAULTS)


def save_defaults(academic_year: int, term: int) -> None:
    """Write defaults atomically (tmp-then-rename)."""
    data = {"academic_year": academic_year, "term": term}
    tmp = _DEFAULTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_DEFAULTS_PATH)


# ── View ──────────────────────────────────────────────────────
@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
def defaults_settings_view(request):
    """
    GET  /ops/settings/defaults/  → current defaults as JSON (any logged-in user)
    POST /ops/settings/defaults/  → save new defaults (SUPER_ADMIN / GENERAL_ADVISOR only)
    """
    if request.method == "GET":
        return JsonResponse(load_defaults())

    # POST — restrict to admin roles
    scope = get_user_scope(request.user)
    role  = str(scope.get("role", "")).upper()
    if role not in {ROLE_SUPER_ADMIN.upper(), ROLE_GENERAL_ADVISOR.upper()}:
        return JsonResponse({"error": "Permission denied. Super admin or general advisor required."}, status=403)

    try:
        body = json.loads(request.body)
        yr   = int(body["academic_year"])
        tm   = int(body["term"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return JsonResponse({"error": f"Invalid payload: {exc}"}, status=400)

    if not (1400 <= yr <= 1600):
        return JsonResponse({"error": "academic_year must be between 1400 and 1600."}, status=400)
    if tm not in (0, 1, 2, 3):
        return JsonResponse({"error": "term must be 0, 1, 2, or 3."}, status=400)

    save_defaults(yr, tm)
    return JsonResponse({
        "message": f"Defaults saved: year={yr}, term={tm}",
        "academic_year": yr,
        "term": tm,
    })