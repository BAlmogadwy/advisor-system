"""The student's actions on one timetable-feedback case: answer a request for
information, add a message, withdraw a point or the whole case, and accept an
answer or reopen it.

Each is a POST that calls the workflow service and, when it is taken, redirects
back to the case with a one-time notice (post/redirect/get). A refusal draws the
case page again: with the service's status, its sentence at the top and at the
field it is about, and the text the student typed. The case page offers an action
only where the service would take it, but the service still decides, because a
case can change between the page and the press. A withdrawal asks first, on a page
of its own, so it works without JavaScript and never happens by a stray tap.

WHO. As on every feedback page: the student is the session's, and a case is found
by their id and its reference together, so another student's case is not found.
Nothing here reads TIMETABLE_FEEDBACK_ACCEPTING or the pilot programmes: they
close the sending of new feedback, not the conversation about feedback sent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from core.models import TimetableFeedback
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_workflow as workflow
from core.student_timetable_feedback_views import (
    NOTICE_TAG,
    _context,
    _normalise_newlines,
    _own_case,
    _point_view,
    _student,
    _unavailable,
    case_page,
)

#: "Was the answer helpful?" as the form sends it. Anything else is no answer: the
#: service stores what it is given, and a stray value would fail inside its write.
HELPFUL = {"yes": True, "no": False}

#: Refusals about the text the student wrote, which are shown at its field too.
_TEXT_CODES = frozenset({"BODY_REQUIRED", "BODY_TOO_LONG", "BODY_INVALID"})
#: The case page's text fields. A case page shows at most one of them, so text
#: refused by one action fills whichever the case offers now: a reply refused
#: because the advisor took the question back becomes a follow-up, not lost.
_TEXT_FIELDS = ("reply_body", "follow_up_body", "reopen_body")


def _begin(
    request: HttpRequest, reference: str
) -> tuple[str, int, TimetableFeedback] | HttpResponse:
    """The language, the session's student and their case, or the response that
    ends the request."""
    language = copy.language_of(getattr(request, "LANGUAGE_CODE", ""))
    student_id = _student(request)
    if isinstance(student_id, HttpResponse):
        return student_id
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return _unavailable(request, language, status=404)
    case = _own_case(request, language, student_id, reference)
    if isinstance(case, HttpResponse):
        return case
    return language, student_id, case


def _done(request: HttpRequest, case: TimetableFeedback, code: str) -> HttpResponse:
    messages.add_message(request, messages.SUCCESS, code, extra_tags=NOTICE_TAG)
    return redirect("student_timetable_feedback_detail", reference=case.reference)


def _refused(
    request: HttpRequest,
    refused: workflow.WorkflowRefused,
    *,
    language: str,
    student_id: int,
    case: TimetableFeedback,
    field: str = "",
    values: dict[str, str] | None = None,
) -> HttpResponse:
    """The case page again, as the case is now, with the refusal."""
    if refused.code == "CASE_NOT_FOUND":
        return _unavailable(
            request, language, status=404, message=copy.refusal("CASE_NOT_FOUND", language)
        )
    case.refresh_from_db()
    error = {
        "field": field if refused.code in _TEXT_CODES else "",
        "html": copy.refusal(refused.code, language, retry_after=refused.retry_after),
    }
    response = case_page(
        request,
        case,
        student_id=student_id,
        language=language,
        errors=[error],
        values=values,
        status=refused.status,
    )
    if refused.status == 429:
        # The limiter's own number, never a guess.
        response["Retry-After"] = str(refused.retry_after)
    return response


# ---------------------------------------------------------------------------
# Writing to the case
# ---------------------------------------------------------------------------


def _write(
    request: HttpRequest,
    reference: str,
    *,
    act: Callable[..., Any],
    field: str,
    notice: str,
) -> HttpResponse:
    begun = _begin(request, reference)
    if isinstance(begun, HttpResponse):
        return begun
    language, student_id, case = begun
    body = _normalise_newlines(request.POST.get("body"))
    try:
        act(student_id=student_id, reference=case.reference, body=body)
    except workflow.WorkflowRefused as refused:
        return _refused(
            request,
            refused,
            language=language,
            student_id=student_id,
            case=case,
            field=field,
            values=dict.fromkeys(_TEXT_FIELDS, body),
        )
    return _done(request, case, notice)


@never_cache
@require_POST
def feedback_reply_view(request: HttpRequest, reference: str) -> HttpResponse:
    """Answer the advisor's request for information."""
    return _write(request, reference, act=workflow.reply, field="reply_body", notice="REPLY_SENT")


@never_cache
@require_POST
def feedback_follow_up_view(request: HttpRequest, reference: str) -> HttpResponse:
    """Add a message while the case is being handled."""
    return _write(
        request,
        reference,
        act=workflow.follow_up,
        field="follow_up_body",
        notice="FOLLOW_UP_SENT",
    )


@never_cache
@require_POST
def feedback_reopen_view(request: HttpRequest, reference: str) -> HttpResponse:
    """Say the answer did not settle it, and why, within the window."""
    return _write(
        request,
        reference,
        act=workflow.reopen_answer,
        field="reopen_body",
        notice="ANSWER_REOPENED",
    )


@never_cache
@require_POST
def feedback_confirm_view(request: HttpRequest, reference: str) -> HttpResponse:
    """Accept the answer, which closes the case, saying whether it helped."""
    begun = _begin(request, reference)
    if isinstance(begun, HttpResponse):
        return begun
    language, student_id, case = begun
    chosen = str(request.POST.get("helpful", ""))
    try:
        workflow.confirm_answer(
            student_id=student_id, reference=case.reference, helpful=HELPFUL.get(chosen)
        )
    except workflow.WorkflowRefused as refused:
        return _refused(
            request,
            refused,
            language=language,
            student_id=student_id,
            case=case,
            values={"helpful": chosen},
        )
    return _done(request, case, "ANSWER_CONFIRMED")


# ---------------------------------------------------------------------------
# Withdrawing, after a confirmation
# ---------------------------------------------------------------------------


def _confirm_page(
    request: HttpRequest,
    case: TimetableFeedback,
    *,
    language: str,
    title: str,
    body: object,
    yes: str,
    action_url: str,
    point: dict[str, Any] | None = None,
) -> HttpResponse:
    reference = copy.bdi(case.reference)
    return render(
        request,
        "core/student_timetable_feedback_confirm.html",
        _context(
            request,
            language,
            heading=copy.text(title, language, reference=reference),
            # A <title> shows markup literally, so it gets the same words as text.
            page_title=copy.plain(title, language, reference=case.reference),
            body=body,
            yes=yes,
            action_url=action_url,
            case_url=reverse("student_timetable_feedback_detail", args=[case.reference]),
            point=point,
        ),
    )


@never_cache
@require_http_methods(["GET", "POST"])
def feedback_withdraw_view(request: HttpRequest, reference: str) -> HttpResponse:
    """GET asks; POST withdraws the whole case."""
    begun = _begin(request, reference)
    if isinstance(begun, HttpResponse):
        return begun
    language, student_id, case = begun
    if request.method == "POST":
        try:
            workflow.withdraw_case(student_id=student_id, reference=case.reference)
        except workflow.WorkflowRefused as refused:
            return _refused(request, refused, language=language, student_id=student_id, case=case)
        return _done(request, case, "CASE_WITHDRAWN")

    refusal = workflow.student_action_refusal(case, "withdraw_case")
    if refusal is not None:
        return _refused(request, refusal, language=language, student_id=student_id, case=case)
    hours = int(feedback.COOL_DOWN_AFTER_WITHDRAWAL.total_seconds() // 3600)
    return _confirm_page(
        request,
        case,
        language=language,
        title="confirm_case_title",
        body=copy.text(
            "confirm_case_body", language, hours=copy.count_text(hours, "hour", language)
        ),
        yes=copy.strings(language)["confirm_case_yes"],
        action_url=reverse("student_timetable_feedback_withdraw", args=[case.reference]),
    )


@never_cache
@require_http_methods(["GET", "POST"])
def feedback_withdraw_point_view(
    request: HttpRequest, reference: str, point_id: str
) -> HttpResponse:
    """GET asks; POST withdraws one point, leaving the rest of the case open."""
    begun = _begin(request, reference)
    if isinstance(begun, HttpResponse):
        return begun
    language, student_id, case = begun
    if request.method == "POST":
        try:
            workflow.withdraw_point(
                student_id=student_id, reference=case.reference, point_id=point_id
            )
        except workflow.WorkflowRefused as refused:
            return _refused(request, refused, language=language, student_id=student_id, case=case)
        return _done(request, case, "POINT_WITHDRAWN")

    refusal = workflow.student_action_refusal(case, "withdraw_point", point_id=point_id)
    if refusal is not None:
        return _refused(request, refusal, language=language, student_id=student_id, case=case)
    point = case.points.get(pk=point_id)
    return _confirm_page(
        request,
        case,
        language=language,
        title="confirm_point_title",
        body=copy.text("confirm_point_body", language, reference=copy.bdi(case.reference)),
        yes=copy.strings(language)["confirm_point_yes"],
        action_url=reverse(
            "student_timetable_feedback_withdraw_point", args=[case.reference, point.pk]
        ),
        point=_point_view(point, language),
    )
