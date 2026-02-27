from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from core.authz import role_required, throttle
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.scrape_ops import get_scrape_status, start_batch_scrape, stop_batch_scrape

# Allowed directory for CSV uploads (data/ under project root)
_ALLOWED_CSV_DIR = Path(settings.BASE_DIR) / "data"


def _validate_csv_path(raw_path: str) -> tuple[Path | None, str | None]:
    """Resolve *raw_path* and verify it lives under the allowed data directory.

    Returns (resolved_path, None) on success, or (None, error_message) on failure.
    """
    try:
        resolved = Path(raw_path).resolve(strict=False)
    except (OSError, ValueError) as exc:
        return None, f"Invalid path: {exc}"

    allowed_dir = _ALLOWED_CSV_DIR.resolve()

    # Must be under the allowed directory
    try:
        resolved.relative_to(allowed_dir)
    except ValueError:
        return None, "Path must be inside the data/ directory."

    if not resolved.name.endswith(".csv"):
        return None, "Only .csv files are accepted."

    if not resolved.is_file():
        return None, f"File not found: {resolved.name}"

    return resolved, None


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@role_required(ROLE_SUPER_ADMIN)
@require_GET
@throttle(max_calls=3, window_seconds=120)
def scrape_start_view(request: HttpRequest) -> JsonResponse:
    concurrency = _to_int(request.GET.get("concurrency"), 2)
    students_csv = request.GET.get("students_csv", "").strip() or None

    if students_csv is not None:
        path, error = _validate_csv_path(students_csv)
        if error:
            return JsonResponse({"ok": False, "error": error}, status=400)
        students_csv = str(path)

    result = start_batch_scrape(concurrency=concurrency, students_csv=students_csv)
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def scrape_status_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(get_scrape_status())


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def scrape_stop_view(request: HttpRequest) -> JsonResponse:
    result = stop_batch_scrape()
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)
