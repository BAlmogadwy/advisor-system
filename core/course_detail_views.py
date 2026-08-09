"""One endpoint over one course, for the student who owns the question.

Identity comes from the session and nothing else — the URL names a COURSE, never a
student. That is the same rule as the planner and the conversation endpoints, and
it is why there is no ownership check after the query: there is no student id to
check against.

Everything this returns is assembled by `services.course_detail`, including all of
the Arabic. This view parses, spends a read budget, and shapes the reply.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .advisor_http import forbidden as _forbidden
from .advisor_http import over_budget as _over_budget
from .advisor_http import student_principal as _principal
from .services.course_detail import CourseDetailUnavailable, build_course_detail
from .services.planner_drafts import DraftRejected
from .services.rate_limit import CONVERSATION, HISTORY
from .services.student_planner import PlannerUnavailable
from .sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


@require_GET
def course_detail_view(request: HttpRequest, course_code: str) -> JsonResponse:
    """What this course is, what it requires, and where this student stands.

    Charged to the READ budget. Nothing here runs a solver or a model — it is one
    `build_unlock_report` and a handful of lookups — so it must not draw on the
    planner's or the adviser's allowance, which is the mistake the planner made in
    the other direction and had to undo.
    """
    principal = _principal(request)
    if principal is None:
        return _forbidden()
    over = _over_budget(HISTORY, principal.student_id)
    if over:
        return over

    try:
        detail = build_course_detail(principal.student_id, course_code)
    except CourseDetailUnavailable as exc:
        # A refusal with a sentence the student can act on, not a 500 and not a
        # report built from whichever programme sorted first.
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse(detail)


#: Said on an HTML route, in Arabic, as a PAGE. `over_budget` answers JSON, which
#: is right for a fetch and wrong in a browser window — the planner learned the
#: same lesson about refusals on page routes.
#:
#: TWO messages, because two different things get throttled and one sentence
#: described only the first. A student who pressed "add to planner" was told they
#: had opened too many PAGES, under the heading "cannot show the course" — an
#: accurate refusal about an action they did not take.
THROTTLED_PAGE_AR = "لقد فتحت صفحات كثيرة في وقت قصير. يرجى المحاولة بعد قليل."
THROTTLED_ACTION_AR = "لقد أرسلت طلبات كثيرة في وقت قصير. يرجى المحاولة بعد قليل."

#: The heading has to move with the message, or the page still says "cannot show
#: the course" over a sentence about sending requests.
THROTTLED_PAGE_HEADING_AR = "تعذّر عرض المقرر"
THROTTLED_ACTION_HEADING_AR = "تعذّر تنفيذ الطلب"


def _throttled_page(
    request: HttpRequest,
    over: JsonResponse,
    *,
    message: str = THROTTLED_PAGE_AR,
    heading: str = THROTTLED_PAGE_HEADING_AR,
):
    """The limiter's OWN answer, rendered as a page, about the thing that was refused.

    The first version collapsed `over_budget` to a boolean and then hard-coded
    `Retry-After: 60`. The `HISTORY` window is 600 seconds, so that told a student
    to come back in a minute while the limiter went on refusing them for ten — a
    number invented by the layer least entitled to invent it. The decision is
    carried through instead of re-guessed.
    """
    response = render(
        request,
        "core/student_course_detail.html",
        {
            **get_sidebar_context(request),
            "refusal": message,
            "refusal_heading": heading,
            "retry_after": over["Retry-After"],
        },
        status=429,
    )
    response["Retry-After"] = over["Retry-After"]
    return response


@require_GET
def course_detail_page(request: HttpRequest, course_code: str):
    """The screen. Server-rendered, like the locked-course page it is reached from.

    No JavaScript: every sentence arrives already translated from the service, and
    the strongest expression of "no academic rule in the browser" in this project
    is a page that has no browser code at all.
    """
    principal = _principal(request)
    if principal is None:
        # An HTML route, so an HTML answer. A JsonResponse here would render as a
        # line of JSON in the window where a page should be.
        raise PermissionDenied("This page is for signed-in students.")

    # The SAME read budget as the JSON route. This page calls the identical
    # `build_unlock_report` — 118 to 158 queries — and without this it was an
    # unmetered way round the limit its own sibling enforces. It is also the route
    # a browser actually takes, so it was the cheap one to abuse.
    over = _over_budget(HISTORY, principal.student_id)
    if over:
        return _throttled_page(request, over)

    try:
        detail = build_course_detail(principal.student_id, course_code)
    except CourseDetailUnavailable as exc:
        return render(
            request,
            "core/student_course_detail.html",
            {**get_sidebar_context(request), "refusal": str(exc)},
            status=409,
        )
    return render(
        request,
        "core/student_course_detail.html",
        {**get_sidebar_context(request), "detail": detail},
    )


@require_POST
def course_to_planner_view(request: HttpRequest, course_code: str):
    """Put ONE course into a planner draft, then redirect to the planner.

    Post/Redirect/Get, because the page it is posted from has no JavaScript. It
    calls the same `create_draft` as the JSON endpoint — one service, two doors —
    so the course is validated against what this student may take either way.

    It plans; it does not register, and it does not assert eligibility. The button
    is offered only when the prerequisites are already satisfied, and the sentence
    beside it says what it does not do.
    """
    principal = _principal(request)
    if principal is None:
        raise PermissionDenied("This page is for signed-in students.")
    # An HTML form POST, so an HTML refusal. Returning the limiter's JsonResponse
    # here put a JSON document in the browser window — the same defect just fixed
    # on the GET route, in the other door of the same screen.
    over = _over_budget(CONVERSATION, principal.student_id)
    if over:
        return _throttled_page(
            request,
            over,
            message=THROTTLED_ACTION_AR,
            heading=THROTTLED_ACTION_HEADING_AR,
        )

    from django.shortcuts import redirect

    from .services.planner_drafts import create_draft

    try:
        draft = create_draft(student_id=principal.student_id, course_codes=[course_code])
    except (DraftRejected, PlannerUnavailable) as exc:
        return render(
            request,
            "core/student_course_detail.html",
            {**get_sidebar_context(request), "refusal": str(exc)},
            status=409,
        )
    return redirect("student_planner_page", draft_id=str(draft.id))


__all__ = ["course_detail_page", "course_detail_view", "course_to_planner_view"]
