import csv
import io
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required, throttle
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.scrape_ops import get_scrape_status, start_batch_scrape, stop_batch_scrape
from core.utils import parse_json_body as _parse_json_body

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
@require_POST
@throttle(max_calls=3, window_seconds=120)
def scrape_start_view(request: HttpRequest) -> JsonResponse:
    payload, err = _parse_json_body(request)
    if err:
        return err
    concurrency = _to_int(str(payload.get("concurrency", "")), 2)
    students_csv = str(payload.get("students_csv", "")).strip() or None

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
@require_POST
def scrape_stop_view(request: HttpRequest) -> JsonResponse:
    result = stop_batch_scrape()
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

    buf = io.StringIO()
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
