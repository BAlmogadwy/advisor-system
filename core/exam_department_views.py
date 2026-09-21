"""Additional department downloads; the existing master export is unchanged."""

from __future__ import annotations

import json
from io import BytesIO

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.exam_views import _require_exam_access
from core.models import ExamTimetableRun
from core.services.exam_department_export import (
    DepartmentExportUnavailable,
    department_export_options,
    export_department_workbooks,
)
from core.services.exam_run_schema import load_normalised_run


@require_GET
def exam_department_options_view(request, run_id: int):
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        run = ExamTimetableRun.objects.get(pk=run_id)
        options = department_export_options(
            load_normalised_run(run), request.GET.get("language", "en")
        )
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
    except DepartmentExportUnavailable as exc:
        return JsonResponse({"ok": False, "code": exc.code, "error": str(exc)}, status=409)
    return JsonResponse({"ok": True, **options})


@require_POST
def exam_department_export_view(request, run_id: int):
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        if len(request.body) > 32768:
            raise ValueError("Export options are too large.")
        payload = json.loads(request.body)
        run = ExamTimetableRun.objects.get(pk=run_id)
        content, filename, content_type = export_department_workbooks(run, payload)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
    except DepartmentExportUnavailable as exc:
        return JsonResponse({"ok": False, "code": exc.code, "error": str(exc)}, status=409)
    except (ValueError, UnicodeDecodeError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    response = FileResponse(
        BytesIO(content), as_attachment=True, filename=filename, content_type=content_type
    )
    response["Cache-Control"] = "private, no-store"
    return response
