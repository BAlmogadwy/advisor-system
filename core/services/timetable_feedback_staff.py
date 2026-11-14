"""The staff side of timetable feedback: who sees a case, and what staff do with it.

WHO SEES A CASE (owner, 2026-11-14). The grounds a student's record already has
(`policy.require_student_scope`), read LIVE from the student's row, plus the
department's assignment:

- an ADVISOR sees the cases of students whose advisor id is theirs, and the cases
  the department assigned to that id. Every account holding the id sees the same
  cases: they are one person, and seeding made up to seven accounts each;
- a GENERAL_ACADEMIC_ADVISOR is the department: every case of a student whose
  programme is in their departments, by exact code (AI is not AI2), and nothing
  through an advisor id;
- SUPER_ADMIN sees every case; nobody else sees any.

A student whose advisor id matches no account (a raw name, or blank) has no
advisor who could see their case, so it is the department's. That catch-all needs
no rule of its own, only a way to find those cases (`unrouted_q`). A student moved
to another advisor takes their cases along, and the previous advisor loses them.
Reading and acting are one scope: a case a person can open is a case they can act
on, and one outside it is simply not found.

THE CLAIM. Every action locks the case through that scope, refuses a case that
changed since the page showed it (the version the page posted), checks its own
rule, and then moves the case with `workflow.cas_transition` (actor STAFF) through
the same scope. Two people acting on one case cannot both succeed; the second is
told who changed it. Each rule is one function, used by the action under the lock
and by `staff_action_refusal` without it, so a page offers only what the action
would take.

WHAT THE STUDENT READS. A message to the student is labelled by `author_kind`,
decided here: ADVISOR only when the writer holds the student's own advisor id at
that moment, the department otherwise. Staff names never leave the staff side.
Every write the student can see (a question, an answer, a closing) stores
`workflow.student_visible_at(now)` in the claim itself, so it shows as new and
the mark never moves back. Internal comments and forwarding are not news to the
student. Nothing here writes a timetable: no section, meeting or registration row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, QuerySet, TextField, Value
from django.db.models.functions import Replace, Trim, Upper
from django.utils import timezone

from core.models import (
    TIMETABLE_FEEDBACK_TERMINAL_STATUSES,
    Student,
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
    UserScope,
)
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_workflow as workflow
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    get_user_scope,
)

S = TimetableFeedback.Status
Event = TimetableFeedbackEvent.Kind
Message = TimetableFeedbackMessage
WorkflowRefused = workflow.WorkflowRefused

#: Who may use the staff side at all; `role_required(ROLE_ADVISOR)` admits them.
STAFF_ROLES = frozenset({ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN})
#: The department: may assign a case to an advisor and remove text from it.
DEPARTMENT_ROLES = frozenset({ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN})

#: The longest message staff may write, to the student or to each other.
STAFF_TEXT_MAX_CHARS = 2000
#: The most cases one bulk action may touch, which is one page of the queue.
BULK_MAX = 50
#: A member of staff opening the same case again within this time is not logged
#: again in the case's history (every opening still goes to the audit log).
VIEW_EVENT_EVERY = timedelta(hours=1)
#: What removed text is replaced with, so a reader sees that, and where, it was.
REDACTION_MARKER = "[…]"
#: The reasons staff may give for closing a case. The others belong to the
#: student (ANSWER_ACCEPTED) and to the scheduled commands.
STAFF_CLOSE_REASONS = (
    TimetableFeedback.CloseReason.DUPLICATE,
    TimetableFeedback.CloseReason.NOT_APPLICABLE,
    TimetableFeedback.CloseReason.OTHER,
)
#: Cases waiting on staff: nobody has answered them yet.
WAITING_ON_STAFF = (S.SUBMITTED, S.IN_REVIEW)
#: Statuses a person may take a case in: from the queue or from the department,
#: or over from a colleague while it is being handled or waits on the student.
TAKE_STATUSES = frozenset({S.SUBMITTED, S.FORWARDED, S.IN_REVIEW, S.NEEDS_INFORMATION})
#: Statuses the department may hand a case to an advisor in: before an answer.
ASSIGN_STATUSES = frozenset({S.SUBMITTED, S.IN_REVIEW, S.NEEDS_INFORMATION, S.FORWARDED})


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Staff:
    """A member of staff as the feedback pages see them."""

    user: Any
    role: str
    #: Their advisor id, '' when they hold none.
    advisor_id: str
    #: Their programmes, upper-cased, for the department's scope.
    departments: tuple[str, ...]
    #: How they are named on staff pages and in the trail: never to a student.
    label: str

    @property
    def is_department(self) -> bool:
        return self.role in DEPARTMENT_ROLES

    @property
    def username(self) -> str:
        return str(self.user.get_username())


def staff_of(user: Any, scope: Mapping[str, Any] | None = None) -> Staff | None:
    """The member of staff behind ``user``, or None for anyone else. ``scope`` is
    `get_user_scope(user)` when the caller has already read it."""
    if not getattr(user, "is_authenticated", False):
        return None
    scope = scope if scope is not None else get_user_scope(user)
    role = str(scope.get("role") or "")
    if role not in STAFF_ROLES:
        return None
    advisor_id = str(scope.get("advisor_id") or "").strip()
    departments = tuple(
        sorted({str(d).strip().upper() for d in scope.get("departments") or [] if str(d).strip()})
    )
    # The rule `advisor_inbox.adviser_label` uses: a name, else the advisor id,
    # else the username.
    name = (user.get_full_name() or "").strip()
    return Staff(
        user=user,
        role=role,
        advisor_id=advisor_id,
        departments=departments,
        label=name or advisor_id or str(user.get_username()),
    )


#: Whitespace an import can leave in a stored id or programme besides spaces: tab,
#: line feed, vertical tab, form feed, carriage return and the no-break space.
#: Python's ``strip()``, which the record check uses, removes them all; SQL's TRIM
#: removes spaces only.
_STRAY_WHITESPACE = (chr(9), chr(10), chr(11), chr(12), chr(13), chr(0xA0))


def _bare(field: str) -> Trim:
    """``field`` as `require_student_scope` reads it: without that whitespace,
    trimmed. The characters go wherever they are, not only at the ends, which
    changes nothing for a value that can match: an advisor id is a number and a
    programme a code."""
    value: Any = F(field)
    for character in _STRAY_WHITESPACE:
        value = Replace(value, Value(character), Value(""), output_field=TextField())
    return Trim(value)


def _students_advised_by(advisor_id: str) -> QuerySet[Student]:
    return Student.objects.annotate(live_advisor=_bare("advisor_id")).filter(
        live_advisor=advisor_id
    )


def students_in(programmes: Sequence[str]) -> QuerySet[Student]:
    return Student.objects.annotate(live_programme=Upper(_bare("program"))).filter(
        live_programme__in=list(programmes)
    )


def visible_to(staff: Staff | None) -> QuerySet[TimetableFeedback]:
    """The cases ``staff`` may open and act on, as a queryset, never a filter
    applied to rows already fetched."""
    cases = TimetableFeedback.objects.all()
    if staff is None:
        return cases.none()
    if staff.role == ROLE_SUPER_ADMIN:
        return cases
    if staff.role == ROLE_GENERAL_ADVISOR:
        if not staff.departments:
            return cases.none()
        return cases.filter(student_id__in=students_in(staff.departments).values("student_id"))
    if staff.role == ROLE_ADVISOR and staff.advisor_id:
        return cases.filter(
            Q(student_id__in=_students_advised_by(staff.advisor_id).values("student_id"))
            | Q(assigned_advisor_id=staff.advisor_id)
        )
    return cases.none()


def why_nothing_is_visible(staff: Staff | None) -> str:
    """Why ``staff`` can see no case at all, as a code, or '' when they can see
    some. The queue says it rather than showing an empty table."""
    if staff is None:
        return "NOT_STAFF"
    if staff.role == ROLE_ADVISOR and not staff.advisor_id:
        return "NO_ADVISOR_ID"
    if staff.role == ROLE_GENERAL_ADVISOR and not staff.departments:
        return "NO_DEPARTMENTS"
    return ""


def may_open_student_record(staff: Staff, student: Mapping[str, Any] | None) -> bool:
    """Whether ``staff`` passes `require_student_scope` for this student, so a link
    to the student's own pages would open. An advisor sees a case the department
    assigned them without being the student's advisor."""
    if student is None:
        return False
    if staff.role == ROLE_SUPER_ADMIN:
        return True
    if staff.role == ROLE_GENERAL_ADVISOR:
        return str(student.get("program") or "").strip().upper() in staff.departments
    return (
        bool(staff.advisor_id) and staff.advisor_id == str(student.get("advisor_id") or "").strip()
    )


#: Groups that make an account something other than an advisor, whatever advisor
#: id its scope holds (`rbac.get_user_role`): it then sees no case by that id.
_NOT_ADVISOR_GROUPS = (ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_EXAM_COMMITTEE, ROLE_STUDENT)


def _advisor_scopes() -> QuerySet[UserScope]:
    """The scopes of active accounts whose role is ADVISOR, with the advisor id
    they hold, trimmed, as ``held``: `get_user_role`'s fall-through to ADVISOR,
    written as a query. Only these see cases by an advisor id."""
    return (
        UserScope.objects.filter(user__is_active=True, user__is_superuser=False)
        .exclude(user__groups__name__in=_NOT_ADVISOR_GROUPS)
        .annotate(held=_bare("advisor_id"))
        .exclude(held="")
    )


def active_advisor_ids() -> QuerySet[UserScope]:
    """The advisor ids some active advisor account holds, as a subquery."""
    return _advisor_scopes().values("held")


def accounts_holding(advisor_id: str) -> int:
    """How many active advisor accounts hold ``advisor_id``: each sees its cases."""
    if not advisor_id:
        return 0
    return _advisor_scopes().filter(held=advisor_id).count()


def advisor_id_exists(advisor_id: object) -> bool:
    """Whether some active advisor account holds ``advisor_id``: the only ids a
    case can be assigned to, because only they would then see it."""
    wanted = str(advisor_id or "").strip()
    return bool(wanted) and _advisor_scopes().filter(held=wanted).exists()


# ---------------------------------------------------------------------------
# The queue's groupings
# ---------------------------------------------------------------------------


def unrouted_q() -> Q:
    """Cases no advisor account can see: the student's advisor id is held by no
    active account (a raw name, a blank, or no student row at all), and the case
    is not assigned to an id an active account holds. The department's."""
    routed = Student.objects.annotate(live_advisor=_bare("advisor_id")).filter(
        live_advisor__in=active_advisor_ids()
    )
    return ~Q(student_id__in=routed.values("student_id")) & ~Q(
        assigned_advisor_id__in=active_advisor_ids()
    )


def handler_gone_q() -> Q:
    """Cases whose handler can no longer act on them: the account is switched off,
    or it took the case as the advisor of a student who has since moved to another
    advisor, and the department did not assign the case to it. Such a case counts
    as untaken; otherwise it would wait on nobody. Only an advisor's handling is
    tied to an advisor id (`_handled_by`), so the department's never lapses so."""
    still_routes = Exists(
        Student.objects.annotate(live_advisor=_bare("advisor_id")).filter(
            student_id=OuterRef("student_id"), live_advisor=OuterRef("handled_advisor_id")
        )
    )
    return Q(handled_by__is_active=False) | (
        ~Q(handled_advisor_id="")
        & ~Q(still_routes)
        & ~Q(assigned_advisor_id=F("handled_advisor_id"))
    )


def untaken_q() -> Q:
    """Cases nobody is handling, or whose handler can no longer act on them."""
    return Q(handled_by__isnull=True) | handler_gone_q()


def no_forecast_only_q() -> Q:
    """Cases whose every live point says no expected timetable was prepared: the
    department's to handle (owner decision 6), so not counted for advisors."""
    return ~Q(
        Exists(
            TimetableFeedbackPoint.objects.filter(
                case=OuterRef("pk"), withdrawn_at__isnull=True
            ).exclude(anchor_kind=TimetableFeedbackPoint.AnchorKind.NO_FORECAST)
        )
    )


def student_wrote_since_staff_q() -> Q:
    """Cases where the student did something after staff last did."""
    return Q(last_student_activity_at__isnull=False) & (
        Q(last_staff_action_at__isnull=True)
        | Q(last_student_activity_at__gt=F("last_staff_action_at"))
    )


def needs_action_q(staff: Staff) -> Q:
    """The cases waiting on THIS person: the queue's first tab and the sidebar's
    count.

    An advisor: cases nobody has answered, unless they are about a missing
    timetable (the department's) or a colleague outside their advisor id is
    handling them. The department: cases forwarded to it; unanswered cases with no
    advisor to see them, or about a missing timetable, that nobody is handling;
    and unanswered cases they took themselves. A handler who can no longer act
    (`handler_gone_q`) handles nothing."""
    waiting = Q(status__in=WAITING_ON_STAFF)
    free = untaken_q()
    if staff.is_department:
        return (
            Q(status=S.FORWARDED)
            | (waiting & Q(handled_by=staff.user))
            | (waiting & free & (unrouted_q() | no_forecast_only_q()))
        )
    return waiting & ~no_forecast_only_q() & (free | Q(handled_advisor_id=staff.advisor_id))


#: The queue's tabs, in order. `needs_action_q` is the first; the rest group by
#: status, which is the same for everyone.
TABS = ("action", "open", "student", "department", "closed", "all")
_TAB_STATUSES: Mapping[str, Q] = {
    "open": ~Q(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES),
    "student": Q(status__in=(S.NEEDS_INFORMATION, S.ANSWERED)),
    "department": Q(status=S.FORWARDED),
    "closed": Q(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES),
    "all": Q(),
}


def tab_q(tab: str, staff: Staff) -> Q:
    """The cases of one tab; an unknown tab is the first."""
    if tab in _TAB_STATUSES:
        return _TAB_STATUSES[tab]
    return needs_action_q(staff)


def action_count(staff: Staff | None) -> int:
    """How many cases wait on ``staff``: one COUNT."""
    if staff is None:
        return 0
    return visible_to(staff).filter(needs_action_q(staff)).count()


# ---------------------------------------------------------------------------
# The rules, each one function
# ---------------------------------------------------------------------------


def _check_take(staff: Staff, case: TimetableFeedback) -> None:
    if case.status not in TAKE_STATUSES:
        raise WorkflowRefused("NOT_ALLOWED")
    if case.handled_by_id is not None and case.handled_by_id == staff.user.pk:
        raise WorkflowRefused("ALREADY_YOURS")


def _check_note(staff: Staff, case: TimetableFeedback) -> None:
    """A comment for colleagues may be added in any status, closed ones included."""


def _check_move(to_status: str) -> Callable[[Staff, TimetableFeedback], None]:
    def check(staff: Staff, case: TimetableFeedback) -> None:
        if not workflow.may_move(workflow.STAFF, case.status, to_status):
            raise WorkflowRefused("NOT_ALLOWED")

    return check


def _check_department(staff: Staff, case: TimetableFeedback) -> None:
    if not staff.is_department:
        raise WorkflowRefused("DEPARTMENT_ONLY", status=403)


def _check_assign(staff: Staff, case: TimetableFeedback) -> None:
    _check_department(staff, case)
    if case.status not in ASSIGN_STATUSES:
        raise WorkflowRefused("NOT_ALLOWED")


_CHECKS: Mapping[str, Callable[[Staff, TimetableFeedback], None]] = {
    "take": _check_take,
    "note": _check_note,
    "ask": _check_move(S.NEEDS_INFORMATION),
    "answer": _check_move(S.ANSWERED),
    "forward": _check_move(S.FORWARDED),
    "close": _check_move(S.CLOSED),
    "assign": _check_assign,
    "redact": _check_department,
}
#: Every action a case page may offer, by name.
STAFF_ACTIONS = tuple(_CHECKS)


def staff_action_refusal(
    staff: Staff, case: TimetableFeedback, action: str
) -> WorkflowRefused | None:
    """Why ``action`` would be refused on ``case`` now, or None. Read-only, and the
    very check the action makes under its lock; what no page can foresee (the text
    still to be written, a colleague acting first) is left to the action."""
    try:
        _CHECKS[action](staff, case)
    except WorkflowRefused as refused:
        return refused
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def clean_staff_text(text: object) -> str:
    """Staff's words, trimmed, or a refusal: visible, bounded, and storable."""
    cleaned = str(text or "").strip()
    if not workflow.visible(cleaned):
        raise WorkflowRefused("BODY_REQUIRED", status=400)
    if len(cleaned) > STAFF_TEXT_MAX_CHARS:
        raise WorkflowRefused("BODY_TOO_LONG", status=400)
    if not workflow.storable(cleaned):
        raise WorkflowRefused("BODY_INVALID", status=400)
    return cleaned


def _locked_case(staff: Staff, reference: object) -> TimetableFeedback:
    """The case by its reference, under its row lock, through ``staff``'s scope."""
    wanted = workflow.case_reference(reference)
    case = (
        visible_to(staff).select_for_update(of=("self",)).filter(reference=wanted).first()
        if wanted
        else None
    )
    if case is None:
        raise WorkflowRefused("CASE_NOT_FOUND", status=404)
    return case


def _expect_version(case: TimetableFeedback, version: object) -> None:
    """Refuse a case that changed since the page that sent this showed it."""
    try:
        seen = int(str(version).strip())
    except (TypeError, ValueError):
        raise WorkflowRefused("CASE_CHANGED") from None
    if seen != case.version:
        raise WorkflowRefused("CASE_CHANGED")


def author_kind_for(staff: Staff, case: TimetableFeedback) -> str:
    """How a student reads who wrote: their advisor only when the writer holds the
    student's own advisor id right now."""
    advisor_id = (
        Student.objects.filter(student_id=case.student_id)
        .values_list("advisor_id", flat=True)
        .first()
    )
    if staff.advisor_id and staff.advisor_id == str(advisor_id or "").strip():
        return Message.AuthorKind.ADVISOR
    return Message.AuthorKind.DEPARTMENT


def _touched(case: TimetableFeedback, now: datetime) -> dict[str, Any]:
    return {
        "last_staff_action_at": now,
        "first_staff_action_at": case.first_staff_action_at or now,
    }


def _handled_by(staff: Staff) -> dict[str, Any]:
    """``staff`` as the case's handler. The advisor id is kept only for an advisor,
    whose handling lapses when the student moves to another advisor
    (`handler_gone_q`); the department's handling follows its programmes."""
    return {
        "handled_by": staff.user,
        "handled_by_username": staff.username,
        "handled_advisor_id": staff.advisor_id if staff.role == ROLE_ADVISOR else "",
    }


def _claim(
    staff: Staff,
    case: TimetableFeedback,
    now: datetime,
    *,
    to_status: str | None = None,
    acted: bool = True,
    **changes: Any,
) -> None:
    """Claim the case for a staff write. ``acted`` is False for housekeeping that
    is no answer to anyone (a comment for colleagues, removing words): it still
    claims the case, but leaves the times staff last and first acted, which say
    whether the student has had a response."""
    workflow.cas_transition(
        visible_to(staff),
        case,
        actor=workflow.STAFF,
        now=now,
        to_status=to_status,
        **(_touched(case, now) if acted else {}),
        **changes,
    )


def _leave_a_colleagues_case(staff: Staff, case: TimetableFeedback) -> None:
    """For many cases at once: a case someone else is handling is left to them. On
    its own page the handler is shown, and anyone in scope may still act."""
    if case.handled_by_id is not None and case.handled_by_id != staff.user.pk:
        raise WorkflowRefused("HANDLED_BY_OTHER")


def _message(
    case: TimetableFeedback, staff: Staff, *, audience: str, body: str
) -> TimetableFeedbackMessage:
    return TimetableFeedbackMessage.objects.create(
        case=case,
        audience=audience,
        author_kind=author_kind_for(staff, case),
        author=staff.user,
        author_username=staff.username,
        author_label=staff.label,
        body=body,
        body_script=workflow.body_script(body),
    )


def _event(case: TimetableFeedback, staff: Staff, kind: str, **fields: Any) -> None:
    TimetableFeedbackEvent.objects.create(
        case=case,
        kind=kind,
        actor=staff.user,
        actor_username=staff.username,
        actor_label=staff.label,
        actor_advisor_id=staff.advisor_id,
        **fields,
    )


def take(
    staff: Staff, reference: object, *, version: object, now: datetime | None = None
) -> TimetableFeedback:
    """Become the person handling the case. A new or forwarded case moves to IN
    REVIEW; one a colleague is handling, or that waits on the student, keeps its
    status and changes hands."""
    now = now or timezone.now()
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _check_take(staff, case)
        before = case.status
        target = S.IN_REVIEW if before in (S.SUBMITTED, S.FORWARDED) else None
        _claim(staff, case, now, to_status=target, **_handled_by(staff))
        _event(case, staff, Event.TAKEN, from_value=before, to_value=target or before)
    return case


def add_note(
    staff: Staff,
    reference: object,
    *,
    version: object,
    body: object,
    now: datetime | None = None,
) -> TimetableFeedbackMessage:
    """A comment for colleagues only. The student never sees it."""
    now = now or timezone.now()
    text = clean_staff_text(body)
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _check_note(staff, case)
        _claim(staff, case, now, acted=False)
        message = _message(case, staff, audience=Message.Audience.INTERNAL, body=text)
        _event(case, staff, Event.NOTE_ADDED, message_id=message.pk)
    return message


def ask(
    staff: Staff,
    reference: object,
    *,
    version: object,
    body: object,
    now: datetime | None = None,
) -> TimetableFeedbackMessage:
    """Ask the student for more information. The asker handles the case, so the
    student's reply comes back to them."""
    now = now or timezone.now()
    text = clean_staff_text(body)
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _CHECKS["ask"](staff, case)
        before = case.status
        _claim(
            staff,
            case,
            now,
            to_status=S.NEEDS_INFORMATION,
            last_student_visible_at=workflow.student_visible_at(now),
            **_handled_by(staff),
        )
        message = _message(case, staff, audience=Message.Audience.STUDENT_VISIBLE, body=text)
        _event(
            case,
            staff,
            Event.STAFF_MESSAGE,
            message_id=message.pk,
            from_value=before,
            to_value=S.NEEDS_INFORMATION,
        )
    return message


def answer(
    staff: Staff,
    reference: object,
    *,
    version: object,
    body: object = "",
    template: str = "",
    now: datetime | None = None,
    leave_colleagues: bool = False,
) -> TimetableFeedbackMessage:
    """Answer the student. ``template`` names a ready-made answer, used in the
    language the student wrote in when no text is given. The student may then
    accept the answer or reopen the case within its window, which starts now.
    ``leave_colleagues`` (many cases at once) refuses a case someone else handles."""
    now = now or timezone.now()
    if template and template not in copy.REPLY_TEMPLATES:
        raise WorkflowRefused("TEMPLATE_UNKNOWN", status=400)
    typed = str(body or "").strip()
    text = clean_staff_text(typed) if typed or not template else ""
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _CHECKS["answer"](staff, case)
        if leave_colleagues:
            _leave_a_colleagues_case(staff, case)
        if not text:
            text = copy.reply_template(template, case.submitted_language or "en")
        before = case.status
        _claim(
            staff,
            case,
            now,
            to_status=S.ANSWERED,
            answered_at=now,
            # A new answer has not been judged yet, whatever the last one was.
            answer_helpful=None,
            last_student_visible_at=workflow.student_visible_at(now),
            **_handled_by(staff),
        )
        message = _message(case, staff, audience=Message.Audience.STUDENT_VISIBLE, body=text)
        _event(
            case,
            staff,
            Event.STAFF_MESSAGE,
            message_id=message.pk,
            from_value=before,
            to_value=S.ANSWERED,
        )
    return message


def forward(
    staff: Staff,
    reference: object,
    *,
    version: object,
    body: object,
    now: datetime | None = None,
) -> TimetableFeedbackMessage:
    """Pass the case to the department, saying why in a comment for colleagues.
    Whoever handled it stays named; nobody who forwards a case loses sight of it."""
    now = now or timezone.now()
    text = clean_staff_text(body)
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _CHECKS["forward"](staff, case)
        before = case.status
        handler = {} if case.handled_by_id is not None else _handled_by(staff)
        _claim(staff, case, now, to_status=S.FORWARDED, forwarded_at=now, **handler)
        message = _message(case, staff, audience=Message.Audience.INTERNAL, body=text)
        _event(
            case,
            staff,
            Event.FORWARDED,
            message_id=message.pk,
            from_value=before,
            to_value=S.FORWARDED,
        )
    return message


def close(
    staff: Staff,
    reference: object,
    *,
    version: object,
    reason: object,
    body: object,
    now: datetime | None = None,
    leave_colleagues: bool = False,
) -> TimetableFeedbackMessage:
    """Close the case for one of the staff's reasons, saying why in a comment for
    colleagues. The student sees the case closed, and why, as something new.
    ``leave_colleagues`` (many cases at once) refuses a case someone else handles."""
    now = now or timezone.now()
    code = str(reason or "").strip().upper()
    if code not in STAFF_CLOSE_REASONS:
        raise WorkflowRefused("CLOSE_REASON_REQUIRED", status=400)
    text = clean_staff_text(body)
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _CHECKS["close"](staff, case)
        if leave_colleagues:
            _leave_a_colleagues_case(staff, case)
        before = case.status
        _claim(
            staff,
            case,
            now,
            to_status=S.CLOSED,
            close_reason=code,
            closed_at=now,
            last_student_visible_at=workflow.student_visible_at(now),
        )
        message = _message(case, staff, audience=Message.Audience.INTERNAL, body=text)
        _event(
            case,
            staff,
            Event.STATUS_CHANGED,
            message_id=message.pk,
            from_value=before,
            to_value=S.CLOSED,
        )
    return message


def assign(
    staff: Staff,
    reference: object,
    *,
    version: object,
    advisor_id: object,
    now: datetime | None = None,
) -> TimetableFeedback:
    """The department hands the case to the advisor who holds ``advisor_id``.

    That advisor then sees it whoever the student's own advisor is, until the
    department assigns it elsewhere. Nobody handles it until they take it, and a
    forwarded case comes back from the department."""
    now = now or timezone.now()
    if not staff.is_department:
        raise WorkflowRefused("DEPARTMENT_ONLY", status=403)
    target = str(advisor_id or "").strip()
    if not advisor_id_exists(target):
        raise WorkflowRefused("UNKNOWN_ADVISOR", status=400)
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _check_assign(staff, case)
        if case.assigned_advisor_id == target:
            raise WorkflowRefused("ALREADY_ASSIGNED")
        before = case.assigned_advisor_id
        _claim(
            staff,
            case,
            now,
            to_status=S.IN_REVIEW if case.status == S.FORWARDED else None,
            assigned_advisor_id=target,
            handled_by=None,
            handled_by_username="",
            handled_advisor_id="",
        )
        _event(case, staff, Event.ASSIGNED, from_value=before, to_value=target)
    return case


def redacted(text: str, passages: Sequence[str]) -> str:
    """``text`` with every occurrence of each passage replaced by the marker.

    Matched on the text as it is, and overlapping or touching matches become one
    marker, so the result only ever loses words: nothing can be added, and no
    passage can match inside a marker another one left."""
    spans: list[tuple[int, int]] = []
    for passage in passages:
        start = text.find(passage)
        if start < 0:
            raise WorkflowRefused("REDACT_TEXT_NOT_FOUND", status=400)
        while start >= 0:
            spans.append((start, start + len(passage)))
            start = text.find(passage, start + 1)
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(REDACTION_MARKER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redaction_passages(remove: object) -> list[str]:
    """The passages to remove, one per line, as typed; blank lines ignored."""
    lines = str(remove or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.strip() for line in lines if line.strip()]


def redact(
    staff: Staff,
    reference: object,
    *,
    version: object,
    remove: object,
    message_id: object = None,
    point_id: object = None,
    now: datetime | None = None,
) -> None:
    """The department removes words from a message or from a point's details: a
    health or family detail, say. Each passage becomes the marker, the rest stays,
    and the original is not kept anywhere."""
    now = now or timezone.now()
    if not staff.is_department:
        raise WorkflowRefused("DEPARTMENT_ONLY", status=403)
    passages = redaction_passages(remove)
    if not passages:
        raise WorkflowRefused("REDACT_NOTHING", status=400)
    try:
        message_pk = uuid.UUID(str(message_id)) if message_id else None
        point_pk = uuid.UUID(str(point_id)) if point_id and not message_id else None
    except ValueError:
        raise WorkflowRefused("REDACT_TARGET_NOT_FOUND", status=404) from None
    with transaction.atomic():
        case = _locked_case(staff, reference)
        _expect_version(case, version)
        _check_department(staff, case)
        if message_pk is not None:
            message = case.messages.filter(pk=message_pk).first()
            if message is None:
                raise WorkflowRefused("REDACT_TARGET_NOT_FOUND", status=404)
            text = redacted(message.body, passages)
            _claim(staff, case, now, acted=False)
            case.messages.filter(pk=message_pk).update(body=text, redacted_at=now)
            _event(case, staff, Event.REDACTED, message_id=message_pk)
            return
        point = case.points.filter(pk=point_pk).first() if point_pk is not None else None
        if point is None:
            raise WorkflowRefused("REDACT_TARGET_NOT_FOUND", status=404)
        text = redacted(point.note, passages)
        _claim(staff, case, now, acted=False)
        case.points.filter(pk=point_pk).update(note=text, note_redacted_at=now)
        _event(case, staff, Event.REDACTED, point_id=point_pk)


def record_view(staff: Staff, case: TimetableFeedback, *, now: datetime | None = None) -> bool:
    """Note in the case's history that ``staff`` opened it, at most once an hour
    each. Not a claim: opening a case changes nothing about it."""
    now = now or timezone.now()
    recent = case.events.filter(
        kind=Event.VIEWED, actor=staff.user, created_at__gte=now - VIEW_EVENT_EVERY
    ).exists()
    if recent:
        return False
    _event(case, staff, Event.VIEWED)
    return True


# ---------------------------------------------------------------------------
# Many cases at once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BulkOutcome:
    reference: str
    #: '' when the action was taken, otherwise the refusal's code.
    refused: str = ""


def bulk_picks(values: Sequence[object]) -> list[tuple[str, str]]:
    """``REFERENCE@VERSION`` values as the queue sends them, each case once, in
    the order sent. Anything else is dropped: a case outside the scope is refused
    by the action itself."""
    picks: dict[str, str] = {}
    for value in values:
        reference, _, version = str(value or "").partition("@")
        wanted = workflow.case_reference(reference)
        if wanted and wanted not in picks:
            picks[wanted] = version
    return list(picks.items())


def _bulk(picks: Sequence[tuple[str, str]], act: Callable[[str, str], object]) -> list[BulkOutcome]:
    if not picks:
        raise WorkflowRefused("BULK_NOTHING", status=400)
    if len(picks) > BULK_MAX:
        raise WorkflowRefused("BULK_TOO_MANY", status=400)
    outcomes = []
    for reference, version in picks:
        # Each case on its own: one that changed is skipped and said, never lost.
        try:
            act(reference, version)
        except WorkflowRefused as refused:
            outcomes.append(BulkOutcome(reference, refused.code))
        else:
            outcomes.append(BulkOutcome(reference))
    return outcomes


def bulk_answer(
    staff: Staff,
    picks: Sequence[tuple[str, str]],
    *,
    template: str,
    now: datetime | None = None,
) -> list[BulkOutcome]:
    """Answer each picked case with one ready-made answer, in its student's language."""
    if template not in copy.REPLY_TEMPLATES:
        raise WorkflowRefused("TEMPLATE_UNKNOWN", status=400)
    return _bulk(
        picks,
        lambda reference, version: answer(
            staff, reference, version=version, template=template, now=now, leave_colleagues=True
        ),
    )


def bulk_close_duplicates(
    staff: Staff,
    picks: Sequence[tuple[str, str]],
    *,
    body: object,
    now: datetime | None = None,
) -> list[BulkOutcome]:
    """Close each picked case as a duplicate, with one comment for colleagues."""
    text = clean_staff_text(body)
    return _bulk(
        picks,
        lambda reference, version: close(
            staff,
            reference,
            version=version,
            reason=TimetableFeedback.CloseReason.DUPLICATE,
            body=text,
            now=now,
            leave_colleagues=True,
        ),
    )
