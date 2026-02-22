from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponseBase, JsonResponse

from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope

ROLE_ORDER = {
    ROLE_ADVISOR: 1,
    ROLE_GENERAL_ADVISOR: 2,
    ROLE_SUPER_ADMIN: 3,
}


def role_required(
    min_role: str,
) -> Callable[[Callable[..., HttpResponseBase]], Callable[..., HttpResponseBase]]:
    def deco(fn: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
        @wraps(fn)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required"}, status=401)
            scope = get_user_scope(request.user)
            role = str(scope.get("role", ROLE_ADVISOR))
            if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(min_role, 0):
                return JsonResponse(
                    {"error": f"Insufficient role: requires {min_role}"}, status=403
                )
            return fn(request, *args, **kwargs)

        return wrapper

    return deco
