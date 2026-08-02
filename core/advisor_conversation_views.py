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

import hashlib
import json
import logging
import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import AdvisorConversation, AdvisorFeedback, AdvisorMessage, AdvisorMessageCitation
from .services.rbac import ROLE_STUDENT, get_user_scope

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 4000
MAX_TITLE_CHARS = 120


def _student_id(request: HttpRequest) -> int | None:
    """The effective student, from the session ONLY.

    Never from the payload. A request that names a student id is describing what
    it wants, not who it is.
    """
    scope = get_user_scope(request.user)
    if str(scope.get("role", "")) != ROLE_STUDENT:
        return None
    student_id = scope.get("student_id")
    return int(student_id) if student_id is not None else None


def _owned_conversation(conversation_id: Any, student_id: int) -> AdvisorConversation:
    """Fetch a conversation the student owns, or 404.

    Ownership is part of the filter. Fetching by id and checking `.student_id`
    afterwards would be one forgotten line away from a leak, and the forgotten
    line would look like working code.
    """
    try:
        parsed = uuid.UUID(str(conversation_id))
    except (ValueError, AttributeError, TypeError):
        # A malformed id is indistinguishable from someone else's id, and both
        # should tell the caller the same thing: nothing here.
        from django.http import Http404

        raise Http404("No such conversation") from None
    return get_object_or_404(AdvisorConversation, id=parsed, student_id=student_id)


def _body(request: HttpRequest) -> tuple[dict[str, Any], JsonResponse | None]:
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}, JsonResponse({"error": "Invalid JSON body."}, status=400)
    if not isinstance(payload, dict):
        return {}, JsonResponse({"error": "Body must be a JSON object."}, status=400)
    return payload, None


def _forbidden() -> JsonResponse:
    return JsonResponse({"error": "This endpoint is for signed-in students."}, status=403)


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


def _message_json(message: AdvisorMessage) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "created_at": message.created_at.isoformat(),
    }
    if message.role == AdvisorMessage.ROLE_ASSISTANT:
        data.update(
            answer_mode=message.answer_mode or None,
            grounding_state=message.grounding_state or None,
            final_disposition=message.final_disposition or None,
            citations=[_citation_json(c) for c in message.citations.all()],
        )
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
    student_id = _student_id(request)
    if student_id is None:
        return _forbidden()
    conversations = AdvisorConversation.objects.filter(student_id=student_id)
    return JsonResponse({"conversations": [_conversation_json(c) for c in conversations]})


@require_POST
def conversation_create_view(request: HttpRequest) -> JsonResponse:
    student_id = _student_id(request)
    if student_id is None:
        return _forbidden()
    payload, err = _body(request)
    if err:
        return err
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_CHARS]
    conversation = AdvisorConversation.objects.create(student_id=student_id, title=title)
    return JsonResponse({"conversation": _conversation_json(conversation)}, status=201)


@require_GET
def conversation_messages_view(request: HttpRequest, conversation_id: str) -> JsonResponse:
    student_id = _student_id(request)
    if student_id is None:
        return _forbidden()
    conversation = _owned_conversation(conversation_id, student_id)
    messages = conversation.messages.prefetch_related("citations").order_by("created_at")
    mine = {
        f.message_id: f
        for f in AdvisorFeedback.objects.filter(
            message__conversation=conversation, student_id=student_id
        )
    }
    out = []
    for message in messages:
        data = _message_json(message)
        feedback = mine.get(message.id)
        if feedback:
            data["feedback"] = {"rating": feedback.rating, "reason_codes": feedback.reason_codes}
        out.append(data)
    return JsonResponse({"conversation": _conversation_json(conversation), "messages": out})


def _title_from(question: str) -> str:
    words = question.strip().split()
    return " ".join(words[:8])[:MAX_TITLE_CHARS]


@require_POST
def conversation_post_message_view(request: HttpRequest, conversation_id: str) -> JsonResponse:
    """Ask a question and persist the whole turn.

    The adviser call sits OUTSIDE the transaction and the writes sit inside it. A
    model that takes ninety seconds must not hold a database transaction open for
    ninety seconds, and the writes must still be all-or-nothing: an assistant
    message without its citations would be an uncited answer that looks cited-by-
    omission rather than one that failed.
    """
    from .services.local_llm import LocalLLMError
    from .services.virtual_advisor import answer_virtual_advisor

    student_id = _student_id(request)
    if student_id is None:
        return _forbidden()
    conversation = _owned_conversation(conversation_id, student_id)

    payload, err = _body(request)
    if err:
        return err
    question = str(payload.get("message") or "").strip()
    if not question:
        return JsonResponse({"error": "message is required"}, status=400)
    if len(question) > MAX_QUESTION_CHARS:
        return JsonResponse({"error": "message is too long"}, status=400)

    key = str(payload.get("idempotency_key") or "").strip()[:64]
    request_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()

    if key:
        existing = conversation.messages.filter(
            idempotency_key=key, role=AdvisorMessage.ROLE_STUDENT
        ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                # The same key carrying a different question is a client bug, not a
                # retry. Answering it would silently attach one question's answer to
                # another's key.
                return JsonResponse(
                    {"error": "idempotency_key was already used for a different message."},
                    status=409,
                )
            return _replay(conversation, existing, student_id)

    with transaction.atomic():
        try:
            student_message = AdvisorMessage.objects.create(
                conversation=conversation,
                role=AdvisorMessage.ROLE_STUDENT,
                content=question,
                idempotency_key=key,
                request_hash=request_hash,
                status=AdvisorMessage.STATUS_PENDING,
            )
        except IntegrityError:
            # Two concurrent sends of the same key. The other one is authoritative.
            existing = conversation.messages.filter(
                idempotency_key=key, role=AdvisorMessage.ROLE_STUDENT
            ).first()
            if existing is None:
                raise
            return _replay(conversation, existing, student_id)

    try:
        result = answer_virtual_advisor(
            question=question,
            scope={"role": ROLE_STUDENT, "student_id": student_id},
        )
    except (LocalLLMError, Exception) as exc:  # noqa: BLE001
        logger.exception("Adviser generation failed for conversation %s", conversation.id)
        student_message.status = AdvisorMessage.STATUS_FAILED
        student_message.save(update_fields=["status"])
        return JsonResponse(
            {
                "conversation": _conversation_json(conversation),
                "student_message": _message_json(student_message),
                "assistant_message": None,
                "error": "The adviser could not answer just now. Your question was saved.",
                "detail": type(exc).__name__,
            },
            status=503,
        )

    assistant_message = _persist_answer(conversation, student_message, result)
    return JsonResponse(
        {
            "conversation": _conversation_json(conversation),
            "student_message": _message_json(student_message),
            "assistant_message": _message_json(assistant_message),
        },
        status=201,
    )


def _replay(
    conversation: AdvisorConversation, student_message: AdvisorMessage, student_id: int
) -> JsonResponse:
    """Return the turn this key already produced, rather than generating again."""
    assistant = (
        conversation.messages.filter(
            role=AdvisorMessage.ROLE_ASSISTANT, created_at__gte=student_message.created_at
        )
        .prefetch_related("citations")
        .order_by("created_at")
        .first()
    )
    return JsonResponse(
        {
            "conversation": _conversation_json(conversation),
            "student_message": _message_json(student_message),
            "assistant_message": _message_json(assistant) if assistant else None,
            "replayed": True,
        },
        status=200,
    )


def _persist_answer(
    conversation: AdvisorConversation,
    student_message: AdvisorMessage,
    result: dict[str, Any],
) -> AdvisorMessage:
    """Save the assistant turn and its citations as one unit.

    Only citations the answer ACTUALLY made are stored, intersected with what the
    request was entitled to cite. Saving everything retrieved would attach
    authority to records the answer never used, which reads to a student as
    "these sources support this" — the same defect as background evidence
    appearing beside a claim.
    """
    from .services.virtual_advisor import _claimed_citations

    agent = result.get("agent") or {}
    answer = str(result.get("answer") or "")
    entitled = {c.get("policy_id"): c for c in (result.get("citations") or [])}
    claimed = [c for c in _claimed_citations(answer) if c.get("policy_id") in entitled]

    disposition = AdvisorMessage.STATUS_COMPLETED
    if agent.get("citation_refused"):
        disposition = AdvisorMessage.STATUS_ABSTAINED

    with transaction.atomic():
        assistant = AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_ASSISTANT,
            content=answer,
            grounding_state=str(agent.get("policy_grounding") or ""),
            final_disposition="ABSTAIN" if agent.get("citation_refused") else "PASS",
            model_name=str(result.get("model") or ""),
            route=(
                AdvisorMessage.ROUTE_AGENT
                if agent.get("loop_used")
                else AdvisorMessage.ROUTE_SEEDED_FALLBACK
            ),
            status=disposition,
        )
        for claim in claimed:
            source = entitled[claim["policy_id"]]
            AdvisorMessageCitation.objects.create(
                message=assistant,
                policy_id=source.get("policy_id") or "",
                document_title=source.get("document_title") or "",
                edition=str(source.get("edition") or ""),
                page=str(source.get("page") if source.get("page") is not None else ""),
                effective_from=str(source.get("effective_from") or ""),
                effective_to=str(source.get("effective_to") or ""),
                authority_status="AUTHORITY_APPROVED",
                validation_status=AdvisorMessageCitation.VALID,
                source_version_hash=_source_hash(source),
            )

        student_message.status = AdvisorMessage.STATUS_COMPLETED
        student_message.save(update_fields=["status"])

        conversation.last_message_at = timezone.now()
        if not conversation.title:
            conversation.title = _title_from(student_message.content)
        conversation.save(update_fields=["last_message_at", "title", "updated_at"])

    # Re-read so the response is built from what the database holds, not from the
    # objects that were just constructed in memory.
    return AdvisorMessage.objects.prefetch_related("citations").get(pk=assistant.pk)


def _source_hash(citation: dict[str, Any]) -> str:
    """Fingerprint of the cited source AS SHOWN, so a later revision is detectable."""
    material = "|".join(
        str(citation.get(field) or "")
        for field in (
            "policy_id",
            "document_id",
            "edition",
            "page",
            "effective_from",
            "effective_to",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@require_POST
def message_feedback_view(request: HttpRequest, message_id: str) -> JsonResponse:
    """Rate an assistant message in one of the student's OWN conversations."""
    student_id = _student_id(request)
    if student_id is None:
        return _forbidden()
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
