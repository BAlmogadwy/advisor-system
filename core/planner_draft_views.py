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
end. Not rooms, not instructors, not registered counts, not the baseline the
solver saw, not the fingerprint — those sit in `generated_inputs` for the server's
own use and describe the institution rather than the student's week. The one
identifier that does travel is the term-section id, because pinning a section is
the student naming one, and they cannot name what they have not been given.
"""

from __future__ import annotations

import logging
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .advisor_http import forbidden as _forbidden
from .advisor_http import json_body as _body
from .advisor_http import over_budget as _over_budget
from .advisor_http import student_principal as _principal
from .services.planner_drafts import (
    ConfirmationRequired,
    DraftError,
    DraftExpired,
    DraftRejected,
    create_draft,
    edit_draft,
    generate,
    issue_rebuild_token,
    owned_draft,
    select_alternative,
)
from .services.rate_limit import CONVERSATION, GENERATION, HISTORY

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


def _alternative_json(
    alternative: dict[str, Any], names: dict[str, str], selected: str, requested: set[str]
) -> dict[str, Any]:
    return {
        "key": alternative.get("key", ""),
        "selected": bool(selected) and alternative.get("key") == selected,
        "credit_hours": alternative.get("credit_hours", 0),
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
                # The pin affordance: "keep this one next time" has to name a
                # section, and this is where the browser learns which.
                "term_section_id": c.get("term_section_id"),
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
            "selected_alternative": draft.selected_alternative,
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
        # Why a requested course is missing, in the student's own language. The
        # builder's raw reason is already translated by `_translate_unplaced`; the
        # machine-readable code goes no further than here.
        "unplaced": [
            {
                "course_code": str(u.get("course_code") or ""),
                "course_name": names.get(str(u.get("course_code") or ""), ""),
                "reason": str(u.get("reason") or ""),
            }
            for u in unplaced
        ],
    }


def _refused(exc: Exception, status: int = 409) -> JsonResponse:
    return JsonResponse({"error": str(exc)}, status=status)


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
    source = None
    raw_source = payload.get("source_message_id")
    if raw_source:
        from .models import AdvisorMessage

        source = AdvisorMessage.objects.filter(
            pk=str(raw_source), conversation__student_id=principal.student_id
        ).first()

    try:
        draft = create_draft(
            student_id=principal.student_id,
            course_codes=codes,
            keep_current_sections=bool(payload.get("keep_current_sections", True)),
            source_message=source,
        )
    except DraftRejected as exc:
        return _refused(exc, status=400)

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
    except DraftError as exc:
        return _refused(exc, status=400)
    return JsonResponse(
        {
            "confirmation": token,
            "version": draft.version,
            # Said plainly, because this is the sentence the student is agreeing to.
            "warning": "سيتم تجاهل الشُعب المسجّلة حاليًا وإعادة بناء الجدول من جديد.",
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
    # The expensive budget, shared with asking a question: the solver is the cost
    # either way, so a student cannot spend their way past one limit using the other.
    over = _over_budget(GENERATION, principal.student_id)
    if over:
        return over

    draft = owned_draft(principal.student_id, draft_id)
    try:
        draft = generate(draft, confirmation=payload.get("confirmation"))
    except ConfirmationRequired as exc:
        # 428: the request is well-formed and the student is entitled to make it;
        # what is missing is the confirmation.
        return JsonResponse({"error": str(exc), "needs_confirmation": True}, status=428)
    except DraftExpired as exc:
        return _refused(exc, status=410)
    except DraftRejected as exc:
        # Revalidation at generation time: a section withdrawn since the draft was
        # made, or a course the student may no longer take.
        return _refused(exc, status=409)
    except DraftError as exc:
        return _refused(exc, status=409)
    return JsonResponse(_draft_json(draft))


@require_POST
def draft_select_view(request: HttpRequest, draft_id: str) -> JsonResponse:
    """Record a preference. NOT a registration — nothing downstream writes one."""
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
    try:
        draft = select_alternative(draft, str(payload.get("key") or ""))
    except DraftExpired as exc:
        return _refused(exc, status=410)
    except DraftError as exc:
        return _refused(exc, status=409)
    data = _draft_json(draft)
    data["message"] = "تم حفظ هذا الجدول كخيارك المفضل. لم يتم تسجيلك في أي مقرر."
    return JsonResponse(data)


@require_GET
def student_planner_page(request: HttpRequest, draft_id: str):
    """The screen itself. Renders the shell; every fact on it arrives by fetch.

    The draft is looked up here too, under the same ownership filter, so someone
    else's id gives a 404 page rather than an empty planner that only fails once
    JavaScript runs.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    draft = owned_draft(principal.student_id, draft_id)
    return render(
        request,
        "core/student_planner.html",
        {
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
    "student_planner_page",
]
