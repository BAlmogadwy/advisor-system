"""
core/sidebar_context.py
Shared helper that builds the sidebar permission context variables.
Import and call `get_sidebar_context(request)` in any view that
renders a template including the sidebar partial.
"""

from django.conf import settings
from django.http import HttpRequest

from core.models import TimetableFeedback
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_scope,
)
from core.services.timetable_feedback_workflow import unread_q


def get_sidebar_context(request: HttpRequest) -> dict[str, object]:
    """Return the template context variables needed by sidebar.html."""
    ensure_role_groups()
    ensure_scope_schema()
    scope = get_user_scope(request.user)
    role = str(scope.get("role", ROLE_ADVISOR))
    # A setting, not a query: staff pages (the MRI page among them) pay nothing.
    timetable_feedback = role == ROLE_STUDENT and bool(settings.TIMETABLE_FEEDBACK_ENABLED)

    return {
        "role": role,
        "user_advisor_id": str(scope.get("advisor_id", "")),
        "can_admin_advisors": role == ROLE_SUPER_ADMIN,
        "can_view_portfolio": role in {ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR},
        "can_graduation_planning": role == ROLE_SUPER_ADMIN,
        "can_db_admin": role == ROLE_SUPER_ADMIN,
        "can_exam_timetable": role in {ROLE_SUPER_ADMIN, ROLE_EXAM_COMMITTEE},
        "can_delete_exam_timetable": role == ROLE_SUPER_ADMIN,
        "can_section_planning": role in {ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR},
        "can_timetable_feedback": timetable_feedback,
        # One indexed query, for that student only, behind the same setting.
        "timetable_feedback_unread": (
            _unread_feedback(scope.get("student_id")) if timetable_feedback else 0
        ),
    }


def _unread_feedback(student_id: object) -> int:
    """How many of the student's cases hold something they have not seen."""
    if student_id is None:
        return 0
    return TimetableFeedback.objects.filter(student_id=student_id).filter(unread_q()).count()
