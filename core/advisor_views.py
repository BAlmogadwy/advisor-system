import io

from django.core.management import call_command
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.services.advisors import (
    assign_students_to_advisors,
    ensure_students_advisor_column,
    list_academic_advisors,
    list_students_by_advisor,
    parse_student_advisor_csv,
    upsert_academic_advisor,
)
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope
from core.utils import parse_json_body as _parse_json_body


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def advisors_list_view(request: HttpRequest) -> JsonResponse:
    payload = list_academic_advisors()
    scope = get_user_scope(request.user)
    role = str(scope.get("role", ""))

    items = list(payload.get("items", []))
    if role == ROLE_SUPER_ADMIN:
        return JsonResponse(payload)

    if role == ROLE_GENERAL_ADVISOR:
        allowed = {str(x).upper() for x in scope.get("departments", [])}
        items = [
            a for a in items if any(str(d).upper() in allowed for d in a.get("departments", []))
        ]
    else:
        own = str(scope.get("advisor_id", "")).strip()
        items = [a for a in items if str(a.get("advisor_id", "")).strip() == own]

    return JsonResponse({"count": len(items), "items": items})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def advisor_upsert_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err

    advisor_id = str(payload.get("advisor_id", "")).strip()
    full_name = str(payload.get("full_name", "")).strip()
    email = str(payload.get("email", "")).strip()
    department = str(payload.get("department", "")).strip()

    if not advisor_id or not full_name or not email or not department:
        log_audit_event(
            request,
            action="advisor.upsert",
            status="error",
            details={"advisor_id": advisor_id, "email": email, "department": department},
            error_text="missing required fields",
        )
        return JsonResponse(
            {"error": "advisor_id, full_name, email, and department are required"},
            status=400,
        )

    try:
        result = upsert_academic_advisor(advisor_id, full_name, email, department)
        log_audit_event(
            request,
            action="advisor.upsert",
            status="success",
            details={"advisor_id": advisor_id, "email": email, "department": department},
        )
        return JsonResponse(result)
    except Exception as exc:
        msg = str(exc)
        log_audit_event(
            request,
            action="advisor.upsert",
            status="error",
            details={"advisor_id": advisor_id, "email": email, "department": department},
            error_text=msg,
        )
        if (
            "academic_advisors.email" in msg
            or "idx_academic_advisors_email" in msg
            or "UNIQUE constraint failed" in msg
        ):
            return JsonResponse({"error": "Email already exists for another advisor."}, status=400)
        if "advisor_id" in msg and "UNIQUE" in msg:
            return JsonResponse({"error": "Advisor ID already exists."}, status=400)
        return JsonResponse({"error": msg}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def ensure_students_advisor_column_view(request: HttpRequest) -> JsonResponse:
    result = ensure_students_advisor_column()
    log_audit_event(
        request,
        action="advisor.ensure_students_column",
        status="success",
        details=result,
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def assign_students_advisors_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err

    mappings = payload.get("mappings")
    csv_text = str(payload.get("csv_text", "")).strip()

    try:
        if isinstance(mappings, list):
            rows = mappings
        elif csv_text:
            rows = parse_student_advisor_csv(csv_text)
        else:
            log_audit_event(
                request,
                action="advisor.assign_students",
                status="error",
                details={"input": "none"},
                error_text="Provide mappings[] or csv_text",
            )
            return JsonResponse({"error": "Provide mappings[] or csv_text"}, status=400)

        result = assign_students_to_advisors(rows)
        log_audit_event(
            request,
            action="advisor.assign_students",
            status="success",
            details={
                "received": result.get("received", len(rows) if isinstance(rows, list) else 0),
                "updated": result.get("updated", 0),
                "errors_count": result.get("errors_count", 0),
            },
        )
        return JsonResponse(result)
    except ValueError as exc:
        log_audit_event(
            request,
            action="advisor.assign_students",
            status="error",
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@role_required(ROLE_ADVISOR)
@require_GET
def students_by_advisor_view(request: HttpRequest) -> JsonResponse:
    advisor_id = (request.GET.get("advisor_id") or "").strip()
    if not advisor_id:
        return JsonResponse({"error": "advisor_id is required"}, status=400)

    search = (request.GET.get("search") or "").strip() or None
    focus = (request.GET.get("focus") or "").strip() or None
    program_filter = (request.GET.get("program_filter") or "").strip() or None

    try:
        page = max(1, int(request.GET.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = max(0, min(int(request.GET.get("page_size", "50")), 500))
    except (ValueError, TypeError):
        page_size = 50

    scope = get_user_scope(request.user)
    role = str(scope.get("role", ""))
    forced_advisor_id = str(scope.get("advisor_id", "")).strip() if role != ROLE_SUPER_ADMIN else ""
    if forced_advisor_id and advisor_id != forced_advisor_id:
        return JsonResponse(
            {"error": "You can only access your assigned advisor portfolio."}, status=403
        )
    if role == ROLE_GENERAL_ADVISOR:
        allowed_departments = [str(x).upper() for x in scope.get("departments", [])]
    else:
        allowed_departments = None

    return JsonResponse(
        list_students_by_advisor(
            advisor_id,
            search=search,
            focus=focus,
            program_filter=program_filter,
            forced_advisor_id=forced_advisor_id,
            allowed_departments=allowed_departments,
            page=page,
            page_size=page_size,
        )
    )


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def seed_advisors_view(request: HttpRequest) -> JsonResponse:
    """Run the seed_advisors management command from the UI."""
    try:
        out = io.StringIO()
        call_command("seed_advisors", stdout=out)
        output = out.getvalue()
        log_audit_event(
            request,
            action="advisor.seed",
            status="ok",
            details={"output_length": len(output)},
        )
        return JsonResponse({"ok": True, "output": output})
    except Exception as exc:
        log_audit_event(
            request,
            action="advisor.seed",
            status="error",
            error_text=str(exc),
        )
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)
