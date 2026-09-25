"""Exam student data export: a preflight that counts, and an audited download.

Both endpoints take their options as a CSRF-protected JSON body (never the
URL) and are open to Super Admins and the Exam Committee only. The preflight
answers counts and timetable facts - never a student ID or name - and is not
audited. The download is synchronous (see ``EXPORT_MODE``): gates, roster,
rows and ``data_sha256``, then a fail-closed audit row, then the workbook. If
the audit row cannot be written, no file is made.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO

from django.http import FileResponse, HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from core.exam_views import _require_exam_access
from core.models import ExamTimetableRun
from core.services.audit import (
    AuditUnavailable,
    audit_actor,
    log_audit_event,
    record_audit_event,
)
from core.services.exam_rosters import ExportRefused, build_roster_model
from core.services.exam_student_export import (
    ExportOptionsError,
    parse_export_options,
    preflight_summary,
    prepare_export,
    render_export,
)
from core.services.rbac import get_user_role

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
EXPORT_ACTION = "exam_timetable.export_students"
EXPORT_FAILED_ACTION = "exam_timetable.export_students_failed"

#: Measured 2026-09-25 on the local DB, whole timetable (20,372 rows), Full,
#: Arabic, openpyxl write-only without lxml (as deployed): 0.37 s roster +
#: 0.41 s rows + 3.8 s workbook = 4.6 s. Gunicorn's --timeout is 120 s, more
#: than 6x that, so every export is synchronous (owner decision 7).
EXPORT_MODE = "sync"

# One roster rebuild at a time per process: the four gthreads share 512 MB
# with CP-SAT solves, and a whole-run export holds ~40 MB while it works.
_EXPORT_SLOT = threading.BoundedSemaphore(1)
EXPORT_SLOT_WAIT_SECONDS = 20


class ExportSlotBusy(Exception):
    pass


@contextmanager
def _export_slot() -> Iterator[None]:
    if not _EXPORT_SLOT.acquire(timeout=EXPORT_SLOT_WAIT_SECONDS):
        raise ExportSlotBusy
    try:
        yield
    finally:
        _EXPORT_SLOT.release()


def _private(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _error(status: int, code: str, message: str, **extra) -> JsonResponse:
    return _private(
        JsonResponse({"ok": False, "code": code, "error": message, **extra}, status=status)
    )


def _json_body(request: HttpRequest) -> object:
    if len(request.body) > MAX_BODY_BYTES:
        raise ExportOptionsError("Export options are too large.", field="body")
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExportOptionsError("Export options must be JSON.", field="body") from exc


def _handled(exc: Exception) -> JsonResponse:
    if isinstance(exc, ExamTimetableRun.DoesNotExist):
        return _error(404, "not_found", "Run not found")
    if isinstance(exc, ExportRefused):
        return _error(exc.status, exc.code, str(exc))
    if isinstance(exc, ExportOptionsError):
        return _error(400, exc.code, str(exc), field=exc.field)
    if isinstance(exc, ExportSlotBusy):
        return _error(
            503, "export_slot_busy", "Another export is being prepared. Try again in a moment."
        )
    raise exc


@require_POST
def exam_student_export_preflight_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Check the lists against the saved run and count what a choice would export."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        payload = _json_body(request)
        run = ExamTimetableRun.objects.get(pk=run_id)
        with _export_slot():
            model = build_roster_model(run)
            options = parse_export_options(payload, model, require_scope=False)
            body = preflight_summary(
                model,
                options,
                pickers=payload.get("pickers") if isinstance(payload, dict) else None,
            )
    except (
        ExamTimetableRun.DoesNotExist,
        ExportRefused,
        ExportOptionsError,
        ExportSlotBusy,
    ) as exc:
        return _handled(exc)
    body["mode"] = EXPORT_MODE
    return _private(JsonResponse(body))


@require_POST
def exam_student_export_view(request: HttpRequest, run_id: int):
    """The download: audited before a single byte of the workbook is written."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        payload = _json_body(request)
        run = ExamTimetableRun.objects.get(pk=run_id)
        with _export_slot():
            model = build_roster_model(run)
            options = parse_export_options(payload, model)
            actor, role = audit_actor(request)
            prepared = prepare_export(
                model,
                options,
                generated_by=actor,
                generated_role=get_user_role(request.user),
            )
            try:
                entry_hash = record_audit_event(
                    actor_username=actor,
                    actor_role=role,
                    action=EXPORT_ACTION,
                    endpoint=request.path,
                    method=str(request.method),
                    status="success",
                    details=prepared.audit_details(),
                )
            except AuditUnavailable:
                return _error(
                    503,
                    "audit_unavailable",
                    "Couldn't record this export, so no file was made. Try again.",
                )
            reference = "EXR-" + entry_hash[:8].upper()
            try:
                content, filename, content_type = render_export(
                    prepared, reference=reference, audit_hash=entry_hash
                )
            except Exception:
                logger.exception("Student export %s failed after it was audited", reference)
                log_audit_event(
                    request,
                    action=EXPORT_FAILED_ACTION,
                    status="error",
                    details={"run_id": run.pk, "reference": reference},
                )
                return _error(
                    500,
                    "export_failed",
                    f"The file could not be made. Nothing was downloaded. Reference {reference}.",
                )
    except (
        ExamTimetableRun.DoesNotExist,
        ExportRefused,
        ExportOptionsError,
        ExportSlotBusy,
    ) as exc:
        return _handled(exc)
    response = FileResponse(
        BytesIO(content), as_attachment=True, filename=filename, content_type=content_type
    )
    response["X-Export-Reference"] = reference
    return _private(response)
