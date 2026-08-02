import json
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required, throttle
from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.audit import log_audit_event
from core.services.local_llm import LocalLLMError, check_local_llm_health
from core.services.policy import require_student_scope
from core.services.rbac import ROLE_ADVISOR, ROLE_STUDENT, get_user_scope
from core.services.virtual_advisor import answer_virtual_advisor, run_planned_tools
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context


def _json_body(request: HttpRequest) -> tuple[dict[str, Any] | None, JsonResponse | None]:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "JSON body must be an object"}, status=400)
    return payload, None


def _optional_int(value: Any, field: str) -> tuple[int | None, JsonResponse | None]:
    if value in (None, ""):
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, JsonResponse({"error": f"{field} must be an integer"}, status=400)


@login_required(login_url="login")
@role_required(ROLE_ADVISOR)
def virtual_advisor_page(request: HttpRequest) -> HttpResponse:
    defaults = load_defaults()
    context = {
        **get_sidebar_context(request),
        "llm_base_url": str(getattr(settings, "LOCAL_LLM_BASE_URL", "")),
        "llm_default_model": str(getattr(settings, "LOCAL_LLM_MODEL", "")),
        "academic_year": defaults["academic_year"],
        "term": defaults["term"],
    }
    return render(request, "core/virtual_advisor.html", context)


# Staff only: this makes an outbound call to the model host, so an ungated version
# lets any signed-in student aim the deployment's own LLM connection.
@login_required(login_url="login")
@role_required(ROLE_ADVISOR)
@require_GET
@throttle(max_calls=30, window_seconds=60)
def virtual_advisor_health_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(check_local_llm_health())


@login_required(login_url="login")
@role_required(ROLE_ADVISOR)
@require_POST
@throttle(max_calls=30, window_seconds=60)
def virtual_advisor_tool_preview_view(request: HttpRequest) -> JsonResponse:
    payload, err = _json_body(request)
    if err:
        return err
    assert payload is not None

    question = str(payload.get("message", "")).strip()
    if not question:
        return JsonResponse({"error": "message is required"}, status=400)
    if len(question) > 4000:
        return JsonResponse({"error": "message is too long"}, status=400)

    results = run_planned_tools(question, scope=get_user_scope(request.user))
    return JsonResponse({"ok": True, "tool_results": results})


# Staff only. This is the OPERATOR console, and it reaches the same generator as
# the student adviser on a separate, looser budget — so without a role gate a
# student who has spent their generation allowance could simply call this one from
# the browser console. The ROLE_STUDENT branch inside is left in place as a second
# lock: if this decorator is ever removed, the clamp to their own record still holds.
@login_required(login_url="login")
@role_required(ROLE_ADVISOR)
@require_POST
@throttle(max_calls=12, window_seconds=60)
def virtual_advisor_chat_view(request: HttpRequest) -> JsonResponse:
    payload, err = _json_body(request)
    if err:
        return err
    assert payload is not None

    question = str(payload.get("message", "")).strip()
    if not question:
        return JsonResponse({"error": "message is required"}, status=400)
    if len(question) > 4000:
        return JsonResponse({"error": "message is too long"}, status=400)

    scope = get_user_scope(request.user)
    if str(scope.get("role", "")) == ROLE_STUDENT:
        # A student's identity is never taken from the request: their own principal
        # forces their own id and ignores whatever the payload claims.
        try:
            principal = AdvisorPrincipal.for_student(request)
        except IdentityError as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        student_id = principal.student_id
    else:
        student_id, err = _optional_int(payload.get("student_id"), "student_id")
        if err:
            return err
        principal = None  # built below, once the subject has been authorised

    defaults = load_defaults()
    academic_year, err = _optional_int(
        payload.get("academic_year", defaults["academic_year"]), "academic_year"
    )
    if err:
        return err
    term, err = _optional_int(payload.get("term", defaults["term"]), "term")
    if err:
        return err

    if student_id is not None:
        scope_err = require_student_scope(request, student_id)
        if scope_err:
            return scope_err

    if principal is None:
        # Built only AFTER the subject was authorised: the principal names whose
        # record loads, and must never be the thing that decides it may.
        try:
            principal = AdvisorPrincipal.for_staff(request, student_id)
        except IdentityError as exc:
            return JsonResponse({"error": str(exc)}, status=403)

    model = str(payload.get("model", "")).strip() or None
    if model and len(model) > 220:
        return JsonResponse({"error": "model is too long"}, status=400)

    try:
        result = answer_virtual_advisor(
            question=question,
            principal=principal,
            academic_year=academic_year,
            term=term,
            history=payload.get("history", []),
            model=model,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    except LocalLLMError as exc:
        return JsonResponse({"error": str(exc), "source": "local_llm"}, status=503)

    agent_meta = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    log_audit_event(
        request,
        action="virtual_advisor.chat",
        status="ok",
        details={
            "student_id": student_id,
            "has_student_context": student_id is not None,
            "question_chars": len(question),
            "answer_chars": len(str(result.get("answer", ""))),
            "model": result.get("model"),
            "agent_loop_used": bool(agent_meta.get("loop_used")),
            "agent_iterations": agent_meta.get("iterations"),
            "agent_tools_called": [
                str(item.get("name"))
                for item in agent_meta.get("tools_called", [])
                if isinstance(item, dict)
            ],
        },
    )
    return JsonResponse(result)
