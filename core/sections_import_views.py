from __future__ import annotations

import time as _time
import uuid
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.services.audit import log_audit_event
from core.services.oracle_sections_parser import extract_rows_from_oracle_html, write_rows_to_csv
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.term_sections import import_term_sections_from_csv
from core.sidebar_context import get_sidebar_context
from core.utils import parse_json_body as _parse_json_body


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def sections_import_page(request: HttpRequest) -> HttpResponse:
    return render(request, "core/sections_import.html", get_sidebar_context(request))


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def sections_import_preview_view(request: HttpRequest) -> JsonResponse:
    upload = request.FILES.get("oracle_file")
    is_department = (request.POST.get("is_department") or "").lower() in {"1", "true", "yes", "on"}

    if not upload:
        return JsonResponse({"error": "oracle_file is required"}, status=400)

    source_tag = "department" if is_department else "other"

    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Clean up old temp files (older than 1 hour)
    for old_file in temp_dir.glob("*"):
        try:
            if _time.time() - old_file.stat().st_mtime > 3600:
                old_file.unlink()
        except OSError:
            pass

    token = uuid.uuid4().hex
    html_path = temp_dir / f"{token}.html"
    csv_path = temp_dir / f"{token}.csv"

    with html_path.open("wb") as f:
        for chunk in upload.chunks():
            f.write(chunk)

    try:
        rows = extract_rows_from_oracle_html(html_path)
        write_rows_to_csv(rows, csv_path)
    except Exception as exc:
        return JsonResponse({"error": f"Parse failed: {exc}"}, status=400)

    preview_rows = rows[:300]
    return JsonResponse(
        {
            "token": token,
            "source_tag": source_tag,
            "total_rows": len(rows),
            "preview_count": len(preview_rows),
            "preview_rows": preview_rows,
        }
    )


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def sections_import_insert_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    token = str(payload.get("token", "")).strip()
    is_department = bool(payload.get("is_department", False))
    trunc_raw = payload.get("truncate_existing", True)
    if isinstance(trunc_raw, str):
        truncate_existing = trunc_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        truncate_existing = bool(trunc_raw)

    if not token:
        return JsonResponse({"error": "token is required"}, status=400)

    source_tag = "department" if is_department else "other"
    csv_path = Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.csv"
    if not csv_path.exists():
        return JsonResponse(
            {"error": "Preview token not found or expired. Please parse again."}, status=400
        )

    try:
        result = import_term_sections_from_csv(
            csv_path=csv_path,
            source_tag=source_tag,
            truncate_existing_term=truncate_existing,
        )
        log_audit_event(
            request,
            action="sections_import.insert",
            status="success",
            details={
                "source_tag": source_tag,
                "rows_total": result.get("rows_total", 0),
            },
        )

        # Clean up temp files after successful import
        try:
            csv_path.unlink(missing_ok=True)
            html_path = csv_path.with_suffix(".html")
            html_path.unlink(missing_ok=True)
        except OSError:
            pass

        return JsonResponse(result)
    except Exception as exc:
        log_audit_event(
            request, action="sections_import.insert", status="error", error_text=str(exc)
        )
        return JsonResponse({"error": str(exc)}, status=400)
