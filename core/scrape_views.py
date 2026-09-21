import csv
import io
import logging
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required, throttle
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.scrape_ops import get_scrape_status, start_batch_scrape, stop_batch_scrape
from core.services.scrape_student_source import inspect_database_student_source

# Allowed directory for CSV uploads (data/ under project root)
_ALLOWED_CSV_DIR = Path(settings.BASE_DIR) / "data"
logger = logging.getLogger(__name__)
_DATABASE_ROSTER_TOKEN_SALT = (  # nosec B105 -- public domain separator, not a credential
    "core.scrape.database-roster.v1"
)
_DATABASE_ROSTER_TOKEN_MAX_AGE_SECONDS = 15 * 60


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


def _reject_scrape_start(
    request: HttpRequest,
    *,
    error: str,
    error_code: str,
    status: int,
    audit_error_text: str | None = None,
) -> JsonResponse:
    """Audit a validated mutation rejection without retaining paths or tokens."""
    requested_source = (request.POST.get("student_source") or "csv").strip().lower()
    source = requested_source if requested_source in {"csv", "database"} else "invalid"
    log_audit_event(
        request,
        action="scrape.batch_start",
        status="error",
        details={"student_source": source[:32], "error_code": error_code},
        error_text=audit_error_text if audit_error_text is not None else error,
    )
    return JsonResponse(
        {"ok": False, "error": error, "error_code": error_code},
        status=status,
    )


@role_required(ROLE_SUPER_ADMIN)
@throttle(max_calls=3, window_seconds=120)
@require_POST
def scrape_start_view(request: HttpRequest) -> JsonResponse:
    """Start a guarded background scrape from CSV or the current DB roster."""
    try:
        concurrency = int(request.POST.get("concurrency") or "2")
    except (TypeError, ValueError):
        return _reject_scrape_start(
            request,
            error="concurrency must be an integer between 1 and 8",
            error_code="invalid_concurrency",
            status=400,
        )
    if concurrency < 1 or concurrency > 8:
        return _reject_scrape_start(
            request,
            error="concurrency must be between 1 and 8",
            error_code="invalid_concurrency",
            status=400,
        )

    student_source = (request.POST.get("student_source") or "csv").strip().lower()
    if student_source not in {"csv", "database"}:
        return _reject_scrape_start(
            request,
            error="student_source must be 'csv' or 'database'",
            error_code="invalid_student_source",
            status=400,
        )
    students_csv = request.POST.get("students_csv", "").strip() or None

    if student_source == "database" and students_csv is not None:
        return _reject_scrape_start(
            request,
            error="students_csv cannot be combined with the database student source",
            error_code="ambiguous_student_source",
            status=400,
        )
    expected_database_student_count: int | None = None
    expected_database_roster_sha256 = ""
    if student_source == "database":
        roster_token = (request.POST.get("database_roster_token") or "").strip()
        if not roster_token:
            return _reject_scrape_start(
                request,
                error="Refresh the database student roster before starting.",
                error_code="database_roster_token_required",
                status=409,
            )
        try:
            approved_roster = signing.loads(
                roster_token,
                salt=_DATABASE_ROSTER_TOKEN_SALT,
                max_age=_DATABASE_ROSTER_TOKEN_MAX_AGE_SECONDS,
            )
            expected_database_student_count = int(approved_roster["count"])
            expected_database_roster_sha256 = str(approved_roster["sha256"])
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            return _reject_scrape_start(
                request,
                error="The database student roster approval is invalid or expired; refresh it.",
                error_code="database_roster_token_invalid",
                status=409,
            )
    if student_source == "csv" and students_csv is not None:
        path, error = _validate_csv_path(students_csv)
        if error:
            return _reject_scrape_start(
                request,
                error=error,
                error_code="invalid_csv_path",
                status=400,
                audit_error_text="CSV path validation failed",
            )
        students_csv = str(path)

    result = start_batch_scrape(
        concurrency=concurrency,
        students_csv=students_csv,
        student_source=student_source,
        expected_database_student_count=expected_database_student_count,
        expected_database_roster_sha256=expected_database_roster_sha256,
    )
    result_params = result.get("params")
    params = result_params if isinstance(result_params, dict) else {}
    log_audit_event(
        request,
        action="scrape.batch_start",
        status="ok" if result.get("ok") else "error",
        details={
            "student_source": student_source,
            "student_count": params.get("student_count"),
            "concurrency": concurrency,
            "pid": result.get("pid"),
        },
        error_text=str(result.get("error") or ""),
    )
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def scrape_status_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(get_scrape_status())


@role_required(ROLE_SUPER_ADMIN)
@require_GET
def scrape_source_summary_view(request: HttpRequest) -> JsonResponse:
    try:
        summary = inspect_database_student_source()
    except Exception:
        logger.exception("Database student source summary failed")
        return JsonResponse(
            {"ok": False, "error": "Could not inspect the database student roster."},
            status=503,
        )
    return JsonResponse(
        {
            "ok": True,
            "database": {
                "total": summary["total"],
                "valid": summary["valid"],
                "excluded": summary["excluded"],
                "invalid": summary["invalid"],
                "ready": summary["ready"],
                "excluded_reasons": summary["excluded_reasons"],
                "roster_token": signing.dumps(
                    {
                        "count": summary["valid"],
                        "sha256": summary["roster_sha256"],
                    },
                    salt=_DATABASE_ROSTER_TOKEN_SALT,
                    compress=True,
                ),
            },
        }
    )


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def scrape_stop_view(request: HttpRequest) -> JsonResponse:
    """Stop the currently running batch scraper."""
    result = stop_batch_scrape()
    log_audit_event(
        request,
        action="scrape.batch_stop",
        status="ok" if result.get("ok") else "error",
        details={"pid": result.get("pid")},
        error_text=str(result.get("error") or ""),
    )
    code = 200 if result.get("ok") else 409
    return JsonResponse(result, status=code)


@role_required(ROLE_SUPER_ADMIN)
@require_POST
def oracle_students_csv_view(request: HttpRequest) -> JsonResponse:
    """Parse an Oracle student-list export and generate ``data/students_list.csv``.

    Accepts a multipart file upload (tab-separated, windows-1256) plus
    ``program`` and ``section`` fields.  Extracts student IDs from the file,
    writes ``data/students_list.csv`` with ``student_id,program,section``
    columns, and returns a summary.
    """
    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"ok": False, "error": "No file uploaded."}, status=400)

    if uploaded.size and uploaded.size > 5 * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "File too large (max 5 MB)."}, status=400)

    program = (request.POST.get("program") or "").strip()
    section = (request.POST.get("section") or "").strip()
    if not program or not section:
        return JsonResponse(
            {"ok": False, "error": "Both program and section are required."},
            status=400,
        )

    encoding = (request.POST.get("encoding") or "windows-1256").strip()

    raw = uploaded.read()
    try:
        text = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError) as exc:
        return JsonResponse({"ok": False, "error": f"Encoding error: {exc}"}, status=400)

    # Parse student IDs -------------------------------------------------------
    student_ids: list[str] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip repeating header rows
        if (
            "\u0627\u0644\u0639\u0627\u0645 \u0627\u0644\u062f\u0631\u0627\u0633\u064a" in line
        ):  # العام الدراسي
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            skipped += 1
            continue
        sid = fields[1].strip()
        if sid.isdigit() and len(sid) >= 5:
            student_ids.append(sid)
        else:
            skipped += 1

    if not student_ids:
        return JsonResponse(
            {"ok": False, "error": "No valid student IDs found in file."},
            status=400,
        )

    # Write data/students_list.csv --------------------------------------------
    out_dir = Path(settings.BASE_DIR) / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "students_list.csv"

    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["student_id", "program", "section"])
    for sid in student_ids:
        writer.writerow([sid, program, section])

    out_path.write_text(buf.getvalue(), encoding="utf-8")

    log_audit_event(
        request,
        action="scrape.oracle_students_csv",
        status="ok",
        details={
            "count": len(student_ids),
            "program": program,
            "section": section,
            "skipped": skipped,
        },
    )

    return JsonResponse(
        {
            "ok": True,
            "count": len(student_ids),
            "skipped": skipped,
            "path": "data/students_list.csv",
            "sample": student_ids[:5],
        }
    )
