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
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    AdvisorConversation,
    AdvisorFeedback,
    AdvisorMessage,
    AdvisorMessageCitation,
    FinalDisposition,
)
from .services.advisor_outcome import derive_outcome
from .services.advisor_principal import AdvisorPrincipal, IdentityError
from .services.rate_limit import CONVERSATION, FEEDBACK, GENERATION, HISTORY
from .services.rate_limit import consume as spend_budget
from .services.rate_limit import release as refund_budget

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 4000
MAX_TITLE_CHARS = 120


def _principal(request: HttpRequest) -> AdvisorPrincipal | None:
    """The effective student, from the authenticated session ONLY.

    Never from the payload. A request that names a student id is describing what
    it wants, not who it is. Returns None so each view can answer 403 in its own
    shape; the principal itself fails closed by raising.
    """
    try:
        return AdvisorPrincipal.for_student(request)
    except IdentityError:
        return None


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


def _over_budget(budget: str, student_id: int) -> JsonResponse | None:
    """Spend one unit, or explain how long to wait.

    Budgets are named for the RESOURCE. Generation is the expensive one and every
    door onto it — a new question, a retry of a failed turn — draws on the same
    allowance, so retrying cannot become a way around the limit on asking.
    """
    decision = spend_budget(budget, student_id)
    if decision.allowed:
        return None
    response = JsonResponse(
        {
            "error": "لقد أرسلت طلبات كثيرة. يرجى المحاولة بعد قليل.",
            "retry_after": decision.retry_after,
        },
        status=429,
    )
    response["Retry-After"] = str(decision.retry_after)
    return response


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
    from .services.virtual_advisor import answer_virtual_advisor

    principal = _principal(request)
    if principal is None:
        return _forbidden()
    student_id = principal.student_id
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

    student_message = None
    if key:
        existing = conversation.messages.filter(
            idempotency_key=key, role=AdvisorMessage.ROLE_STUDENT
        ).first()
        if existing is not None:
            resumed, response = _resume_or_replay(conversation, existing, student_id, request_hash)
            if response is not None:
                return response
            student_message = resumed

    # Charged here: after ownership, after validation, and after the idempotency
    # branch that may replay a stored answer — but still before the model is
    # called, which is what admission control requires. Charged earlier, a replay
    # served entirely from storage cost the student the same as a new question.
    over = _over_budget(GENERATION, student_id)
    if over:
        return over

    if student_message is None:
        try:
            with transaction.atomic():
                student_message = AdvisorMessage.objects.create(
                    conversation=conversation,
                    role=AdvisorMessage.ROLE_STUDENT,
                    content=question,
                    idempotency_key=key,
                    request_hash=request_hash,
                    status=AdvisorMessage.STATUS_PENDING,
                )
        except IntegrityError:
            if not key:
                # The unique constraint only covers non-empty keys, so this cannot be
                # an idempotency collision — it is some other integrity failure, and
                # the recovery below would match an unrelated earlier keyless turn
                # and serve its stored answer as this question's.
                raise
            # Two concurrent sends of the same key. The other one is authoritative.
            existing = conversation.messages.filter(
                idempotency_key=key, role=AdvisorMessage.ROLE_STUDENT
            ).first()
            if existing is None:
                raise
            resumed, response = _resume_or_replay(conversation, existing, student_id, request_hash)
            if response is not None:
                return response
            student_message = resumed

    try:
        result = answer_virtual_advisor(question=question, principal=principal)
    except ValueError as exc:
        # The student's own row is gone — a roster re-import can do this. Passing a
        # real identity makes it raise where the general-mode stub used to answer
        # blandly and work, so without this branch the student gets a 503 on every
        # question they ever ask, permanently, with no explanation. Do NOT fall back
        # to the general context: an answer that silently stops being about them is
        # the failure this whole change removes.
        logger.warning(
            "Adviser has no student record for conversation %s: %s", conversation.id, exc
        )
        # No answer is possible for this student until their record is restored, so
        # charging them would spend the whole allowance on a diagnosis and then
        # replace it with a rate-limit message that hides the diagnosis.
        refund_budget(GENERATION, student_id)
        student_message.status = AdvisorMessage.STATUS_FAILED
        student_message.save(update_fields=["status"])
        return JsonResponse(
            {
                "conversation": _conversation_json(conversation),
                "student_message": _message_json(student_message),
                "assistant_message": None,
                "error": ("تعذر العثور على سجلك الأكاديمي. يرجى التواصل مع عمادة القبول والتسجيل."),
            },
            status=409,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Adviser generation failed for conversation %s", conversation.id)
        student_message.status = AdvisorMessage.STATUS_FAILED
        student_message.save(update_fields=["status"])
        return JsonResponse(
            {
                "conversation": _conversation_json(conversation),
                "student_message": _message_json(student_message),
                "assistant_message": None,
                # No exception class name: varying the input and reading back
                # `ConnectionError` vs `OperationalError` is a free map of which
                # subsystem the student just broke.
                "error": "The adviser could not answer just now. Your question was saved.",
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


#: How long a turn may sit PENDING before we accept that whatever was generating it
#: is gone. Generously longer than the slowest model call, because resuming a turn
#: that IS still running would answer it twice.
STALE_GENERATION = timedelta(minutes=15)


def _is_resumable(message: AdvisorMessage) -> bool:
    """Whether this turn still needs an answer.

    FAILED is the clean case: generation raised and said so. PENDING is the dirty
    one — the worker was killed, the deploy restarted, the request timed out
    upstream — and nothing else will ever move it on. Treating PENDING as
    permanently in-flight rebuilds, one state over, exactly the trap that made a
    failed question unanswerable forever.
    """
    if message.status == AdvisorMessage.STATUS_FAILED:
        return True
    if message.status != AdvisorMessage.STATUS_PENDING:
        return False
    started = message.generation_started_at or message.created_at
    return timezone.now() - started > STALE_GENERATION


def _resume_or_replay(
    conversation: AdvisorConversation,
    existing: AdvisorMessage,
    student_id: int,
    request_hash: str,
) -> tuple[AdvisorMessage | None, JsonResponse | None]:
    """Decide what a repeated idempotency key means for THIS turn.

    Idempotency exists so a retry cannot produce a second answer to a question
    already answered. It must not also mean a question that FAILED can never be
    answered at all — and it did: replaying returned the failed student message
    with `assistant_message: null`, so every retry succeeded at doing nothing and
    the student's question was permanently stuck.

    A pending turn is still being generated, so replaying is right. A finished one
    has its answer. A failed one is unfinished, and gets resumed.

    Returns `(message_to_answer, None)` to generate, or `(None, response)` to
    return immediately.
    """
    if existing.request_hash != request_hash:
        # The same key carrying a different question is a client bug, not a retry.
        # Answering it would silently attach one question's answer to another's key.
        return None, JsonResponse(
            {"error": "idempotency_key was already used for a different message."},
            status=409,
        )
    if not _is_resumable(existing):
        return None, _replay(conversation, existing, student_id)

    # Claim it with ONE conditional UPDATE. Reading the status and then writing it
    # is two statements with a gap in between, and a double-clicked Retry fits
    # inside that gap: both requests see FAILED, both claim it, both call the
    # model, and the student gets two answers to one question.
    claimed = AdvisorMessage.objects.filter(pk=existing.pk, status=existing.status).update(
        status=AdvisorMessage.STATUS_PENDING, generation_started_at=timezone.now()
    )
    if not claimed:
        # Someone else claimed it first. They are generating; we replay.
        existing.refresh_from_db()
        return None, _replay(conversation, existing, student_id)

    existing.status = AdvisorMessage.STATUS_PENDING
    return existing, None


def _replay(
    conversation: AdvisorConversation, student_message: AdvisorMessage, student_id: int
) -> JsonResponse:
    """Return the turn this key already produced, rather than generating again."""
    assistant = (
        conversation.messages.filter(in_reply_to=student_message)
        .prefetch_related("citations")
        .order_by("created_at")
        .first()
    )
    if assistant is None:
        # Rows written before turns were paired explicitly. The old heuristic is
        # wrong whenever turns finish out of order, so it is a fallback for history
        # only — never the primary answer.
        assistant = (
            conversation.messages.filter(
                role=AdvisorMessage.ROLE_ASSISTANT,
                in_reply_to__isnull=True,
                created_at__gte=student_message.created_at,
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

    # Derived ONCE, here, from the result the student will actually see — after the
    # citation check and any grounding retry. Deriving it earlier and correcting it
    # later leaves a window where the stored outcome disagrees with the stored
    # answer, and the escalation layer reads the stored outcome.
    outcome = derive_outcome(result)
    entitled = {c.get("policy_id"): c for c in (result.get("citations") or [])}
    claimed = [c for c in _claimed_citations(answer) if c.get("policy_id") in entitled]

    status = AdvisorMessage.STATUS_COMPLETED
    if outcome.disposition in {FinalDisposition.ABSTAIN, FinalDisposition.ESCALATE}:
        status = AdvisorMessage.STATUS_ABSTAINED

    with transaction.atomic():
        assistant = AdvisorMessage.objects.create(
            conversation=conversation,
            in_reply_to=student_message,
            role=AdvisorMessage.ROLE_ASSISTANT,
            content=answer,
            grounding_state=str(agent.get("policy_grounding") or ""),
            final_disposition=outcome.disposition,
            reason_codes=outcome.reason_codes,
            missing_information=outcome.missing_information,
            outcome_schema_version=outcome.schema_version,
            model_name=str(result.get("model") or ""),
            route=(
                AdvisorMessage.ROUTE_AGENT
                if agent.get("loop_used")
                else AdvisorMessage.ROUTE_SEEDED_FALLBACK
            ),
            status=status,
        )
        for claim in claimed:
            source = entitled[claim["policy_id"]]
            AdvisorMessageCitation.objects.create(
                message=assistant,
                policy_id=source.get("policy_id") or "",
                document_title=source.get("document_title") or "",
                edition=str(source.get("edition") or ""),
                page=_page_shown(claim, source),
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


def _page_shown(claim: dict[str, Any], source: dict[str, Any]) -> str:
    """The single page this citation points a student at.

    A policy record's `page` is an int when the rule sits on one page and a LIST
    when it spans several, so taking it verbatim renders "p. [24, 25]" in the
    Sources block — and on PostgreSQL a long enough span overflows the column,
    raising inside the atomic block, leaving the turn stranded mid-generation.
    SQLite truncates silently, so it would never show up in development.

    The claim is the better source anyway: it carries the page the answer actually
    sent the student to, already parsed to one integer.
    """
    page = claim.get("page")
    if page is None:
        page = source.get("page")
    if isinstance(page, list | tuple):
        page = page[0] if page else None
    return "" if page is None else str(page)[:40]


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
