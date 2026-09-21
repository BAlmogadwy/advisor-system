from __future__ import annotations

import hmac
import json
import re
import time as _time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.models import ProgrammeRequirement, TermSectionProgram
from core.services.audit import log_audit_event
from core.services.oracle_sections_parser import (
    extract_rows_from_oracle_html,
    write_rows_to_csv,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.section_programmes import normalize_section_program
from core.services.term_sections import (
    import_term_sections_from_csv,
    preview_term_sections_from_csv,
)
from core.sidebar_context import get_sidebar_context
from core.utils import parse_json_body as _parse_json_body

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ORACLE_UPLOAD_BYTES = 10 * 1024 * 1024
_ALLOWED_ORACLE_UPLOAD_SUFFIXES = {".html", ".htm"}
_PREVIEW_TOKEN_MAX_AGE_SECONDS = 60 * 60


def _normalize_programs(values: Iterable[object]) -> list[str]:
    return sorted(
        {normalized for value in values if (normalized := normalize_section_program(value))}
    )


def _available_program_codes() -> list[str]:
    linked = TermSectionProgram.objects.values_list("program", flat=True).distinct()
    configured = ProgrammeRequirement.objects.values_list("program", flat=True).distinct()
    # Include configured programmes even before their first section link exists,
    # while retaining any valid historical link not present in requirements.
    return _normalize_programs([*linked, *configured])


def _temp_paths(token: str) -> tuple[Path, Path, Path]:
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    return (
        temp_dir / f"{token}.html",
        temp_dir / f"{token}.csv",
        temp_dir / f"{token}.json",
    )


def _cleanup_temp_token(token: str) -> None:
    for path in _temp_paths(token):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_expired_preview_tokens() -> None:
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    if not temp_dir.exists():
        return

    expired_tokens: set[str] = set()
    for old_file in temp_dir.glob("*"):
        try:
            if _time.time() - old_file.stat().st_mtime <= _PREVIEW_TOKEN_MAX_AGE_SECONDS:
                continue
            if _TOKEN_RE.fullmatch(old_file.stem):
                expired_tokens.add(old_file.stem)
            else:
                old_file.unlink()
        except OSError:
            pass
    for expired_token in expired_tokens:
        _cleanup_temp_token(expired_token)


def _confirmation_phrase(preview: dict[str, object]) -> str:
    impact = preview.get("impact")
    sections = impact.get("sections_unique", 0) if isinstance(impact, dict) else 0
    try:
        count = max(0, int(sections))
    except (TypeError, ValueError):
        count = 0
    return f"IMPORT {count}"


def _write_preview_metadata(
    path: Path,
    *,
    source_tag: str,
    default_programs: list[str],
    preview: dict[str, object],
) -> None:
    metadata = {
        "version": 1,
        "created_at_epoch": _time.time(),
        "source_tag": source_tag,
        "default_programs": default_programs,
        "confirmation_phrase": _confirmation_phrase(preview),
        "preview_fingerprint": preview.get("preview_fingerprint"),
    }
    path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")


def _read_preview_metadata(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def sections_import_page(request: HttpRequest) -> HttpResponse:
    _cleanup_expired_preview_tokens()
    context = get_sidebar_context(request)
    context["section_program_options"] = _available_program_codes()
    return render(request, "core/sections_import.html", context)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def sections_import_preview_view(request: HttpRequest) -> JsonResponse:
    upload = request.FILES.get("oracle_file")
    is_department = _as_bool(request.POST.get("is_department"))

    if not upload:
        return JsonResponse({"error": "oracle_file is required"}, status=400)
    suffix = Path(str(upload.name or "")).suffix.lower()
    if suffix not in _ALLOWED_ORACLE_UPLOAD_SUFFIXES:
        return JsonResponse(
            {"error": "Oracle section exports must be an .html or .htm file"},
            status=400,
        )
    upload_size = upload.size
    if upload_size is None or upload_size <= 0:
        return JsonResponse({"error": "The uploaded Oracle file is empty"}, status=400)
    if upload_size > _MAX_ORACLE_UPLOAD_BYTES:
        return JsonResponse(
            {"error": "The Oracle file exceeds the 10 MB upload limit"},
            status=400,
        )

    try:
        default_programs = _normalize_programs(request.POST.getlist("default_programs"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    source_tag = "department" if is_department else "other"

    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Preview tokens expire after one hour. Valid failed inserts are retained so
    # an administrator can retry without re-uploading.
    _cleanup_expired_preview_tokens()

    token = uuid.uuid4().hex
    html_path, csv_path, metadata_path = _temp_paths(token)

    try:
        with html_path.open("wb") as output:
            for chunk in upload.chunks():
                output.write(chunk)
        rows = extract_rows_from_oracle_html(html_path)
        write_rows_to_csv(rows, csv_path)
        preview = preview_term_sections_from_csv(
            csv_path,
            source_tag=source_tag,
            default_programs=default_programs,
        )
        preview_fingerprint = str(preview.get("preview_fingerprint", ""))
        if not _FINGERPRINT_RE.fullmatch(preview_fingerprint):
            raise ValueError("Preview did not produce a valid database fingerprint")
        # This screen parses Oracle HTML, which has no programme column. Require
        # the administrator to make the ownership assignment explicit even when
        # every matching section already happens to have a stored membership.
        phrase = _confirmation_phrase(preview)
        has_sections = phrase != "IMPORT 0"
        preview["program_selection_required"] = not bool(default_programs)
        preview["can_import"] = bool(
            default_programs and has_sections and preview.get("can_import")
        )
        preview["confirmation_phrase"] = phrase
        preview["token"] = token
        _write_preview_metadata(
            metadata_path,
            source_tag=source_tag,
            default_programs=default_programs,
            preview=preview,
        )
        return JsonResponse(preview)
    except Exception as exc:
        _cleanup_temp_token(token)
        return JsonResponse({"error": f"Parse failed: {exc}"}, status=400)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def sections_import_insert_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err

    token = str(payload.get("token", "")).strip()
    raw_programs = payload.get("default_programs")
    if not isinstance(raw_programs, list):
        return JsonResponse(
            {"error": "default_programs must be a list of programme codes"},
            status=400,
        )
    try:
        default_programs = _normalize_programs(raw_programs)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if not default_programs:
        return JsonResponse(
            {
                "error": "Choose at least one programme and run Preview again before importing.",
                "code": "programs_required",
            },
            status=400,
        )
    if not _TOKEN_RE.fullmatch(token):
        return JsonResponse({"error": "A valid preview token is required"}, status=400)
    if _as_bool(payload.get("truncate_existing", False)):
        return JsonResponse(
            {
                "error": (
                    "Replacement mode is disabled. Use DB Admin > Clear Current "
                    "Sections before running this merge import."
                )
            },
            status=400,
        )

    is_department = _as_bool(payload.get("is_department", False))
    source_tag = "department" if is_department else "other"
    _html_path, csv_path, metadata_path = _temp_paths(token)
    metadata = _read_preview_metadata(metadata_path)
    if not csv_path.exists() or metadata is None:
        return JsonResponse(
            {"error": "Preview token not found or expired. Please parse again."},
            status=400,
        )
    try:
        created_at = float(metadata.get("created_at_epoch", metadata_path.stat().st_mtime))
        csv_mtime = float(csv_path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        created_at = 0.0
        csv_mtime = 0.0
    if _time.time() - min(created_at, csv_mtime) > _PREVIEW_TOKEN_MAX_AGE_SECONDS:
        _cleanup_temp_token(token)
        return JsonResponse(
            {
                "error": "Preview token expired. Upload the file and run Preview again.",
                "code": "preview_expired",
            },
            status=410,
        )

    preview_programs = metadata.get("default_programs")
    if not isinstance(preview_programs, list):
        return JsonResponse(
            {"error": "Preview metadata is invalid or expired. Please parse again."},
            status=400,
        )
    preview_fingerprint = str(metadata.get("preview_fingerprint", ""))
    if not _FINGERPRINT_RE.fullmatch(preview_fingerprint):
        return JsonResponse(
            {"error": "Preview metadata is invalid or expired. Please parse again."},
            status=400,
        )
    try:
        normalized_preview_programs = _normalize_programs(preview_programs)
    except ValueError:
        normalized_preview_programs = []
    if source_tag != metadata.get("source_tag") or default_programs != normalized_preview_programs:
        return JsonResponse(
            {
                "error": (
                    "The file source or programme selection changed after Preview. "
                    "Run Preview again before importing."
                ),
                "code": "preview_changed",
            },
            status=400,
        )

    backup: dict[str, Any] | None = None
    try:
        # Never trust the browser's impact/count. Rebuild the preview against the
        # live database immediately before validating the typed confirmation.
        fresh_preview = preview_term_sections_from_csv(
            csv_path,
            source_tag=source_tag,
            default_programs=default_programs,
        )
        fresh_fingerprint = str(fresh_preview.get("preview_fingerprint", ""))
        if not _FINGERPRINT_RE.fullmatch(fresh_fingerprint):
            raise ValueError("Fresh preview did not produce a valid database fingerprint")
        if not hmac.compare_digest(fresh_fingerprint, preview_fingerprint):
            return JsonResponse(
                {
                    "error": (
                        "Section data changed after Preview. Run Preview again and "
                        "review the updated impact before importing."
                    ),
                    "code": "preview_stale",
                },
                status=409,
            )
        confirmation_phrase = _confirmation_phrase(fresh_preview)
        if confirmation_phrase == "IMPORT 0" or not fresh_preview.get("can_import"):
            return JsonResponse(
                {
                    "error": (
                        "This preview can no longer be imported safely. Run Preview "
                        "again and review the updated impact."
                    ),
                    "code": "preview_stale",
                },
                status=409,
            )

        confirmation = payload.get("confirmation")
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            confirmation,
            confirmation_phrase,
        ):
            return JsonResponse(
                {
                    "error": f"Type {confirmation_phrase} exactly to confirm this import.",
                    "code": "confirmation_mismatch",
                    "confirmation_phrase": confirmation_phrase,
                },
                status=400,
            )

        result = import_term_sections_from_csv(
            csv_path=csv_path,
            source_tag=source_tag,
            truncate_existing_term=False,
            default_programs=default_programs,
            expected_preview_fingerprint=preview_fingerprint,
            backup_before_import=True,
        )
        backup_result = result.get("backup")
        if not isinstance(backup_result, dict) or backup_result.get("ok") is not True:
            raise RuntimeError("Import completed without valid backup metadata")
        backup = backup_result
        log_audit_event(
            request,
            action="sections_import.insert",
            status="success",
            details={
                "source_tag": source_tag,
                "default_programs": default_programs,
                "rows_total": result.get("rows_total", 0),
                "confirmation_phrase": confirmation_phrase,
                "mode": "merge",
                "backup": backup,
            },
        )

        # Preserve the preview token on every failure so the administrator can
        # retry. Successful imports consume it and remove all three temp files.
        _cleanup_temp_token(token)

        return JsonResponse(result)
    except Exception as exc:
        log_audit_event(
            request,
            action="sections_import.insert",
            status="error",
            error_text=str(exc),
            details={
                "source_tag": source_tag,
                "default_programs": default_programs,
                "backup": backup,
            },
        )
        message = str(exc)
        if "Import preview is stale; run preview again" in message:
            return JsonResponse(
                {"error": message, "code": "preview_stale"},
                status=409,
            )
        return JsonResponse({"error": message}, status=400)
