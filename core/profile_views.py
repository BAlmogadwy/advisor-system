"""Self-service profile endpoints for authenticated users.

Allows any logged-in user to view their profile and change
their own username or password.
"""

import json

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.services.audit import log_audit_event
from core.services.rbac import get_user_role, get_user_scope


@login_required(login_url="login")
@require_GET
def profile_me_view(request: HttpRequest) -> JsonResponse:
    """Return current user info."""
    scope = get_user_scope(request.user)
    return JsonResponse({
        "ok": True,
        "user": {
            "id": request.user.id,
            "username": request.user.username,
            "role": get_user_role(request.user),
            "advisor_id": scope.get("advisor_id", ""),
            "departments": scope.get("departments", []),
        },
    })


@login_required(login_url="login")
@require_POST
def profile_change_username_view(request: HttpRequest) -> JsonResponse:
    """Change own username."""
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    new_username = str(payload.get("new_username", "")).strip()
    if not new_username:
        return JsonResponse({"error": "Username cannot be empty"}, status=400)

    if len(new_username) > 150:
        return JsonResponse({"error": "Username too long (max 150)"}, status=400)

    if User.objects.filter(username=new_username).exclude(id=request.user.id).exists():
        return JsonResponse({"error": "Username already taken"}, status=400)

    old_username = request.user.username
    request.user.username = new_username
    request.user.save(update_fields=["username"])

    log_audit_event(
        request,
        action="profile.change_username",
        status="success",
        details={"old": old_username, "new": new_username},
    )

    return JsonResponse({"ok": True, "username": new_username})


@login_required(login_url="login")
@require_POST
def profile_change_password_view(request: HttpRequest) -> JsonResponse:
    """Change own password (requires current password)."""
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))

    if not current_password or not new_password:
        return JsonResponse(
            {"error": "Both current_password and new_password are required"},
            status=400,
        )

    if not request.user.check_password(current_password):
        return JsonResponse({"error": "Current password is incorrect"}, status=400)

    if len(new_password) < 6:
        return JsonResponse(
            {"error": "New password must be at least 6 characters"}, status=400
        )

    request.user.set_password(new_password)
    request.user.save(update_fields=["password"])

    # Keep the session alive after password change
    update_session_auth_hash(request, request.user)

    log_audit_event(
        request,
        action="profile.change_password",
        status="success",
    )

    return JsonResponse({"ok": True})
