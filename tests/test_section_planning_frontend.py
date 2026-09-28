"""Run the real Section Planning page script against the rendered page in a DOM.

The page is rendered by its real view (English, and Arabic when asked for), then
``tests/frontend/section-planning.test.cjs`` runs ``static/js/page-section-planning.js``
unmodified in jsdom with scripted HTTP answers. No browser, server or network is
used, and any request the suite did not expect fails it. Install the locked
development dependencies with ``npm ci`` first.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["en", "ar"])
def test_section_planning_page_interactions(client: Client, tmp_path: Path, language: str) -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Section planning DOM tests require Node.js and the locked npm ci dependencies")

    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="sp-frontend")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    response = client.get("/section-planning/", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert f'lang="{language}"' in html

    fixture = tmp_path / f"section-planning-{language}.html"
    fixture.write_text(html, encoding="utf-8")
    result = subprocess.run(
        [node, "--test", "tests/frontend/section-planning.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "SP_TEST_HTML": str(fixture), "SP_TEST_LANGUAGE": language},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
