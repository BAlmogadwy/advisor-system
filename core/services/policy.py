from django.http import HttpRequest, JsonResponse

from core.models import Student
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope


def _policy_deny(
    request: HttpRequest,
    *,
    action: str,
    reason_code: str,
    error: str,
    status: int,
    details: dict[str, object] | None = None,
) -> JsonResponse:
    payload: dict[str, object] = {"error": error, "reason_code": reason_code, "decision": "deny"}
    if details:
        payload.update(details)

    log_audit_event(
        request,
        action=action,
        status="deny",
        details={"reason_code": reason_code, **(details or {})},
        error_text=error,
    )
    return JsonResponse(payload, status=status)


def _policy_allow(
    request: HttpRequest,
    *,
    action: str,
    reason_code: str,
    details: dict[str, object] | None = None,
) -> None:
    log_audit_event(
        request,
        action=action,
        status="allow",
        details={"reason_code": reason_code, **(details or {})},
    )


def allowed_programs_for_request(request: HttpRequest) -> set[str] | None:
    scope = get_user_scope(request.user)
    role = str(scope.get("role", ""))

    if role == ROLE_SUPER_ADMIN:
        return None

    if role == ROLE_GENERAL_ADVISOR:
        allowed = {str(x).upper() for x in scope.get("departments", []) if str(x).strip()}
        return allowed or None

    own_advisor = str(scope.get("advisor_id", "")).strip()
    if not own_advisor:
        return set()

    programs = Student.objects.filter(
        advisor_id=own_advisor,
    ).exclude(
        program__isnull=True,
    ).exclude(
        program="",
    ).values_list("program", flat=True).distinct()
    return {str(p).strip().upper() for p in programs if p is not None}


def require_program_scope(
    request: HttpRequest,
    program: str | None,
    *,
    require_program_for_scoped: bool = True,
) -> JsonResponse | None:
    allowed = allowed_programs_for_request(request)
    if allowed is None:
        _policy_allow(
            request,
            action="policy.program_scope",
            reason_code="PROGRAM_SCOPE_BYPASS_SUPER_ADMIN",
            details={"program": (program or "").strip().upper() or None},
        )
        return None

    p = (program or "").strip().upper()
    if require_program_for_scoped and not p:
        return _policy_deny(
            request,
            action="policy.program_scope",
            reason_code="PROGRAM_SCOPE_MISSING_PROGRAM",
            error="program is required for your role scope.",
            status=400,
            details={"allowed_programs": sorted(allowed)},
        )

    if p and p not in allowed:
        return _policy_deny(
            request,
            action="policy.program_scope",
            reason_code="PROGRAM_SCOPE_OUTSIDE_ALLOWED",
            error="program is outside your role scope.",
            status=403,
            details={"allowed_programs": sorted(allowed), "program": p},
        )

    _policy_allow(
        request,
        action="policy.program_scope",
        reason_code="PROGRAM_SCOPE_ALLOWED",
        details={"program": p or None},
    )
    return None


def require_student_scope(request: HttpRequest, student_id: int) -> JsonResponse | None:
    row = Student.objects.filter(student_id=student_id).values_list("advisor_id", "program").first()
    if not row:
        return _policy_deny(
            request,
            action="policy.student_scope",
            reason_code="STUDENT_SCOPE_NOT_FOUND",
            error=f"Student not found: {student_id}",
            status=404,
            details={"student_id": student_id},
        )

    student_advisor_id = "" if row[0] is None else str(row[0]).strip()
    student_program = "" if row[1] is None else str(row[1]).strip().upper()

    scope = get_user_scope(request.user)
    role = str(scope.get("role", ""))

    if role == ROLE_ADVISOR:
        own_advisor = str(scope.get("advisor_id", "")).strip()
        if not own_advisor or own_advisor != student_advisor_id:
            return _policy_deny(
                request,
                action="policy.student_scope",
                reason_code="STUDENT_SCOPE_ADVISOR_MISMATCH",
                error="Student is outside your advisor scope.",
                status=403,
                details={"student_id": student_id},
            )

    if role == ROLE_GENERAL_ADVISOR:
        allowed = {str(x).upper() for x in scope.get("departments", []) if str(x).strip()}
        if allowed and student_program not in allowed:
            return _policy_deny(
                request,
                action="policy.student_scope",
                reason_code="STUDENT_SCOPE_DEPARTMENT_MISMATCH",
                error="Student is outside your department scope.",
                status=403,
                details={"allowed_programs": sorted(allowed), "student_id": student_id},
            )

    _policy_allow(
        request,
        action="policy.student_scope",
        reason_code="STUDENT_SCOPE_ALLOWED",
        details={"student_id": student_id, "student_program": student_program or None},
    )
    return None
