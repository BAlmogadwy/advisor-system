import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.services.audit import log_audit_event
from core.services.db_admin_ops import (
    create_backup_snapshot,
    delete_program_catalog,
    delete_students,
    import_program_plan,
    legacy_load_department_files_exact,
    preview_delete_program_catalog,
    preview_delete_students,
    run_integrity_checks,
)
from core.services.term_sections import (
    import_term_sections_from_csv,
    preview_term_sections_from_csv,
)
from core.sidebar_context import get_sidebar_context


@require_GET
def db_admin_page(request: HttpRequest) -> HttpResponse:
    return render(request, "core/db_admin.html", get_sidebar_context(request))


@require_GET
def db_preview_delete_students_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip() or None
    section = (request.GET.get("section") or "").strip() or None
    return JsonResponse(preview_delete_students(program=program, section=section))


@require_POST
def db_delete_students_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="db.delete_students",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse(
            {"error": "Confirmation required: send confirm=DELETE"}, status=400
        )

    program = str(payload.get("program", "")).strip() or None
    section = str(payload.get("section", "")).strip() or None
    result = delete_students(program=program, section=section)
    log_audit_event(
        request,
        action="db.delete_students",
        status="success",
        details={
            "program": program or "",
            "section": section or "",
            "deleted": result.get("deleted", 0),
        },
    )
    return JsonResponse(result)


@require_GET
def db_preview_delete_program_catalog_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip()
    if not program:
        return JsonResponse({"error": "program is required"}, status=400)
    return JsonResponse(preview_delete_program_catalog(program=program))


@require_POST
def db_delete_program_catalog_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="db.delete_program_catalog",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse(
            {"error": "Confirmation required: send confirm=DELETE"}, status=400
        )

    program = str(payload.get("program", "")).strip()
    if not program:
        log_audit_event(
            request,
            action="db.delete_program_catalog",
            status="error",
            error_text="missing program",
        )
        return JsonResponse({"error": "program is required"}, status=400)

    result = delete_program_catalog(program=program)
    log_audit_event(
        request,
        action="db.delete_program_catalog",
        status="success",
        details={
            "program": program,
            "deleted_requirements": result.get("deleted_requirements", 0),
            "deleted_prerequisites": result.get("deleted_prerequisites", 0),
        },
    )
    return JsonResponse(result)


@require_POST
def db_import_program_plan_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    program = str(payload.get("program", "")).strip()
    csv_text = str(payload.get("csv_text", ""))
    replace_existing = bool(payload.get("replace_existing", False))

    if not program:
        log_audit_event(
            request,
            action="db.import_program_plan",
            status="error",
            error_text="missing program",
        )
        return JsonResponse({"error": "program is required"}, status=400)
    if not csv_text.strip():
        log_audit_event(
            request,
            action="db.import_program_plan",
            status="error",
            details={"program": program},
            error_text="missing csv_text",
        )
        return JsonResponse({"error": "csv_text is required"}, status=400)

    try:
        result = import_program_plan(
            program=program,
            csv_text=csv_text,
            replace_existing=replace_existing,
        )
        log_audit_event(
            request,
            action="db.import_program_plan",
            status="success",
            details={
                "program": program,
                "replace_existing": replace_existing,
                "inserted": result.get("inserted", 0),
                "updated": result.get("updated", 0),
            },
        )
        return JsonResponse(result)
    except ValueError as exc:
        log_audit_event(
            request,
            action="db.import_program_plan",
            status="error",
            details={"program": program},
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def db_import_legacy_exact_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    req_path = str(payload.get("requirements_csv_path", "")).strip() or None
    pre_path = str(payload.get("prerequisites_csv_path", "")).strip() or None

    try:
        result = legacy_load_department_files_exact(
            requirements_csv_path=req_path,
            prerequisites_csv_path=pre_path,
        )
        log_audit_event(
            request,
            action="db.import_legacy_exact",
            status="success",
            details={"requirements_csv_path": req_path or "", "prerequisites_csv_path": pre_path or ""},
        )
        return JsonResponse(result)
    except ValueError as exc:
        log_audit_event(
            request,
            action="db.import_legacy_exact",
            status="error",
            details={"requirements_csv_path": req_path or "", "prerequisites_csv_path": pre_path or ""},
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def db_preview_term_sections_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}

    csv_path = str(payload.get("csv_path", "")).strip()
    academic_year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    is_department = bool(payload.get("is_department", False))

    if not csv_path or not academic_year or not term:
        return JsonResponse({"error": "csv_path, academic_year, and term are required"}, status=400)

    source_tag = "department" if is_department else "other"

    try:
        result = preview_term_sections_from_csv(
            csv_path=csv_path,
            academic_year=academic_year,
            term=term,
            source_tag=source_tag,
        )
        return JsonResponse(result)
    except (ValueError, FileNotFoundError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def db_import_term_sections_view(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body.decode("utf-8")) if request.body else {}

    csv_path = str(payload.get("csv_path", "")).strip()
    academic_year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    is_department = bool(payload.get("is_department", False))
    truncate_existing = bool(payload.get("truncate_existing", True))

    if not csv_path or not academic_year or not term:
        return JsonResponse({"error": "csv_path, academic_year, and term are required"}, status=400)

    source_tag = "department" if is_department else "other"

    try:
        result = import_term_sections_from_csv(
            csv_path=csv_path,
            academic_year=academic_year,
            term=term,
            source_tag=source_tag,
            truncate_existing_term=truncate_existing,
        )
        log_audit_event(
            request,
            action="db.import_term_sections",
            status="success",
            details={
                "csv_path": csv_path,
                "academic_year": academic_year,
                "term": term,
                "source_tag": source_tag,
                "truncate_existing": truncate_existing,
                "rows_for_term": result.get("rows_for_term", 0),
            },
        )
        return JsonResponse(result)
    except (ValueError, FileNotFoundError) as exc:
        log_audit_event(
            request,
            action="db.import_term_sections",
            status="error",
            details={"csv_path": csv_path, "academic_year": academic_year, "term": term, "source_tag": source_tag},
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def db_backup_snapshot_view(request: HttpRequest) -> JsonResponse:
    result = create_backup_snapshot()
    log_audit_event(
        request,
        action="db.backup_snapshot",
        status="success",
        details={"backup_path": result.get("backup_path", "")},
    )
    return JsonResponse(result)


@require_GET
def db_integrity_report_view(request: HttpRequest) -> JsonResponse:
    result = run_integrity_checks()
    log_audit_event(
        request,
        action="db.integrity_report",
        status="success",
        details={"ok": result.get("ok", False), "issues_count": len(result.get("issues", [])) if isinstance(result.get("issues"), list) else 0},
    )
    return JsonResponse(result)