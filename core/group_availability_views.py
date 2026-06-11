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

from core.authz import throttle
from core.services.group_availability import (
    MAX_STUDENTS,
    compute_group_availability,
)
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


@login_required(login_url="login")
def group_availability_page(request: HttpRequest) -> HttpResponse:
    context = {
        **get_sidebar_context(request),
        "max_students": MAX_STUDENTS,
    }
    return render(request, "core/group_availability.html", context)


@login_required(login_url="login")
@require_POST
@throttle(max_calls=30, window_seconds=60)
def group_availability_compute_view(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "JSON body must be an object"}, status=400)

    ids = _parse_student_ids(payload.get("student_ids"))
    if not ids:
        return JsonResponse({"error": "Provide at least one numeric student ID."}, status=400)

    # Term is auto-detected (the students' current timetable) — no year/term input.
    result = compute_group_availability(ids)
    return JsonResponse(result)
