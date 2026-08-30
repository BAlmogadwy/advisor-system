"""Views for the group-availability (common free-slot) finder.

A registrar pastes a list of student IDs and gets an aggregated weekly busy
grid for the group, so they can pick a teaching slot that is free for everyone
before opening a new course section. See
``core.services.group_availability`` for the aggregation logic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.authz import role_required, throttle
from core.services.group_availability import (
    MAX_STUDENTS,
    compute_group_availability,
)
from core.services.rbac import ROLE_ADVISOR
from core.sidebar_context import get_sidebar_context


def _parse_student_ids(raw: Any) -> list[int]:
    """Extract student IDs from a JSON list or a free-text blob.

    The UI sends a textarea, so accept either an explicit list or a string
    where IDs are separated by commas, spaces, or newlines. Any run of digits
    is treated as one ID.
    """
    if isinstance(raw, list):
        ids: list[int] = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return ids
    if isinstance(raw, str | int):
        return [int(tok) for tok in re.findall(r"\d+", str(raw))]
    return []


def _parse_json_payload(request: HttpRequest) -> tuple[dict[str, Any] | None, JsonResponse | None]:
    """Parse the shared compute/export JSON envelope."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "JSON body must be an object"}, status=400)
    return payload, None


@login_required(login_url="login")
@role_required(ROLE_ADVISOR)  # registrar tool: never a student (exposes arbitrary students' grids)
def group_availability_page(request: HttpRequest) -> HttpResponse:
    context = {
        **get_sidebar_context(request),
        "max_students": MAX_STUDENTS,
    }
    return render(request, "core/group_availability.html", context)


@login_required(login_url="login")
@role_required(ROLE_ADVISOR)  # registrar tool: never a student
@require_POST
@throttle(max_calls=30, window_seconds=60)
def group_availability_compute_view(request: HttpRequest) -> JsonResponse:
    payload, error = _parse_json_payload(request)
    if error is not None:
        return error
    assert payload is not None

    ids = _parse_student_ids(payload.get("student_ids"))
    if not ids:
        return JsonResponse({"error": "Provide at least one numeric student ID."}, status=400)

    # Term is auto-detected (the students' current timetable) — no year/term input.
    result = compute_group_availability(ids)
    return JsonResponse(result)


@login_required(login_url="login")
@role_required(ROLE_ADVISOR)  # same arbitrary-student scope as the workspace
@require_POST
@throttle(max_calls=10, window_seconds=60)
def group_availability_export_xlsx_view(request: HttpRequest) -> HttpResponse:
    """Recompute the current group and download every availability grid as XLSX."""
    payload, error = _parse_json_payload(request)
    if error is not None:
        return error
    assert payload is not None

    ids = _parse_student_ids(payload.get("student_ids"))
    if not ids:
        return JsonResponse({"error": "Provide at least one numeric student ID."}, status=400)

    # Recompute from authoritative registrar data. Never accept client-supplied
    # cells: they may be stale or tampered with, while the workbook must match
    # the same rules as a fresh on-screen calculation.
    result = compute_group_availability(ids)
    try:
        from core.services.group_availability_export import build_group_availability_xlsx

        content = build_group_availability_xlsx(result)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    year = re.sub(r"[^0-9A-Za-z_-]+", "_", str(result.get("academic_year") or "current"))
    term = re.sub(r"[^0-9A-Za-z_-]+", "_", str(result.get("term") or "current"))
    filename = f"group_availability_{year}_T{term}.xlsx"
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
