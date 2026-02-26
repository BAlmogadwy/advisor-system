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
    return render(request, "core/advisor_portfolio.html", get_sidebar_context(request))
