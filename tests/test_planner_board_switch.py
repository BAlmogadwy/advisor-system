"""/planner/ always renders the board.

The old page was retired on 2026-11-25; there is no switch and `?layout=` changes
nothing. The staff rule, the deep link and the Arabic page are pinned here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client

from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups

BOARD = "core/planner_board.html"


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
class TestBoardIsThePage:
    def test_planner_renders_the_board_for_staff(self, staff_client: Client) -> None:
        response = staff_client.get("/planner/")
        assert response.status_code == 200
        assert BOARD in _templates(response)

    @pytest.mark.parametrize("query", ["?layout=classic", "?layout=board", "?layout=nonsense"])
    def test_the_layout_query_changes_nothing(self, staff_client: Client, query: str) -> None:
        response = staff_client.get(f"/planner/{query}")
        assert response.status_code == 200
        assert _templates(response).count(BOARD) == 1
        assert not [name for name in _templates(response) if name == "core/planner" + ".html"]

    def test_the_page_loads_its_own_script_and_not_the_retired_one(
        self, staff_client: Client
    ) -> None:
        # The deep link itself is measured in a browser: test_planner_board_guards_browser.py.
        html = staff_client.get("/planner/?student=4500000").content.decode()
        assert "js/page-planner-board.js?v=21" in html
        assert "js/page-planner.js" not in html

    def test_the_page_has_arabic_and_right_to_left_from_the_server(
        self, staff_client: Client
    ) -> None:
        staff_client.cookies["advisor_language"] = "ar"
        html = staff_client.get("/planner/").content.decode()
        assert 'lang="ar"' in html
        assert 'dir="rtl"' in html
        assert "التخرّج المتوقع" in html
        assert "القواعد المعتادة" in html
        assert "خطة الفصل القادم" in html


@pytest.mark.django_db
class TestStaffRule:
    @pytest.mark.parametrize("query", ["", "?layout=board", "?layout=classic"])
    def test_a_non_staff_role_is_refused(self, query: str) -> None:
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


def test_nothing_still_names_the_retired_switch_or_page() -> None:
    # Built from pieces so this file does not name them itself.
    needles = ["PLANNER_BOARD" + "_ENABLED", "core/planner" + ".html"]
    base = Path(settings.BASE_DIR)
    tracked = subprocess.run(
        ["git", "ls-files", "*.py", "*.html", "*.js", "*.cjs", "render.yaml"],
        cwd=base,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    hits = []
    for name in tracked:
        path = base / name
        if not path.is_file() or "node_modules" in name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in needles:
            if needle in text:
                hits.append(f"{name}: {needle}")
    assert hits == []
