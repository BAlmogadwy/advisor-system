"""The advisor inbox in the sidebar.

The questions the virtual advisor passes to a person could be reached only by
typing /ops/advisor-cases/: no link to the inbox was ever drawn. The sidebar now
has one, for the roles the inbox lets in, while students can send such questions
(the same setting), with how many wait for an advisor. Never on the MRI page
(the owner's rule: it stays exactly as it is).
"""

from __future__ import annotations

import re

import pytest
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import AdvisorEscalation, Student
from core.services import advisor_inbox, student_otp
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
)
from core.sidebar_context import get_sidebar_context
from tests.test_advisor_inbox import MINE, THEIRS, _case, _staff

pytestmark = pytest.mark.django_db

PAGE = "/advisor-portfolio/"
Status = AdvisorEscalation.Status


@pytest.fixture(autouse=True)
def _escalations_on(settings):
    settings.STUDENT_ADVISOR_V2_ENABLED = True


def _html(user, url=PAGE, language="en"):
    client = Client()
    client.force_login(user)
    response = client.get(url, headers={"accept-language": language})
    assert response.status_code == 200, url
    return response.content.decode()


def _link(html):
    """The inbox's sidebar link, whole, or None."""
    match = re.search(r'<a [^>]*href="/ops/advisor-cases/"[^>]*>.*?</a>', html, re.S)
    return match.group(0) if match else None


def test_an_advisor_finds_the_inbox_with_how_many_wait_for_an_advisor():
    advisor = _staff("inbox-link-adv", ROLE_ADVISOR, advisor_id="A1")
    _case(MINE)
    _case(MINE, status=Status.ASSIGNED)
    # Waiting on the student, finished, or another advisor's student: not counted.
    _case(MINE, status=Status.NEEDS_INFORMATION)
    _case(MINE, status=Status.RESOLVED)
    _case(MINE, status=Status.CLOSED)
    _case(THEIRS, advisor_id="A2")
    link = _link(_html(advisor))
    assert link is not None
    assert "Escalated cases" in link
    # The number for the eye, and the same in words for a screen reader.
    assert '<span class="nav-badge" aria-hidden="true">2</span>' in link
    assert '<span class="visually-hidden"> (Waiting for an advisor: 2)</span>' in link


def test_in_arabic_the_link_and_its_count_are_said_in_arabic():
    advisor = _staff("inbox-link-ar", ROLE_ADVISOR, advisor_id="A1")
    _case(MINE)
    link = _link(_html(advisor, language="ar"))
    assert "الحالات المحالة" in link
    assert "(بانتظار مرشد: 1)" in link


def test_no_count_is_drawn_when_nothing_waits():
    advisor = _staff("inbox-link-none", ROLE_ADVISOR, advisor_id="A1")
    _case(MINE, status=Status.NEEDS_INFORMATION)
    link = _link(_html(advisor))
    assert link is not None and "nav-badge" not in link and "Waiting" not in link


def test_the_link_is_lit_on_the_inbox_and_on_its_cases():
    advisor = _staff("inbox-link-lit", ROLE_ADVISOR, advisor_id="A1")
    case = _case(MINE)
    for url in (reverse("advisor_inbox"), reverse("advisor_inbox_case", args=[case.reference])):
        link = _link(_html(advisor, url))
        assert 'class="nav-a nav-link active"' in link and 'aria-current="page"' in link, url
    # Elsewhere it is a plain link.
    assert "active" not in _link(_html(advisor))


@pytest.mark.parametrize(
    ("who", "offered"),
    [
        (ROLE_ADVISOR, True),
        (ROLE_GENERAL_ADVISOR, True),
        (ROLE_SUPER_ADMIN, True),
        (ROLE_EXAM_COMMITTEE, False),
        ("STUDENT", False),
    ],
)
def test_the_link_is_offered_to_the_roles_the_inbox_lets_in(who, offered):
    """The flag itself, not only what the sidebar draws: a student's or the
    committee's sidebar has no staff group, so the drawing alone would not show a
    wrong flag."""
    _case(MINE)
    if who == "STUDENT":
        Student.objects.get_or_create(student_id=MINE)
        user = student_otp.provision_student_user(MINE)
    else:
        user = _staff(f"inbox-link-{who.lower()}", who, advisor_id="A1")
    request = RequestFactory().get(PAGE)
    request.user = user
    context = get_sidebar_context(request)
    assert context["can_advisor_inbox"] is offered
    if not offered:
        assert context["advisor_inbox_waiting"] == 0


def test_while_students_cannot_escalate_there_is_no_link_and_no_count(settings):
    settings.STUDENT_ADVISOR_V2_ENABLED = False
    advisor = _staff("inbox-link-off", ROLE_ADVISOR, advisor_id="A1")
    _case(MINE)
    client = Client()
    client.force_login(advisor)
    with CaptureQueriesContext(connection) as captured:
        html = client.get(PAGE).content.decode()
    assert _link(html) is None
    assert not [q["sql"] for q in captured if "advisor_escalations" in q["sql"]]


def test_the_count_is_the_one_query_the_link_adds(settings):
    """Every staff page draws the sidebar: the link costs its COUNT and nothing
    more (the scope the sidebar has already read is passed on, not read again)."""
    advisor = _staff("inbox-link-cost", ROLE_ADVISOR, advisor_id="A1")
    _case(MINE)
    request = RequestFactory().get(PAGE)
    request.user = advisor

    def cost():
        get_sidebar_context(request)  # the role groups and the scope table, once
        with CaptureQueriesContext(connection) as captured:
            get_sidebar_context(request)
        return len(captured)

    with_link = cost()
    settings.STUDENT_ADVISOR_V2_ENABLED = False
    assert with_link - cost() == 1


def test_a_failing_count_never_takes_the_page_down(monkeypatch):
    def broken(_user, scope=None):
        raise RuntimeError("count failed")

    monkeypatch.setattr(advisor_inbox, "visible_cases", broken)
    advisor = _staff("inbox-link-broken", ROLE_ADVISOR, advisor_id="A1")
    link = _link(_html(advisor))
    assert link is not None and "nav-badge" not in link


def test_the_mri_page_shows_no_inbox_link_and_counts_nothing():
    """The owner's rule: the MRI page stays exactly as it is. Read only; no MRI
    file is touched."""
    admin = _staff("inbox-link-admin", ROLE_SUPER_ADMIN)
    _case(MINE)
    client = Client()
    client.force_login(admin)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(reverse("timetable_workspace_mri_page"))
    assert response.status_code == 200
    assert "/ops/advisor-cases/" not in response.content.decode()
    assert not [q["sql"] for q in captured if "advisor_escalations" in q["sql"]]
    # The same person on any other page does get the link, with the count.
    link = _link(_html(admin))
    assert link is not None and "(Waiting for an advisor: 1)" in link
