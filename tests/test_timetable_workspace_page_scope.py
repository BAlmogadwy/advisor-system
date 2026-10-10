import re
import shutil
import subprocess

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import TimetableScenario
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db
PAGES = [
    "/timetable-workspace/",
    "/timetable-workspace/split/",
    "/timetable-workspace/mri/",
    "/timetable-workspace/graph/",
]


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(
        "core.timetable_workspace_views.load_defaults",
        lambda: {
            "academic_year": "1447",
            "term": "1",
        },
    )
    ensure_role_groups()
    user = User.objects.create_user(username="workspace-scope-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.parametrize("path", PAGES)
def test_explicit_scenario_initializes_its_year_and_semester_before_lists_load(path, http):
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", programs=["AI"])
    with CaptureQueriesContext(connection) as queries:
        response = http.get(path, {"scenario": str(scenario.id), "board": "2420"})
    assert response.status_code == 200
    assert response.context["default_year"] == "1448"
    assert response.context["default_term"] == "2"
    assert response.context["initial_scenario"] == str(scenario.id)
    assert response.context["initial_board"] == "2420"
    rendered = response.content.decode()
    if path.endswith("/split/"):
        assert 'const DEFAULT_YEAR = "1448";' in rendered
        assert 'const DEFAULT_TERM = "2";' in rendered
    else:
        assert 'value="1448"' in rendered
        assert 'value="2"' in rendered
    assert not any(
        query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in queries
    )


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("invalid", [None, "not-a-scenario", "999999", "-1", "9" * 100])
def test_missing_invalid_and_unknown_scenario_keep_saved_defaults(path, invalid, http):
    response = http.get(path, {} if invalid is None else {"scenario": invalid})
    assert response.status_code == 200
    assert response.context["default_year"] == "1447"
    assert response.context["default_term"] == "1"
    assert response.context["initial_scenario"] == ""


@pytest.mark.parametrize("path", PAGES)
def test_explicit_scenario_preserves_login_and_role_checks(path, http):
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2")
    anonymous = Client().get(path, {"scenario": scenario.id})
    assert anonymous.status_code == 302
    assert "/login" in anonymous["Location"]
    user = User.objects.create_user(username="workspace-scope-unprivileged")
    http.force_login(user)
    denied = http.get(path, {"scenario": scenario.id})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ROLE_REQUIRED"
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    permitted = http.get(path, {"scenario": scenario.id})
    assert permitted.status_code == 200
    assert permitted.context["default_term"] == "2"


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("year,term", [("١٤٤٨", "2"), ("", ""), ('14"8', "2")])
def test_requested_scope_values_do_not_break_the_rendered_javascript(path, year, term, http):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to parse rendered workspace JavaScript")
    scenario = TimetableScenario.objects.create(academic_year=year, term=term)
    response = http.get(path, {"scenario": scenario.id})
    assert response.status_code == 200
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", response.content.decode(), flags=re.S)
    bootstrap = next(
        script for script in scripts if "defaultYear" in script or "DEFAULT_YEAR" in script
    )
    parsed = subprocess.run(
        [node, "--check"],
        input=bootstrap,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
