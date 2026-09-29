"""The exam page's lock controls: who is offered them, and the page parts they use.

The buttons follow the Save rule: whoever may build and save an exam timetable
(a Super Admin or the Exam Committee) may lock and unlock its days and
periods. The page script reads that from ``data-can-edit-exam-timetable``;
tests/frontend/exam-locks.test.cjs covers what it does with it.
"""

from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from core.services.rbac import ROLE_EXAM_COMMITTEE, ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

LOCK_PARTS = (
    "examLockEditor",
    "examLockRows",
    "examLockNotice",
    "examLockLine",
    "examLockRefusal",
    "lockBar",
    "lockBarReview",
    "examMoveError",
)


class Main(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.main: dict[str, str | None] = {}
        self.ids: set[str] = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "main":
            self.main = attributes
        if attributes.get("id"):
            self.ids.add(attributes["id"])


def _page(client, django_user_model, role: str, language: str) -> Main:
    ensure_role_groups()
    user = django_user_model.objects.create_user(username=f"locks-{role.lower()}-{language}")
    user.groups.add(Group.objects.get(name=role))
    client.force_login(user)
    response = client.get(reverse("exam_timetable_page"), HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    return Main(response.content.decode())


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize("role", [ROLE_SUPER_ADMIN, ROLE_EXAM_COMMITTEE])
def test_every_role_that_may_save_is_offered_the_lock_buttons(
    client, django_user_model, role, language
):
    page = _page(client, django_user_model, role, language)
    # The same rule as every build and save endpoint: both roles may Save.
    assert page.main["data-can-edit-exam-timetable"] == "true"
    assert page.main["data-can-delete-exam-timetable"] == (
        "true" if role == ROLE_SUPER_ADMIN else "false"
    )
    assert set(LOCK_PARTS) <= page.ids


def test_a_page_rendered_without_the_flag_offers_no_lock_buttons():
    from django.template.loader import render_to_string

    html = render_to_string(
        "core/exam_timetable.html", {"LANGUAGE_CODE": "en", "role": "", "csrf_token": "x"}
    )
    # Missing means "may not": the buttons are never offered by default.
    assert Main(html).main["data-can-edit-exam-timetable"] == "false"
