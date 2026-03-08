from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.services.audit import log_audit_event
from core.services.rbac import (
    ROLE_NAMES,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_role,
    set_user_scope,
)
from core.sidebar_context import get_sidebar_context
from core.utils import parse_json_body as _parse_json_body


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def user_management_page(request: HttpRequest) -> HttpResponse:
    return render(request, "core/user_management.html", get_sidebar_context(request))


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def users_list_view(request: HttpRequest) -> JsonResponse:
    ensure_scope_schema()
    items: list[dict[str, object]] = []
    for u in User.objects.all().order_by("username"):
        scope = {
            "advisor_id": "",
            "departments": [],
        }
        # Reuse existing scope helper via set/get table directly through service
        from core.services.rbac import get_user_scope

        scope_data = get_user_scope(u)
        scope["advisor_id"] = str(scope_data.get("advisor_id", ""))
        scope["departments"] = list(scope_data.get("departments", []))
        items.append(
            {
                "id": u.id,
                "username": u.username,
                "is_active": u.is_active,
                "is_superuser": u.is_superuser,
                "role": get_user_role(u),
                "advisor_id": scope["advisor_id"],
                "departments": scope["departments"],
                "last_login": u.last_login.isoformat() if u.last_login else None,
                "date_joined": u.date_joined.isoformat() if u.date_joined else None,
            }
        )

    return JsonResponse({"count": len(items), "items": items})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def users_create_view(request: HttpRequest) -> JsonResponse:
    ensure_role_groups()
    ensure_scope_schema()
    payload, err = _parse_json_body(request)
    if err:
        return err

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    role = str(payload.get("role", "")).strip()
    advisor_id = str(payload.get("advisor_id", "")).strip()
    departments = str(payload.get("departments", "")).strip()

    if not username or not password or role not in ROLE_NAMES:
        return JsonResponse(
            {"error": "username, password, and valid role are required"}, status=400
        )

    try:
        validate_password(password)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)

    if User.objects.filter(username=username).exists():
        return JsonResponse({"error": "username already exists"}, status=400)

    with transaction.atomic():
        user = User.objects.create_user(username=username, password=password, is_active=True)
        user.groups.clear()
        user.groups.add(Group.objects.get(name=role))
        set_user_scope(int(user.id), advisor_id=advisor_id, departments=departments)

    log_audit_event(
        request,
        action="user.create",
        status="success",
        details={
            "username": username,
            "role": role,
            "advisor_id": advisor_id,
            "departments": departments,
        },
    )
    return JsonResponse({"ok": True, "username": username, "role": role})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def users_update_role_view(request: HttpRequest) -> JsonResponse:
    ensure_role_groups()
    ensure_scope_schema()
    payload, err = _parse_json_body(request)
    if err:
        return err

    username = str(payload.get("username", "")).strip()
    role = str(payload.get("role", "")).strip()
    advisor_id = str(payload.get("advisor_id", "")).strip()
    departments = str(payload.get("departments", "")).strip()

    if not username or role not in ROLE_NAMES:
        return JsonResponse({"error": "username and valid role are required"}, status=400)

    user = User.objects.filter(username=username).first()
    if user is None:
        return JsonResponse({"error": "user not found"}, status=404)

    with transaction.atomic():
        user.groups.clear()
        user.groups.add(Group.objects.get(name=role))
        set_user_scope(int(user.id), advisor_id=advisor_id, departments=departments)

    log_audit_event(
        request,
        action="user.update_role",
        status="success",
        details={
            "username": username,
            "role": role,
            "advisor_id": advisor_id,
            "departments": departments,
        },
    )
    return JsonResponse({"ok": True, "username": username, "role": role})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def users_set_password_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    username = str(payload.get("username", "")).strip()
    new_password = str(payload.get("new_password", "")).strip()

    if not username or not new_password:
        return JsonResponse({"error": "username and new_password are required"}, status=400)

    user = User.objects.filter(username=username).first()
    if user is None:
        return JsonResponse({"error": "user not found"}, status=404)

    try:
        validate_password(new_password, user)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)

    user.set_password(new_password)
    user.save(update_fields=["password"])
    log_audit_event(
        request, action="user.set_password", status="success", details={"username": username}
    )
    return JsonResponse({"ok": True, "username": username})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def users_set_active_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    username = str(payload.get("username", "")).strip()
    is_active = bool(payload.get("is_active", True))

    if not username:
        return JsonResponse({"error": "username is required"}, status=400)

    if request.user.username == username and not is_active:
        return JsonResponse({"error": "You cannot deactivate your own account."}, status=400)

    user = User.objects.filter(username=username).first()
    if user is None:
        return JsonResponse({"error": "user not found"}, status=404)

    if get_user_role(user) == ROLE_SUPER_ADMIN and not is_active:
        super_admin_count = sum(
            1 for u in User.objects.all() if get_user_role(u) == ROLE_SUPER_ADMIN and u.is_active
        )
        if super_admin_count <= 1:
            return JsonResponse(
                {"error": "Cannot deactivate the last active SUPER_ADMIN user."}, status=400
            )

    user.is_active = is_active
    user.save(update_fields=["is_active"])
    log_audit_event(
        request,
        action="user.set_active",
        status="success",
        details={"username": username, "is_active": is_active},
    )
    return JsonResponse({"ok": True, "username": username, "is_active": is_active})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def users_delete_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    username = str(payload.get("username", "")).strip()

    if not username:
        return JsonResponse({"error": "username is required"}, status=400)

    if request.user.username == username:
        return JsonResponse({"error": "You cannot delete your own account."}, status=400)

    user = User.objects.filter(username=username).first()
    if user is None:
        return JsonResponse({"error": "user not found"}, status=404)

    if get_user_role(user) == ROLE_SUPER_ADMIN:
        super_admin_count = sum(
            1 for u in User.objects.all() if get_user_role(u) == ROLE_SUPER_ADMIN
        )
        if super_admin_count <= 1:
            return JsonResponse({"error": "Cannot delete the last SUPER_ADMIN user."}, status=400)

    user.delete()
    log_audit_event(request, action="user.delete", status="success", details={"username": username})
    return JsonResponse({"ok": True, "username": username})
