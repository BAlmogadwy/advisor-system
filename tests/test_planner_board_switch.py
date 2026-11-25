"""The new planner screen is reachable only through a switch.

`PLANNER_BOARD_ENABLED` (default off) picks which template /planner/ renders;
`?layout=board` and `?layout=classic` let staff open either one while both
exist. The staff rule is the same for both.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings

from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups

BOARD = "core/planner_board.html"
CLASSIC = "core/planner.html"


@pytest.fixture
def staff_client(db) -> Client:
    user = get_user_model().objects.create_superuser(
        username="board-switch", email="board-switch@example.test", password="unused"
    )
    client = Client()
    client.force_login(user)
    return client


def _templates(response) -> list[str]:
    return [t.name for t in response.templates if t.name]


@pytest.mark.django_db
class TestSwitch:
    @override_settings(PLANNER_BOARD_ENABLED=False)
    def test_off_serves_the_current_page(self, staff_client: Client) -> None:
        response = staff_client.get("/planner/")
        assert response.status_code == 200
        assert CLASSIC in _templates(response)
        assert BOARD not in _templates(response)

    @override_settings(PLANNER_BOARD_ENABLED=True)
    def test_on_serves_the_new_page(self, staff_client: Client) -> None:
        response = staff_client.get("/planner/")
        assert response.status_code == 200
        assert BOARD in _templates(response)
        assert CLASSIC not in _templates(response)

    @override_settings(PLANNER_BOARD_ENABLED=False)
    def test_layout_board_opens_the_new_page_when_off(self, staff_client: Client) -> None:
        response = staff_client.get("/planner/?layout=board")
        assert BOARD in _templates(response)

    @override_settings(PLANNER_BOARD_ENABLED=True)
    def test_layout_classic_opens_the_current_page_when_on(self, staff_client: Client) -> None:
        response = staff_client.get("/planner/?layout=classic")
        assert CLASSIC in _templates(response)
        assert BOARD not in _templates(response)

    @override_settings(PLANNER_BOARD_ENABLED=False)
    def test_an_unknown_layout_value_changes_nothing(self, staff_client: Client) -> None:
        assert CLASSIC in _templates(staff_client.get("/planner/?layout=nonsense"))

    @override_settings(PLANNER_BOARD_ENABLED=True)
    def test_an_unknown_layout_value_keeps_the_default_when_on(self, staff_client: Client) -> None:
        assert BOARD in _templates(staff_client.get("/planner/?layout=nonsense"))

    def test_the_setting_defaults_to_off(self) -> None:
        from django.conf import settings

        assert settings.PLANNER_BOARD_ENABLED is False

    @override_settings(PLANNER_BOARD_ENABLED=False)
    def test_the_new_page_loads_its_own_script_and_the_deep_link_keeps_working(
        self, staff_client: Client
    ) -> None:
        html = staff_client.get("/planner/?layout=board&student=4500000").content.decode()
        assert "js/page-planner-board.js?v=6" in html
        assert "js/page-planner.js" not in html

    @override_settings(PLANNER_BOARD_ENABLED=False)
    def test_the_new_page_has_arabic_and_right_to_left_from_the_server(
        self, staff_client: Client
    ) -> None:
        staff_client.cookies["advisor_language"] = "ar"
        html = staff_client.get("/planner/?layout=board").content.decode()
        assert 'lang="ar"' in html
        assert 'dir="rtl"' in html
        assert "التخرّج المتوقع" in html
        assert "تعديل القواعد" in html


@pytest.mark.django_db
class TestStaffRule:
    @pytest.mark.parametrize("query", ["", "?layout=board", "?layout=classic"])
    def test_a_non_staff_role_is_refused_on_either_page(self, query: str) -> None:
        user = get_user_model().objects.create_user(
            username="board-committee", email="board-committee@example.test", password="unused"
        )
        ensure_role_groups()
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        response = client.get(f"/planner/{query}")
        assert response.status_code == 403

    @pytest.mark.parametrize("query", ["", "?layout=board"])
    def test_an_anonymous_visitor_is_sent_to_login(self, query: str) -> None:
        response = Client().get(f"/planner/{query}")
        assert response.status_code == 302
        assert "login" in response["Location"]
