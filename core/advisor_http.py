"""The four things every student-facing adviser endpoint does before it works.

Resolve who is asking, parse the body, refuse politely, and spend a unit of the
right budget. They were written once in the conversation views and would have been
written a second time in the planner views, which is how two endpoints end up
disagreeing about where identity comes from — and the copy that reads it from the
payload is the one nobody notices.
"""

from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, JsonResponse

from .services.advisor_principal import AdvisorPrincipal, IdentityError
from .services.rate_limit import consume as spend_budget


def student_principal(request: HttpRequest) -> AdvisorPrincipal | None:
    """The effective student, from the authenticated session ONLY.

    Never from the payload. A request that names a student id is describing what
    it wants, not who it is. Returns None so each view can answer 403 in its own
    shape; the principal itself fails closed by raising.
    """
    try:
        return AdvisorPrincipal.for_student(request)
    except IdentityError:
        return None


def json_body(request: HttpRequest) -> tuple[dict[str, Any], JsonResponse | None]:
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}, JsonResponse({"error": "تعذّرت قراءة الطلب."}, status=400)
    if not isinstance(payload, dict):
        return {}, JsonResponse({"error": "صيغة الطلب غير صحيحة."}, status=400)
    return payload, None


def forbidden() -> JsonResponse:
    return JsonResponse({"error": "هذه الخدمة متاحة للطلاب المسجَّل دخولهم فقط."}, status=403)


def over_budget(budget: str, student_id: int) -> JsonResponse | None:
    """Spend one unit, or explain how long to wait.

    Budgets are named for the RESOURCE. Generation is the expensive one and every
    door onto it — a new question, a retry of a failed turn, a planner rebuild —
    draws on the same allowance, so no door becomes a way around the others.
    """
    decision = spend_budget(budget, student_id)
    if decision.allowed:
        return None
    response = JsonResponse(
        {
            "error": "لقد أرسلت طلبات كثيرة. يرجى المحاولة بعد قليل.",
            "retry_after": decision.retry_after,
        },
        status=429,
    )
    response["Retry-After"] = str(decision.retry_after)
    return response


__all__ = ["forbidden", "json_body", "over_budget", "student_principal"]
