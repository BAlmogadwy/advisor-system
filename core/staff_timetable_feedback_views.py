"""The staff side of timetable feedback: the queue, one case, the actions on it,
removing words, many cases at once, and the guide.

WHO. Every route is wrapped in ``login_required(role_required(ROLE_ADVISOR))`` in
urls.py, so students and the exam committee never reach a view. Inside, every case
comes from `timetable_feedback_staff.visible_to`: a case outside a person's scope
is not found, rather than found and refused, and the page says so in the same
words as a case that does not exist.

FLAGS. With TIMETABLE_FEEDBACK_ENABLED off nothing here exists. ACCEPTING and the
pilot programmes close the sending of new feedback, not the work on feedback
already sent, so they are not read here.

WRITING. Each action is a plain form POST that works without JavaScript and names
the version of the case the page showed. Taken, it redirects back to the case
with a one-time notice (post/redirect/get); refused, it draws the case again with
the refusal's status, its sentence at the top and at its field, who last changed
the case when that is why, and the text that was typed. Every opening of a case
and every action goes to the audit log; the case's own history keeps the actions
and, once an hour per person, the openings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.models import (
    Student,
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
)
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff as service
from core.services import timetable_feedback_staff_copy as scopy
from core.services import timetable_feedback_workflow as workflow
from core.services.audit import log_audit_event
from core.services.credit_policy import EXPECTED_GRADUATE_STATUS
from core.services.rbac import ROLE_GENERAL_ADVISOR
from core.services.student_sections import cohort_of_student_section
from core.services.student_term_timetable import (
    TimetableUnavailable,
    read_student_term_timetable,
)
from core.sidebar_context import get_sidebar_context

#: The marker on this side's one-time notices, apart from the student pages' own.
NOTICE_TAG = "ttfs"
#: Cases on one page of the queue: one bulk action's worth.
QUEUE_PAGE_SIZE = service.BULK_MAX
#: Refusals about text that was typed, which are shown at its field too.
_TEXT_CODES = frozenset({"BODY_REQUIRED", "BODY_TOO_LONG", "BODY_INVALID"})
#: Which field a refusal is about, beyond the typed text.
_FIELD_OF_CODE = {
    "CLOSE_REASON_REQUIRED": "close_reason",
    "UNKNOWN_ADVISOR": "assign_advisor",
    "ALREADY_ASSIGNED": "assign_advisor",
    "TEMPLATE_UNKNOWN": "answer_template",
}
#: History entries the student made; the rest with no person are the system's.
_STUDENT_EVENTS = frozenset(
    {
        "SUBMITTED",
        "POINT_ADDED",
        "POINT_WITHDRAWN",
        "STUDENT_REPLY",
        "STUDENT_FOLLOWUP",
        "ANSWER_CONFIRMED",
        "REOPENED",
        "WITHDRAWN",
    }
)
#: History entries whose before and after are statuses.
_STATUS_EVENTS = frozenset(
    {
        "TAKEN",
        "STATUS_CHANGED",
        "STAFF_MESSAGE",
        "FORWARDED",
        "STUDENT_REPLY",
        "ANSWER_CONFIRMED",
        "REOPENED",
        "WITHDRAWN",
        "AUTO_CLOSED",
    }
)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _language(request: HttpRequest) -> str:
    return copy.language_of(getattr(request, "LANGUAGE_CODE", ""))


def _context(request: HttpRequest, language: str, **extra: Any) -> dict[str, Any]:
    return {
        **get_sidebar_context(request),
        "language": language,
        "t": scopy.strings(language),
        **extra,
    }


def _unavailable(
    request: HttpRequest, language: str, *, status: int, message: object = None
) -> HttpResponse:
    context = _context(request, language)
    context["message"] = message or context["t"]["unavailable_body"]
    return render(request, "core/staff_timetable_feedback_unavailable.html", context, status=status)


def _begin(request: HttpRequest) -> tuple[str, service.Staff] | HttpResponse:
    """The page's language and the member of staff, or the response that ends the
    request."""
    language = _language(request)
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return _unavailable(request, language, status=404)
    staff = service.staff_of(request.user)
    if staff is None:
        # role_required has already let only staff through; this is its backstop.
        return _unavailable(request, language, status=403)
    return language, staff


def _case(
    request: HttpRequest, language: str, staff: service.Staff, reference: object
) -> TimetableFeedback | HttpResponse:
    """The case by its reference within ``staff``'s scope, or the not-found page."""
    wanted = workflow.case_reference(reference)
    case = (
        service.visible_to(staff).filter(reference=wanted).select_related("handled_by").first()
        if wanted
        else None
    )
    if case is None:
        return _unavailable(
            request, language, status=404, message=scopy.refusal("CASE_NOT_FOUND", language)
        )
    return case


def _in_scope_now(staff: service.Staff, case: TimetableFeedback) -> TimetableFeedback | None:
    """``case`` read again through ``staff``'s scope: None once it has left it (the
    student moved to another advisor, say) since it was first read."""
    return service.visible_to(staff).filter(pk=case.pk).select_related("handled_by").first()


def _date(value: Any) -> str:
    """A plain ISO date: Django's Arabic formats would add marks and 12-hour times."""
    return timezone.localtime(value).strftime("%Y-%m-%d") if value else ""


def _moment(value: Any) -> str:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _normalise_newlines(value: object) -> str:
    """A browser sends each line break as CR LF, which would count twice."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _language_name(code: str, language: str) -> str:
    key = "language_ar" if code == "ar" else "language_en"
    return scopy.strings(language)[key]


def _cohort(code: str, language: str) -> str:
    return scopy.strings(language).get(f"cohort_{code}", "") if code else ""


def _students(student_ids: set[int]) -> dict[int, dict[str, Any]]:
    """The students' rows as they are now, by id: the queue shows who they are today."""
    return {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=student_ids).values(
            "student_id", "name", "program", "section", "status", "advisor_id"
        )
    }


def _point_summary(point: TimetableFeedbackPoint, language: str) -> str:
    reason = scopy.reason_label(point.reason_code, language)
    if point.anchor_kind == TimetableFeedbackPoint.AnchorKind.SECTION:
        return f"{point.course_key} {point.section_label} — {reason}"
    if point.anchor_kind == TimetableFeedbackPoint.AnchorKind.TIMETABLE:
        return f"{scopy.strings(language)['point_whole_timetable']} — {reason}"
    return reason


def _last_change(case: TimetableFeedback, language: str) -> object:
    """Who changed the case last and when, for a refusal that says it changed."""
    event = (
        case.events.exclude(kind=TimetableFeedbackEvent.Kind.VIEWED).order_by("-created_at").first()
    )
    if event is None:
        return ""
    return scopy.text(
        "changed_by",
        language,
        name=_actor(event, language),
        time=copy.bdi(_moment(event.created_at)),
    )


def _actor(event: TimetableFeedbackEvent, language: str) -> str:
    if event.actor_label or event.actor_username:
        return event.actor_label or event.actor_username
    key = "history_student" if event.kind in _STUDENT_EVENTS else "history_system"
    return scopy.strings(language)[key]


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    tab: str
    search: str
    reason: str
    programme: str
    registered: str
    new: bool
    graduating: bool
    unrouted: bool
    page: str

    @classmethod
    def read(cls, query: QueryDict, staff: service.Staff) -> _Filters:
        tab = str(query.get("tab") or "")
        reason = str(query.get("reason") or "").strip().upper()
        registered = str(query.get("registered") or "")
        return cls(
            tab=tab if tab in service.TABS else service.TABS[0],
            search=str(query.get("q") or "").strip(),
            reason=reason if reason in scopy.REASON else "",
            programme=str(query.get("programme") or "").strip().upper(),
            registered=registered if registered in ("yes", "no") else "",
            new=query.get("new") == "1",
            graduating=query.get("graduating") == "1",
            # The catch-all is the department's; for anyone else it filters nothing.
            unrouted=query.get("unrouted") == "1" and staff.is_department,
            page=str(query.get("page") or "1"),
        )

    def q(self) -> Q:
        """Everything but the tab."""
        q = Q()
        if self.search:
            reference = workflow.case_reference(self.search)
            if reference:
                q &= Q(reference=reference)
            elif self.search.isascii() and self.search.isdigit():
                q &= Q(student_id=int(self.search))
            else:
                q &= Q(pk__in=[])
        if self.reason:
            q &= Q(
                Exists(
                    TimetableFeedbackPoint.objects.filter(
                        case=OuterRef("pk"),
                        withdrawn_at__isnull=True,
                        reason_code=self.reason,
                    )
                )
            )
        if self.programme:
            q &= Q(student_id__in=service.students_in([self.programme]).values("student_id"))
        if self.registered:
            q &= Q(has_registration_at_submission=self.registered == "yes")
        if self.new:
            q &= service.student_wrote_since_staff_q()
        if self.graduating:
            q &= Q(
                student_id__in=Student.objects.filter(status=EXPECTED_GRADUATE_STATUS).values(
                    "student_id"
                )
            )
        if self.unrouted:
            q &= service.unrouted_q()
        return q

    def query_for(self, **changes: object) -> str:
        """The query string for these filters with ``changes``, for links."""
        values = {
            "tab": self.tab,
            "q": self.search,
            "reason": self.reason,
            "programme": self.programme,
            "registered": self.registered,
            "new": "1" if self.new else "",
            "graduating": "1" if self.graduating else "",
            "unrouted": "1" if self.unrouted else "",
        }
        values.update({key: str(value) for key, value in changes.items()})
        query = QueryDict(mutable=True)
        for key, value in values.items():
            if value:
                query[key] = value
        return query.urlencode()


#: Oldest first while a case waits on someone; newest first in the record.
_ORDER = {
    "action": ("created_at",),
    "open": ("created_at",),
    "student": ("created_at",),
    "department": ("created_at",),
    "closed": ("-created_at",),
    "all": ("-created_at",),
}


def _programmes(staff: service.Staff) -> list[str]:
    """The programmes a person can filter by: their departments, or those of the
    students whose cases they can see."""
    if staff.role == ROLE_GENERAL_ADVISOR:
        return list(staff.departments)
    students = service.visible_to(staff).values("student_id")
    return sorted(
        {
            str(programme).strip().upper()
            for programme in Student.objects.filter(student_id__in=students).values_list(
                "program", flat=True
            )
            if str(programme or "").strip()
        }
    )


@never_cache
@require_GET
def queue_view(request: HttpRequest) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, staff = begun
    filters = _Filters.read(request.GET, staff)
    now = timezone.now()

    filtered = service.visible_to(staff).filter(filters.q())
    counts = filtered.aggregate(
        **{tab: Count("pk", filter=service.tab_q(tab, staff)) for tab in service.TABS}
    )
    live_points = TimetableFeedbackPoint.objects.filter(withdrawn_at__isnull=True).order_by(
        "created_at"
    )
    cases = (
        filtered.filter(service.tab_q(filters.tab, staff))
        .order_by(*_ORDER[filters.tab], "reference")
        .prefetch_related(Prefetch("points", queryset=live_points, to_attr="live_points"))
    )
    page = Paginator(cases, QUEUE_PAGE_SIZE).get_page(filters.page)
    rows_on_page = list(page.object_list)
    students = _students({case.student_id for case in rows_on_page})
    routed = (
        set(service.active_advisor_ids().values_list("held", flat=True))
        if staff.is_department
        else set()
    )
    t = scopy.strings(language)
    rows = []
    for case in rows_on_page:
        student = students.get(case.student_id)
        advisor_now = str((student or {}).get("advisor_id") or "").strip()
        points = [_point_summary(point, language) for point in case.live_points]
        latest = max(
            moment
            for moment in (
                case.created_at,
                case.last_student_activity_at,
                case.last_staff_action_at,
            )
            if moment is not None
        )
        rows.append(
            {
                "reference": case.reference,
                "version": case.version,
                "select_label": scopy.plain("select_case", language, reference=case.reference),
                "status_code": case.status,
                "status": scopy.status_label(case.status, language),
                "days": max(0, (now - case.created_at).days),
                "student_id": case.student_id,
                "student_name": (student or {}).get("name") or "",
                "student_missing": student is None,
                "programme": (student or {}).get("program") or case.program_at_submission,
                "points": points[:1],
                "more": (
                    scopy.text("more_points", language, count=copy.bdi(len(points) - 1))
                    if len(points) > 1
                    else ""
                ),
                "handler": case.handled_by_username,
                "activity": _date(latest),
                "new": bool(
                    case.last_student_activity_at
                    and (
                        case.last_staff_action_at is None
                        or case.last_student_activity_at > case.last_staff_action_at
                    )
                ),
                "unrouted": staff.is_department
                and advisor_now not in routed
                and case.assigned_advisor_id not in routed,
                "registered": case.has_registration_at_submission,
                "graduating": (student or {}).get("status") == EXPECTED_GRADUATE_STATUS,
            }
        )

    tabs = [
        {
            "key": tab,
            "label": t[f"tab_{tab}"],
            "count": counts[tab],
            "current": tab == filters.tab,
            "query": filters.query_for(tab=tab),
        }
        for tab in service.TABS
    ]
    empty = service.why_nothing_is_visible(staff)
    return render(
        request,
        "core/staff_timetable_feedback_queue.html",
        _context(
            request,
            language,
            staff=staff,
            filters=filters,
            tabs=tabs,
            rows=rows,
            page=page,
            page_label=scopy.text(
                "page_of",
                language,
                page=copy.bdi(page.number),
                pages=copy.bdi(page.paginator.num_pages),
            ),
            previous_query=(
                filters.query_for(page=page.previous_page_number()) if page.has_previous() else ""
            ),
            next_query=(filters.query_for(page=page.next_page_number()) if page.has_next() else ""),
            empty=t[f"empty_{empty}"] if empty in ("NO_ADVISOR_ID", "NO_DEPARTMENTS") else "",
            reasons=[(code, scopy.reason_label(code, language)) for code in scopy.REASON],
            programmes=_programmes(staff),
            templates=[(key, scopy.template_name(key, language)) for key in copy.REPLY_TEMPLATES],
            bulk_hint=scopy.text("bulk_hint", language, max=copy.bdi(service.BULK_MAX)),
            text_max=service.STAFF_TEXT_MAX_CHARS,
        ),
    )


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def _current_timetable(
    case: TimetableFeedback, student: dict[str, Any] | None, language: str
) -> dict[str, Any]:
    """The student's expected timetable now, for a case about the term being
    planned only: a later term's import reuses section ids, so an older case is
    shown as it was sent."""
    academic_year, term = feedback.planning_term()
    if (str(case.academic_year), str(case.term)) != (str(academic_year), str(term)):
        return {
            "note": scopy.text(
                "current_other_term",
                language,
                term=copy.term_html(case.academic_year, case.term),
            )
        }
    if student is None:
        return {"note": scopy.strings(language)["current_unavailable"]}
    try:
        timetable = read_student_term_timetable(case.student_id, academic_year, term)
    except TimetableUnavailable:
        return {"note": scopy.strings(language)["current_unavailable"]}
    sections = feedback.seen_sections(
        timetable, programme=str(student.get("program") or ""), language=language
    )
    return {
        "fingerprint": feedback.forecast_fingerprint(
            academic_year=academic_year,
            term=term,
            forecast_class=timetable.forecast_class,
            sections=sections,
        ),
        "registered": timetable.has_registration,
        "sections": [
            {
                "course": section.course_key,
                "section": section.section,
                "name": section.displayed_course_name,
                "meetings": [
                    {"day": copy.day_label(day, language), "start": start, "end": end}
                    for day, start, end, _room in section.meetings
                ],
            }
            for section in sections
        ],
    }


def _point_view(point: TimetableFeedbackPoint, language: str, fingerprint: str) -> dict[str, Any]:
    t = scopy.strings(language)
    name, meetings = "", []
    if point.anchor_kind == TimetableFeedbackPoint.AnchorKind.SECTION:
        seen = next(
            (
                entry
                for entry in (point.seen_snapshot or {}).get("sections", [])
                if entry.get("term_section_id") == point.term_section_id
            ),
            None,
        )
        about = scopy.text(
            "point_section",
            language,
            course=copy.bdi((seen or {}).get("course_key") or point.course_key),
            section=copy.bdi((seen or {}).get("section") or point.section_label),
        )
        name = (seen or {}).get("displayed_course_name") or ""
        meetings = [
            {"day": copy.day_label(day, language), "start": start, "end": end}
            for day, start, end, _room in (seen or {}).get("meetings") or []
        ]
    elif point.anchor_kind == TimetableFeedbackPoint.AnchorKind.TIMETABLE:
        about = t["point_whole_timetable"]
    else:
        about = t["point_no_forecast"]
    withdrawn = point.withdrawn_at is not None
    return {
        "id": str(point.pk),
        "about": about,
        "name": name,
        "meetings": meetings,
        "reason": scopy.reason_label(point.reason_code, language),
        "requested_section": point.requested_section_label,
        "requested_course": point.requested_course_key,
        "note": point.note,
        "note_redacted": point.note_redacted_at is not None,
        "withdrawn": withdrawn,
        "sent": _date(point.created_at),
        "changed": bool(fingerprint) and not withdrawn and point.seen_fingerprint != fingerprint,
    }


def _thread(case: TimetableFeedback, language: str) -> list[dict[str, Any]]:
    t = scopy.strings(language)
    rows = []
    for message in case.messages.order_by("created_at"):
        from_student = message.author_kind == TimetableFeedbackMessage.AuthorKind.STUDENT
        internal = message.audience == TimetableFeedbackMessage.Audience.INTERNAL
        rows.append(
            {
                "id": str(message.pk),
                "author": t["thread_from_student"]
                if from_student
                else (message.author_label or message.author_username),
                "username": "" if from_student else message.author_username,
                "shown_as": ""
                if from_student or internal
                else scopy.text(
                    "thread_shown_as",
                    language,
                    label=copy.author_label(message.author_kind, language),
                ),
                "internal": internal,
                "from_student": from_student,
                "body": message.body,
                "sent": _moment(message.created_at),
                "redacted": message.redacted_at is not None,
            }
        )
    return rows


def _history(case: TimetableFeedback, language: str) -> list[dict[str, str]]:
    arrow = scopy.strings(language)["change_arrow"]
    rows = []
    for event in case.events.order_by("created_at"):
        change = ""
        if event.kind in _STATUS_EVENTS and event.to_value and event.from_value != event.to_value:
            change = (
                f"{scopy.status_label(event.from_value, language)} {arrow} "
                f"{scopy.status_label(event.to_value, language)}"
            )
        elif event.kind == TimetableFeedbackEvent.Kind.ASSIGNED:
            change = f"{event.from_value or '—'} {arrow} {event.to_value}"
        rows.append(
            {
                "what": scopy.event_label(event.kind, language),
                "who": _actor(event, language),
                "change": change,
                "when": _moment(event.created_at),
            }
        )
    return rows


def case_page(
    request: HttpRequest,
    case: TimetableFeedback,
    *,
    staff: service.Staff,
    language: str,
    notice: object = "",
    errors: list[dict[str, Any]] | None = None,
    values: dict[str, str] | None = None,
    status: int = 200,
) -> HttpResponse:
    """One case as staff see it, with what this person can do on it now."""
    t = scopy.strings(language)
    errors = list(errors or [])
    for index, error in enumerate(errors):
        error["id"] = f"ttfsError-{index}"
    student = (
        Student.objects.filter(student_id=case.student_id)
        .values("name", "program", "section", "status", "advisor_id")
        .first()
    )
    advisor_now = str((student or {}).get("advisor_id") or "").strip()
    accounts = service.accounts_holding(advisor_now)
    assigned_reachable = bool(case.assigned_advisor_id) and service.advisor_id_exists(
        case.assigned_advisor_id
    )
    current = _current_timetable(case, student, language)
    offered = {
        action: service.staff_action_refusal(staff, case, action) is None
        for action in service.STAFF_ACTIONS
    }
    taking_over = case.handled_by_id is not None and offered["take"]
    text_max = copy.bdi(service.STAFF_TEXT_MAX_CHARS)
    record_url = (
        reverse("advisor_portfolio_student_graduation_page", args=[case.student_id])
        if service.may_open_student_record(staff, student)
        else ""
    )
    cohort = cohort_of_student_section((student or {}).get("section")) if student else ""
    context = _context(
        request,
        language,
        staff=staff,
        case={
            "reference": case.reference,
            "version": case.version,
            "title": scopy.text("case_title", language, reference=copy.bdi(case.reference)),
            "page_title": scopy.plain("case_title", language, reference=case.reference),
            "status_code": case.status,
            "status": scopy.status_label(case.status, language),
            "close_reason": (
                scopy.close_reason_label(case.close_reason, language) if case.close_reason else ""
            ),
            "term": copy.term_html(case.academic_year, case.term),
            "sent": _date(case.created_at),
            "handler": case.handled_by_username,
            "assigned": case.assigned_advisor_id,
            "advisor_then": case.routed_advisor_id,
            "registered": case.has_registration_at_submission,
            "language": _language_name(case.submitted_language, language),
        },
        student={
            "id": case.student_id,
            "name": (student or {}).get("name") or "",
            "missing": student is None,
            "programme": (student or {}).get("program") if student else case.program_at_submission,
            "cohort": _cohort(cohort if student else case.cohort_at_submission, language),
            "standing": (student or {}).get("status") if student else case.standing_at_submission,
            "advisor_now": advisor_now,
            "record_url": record_url,
        },
        visible_to=(
            scopy.text(
                "case_visible_to", language, advisor=copy.bdi(advisor_now), count=copy.bdi(accounts)
            )
            if accounts
            else ""
        ),
        unrouted=not accounts and not assigned_reachable,
        current=current,
        points=[
            _point_view(point, language, current.get("fingerprint", ""))
            for point in case.points.order_by("created_at")
        ],
        thread=_thread(case, language),
        history=_history(case, language),
        offered=offered,
        taking_over=taking_over,
        language_hint=scopy.text(
            "language_hint",
            language,
            name=_language_name(case.submitted_language, language),
        )
        if case.submitted_language in ("ar", "en")
        else "",
        text_hint=scopy.text("text_hint", language, max=text_max),
        text_max=service.STAFF_TEXT_MAX_CHARS,
        templates=[(key, scopy.template_name(key, language)) for key in copy.REPLY_TEMPLATES],
        close_reasons=[
            (code, scopy.close_reason_label(code, language)) for code in service.STAFF_CLOSE_REASONS
        ],
        notice=notice,
        errors=errors,
        error_for={error["field"]: error for error in errors if error.get("field")},
        values=values or {},
        empty_text=t["case_none"],
    )
    return render(request, "core/staff_timetable_feedback_case.html", context, status=status)


def _notice(request: HttpRequest, case: TimetableFeedback, language: str) -> object:
    for message in messages.get_messages(request):
        if NOTICE_TAG in message.tags and message.message in scopy.NOTICE:
            return scopy.notice(
                message.message, language, advisor=copy.bdi(case.assigned_advisor_id)
            )
    return ""


@never_cache
@require_GET
def case_view(request: HttpRequest, reference: str) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, staff = begun
    case = _case(request, language, staff, reference)
    if isinstance(case, HttpResponse):
        return case
    service.record_view(staff, case)
    log_audit_event(
        request,
        action="timetable_feedback.case_view",
        status="allow",
        details={"reference": case.reference, "student_id": case.student_id},
    )
    return case_page(
        request, case, staff=staff, language=language, notice=_notice(request, case, language)
    )


# ---------------------------------------------------------------------------
# Acting on one case
# ---------------------------------------------------------------------------

#: Each action: what it does with the POST, and the notice it leaves.
_ACTIONS: dict[str, tuple[Callable[[service.Staff, str, QueryDict, str], object], str]] = {
    "take": (lambda s, ref, post, body: service.take(s, ref, version=post.get("version")), "TAKEN"),
    "note": (
        lambda s, ref, post, body: service.add_note(s, ref, version=post.get("version"), body=body),
        "NOTE_ADDED",
    ),
    "ask": (
        lambda s, ref, post, body: service.ask(s, ref, version=post.get("version"), body=body),
        "ASKED",
    ),
    "answer": (
        lambda s, ref, post, body: service.answer(
            s,
            ref,
            version=post.get("version"),
            body=body,
            template=str(post.get("template") or ""),
        ),
        "ANSWERED",
    ),
    "forward": (
        lambda s, ref, post, body: service.forward(s, ref, version=post.get("version"), body=body),
        "FORWARDED",
    ),
    "close": (
        lambda s, ref, post, body: service.close(
            s, ref, version=post.get("version"), reason=post.get("reason"), body=body
        ),
        "CLOSED",
    ),
    "assign": (
        lambda s, ref, post, body: service.assign(
            s, ref, version=post.get("version"), advisor_id=post.get("advisor_id")
        ),
        "ASSIGNED",
    ),
}


def _refused_page(
    request: HttpRequest,
    refused: workflow.WorkflowRefused,
    *,
    case: TimetableFeedback,
    staff: service.Staff,
    language: str,
    field: str,
    values: dict[str, str],
) -> HttpResponse:
    """The case again, as it is now, with the refusal: read again through the
    person's scope, which may have changed since the case was first read."""
    fresh = _in_scope_now(staff, case) if refused.code != "CASE_NOT_FOUND" else None
    if fresh is None:
        return _unavailable(
            request, language, status=404, message=scopy.refusal("CASE_NOT_FOUND", language)
        )
    case = fresh
    errors = [
        {
            "field": field if refused.code in _TEXT_CODES else _FIELD_OF_CODE.get(refused.code, ""),
            "html": scopy.refusal(refused.code, language),
            "changed_by": _last_change(case, language) if refused.code == "CASE_CHANGED" else "",
        }
    ]
    return case_page(
        request,
        case,
        staff=staff,
        language=language,
        errors=errors,
        values=values,
        status=refused.status,
    )


@never_cache
@require_POST
def action_view(request: HttpRequest, reference: str) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, staff = begun
    case = _case(request, language, staff, reference)
    if isinstance(case, HttpResponse):
        return case
    action = str(request.POST.get("action") or "")
    body = _normalise_newlines(request.POST.get("body"))
    values = {f"{action}_body": body} if action in _ACTIONS else {}
    if action == "close":
        values["close_reason"] = str(request.POST.get("reason") or "")
    if action == "assign":
        values["assign_advisor"] = str(request.POST.get("advisor_id") or "")
    if action == "answer":
        values["answer_template"] = str(request.POST.get("template") or "")
    try:
        if action not in _ACTIONS:
            raise workflow.WorkflowRefused("NOT_ALLOWED", status=400)
        act, notice_code = _ACTIONS[action]
        act(staff, case.reference, request.POST, body)
    except workflow.WorkflowRefused as refused:
        log_audit_event(
            request,
            action=f"timetable_feedback.{action or 'unknown'}",
            status="deny",
            details={"reference": case.reference, "reason_code": refused.code},
        )
        return _refused_page(
            request,
            refused,
            case=case,
            staff=staff,
            language=language,
            field=f"{action}_body",
            values=values,
        )
    log_audit_event(
        request,
        action=f"timetable_feedback.{action}",
        status="success",
        details={"reference": case.reference},
    )
    messages.add_message(request, messages.SUCCESS, notice_code, extra_tags=NOTICE_TAG)
    return redirect("staff_timetable_feedback_case", reference=case.reference)


# ---------------------------------------------------------------------------
# Removing words
# ---------------------------------------------------------------------------


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value or ""))
    except ValueError:
        return None


def _redaction_target(case: TimetableFeedback, query: QueryDict) -> tuple[str, str, str] | None:
    """(kind, id, text) for the message or point named in ``query``, within the case;
    None for anything else, text that is not an id included."""
    if query.get("message"):
        message_id = _uuid(query.get("message"))
        message = case.messages.filter(pk=message_id).first() if message_id else None
        return ("message", str(message_id), message.body) if message else None
    point_id = _uuid(query.get("point"))
    point = case.points.filter(pk=point_id).first() if point_id else None
    return ("point", str(point_id), point.note) if point and point.note else None


@never_cache
@require_http_methods(["GET", "POST"])
def redact_view(request: HttpRequest, reference: str) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, staff = begun
    case = _case(request, language, staff, reference)
    if isinstance(case, HttpResponse):
        return case
    if not staff.is_department:
        return _unavailable(
            request, language, status=403, message=scopy.refusal("DEPARTMENT_ONLY", language)
        )
    query = request.POST if request.method == "POST" else request.GET
    target = _redaction_target(case, query)
    if target is None:
        return _unavailable(
            request,
            language,
            status=404,
            message=scopy.refusal("REDACT_TARGET_NOT_FOUND", language),
        )
    kind, target_id, current_text = target
    remove = _normalise_newlines(request.POST.get("remove")) if request.method == "POST" else ""
    errors: list[dict[str, Any]] = []
    status = 200
    if request.method == "POST":
        try:
            service.redact(
                staff,
                case.reference,
                version=request.POST.get("version"),
                remove=remove,
                message_id=target_id if kind == "message" else None,
                point_id=target_id if kind == "point" else None,
            )
        except workflow.WorkflowRefused as refused:
            log_audit_event(
                request,
                action="timetable_feedback.redact",
                status="deny",
                details={"reference": case.reference, "reason_code": refused.code},
            )
            fresh = _in_scope_now(staff, case) if refused.code != "CASE_NOT_FOUND" else None
            if fresh is None:
                return _unavailable(
                    request,
                    language,
                    status=404,
                    message=scopy.refusal("CASE_NOT_FOUND", language),
                )
            case = fresh
            target = _redaction_target(case, query)
            current_text = target[2] if target else current_text
            errors = [
                {
                    "id": "ttfsError-0",
                    "field": "remove" if refused.code.startswith("REDACT_") else "",
                    "html": scopy.refusal(refused.code, language),
                    "changed_by": (
                        _last_change(case, language) if refused.code == "CASE_CHANGED" else ""
                    ),
                }
            ]
            status = refused.status
        else:
            log_audit_event(
                request,
                action="timetable_feedback.redact",
                status="success",
                details={"reference": case.reference, "target": kind, "target_id": target_id},
            )
            messages.add_message(request, messages.SUCCESS, "REDACTED", extra_tags=NOTICE_TAG)
            return redirect("staff_timetable_feedback_case", reference=case.reference)
    what = scopy.strings(language)["redact_message" if kind == "message" else "redact_point"]
    return render(
        request,
        "core/staff_timetable_feedback_redact.html",
        _context(
            request,
            language,
            case={"reference": case.reference, "version": case.version},
            title=scopy.text("redact_title", language, reference=copy.bdi(case.reference)),
            page_title=scopy.plain("redact_title", language, reference=case.reference),
            intro=scopy.text("redact_intro", language, marker=copy.bdi(service.REDACTION_MARKER)),
            what=what,
            kind=kind,
            target_id=target_id,
            current_text=current_text,
            remove=remove,
            errors=errors,
            error_for={error["field"]: error for error in errors if error.get("field")},
        ),
        status=status,
    )


# ---------------------------------------------------------------------------
# Many cases at once
# ---------------------------------------------------------------------------


@never_cache
@require_POST
def bulk_view(request: HttpRequest) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, staff = begun
    picks = service.bulk_picks(request.POST.getlist("pick"))
    action = str(request.POST.get("bulk") or "")
    outcomes: list[service.BulkOutcome] = []
    error: object = ""
    status = 200
    try:
        if action == "answer":
            outcomes = service.bulk_answer(
                staff, picks, template=str(request.POST.get("template") or "")
            )
        elif action == "close":
            outcomes = service.bulk_close_duplicates(
                staff, picks, body=_normalise_newlines(request.POST.get("note"))
            )
        else:
            raise workflow.WorkflowRefused("NOT_ALLOWED", status=400)
    except workflow.WorkflowRefused as refused:
        error = scopy.refusal(refused.code, language)
        status = refused.status
    done = [outcome.reference for outcome in outcomes if not outcome.refused]
    skipped = [
        {"reference": outcome.reference, "why": scopy.refusal(outcome.refused, language)}
        for outcome in outcomes
        if outcome.refused
    ]
    log_audit_event(
        request,
        action=f"timetable_feedback.bulk_{action or 'unknown'}",
        status="success" if not error else "deny",
        details={
            "done": done,
            "skipped": [outcome.reference for outcome in outcomes if outcome.refused],
        },
    )
    return render(
        request,
        "core/staff_timetable_feedback_bulk.html",
        _context(
            request,
            language,
            error=error,
            done=done,
            skipped=skipped,
            done_count=scopy.text("bulk_done", language, count=copy.bdi(len(done))),
            skipped_count=scopy.text("bulk_skipped", language, count=copy.bdi(len(skipped))),
        ),
        status=status,
    )


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------


@never_cache
@require_GET
def guide_view(request: HttpRequest) -> HttpResponse:
    begun = _begin(request)
    if isinstance(begun, HttpResponse):
        return begun
    language, _staff = begun
    days = copy.count_text(int(settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS), "day", language)
    return render(
        request,
        "core/staff_timetable_feedback_guide.html",
        _context(
            request,
            language,
            steps=[
                {
                    "title": scopy.fill(title, language),
                    "body": scopy.fill(body, language, days=days),
                }
                for title, body in scopy.GUIDE_STEPS
            ],
            reasons=[
                {
                    "reason": scopy.reason_label(code, language),
                    "advice": scopy.fill(advice, language),
                }
                for code, advice in scopy.GUIDE_REASONS.items()
            ],
            rules=[scopy.fill(rule, language) for rule in scopy.GUIDE_RULES],
        ),
    )
