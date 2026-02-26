from django.db.utils import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.services.audit import export_audit_logs_csv, query_audit_logs, validate_hash_chain
from core.services.rbac import ROLE_SUPER_ADMIN
from core.sidebar_context import get_sidebar_context


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def _excel_csv_response(filename: str, content: str) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    response.write(content)
    return response


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def audit_explorer_page(request: HttpRequest) -> HttpResponse:
    action = (request.GET.get("action") or "").strip() or None
    actor_username = (request.GET.get("user") or "").strip() or None
    status = (request.GET.get("status") or "").strip() or None
    from_utc = (request.GET.get("from") or "").strip() or None
    to_utc = (request.GET.get("to") or "").strip() or None
    limit = _safe_int(request.GET.get("limit"), 200)

    try:
        rows = query_audit_logs(
            action=action,
            actor_username=actor_username,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            limit=limit,
        )
        chain = validate_hash_chain(limit=2000)
    except OperationalError:
        rows = []
        chain = {"ok": False, "checked": 0, "error": "audit table missing"}

    return render(
        request,
        "core/audit_explorer.html",
        {
            **get_sidebar_context(request),
            "rows": rows,
            "filters": {
                "action": action or "",
                "user": actor_username or "",
                "status": status or "",
                "from": from_utc or "",
                "to": to_utc or "",
                "limit": limit,
            },
            "chain": chain,
        },
    )


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def audit_explorer_api(request: HttpRequest) -> JsonResponse:
    action = (request.GET.get("action") or "").strip() or None
    actor_username = (request.GET.get("user") or "").strip() or None
    status = (request.GET.get("status") or "").strip() or None
    from_utc = (request.GET.get("from") or "").strip() or None
    to_utc = (request.GET.get("to") or "").strip() or None
    limit = _safe_int(request.GET.get("limit"), 200)

    try:
        rows = query_audit_logs(
            action=action,
            actor_username=actor_username,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            limit=limit,
        )
        chain = validate_hash_chain(limit=2000)
    except OperationalError:
        rows = []
        chain = {"ok": False, "checked": 0, "error": "audit table missing"}
    return JsonResponse({"count": len(rows), "items": rows, "chain": chain})


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def audit_export_csv_view(request: HttpRequest) -> HttpResponse:
    action = (request.GET.get("action") or "").strip() or None
    actor_username = (request.GET.get("user") or "").strip() or None
    status = (request.GET.get("status") or "").strip() or None
    from_utc = (request.GET.get("from") or "").strip() or None
    to_utc = (request.GET.get("to") or "").strip() or None
    limit = _safe_int(request.GET.get("limit"), 2000)

    try:
        rows = query_audit_logs(
            action=action,
            actor_username=actor_username,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            limit=limit,
        )
        csv_text = export_audit_logs_csv(rows)
    except OperationalError:
        csv_text = "id,ts_utc,actor_username,actor_role,action,status,endpoint,method,error_text\n"
    return _excel_csv_response("audit_explorer.csv", csv_text)
