"""Durable, ownership-scoped conversations for the student adviser.

Two invariants hold this file together, and both are structural rather than
conventional:

**Ownership is in the query, never after it.** Every lookup filters on the
student id taken from the session, so there is no moment at which a row belonging
to another student exists in a local variable waiting to be checked. A missed
check is then impossible rather than merely unlikely, and cross-student access
returns 404 — 403 would confirm the conversation exists.

**The response is serialised from the saved rows.** Not from the transient result
that produced them. That makes "what the student sees is what was stored"
mechanically true instead of a property that has to be tested for, which matters
most for citations: rendering from one object while persisting another is exactly
how provenance drifts without anyone noticing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .advisor_http import forbidden as _forbidden
from .advisor_http import json_body as _body
from .advisor_http import over_budget as _over_budget
from .advisor_http import student_principal as _principal
from .models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorFeedback,
    AdvisorMessage,
    AdvisorMessageCitation,
)
from .services import advisor_turn
from .services.advisor_presentations import normalise_presentation
from .services.advisor_principal import AdvisorPrincipal
from .services.advisor_turn import (
    MAX_TITLE_CHARS,
    ConversationNotFound,
    open_conversation,
)

# Re-exported, not merely imported: the browser's retry affordance is decided by
# the same staleness window the turn service uses to reclaim an abandoned turn, and
# a second copy of "fifteen minutes" here is a way for the screen and the service
# to disagree about which questions are still being answered.
from .services.advisor_turn import STALE_GENERATION as STALE_GENERATION  # noqa: PLC0414
from .services.advisor_turn import is_resumable as _is_resumable
from .services.rate_limit import CONVERSATION, FEEDBACK, HISTORY

logger = logging.getLogger(__name__)


def _owned_conversation(principal: AdvisorPrincipal, conversation_id: Any) -> AdvisorConversation:
    """Fetch a conversation the student owns, or 404.

    Ownership is part of the filter, and the filter lives in `advisor_turn` so
    that every channel proves it the same way. A malformed id is indistinguishable
    from someone else's id, and both should tell the caller the same thing:
    nothing here.
    """
    try:
        return open_conversation(principal=principal, conversation_id=conversation_id)
    except ConversationNotFound:
        from django.http import Http404

        raise Http404("No such conversation") from None


# ── serialisation: the ONLY shape the browser ever sees ──────────
#
# Everything absent here is absent deliberately: tool results, judge reasoning,
# prompt contents, policy-role candidates and model traces name database tables,
# quote cohort statistics and expose internal machinery. They belong in an
# operator audit record.


def _citation_json(citation: AdvisorMessageCitation) -> dict[str, Any]:
    return {
        "policy_id": citation.policy_id,
        "document_title": citation.document_title,
        "edition": citation.edition,
        "page": citation.page,
        "effective_from": citation.effective_from or None,
        "effective_to": citation.effective_to or None,
    }


def _language_of(text: str) -> str:
    """The answer language, decided by the SAME rule the model was pinned with.

    `virtual_advisor._answer_language` already makes this decision, deterministically
    and before generation, and instructs the model never to switch. Re-deriving it in
    the browser from the characters that came back is a second opinion that can
    disagree with the first — and every character heuristic tried here disagreed on a
    real answer shape. Arabic words are short and course titles are long, so
    «الشرط المسبق هو Introduction to Artificial Intelligence قبل التسجيل» is 23 Arabic
    characters against 36 Latin ones; a timetable table is mostly course codes and
    clock times. Both are Arabic answers and both lose a character count.

    So the direction travels WITH the message, from the side that chose it.
    """
    from core.services.virtual_advisor import _answer_language

    return "ar" if _answer_language(text or "") == "Arabic" else "en"


def _message_json(message: AdvisorMessage, *, language: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "created_at": message.created_at.isoformat(),
        # A student message is in its own language; an assistant message is in the
        # language its QUESTION pinned, which is why the caller passes it in.
        "language": language or _language_of(message.content),
    }
    if (
        message.role == AdvisorMessage.ROLE_STUDENT
        and message.idempotency_key
        and _is_resumable(message)
    ):
        # The one piece of turn machinery the browser genuinely needs, and only on
        # the turns that need it. Retry has to resume THIS question rather than ask
        # it again, and a key held only in page memory is gone after a reload — so
        # a resumable turn carries its own token back. It is the client's own value
        # echoed to the client, scoped to a message it already owns.
        #
        # Keyed on resumability rather than on FAILED so that an ABANDONED turn is
        # recoverable too: a worker killed mid-generation leaves PENDING set
        # forever, and "Preparing the answer…" with no way out is a lost question
        # wearing a spinner.
        data["retry_token"] = message.idempotency_key

    if message.role == AdvisorMessage.ROLE_ASSISTANT:
        # `answer_mode`, `grounding_state` and `final_disposition` are NOT here on
        # purpose. They are the system's account of its own reasoning — which
        # retrieval state it was in, what the judge decided — and nothing on the
        # screen reads them. Shipping them anyway would let a student open the
        # network tab and read "grounding_state: not_consulted" beside an answer
        # about withdrawal limits. `status` already carries what the UI needs.
        data["citations"] = [_citation_json(c) for c in message.citations.all()]
        presentation = normalise_presentation(message.presentation)
        if presentation:
            data["presentation"] = presentation
        case = next(iter(message.escalations.all()), None)
        if case is not None:
            # Enough for the thread to show that a person has this, and no more:
            # the full case is its own endpoint.
            data["escalation"] = {
                "reference": case.reference,
                "status": case.status,
                "status_label": STATUS_LABELS_AR.get(case.status, case.status),
                # A finished case stays on screen — the student wants the outcome —
                # but it stops standing in the way of raising the question again.
                "is_open": case.status not in AdvisorEscalation.TERMINAL_STATUSES,
                "resolution_message": case.resolution_message,
                "created_at": case.created_at.isoformat(),
                # A human adviser's reply has no pinned language of its own, so it
                # takes the one the student asked in. That is who it is addressed to,
                # and it is the only evidence on file — reading the direction off the
                # reply's own characters is the guess this whole change removes.
                "language": data["language"],
            }
    return data


def _conversation_json(conversation: AdvisorConversation) -> dict[str, Any]:
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "status": conversation.status,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "last_message_at": (
            conversation.last_message_at.isoformat() if conversation.last_message_at else None
        ),
    }


# ── endpoints ────────────────────────────────────────────────────


@require_GET
def conversation_list_view(request: HttpRequest) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    student_id = principal.student_id
    over = _over_budget(HISTORY, student_id)
    if over:
        return over
    conversations = AdvisorConversation.objects.filter(student_id=student_id)
    return JsonResponse({"conversations": [_conversation_json(c) for c in conversations]})


@require_POST
def conversation_create_view(request: HttpRequest) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    student_id = principal.student_id
    # Its OWN budget, not the generation one. The client creates a conversation on
    # its way to asking, so charging both made the real ceiling three questions per
    # ten minutes against a limit that reads as six — and the refusal surfaced on
    # the create call, where the client had no wait to show.
    over = _over_budget(CONVERSATION, student_id)
    if over:
        return over
    payload, err = _body(request)
    if err:
        return err
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_CHARS]
    conversation = AdvisorConversation.objects.create(student_id=student_id, title=title)
    return JsonResponse({"conversation": _conversation_json(conversation)}, status=201)


@require_GET
def conversation_messages_view(request: HttpRequest, conversation_id: str) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    student_id = principal.student_id
    over = _over_budget(HISTORY, student_id)
    if over:
        return over
    conversation = _owned_conversation(principal, conversation_id)
    messages = conversation.messages.prefetch_related("citations", "escalations").order_by(
        "sequence", "created_at"
    )
    mine = {
        f.message_id: f
        for f in AdvisorFeedback.objects.filter(
            message__conversation=conversation, student_id=student_id
        )
    }
    out = []
    # An assistant turn inherits the language of the question that produced it —
    # the same value `_answer_language` pinned the model to before it wrote a word.
    # Reading it off the answer instead would let one Arabic office name in an
    # English reply, or one English course title in an Arabic one, decide the
    # direction of the whole message.
    # Empty until the first question is seen; `_message_json` then falls back to the
    # message's own text, which is the right answer for a student message and the
    # only one available for a thread that somehow starts with an assistant turn.
    asked_in = ""
    for message in messages:
        if message.role == AdvisorMessage.ROLE_STUDENT:
            asked_in = _language_of(message.content)
        data = _message_json(message, language=asked_in)
        feedback = mine.get(message.id)
        if feedback:
            data["feedback"] = {"rating": feedback.rating, "reason_codes": feedback.reason_codes}
        out.append(data)
    return JsonResponse({"conversation": _conversation_json(conversation), "messages": out})


def _turn_json(result: advisor_turn.TurnResult, **extra: Any) -> dict[str, Any]:
    """The three objects every turn response carries, from the SAVED rows.

    The answer inherits the QUESTION's language: that is the value
    `_answer_language` pinned the model to before it wrote a word.
    """
    student_message = result.student_message
    assistant_message = result.assistant_message
    asked_in = _language_of(student_message.content) if student_message else ""
    return {
        "conversation": _conversation_json(result.conversation) if result.conversation else None,
        "student_message": _message_json(student_message) if student_message else None,
        "assistant_message": (
            _message_json(assistant_message, language=asked_in) if assistant_message else None
        ),
        **extra,
    }


@require_POST
def conversation_post_message_view(request: HttpRequest, conversation_id: str) -> JsonResponse:
    """Ask a question and persist the whole turn.

    The turn itself lives in `core.services.advisor_turn`, which the Telegram
    channel calls too — so ownership, idempotency, the generation budget and the
    order they run in are one implementation rather than two that drift. What
    stays here is what HTTP owns: reading the body, and rendering the outcome.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    conversation = _owned_conversation(principal, conversation_id)

    payload, err = _body(request)
    if err:
        return err

    result = advisor_turn.run_advisor_turn(
        principal=principal,
        conversation=conversation,
        question=str(payload.get("message") or ""),
        idempotency_key=str(payload.get("idempotency_key") or ""),
    )

    if result.outcome == advisor_turn.QUESTION_EMPTY:
        return JsonResponse({"error": "message is required"}, status=400)
    if result.outcome == advisor_turn.QUESTION_TOO_LONG:
        return JsonResponse({"error": "message is too long"}, status=400)
    if result.outcome == advisor_turn.KEY_CONFLICT:
        return JsonResponse(
            {"error": "idempotency_key was already used for a different message."},
            status=409,
        )
    if result.outcome == advisor_turn.RATE_LIMITED:
        response = JsonResponse(
            {
                "error": "لقد أرسلت طلبات كثيرة. يرجى المحاولة بعد قليل.",
                "retry_after": result.retry_after,
            },
            status=429,
        )
        response["Retry-After"] = str(result.retry_after)
        return response
    if result.outcome == advisor_turn.REPLAYED:
        return JsonResponse(_turn_json(result, replayed=True), status=200)
    if result.outcome == advisor_turn.NO_STUDENT_RECORD:
        return JsonResponse(
            _turn_json(
                result,
                error="تعذر العثور على سجلك الأكاديمي. يرجى التواصل مع عمادة القبول والتسجيل.",
            ),
            status=409,
        )
    if result.outcome == advisor_turn.GENERATION_FAILED:
        return JsonResponse(
            _turn_json(
                result,
                # No exception class name: varying the input and reading back
                # `ConnectionError` vs `OperationalError` is a free map of which
                # subsystem the student just broke.
                error="The adviser could not answer just now. Your question was saved.",
            ),
            status=503,
        )
    return JsonResponse(_turn_json(result), status=201)


@require_POST
def message_feedback_view(request: HttpRequest, message_id: str) -> JsonResponse:
    """Rate an assistant message in one of the student's OWN conversations."""
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    student_id = principal.student_id
    over = _over_budget(FEEDBACK, student_id)
    if over:
        return over

    payload, err = _body(request)
    if err:
        return err

    rating = str(payload.get("rating") or "").strip().upper()
    if rating not in {AdvisorFeedback.HELPFUL, AdvisorFeedback.NOT_HELPFUL}:
        return JsonResponse({"error": "rating must be HELPFUL or NOT_HELPFUL"}, status=400)

    reasons = payload.get("reason_codes") or []
    if not isinstance(reasons, list):
        return JsonResponse({"error": "reason_codes must be a list"}, status=400)
    reasons = [r for r in (str(x) for x in reasons) if r in AdvisorFeedback.REASON_CODES]

    try:
        parsed = uuid.UUID(str(message_id))
    except (ValueError, AttributeError, TypeError):
        from django.http import Http404

        raise Http404("No such message") from None

    # Ownership through the conversation, in the same query — a message id alone
    # says nothing about who may rate it.
    message = get_object_or_404(
        AdvisorMessage,
        id=parsed,
        role=AdvisorMessage.ROLE_ASSISTANT,
        conversation__student_id=student_id,
    )

    feedback, _created = AdvisorFeedback.objects.update_or_create(
        message=message,
        student_id=student_id,
        defaults={
            "rating": rating,
            "reason_codes": reasons,
            "comment": str(payload.get("comment") or "")[:2000],
        },
    )
    return JsonResponse(
        {
            "message_id": str(message.id),
            "feedback": {"rating": feedback.rating, "reason_codes": feedback.reason_codes},
        }
    )


# ── escalation: handing one turn to a person ─────────────────────


#: What a student is told a case is doing. The internal names are a workflow
#: vocabulary — "OPEN" tells someone waiting on a decision nothing about whether a
#: person has looked at it yet.
STATUS_LABELS_AR = {
    AdvisorEscalation.Status.OPEN: "جديدة",
    AdvisorEscalation.Status.ASSIGNED: "قيد المراجعة",
    AdvisorEscalation.Status.NEEDS_INFORMATION: "مطلوب معلومات إضافية",
    AdvisorEscalation.Status.RESOLVED: "تمت المعالجة",
    AdvisorEscalation.Status.CLOSED: "مغلقة",
}


def _escalation_json(escalation: AdvisorEscalation) -> dict[str, Any]:
    """What the STUDENT may see of their own case.

    An allowlist, and a short one. `adviser_notes` is working correspondence about
    them rather than to them; `evidence_snapshot` is the adviser's copy of material
    the student already has in the conversation, restated in machine shape; and
    `assigned_adviser_id` names a colleague who has not agreed to be named. The
    reference, the status and the summary are what a person chasing their own case
    actually needs.
    """
    return {
        "reference": escalation.reference,
        "status": escalation.status,
        "status_label": STATUS_LABELS_AR.get(escalation.status, escalation.status),
        "reason_code": escalation.reason_code,
        "student_note": escalation.student_note,
        "generated_summary": escalation.generated_summary,
        # What the adviser decided, written TO the student. Their working notes are
        # a different field and are never here.
        "resolution_message": escalation.resolution_message,
        "created_at": escalation.created_at.isoformat(),
        "updated_at": escalation.updated_at.isoformat(),
        "resolved_at": escalation.resolved_at.isoformat() if escalation.resolved_at else None,
    }


def _owned_assistant_message(message_id: Any, student_id: int) -> AdvisorMessage:
    """An assistant turn this student owns, with the question it answered.

    Ownership is in the query, as everywhere else here. The `in_reply_to` filter is
    not decoration: a case whose evidence cannot include the question is a case an
    adviser has to reconstruct from the student's memory of it.
    """
    try:
        parsed = uuid.UUID(str(message_id))
    except (ValueError, AttributeError, TypeError):
        from django.http import Http404

        raise Http404("No such message") from None
    return get_object_or_404(
        AdvisorMessage.objects.select_related("in_reply_to"),
        id=parsed,
        role=AdvisorMessage.ROLE_ASSISTANT,
        conversation__student_id=student_id,
        in_reply_to__isnull=False,
    )


@require_POST
def escalation_create_view(request: HttpRequest, message_id: str) -> JsonResponse:
    """Hand one answered turn to a human adviser.

    The decision itself lives in `core.services.advisor_turn.escalate_turn`, which
    the Telegram `/advisor` command calls too — so a case raised from a phone and a
    case raised from the browser are the same case, made the same way, with the
    same lock over the source message.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()

    payload, err = _body(request)
    if err:
        return err

    message = _owned_assistant_message(message_id, principal.student_id)

    result = advisor_turn.escalate_turn(
        principal=principal,
        message=message,
        student_note=str(payload.get("student_note") or ""),
        student_requested=bool(payload.get("student_requested")),
    )

    if result.outcome == advisor_turn.ESCALATION_NOT_WARRANTED:
        return JsonResponse(
            {
                "error": (
                    "هذه الإجابة لا تحتاج إلى مراجعة المرشد الأكاديمي. "
                    "يمكنك طلب المراجعة صراحةً إذا رغبت."
                )
            },
            status=409,
        )
    if result.outcome == advisor_turn.ESCALATION_RATE_LIMITED:
        response = JsonResponse(
            {
                "error": "لقد أرسلت طلبات كثيرة. يرجى المحاولة بعد قليل.",
                "retry_after": result.retry_after,
            },
            status=429,
        )
        response["Retry-After"] = str(result.retry_after)
        return response
    if result.outcome == advisor_turn.ESCALATION_EXISTS:
        return JsonResponse({"escalation": _escalation_json(result.escalation)}, status=200)
    return JsonResponse({"escalation": _escalation_json(result.escalation)}, status=201)


@require_GET
def escalation_list_view(request: HttpRequest) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    over = _over_budget(HISTORY, principal.student_id)
    if over:
        return over
    cases = AdvisorEscalation.objects.filter(student_id=principal.student_id)
    return JsonResponse({"escalations": [_escalation_json(c) for c in cases]})


@require_GET
def escalation_detail_view(request: HttpRequest, escalation_id: str) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    over = _over_budget(HISTORY, principal.student_id)
    if over:
        return over

    # By reference, which is the only identifier the student was ever shown. The
    # primary key stays internal.
    case = get_object_or_404(
        AdvisorEscalation,
        reference=str(escalation_id or "").strip().upper(),
        student_id=principal.student_id,
    )
    return JsonResponse({"escalation": _escalation_json(case)})
