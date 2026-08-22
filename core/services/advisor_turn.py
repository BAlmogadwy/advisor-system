"""One student turn, with no transport in it.

The whole of a turn — prove ownership, validate, honour an idempotency key, spend
the generation budget, write the question, call the adviser, write the answer and
its citations — lived inside `conversation_post_message_view`. That was correct
while a browser was the only way in. A second channel makes it a fork in the road:
either Telegram calls the HTTP view (and has to fabricate a Django request, a
session and a CSRF token to do it), or it reimplements the sequence and the two
copies start to disagree.

They disagree in a specific direction. Every step here is a security property held
in place by its ORDER relative to the others, and the copy that gets the order
subtly wrong still works:

* ownership is proved BEFORE the conversation is a local variable, so a row
  belonging to another student never exists to be forgotten about;
* the budget is charged AFTER the replay branch, so a stored answer served from
  the database does not cost a question, and BEFORE the model call, which is what
  admission control means;
* the student's question is written BEFORE generation, so a dropped response can
  be resumed rather than re-asked.

So the sequence is the unit that gets extracted, not the pieces. What stays behind
in a transport is what a transport actually owns: how the request arrived, and how
the outcome is rendered. `TurnResult.outcome` is a closed vocabulary precisely so
that a new channel switches on a value rather than inventing its own reading of a
half-populated result — the HTTP view maps those eight values onto the same status
codes it always returned, and Telegram maps them onto sentences.

Nothing in this module imports `django.http`, and that is the invariant worth
keeping: the moment a `JsonResponse` appears here, the browser has quietly become
the only real caller again.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorMessage,
    FinalDisposition,
)
from core.services.advisor_escalation import (
    build_evidence,
    deterministic_summary,
    escalation_reason,
    may_escalate,
)
from core.services.advisor_history import (
    load_latest_profile_presentation,
    load_profiled_history,
    load_visible_history,
)
from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.rate_limit import ESCALATION, GENERATION
from core.services.rate_limit import consume as spend_budget
from core.services.rate_limit import release as refund_budget

logger = logging.getLogger(__name__)

#: Kept here rather than in the view because it is a property of a turn, not of
#: HTTP: every channel has to refuse the same oversized question, and a channel
#: that picks its own ceiling lets the same student store a longer one by
#: switching app.
MAX_QUESTION_CHARS = 4000
MAX_TITLE_CHARS = 120
MAX_NOTE_CHARS = 2000

# ── the closed outcome vocabulary ────────────────────────────────
#
# A transport switches on exactly these. They are strings rather than an Enum
# because they are also written into logs and compared in tests, and an Enum
# repr in a log line is noise.

#: A new answer was generated and stored. (HTTP 201)
CREATED = "CREATED"
#: This idempotency key already produced an answer; it is being served again,
#: and no model call was made. (HTTP 200)
REPLAYED = "REPLAYED"
#: The same key arrived carrying a DIFFERENT question. A client bug, never a
#: retry — answering it would attach one question's answer to another's key.
KEY_CONFLICT = "KEY_CONFLICT"
#: The generation budget for this student is exhausted. (HTTP 429)
RATE_LIMITED = "RATE_LIMITED"
#: The student has no `Student` row — a roster re-import can do this. The budget
#: is refunded, because no answer is possible until the record is restored and
#: charging for the diagnosis would replace it with a rate-limit message.
NO_STUDENT_RECORD = "NO_STUDENT_RECORD"
#: The adviser raised. The question is stored and resumable. (HTTP 503)
GENERATION_FAILED = "GENERATION_FAILED"
#: Nothing was asked. (HTTP 400)
QUESTION_EMPTY = "QUESTION_EMPTY"
#: Longer than `MAX_QUESTION_CHARS`. (HTTP 400)
QUESTION_TOO_LONG = "QUESTION_TOO_LONG"


class ConversationNotFound(Exception):
    """No conversation with that id belongs to this principal.

    Deliberately not `Http404`: this module has no transport in it, and a
    Telegram handler catching a Django HTTP exception would be the coupling this
    extraction removes. The HTTP view re-raises it as `Http404` — which is also
    what a *malformed* id produces, because a malformed id and someone else's id
    should tell the caller the same thing: nothing here.
    """


@dataclass(frozen=True)
class TurnResult:
    """What happened, in a shape both a browser and a chat bot can render.

    The messages are the SAVED rows, not the transient result that produced them,
    so "what the student sees is what was stored" stays mechanically true on
    every channel rather than being a property each transport has to re-establish.
    """

    outcome: str
    conversation: AdvisorConversation | None = None
    student_message: AdvisorMessage | None = None
    assistant_message: AdvisorMessage | None = None
    #: Seconds. Only meaningful for RATE_LIMITED.
    retry_after: int = 0

    @property
    def answered(self) -> bool:
        """Whether a student-visible answer exists to deliver."""
        return self.assistant_message is not None


def student_id_of(principal: AdvisorPrincipal) -> int:
    """The student this turn is about, re-checked rather than assumed.

    `AdvisorPrincipal.__post_init__` already refuses a STUDENT principal with no
    id, but this service also takes principals built by callers rather than by
    `for_student` — the eval battery and the Telegram link both do that. A STAFF
    principal's `student_id` is legitimately `None`, and letting one through here
    would not fail loudly: `consume(GENERATION, None)` keys the rate-limit bucket
    on the string "None", so every such call would share one budget, and
    `filter(student_id=None)` matches nothing while looking like a lookup.

    So the narrowing is a guard, not a cast.
    """
    from core.services.rbac import ROLE_STUDENT

    if principal.role != ROLE_STUDENT or not isinstance(principal.student_id, int):
        raise IdentityError("This operation requires a student principal.")
    return principal.student_id


def open_conversation(*, principal: AdvisorPrincipal, conversation_id: Any) -> AdvisorConversation:
    """Fetch a conversation this principal owns, or refuse.

    Ownership is part of the filter, exactly as it is in the view this was taken
    from. Fetching by id and checking `.student_id` afterwards would be one
    forgotten line away from a leak, and the forgotten line would look like
    working code.
    """
    import uuid as _uuid

    try:
        parsed = _uuid.UUID(str(conversation_id))
    except (ValueError, AttributeError, TypeError):
        raise ConversationNotFound(str(conversation_id)) from None
    conversation = AdvisorConversation.objects.filter(
        id=parsed, student_id=student_id_of(principal)
    ).first()
    if conversation is None:
        raise ConversationNotFound(str(conversation_id))
    return conversation


def start_conversation(*, principal: AdvisorPrincipal, title: str = "") -> AdvisorConversation:
    """A fresh thread owned by this principal."""
    return AdvisorConversation.objects.create(
        student_id=student_id_of(principal), title=str(title or "").strip()[:MAX_TITLE_CHARS]
    )


def _title_from(question: str) -> str:
    words = question.strip().split()
    return " ".join(words[:8])[:MAX_TITLE_CHARS]


def run_advisor_turn(
    *,
    principal: AdvisorPrincipal,
    conversation: AdvisorConversation,
    question: str,
    idempotency_key: str = "",
    channel_profile: str = "",
) -> TurnResult:
    """Ask one question and persist the whole turn.

    `principal` and `conversation` are separate parameters because ownership must
    already have been proved — by `open_conversation`, or by a channel that
    created the conversation itself. Passing a conversation id here instead would
    invite a caller to pass one it never checked.

    The adviser call sits OUTSIDE the transaction and the writes sit inside it. A
    model that takes ninety seconds must not hold a database transaction open for
    ninety seconds, and the writes must still be all-or-nothing: an assistant
    message without its citations would be an uncited answer that looks
    cited-by-omission rather than one that failed.
    """
    from core.services.student_advisor_v2 import answer_student_advisor

    student_id = student_id_of(principal)

    question = str(question or "").strip()
    if not question:
        return TurnResult(outcome=QUESTION_EMPTY, conversation=conversation)
    if len(question) > MAX_QUESTION_CHARS:
        return TurnResult(outcome=QUESTION_TOO_LONG, conversation=conversation)

    key = str(idempotency_key or "").strip()[:64]
    profile = str(channel_profile or "").strip()[:32]
    from core.services.advisor_channel_privacy import (
        TELEGRAM_SAFE_PROFILE,
        TELEGRAM_UNVALIDATED_PROFILE,
    )

    initial_generation_profile = (
        TELEGRAM_UNVALIDATED_PROFILE if profile == TELEGRAM_SAFE_PROFILE else profile
    )
    request_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()

    student_message = None
    if key:
        existing = conversation.messages.filter(
            idempotency_key=key, role=AdvisorMessage.ROLE_STUDENT
        ).first()
        if existing is not None:
            resumed = _resume_or_replay(
                conversation,
                existing,
                request_hash,
                expected_profile=profile,
            )
            if isinstance(resumed, TurnResult):
                return resumed
            student_message = resumed

    # Charged here: after ownership, after validation, and after the idempotency
    # branch that may replay a stored answer — but still before the model is
    # called, which is what admission control requires. Charged earlier, a replay
    # served entirely from storage cost the student the same as a new question.
    decision = spend_budget(GENERATION, student_id)
    if not decision.allowed:
        return TurnResult(
            outcome=RATE_LIMITED, conversation=conversation, retry_after=decision.retry_after
        )

    if student_message is None:
        try:
            with transaction.atomic():
                student_message = AdvisorMessage.objects.create(
                    conversation=conversation,
                    role=AdvisorMessage.ROLE_STUDENT,
                    content=question,
                    idempotency_key=key,
                    request_hash=request_hash,
                    generation_profile=initial_generation_profile,
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
            resumed = _resume_or_replay(
                conversation,
                existing,
                request_hash,
                expected_profile=profile,
            )
            if isinstance(resumed, TurnResult):
                return resumed
            student_message = resumed

    try:
        # The turns the student already saw, so a follow-up has something to refer
        # to. Excludes THIS question, which was written to the database before
        # generation and would otherwise arrive twice — and twice again on a retry,
        # which reuses the same row.
        from core.services.advisor_channel_privacy import (
            TELEGRAM_SAFE_PROFILE,
            is_telegram_safe_profile,
            project_history,
        )

        if is_telegram_safe_profile(channel_profile):
            history = load_profiled_history(
                conversation,
                channel_profile=TELEGRAM_SAFE_PROFILE,
                exclude_message_id=student_message.pk,
            )
        else:
            history = load_visible_history(conversation, exclude_message_id=student_message.pk)

        prior_presentation = load_latest_profile_presentation(
            conversation,
            channel_profile=str(channel_profile or ""),
            exclude_message_id=student_message.pk,
        )

        result = answer_student_advisor(
            question=question,
            principal=principal,
            history=project_history(history, profile=channel_profile),
            prior_presentation=prior_presentation,
            channel_profile=channel_profile,
        )
    except ValueError as exc:
        # The student's own row is gone — a roster re-import can do this. Passing a
        # real identity makes it raise where the general-mode stub used to answer
        # blandly and work, so without this branch the student gets a failure on
        # every question they ever ask, permanently, with no explanation. Do NOT
        # fall back to the general context: an answer that silently stops being
        # about them is the failure the principal work removed.
        logger.warning(
            "Adviser has no student record for conversation %s: %s", conversation.id, exc
        )
        # No answer is possible for this student until their record is restored, so
        # charging them would spend the whole allowance on a diagnosis and then
        # replace it with a rate-limit message that hides the diagnosis.
        refund_budget(GENERATION, student_id)
        student_message.status = AdvisorMessage.STATUS_FAILED
        student_message.save(update_fields=["status"])
        return TurnResult(
            outcome=NO_STUDENT_RECORD,
            conversation=conversation,
            student_message=student_message,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Adviser generation failed for conversation %s", conversation.id)
        student_message.status = AdvisorMessage.STATUS_FAILED
        student_message.save(update_fields=["status"])
        return TurnResult(
            outcome=GENERATION_FAILED,
            conversation=conversation,
            student_message=student_message,
        )

    assistant_message = persist_answer(conversation, student_message, result)
    return TurnResult(
        outcome=CREATED,
        conversation=conversation,
        student_message=student_message,
        assistant_message=assistant_message,
    )


def _resume_or_replay(
    conversation: AdvisorConversation,
    existing: AdvisorMessage,
    request_hash: str,
    *,
    expected_profile: str = "",
) -> AdvisorMessage | TurnResult:
    """Decide what a repeated idempotency key means for THIS turn.

    Idempotency exists so a retry cannot produce a second answer to a question
    already answered. It must not also mean a question that FAILED can never be
    answered at all — and it did: replaying returned the failed student message
    with no assistant message, so every retry succeeded at doing nothing and the
    student's question was permanently stuck.

    A pending turn is still being generated, so replaying is right. A finished one
    has its answer. A failed one is unfinished, and gets resumed.

    Returns the message to answer, or a finished `TurnResult` to return
    immediately. A union rather than a pair of optionals: the pair let a caller
    read both as `None` and carry on, which is the one outcome this function never
    produces.
    """
    from core.services.advisor_channel_privacy import (
        TELEGRAM_SAFE_PROFILE,
        TELEGRAM_UNVALIDATED_PROFILE,
        TELEGRAM_WITHHELD_PROFILE,
    )

    existing_profile = str(existing.generation_profile or "")
    compatible_profiles = {expected_profile}
    if expected_profile == TELEGRAM_SAFE_PROFILE:
        # Telegram first writes an unvalidated provenance marker. Only the
        # transport-side output boundary can promote it to safe or withheld.
        # All three remain idempotently replayable, while history admits only the
        # final safe state.
        compatible_profiles.update({TELEGRAM_UNVALIDATED_PROFILE, TELEGRAM_WITHHELD_PROFILE})
    if existing_profile not in compatible_profiles:
        return TurnResult(
            outcome=KEY_CONFLICT,
            conversation=conversation,
            student_message=existing,
        )
    if existing.request_hash != request_hash:
        # The same key carrying a different question is a client bug, not a retry.
        # Answering it would silently attach one question's answer to another's key.
        return TurnResult(outcome=KEY_CONFLICT, conversation=conversation, student_message=existing)
    if not is_resumable(existing):
        return _replay(conversation, existing)

    # Claim it with ONE conditional UPDATE. Reading the status and then writing it
    # is two statements with a gap in between, and a double-clicked Retry fits
    # inside that gap: both requests see FAILED, both claim it, both call the
    # model, and the student gets two answers to one question.
    claim = AdvisorMessage.objects.filter(pk=existing.pk, status=existing.status)
    if existing.status == AdvisorMessage.STATUS_PENDING:
        # PENDING -> PENDING does not change the status, so status alone is not a
        # compare-and-swap: two stale retries both update one row and both call the
        # model. The generation timestamp is the fencing value for that state.
        claim = claim.filter(generation_started_at=existing.generation_started_at)
    claimed = claim.update(
        status=AdvisorMessage.STATUS_PENDING,
        generation_started_at=timezone.now(),
    )
    if not claimed:
        # Someone else claimed it first. They are generating; we replay.
        existing.refresh_from_db()
        return _replay(conversation, existing)

    existing.status = AdvisorMessage.STATUS_PENDING
    return existing


def _replay(conversation: AdvisorConversation, student_message: AdvisorMessage) -> TurnResult:
    """Return the turn this key already produced, rather than generating again."""
    return TurnResult(
        outcome=REPLAYED,
        conversation=conversation,
        student_message=student_message,
        assistant_message=paired_answer(conversation, student_message),
    )


def paired_answer(
    conversation: AdvisorConversation, student_message: AdvisorMessage
) -> AdvisorMessage | None:
    """The assistant turn that answered this question, if there is one."""
    assistant = (
        conversation.messages.filter(in_reply_to=student_message)
        .prefetch_related("citations")
        .order_by("sequence", "created_at")
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
            .order_by("sequence", "created_at")
            .first()
        )
    return assistant


#: How long a turn may sit PENDING before we accept that whatever was generating it
#: is gone - because resuming a turn that IS still running would answer it twice.
#: Sized to the TURN's ceiling, not one model call's: the adviser turn carries a
#: 60-second wall-clock budget, so three minutes is already generous, and the
#: previous fifteen left an edge-killed question replaying its own empty PENDING
#: row at the student for a quarter of an hour.
STALE_GENERATION = timedelta(minutes=3)


def is_resumable(message: AdvisorMessage) -> bool:
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


def persist_answer(
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
    from core.models import AdvisorMessageCitation
    from core.services.advisor_evidence_audit import (
        normalise_evidence_audit,
        normalise_model_revision,
        normalise_prompt_version,
    )
    from core.services.advisor_outcome import derive_outcome
    from core.services.advisor_presentations import normalise_presentation
    from core.services.virtual_advisor import _claimed_citations

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
    stored_presentation = (
        {}
        if bool(agent.get("grounding_refused"))
        else normalise_presentation(result.get("presentation"))
    )

    with transaction.atomic():
        assistant = AdvisorMessage.objects.create(
            conversation=conversation,
            in_reply_to=student_message,
            role=AdvisorMessage.ROLE_ASSISTANT,
            content=answer,
            presentation=stored_presentation,
            grounding_state=str(agent.get("policy_grounding") or ""),
            final_disposition=outcome.disposition,
            reason_codes=outcome.reason_codes,
            missing_information=outcome.missing_information,
            outcome_schema_version=outcome.schema_version,
            model_name=str(result.get("model") or ""),
            model_revision=normalise_model_revision(agent.get("model_revision")),
            route=(
                AdvisorMessage.ROUTE_AGENT
                if agent.get("loop_used")
                else AdvisorMessage.ROUTE_SEEDED_FALLBACK
            ),
            prompt_version=normalise_prompt_version(agent.get("prompt_version")),
            evidence_audit=normalise_evidence_audit(agent.get("evidence_audit")),
            status=status,
        )
        for claim in claimed:
            source = entitled[claim["policy_id"]]
            AdvisorMessageCitation.objects.create(
                message=assistant,
                policy_id=source.get("policy_id") or "",
                document_title=source.get("document_title") or "",
                edition=str(source.get("edition") or ""),
                page=page_shown(claim, source),
                effective_from=str(source.get("effective_from") or ""),
                effective_to=str(source.get("effective_to") or ""),
                authority_status="AUTHORITY_APPROVED",
                validation_status=AdvisorMessageCitation.VALID,
                source_version_hash=source_hash(source),
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


def page_shown(claim: dict[str, Any], source: dict[str, Any]) -> str:
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


def source_hash(citation: dict[str, Any]) -> str:
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


# ── escalation: handing one turn to a person ─────────────────────

#: A case already exists and is open for this turn; it is returned unchanged.
ESCALATION_EXISTS = "ESCALATION_EXISTS"
#: Created. (HTTP 201)
ESCALATION_CREATED = "ESCALATION_CREATED"
#: This answer does not warrant a human, and the student did not explicitly ask
#: for one. (HTTP 409)
ESCALATION_NOT_WARRANTED = "ESCALATION_NOT_WARRANTED"
#: The escalation budget is exhausted. (HTTP 429)
ESCALATION_RATE_LIMITED = "ESCALATION_RATE_LIMITED"


@dataclass(frozen=True)
class EscalationResult:
    outcome: str
    escalation: AdvisorEscalation | None = None
    retry_after: int = 0


def escalate_turn(
    *,
    principal: AdvisorPrincipal,
    message: AdvisorMessage,
    student_note: str = "",
    student_requested: bool = False,
) -> EscalationResult:
    """Hand one answered turn to a human adviser.

    The whole thing is one transaction over a LOCKED source message, because three
    things have to agree afterwards: a case exists, its evidence matches the answer
    that produced it, and that answer says it was escalated. A database holding an
    open case whose source turn still reads ABSTAIN is one where the adviser and
    the student are looking at different accounts of the same event.

    `message` must already have been fetched under this principal's ownership.
    """
    student_id = student_id_of(principal)
    note = str(student_note or "").strip()[:MAX_NOTE_CHARS]

    with transaction.atomic():
        # Locked for the whole decision: without it two taps of the button both see
        # no open case and both try to create one.
        locked = (
            AdvisorMessage.objects.select_for_update()
            .select_related("in_reply_to", "conversation")
            .get(pk=message.pk)
        )

        # Ownership, re-proved here rather than trusted from the caller. Both
        # callers today do filter on it — but "every caller remembers" is exactly
        # the shape of the authorisation defect PR #61 fixed, and a service that
        # locks a row by primary key is one careless call site away from repeating
        # it. The check is one comparison against a row already loaded.
        if locked.conversation.student_id != student_id:
            raise ConversationNotFound(str(locked.conversation_id))

        existing = (
            AdvisorEscalation.objects.filter(source_message=locked)
            .exclude(status__in=AdvisorEscalation.TERMINAL_STATUSES)
            .first()
        )
        if existing is not None:
            # The same request, not a second one. Returned as-is: regenerating the
            # summary would rewrite what an adviser may already have read.
            return EscalationResult(outcome=ESCALATION_EXISTS, escalation=existing)

        if not may_escalate(locked, student_requested=student_requested):
            return EscalationResult(outcome=ESCALATION_NOT_WARRANTED)

        decision = spend_budget(ESCALATION, student_id)
        if not decision.allowed:
            return EscalationResult(
                outcome=ESCALATION_RATE_LIMITED, retry_after=decision.retry_after
            )

        evidence = build_evidence(locked)
        try:
            # Its OWN savepoint. An IntegrityError marks the enclosing atomic block
            # broken, so without this the recovery query below cannot run — the
            # concurrency handler would itself raise, in production, on exactly the
            # contended path it exists to survive.
            with transaction.atomic():
                escalation = AdvisorEscalation.objects.create(
                    conversation=locked.conversation,
                    source_message=locked,
                    student_id=student_id,
                    reason_code=escalation_reason(locked, student_requested=student_requested),
                    student_note=note,
                    generated_summary=deterministic_summary(evidence),
                    evidence_snapshot=evidence,
                )
        except IntegrityError:
            # The partial unique index caught a concurrent creation. That request
            # won; this one reports its result rather than inventing a second case.
            winner = (
                AdvisorEscalation.objects.filter(source_message=locked)
                .exclude(status__in=AdvisorEscalation.TERMINAL_STATUSES)
                .first()
            )
            if winner is None:
                raise
            return EscalationResult(outcome=ESCALATION_EXISTS, escalation=winner)

        # The turn now says it was escalated — but keeps the reasons that
        # constrained the ANSWER. Those record why the adviser stopped where it
        # did; the case records why a person was asked, and for a student who
        # simply wanted a human to look, that is a different fact.
        locked.final_disposition = FinalDisposition.ESCALATE
        locked.status = AdvisorMessage.STATUS_ESCALATED
        locked.save(update_fields=["final_disposition", "status"])

    return EscalationResult(outcome=ESCALATION_CREATED, escalation=escalation)


__all__ = [
    "CREATED",
    "ESCALATION_CREATED",
    "ESCALATION_EXISTS",
    "ESCALATION_NOT_WARRANTED",
    "ESCALATION_RATE_LIMITED",
    "GENERATION_FAILED",
    "KEY_CONFLICT",
    "MAX_QUESTION_CHARS",
    "MAX_TITLE_CHARS",
    "NO_STUDENT_RECORD",
    "QUESTION_EMPTY",
    "QUESTION_TOO_LONG",
    "RATE_LIMITED",
    "REPLAYED",
    "ConversationNotFound",
    "EscalationResult",
    "TurnResult",
    "escalate_turn",
    "is_resumable",
    "open_conversation",
    "paired_answer",
    "persist_answer",
    "run_advisor_turn",
    "start_conversation",
    "student_id_of",
]
