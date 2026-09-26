"""Student lists: the page, its navigator facts, scope rosters and the student lookup.

Open to Super Admins and the Exam Committee only (each view checks the role;
the committee middleware allow-lists them by name). The rules:

* **Student data only through POST** (CSRF-protected, body size capped): the
  scope roster and the lookup. The one GET answers navigator facts - counts
  and timetable facts, never a student - and so is not audited.
* **Nothing about a student in a URL.** Routes carry the saved run id only;
  a scope is timetable facts and travels in the body with the student ID and
  the search text. No view reads a student from the query string.
* **Audited first, fail-closed.** A scope load or a settled lookup writes its
  row with ``record_audit_event`` BEFORE any row is serialized; if the row
  cannot be written the answer is 503 and carries no student data.
* **The phase-1 gates**, unchanged: 404 for a missing run, 409
  ``rebuild_required`` / ``lists_unavailable`` / ``lists_term_mismatch``.
* **Viewing cache** (``exam_roster_view.ROSTER_VIEW_CACHE``): 60 s per run and
  process, bypassed by ``refresh``. Downloads stay on the phase-1 export
  endpoint, which always rebuilds.
"""

from __future__ import annotations

import json
import re
from functools import partial

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.exam_student_export_views import _error, _handled, _private
from core.exam_views import _require_exam_access
from core.models import ExamTimetableRun
from core.services.audit import AuditUnavailable, audit_actor, record_audit_event
from core.services.exam_roster_view import (
    ROSTER_VIEW_CACHE,
    RosterBusy,
    RosterRequestError,
    compact_check,
    freshness,
    lookup_audit_details,
    lookup_payload,
    lookup_sittings,
    parse_lookup,
    parse_view_scope,
    scope_audit_details,
    scope_payload,
    search_payload,
    search_students,
    select_scope,
)
from core.services.exam_rosters import ExportRefused
from core.services.exam_student_export import ExportOptionsError
from core.sidebar_context import get_sidebar_context

#: A scope is a handful of timetable facts; a lookup one ID or a short search.
MAX_BODY_BYTES = 4 * 1024
VIEW_ACTION = "exam_timetable.roster_view"
LOOKUP_ACTION = "exam_timetable.roster_lookup"
_RUN_PARAM = re.compile(r"[0-9]{1,18}")


def _json_body(request: HttpRequest) -> dict:
    """The request's JSON object, refused before reading when it is too large."""
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES or len(request.body) > MAX_BODY_BYTES:
        raise RosterRequestError("The request is too large.", field="body")
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RosterRequestError("The request must be JSON.", field="body") from exc
    if not isinstance(payload, dict):
        raise RosterRequestError("The request must be a JSON object.", field="body")
    refresh = payload.get("refresh", False)
    if not isinstance(refresh, bool):
        raise RosterRequestError("Refresh must be true or false.", field="refresh")
    return payload


def _run(run_id: int) -> ExamTimetableRun:
    # The saved JSON (~1.4 MB) is read only when the cache has to build.
    return ExamTimetableRun.objects.only("id", "label", "created_at").get(pk=run_id)


def _answer(body: dict) -> JsonResponse:
    # Compact separators: a whole-run navigator is hundreds of rooms and exams.
    response = JsonResponse(body, json_dumps_params={"separators": (",", ":")})
    _private(response)
    return response


def _busy() -> JsonResponse:
    return _error(503, "roster_busy", "The student lists are being checked. Try again in a moment.")


def _refused(exc: Exception) -> JsonResponse:
    if isinstance(exc, RosterBusy):
        return _busy()
    return _handled(exc)


_REFUSALS = (
    ExamTimetableRun.DoesNotExist,
    ExportRefused,
    ExportOptionsError,
    RosterBusy,
)


def _audit(request: HttpRequest, action: str, details: dict) -> None:
    actor, role = audit_actor(request)
    record_audit_event(
        actor_username=actor,
        actor_role=role,
        action=action,
        endpoint=request.path,
        method=str(request.method),
        status="success",
        details=details,
    )


# ── The page ───────────────────────────────────────────────────


@require_GET
def exam_rosters_page(request: HttpRequest) -> HttpResponse:
    """The Student lists page shell for one saved run (``?run=<id>``, else the newest).

    It reads no student and runs no gate: the page asks the navigator
    endpoint, which answers the gates in the page's own words.
    """
    deny = _require_exam_access(request)
    if deny:
        return deny
    raw = request.GET.get("run", "")
    runs = ExamTimetableRun.objects.only("id", "label", "created_at")
    status, state, run = 200, "ready", None
    if not raw:
        run = runs.order_by("-created_at", "-id").first()
        state = "ready" if run else "no_saved_run"
    elif _RUN_PARAM.fullmatch(raw):
        run = runs.filter(pk=int(raw)).first()
    if raw and run is None:
        status, state = 404, "run_not_found"
    context = get_sidebar_context(request)
    context.update(roster_run=run, roster_state=state)
    return render(request, "core/exam_rosters.html", context, status=status)


# ── Navigator facts (GET, no student) ──────────────────────────


@require_GET
def exam_roster_index_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Days, periods, rooms and exams of a saved run with counts: no student data."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        run = _run(run_id)
        view, cached = ROSTER_VIEW_CACHE.get(run, refresh=request.GET.get("refresh") == "1")
    except _REFUSALS as exc:
        return _refused(exc)
    return _answer({"ok": True, **freshness(view, cached), **view.navigator})


# ── Scope roster (POST, audited) ───────────────────────────────


@require_POST
def exam_roster_detail_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """The students of one room, course or section, audited before they are sent."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        payload = _json_body(request)
        run = _run(run_id)
        view, cached = ROSTER_VIEW_CACHE.get(run, refresh=payload.get("refresh") is True)
        options = parse_view_scope(payload, view.model)
        sittings = select_scope(view, options)
    except _REFUSALS as exc:
        return _refused(exc)
    try:
        _audit(request, VIEW_ACTION, scope_audit_details(view, options, sittings, cached))
    except AuditUnavailable:
        return _error(
            503,
            "audit_unavailable",
            "Couldn't record this access, so the list wasn't shown. Try again.",
        )
    body = {
        "ok": True,
        **freshness(view, cached),
        "check": compact_check(view),
        **scope_payload(view, options, sittings),
    }
    return _answer(body)


# ── Student lookup and search (POST, audited) ──────────────────


@require_POST
def exam_roster_lookup_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """One student's exams by exact ID, or Find suggestions: each settled ask audited."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        payload = _json_body(request)
        lookup = parse_lookup(payload)
        run = _run(run_id)
        view, cached = ROSTER_VIEW_CACHE.get(run, refresh=payload.get("refresh") is True)
    except _REFUSALS as exc:
        return _refused(exc)
    if lookup.mode == "student":
        sittings = lookup_sittings(view, lookup.student_id)
        matched = [lookup.student_id] if sittings else []
        total = len(matched)
        render_result = partial(lookup_payload, view, sittings)
    else:
        matched, total = search_students(view, lookup.query)
        render_result = partial(search_payload, view, matched, total)
    try:
        _audit(request, LOOKUP_ACTION, lookup_audit_details(view, lookup, matched, total, cached))
    except AuditUnavailable:
        return _error(
            503,
            "audit_unavailable",
            "Couldn't record this lookup, so nothing was shown. Try again.",
        )
    result = render_result()
    body = {"ok": True, **freshness(view, cached), "check": compact_check(view), **result}
    return _answer(body)
