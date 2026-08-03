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

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from .advisor_http import forbidden as _forbidden
from .advisor_http import over_budget as _over_budget
from .advisor_http import student_principal as _principal
from .services.course_detail import CourseDetailUnavailable, build_course_detail
from .services.rate_limit import HISTORY

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


__all__ = ["course_detail_view"]
