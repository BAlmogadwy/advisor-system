"""What a timetable-feedback case can become, who may move it, and the student's side.

THE STATE MACHINE

A case is SUBMITTED, then IN_REVIEW, may wait on the student (NEEDS_INFORMATION)
or go to the department (FORWARDED), is ANSWERED, and is CLOSED. The student may
WITHDRAW it from any open status. Who may make which move is three tables, one per
actor: STAFF (advisors and the department), STUDENT, and SYSTEM (scheduled
commands). A move that is not in its actor's table is refused, whoever asks.

THE CLAIM

Every write names the case, the version AND the status it read, through a
queryset already scoped to the caller (a student's own cases; later a staff
member's visible ones). Ownership, the move and the version are one UPDATE, so
two people acting on one case cannot both succeed, and a caller can never touch a
case outside its scope: the row is simply not there for it.

THE STUDENT'S SIDE

A reply when the advisor asked for information; a follow-up while the case is
being handled; withdrawing one point or the whole case; and, once answered,
confirming the answer or saying it did not help (within a window) to reopen it.
A new point reopens an answered case within the same window. After it the
answer stands: the case is closed, and the new point opens a new case.
The student is always identified by the session and the case by its reference,
together, in the query: another student's reference is simply not found.

Each action's own check of the case (the status it needs, a follow-up left, a
point other than the last, an answer still inside its window) is one function,
called by the action under the case's row lock and by `student_action_refusal`
without it. A page asks that before it offers the action, so it offers only what
the action will take.

WHAT THE STUDENT HAS NOT SEEN

A case is unread while ``last_student_visible_at`` is later than
``student_seen_at``. Every staff or system write the student can see (a message
to them, a request for information, an answer, a closing) sets
``last_student_visible_at`` to its ``now`` inside its own claim; the student's own
actions never do. Opening the case moves ``student_seen_at`` up to the value the
page read, never to the clock, and never back.

Staff moves and the system's closing commands use the same tables and the same
claim; they land with the advisor queue and the lifecycle commands.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from core.models import (
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackMessage,
    TimetableFeedbackStatus,
)
from core.services import rate_limit

S = TimetableFeedbackStatus
STAFF = "STAFF"
STUDENT = "STUDENT"
SYSTEM = "SYSTEM"

#: Moves an advisor or the department may make.
STAFF_TRANSITIONS: Mapping[str, frozenset[str]] = {
    S.SUBMITTED: frozenset({S.IN_REVIEW, S.NEEDS_INFORMATION, S.FORWARDED, S.ANSWERED, S.CLOSED}),
    S.IN_REVIEW: frozenset({S.NEEDS_INFORMATION, S.FORWARDED, S.ANSWERED, S.CLOSED}),
    S.NEEDS_INFORMATION: frozenset({S.IN_REVIEW, S.ANSWERED, S.CLOSED}),
    S.FORWARDED: frozenset({S.IN_REVIEW, S.ANSWERED, S.CLOSED}),
    S.ANSWERED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
    S.WITHDRAWN: frozenset(),
}
#: Moves the student may make: answer a request for information, reopen or
#: accept an answer, and withdraw at any time before the case is finished.
STUDENT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    S.SUBMITTED: frozenset({S.WITHDRAWN}),
    S.IN_REVIEW: frozenset({S.WITHDRAWN}),
    S.FORWARDED: frozenset({S.WITHDRAWN}),
    S.NEEDS_INFORMATION: frozenset({S.IN_REVIEW, S.SUBMITTED, S.WITHDRAWN}),
    S.ANSWERED: frozenset({S.IN_REVIEW, S.SUBMITTED, S.CLOSED, S.WITHDRAWN}),
    S.CLOSED: frozenset(),
    S.WITHDRAWN: frozenset(),
}
#: Moves a scheduled command may make: closing a case whose term has ended, or
#: an answered case nobody replied to.
SYSTEM_TRANSITIONS: Mapping[str, frozenset[str]] = {
    S.SUBMITTED: frozenset({S.CLOSED}),
    S.IN_REVIEW: frozenset({S.CLOSED}),
    S.NEEDS_INFORMATION: frozenset({S.CLOSED}),
    S.FORWARDED: frozenset({S.CLOSED}),
    S.ANSWERED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
    S.WITHDRAWN: frozenset(),
}
TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = {
    STAFF: STAFF_TRANSITIONS,
    STUDENT: STUDENT_TRANSITIONS,
    SYSTEM: SYSTEM_TRANSITIONS,
}

#: What a student writes to their advisor: the same bound as a point's note.
STUDENT_TEXT_MAX_CHARS = 1000
#: Follow-ups a student may add to one case, apart from replies when asked.
MAX_FOLLOW_UPS = 5
#: The only control characters student text may hold: tab, line feed, return.
_TEXT_WHITESPACE = frozenset({chr(9), chr(10), chr(13)})
#: The statuses a follow-up may be added in. Waiting on the student, the case
#: wants a REPLY; answered, it wants a confirmation or a reopening.
FOLLOW_UP_STATUSES = frozenset({S.SUBMITTED, S.IN_REVIEW, S.FORWARDED})
#: The statuses a single point may be withdrawn in: before an answer. An answer
#: covers the case as it stood, so afterwards it is accepted, reopened or
#: withdrawn whole.
POINT_WITHDRAWAL_STATUSES = frozenset({S.SUBMITTED, S.IN_REVIEW, S.NEEDS_INFORMATION, S.FORWARDED})
#: Characters that render as nothing although Unicode files them as letters or
#: symbols: the Hangul fillers and the blank Braille pattern.
_BLANK_LOOKING = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800})
#: Categories that show nothing on their own: format marks, and combining marks
#: with no letter to sit on.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me"})


class WorkflowRefused(Exception):
    """A move or a write that was not made, and why. ``status`` is the HTTP answer."""

    def __init__(self, code: str, *, status: int = 409, retry_after: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def may_move(actor: str, from_status: str, to_status: str) -> bool:
    """Whether ``actor`` may move a case from one status to another."""
    return to_status in TRANSITIONS[actor].get(from_status, frozenset())


def reopen_target(case: TimetableFeedback) -> str:
    """Where a case goes back to when the student reopens it: in front of whoever
    handles it, or back in the queue if nobody does."""
    return S.IN_REVIEW if case.handled_by_id else S.SUBMITTED


def cas_transition(
    scoped: QuerySet[TimetableFeedback],
    case: TimetableFeedback,
    *,
    actor: str,
    now: datetime,
    to_status: str | None = None,
    **changes: Any,
) -> None:
    """Claim ``case`` as it was read, and move it if ``to_status`` is given.

    ``scoped`` is the caller's own view of cases; a case outside it matches no row.
    The UPDATE names the id, the version and the status read, so a case that
    moved, or was touched, since it was read is refused rather than overwritten.

    The scope goes in as a subquery on the id, and the claim stays on the case's
    own columns. A scope that joins another table would otherwise make Django
    rewrite the whole UPDATE as ``id IN (subquery)``. PostgreSQL re-checks a row
    that changed while the statement waited on its lock only against that outer
    condition, so the version inside the subquery would be read as it was before
    the wait, and two claims could both win.
    """
    if "status" in changes:
        raise TypeError("cas_transition takes a new status only as to_status")
    if to_status == S.CLOSED and not changes.get("close_reason"):
        raise ValueError("a move to CLOSED needs a close_reason")
    if to_status != S.CLOSED and changes.get("close_reason"):
        raise ValueError("only a move to CLOSED takes a close_reason")
    if to_status is not None:
        if not may_move(actor, case.status, to_status):
            raise WorkflowRefused("NOT_ALLOWED")
        changes["status"] = to_status
    claimed = TimetableFeedback.objects.filter(
        pk=case.pk,
        version=case.version,
        status=case.status,
        pk__in=scoped.values("pk"),
    ).update(version=F("version") + 1, updated_at=now, **changes)
    if claimed != 1:
        raise WorkflowRefused("CASE_CHANGED")


def answer_window_passed(case: TimetableFeedback, now: datetime) -> bool:
    """Whether the student's time to come back to an answer has run out.

    Inside the window the answered case is still the student's to reopen, by
    saying the answer did not help or by raising a new point. After it the answer
    stands: a new point closes the case and opens a new one, and the scheduled
    lifecycle command will close the rest. Staff must set ``answered_at`` with
    every answer. A case marked answered without it (set by hand, say) is timed
    from its last change: neither refused outright nor open for ever."""
    return now > reopen_until(case)


def reopen_until(case: TimetableFeedback) -> datetime:
    """The last moment an answer can still be reopened: the end of its window,
    which the pages show as a date."""
    answered_at = case.answered_at or case.updated_at
    return answered_at + timedelta(days=settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS)


def close_settled_answer(case: TimetableFeedback, *, now: datetime) -> None:
    """Close an answered case whose window has passed, as the system would.

    A new point calls this the moment it arrives; the scheduled lifecycle command
    will close the rest. Doing it as the point comes in keeps the rule from
    depending on when that command runs. (Accepting the answer closes as
    ANSWER_ACCEPTED, and withdrawing withdraws, whatever the answer's age.)"""
    cas_transition(
        TimetableFeedback.objects.filter(student_id=case.student_id),
        case,
        actor=SYSTEM,
        now=now,
        to_status=S.CLOSED,
        close_reason=TimetableFeedback.CloseReason.NO_REPLY_AFTER_ANSWER,
        closed_at=now,
    )
    _event(
        case,
        TimetableFeedbackEvent.Kind.AUTO_CLOSED,
        from_value=S.ANSWERED,
        to_value=S.CLOSED,
    )


def body_script(text: str) -> str:
    """'ar', 'en' or 'mixed' by the letters used, so staff can be warned before
    answering in the other language; '' when there are no letters at all.

    Only letters count: digits, punctuation, emoji and their variation selectors,
    and invisible marks say nothing about the language. A letter is Arabic by its
    Unicode name, which also covers the supplement and presentation-form blocks."""
    arabic = latin = False
    for ch in text:
        if not ch.isalpha():
            continue
        if ch.isascii():
            latin = True
        elif unicodedata.name(ch, "").startswith("ARABIC"):
            arabic = True
    if arabic and latin:
        return "mixed"
    return "ar" if arabic else "en" if latin else ""


def visible(text: str) -> bool:
    """Whether ``text`` shows anything: more than spaces, invisible format marks
    (zero-width spaces, direction marks, a byte-order mark), combining marks on
    their own, and the few letters and symbols that render blank."""
    return any(
        not ch.isspace()
        and unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
        and ord(ch) not in _BLANK_LOOKING
        for ch in text
    )


def storable(text: str) -> bool:
    """Whether ``text`` can be stored as it is.

    Of the control characters (C0, DEL and C1) only tabs and line breaks are
    allowed; PostgreSQL refuses a NUL outright, and the rest would be stored as
    nothing anyone can read. A lone surrogate is refused as well, because neither
    database driver can encode it: that would otherwise be a server error at the
    insert."""
    if any(unicodedata.category(ch) == "Cc" and ch not in _TEXT_WHITESPACE for ch in text):
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def clean_student_text(text: object) -> str:
    """The student's words, trimmed, or a refusal: something visible, bounded, and
    storable."""
    cleaned = str(text or "").strip()
    if not visible(cleaned):
        raise WorkflowRefused("BODY_REQUIRED", status=400)
    if len(cleaned) > STUDENT_TEXT_MAX_CHARS:
        raise WorkflowRefused("BODY_TOO_LONG", status=400)
    if not storable(cleaned):
        raise WorkflowRefused("BODY_INVALID", status=400)
    return cleaned


# ---------------------------------------------------------------------------
# The student's side
# ---------------------------------------------------------------------------


def _student_scope(student_id: int) -> QuerySet[TimetableFeedback]:
    return TimetableFeedback.objects.filter(student_id=student_id)


def _spend(student_id: int, now: datetime) -> None:
    decision = rate_limit.consume(rate_limit.TIMETABLE_FEEDBACK_REPLY, student_id, now=now)
    if not decision.allowed:
        raise WorkflowRefused("RATE_LIMITED", status=429, retry_after=decision.retry_after)


def case_reference(value: object) -> str:
    """A case reference as typed, normalised (TTF-2026-00012), or '' when the text
    cannot be one. Anything else, a lone surrogate included, never reaches a
    query, where it could only fail to encode."""
    reference = str(value or "").strip().upper()
    prefix, _, rest = reference.partition("-")
    year, _, number = rest.partition("-")
    digits = year + number
    if prefix == "TTF" and len(year) == 4 and number and digits.isascii() and digits.isdigit():
        return reference
    return ""


def _own_case(student_id: int, reference: object) -> TimetableFeedback:
    """The student's case by reference, under its row lock, or CASE_NOT_FOUND."""
    wanted = case_reference(reference)
    case = (
        _student_scope(student_id).select_for_update().filter(reference=wanted).first()
        if wanted
        else None
    )
    if case is None:
        raise WorkflowRefused("CASE_NOT_FOUND", status=404)
    return case


def _student_message(case: TimetableFeedback, body: str) -> TimetableFeedbackMessage:
    return TimetableFeedbackMessage.objects.create(
        case=case,
        audience=TimetableFeedbackMessage.Audience.STUDENT_VISIBLE,
        author_kind=TimetableFeedbackMessage.AuthorKind.STUDENT,
        body=body,
        body_script=body_script(body),
    )


def _event(case: TimetableFeedback, kind: str, **fields: Any) -> TimetableFeedbackEvent:
    return TimetableFeedbackEvent.objects.create(case=case, kind=kind, **fields)


# What each action needs of the case, in the order the action checks it. Each
# raises the action's own refusal; ``point_id`` matters to withdraw_point only.


def _check_reply(case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None) -> None:
    if case.status != S.NEEDS_INFORMATION:
        raise WorkflowRefused("NOT_ALLOWED")


def follow_ups_used(case: TimetableFeedback) -> int:
    """Follow-ups the student has added to ``case`` over its whole life."""
    return case.events.filter(kind=TimetableFeedbackEvent.Kind.STUDENT_FOLLOWUP).count()


def _point_uuid(value: object) -> uuid.UUID:
    """A point id as sent, or POINT_NOT_FOUND for text that cannot be one."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise WorkflowRefused("POINT_NOT_FOUND", status=404) from None


def _check_follow_up(case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None) -> None:
    if case.status not in FOLLOW_UP_STATUSES:
        raise WorkflowRefused("NOT_ALLOWED")
    if follow_ups_used(case) >= MAX_FOLLOW_UPS:
        raise WorkflowRefused("TOO_MANY_FOLLOW_UPS")


def _check_withdraw_point(
    case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None
) -> None:
    """With no ``point_id``: whether some point of the case could be withdrawn."""
    if case.status not in POINT_WITHDRAWAL_STATUSES:
        raise WorkflowRefused("NOT_ALLOWED")
    live = case.points.filter(withdrawn_at__isnull=True)
    if point_id is not None and not live.filter(pk=point_id).exists():
        raise WorkflowRefused("POINT_NOT_FOUND", status=404)
    if live.count() <= 1:
        raise WorkflowRefused("LAST_POINT")


def _check_withdraw_case(
    case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None
) -> None:
    if not may_move(STUDENT, case.status, S.WITHDRAWN):
        raise WorkflowRefused("NOT_ALLOWED")


def _check_confirm_answer(
    case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None
) -> None:
    if case.status != S.ANSWERED:
        raise WorkflowRefused("NOT_ALLOWED")


def _check_reopen_answer(
    case: TimetableFeedback, now: datetime, point_id: uuid.UUID | None
) -> None:
    if case.status != S.ANSWERED:
        raise WorkflowRefused("NOT_ALLOWED")
    if answer_window_passed(case, now):
        raise WorkflowRefused("REOPEN_WINDOW_PASSED")


_STUDENT_CHECKS: Mapping[str, Callable[[TimetableFeedback, datetime, uuid.UUID | None], None]] = {
    "reply": _check_reply,
    "follow_up": _check_follow_up,
    "withdraw_point": _check_withdraw_point,
    "withdraw_case": _check_withdraw_case,
    "confirm_answer": _check_confirm_answer,
    "reopen_answer": _check_reopen_answer,
}


def student_action_refusal(
    case: TimetableFeedback,
    action: str,
    *,
    now: datetime | None = None,
    point_id: object = None,
) -> WorkflowRefused | None:
    """Why the student's ``action`` would be refused on ``case`` now, or None.

    Read-only, and the very check the action makes under its lock, so a page
    offers an action only where it would be taken. What no page can foresee is
    left out: the rate limit, the text the student has yet to write, and a case
    that changes between this read and the action. For withdraw_point without a
    ``point_id`` it answers for the case: whether a point other than the last is
    live.
    """
    try:
        wanted = None if point_id is None else _point_uuid(point_id)
        _STUDENT_CHECKS[action](case, now or timezone.now(), wanted)
    except WorkflowRefused as refused:
        return refused
    return None


def waits_on_student(case: TimetableFeedback, now: datetime) -> bool:
    """Whether the case can move on only when the student acts: a request for
    information to answer, or an answer to accept or reopen while it still can be."""
    if case.status == S.NEEDS_INFORMATION:
        return True
    return case.status == S.ANSWERED and not answer_window_passed(case, now)


def unread_q() -> Q:
    """Cases where something the student may read appeared after they last looked."""
    return Q(last_student_visible_at__isnull=False) & (
        Q(student_seen_at__isnull=True) | Q(last_student_visible_at__gt=F("student_seen_at"))
    )


def has_unread(case: TimetableFeedback) -> bool:
    """`unread_q` for a case already read."""
    visible = case.last_student_visible_at
    seen = case.student_seen_at
    return visible is not None and (seen is None or visible > seen)


def mark_seen(case: TimetableFeedback) -> None:
    """Record that the student saw ``case`` as it was read.

    The mark is the ``last_student_visible_at`` the page read, not the clock, so a
    write that lands while the page renders stays unread. It only moves forward,
    and it is a plain UPDATE of that one column: the version and ``updated_at``
    stay as they were, so no claim made since the read is refused for it and no
    window timed from ``updated_at`` moves."""
    seen = case.last_student_visible_at
    if seen is None:
        return
    TimetableFeedback.objects.filter(pk=case.pk, student_id=case.student_id).filter(
        Q(student_seen_at__isnull=True) | Q(student_seen_at__lt=seen)
    ).update(student_seen_at=seen)


def reply(
    *, student_id: int, reference: object, body: object, now: datetime | None = None
) -> TimetableFeedbackMessage:
    """Answer the advisor's request for information. The case goes back to whoever
    handles it, or back to the queue."""
    now = now or timezone.now()
    text = clean_student_text(body)
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_reply(case, now, None)
        target = reopen_target(case)
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            to_status=target,
            last_student_activity_at=now,
        )
        message = _student_message(case, text)
        _event(
            case,
            TimetableFeedbackEvent.Kind.STUDENT_REPLY,
            message_id=message.pk,
            from_value=S.NEEDS_INFORMATION,
            to_value=target,
        )
    return message


def follow_up(
    *, student_id: int, reference: object, body: object, now: datetime | None = None
) -> TimetableFeedbackMessage:
    """Add something to a case while it is being handled, without being asked."""
    now = now or timezone.now()
    text = clean_student_text(body)
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_follow_up(case, now, None)
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            last_student_activity_at=now,
        )
        message = _student_message(case, text)
        _event(case, TimetableFeedbackEvent.Kind.STUDENT_FOLLOWUP, message_id=message.pk)
    return message


def withdraw_point(
    *, student_id: int, reference: object, point_id: object, now: datetime | None = None
) -> None:
    """Take one point back, before the case is answered. The last live point is
    never withdrawn alone: an empty case in an advisor's queue helps nobody, so the
    case is withdrawn instead."""
    now = now or timezone.now()
    wanted = _point_uuid(point_id)
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_withdraw_point(case, now, wanted)
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            last_student_activity_at=now,
        )
        case.points.filter(pk=wanted, withdrawn_at__isnull=True).update(withdrawn_at=now)
        _event(case, TimetableFeedbackEvent.Kind.POINT_WITHDRAWN, point_id=wanted)


def withdraw_case(*, student_id: int, reference: object, now: datetime | None = None) -> None:
    """Take the whole case back. A new one may be opened after the cool-down."""
    now = now or timezone.now()
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_withdraw_case(case, now, None)
        before = case.status
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            to_status=S.WITHDRAWN,
            closed_at=now,
            last_student_activity_at=now,
        )
        _event(case, TimetableFeedbackEvent.Kind.WITHDRAWN, from_value=before, to_value=S.WITHDRAWN)


def confirm_answer(
    *,
    student_id: int,
    reference: object,
    helpful: bool | None = None,
    now: datetime | None = None,
) -> None:
    """Accept the answer, which closes the case, saying whether it helped."""
    now = now or timezone.now()
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_confirm_answer(case, now, None)
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            to_status=S.CLOSED,
            close_reason=TimetableFeedback.CloseReason.ANSWER_ACCEPTED,
            closed_at=now,
            answer_helpful=helpful,
            last_student_activity_at=now,
        )
        _event(
            case,
            TimetableFeedbackEvent.Kind.ANSWER_CONFIRMED,
            from_value=S.ANSWERED,
            to_value=S.CLOSED,
        )


def reopen_answer(
    *, student_id: int, reference: object, body: object, now: datetime | None = None
) -> TimetableFeedbackMessage:
    """Say the answer did not help, and why, within the reopening window."""
    now = now or timezone.now()
    text = clean_student_text(body)
    _spend(student_id, now)
    with transaction.atomic():
        case = _own_case(student_id, reference)
        _check_reopen_answer(case, now, None)
        target = reopen_target(case)
        cas_transition(
            _student_scope(student_id),
            case,
            actor=STUDENT,
            now=now,
            to_status=target,
            answer_helpful=False,
            last_student_activity_at=now,
        )
        message = _student_message(case, text)
        _event(
            case,
            TimetableFeedbackEvent.Kind.REOPENED,
            message_id=message.pk,
            from_value=S.ANSWERED,
            to_value=target,
        )
    return message
