import time
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


# ---------------------------------------------------------------------------
# Lightweight per-user rate limiting (no extra dependency)
# ---------------------------------------------------------------------------
# In-process sliding-window store.  Acceptable for single-process / gunicorn
# deployments.  For multi-process horizontal scale, swap to Django cache.
_rate_buckets: dict[str, list[float]] = {}


def throttle(
    max_calls: int = 10,
    window_seconds: int = 60,
) -> Callable[[Callable[..., HttpResponseBase]], Callable[..., HttpResponseBase]]:
    """Decorator that rate-limits per (user, endpoint) using a sliding window.

    Usage::

        @login_required
        @throttle(max_calls=5, window_seconds=60)
        def expensive_view(request): ...
    """

    def deco(fn: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
        @wraps(fn)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            uid = getattr(request.user, "pk", None) or "anon"
            key = f"throttle:{fn.__qualname__}:{uid}"
            now = time.monotonic()
            window_start = now - window_seconds

            # Prune expired entries and check count
            hits = _rate_buckets.get(key, [])
            hits = [t for t in hits if t > window_start]

            if len(hits) >= max_calls:
                retry_after = int(hits[0] - window_start) + 1
                resp = JsonResponse(
                    {"error": "Rate limit exceeded. Please try again later."},
                    status=429,
                )
                resp["Retry-After"] = str(retry_after)
                return resp

            hits.append(now)
            _rate_buckets[key] = hits
            return fn(request, *args, **kwargs)

        return wrapper

    return deco
