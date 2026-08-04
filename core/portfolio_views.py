"""
core/portfolio_views.py
Standalone Advisor Portfolio page view.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.services.rbac import ROLE_ADVISOR
from core.sidebar_context import get_sidebar_context


@role_required(ROLE_ADVISOR)  # ADVISOR, GENERAL_ACADEMIC_ADVISOR, SUPER_ADMIN
@require_GET
def advisor_portfolio_page(request: HttpRequest) -> HttpResponse:
    context = get_sidebar_context(request)
    # ONE condition for the picker, computed here and handed to both the template
    # and (via the template) the JS. The template used to hide the bar on
    # `role == 'ADVISOR'` while the JS decided to skip it on
    # `role === 'ADVISOR' && USER_ADVISOR_ID`. Two conditions, one of them wrong
    # whenever the id was blank, and no way for either to notice.
    context["hide_advisor_picker"] = bool(
        context.get("role") == ROLE_ADVISOR and str(context.get("user_advisor_id") or "").strip()
    )
    return render(request, "core/advisor_portfolio.html", context)
