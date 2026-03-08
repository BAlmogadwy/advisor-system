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
_eviction_counter: int = 0


def _do_eviction_sweep(window_seconds: int) -> None:
    """Periodically evict stale entries from _rate_buckets to prevent memory leaks."""
    global _eviction_counter
    _eviction_counter += 1
    if _eviction_counter < 100:
        return
    _eviction_counter = 0
    now = time.monotonic()
    cutoff = now - window_seconds
    stale = [k for k, v in _rate_buckets.items() if not v or v[-1] < cutoff]
    for k in stale:
        _rate_buckets.pop(k, None)


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
            # SUPER_ADMIN gets 5x the normal limit
            if request.user.is_authenticated:
                scope = get_user_scope(request.user)
                if scope.get("role") == ROLE_SUPER_ADMIN:
                    effective_max = max_calls * 5
                else:
                    effective_max = max_calls
            else:
                effective_max = max_calls

            uid = getattr(request.user, "pk", None) or "anon"
            key = f"throttle:{fn.__qualname__}:{uid}"
            now = time.monotonic()
            window_start = now - window_seconds

            # Prune expired entries and check count
            hits = _rate_buckets.get(key, [])
            hits = [t for t in hits if t > window_start]

            if len(hits) >= effective_max:
                retry_after = int(hits[0] - window_start) + 1
                resp = JsonResponse(
                    {"error": "Rate limit exceeded. Please try again later."},
                    status=429,
                )
                resp["Retry-After"] = str(retry_after)
                return resp

            hits.append(now)
            _rate_buckets[key] = hits

            # Periodic eviction of stale entries
            _do_eviction_sweep(window_seconds)

            return fn(request, *args, **kwargs)

        return wrapper

    return deco
