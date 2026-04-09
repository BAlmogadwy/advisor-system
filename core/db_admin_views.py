import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.models import ProgrammeRequirement
from core.services.audit import log_audit_event
from core.services.db_admin_ops import (
    create_backup_snapshot,
    delete_external_courses,
    delete_program_catalog,
    delete_students,
    import_elective_catalogue,
    import_oracle_plan_from_rows,
    import_program_plan,
    legacy_load_department_files_exact,
    list_external_courses,
    preview_delete_program_catalog,
    preview_delete_students,
    preview_oracle_plan,
    run_integrity_checks,
    set_elective_term_mapping,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.term_sections import (
    import_term_sections_from_csv,
    preview_term_sections_from_csv,
)
from core.sidebar_context import get_sidebar_context
from core.utils import parse_json_body as _parse_json_body
from core.utils import validate_import_path


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_admin_page(request: HttpRequest) -> HttpResponse:
    return render(request, "core/db_admin.html", get_sidebar_context(request))


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_preview_delete_students_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip() or None
    section = (request.GET.get("section") or "").strip() or None
    return JsonResponse(preview_delete_students(program=program, section=section))


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_delete_students_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="db.delete_students",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse({"error": "Confirmation required: send confirm=DELETE"}, status=400)

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
            "deleted_students": result.get("students_count", 0),
            "deleted_student_courses": result.get("student_courses_count", 0),
        },
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_preview_delete_program_catalog_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip()
    if not program:
        return JsonResponse({"error": "program is required"}, status=400)
    return JsonResponse(preview_delete_program_catalog(program=program))


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_delete_program_catalog_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="db.delete_program_catalog",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse({"error": "Confirmation required: send confirm=DELETE"}, status=400)

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
            "deleted_requirements": result.get("requirements_count", 0),
            "deleted_prerequisites": result.get("prerequisites_count", 0),
        },
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_import_program_plan_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
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
                "rows_upserted": result.get("rows_upserted", 0),
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


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_import_legacy_exact_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    req_path = str(payload.get("requirements_csv_path", "")).strip() or None
    pre_path = str(payload.get("prerequisites_csv_path", "")).strip() or None

    if req_path:
        resolved, err_msg = validate_import_path(req_path)
        if err_msg:
            return JsonResponse({"error": f"requirements_csv_path: {err_msg}"}, status=400)
        req_path = str(resolved)
    if pre_path:
        resolved, err_msg = validate_import_path(pre_path)
        if err_msg:
            return JsonResponse({"error": f"prerequisites_csv_path: {err_msg}"}, status=400)
        pre_path = str(resolved)

    try:
        result = legacy_load_department_files_exact(
            requirements_csv_path=req_path,
            prerequisites_csv_path=pre_path,
        )
        log_audit_event(
            request,
            action="db.import_legacy_exact",
            status="success",
            details={
                "requirements_csv_path": req_path or "",
                "prerequisites_csv_path": pre_path or "",
            },
        )
        return JsonResponse(result)
    except ValueError as exc:
        log_audit_event(
            request,
            action="db.import_legacy_exact",
            status="error",
            details={
                "requirements_csv_path": req_path or "",
                "prerequisites_csv_path": pre_path or "",
            },
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_preview_term_sections_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err

    csv_path = str(payload.get("csv_path", "")).strip()
    academic_year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    is_department = bool(payload.get("is_department", False))

    if not csv_path or not academic_year or not term:
        return JsonResponse({"error": "csv_path, academic_year, and term are required"}, status=400)

    resolved, err_msg = validate_import_path(csv_path)
    if err_msg:
        return JsonResponse({"error": f"csv_path: {err_msg}"}, status=400)
    csv_path = str(resolved)

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


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_import_term_sections_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err

    csv_path = str(payload.get("csv_path", "")).strip()
    academic_year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    is_department = bool(payload.get("is_department", False))
    truncate_existing = bool(payload.get("truncate_existing", True))

    if not csv_path or not academic_year or not term:
        return JsonResponse({"error": "csv_path, academic_year, and term are required"}, status=400)

    resolved, err_msg = validate_import_path(csv_path)
    if err_msg:
        return JsonResponse({"error": f"csv_path: {err_msg}"}, status=400)
    csv_path = str(resolved)

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
            details={
                "csv_path": csv_path,
                "academic_year": academic_year,
                "term": term,
                "source_tag": source_tag,
            },
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_preview_oracle_plan_view(request: HttpRequest) -> JsonResponse:
    uploaded_file = request.FILES.get("file")
    program = request.POST.get("program", "").strip()
    encoding = request.POST.get("encoding", "windows-1256").strip() or "windows-1256"

    if not uploaded_file:
        return JsonResponse({"error": "file is required"}, status=400)
    if not program:
        return JsonResponse({"error": "program is required"}, status=400)

    # File size check: 5 MB max
    max_size = 5 * 1024 * 1024
    if uploaded_file.size and uploaded_file.size > max_size:
        return JsonResponse(
            {"error": f"File too large ({uploaded_file.size} bytes). Maximum is 5 MB."},
            status=400,
        )

    # File extension validation
    allowed_extensions = {".csv", ".tsv", ".txt", ".dat"}
    file_name = uploaded_file.name or ""
    file_ext = ("." + file_name.rsplit(".", 1)[-1]).lower() if "." in file_name else ""
    if file_ext not in allowed_extensions:
        return JsonResponse(
            {
                "error": f"Unsupported file extension '{file_ext}'. Allowed: {', '.join(sorted(allowed_extensions))}"
            },
            status=400,
        )

    try:
        raw_bytes = uploaded_file.read()
        content = raw_bytes.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        return JsonResponse({"error": f"Encoding error: {exc}"}, status=400)

    try:
        result = preview_oracle_plan(
            program=program,
            encoding=encoding,
            content=content,
        )
        return JsonResponse(result)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_import_oracle_plan_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    program = str(payload.get("program", "")).strip()
    rows = payload.get("rows", [])
    replace_existing = bool(payload.get("replace_existing", False))

    if not program:
        log_audit_event(
            request,
            action="db.import_oracle_plan",
            status="error",
            error_text="missing program",
        )
        return JsonResponse({"error": "program is required"}, status=400)
    if not rows:
        log_audit_event(
            request,
            action="db.import_oracle_plan",
            status="error",
            details={"program": program},
            error_text="no rows provided",
        )
        return JsonResponse({"error": "rows are required"}, status=400)

    try:
        result = import_oracle_plan_from_rows(
            program=program,
            rows=rows,
            replace_existing=replace_existing,
        )
        log_audit_event(
            request,
            action="db.import_oracle_plan",
            status="success",
            details={
                "program": program,
                "replace_existing": replace_existing,
                "requirements_upserted": result.get("requirements_upserted", 0),
                "prerequisites_inserted": result.get("prerequisites_inserted", 0),
                "courses_upserted": result.get("courses_upserted", 0),
            },
        )
        return JsonResponse(result)
    except ValueError as exc:
        log_audit_event(
            request,
            action="db.import_oracle_plan",
            status="error",
            details={"program": program},
            error_text=str(exc),
        )
        return JsonResponse({"error": str(exc)}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_backup_snapshot_view(request: HttpRequest) -> JsonResponse:
    result = create_backup_snapshot()
    log_audit_event(
        request,
        action="db.backup_snapshot",
        status="success",
        details={"backup_file": result.get("backup_file", "")},
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_integrity_report_view(request: HttpRequest) -> JsonResponse:
    result = run_integrity_checks()
    log_audit_event(
        request,
        action="db.integrity_report",
        status="success",
        details={
            "ok": result.get("ok", False),
            "issues_count": len(result.get("issues", []))
            if isinstance(result.get("issues"), list)
            else 0,
        },
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_list_external_courses_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(list_external_courses())


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_delete_external_courses_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="db.delete_external_courses",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse({"error": "Confirmation required: send confirm=DELETE"}, status=400)

    raw_ids = payload.get("course_ids")
    try:
        course_ids = [int(x) for x in raw_ids] if isinstance(raw_ids, list) else None
    except (ValueError, TypeError):
        return JsonResponse({"error": "course_ids must be a list of integers"}, status=400)

    result = delete_external_courses(course_ids=course_ids)
    log_audit_event(
        request,
        action="db.delete_external_courses",
        status="success",
        details={
            "course_ids": course_ids,
            "courses_deleted": result.get("courses_deleted", 0),
        },
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def db_programme_capacities_view(request: HttpRequest) -> JsonResponse:
    """List ProgrammeRequirement rows for a given program with max_capacity."""
    program = (request.GET.get("program") or "").strip()
    if not program:
        return JsonResponse({"error": "program is required"}, status=400)
    rows = list(
        ProgrammeRequirement.objects.filter(program=program)
        .values("course_code", "credit_hours", "max_capacity")
        .order_by("course_code")
    )
    return JsonResponse({"ok": True, "rows": rows})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def db_update_programme_capacities_view(request: HttpRequest) -> JsonResponse:
    """Bulk-update max_capacity for ProgrammeRequirement rows of a program."""
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    program = str(body.get("program", "")).strip()
    capacities = body.get("capacities", {})  # {"CS101": 30, "AI201": null}

    if not program:
        return JsonResponse({"ok": False, "error": "program required"}, status=400)

    updated = 0
    for code, cap in capacities.items():
        val = int(cap) if cap is not None and str(cap).strip() != "" else None
        if val is not None and val < 1:
            continue
        count = ProgrammeRequirement.objects.filter(program=program, course_code=code).update(
            max_capacity=val
        )
        updated += count

    log_audit_event(
        request,
        action="db.update_programme_capacities",
        status="success",
        details={
            "program": program,
            "updated": updated,
            "entries": len(capacities),
        },
    )
    return JsonResponse({"ok": True, "updated": updated})


# ── Elective Catalogue & Mapping ────────────────────────────────────


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def elective_catalogue_import_view(request: HttpRequest) -> JsonResponse:
    """Import elective course catalogue from tab-separated content.

    Expects JSON body: ``{"programme": "AI", "content": "...tsv..."}``
    TSV columns: Code, Name, Req (prerequisites), Cat, T, L, C
    """
    payload = _parse_json_body(request)
    if isinstance(payload, JsonResponse):
        return payload

    programme = str(payload.get("programme", "")).strip().upper()
    content = str(payload.get("content", "")).strip()
    if not programme or not content:
        return JsonResponse({"error": "programme and content required"}, status=400)

    import csv as _csv

    rows = []
    reader = _csv.reader(content.splitlines(), delimiter="\t")
    for line in reader:
        if len(line) < 2:
            continue
        code = line[0].strip()
        if not code or code.upper() == "CODE":
            continue  # skip header
        name = line[1].strip() if len(line) > 1 else ""
        prereqs = line[2].strip() if len(line) > 2 else ""
        category = line[3].strip() if len(line) > 3 else ""
        credit_hours = int(line[6]) if len(line) > 6 and line[6].strip().isdigit() else 3
        rows.append(
            {
                "course_code": code,
                "course_name": name,
                "prerequisites": prereqs,
                "category": category,
                "credit_hours": credit_hours,
            }
        )

    result = import_elective_catalogue(programme, rows)
    log_audit_event(
        request,
        action="elective.catalogue.import",
        status="success",
        details=result,
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def elective_catalogue_list_view(request: HttpRequest) -> JsonResponse:
    """List all elective courses for a programme."""
    from core.models import ElectiveCourse

    programme = (request.GET.get("programme") or "").strip().upper()
    qs = ElectiveCourse.objects.all()
    if programme:
        qs = qs.filter(programme=programme)

    data = list(
        qs.order_by("programme", "course_code").values(
            "id",
            "course_code",
            "course_name",
            "programme",
            "category",
            "credit_hours",
            "prerequisites_csv",
        )
    )
    return JsonResponse({"ok": True, "items": data, "count": len(data)})


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def elective_mapping_set_view(request: HttpRequest) -> JsonResponse:
    """Set elective-to-placeholder mappings for a term.

    Expects JSON: ``{"academic_year": "1448", "term": 1, "programme": "AI",
    "mappings": [{"placeholder_code": "AI1", "course_code": "AI461"}, ...]}``
    """
    payload = _parse_json_body(request)
    if isinstance(payload, JsonResponse):
        return payload

    year = str(payload.get("academic_year", "")).strip()
    term = payload.get("term")
    programme = str(payload.get("programme", "")).strip().upper()
    mappings = payload.get("mappings", [])

    if not year or term is None or not programme:
        return JsonResponse({"error": "academic_year, term, programme required"}, status=400)

    result = set_elective_term_mapping(year, int(term), programme, mappings)
    log_audit_event(
        request,
        action="elective.mapping.set",
        status="success",
        details=result,
    )
    return JsonResponse(result)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def elective_mapping_list_view(request: HttpRequest) -> JsonResponse:
    """List current elective mappings for a term/programme."""
    from core.models import ElectiveTermMapping

    year = (request.GET.get("academic_year") or "").strip()
    term = request.GET.get("term")
    programme = (request.GET.get("programme") or "").strip().upper()

    qs = ElectiveTermMapping.objects.select_related("elective").all()
    if year:
        qs = qs.filter(academic_year=year)
    if term:
        qs = qs.filter(term=int(term))
    if programme:
        qs = qs.filter(programme=programme)

    data = []
    for m in qs.order_by("programme", "placeholder_code", "elective__course_code"):
        data.append(
            {
                "id": m.id,
                "academic_year": m.academic_year,
                "term": m.term,
                "programme": m.programme,
                "placeholder_code": m.placeholder_code,
                "course_code": m.elective.course_code,
                "course_name": m.elective.course_name,
                "prerequisites_csv": m.elective.prerequisites_csv,
            }
        )
    return JsonResponse({"ok": True, "items": data, "count": len(data)})
