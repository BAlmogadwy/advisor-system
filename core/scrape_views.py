from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from core.services.scrape_ops import get_scrape_status, start_batch_scrape, stop_batch_scrape


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@require_GET
def scrape_start_view(request: HttpRequest) -> JsonResponse:
    concurrency = _to_int(request.GET.get("concurrency"), 2)
    students_csv = request.GET.get("students_csv")
    result = start_batch_scrape(concurrency=concurrency, students_csv=students_csv)
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)


@require_GET
def scrape_status_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(get_scrape_status())


@require_GET
def scrape_stop_view(request: HttpRequest) -> JsonResponse:
    result = stop_batch_scrape()
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)
