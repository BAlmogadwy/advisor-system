"""What a Telegram update means, and what the gateway does about it.

This module is the channel, and it is deliberately thin: it decides who is asking,
refuses everything it is not sure about, and hands the actual question to
`core.services.advisor_turn` — the same application service the web chat calls.
There is no prompt here, no tool list, no model client and no second adviser. If
this file ever needs to know what a prerequisite is, something has gone wrong.

**Parsing is refusal-first.** `parse_update` returns `None` for everything that is
not a private text message from a real user, and it is the only place that
decision is made. A filter applied later — in the router, in the handler — is a
filter that some future branch reaches around. In particular:

* group, supergroup and channel chats are dropped before any lookup, so a bot
  added to a class group cannot be made to read out somebody's record;
* edited messages, callback queries, inline queries and channel posts are dropped,
  because the webhook subscribes to `message` only and anything else arriving is
  either a misconfiguration or an attempt;
* photos, documents, voice notes, contacts and locations are dropped without being
  fetched. Downloading a file from a webhook payload is a request to a
  third-party URL made on the strength of an untrusted message.

**A private chat's room id must equal its sender id.** Telegram guarantees it, and
checking it costs one comparison — but it means the gateway never has to store a
chat id alongside a user id, and a payload that claims otherwise is malformed by
construction rather than something to reason about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import AdvisorMessage, Student, StudentCourse
from core.services import advisor_turn
from core.services.advisor_channel_privacy import (
    TELEGRAM_SAFE_IDEMPOTENCY_PREFIX,
    TELEGRAM_SAFE_PROFILE,
    TELEGRAM_UNVALIDATED_PROFILE,
    TELEGRAM_WITHHELD_PROFILE,
)
from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.rbac import ROLE_STUDENT

from . import linking, messages
from .formatting import render_answer
from .models import TelegramLink, TelegramUpdateReceipt
from .transport import send_photo, send_text

logger = logging.getLogger(__name__)

#: The only `allowed_updates` the webhook is registered for, restated here so the
#: server refuses what it did not ask for even if the registration drifts.
SUPPORTED_UPDATE_KEYS = ("message",)

#: Commands a chat may use before it has proved who it is. Everything else gets
#: the linking prompt and nothing about any student.
UNAUTHENTICATED_COMMANDS = frozenset({"/start", "/link", "/confirm", "/help", "/privacy"})

#: Most pictures one answer may send. Telegram allows ten in an album; four is
#: what a student will actually look at, and each one costs a browser render.
MAX_CARD_IMAGES = 4


_PERSONAL_ACADEMIC_RECORD = re.compile(
    r"(?:\bmy\s+(?:cumulative\s+|term\s+)?gpa\b|"
    r"\bmy\s+(?:cgpa|mark|marks|grade|grades|result|results|transcript|academic\s+standing|failed\s+courses?)\b|"
    r"\b(?:what|how)\s+(?:is|was)\s+my\s+(?:gpa|cgpa|grade|mark|score|result)\b|"
    r"\bwhat\s+(?:grade|mark|score|result)\s+did\s+i\s+get\b|"
    r"\bwhat\s+did\s+i\s+get\b|"
    r"\b(?:show|list|give|tell)\s+me\s+(?:the\s+)?(?:grades?|marks?|results?|transcript|academic\s+standing|courses?\s+i\s+failed)\b|"
    r"\b(?:list|show)\s+(?:the\s+)?(?:courses?|classes?|subjects?)\s+i\s+(?:failed|did\s+not\s+pass)\b|"
    r"\bhow\s+many\s+(?:failed\s+)?(?:courses?|classes?|subjects?)\s+(?:are\s+)?(?:on|in)\s+my\s+(?:record|transcript)\b|"
    r"\b(?:what|which)\s+(?:courses?|classes?|subjects?)\s+(?:have\s+i|i\s+have)\s+not\s+passed\b|"
    r"\b(?:did\s+i|have\s+i)\s+(?:pass|fail)\b|"
    r"\bam\s+i\s+(?:passing|failing)\b|"
    r"\bam\s+i\s+(?:on|under)\s+academic\s+probation\b|"
    r"\bwhere\s+do\s+i\s+stand\s+academically\b|"
    r"\b(?:name|show|list)\s+(?:my\s+)?unsuccessful\s+(?:courses?|classes?|modules?|subjects?)\b|"
    r"\bwhich\s+(?:course|class|module|subject)\s+am\s+i\s+retaking\b|"
    r"\bwhat\s+(?:letter|number)\s+(?:appears|is\s+recorded)\s+(?:beside|next\s+to)\b|"
    r"\b(?:what\s+about\s+mine|and\s+mine|what\s+is\s+mine)\b|"
    r"(?:ممكن\s+)?(?:تعرض|توريني|تطلع)\s+(?:لي\s+)?درجات\s+(?:المواد|المقررات)|"
    r"(?:كم|وش|ايش|إيش|قد\s*ايش).*?(?:معدلي|نسبتي|تقديري|درجتي|درجاتي|علامتي|نتيجتي)|"
    r"(?:معدلي|المعدل\s+حقي|(?:gpa|cgpa)\s+حقي|نسبتي|تقديري|درجتي|درجاتي|علامتي|نتيجتي|سجلي\s+الأكاديمي)|"
    r"(?:كم|وش|ايش|إيش)\s+جبت(?:\s+في)?|"
    r"(?:اعرض|أعرض|ورني|طلع|هات|اعطني|أعطني)\s+.*?(?:درجاتي|علاماتي|نتيجتي|سجلي\s+الأكاديمي|المواد\s+اللي\s+رسبت)|"
    r"(?:نتيجة|درجة|علامة)\s+(?:مقرر|مادة)\s+[A-Z]{2,5}\s*\d{2,4}|"
    r"(?:هل|وش|ايش|إيش).*?(?:رسبت|نجحت|ناجح|راسب).*?(?:في|بـ|ب)?|"
    r"(?:وش|ما|ايش).*?(?:المواد|المقررات).*?(?:رسبت|راسب)|"
    r"(?:المواد|المقررات).*?(?:اللي|التي).*?(?:رسبت|ما\s+نجحت)|"
    r"(?:انا|أنا).*?(?:ناجح|نجحت|راسب|رسبت|انذار\s+اكاديمي|إنذار\s+أكاديمي)|"
    r"(?:طيب\s+)?(?:وش\s+عني|حقي\s*\??|بالنسبة\s+لي\s*\??))",
    re.IGNORECASE,
)

_PERSONAL_ACADEMIC_OUTPUT = re.compile(
    r"(?:\byour\s+(?:current\s+|cumulative\s+|term\s+)?(?:gpa|cgpa|mark|marks|grade|grades|score|result|results|transcript|academic\s+standing|failed\s+courses?)\b|"
    r"\byou\s+(?:received|got|scored|(?:have\s+)?failed|(?:have\s+)?passed)\b|"
    r"\b(?:courses?|classes?|subjects?)\s+you\s+(?:failed|did\s+not\s+pass)\b|"
    r"(?:معدلك|نسبتك|تقديرك|درجتك|درجاتك|علامتك|علاماتك|نتيجتك)|"
    r"(?:أنت|انت).*?(?:ناجح|راسب|نجحت|رسبت)|"
    r"(?:نجحت|رسبت)\s+في)",
    re.IGNORECASE,
)


def requires_secure_record_surface(question: str) -> bool:
    """Whether an exact personal result must stay off the Telegram channel.

    General policy questions such as "how is GPA calculated?" remain answerable.
    The boundary is for the caller's recorded result, not for the academic topic.
    """

    text = str(question or "")
    text = re.sub(r"[ًٌٍَُِّْـ]", "", text)
    return bool(_PERSONAL_ACADEMIC_RECORD.search(text))


def contains_personal_record_output(
    answer: str,
    *,
    student_id: int | None = None,
    question: str = "",
) -> bool:
    """Fail closed if a model volunteers a personal result despite input gating.

    Language patterns catch explicit phrasing. The second layer compares output
    with the student's structured GPA and latest course results, so terse forms
    such as ``GPA: 2.86`` or ``CS113 - B`` cannot bypass the boundary merely by
    omitting a possessive phrase.
    """

    text = _normalise_record_text(answer)
    if _PERSONAL_ACADEMIC_OUTPUT.search(text):
        return True
    if not isinstance(student_id, int) or student_id <= 0:
        return False

    student = Student.objects.filter(student_id=student_id).values("gpa").first()
    stored_gpa = student.get("gpa") if student else None
    if stored_gpa is not None:
        expected_gpa = float(stored_gpa)
        labelled_gpas = re.findall(
            r"(?:\b(?:gpa|cgpa)\b(?:\s+on\s+file)?\s*(?::|=|\-|is)\s*"
            r"([0-5](?:[.,]\d{1,4})?)|"
            r"([0-5](?:[.,]\d{1,4})?)\s*\b(?:gpa|cgpa)\b)",
            text,
            re.IGNORECASE,
        )
        for before, after in labelled_gpas:
            raw_number = before or after
            try:
                shown = float(raw_number.replace(",", "."))
            except ValueError:
                continue
            if abs(shown - expected_gpa) < 0.0051:
                return True

    folded = text.casefold()
    rows = (
        StudentCourse.objects.filter(student_id=student_id)
        .select_related("course")
        .values("course__course_code", "status", "grade", "mark")
    )
    for row in rows:
        code = str(row.get("course__course_code") or "").strip()
        if not code:
            continue
        code_match = re.search(_course_code_pattern(code), text, re.IGNORECASE)
        if code_match is None:
            continue
        record_context = f"{question}\n{text}"
        if row.get("status") == StudentCourse.Status.FAILED and re.search(
            r"(?:failed|failure|unsuccessful|did\s+not\s+pass|retak(?:e|ing)|"
            r"رس(?:ب|وب)|راسب|إعادة|اعادة)",
            record_context,
            re.IGNORECASE,
        ):
            return True

        left = max(0, code_match.start() - 100)
        right = min(len(text), code_match.end() + 100)
        neighbourhood = folded[left:right]
        grade = str(row.get("grade") or "").strip().casefold()
        if grade and re.search(
            rf"(?:[-:=]\s*|\b(?:grade|result|التقدير|النتيجة)\s*(?:is|هي|:)?\s*)"
            rf"{re.escape(grade)}(?![\w])",
            neighbourhood,
            re.IGNORECASE,
        ):
            return True
        mark = row.get("mark")
        if mark is not None and _contains_exact_mark(neighbourhood, float(mark)):
            return True
    return False


def _normalise_record_text(value: Any) -> str:
    text = re.sub(r"[ًٌٍَُِّْـ]", "", str(value or ""))
    return text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))


def _course_code_pattern(code: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", code)
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", compact)
    if match:
        return rf"(?<![A-Za-z0-9]){re.escape(match.group(1))}\s*[- ]?\s*{re.escape(match.group(2))}(?![A-Za-z0-9])"
    return rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])"


def _contains_exact_mark(text: str, mark: float) -> bool:
    for raw_number in re.findall(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,3})?)(?!\d)", text):
        try:
            shown = float(raw_number.replace(",", "."))
        except ValueError:
            continue
        if abs(shown - mark) < 0.0051:
            return True
    return False


@dataclass(frozen=True)
class InboundMessage:
    """A private text message from one Telegram user. The only thing acted on."""

    update_id: int
    telegram_user_id: int
    chat_id: int
    text: str

    @property
    def command(self) -> str:
        """The leading `/command`, lower-cased, with any `@botname` suffix removed.

        Telegram appends `@thebot` to commands in groups; groups never reach here,
        but a client may send the suffixed form in a private chat too and a router
        that compares against the raw token would call it unknown.
        """
        head = self.text.strip().split(maxsplit=1)[0] if self.text.strip() else ""
        if not head.startswith("/"):
            return ""
        return head.split("@", 1)[0].lower()


def parse_update(payload: Any) -> InboundMessage | None:
    """The one place an update becomes something the gateway will act on.

    Returns `None` — meaning "acknowledge and do nothing" — for every shape that
    is not a private text message. `None` is not an error: Telegram must still get
    its `200`, or it redelivers.
    """
    if not isinstance(payload, dict):
        return None

    update_id = payload.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        # Without an update id there is no idempotency key, and without an
        # idempotency key a retry becomes a second answer. Refuse.
        return None

    # `message` only. `edited_message`, `channel_post`, `callback_query`,
    # `inline_query` and the rest are not subscribed to and are not handled.
    # Resolved THROUGH the tuple rather than beside it: a constant that is checked
    # and then ignored in favour of a hard-coded key looks like a control and is a
    # comment, and the two drift the moment either is edited.
    message = next((payload[key] for key in SUPPORTED_UPDATE_KEYS if key in payload), None)
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None

    # Private chats only, and the check is on the chat's declared type rather than
    # on an id heuristic.
    if str(chat.get("type") or "") != "private":
        return None

    if sender.get("is_bot"):
        return None

    chat_id = chat.get("id")
    user_id = sender.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    # In a private chat these are the same number. Requiring it means the link
    # table needs only one column to answer both "who is this" and "where do I
    # reply", and a payload that separates them is refused rather than guessed at.
    if chat_id != user_id:
        return None

    text = message.get("text")
    if not isinstance(text, str):
        # Photos, documents, voice notes, contacts, locations, stickers. Nothing is
        # fetched, nothing is stored; the caller answers with a short refusal.
        return InboundMessage(
            update_id=update_id, telegram_user_id=user_id, chat_id=chat_id, text=""
        )

    return InboundMessage(
        update_id=update_id,
        telegram_user_id=user_id,
        chat_id=chat_id,
        # Trimmed to the adviser's own ceiling plus a little, so an enormous paste
        # is refused by length rather than carried around.
        text=text[: advisor_turn.MAX_QUESTION_CHARS + 1],
    )


def claim_update(update_id: int) -> bool:
    """Record this update as seen. False if it already was.

    Telegram redelivers any update whose webhook call did not return `200`
    promptly — including one that timed out *after* the answer was generated. The
    claim is a primary-key insert, so two concurrent redeliveries cannot both win.
    """
    try:
        with transaction.atomic():
            TelegramUpdateReceipt.objects.create(update_id=int(update_id))
    except IntegrityError:
        return False
    return True


def is_enabled() -> bool:
    """Whether the channel is switched on. Read at call time, default off."""
    return bool(getattr(settings, "TELEGRAM_ADVISOR_ENABLED", False))


def _principal_for(link: TelegramLink) -> AdvisorPrincipal:
    """The self-only student principal for a verified link.

    Built from the link row and from nothing in the message. `AdvisorPrincipal`
    refuses a non-positive id rather than clamping it, which is what stops a
    sentinel from becoming student number 1.
    """
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=int(link.student_id))


def _current_conversation(link: TelegramLink, principal: AdvisorPrincipal) -> Any:
    """The thread this chat is in, creating one on first use.

    Re-checks ownership of a stored conversation instead of trusting the foreign
    key: a link whose student changed — which cannot happen today, but is one
    migration away from being possible — must not carry the previous student's
    thread with it.
    """
    conversation = link.current_conversation
    if conversation is not None and conversation.student_id == principal.student_id:
        return conversation
    conversation = advisor_turn.start_conversation(principal=principal)
    link.current_conversation = conversation
    link.save(update_fields=["current_conversation"])
    return conversation


def _web_url(conversation_id: Any = None, *, path: str = "/student/advisor/") -> str:
    """The web adviser screen, opened ON THIS CONVERSATION.

    The `?c=` is not decoration. The screen bootstraps its thread from that one
    parameter — `page-student-advisor.js` reads
    `new URLSearchParams(location.search).get('c')` and only calls
    `openConversation` when it is present — so a link without it renders the
    sidebar and no messages. To a student following "the full timetable is on the
    platform" from a chat, that is an empty page where their answer should be, and
    it looks like the data is missing rather than like the link is wrong.

    Safe to put in the URL: it is a random UUID, not a student identifier, and the
    screen still proves ownership server-side before returning a single message.
    The browser already carries it exactly this way.
    """
    base = str(getattr(settings, "TELEGRAM_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""
    url = f"{base}{path}"
    if conversation_id:
        url = f"{url}?c={conversation_id}"
    return url


def _link_url(raw_token: str) -> str:
    base = str(getattr(settings, "TELEGRAM_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/telegram/link/{raw_token}/"


# ── command handling ─────────────────────────────────────────────


def _handle_link(inbound: InboundMessage) -> list[str]:
    if linking.active_link_for_chat(inbound.telegram_user_id) is not None:
        return [messages.ALREADY_LINKED]
    issued = linking.issue_link_token(telegram_user_id=inbound.telegram_user_id)
    url = _link_url(issued.raw_token)
    if not url:
        # No public base URL means the invitation would be a token with nowhere to
        # go. Fail closed and say so, rather than sending a broken link.
        logger.warning("telegram: TELEGRAM_PUBLIC_BASE_URL is not set; cannot issue a link")
        return [messages.LINK_NOT_CONFIGURED]
    minutes = max(1, int(linking.token_ttl().total_seconds() // 60))
    return [messages.link_invitation(url, minutes)]


def _handle_confirm(inbound: InboundMessage) -> list[str]:
    """Complete a link with the code shown in the browser.

    This is the half of the ceremony that proves the browser and the chat are the
    same person. The lookup inside `confirm_link` is scoped to THIS chat, so an
    approval earned in somebody else's browser is unreachable from here — and a
    student who followed a forwarded link and then messaged the bot finds no
    approval of their own, which is the correct answer rather than a confusing one.
    """
    parts = inbound.text.strip().split(maxsplit=1)
    code = linking.normalise_code(parts[1]) if len(parts) > 1 else ""
    if not code:
        return [messages.CONFIRM_USAGE]
    try:
        linking.confirm_link(telegram_user_id=inbound.telegram_user_id, code=code)
    except linking.LinkError as exc:
        if exc.code == linking.STUDENT_ALREADY_LINKED:
            return [messages.STUDENT_ALREADY_LINKED_CHAT]
        if exc.code == linking.CHAT_ALREADY_LINKED:
            return [messages.CHAT_ALREADY_LINKED_CHAT]
        # Wrong code, expired approval, or nothing awaiting confirmation — one
        # answer, because distinguishing them says whether an approval exists for
        # a chat, which is what somebody holding a forwarded link wants to know.
        return [messages.CONFIRM_INVALID]
    return [messages.LINK_CONFIRMED]


def _handle_unlink(inbound: InboundMessage) -> list[str]:
    if linking.unlink_chat(inbound.telegram_user_id):
        return [messages.UNLINKED]
    return [messages.NOT_LINKED_TO_UNLINK]


def _handle_new(link: TelegramLink, principal: AdvisorPrincipal) -> list[str]:
    conversation = advisor_turn.start_conversation(principal=principal)
    link.current_conversation = conversation
    link.save(update_fields=["current_conversation"])
    return [messages.NEW_CONVERSATION]


def _handle_advisor(link: TelegramLink, principal: AdvisorPrincipal) -> list[str]:
    """Hand the most recent answered turn to a human adviser.

    Reuses `advisor_turn.escalate_turn`, which is the same call the web button
    makes — so a case raised from a phone is the same case, with the same lock over
    the source message and the same evidence snapshot.
    """
    conversation = link.current_conversation
    if conversation is None or conversation.student_id != principal.student_id:
        return [messages.ESCALATION_NOTHING_TO_ESCALATE]

    # Ownership is in the query here too, and `in_reply_to` is required: a case
    # whose evidence cannot include the question is one an adviser has to
    # reconstruct from the student's memory of it.
    message = (
        AdvisorMessage.objects.select_related("in_reply_to")
        .filter(
            conversation=conversation,
            conversation__student_id=principal.student_id,
            role=AdvisorMessage.ROLE_ASSISTANT,
            in_reply_to__isnull=False,
        )
        .order_by("-sequence", "-created_at")
        .first()
    )
    if message is None:
        return [messages.ESCALATION_NOTHING_TO_ESCALATE]

    result = advisor_turn.escalate_turn(
        principal=principal,
        message=message,
        # The student typed `/advisor`, which IS the explicit request the
        # escalation policy distinguishes from an adviser-initiated hand-off.
        student_requested=True,
    )
    if result.outcome == advisor_turn.ESCALATION_RATE_LIMITED:
        return [messages.rate_limited(result.retry_after)]
    if result.outcome == advisor_turn.ESCALATION_NOT_WARRANTED:
        return [messages.ESCALATION_NOT_WARRANTED]
    if result.escalation is None:
        # Unreachable today — CREATED and EXISTS both carry a case — but a channel
        # that renders `None.reference` fails with a stack trace where it should
        # fail with a sentence.
        return [messages.ESCALATION_NOTHING_TO_ESCALATE]
    if result.outcome == advisor_turn.ESCALATION_EXISTS:
        return [messages.ESCALATION_EXISTS.format(reference=result.escalation.reference)]
    return [messages.ESCALATION_CREATED.format(reference=result.escalation.reference)]


def handle_command(inbound: InboundMessage, link: TelegramLink | None) -> list[str]:
    """Everything that resolves without calling the model.

    Returns the messages to send, in order. Kept synchronous because none of it
    is slow, and a webhook that answers `/help` out of band for no reason is a
    webhook whose failure modes nobody understands.
    """
    command = inbound.command

    # Transport admission happens before the inline receipt is claimed in
    # ``views``. Keeping it there lets overload be acknowledged silently after one
    # explanatory notice instead of persisting and replying to every refused
    # update. This function owns command meaning only.

    if command == "/privacy":
        return [messages.PRIVACY]
    if command == "/start":
        return [messages.START]

    if link is None:
        # The allow-list IS the gate, rather than a list that documents one. Every
        # other command, and every free-text question, gets the linking prompt:
        # nothing about the sender, nothing about any student.
        if command not in UNAUTHENTICATED_COMMANDS:
            return [messages.NEEDS_LINK]
        if command == "/link":
            return _handle_link(inbound)
        if command == "/confirm":
            return _handle_confirm(inbound)
        return [messages.HELP_UNLINKED]

    try:
        principal = _principal_for(link)
    except IdentityError:
        # A link row whose student id will not resolve is a broken link, and the
        # safe reading of a broken link is "not linked".
        logger.warning("telegram: an active link carries an unusable student id; revoking")
        link.revoke()
        return [messages.NEEDS_LINK]

    if command == "/help":
        return [messages.HELP_LINKED]
    if command in {"/link", "/confirm"}:
        return [messages.ALREADY_LINKED]
    if command == "/unlink":
        return _handle_unlink(inbound)
    if command == "/new":
        return _handle_new(link, principal)
    if command == "/advisor":
        return _handle_advisor(link, principal)
    return [messages.UNKNOWN_COMMAND]


# ── the academic turn ────────────────────────────────────────────


def execute_durable_job(job: TelegramUpdateReceipt) -> dict[str, Any]:
    """Materialise one queued question or ordered command without delivering it.

    The queue persists this return value before it contacts Telegram. A delivery
    retry therefore resumes from the stored message cursor and never repeats the
    model call, starts another conversation, or raises a second escalation.
    """

    from .jobs import PermanentJobError, RetryJob

    link = linking.active_link_by_id(job.link_id)
    if link is None:
        raise PermanentJobError("link_revoked")

    try:
        principal = _principal_for(link)
    except IdentityError as exc:
        link.revoke()
        raise PermanentJobError("link_revoked") from exc

    if job.kind == TelegramUpdateReceipt.KIND_COMMAND:
        command = str(job.payload_text or "").strip().split(maxsplit=1)[0]
        command = command.split("@", 1)[0].lower()
        if command == "/new":
            return _execute_durable_new(job, link, principal)
        if command == "/advisor":
            return _execute_durable_advisor(job, link, principal)
        raise PermanentJobError("unsupported_command")

    if job.kind != TelegramUpdateReceipt.KIND_QUESTION:
        raise PermanentJobError("unsupported_job_kind")

    question = str(job.payload_text or "")
    if requires_secure_record_surface(question):
        return {
            "messages": [
                messages.sensitive_record_web_only(_web_url(link.current_conversation_id))
            ],
            "result_code": "secure_surface_required",
            "conversation_id": link.current_conversation_id,
        }

    conversation = _current_conversation(link, principal)
    result = advisor_turn.run_advisor_turn(
        principal=principal,
        conversation=conversation,
        question=question,
        idempotency_key=f"{TELEGRAM_SAFE_IDEMPOTENCY_PREFIX}{int(job.update_id)}",
        channel_profile=TELEGRAM_SAFE_PROFILE,
    )

    if result.outcome == advisor_turn.REPLAYED and result.assistant_message is None:
        # Another execution still owns this idempotent turn, or a dead execution
        # has not crossed the shared stale threshold yet. Marking the queue job
        # successful here would permanently replace the eventual answer with a
        # generic failure. Defer until the row is safely resumable instead.
        pending = result.student_message
        started = (
            pending.generation_started_at or pending.created_at
            if pending is not None
            else timezone.now()
        )
        remaining = advisor_turn.STALE_GENERATION - (timezone.now() - started)
        raise RetryJob(
            "generation_in_progress",
            delay_seconds=max(1.0, remaining.total_seconds() + 1.0),
        )

    # Revalidate the exact university account after generation. A deactivation,
    # role removal, scope change or unlink that lands mid-turn blocks delivery;
    # the stored web answer remains available to an authorised future session.
    if linking.active_link_by_id(link.pk) is None:
        raise PermanentJobError("link_revoked")

    assistant = result.assistant_message
    rendered = _render_outcome(result, question=question)
    result_code = result.outcome.lower()
    if _finalize_telegram_output(
        result,
        student_id=principal.student_id,
        question=question,
    ):
        rendered = [messages.sensitive_record_web_only(_web_url(conversation.pk))]
        result_code = "secure_output_withheld"
        output_withheld = True
    else:
        output_withheld = False
    return {
        "messages": rendered,
        "delivery_payload": {
            "items": _durable_delivery_items(
                result,
                rendered=rendered,
                output_withheld=output_withheld,
                student_id=principal.student_id,
                question=question,
            ),
        },
        "result_code": result_code,
        "assistant_message_id": assistant.pk if assistant is not None else None,
        "conversation_id": conversation.pk,
    }


def _execute_durable_new(
    job: TelegramUpdateReceipt,
    link: TelegramLink,
    principal: AdvisorPrincipal,
) -> dict[str, Any]:
    """Create exactly one conversation for `/new`, including after a retry."""

    from core.services.rate_limit import CONVERSATION
    from core.services.rate_limit import consume as spend_budget

    with transaction.atomic():
        locked_job = (
            TelegramUpdateReceipt.objects.select_for_update()
            .select_related("conversation")
            .get(update_id=job.update_id)
        )
        conversation = locked_job.conversation
        if conversation is not None and conversation.student_id == principal.student_id:
            # The executor committed this side effect before a worker died. Point
            # the link back at the same row and continue; never create a second.
            link.current_conversation = conversation
            link.save(update_fields=["current_conversation"])
        else:
            decision = spend_budget(CONVERSATION, int(principal.student_id or 0))
            if not decision.allowed:
                return {
                    "messages": [messages.rate_limited(decision.retry_after)],
                    "result_code": "rate_limited",
                }
            conversation = advisor_turn.start_conversation(principal=principal)
            locked_job.conversation = conversation
            locked_job.save(update_fields=["conversation"])
            link.current_conversation = conversation
            link.save(update_fields=["current_conversation"])

    return {
        "messages": [messages.NEW_CONVERSATION],
        "result_code": "new_conversation",
        "conversation_id": conversation.pk,
    }


def _execute_durable_advisor(
    job: TelegramUpdateReceipt,
    link: TelegramLink,
    principal: AdvisorPrincipal,
) -> dict[str, Any]:
    """Escalate and materialise its Telegram reply in one transaction.

    A worker may die after the case is committed but before the generic queue
    layer stores the reply. If an adviser closes that case before retry, calling
    ``escalate_turn`` again could create a second case. Keeping the side effect
    and the delivery payload in the same transaction removes that crash gap.
    """

    from . import jobs

    with transaction.atomic():
        locked_job = TelegramUpdateReceipt.objects.select_for_update().get(update_id=job.update_id)
        if locked_job.result_code or locked_job.delivery_payload:
            return {}

        rendered = _handle_advisor(link, principal)
        if not jobs.store_delivery(
            locked_job,
            messages=rendered,
            result_code="advisor_command",
            conversation_id=link.current_conversation_id,
        ):
            # The enclosing transaction also rolls back a newly-created case.
            raise jobs.RetryJob("lease_lost")

    # The queue layer refreshes the job after execution and observes the payload
    # already stored above, so it does not overwrite or repeat this command.
    return {}


def answer_question(*, link_id: Any, update_id: int, question: str, server_port: str = "") -> None:
    """Run one adviser turn for a linked chat and deliver the result.

    Runs off the webhook thread (see `runner`), so it re-reads the link rather
    than carrying an object across a thread boundary — the row may have been
    revoked in between, and a revoked link must not get an answer.

    Everything about *what* to say comes from `run_advisor_turn`; this function
    chooses only which of the outcomes maps to which sentence.
    """
    link = linking.active_link_by_id(link_id)
    if link is None:
        # Unlinked between asking and answering. Silence is correct: the chat is no
        # longer entitled to anything, including an explanation.
        logger.info("telegram: link revoked before the answer was delivered; dropping")
        return

    try:
        principal = _principal_for(link)
    except IdentityError:
        link.revoke()
        return

    if requires_secure_record_surface(question):
        send_text(
            chat_id=int(link.telegram_user_id),
            text=messages.sensitive_record_web_only(_web_url(link.current_conversation_id)),
        )
        return

    conversation = _current_conversation(link, principal)

    result = advisor_turn.run_advisor_turn(
        principal=principal,
        conversation=conversation,
        question=question,
        # Telegram's own counter. A redelivered update that slipped past the
        # receipt still cannot produce a second stored turn or a second model
        # call — the partial unique index on (conversation, idempotency_key)
        # catches it, and `run_advisor_turn` replays the stored answer.
        idempotency_key=f"{TELEGRAM_SAFE_IDEMPOTENCY_PREFIX}{int(update_id)}",
        channel_profile=TELEGRAM_SAFE_PROFILE,
    )

    # Re-read before delivering, not only before generating. A turn takes up to
    # ~90 seconds and the whole point of `/unlink` on a stolen handset is that it
    # takes effect NOW — a revocation that lands mid-generation must not still be
    # followed by the student's GPA arriving in the thief's chat. The answer is
    # already persisted, so dropping the delivery loses the student nothing.
    live = linking.active_link_by_id(link_id)
    if live is None:
        logger.info("telegram: link revoked during generation; dropping delivery")
        return

    chat_id = int(live.telegram_user_id)
    output_withheld = _finalize_telegram_output(
        result,
        student_id=principal.student_id,
        question=question,
    )

    # The picture FIRST, then the words. A student scrolling a phone should meet
    # the timetable and then read the caveats about it — and the caveats travel as
    # their own message, never as a caption, because Telegram caps a caption at
    # 1024 characters and silently truncating a disclaimer is the one failure this
    # channel must not have.
    #
    # Every failure here is silent by design: `_send_card_image` returns nothing
    # rather than raising, so a missing Chromium or a slow render costs the
    # picture and never the answer.
    try:
        if not output_withheld:
            _send_card_image(
                result,
                chat_id,
                server_port=server_port,
                student_id=principal.student_id,
                question=question,
            )
    except Exception:  # noqa: BLE001
        # The picture is a courtesy; the answer is the product. Without this guard
        # a raise here left the student with the acknowledgement and then silence
        # for ever — the answer generated, validated and stored, the webhook
        # already 200'd so Telegram never redelivers, and the exception swallowed
        # by the background runner. The docstring below used to assert this could
        # not happen; nothing enforced it.
        logger.warning("telegram: card image phase failed; sending text only")

    rendered = _render_outcome(result, question=question)
    if output_withheld:
        rendered = [messages.sensitive_record_web_only(_web_url(conversation.pk))]
    for text in rendered:
        # To the link's OWN chat, never to the id carried in the payload that
        # triggered this: the payload is attacker-controlled input and the link row
        # is the verified fact.
        send_text(chat_id=chat_id, text=text)


def _finalize_telegram_output(
    result: advisor_turn.TurnResult,
    *,
    student_id: int | None,
    question: str,
) -> bool:
    """Persist and return whether this turn must stay on the web surface.

    A Telegram answer begins as ``telegram_unvalidated``. Only this function may
    promote it to ``telegram_safe`` after the output boundary, or to
    ``telegram_withheld``. A replay still marked unvalidated means a process died
    after storing the model response but before this decision; it is withheld
    without comparing the old answer to today's mutable academic record.
    """

    assistant = result.assistant_message
    student_message = result.student_message
    if assistant is None:
        return False
    if student_message is None:
        return True

    profile = str(student_message.generation_profile or "")
    if profile == TELEGRAM_WITHHELD_PROFILE:
        return True
    if profile == TELEGRAM_SAFE_PROFILE:
        return False
    if profile != TELEGRAM_UNVALIDATED_PROFILE:
        # Unknown/legacy provenance is never evidence that the output crossed the
        # current boundary.
        return True

    if result.outcome == advisor_turn.REPLAYED:
        target = TELEGRAM_WITHHELD_PROFILE
    else:
        target = (
            TELEGRAM_WITHHELD_PROFILE
            if contains_personal_record_output(
                assistant.content,
                student_id=student_id,
                question=question,
            )
            else TELEGRAM_SAFE_PROFILE
        )

    changed = AdvisorMessage.objects.filter(
        pk=student_message.pk,
        generation_profile=TELEGRAM_UNVALIDATED_PROFILE,
    ).update(generation_profile=target)
    if changed:
        student_message.generation_profile = target
        return target == TELEGRAM_WITHHELD_PROFILE

    # A competing execution made the durable decision first. Respect it rather
    # than overwriting or re-evaluating it.
    student_message.refresh_from_db(fields=["generation_profile"])
    return student_message.generation_profile != TELEGRAM_SAFE_PROFILE


def _send_card_image(
    result: advisor_turn.TurnResult,
    chat_id: int,
    *,
    server_port: str = "",
    student_id: int | None = None,
    question: str = "",
) -> None:
    """Deliver a picture of a supported adviser card, or quietly deliver nothing."""
    from core.services.advisor_presentations import (
        KIND_GRADUATION,
        KIND_TIMETABLE,
        normalise_presentation,
    )

    from .rendering import (
        graduation_images_enabled,
        local_base_url,
        render_cards,
        timetable_images_enabled,
    )

    assistant = result.assistant_message
    if assistant is None or not assistant.presentation:
        return
    presentation = normalise_presentation(assistant.presentation)
    kind = presentation.get("kind") if presentation else ""
    if not (
        (kind == KIND_TIMETABLE and timetable_images_enabled())
        or (kind == KIND_GRADUATION and graduation_images_enabled())
    ):
        return
    if _presentation_contains_personal_record(
        presentation,
        student_id=student_id,
        question=question,
    ):
        return
    alternatives = presentation.get("alternatives") or []
    count = (
        min(len(alternatives), MAX_CARD_IMAGES) if kind == KIND_TIMETABLE and alternatives else 1
    )

    # One browser for all of them. A Chromium launch costs a second or two, and
    # four cold launches per answer against two executor slots is how a busy hour
    # becomes a queue.
    images = render_cards(
        message_id=assistant.pk,
        base_url=local_base_url(server_port),
        option_indexes=[
            i if kind == KIND_TIMETABLE and alternatives else None for i in range(count)
        ],
    )
    for png in images:
        if not png:
            # Already logged by the renderer. Stop rather than press on: if one
            # render failed the next will too, and the text below still carries
            # the link to the web screen, which is what shipped before images.
            return
        send_photo(chat_id=chat_id, png=png)


def _durable_delivery_items(
    result: advisor_turn.TurnResult,
    *,
    rendered: list[str],
    output_withheld: bool,
    student_id: int | None,
    question: str,
) -> list[dict[str, Any]]:
    """Describe photos and text without exporting or persisting any PNG bytes."""

    from core.services.advisor_presentations import (
        KIND_GRADUATION,
        KIND_TIMETABLE,
        normalise_presentation,
    )

    from . import jobs
    from .rendering import graduation_images_enabled, timetable_images_enabled

    # Telegram renders messages in send order. Lead with the complete validated
    # answer so optional card rendering can never make the student wait for (or
    # see images ahead of) the explanation and platform link.
    items: list[dict[str, Any]] = [
        {"kind": jobs.DELIVERY_KIND_TEXT, "text": str(text)} for text in rendered if str(text)
    ]
    assistant = result.assistant_message
    if not output_withheld and assistant is not None:
        presentation = normalise_presentation(assistant.presentation)
        kind = presentation.get("kind") if presentation else ""
        kind_enabled = (kind == KIND_TIMETABLE and timetable_images_enabled()) or (
            kind == KIND_GRADUATION and graduation_images_enabled()
        )
        if kind_enabled and not _presentation_contains_personal_record(
            presentation,
            student_id=student_id,
            question=question,
        ):
            alternatives = presentation.get("alternatives") or []
            option_indexes: list[int | None] = (
                list(range(min(len(alternatives), MAX_CARD_IMAGES)))
                if kind == KIND_TIMETABLE and alternatives
                else [None]
            )
            items.extend(
                {
                    "kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO,
                    "option_index": option_index,
                }
                for option_index in option_indexes
            )
    return items


def _presentation_contains_personal_record(
    presentation: dict[str, Any],
    *,
    student_id: int | None,
    question: str,
) -> bool:
    """Apply Telegram output DLP to every natural-language field in a card.

    Structured planning facts such as course codes, days, times, credits,
    section labels, prerequisite edges and coarse progress states are allowed by
    the channel policy. Natural-language names, failure/unplaced reasons and
    unresolved-requirement labels can originate in older stored rows or tool
    output, so they cross the same personal-record boundary as assistant prose
    before a screenshot recipe is created.
    """

    visible: list[str] = []
    contextual_rows: list[str] = []

    def add_course(row: Any, *, include_reason: bool = False) -> None:
        if not isinstance(row, dict):
            return
        fields = [
            str(value)
            for value in (
                row.get("course_code") or row.get("code"),
                row.get("course_name") or row.get("name"),
            )
            if str(value or "").strip()
        ]
        if include_reason and str(row.get("reason") or "").strip():
            fields.append(str(row["reason"]))
        visible.extend(fields)
        if fields:
            # Keep semantically related values together as well. A row may carry
            # ``course_code=DS341`` and ``reason=failed with mark 40``; neither
            # field proves whose result it is alone, but together they disclose
            # this student's exact record. The non-whitespace separator prevents
            # a trailing digit in a course code from being consumed as the
            # reverse-order value in a later ``GPA: 2.86`` field.
            contextual_rows.append(" | ".join(fields))

    for row in presentation.get("baseline_sections") or []:
        add_course(row)
    for row in presentation.get("constraint_failures") or []:
        add_course(row, include_reason=True)
    for option in presentation.get("alternatives") or []:
        if not isinstance(option, dict):
            continue
        for row in option.get("courses") or []:
            add_course(row)
        for row in option.get("meetings") or []:
            add_course(row)
        for row in option.get("unplaced_courses") or []:
            add_course(row, include_reason=True)

    # Graduation maps include names outside the timetable row shapes above.
    # Treat every natural-language course field as outbound media, while course
    # status enums/codes/edges remain the structured planning facts they are.
    # Older stored presentations are screened here after normalisation too.
    graph = presentation.get("graph")
    if isinstance(graph, dict):
        names = graph.get("nameOf")
        if isinstance(names, dict):
            for code, name in names.items():
                add_course({"course_code": code, "course_name": name})
    for key in ("removed_current_courses", "added_current_courses"):
        for row in presentation.get(key) or []:
            add_course(row)
    for row in presentation.get("unresolved_requirements") or []:
        add_course(row)
        if isinstance(row, dict):
            missing = row.get("missing_prerequisites")
            if isinstance(missing, list):
                visible.extend(str(value) for value in missing if str(value or "").strip())
    for value in (presentation.get("program"), presentation.get("planning_term")):
        if str(value or "").strip():
            visible.append(str(value))
    labels = presentation.get("band_labels")
    if isinstance(labels, dict):
        visible.extend(str(value) for value in labels.values() if str(value or "").strip())

    # Screen fields independently. Joining with whitespace lets a reverse-order
    # GPA pattern bridge unrelated values (for example the trailing ``1`` in a
    # course code followed by ``GPA: 2.86``), consume the GPA label, and hide the
    # actual match from the forward-order pattern.
    return any(
        contains_personal_record_output(
            field,
            student_id=student_id,
            question=question,
        )
        for field in [*visible, *contextual_rows]
    )


def _render_outcome(
    result: advisor_turn.TurnResult,
    *,
    question: str = "",
) -> list[str]:
    """One turn outcome, as the messages a student should receive.

    Only `AdvisorMessage.content` — the stored, validated answer — is ever sent.
    The adviser's result dict also carries the agent trace and, on the V1 branch
    the feature flag still defaults to, the student's own unprojected record; none
    of it is read here, and reading it would put a database row into a chat
    message.
    """
    if result.outcome == advisor_turn.RATE_LIMITED:
        return [messages.rate_limited(result.retry_after)]
    if result.outcome == advisor_turn.QUESTION_TOO_LONG:
        return [messages.QUESTION_TOO_LONG]
    if result.outcome == advisor_turn.QUESTION_EMPTY:
        return [messages.UNSUPPORTED_CONTENT]
    if result.outcome == advisor_turn.NO_STUDENT_RECORD:
        return [messages.NO_STUDENT_RECORD]
    if result.outcome in {advisor_turn.GENERATION_FAILED, advisor_turn.KEY_CONFLICT}:
        return [messages.GENERATION_FAILED]

    assistant = result.assistant_message
    if assistant is None:
        # REPLAYED with nothing paired: the earlier attempt stored the question and
        # never got an answer. Telling the student it failed is true and lets them
        # ask again.
        return [messages.GENERATION_FAILED]

    from core.services.virtual_advisor import _answer_language

    source_question = question or (
        result.student_message.content if result.student_message is not None else ""
    )
    return render_answer(
        answer=assistant.content,
        citations=list(assistant.citations.all()),
        web_url=_web_url(assistant.conversation_id),
        # The web chat draws a structured timetable card and the adviser's prompt
        # keeps its prose short because of it. Rather than rebuild that card out of
        # chat messages, point at the screen that already draws it.
        has_presentation=bool(assistant.presentation),
        language=_answer_language(source_question),
    )


__all__ = [
    "SUPPORTED_UPDATE_KEYS",
    "UNAUTHENTICATED_COMMANDS",
    "InboundMessage",
    "answer_question",
    "claim_update",
    "execute_durable_job",
    "contains_personal_record_output",
    "handle_command",
    "is_enabled",
    "parse_update",
    "requires_secure_record_surface",
]
