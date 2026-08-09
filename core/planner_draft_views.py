"""The student's own planner: five endpoints over one draft.

Every one of them resolves the student from the session and puts that id in the
same query that fetches the draft, so a draft belonging to someone else is not
found rather than found-and-refused.

Nothing here decides anything. The lifecycle — what an edit invalidates, when a
rebuild needs confirming, whether a generation may be reused — lives in
`services.planner_drafts`, because those rules have to hold no matter which door
the request came through. These views parse, spend a budget, call it, and shape
the reply.

**What goes on the wire.** Course code and name, section label, day, start and
end. Not rooms, not instructors, not registered counts, not the solver baseline,
and not the fingerprint. Section ids cross only for the explicit pin control and
are re-authorised against the signed-in student's cohort when posted back.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .advisor_http import forbidden as _forbidden
from .advisor_http import json_body as _body
from .advisor_http import over_budget as _over_budget
from .advisor_http import student_principal as _principal
from .services.planner_drafts import (
    ConfirmationRequired,
    DraftConflict,
    DraftError,
    DraftExpired,
    DraftRejected,
    create_draft,
    credit_ceiling,
    edit_draft,
    generate,
    generation_is_stale,
    issue_rebuild_token,
    owned_draft,
)
from .services.rate_limit import CONVERSATION, HISTORY, PLANNING
from .services.rate_limit import release as _refund_budget
from .services.student_planner import PlannerUnavailable
from .sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


def _names(codes: set[str]) -> dict[str, str]:
    """Course titles, from the resolver the adviser already uses.

    It searches four places — the course table, the programme plan, the elective
    catalogue and the live offerings — because a resolved elective can exist in
    only the last of them. A second, simpler lookup here would show a bare code
    for exactly the courses an elective slot was opened to offer.
    """
    from .services.virtual_advisor import _course_names

    return _course_names(codes)


def _workspace_json(draft: Any) -> dict[str, Any]:
    """Student-safe facts needed to build a proposal on screen.

    This is deliberately not the staff planner catalogue.  Identity and term come
    from the owned draft; sections are cohort-filtered server-side; and rooms,
    instructors, capacity, enrolment counts and internal source fields never cross
    the HTTP boundary.
    """
    from core.models import Course, Prerequisite, ProgrammeRequirement, Student, TermSection
    from core.services.student_helpers import get_student_passed_and_studying, normalize_code
    from core.services.student_planner import DEFAULT_CREDITS, permitted_course_codes
    from core.services.student_sections import (
        gender_section_filter,
        get_student_term_baseline,
        student_gender_strict,
    )

    student = Student.objects.filter(student_id=draft.student_id).values("program").first() or {}
    program = str(student.get("program") or "").strip()
    permitted = permitted_course_codes(program) if program else set()
    passed, studying = get_student_passed_and_studying(draft.student_id)
    passed = {normalize_code(code) for code in passed}
    studying = {normalize_code(code) for code in studying}

    requirements = list(
        ProgrammeRequirement.objects.filter(program__iexact=program).values(
            "course_code", "course_name", "credit_hours"
        )
    )
    requirement_by_code = {
        normalize_code(row.get("course_code") or ""): row for row in requirements
    }
    course_by_code = {
        normalize_code(row.course_code): row
        for row in Course.objects.filter(course_code__in=permitted)
    }

    prerequisites: dict[str, list[str]] = {}
    for row in Prerequisite.objects.filter(program__iexact=program).values(
        "course_code", "prerequisite_course_code"
    ):
        code = normalize_code(row.get("course_code") or "")
        for raw in str(row.get("prerequisite_course_code") or "").split(","):
            prerequisite = normalize_code(raw)
            if code and prerequisite:
                prerequisites.setdefault(code, []).append(prerequisite)

    gender = student_gender_strict(draft.student_id)
    sections_by_code: dict[str, list[Any]] = {}
    sections = list(
        TermSection.objects.filter(scenario__isnull=True)
        .filter(gender_section_filter(gender))
        .prefetch_related("meetings")
        .order_by("course_key", "section")
    )
    for section in sections:
        code = normalize_code(
            section.course_key or f"{section.course_code or ''}{section.course_number or ''}"
        )
        if code in permitted:
            sections_by_code.setdefault(code, []).append(section)

    try:
        recommended = set(_recommended_codes(draft.student_id))
    except Exception:
        recommended = set()
    requested = {normalize_code(code) for code in (draft.course_codes or [])}
    names = _names(permitted | requested | recommended)

    catalog: list[dict[str, Any]] = []
    for code in permitted | requested | recommended:
        code = normalize_code(code)
        if not code or code in passed:
            continue
        requirement = requirement_by_code.get(code) or {}
        course = course_by_code.get(code)
        missing = sorted(
            {
                item
                for item in prerequisites.get(code, [])
                if item not in passed and item not in studying
            }
        )
        safe_sections = []
        for section in sections_by_code.get(code, []):
            meetings = [
                {
                    "day": str(meeting.day or ""),
                    "start": str(meeting.start_time or ""),
                    "end": str(meeting.end_time or ""),
                }
                for meeting in sorted(
                    section.meetings.all(),
                    key=lambda item: (item.day or "", item.start_time or ""),
                )
                if meeting.day and meeting.start_time and meeting.end_time
            ]
            # A section with no complete recorded interval cannot support the
            # screen's only scheduling guarantee: checking time overlap.  It may
            # still exist in the source catalogue, but it is not a schedulable
            # option here until its meeting data is complete.
            if not meetings:
                continue
            safe_sections.append(
                {
                    "id": int(section.id),
                    "label": str(section.section or ""),
                    "meetings": meetings,
                }
            )
        credits = int(
            requirement.get("credit_hours")
            or (getattr(course, "credit_hours", 0) if course else 0)
            or DEFAULT_CREDITS
        )
        catalog.append(
            {
                "course_code": code,
                "course_name": str(
                    names.get(code)
                    or requirement.get("course_name")
                    or (getattr(course, "description", "") if course else "")
                    or ""
                ),
                "credits": credits,
                "recommended": code in recommended,
                "studying": code in studying,
                "status": "blocked"
                if missing
                else ("offering_unknown" if not safe_sections else "ready"),
                "missing_prerequisites": missing,
                "sections": safe_sections,
            }
        )
    catalog.sort(
        key=lambda row: (
            not bool(row["recommended"]),
            row["status"] != "ready",
            str(row["course_code"]),
        )
    )

    baseline = get_student_term_baseline(draft.student_id, draft.academic_year, draft.term)
    current = [
        {
            "course_code": str(row.get("course_code") or row.get("course_key") or ""),
            "course_name": str(row.get("course_name") or ""),
            "section": str(row.get("section") or ""),
            "credits": int(row.get("credits") or 0),
            "day": str(row.get("day") or ""),
            "start": str(row.get("start_time") or ""),
            "end": str(row.get("end_time") or ""),
        }
        for row in baseline
    ]
    return {
        "program": program,
        "credit_ceiling": credit_ceiling(int(draft.term)),
        "catalog": catalog,
        "current_timetable": current,
        # `TermSection` is a current recorded catalogue without term columns.
        # Never let a planning-term label turn that into a claim that the section
        # is offered in that term.
        "section_catalog_term_known": False,
        "clash_check_scope": "recorded_complete_meeting_times",
        "workspace_persistence": "temporary_draft",
        "registration_action": "student_manual_portal_only",
        "can_save_timetable": False,
        "can_register_courses": False,
    }


def _alternative_json(
    alternative: dict[str, Any], names: dict[str, str], selected: str, requested: set[str]
) -> dict[str, Any]:
    option_unplaced = [
        {
            "course_code": str(item.get("course_code") or ""),
            "course_name": names.get(str(item.get("course_code") or ""), ""),
            "reason": UNPLACED_AR.get(str(item.get("reason_code") or ""), UNPLACED_AR_DEFAULT),
        }
        for item in (alternative.get("unplaced") or [])
    ]
    scheduled = int(alternative.get("scheduled_courses") or 0)
    target = int(alternative.get("target_courses") or 0)
    return {
        "key": alternative.get("key", ""),
        # A V2 timetable is an on-screen proposal, not a stored preference.
        # Keep the field false for compatibility with the existing response shape.
        "selected": False,
        "credit_hours": alternative.get("credit_hours", 0),
        "course_count": alternative.get("course_count", 0),
        "days_on_campus": alternative.get("days_on_campus", 0),
        "days": list(alternative.get("days") or []),
        "earliest_start": alternative.get("earliest_start"),
        "latest_end": alternative.get("latest_end"),
        # The exact planner identities and its coverage statement are part of the
        # answer, not operator trivia.  Dropping them made a 3/5 result look like a
        # complete anonymous timetable in the browser.
        "planner_options": [str(name) for name in (alternative.get("planner_options") or [])],
        "scheduled_courses": scheduled,
        "target_courses": target,
        "complete": bool(target and scheduled >= target),
        "unplaced": option_unplaced,
        "courses": [
            {
                "course_code": c.get("course_code", ""),
                "course_name": names.get(c.get("course_code", ""), ""),
                "section": c.get("section", ""),
                # The builder fills the term from the student's own plan, so an
                # alternative routinely contains courses nobody asked for. Marked,
                # because a course the student never named must not be presented as
                # though they did.
                "requested": str(c.get("course_code") or "") in requested,
                "source": "current" if c.get("source") == "current" else "proposed",
                # `term_section_id` is deliberately NOT here. It was carried for a
                # pin affordance this screen does not yet have — so it was a raw
                # primary key, per course, per alternative, shipped for nothing.
                # It comes back when something reads it.
            }
            for c in alternative.get("courses", [])
        ],
        "meetings": [
            {
                "course_code": m.get("course_code", ""),
                "course_name": names.get(m.get("course_code", ""), ""),
                "section": m.get("section", ""),
                "day": m.get("day", ""),
                "start": m.get("start", ""),
                "end": m.get("end", ""),
                "source": "current" if m.get("source") == "current" else "proposed",
            }
            for m in alternative.get("meetings", [])
        ],
    }


def _draft_json(draft: Any) -> dict[str, Any]:
    codes = list(draft.course_codes or [])
    pins = dict(draft.fixed_sections or {})
    inputs = draft.generated_inputs or {}
    unplaced = list(inputs.get("unplaced") or []) if draft.has_current_generation else []
    names = _names(
        {c for c in codes if c}
        | {str(u.get("course_code") or "") for u in unplaced}
        | {
            str(c.get("course_code") or "")
            for a in (draft.alternatives or [])
            for c in a.get("courses", [])
        }
    )

    return {
        "draft": {
            "id": str(draft.id),
            "version": draft.version,
            "academic_year": draft.academic_year,
            "term": draft.term,
            "keep_current_sections": draft.keep_current_sections,
            # The screen asks for confirmation; the server decides whether it has
            # one. A client that skips the dialog gets refused at generate.
            "needs_confirmation": not draft.keep_current_sections,
            "requested": [
                {
                    "course_code": code,
                    "course_name": names.get(code, ""),
                    "fixed_section_id": pins.get(code),
                }
                for code in codes
            ],
            "expires_at": draft.expires_at.isoformat(),
            "is_live": draft.is_live,
            "generated_at": (draft.generated_at.isoformat() if draft.generated_at else None),
            "has_current_generation": draft.has_current_generation,
            # The version catches the student changing their own mind. This catches
            # the other thing that invalidates a timetable — their registrations
            # moving underneath it — which no amount of version bumping can see.
            "is_stale": generation_is_stale(draft),
            "selected_alternative": "",
        },
        "alternatives": (
            [
                _alternative_json(a, names, draft.selected_alternative, set(codes))
                for a in draft.alternatives
            ]
            if draft.has_current_generation
            # Alternatives generated from inputs the student has since changed
            # describe a different question. They are withheld rather than shown
            # with a caveat nobody reads.
            else []
        ),
        # Why a requested course is missing, in the student's own language, from the
        # code rather than from the solver's own sentence. The machine-readable code
        # goes no further than here.
        "unplaced": [
            {
                "course_code": str(u.get("course_code") or ""),
                "course_name": names.get(str(u.get("course_code") or ""), ""),
                "reason": UNPLACED_AR.get(str(u.get("reason_code") or ""), UNPLACED_AR_DEFAULT),
            }
            for u in unplaced
        ],
        "workspace": _workspace_json(draft),
    }


#: Why a course could not be placed, said in Arabic, keyed on the CODE.
#:
#: Not on the accompanying English sentence, and never on the solver's own string.
#: `_translate_unplaced` falls through to `("OTHER", text)`, so an unrecognised
#: reason returns the builder's internal wording — "No candidate sections after
#: hard filters" — which would land on an Arabic page under an Arabic heading.
#: A closed vocabulary in, one sentence out, and an unknown code says only what is
#: actually known.
UNPLACED_AR: dict[str, str] = {
    "NOT_ON_FILE": "لا توجد شُعب مسجَّلة لهذا المقرر في بياناتنا. راجع بوابة التسجيل للتأكد.",
    "ALL_SECTIONS_CLASH": "كل الشُعب المسجّلة في بياناتنا لهذا المقرر تتعارض مع بقية جدولك.",
    "OMITTED_IN_THIS_VARIANT": "لم يضع هذا الخيار المقرر؛ قارنه بخيار آخر قد يتضمنه.",
    "PREREQUISITES": "لم تُستوفَ متطلبات هذا المقرر السابقة بعد.",
    "DID_NOT_FIT": "لم يتّسع له الجدول مع بقية المقررات ضمن الحدود المتاحة.",
}
UNPLACED_AR_DEFAULT = "تعذّر وضع هذا المقرر في الجدول."


def _refused(exc: Exception, status: int = 409) -> JsonResponse:
    return JsonResponse({"error": str(exc)}, status=status)


def _owned_message(raw: Any, student_id: int) -> Any:
    """The student's own turn, or nothing.

    Parsed before it is queried. `AdvisorMessage.id` is a UUID primary key, so
    `filter(pk="abc")` raises `ValidationError` — a 500 on a value a client picked,
    where the sibling lookup in `owned_draft` correctly answers "nothing here".
    """
    if not raw:
        return None
    import uuid as _uuid

    from .models import AdvisorMessage

    try:
        parsed = _uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
    return AdvisorMessage.objects.filter(pk=parsed, conversation__student_id=student_id).first()


# ── endpoints ────────────────────────────────────────────────────


@require_POST
def draft_create_view(request: HttpRequest) -> JsonResponse:
    """The hand-off from chat: make a draft, answer with a link to it.

    The reply is an id and a path. Not the courses, not the sections, not the
    timetable — the planner reads all of that back from the row under the student's
    own principal, so what the address bar carries cannot decide what the planner
    acts on.

    `course_codes` is OPTIONAL, and that is the whole point of the hand-off. Omit
    it and the server recomputes the student's own recommendation; the browser
    never has to scrape course codes out of an Arabic answer, and no eligibility
    rule ends up half-implemented in JavaScript. Supply it — «أريد تسجيل CS113» —
    and every code is still checked against what this student may actually take.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    payload, err = _body(request)
    if err:
        return err
    over = _over_budget(CONVERSATION, principal.student_id)
    if over:
        return over

    codes = payload.get("course_codes")
    if codes is None:
        codes = _recommended_codes(principal.student_id)
    elif not isinstance(codes, list):
        return JsonResponse({"error": "course_codes must be a list."}, status=400)

    # A draft may point back at the turn that produced it, but only at one this
    # student owns — the id is checked against their own messages, in the query.
    source = _owned_message(payload.get("source_message_id"), principal.student_id)

    try:
        draft = create_draft(
            student_id=principal.student_id,
            course_codes=codes,
            keep_current_sections=bool(payload.get("keep_current_sections", True)),
            source_message=source,
        )
    except DraftRejected as exc:
        return _refused(exc, status=400)
    except PlannerUnavailable as exc:
        # No programme on file, or an unresolvable cohort. A real answer, not a
        # crash: the student is told what is missing rather than shown a 500.
        return _refused(exc, status=409)

    return JsonResponse(
        {
            "draft_id": str(draft.id),
            "url": reverse("student_planner_page", args=[str(draft.id)]),
        },
        status=201,
    )


def _recommended_codes(student_id: int) -> list[str]:
    """What this student's own plan says comes next, recomputed here.

    Asked of the same function the adviser's recommendation capability uses, so
    the planner and the chat cannot come to different conclusions about the same
    student in the same minute.
    """
    from .services.planner_drafts import planning_term
    from .services.recommender import recommend_next_courses

    year, term = planning_term()
    return list(recommend_next_courses(int(student_id), int(year), int(term)) or [])


@require_GET
def draft_detail_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    over = _over_budget(HISTORY, principal.student_id)
    if over:
        return over
    draft = owned_draft(principal.student_id, draft_id)
    return JsonResponse(_draft_json(draft))


@require_POST
def draft_edit_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    """Change the selection. THE only writer of course codes, pins and the toggle."""
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    payload, err = _body(request)
    if err:
        return err
    over = _over_budget(CONVERSATION, principal.student_id)
    if over:
        return over

    draft = owned_draft(principal.student_id, draft_id)
    keep = payload.get("keep_current_sections")
    try:
        draft = edit_draft(
            draft,
            course_codes=payload.get("course_codes"),
            fixed_sections=payload.get("fixed_sections"),
            keep_current_sections=None if keep is None else bool(keep),
        )
    except DraftRejected as exc:
        # The student named a course they may not take, or a section not open to
        # them. That is a bad request, not a conflict.
        return _refused(exc, status=400)
    except DraftExpired as exc:
        return _refused(exc, status=410)
    except DraftConflict as exc:
        # Another tab moved the draft between our read and our write. 409 with the
        # reason, so the screen reloads instead of silently overwriting.
        return _refused(exc, status=409)
    return JsonResponse(_draft_json(draft))


@require_POST
def draft_confirm_rebuild_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    """Issue the one-use permission to discard the student's current sections."""
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    over = _over_budget(CONVERSATION, principal.student_id)
    if over:
        return over

    draft = owned_draft(principal.student_id, draft_id)
    try:
        token = issue_rebuild_token(draft)
    except DraftExpired as exc:
        return _refused(exc, status=410)
    except DraftConflict as exc:
        return _refused(exc, status=409)
    except DraftError as exc:
        return _refused(exc, status=400)
    draft.refresh_from_db()
    return JsonResponse(
        {
            "confirmation": token,
            "version": draft.version,
            # Said plainly, because this is the sentence the student is agreeing to.
            "warning": (
                "سيقترح النظام جدولًا جديدًا قد يتضمّن شُعبًا غير التي سجّلت فيها. "
                "لن يتغيّر تسجيلك الفعلي؛ هذا اقتراح فقط."
            ),
        }
    )


@require_POST
def draft_generate_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    payload, err = _body(request)
    if err:
        return err
    # The PLANNER's budget, not the adviser's. They were shared on the reasoning
    # that both "generate"; measurement says one is 0.09 s of local solver and the
    # other is a ninety-second model turn, and sharing them let a student spend
    # their questions on timetables.
    over = _over_budget(PLANNING, principal.student_id)
    if over:
        return over

    # Handed back on every path that did NOT run a solve. The budget is meant to
    # measure work done, not requests made — and the screen's own flow posts once
    # to discover it needs a confirmation, so without this a rebuild would cost two
    # of the six generations a student gets in ten minutes.
    try:
        draft = owned_draft(principal.student_id, draft_id)
    except Http404:
        _refund_budget(PLANNING, principal.student_id)
        raise

    before = draft.generated_version
    try:
        draft = generate(draft, confirmation=payload.get("confirmation"))
    except ConfirmationRequired as exc:
        _refund_budget(PLANNING, principal.student_id)
        # 428: the request is well-formed and the student is entitled to make it;
        # what is missing is the confirmation.
        return JsonResponse({"error": str(exc), "needs_confirmation": True}, status=428)
    except DraftExpired as exc:
        _refund_budget(PLANNING, principal.student_id)
        return _refused(exc, status=410)
    except DraftRejected as exc:
        _refund_budget(PLANNING, principal.student_id)
        # Revalidation at generation time: a section withdrawn since the draft was
        # made, or a course the student may no longer take.
        return _refused(exc, status=409)
    except PlannerUnavailable as exc:
        _refund_budget(PLANNING, principal.student_id)
        return _refused(exc, status=409)
    except DraftError as exc:
        _refund_budget(PLANNING, principal.student_id)
        return _refused(exc, status=409)

    if draft.generated_version == before:
        # A replay: this version was already generated, and the result came from
        # storage rather than the solver. It has been paid for once already.
        _refund_budget(PLANNING, principal.student_id)
    return JsonResponse(_draft_json(draft))


@require_POST
def draft_select_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    """Legacy endpoint retained as an explicit refusal.

    V2 proposals stay on screen.  There is intentionally no server-side notion of
    a student's chosen timetable and no path from this endpoint to registration.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    owned_draft(principal.student_id, draft_id)
    return JsonResponse(
        {
            "error": (
                "لا يحفظ هذا المخطط الجداول. انسخ قائمة المقررات والشُعب ثم "
                "أدخلها بنفسك في بوابة الجامعة الرئيسية."
            ),
            "code": "TIMETABLE_SAVE_DISABLED",
        },
        status=405,
    )


@require_GET
def student_timetable_start_view(request: HttpRequest):
    """Open a new, short-lived planning workspace for the signed-in student."""
    principal = _principal(request)
    if principal is None:
        raise PermissionDenied("This page is for signed-in students.")
    try:
        draft = create_draft(
            student_id=principal.student_id,
            course_codes=_recommended_codes(principal.student_id),
            keep_current_sections=True,
        )
    except (DraftRejected, PlannerUnavailable) as exc:
        return render(
            request,
            "core/student_planner_unavailable.html",
            {
                **get_sidebar_context(request),
                "planner_error": str(exc),
            },
            status=409,
        )
    return redirect("student_planner_page", draft_id=str(draft.id))


@require_GET
def student_planner_page(request: HttpRequest, draft_id: str):
    """The screen itself. Renders the shell; every fact on it arrives by fetch.

    The draft is looked up here too, under the same ownership filter, so someone
    else's id gives a 404 page rather than an empty planner that only fails once
    JavaScript runs.
    """
    principal = _principal(request)
    if principal is None:
        # An HTML route, so an HTML answer: a JsonResponse here would render as a
        # line of JSON in the browser window where a page should be.
        raise PermissionDenied("This page is for signed-in students.")
    draft = owned_draft(principal.student_id, draft_id)
    return render(
        request,
        "core/student_planner.html",
        {
            **get_sidebar_context(request),
            # The id only. Courses, sections and alternatives are fetched, so the
            # template cannot become a second, divergent serialiser of the same row.
            "draft_id": str(draft.id),
        },
    )


__all__ = [
    "draft_confirm_rebuild_view",
    "draft_create_view",
    "draft_detail_view",
    "draft_edit_view",
    "draft_generate_view",
    "draft_select_view",
    "student_timetable_start_view",
    "student_planner_page",
]
