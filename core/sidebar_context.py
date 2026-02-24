"""
core/sidebar_context.py
Shared helper that builds the sidebar permission context variables.
Import and call `get_sidebar_context(request)` in any view that
renders a template including the sidebar partial.
"""

from django.http import HttpRequest

from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_scope,
)


def get_sidebar_context(request: HttpRequest) -> dict[str, object]:
    """Return the template context variables needed by sidebar.html."""
    ensure_role_groups()
    ensure_scope_schema()
    scope = get_user_scope(request.user)
    role = str(scope.get("role", ROLE_ADVISOR))

    return {
        "role": role,
        "user_advisor_id": str(scope.get("advisor_id", "")),
        "can_admin_advisors": role == ROLE_SUPER_ADMIN,
        "can_view_portfolio": role in {ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR},
        "can_db_admin": role == ROLE_SUPER_ADMIN,
        "can_exam_timetable": role == ROLE_SUPER_ADMIN,
    }
