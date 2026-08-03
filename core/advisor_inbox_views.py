"""The adviser's side of an escalation.

Everything here follows the same rule as the student side: the permitted scope is
part of the query, never a check applied to rows already fetched. `visible_cases`
returns a queryset and every view starts from it, so a case belonging to somebody
else's student is not found rather than found-and-refused.

The two adviser text fields are kept apart at every level — column, endpoint,
serialiser and template. `adviser_notes` is correspondence ABOUT a student;
`resolution_message` is written TO them. One field serving both is how the first
request to show a student their outcome publishes the discussion that reached it.
"""

from __future__ import annotations

import json
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import AdvisorEscalation, AdvisorEscalationEvent, Student
from .services.advisor_inbox import (
    InboxError,
    adviser_label,
    check_transition,
    visible_cases,
)
from .services.rbac import get_user_scope
from .sidebar_context import get_sidebar_context

MAX_NOTE_CHARS = 4000


def _body(request: HttpRequest) -> tuple[dict[str, Any], JsonResponse | None]:
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}, JsonResponse({"error": "Invalid JSON body."}, status=400)
    if not isinstance(payload, dict):
        return {}, JsonResponse({"error": "Body must be a JSON object."}, status=400)
    return payload, None


def _record(
    case: AdvisorEscalation,
    request: HttpRequest,
    kind: str,
    *,
    from_status: str = "",
    to_status: str = "",
) -> None:
    """Append to the trail. Bodies are not copied in — they live on the case."""
    AdvisorEscalationEvent.objects.create(
        escalation=case,
        actor=request.user if request.user.is_authenticated else None,
        actor_label=adviser_label(request.user),
        kind=kind,
        from_status=from_status,
        to_status=to_status,
    )


def _student_rows(cases: list[AdvisorEscalation]) -> dict[int, dict[str, Any]]:
    """Programme and name for the queue, read LIVE.

    The evidence snapshot is frozen because it must show what the student was
    given; the queue is a working view and shows who they are today.
    """
    ids = {c.student_id for c in cases}
    return {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=ids).values(
            "student_id", "name", "program"
        )
    }


@require_GET
def inbox_view(request: HttpRequest) -> HttpResponse:
    """The queue, already narrowed to what this adviser may see."""
    cases = visible_cases(request.user).select_related("source_message")

    status = str(request.GET.get("status") or "").strip().upper()
    reason = str(request.GET.get("reason") or "").strip().upper()
    mine = request.GET.get("mine") == "1"
    program = str(request.GET.get("program") or "").strip().upper()

    if status:
        cases = cases.filter(status=status)
    if reason:
        cases = cases.filter(reason_code=reason)
    if mine:
        advisor_id = str(get_user_scope(request.user).get("advisor_id") or "").strip()
        # An empty advisor id must not silently mean "everything".
        cases = cases.filter(assigned_adviser_id=advisor_id) if advisor_id else cases.none()
    if program:
        cases = cases.filter(
            student_id__in=Student.objects.filter(program=program).values("student_id")
        )

    rows = list(cases[:200])
    students = _student_rows(rows)
    context = {
        **get_sidebar_context(request),
        "cases": [
            {
                "case": case,
                "student": students.get(case.student_id, {}),
                "question": (case.evidence_snapshot or {}).get("question", "")[:140],
            }
            for case in rows
        ],
        "statuses": AdvisorEscalation.Status.choices,
        "reasons": AdvisorEscalation.Reason.choices,
        "filters": {"status": status, "reason": reason, "mine": mine, "program": program},
    }
    return render(request, "core/advisor_inbox.html", context)


@require_GET
def inbox_case_view(request: HttpRequest, reference: str) -> HttpResponse:
    """One case, in four parts: what was asked, what was answered, why it came
    here, and what can be done about it."""
    case = get_object_or_404(
        visible_cases(request.user).select_related("source_message"),
        reference=str(reference or "").strip().upper(),
    )
    _record(case, request, AdvisorEscalationEvent.Kind.VIEWED)

    evidence = case.evidence_snapshot or {}
    student = Student.objects.filter(student_id=case.student_id).values("name", "program").first()
    context = {
        **get_sidebar_context(request),
        "case": case,
        "student": student or {},
        "evidence": evidence,
        "citations": evidence.get("citations") or [],
        "missing": evidence.get("missing_information") or [],
        "reason_codes": evidence.get("reason_codes") or [],
        "events": case.events.select_related("actor")[:100],
        "next_statuses": sorted(s for s in dict(AdvisorEscalation.Status.choices) if _can(case, s)),
    }
    return render(request, "core/advisor_inbox_case.html", context)


def _can(case: AdvisorEscalation, to_status: str) -> bool:
    try:
        check_transition(case, to_status)
    except InboxError:
        return False
    return True


@require_POST
def inbox_case_action_view(request: HttpRequest, reference: str) -> JsonResponse:
    """Assign, note, reply, or move the case on.

    One transaction over a locked row, because every action reads the current
    status and writes a new one — and two advisers picking up the same case at the
    same moment would otherwise both believe they had it.
    """
    payload, err = _body(request)
    if err:
        return err
    action = str(payload.get("action") or "").strip()

    with transaction.atomic():
        case = get_object_or_404(
            visible_cases(request.user).select_for_update(),
            reference=str(reference or "").strip().upper(),
        )
        scope = get_user_scope(request.user)
        advisor_id = str(scope.get("advisor_id") or "").strip()
        before = case.status

        if action == "assign_to_me":
            if not advisor_id:
                return JsonResponse(
                    {"error": "No adviser id is linked to your account."}, status=403
                )
            case.assigned_adviser_id = advisor_id
            if case.status == AdvisorEscalation.Status.OPEN:
                case.status = AdvisorEscalation.Status.ASSIGNED
            case.save(update_fields=["assigned_adviser_id", "status", "updated_at"])
            _record(
                case,
                request,
                AdvisorEscalationEvent.Kind.ASSIGNED,
                from_status=before,
                to_status=case.status,
            )

        elif action == "add_note":
            note = str(payload.get("text") or "").strip()[:MAX_NOTE_CHARS]
            if not note:
                return JsonResponse({"error": "The note is empty."}, status=400)
            # Appended, not replaced: a case history that can be overwritten is not
            # a history.
            case.adviser_notes = (case.adviser_notes + "\n\n" + note).strip()
            case.save(update_fields=["adviser_notes", "updated_at"])
            _record(case, request, AdvisorEscalationEvent.Kind.NOTE_ADDED)

        elif action == "record_response":
            reply = str(payload.get("text") or "").strip()[:MAX_NOTE_CHARS]
            if not reply:
                return JsonResponse({"error": "The reply is empty."}, status=400)
            # A DIFFERENT field from the notes above. The student reads this one.
            case.resolution_message = reply
            case.save(update_fields=["resolution_message", "updated_at"])
            _record(case, request, AdvisorEscalationEvent.Kind.RESPONSE_RECORDED)

        elif action == "set_status":
            to_status = str(payload.get("status") or "").strip().upper()
            try:
                check_transition(case, to_status)
            except InboxError as exc:
                return JsonResponse({"error": str(exc)}, status=409)
            case.status = to_status
            fields = ["status", "updated_at"]
            if to_status == AdvisorEscalation.Status.RESOLVED:
                case.resolved_at = timezone.now()
                case.resolved_by = request.user
                fields += ["resolved_at", "resolved_by"]
            case.save(update_fields=fields)
            _record(
                case,
                request,
                AdvisorEscalationEvent.Kind.STATUS_CHANGED,
                from_status=before,
                to_status=to_status,
            )

        else:
            return JsonResponse({"error": f"Unknown action: {action}"}, status=400)

    return JsonResponse(
        {
            "case": {
                "reference": case.reference,
                "status": case.status,
                "assigned_adviser_id": case.assigned_adviser_id,
                "has_response": bool(case.resolution_message.strip()),
            }
        }
    )
