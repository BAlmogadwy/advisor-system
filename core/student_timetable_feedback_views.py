"""The student's timetable-feedback pages: the list, the form, and one case.

WHO. The student is the session's student and nobody else: the id comes only from
the session scope, and every case is looked up by that id AND its reference in one
query, so another student's reference is simply not found. Staff are sent to the
dashboard and an unbound student account gets the portal's 409 page, as on /student/.

WHAT IS SHOWN. A case shows what the student sent, frozen: the section as it stood
in their expected timetable, from the point's own snapshot, never from today's
TermSection (ids are reused by later imports). Messages are filtered to the
student-visible audience in the query, and only the fields a student may read are
fetched, so an internal note or a staff name cannot reach a template.

THE FORM. Two steps that work without JavaScript: first what the feedback is about
(a section of the expected timetable, or the timetable as a whole), then the issue.
It reads the planning term's forecast the way `submit_point` does, without the home
page's Arabic course-name swap, because the alternatives and the fingerprint must
be the ones the service will check. A refusal is shown from its code, with the
status the service gave it; a sent point redirects to its case, which says once
what happened.

FLAGS. With TIMETABLE_FEEDBACK_ENABLED off nothing here exists for students. With
ACCEPTING off, or a programme outside the pilot, the form is closed while the list
and the cases stay readable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.models import (
    TIMETABLE_FEEDBACK_TERMINAL_STATUSES,
    Student,
    TermSection,
    TimetableFeedback,
    TimetableFeedbackMessage,
)
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_workflow as workflow
from core.services.rbac import ROLE_STUDENT, get_user_scope
from core.services.student_term_timetable import (
    StudentTermTimetable,
    TimetableUnavailable,
    read_student_term_timetable,
)
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)

#: The order the issues are offered in. The services decide which are allowed
#: (REASONS_BY_ANCHOR); this only decides how they read.
REASON_ORDER: dict[str, tuple[str, ...]] = {
    "SECTION": (
        "TIME_CLASH",
        "TIME_UNSUITABLE",
        "PREFER_OTHER_SECTION",
        "ALREADY_PASSED",
        "PREREQUISITE_NOT_MET",
        "NOT_IN_MY_PLAN",
        "SECTION_HAS_NO_TIMES",
        "OTHER",
    ),
    "TIMETABLE": (
        "LOAD_TOO_HIGH",
        "LOAD_TOO_LOW",
        "MISSING_COURSE",
        "TOO_MANY_CAMPUS_DAYS",
        "GRADUATION_CONCERN",
        "OTHER",
    ),
}

#: The marker on our own one-shot notices, so a notice is read only by this page.
NOTICE_TAG = "ttf"

#: Issues that must name something: the service refuses them without a requested
#: section (PREFER_OTHER_SECTION) or course (MISSING_COURSE).
NEEDS_A_CHOICE = frozenset({"PREFER_OTHER_SECTION", "MISSING_COURSE"})


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _student(request: HttpRequest) -> int | HttpResponse:
    """The session's student id, or the response that ends the request."""
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("dashboard")
    student_id = scope.get("student_id")
    if student_id is None:
        # The portal's own terminal page for an unbound account, as on /student/.
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )
    return int(student_id)


def _context(request: HttpRequest, language: str, **extra: Any) -> dict[str, Any]:
    return {
        **get_sidebar_context(request),
        "language": language,
        "t": copy.strings(language),
        **extra,
    }


def _unavailable(
    request: HttpRequest, language: str, *, status: int, message: object = None
) -> HttpResponse:
    """A page in the student's own shell and language, for anything not there."""
    context = _context(request, language)
    context["message"] = message or context["t"]["unavailable_body"]
    return render(
        request, "core/student_timetable_feedback_unavailable.html", context, status=status
    )


def _date(value: Any) -> str:
    """A plain ISO date: Django's Arabic formats would add marks and 12-hour times."""
    return timezone.localtime(value).strftime("%Y-%m-%d") if value else ""


def _meetings(rows: Any, language: str) -> list[dict[str, str]]:
    return [
        {"day": copy.day_label(day, language), "start": start, "end": end, "room": room}
        for day, start, end, room in rows
    ]


def _normalise_newlines(value: object) -> str:
    """A browser sends each line break as CR LF, which the services would count as two
    characters against the 1,000 a textarea allowed as one."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# Where the next point would go
# ---------------------------------------------------------------------------
#
# Every page asks the same two questions before it offers to send: which case the
# student's next point would go into, and whether the service would take it. A
# page offers "add another point" or "send" only where the answer is yes, and
# otherwise says why, so nobody writes a point that is then refused.


def _open_case_for(
    student_id: int, academic_year: object, term: object
) -> TimetableFeedback | None:
    """The student's open case for that term: the one a new point is offered to."""
    return (
        TimetableFeedback.objects.filter(
            student_id=student_id, academic_year=str(academic_year), term=str(term)
        )
        .exclude(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES)
        .order_by("-created_at")
        .first()
    )


def _settled(case: TimetableFeedback) -> bool:
    """An answer past its reopen window. The next point does not join it: the
    service closes it and opens a new case."""
    return case.status == TimetableFeedback.Status.ANSWERED and workflow.answer_window_passed(
        case, timezone.now()
    )


def _next_point(
    open_case: TimetableFeedback | None, student_id: int, academic_year: object, term: object
) -> tuple[bool, str]:
    """``(joins_open_case, refusal)`` for the student's next point in that term.

    The point joins the open case unless that case is settled, and a full case
    refuses it. Otherwise it opens a new case, under the cap and the cool-down,
    which are asked of the service itself. ``refusal`` is the service's code, or
    "". Rate limits are not foreseen: they guard against floods, and their own
    refusal says how long to wait."""
    if open_case is not None and not _settled(open_case):
        live = open_case.points.filter(withdrawn_at__isnull=True).count()
        return True, ("TOO_MANY_POINTS" if live >= feedback.MAX_LIVE_POINTS else "")
    refused = feedback.new_case_refusal(student_id, str(academic_year), str(term))
    return False, (refused.code if refused is not None else "")


def _why_not(refusal: str, language: str, open_case: TimetableFeedback | None) -> object:
    """The sentence a page shows where its send button would be, or ""."""
    if not refusal:
        return ""
    if refusal == "TOO_MANY_POINTS" and open_case is not None:
        # "This feedback is full" would not say which, on a page that lists them.
        return copy.text(
            "case_full",
            language,
            reference=copy.bdi(open_case.reference),
            max=copy.bdi(feedback.MAX_LIVE_POINTS),
        )
    return copy.refusal(refusal, language)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@never_cache
@require_GET
def feedback_list_view(request: HttpRequest) -> HttpResponse:
    language = copy.language_of(getattr(request, "LANGUAGE_CODE", ""))
    student_id = _student(request)
    if isinstance(student_id, HttpResponse):
        return student_id
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return _unavailable(request, language, status=404)

    student = Student.objects.filter(student_id=student_id).first()
    cases = (
        TimetableFeedback.objects.filter(student_id=student_id)
        .annotate(live_points=Count("points", filter=Q(points__withdrawn_at__isnull=True)))
        .order_by("-created_at")
    )
    rows = [
        {
            "reference": case.reference,
            "url": reverse("student_timetable_feedback_detail", args=[case.reference]),
            "term": copy.term_html(case.academic_year, case.term),
            "status": copy.status_label(case.status, language),
            "sent": _date(case.created_at),
            "points": copy.text("list_points", language, count=copy.bdi(case.live_points)),
            "unread": workflow.has_unread(case),
        }
        for case in cases
    ]
    accepting = student is not None and feedback.accepting_for(student.program)
    refusal = ""
    open_case = None
    if accepting:
        academic_year, term = feedback.planning_term()
        open_case = _open_case_for(student_id, academic_year, term)
        _, refusal = _next_point(open_case, student_id, academic_year, term)
    can_raise = accepting and not refusal
    return render(
        request,
        "core/student_timetable_feedback_list.html",
        _context(
            request,
            language,
            rows=rows,
            can_raise=can_raise,
            # Say why, rather than leave a list with no way forward.
            send_refused=(
                ""
                if can_raise
                else (
                    _why_not(refusal, language, open_case)
                    if accepting
                    else copy.refusal("NOT_ACCEPTING", language)
                )
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


@dataclass
class _Forecast:
    """The planning term's forecast as the service will rebuild it."""

    academic_year: str
    term: str
    timetable: StudentTermTimetable
    sections: list[feedback.SeenSection]
    fingerprint: str
    clashing: frozenset[int]


def _read_forecast(student_id: int, programme: str, language: str) -> _Forecast:
    """Raises TimetableUnavailable, never an empty forecast in its place."""
    academic_year, term = feedback.planning_term()
    # No Arabic name swap here: the section identity that alternatives are matched
    # on is built from the stored name, exactly as `submit_point` builds it.
    timetable = read_student_term_timetable(student_id, academic_year, term)
    sections = feedback.seen_sections(timetable, programme=programme, language=language)
    clashing = frozenset(
        section_id
        for clash in feedback.clashes_between(sections)
        for section_id in (clash["a_term_section_id"], clash["b_term_section_id"])
    )
    return _Forecast(
        academic_year=academic_year,
        term=term,
        timetable=timetable,
        sections=sections,
        fingerprint=feedback.forecast_fingerprint(
            academic_year=academic_year,
            term=term,
            forecast_class=timetable.forecast_class,
            sections=sections,
        ),
        clashing=clashing,
    )


def _anchor_of(raw: object, forecast: _Forecast) -> tuple[str, Any] | None:
    """('SECTION', section) or ('TIMETABLE', None) for a choice the form offered, else
    None. A section without a label is part of the forecast but cannot be chosen."""
    value = str(raw or "").strip()
    if value == "TIMETABLE":
        return ("TIMETABLE", None) if forecast.sections else None
    kind, sep, ident = value.partition("|")
    if kind != "SECTION" or not sep or not ident.isdecimal():
        return None
    for section in forecast.sections:
        if section.term_section_id == int(ident) and section.section:
            return ("SECTION", section)
    return None


def _section_choice(section: feedback.SeenSection, forecast: _Forecast, language: str) -> dict:
    return {
        "anchor": f"SECTION|{section.term_section_id}",
        "course_key": section.course_key,
        "name": section.displayed_course_name,
        "section": section.section,
        "meetings": _meetings(section.meetings, language),
        "clashes": section.term_section_id in forecast.clashing,
    }


def _alternative(term_section: TermSection, language: str) -> dict:
    rows = sorted(
        {
            (
                feedback.normalise_day(m.day),
                feedback.normalise_time(m.start_time),
                feedback.normalise_time(m.end_time),
                feedback.normalise_room(m.room),
            )
            for m in term_section.meetings.all()
        }
    )
    return {
        "id": term_section.pk,
        "section": term_section.section,
        "meetings": _meetings(rows, language),
    }


def _reasons(kind: str, section: Any, language: str, *, has_choices: bool) -> list[dict[str, str]]:
    """The issues to offer for this choice. An issue the student could never send is
    left out: one that names a section or a course when there is none to name."""
    allowed = feedback.REASONS_BY_ANCHOR[kind]
    codes = [code for code in REASON_ORDER[kind] if code in allowed]
    if section is not None and section.meetings:
        # Offered only for a section that really has no times; the service refuses
        # it for one that has them.
        codes = [code for code in codes if code != "SECTION_HAS_NO_TIMES"]
    if not has_choices:
        codes = [code for code in codes if code not in NEEDS_A_CHOICE]
    return [{"code": code, "label": copy.reason_label(code, language)} for code in codes]


def _render_form(
    request: HttpRequest,
    *,
    language: str,
    student_id: int,
    programme: str,
    forecast: _Forecast,
    anchor: tuple[str, Any] | None,
    values: dict[str, str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    status: int = 200,
) -> HttpResponse:
    values = values or {}
    errors = errors or []
    for index, error in enumerate(errors, start=1):
        error["id"] = f"ttfError-{index}"
    t = copy.strings(language)
    term = copy.term_html(forecast.academic_year, forecast.term)
    max_chars = copy.bdi(feedback.NOTE_MAX_CHARS)
    extra: dict[str, Any] = {
        "values": values,
        "errors": errors,
        # Each field shows its own error and names it through aria-describedby.
        "error_for": {error["field"]: error for error in errors if error.get("field")},
        "fingerprint": forecast.fingerprint,
        # Only beside a form: with no forecast there is nothing for it to qualify.
        "registered_notice": (
            copy.text("registered_notice", language, term=term)
            if forecast.timetable.has_registration and forecast.sections
            else ""
        ),
        "note_hint": copy.text("new_note_hint", language, max=max_chars),
        "note_max": feedback.NOTE_MAX_CHARS,
    }
    if not forecast.sections:
        if forecast.timetable.has_registration:
            extra["mode"] = "registered_only"
            extra["registered_only"] = copy.text("new_registered_only", language, term=term)
        else:
            extra["mode"] = "no_forecast"
            extra["no_forecast_body"] = copy.text("new_no_forecast_body", language, term=term)
            # There is no issue to choose here, so the hint names none.
            extra["note_hint"] = copy.text("new_note_hint_plain", language, max=max_chars)
    elif anchor is None:
        extra["mode"] = "choose"
        extra["choices"] = [
            _section_choice(section, forecast, language)
            for section in forecast.sections
            if section.section
        ]
    else:
        kind, section = anchor
        extra["mode"] = "describe"
        extra["anchor_kind"] = kind
        if kind == "SECTION":
            extra["anchor_value"] = f"SECTION|{section.term_section_id}"
            extra["about"] = _section_choice(section, forecast, language)
            extra["heading"] = copy.text(
                "new_about_section",
                language,
                course=copy.bdi(section.course_key),
                section=copy.bdi(section.section),
            )
            extra["alternatives"] = [
                _alternative(term_section, language)
                for term_section in feedback.alternative_sections_for(
                    student_id, programme, section
                )
            ]
            choices = extra["alternatives"]
        else:
            extra["anchor_value"] = "TIMETABLE"
            extra["heading"] = t["new_about_timetable"]
            extra["candidates"] = feedback.missing_course_candidates(
                student_id,
                programme,
                forecast.timetable,
                feedback.catalogue_sections_for(student_id, programme),
            )
            choices = extra["candidates"]
        extra["reasons"] = _reasons(kind, section, language, has_choices=bool(choices))
    return render(
        request,
        "core/student_timetable_feedback_new.html",
        _context(request, language, **extra),
        status=status,
    )


def _error(field: str, html: object) -> dict[str, Any]:
    return {"field": field, "html": html}


#: Which field a refusal is about, so it can be marked on the form. NOT_OFFERED is
#: deliberately absent: like its sentence, it must not say which choice failed.
_REFUSAL_FIELD = {
    "REASON_DOES_NOT_FIT": "reason",
    "NOTE_REQUIRED": "note",
    "NOTE_TOO_LONG": "note",
    "NOTE_INVALID": "note",
}


@never_cache
@require_http_methods(["GET", "POST"])
def feedback_new_view(request: HttpRequest) -> HttpResponse:
    language = copy.language_of(getattr(request, "LANGUAGE_CODE", ""))
    student_id = _student(request)
    if isinstance(student_id, HttpResponse):
        return student_id
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return _unavailable(request, language, status=404)

    student = Student.objects.filter(student_id=student_id).first()
    if student is None:
        return _unavailable(
            request, language, status=404, message=copy.refusal("UNKNOWN_STUDENT", language)
        )
    programme = str(student.program or "")
    if not feedback.accepting_for(programme):
        return _unavailable(
            request, language, status=403, message=copy.refusal("NOT_ACCEPTING", language)
        )
    try:
        forecast = _read_forecast(student_id, programme, language)
    except TimetableUnavailable:
        logger.exception("timetable feedback form could not read %s", student_id)
        return _unavailable(
            request,
            language,
            status=503,
            message=copy.refusal("TIMETABLE_UNAVAILABLE", language),
        )

    form: dict[str, Any] = {
        "language": language,
        "student_id": student_id,
        "programme": programme,
    }
    if request.method == "POST":
        # The service decides, under its lock; a refusal comes back with the form.
        return _submit(request, forecast=forecast, **form)

    # A point the service would refuse is refused before it is written, not after.
    open_case = _open_case_for(student_id, forecast.academic_year, forecast.term)
    _, refusal = _next_point(open_case, student_id, forecast.academic_year, forecast.term)
    if refusal:
        return _unavailable(
            request, language, status=409, message=_why_not(refusal, language, open_case)
        )

    about = request.GET.get("about")
    if not forecast.sections:
        return _render_form(request, forecast=forecast, anchor=None, **form)
    if about is None:
        if request.GET.get("step") != "choose":
            return _render_form(request, forecast=forecast, anchor=None, **form)
        # Continue pressed with nothing chosen: a radio group sends nothing at all,
        # so only the form's own marker tells this apart from a first visit.
        return _render_form(
            request,
            forecast=forecast,
            anchor=None,
            errors=[_error("about", copy.text("new_about_required", language))],
            status=400,
            **form,
        )
    anchor = _anchor_of(about, forecast)
    if anchor is None:
        return _render_form(
            request,
            forecast=forecast,
            anchor=None,
            errors=[_error("about", copy.text("new_about_required", language))],
            status=400,
            **form,
        )
    return _render_form(request, forecast=forecast, anchor=anchor, **form)


def _submit(
    request: HttpRequest,
    *,
    language: str,
    student_id: int,
    programme: str,
    forecast: _Forecast,
) -> HttpResponse:
    data = request.POST
    raw_anchor = str(data.get("anchor") or "")
    values = {
        "reason": str(data.get("reason") or ""),
        "requested_section": str(data.get("requested_section") or ""),
        "requested_course": str(data.get("requested_course") or ""),
        "note": _normalise_newlines(data.get("note")),
    }
    # The form without JavaScript shows every optional list at once; only what the
    # chosen issue uses is sent on, so a leftover choice cannot refuse the point.
    if values["reason"] not in feedback.SECTION_REQUEST_REASONS:
        values["requested_section"] = ""
    if values["reason"] != "MISSING_COURSE":
        values["requested_course"] = ""

    is_no_forecast = raw_anchor == "NO_FORECAST"
    anchor = None if is_no_forecast else _anchor_of(raw_anchor, forecast)
    form: dict[str, Any] = {"language": language, "student_id": student_id, "programme": programme}
    if not is_no_forecast:
        if anchor is None:
            # The choice is gone from the forecast (or never was in it).
            return _render_form(
                request,
                forecast=forecast,
                anchor=None,
                errors=[_error("about", copy.refusal("UNKNOWN_ANCHOR", language))],
                status=400,
                **form,
            )
        errors = []
        if not values["reason"]:
            errors.append(_error("reason", copy.text("new_reason_required", language)))
        elif values["reason"] == "PREFER_OTHER_SECTION" and not values["requested_section"]:
            errors.append(
                _error("requested_section", copy.text("new_requested_required", language))
            )
        elif values["reason"] == "MISSING_COURSE" and not values["requested_course"]:
            errors.append(_error("requested_course", copy.text("new_missing_required", language)))
        if errors:
            return _render_form(
                request,
                forecast=forecast,
                anchor=anchor,
                values=values,
                errors=errors,
                status=400,
                **form,
            )

    try:
        result = feedback.submit_point(
            student_id=student_id,
            anchor="NO_FORECAST" if is_no_forecast else raw_anchor.strip(),
            reason="NO_FORECAST" if is_no_forecast else values["reason"],
            seen_fingerprint=str(data.get("seen_fingerprint") or ""),
            note=values["note"],
            requested_term_section_id=values["requested_section"] or None,
            requested_course_key=values["requested_course"],
            language=language,
        )
    except (feedback.FeedbackRefused, workflow.WorkflowRefused) as refused:
        return _refused(request, refused, forecast=forecast, anchor=anchor, values=values, **form)

    code = "DUPLICATE" if result.duplicate else "REOPENED" if result.reopened else "SENT"
    messages.add_message(request, messages.SUCCESS, code, extra_tags=NOTICE_TAG)
    return redirect("student_timetable_feedback_detail", reference=result.reference)


def _refused(
    request: HttpRequest,
    refused: feedback.FeedbackRefused | workflow.WorkflowRefused,
    *,
    language: str,
    student_id: int,
    programme: str,
    forecast: _Forecast,
    anchor: tuple[str, Any] | None,
    values: dict[str, str],
) -> HttpResponse:
    """The form again, from the refusal's code and with its status."""
    code = refused.code
    # Only a submission refusal names a case (ALREADY_RAISED); a workflow one never does.
    reference = refused.reference if isinstance(refused, feedback.FeedbackRefused) else ""
    message = copy.refusal(code, language, reference=reference, retry_after=refused.retry_after)
    if code in {"NOT_ACCEPTING", "UNKNOWN_STUDENT", "TIMETABLE_UNAVAILABLE"}:
        return _unavailable(request, language, status=refused.status, message=message)
    form: dict[str, Any] = {"language": language, "student_id": student_id, "programme": programme}
    if code == "TIMETABLE_CHANGED":
        # Draw the form from the timetable as it is now; the choice survives only
        # if it is still there.
        try:
            forecast = _read_forecast(student_id, programme, language)
        except TimetableUnavailable:
            logger.exception("timetable feedback form could not re-read %s", student_id)
            return _unavailable(
                request,
                language,
                status=503,
                message=copy.refusal("TIMETABLE_UNAVAILABLE", language),
            )
        if anchor is not None:
            kind, section = anchor
            anchor = _anchor_of(
                "TIMETABLE" if kind == "TIMETABLE" else f"SECTION|{section.term_section_id}",
                forecast,
            )
    error = _error(_REFUSAL_FIELD.get(code, ""), message)
    if code == "ALREADY_RAISED" and reference:
        error["link"] = {
            "url": reverse("student_timetable_feedback_detail", args=[reference]),
            "html": copy.text("new_open_case", language, reference=copy.bdi(reference)),
        }
    response = _render_form(
        request,
        forecast=forecast,
        anchor=anchor,
        values=values,
        errors=[error],
        status=refused.status,
        **form,
    )
    if refused.status == 429:
        # The limiter's own number, never a guess.
        response["Retry-After"] = str(refused.retry_after)
    return response


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def _point_view(point: Any, language: str) -> dict[str, Any]:
    t = copy.strings(language)
    about: str
    if point.anchor_kind == "SECTION":
        seen = next(
            (
                entry
                for entry in (point.seen_snapshot or {}).get("sections", [])
                if entry.get("term_section_id") == point.term_section_id
            ),
            None,
        )
        course = seen.get("course_key") if seen else point.course_key
        label = seen.get("section") if seen else point.section_label
        about = copy.text(
            "detail_section", language, course=copy.bdi(course), section=copy.bdi(label)
        )
        name = (seen or {}).get("displayed_course_name") or ""
        meetings = _meetings((seen or {}).get("meetings") or [], language)
    elif point.anchor_kind == "TIMETABLE":
        about, name, meetings = t["detail_whole_timetable"], "", []
    else:
        about, name, meetings = t["detail_no_forecast"], "", []
    return {
        "id": str(point.pk),
        "about": about,
        "name": name,
        "meetings": meetings,
        "reason": copy.reason_label(point.reason_code, language),
        "requested_section": point.requested_section_label,
        "requested_course": point.requested_course_key,
        "note": point.note,
        "note_removed": point.note_redacted_at is not None,
        "withdrawn": point.withdrawn_at is not None,
        "sent": _date(point.created_at),
    }


def _own_case(
    request: HttpRequest, language: str, student_id: int, reference: object
) -> TimetableFeedback | HttpResponse:
    """The student's case by its reference, or the not-found page. Ownership is
    part of the query: another student's reference is simply not found."""
    wanted = workflow.case_reference(reference)
    case = (
        TimetableFeedback.objects.filter(student_id=student_id, reference=wanted).first()
        if wanted
        else None
    )
    if case is None:
        return _unavailable(
            request, language, status=404, message=copy.refusal("CASE_NOT_FOUND", language)
        )
    return case


@never_cache
@require_GET
def feedback_detail_view(request: HttpRequest, reference: str) -> HttpResponse:
    language = copy.language_of(getattr(request, "LANGUAGE_CODE", ""))
    student_id = _student(request)
    if isinstance(student_id, HttpResponse):
        return student_id
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return _unavailable(request, language, status=404)
    case = _own_case(request, language, student_id, reference)
    if isinstance(case, HttpResponse):
        return case

    notice = ""
    for message in messages.get_messages(request):
        if NOTICE_TAG in message.tags and message.message in copy.NOTICE:
            notice = copy.notice(message.message, language, reference=case.reference)
    # Opening the case is seeing it as it was just read. Marked before the page is
    # drawn, so the sidebar on this page no longer counts it as new.
    workflow.mark_seen(case)
    return case_page(request, case, student_id=student_id, language=language, notice=notice)


#: The student's actions a case page may offer, by the service's own names.
STUDENT_ACTIONS = (
    "reply",
    "follow_up",
    "withdraw_point",
    "withdraw_case",
    "confirm_answer",
    "reopen_answer",
)
#: Refusals worth a sentence where the action would be: a limit reached, or a time
#: run out. NOT_ALLOWED only means the action does not belong to this status.
_SAID_WHERE_WITHHELD = frozenset({"TOO_MANY_FOLLOW_UPS", "REOPEN_WINDOW_PASSED"})


def case_page(
    request: HttpRequest,
    case: TimetableFeedback,
    *,
    student_id: int,
    language: str,
    notice: object = "",
    errors: list[dict[str, Any]] | None = None,
    values: dict[str, str] | None = None,
    status: int = 200,
) -> HttpResponse:
    """One case as the student sees it, with what they can do on it now.

    The GET draws it, and so does an action the service refused: with the
    refusal's status, its sentence at the top and at the field it is about, and the
    text the student typed kept. Each action is offered only where the service's
    own check says it would be taken."""
    now = timezone.now()
    errors = list(errors or [])
    for index, error in enumerate(errors):
        error["id"] = f"ttfError-{index}"
    refusals = {
        action: workflow.student_action_refusal(case, action, now=now) for action in STUDENT_ACTIONS
    }

    def offered(action: str) -> bool:
        return refusals[action] is None

    def why_withheld(action: str) -> object:
        refusal = refusals[action]
        if refusal is None or refusal.code not in _SAID_WHERE_WITHHELD:
            return ""
        return copy.refusal(refusal.code, language)

    text_max = copy.bdi(workflow.STUDENT_TEXT_MAX_CHARS)
    answer: dict[str, object] | None = None
    if case.status == TimetableFeedback.Status.ANSWERED:
        answer = {
            "open": (
                copy.text(
                    "detail_answer_open",
                    language,
                    date=copy.bdi(_date(workflow.reopen_until(case))),
                )
                if offered("reopen_answer")
                else ""
            ),
            "reopen_withheld": why_withheld("reopen_answer"),
        }

    # Only what a student may read is fetched at all.
    thread = [
        {
            "author": copy.author_label(row["author_kind"], language),
            "mine": row["author_kind"] == TimetableFeedbackMessage.AuthorKind.STUDENT,
            "body": row["body"],
            "sent": _date(row["created_at"]),
            "corrected": row["corrected_by_message_id"] is not None,
            "removed": row["redacted_at"] is not None,
        }
        for row in TimetableFeedbackMessage.objects.filter(
            case=case, audience=TimetableFeedbackMessage.Audience.STUDENT_VISIBLE
        )
        .order_by("created_at")
        .values("author_kind", "body", "created_at", "corrected_by_message_id", "redacted_at")
    ]
    points = [_point_view(point, language) for point in case.points.order_by("created_at")]

    planning = feedback.planning_term()
    is_planning_term = (str(case.academic_year), str(case.term)) == (
        str(planning[0]),
        str(planning[1]),
    )
    student = Student.objects.filter(student_id=student_id).first()
    # Only this term's open case is offered anything, and only while feedback is
    # taken: the next point joins it, or, past its answer's window, opens a new case.
    can_add = can_send_new = False
    send_refused: object = ""
    if (
        is_planning_term
        and case.status not in TIMETABLE_FEEDBACK_TERMINAL_STATUSES
        and student is not None
        and feedback.accepting_for(student.program)
    ):
        joins, refusal = _next_point(case, student_id, case.academic_year, case.term)
        can_add = joins and not refusal
        can_send_new = not joins and not refusal
        send_refused = _why_not(refusal, language, case)
    term = copy.term_html(case.academic_year, case.term)
    return render(
        request,
        "core/student_timetable_feedback_detail.html",
        _context(
            request,
            language,
            case={
                "reference": case.reference,
                "title": copy.text("detail_title", language, reference=copy.bdi(case.reference)),
                # A <title> shows markup literally, so it gets the same words as text.
                "page_title": copy.plain("detail_title", language, reference=case.reference),
                "term": term,
                "status_code": case.status,
                "status": copy.status_label(case.status, language),
                "explained": copy.status_explained(case.status, language),
                "close_reason": (
                    copy.close_reason_label(case.close_reason, language)
                    if case.status == TimetableFeedback.Status.CLOSED
                    else ""
                ),
                "sent": _date(case.created_at),
            },
            notice=notice,
            points=points,
            thread=thread,
            other_term=(
                "" if is_planning_term else copy.text("detail_other_term", language, term=term)
            ),
            can_add=can_add,
            can_send_new=can_send_new,
            send_refused=send_refused,
            # The student's own actions on the case.
            can_reply=offered("reply"),
            can_follow_up=offered("follow_up"),
            follow_up_withheld=why_withheld("follow_up"),
            can_withdraw_point=offered("withdraw_point"),
            can_withdraw_case=offered("withdraw_case"),
            can_confirm=offered("confirm_answer"),
            can_reopen=offered("reopen_answer"),
            answer=answer,
            reply_hint=copy.text("detail_reply_hint", language, max=text_max),
            follow_up_hint=copy.text(
                "detail_follow_up_hint",
                language,
                left=copy.bdi(workflow.MAX_FOLLOW_UPS - workflow.follow_ups_used(case)),
                max=text_max,
            ),
            reopen_hint=copy.text("detail_reopen_hint", language, max=text_max),
            text_max=workflow.STUDENT_TEXT_MAX_CHARS,
            errors=errors,
            error_for={error["field"]: error for error in errors if error.get("field")},
            values=values or {},
        ),
        status=status,
    )


# ---------------------------------------------------------------------------
# The entry on the home page
# ---------------------------------------------------------------------------


def home_feedback_entry(
    *,
    student: Student | None,
    student_id: int,
    academic_year: object,
    term: object,
    timetable: StudentTermTimetable,
    read_failed: bool,
    language: str,
) -> dict[str, Any] | None:
    """What the home page shows about feedback, for its own configured term.

    The home page passes the term it shows, so the button sits on the card it is
    about. Nothing is offered when the forecast could not be read: a failed read is
    not "no timetable was prepared".
    """
    if not settings.TIMETABLE_FEEDBACK_ENABLED:
        return None
    language = copy.language_of(language)
    accepting = student is not None and feedback.accepting_for(student.program)
    # This term's cases, newest first (three at most): one read for the card and
    # the banner. The open one is the case a new point is offered to.
    this_term = list(
        TimetableFeedback.objects.filter(
            student_id=student_id, academic_year=str(academic_year), term=str(term)
        ).order_by("-created_at")
    )
    open_case = next(
        (case for case in this_term if case.status not in TIMETABLE_FEEDBACK_TERMINAL_STATUSES),
        None,
    )
    term_html = copy.term_html(academic_year, term)
    # Both cards that send ask first where the next point would go, and whether
    # the service would take it.
    may_send = accepting and not read_failed
    joins, refusal = (
        _next_point(open_case, student_id, academic_year, term) if may_send else (False, "")
    )
    can_raise = may_send and timetable.has_forecast
    entry: dict[str, Any] = {
        "list_url": reverse("student_timetable_feedback_list"),
        "new_url": reverse("student_timetable_feedback_new"),
        "can_raise": can_raise,
        # A first case, where nothing is open yet.
        "can_send": can_raise and open_case is None and not refusal,
        # Where a send button would be, the reason there is none.
        "send_refused": _why_not(refusal, language, open_case),
        # Only where feedback can be sent: while it is closed, "it still reaches your
        # academic advisor" would be untrue.
        "registered_notice": (
            copy.text("registered_notice", language, term=term_html)
            if can_raise and timetable.has_registration
            else ""
        ),
        # Offered once: after it is sent, the open case is shown instead. Not offered
        # at all when the service would refuse a new case: its body says the student
        # can tell their advisor, and the list says why they cannot.
        "no_forecast": (
            may_send
            and not timetable.has_forecast
            and not timetable.has_registration
            and open_case is None
            and not refusal
        ),
        # Without this term's expected card the open case gets a card of its own.
        "on_expected_card": timetable.has_forecast,
        "t": copy.strings(language),
        "open_case": None,
    }
    if entry["no_forecast"]:
        entry["no_forecast_body"] = copy.text("no_forecast_body", language, term=term_html)
    if open_case is not None:
        entry["open_case"] = {
            "url": reverse("student_timetable_feedback_detail", args=[open_case.reference]),
            "label": copy.text("home_view_case", language, reference=copy.bdi(open_case.reference)),
            "status": copy.text(
                "home_case_status",
                language,
                status=copy.status_label(open_case.status, language),
            ),
            "can_add": can_raise and joins and not refusal,
            # A point sent now would not join an answer past its window: it closes
            # that case and opens a new one, so the card offers "send", not "add".
            "can_send_new": can_raise and not joins and not refusal,
            "unread": workflow.has_unread(open_case),
        }
    # Feedback already sent goes on, whatever the flags say about sending more.
    entry["banner"] = _banner(this_term, language)
    return entry


def _banner(cases: list[TimetableFeedback], language: str) -> dict[str, object] | None:
    """The one thing about this term's feedback the home page leads with, or None.

    First a case that waits on the student (a request for information, or an
    answer they can still accept or reopen); then one with something they have
    not seen. A finished case counts too: its closing may be the news."""
    now = timezone.now()
    chosen = next(
        (
            (
                case,
                "reply" if case.status == TimetableFeedback.Status.NEEDS_INFORMATION else "answer",
            )
            for case in cases
            if workflow.waits_on_student(case, now)
        ),
        None,
    ) or next(((case, "news") for case in cases if workflow.has_unread(case)), None)
    if chosen is None:
        return None
    case, kind = chosen
    reference = copy.bdi(case.reference)
    details: dict[str, object] = {"reference": reference}
    if kind == "answer":
        details["date"] = copy.bdi(_date(workflow.reopen_until(case)))
    return {
        "kind": kind,
        "title": copy.text(f"banner_{kind}_title", language),
        "body": copy.text(f"banner_{kind}_body", language, **details),
        "url": reverse("student_timetable_feedback_detail", args=[case.reference]),
        "link": copy.text("home_view_case", language, reference=reference),
    }
