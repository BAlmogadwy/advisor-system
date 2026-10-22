import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import ProgrammeRequirement, Student, TimetableScenario
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


def test_workspace_page_lists_the_programmes_that_have_a_degree_plan(http):
    # Each programme once however many plan rows it has, sorted, and read from
    # the degree plans: a student's programme with no plan is not offered.
    for program, code in [
        ("DS2", "DS201"),
        ("AI", "AI101"),
        ("AI", "AI102"),
        ("COE", "COE101"),
        ("AI2", "AI101"),
        ("cs ", "CS101"),
    ]:
        ProgrammeRequirement.objects.create(program=program, course_code=code)
    Student.objects.create(student_id=9001, program="ZZ")

    with CaptureQueriesContext(connection) as queries:
        response = http.get("/timetable-workspace/")

    assert response.status_code == 200
    assert response.context["programme_choices"] == ["AI", "AI2", "COE", "CS", "DS2"]
    assert sum("programme_requirements" in q["sql"] for q in queries) == 1


def test_workspace_page_has_no_programme_choices_without_degree_plans(http):
    response = http.get("/timetable-workspace/")

    assert response.status_code == 200
    assert response.context["programme_choices"] == []


# A lock keeps a section's days and times; its room can still change.
LOCK_HELP = {
    "en": "Keeps this section's days and times; its room can still change.",
    "ar": "يثبّت أيام وأوقات هذه الشعبة؛ ويمكن تغيير القاعة.",
}


class _Buttons(HTMLParser):
    """Collects the attributes of every ``<button id=...>`` in a page."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "button" and attributes.get("id"):
            self.by_id[attributes["id"]] = attributes


@pytest.mark.parametrize("language", ["en", "ar"])
def test_split_drawer_lock_button_says_a_lock_keeps_days_and_times_not_the_room(http, language):
    # The jsdom harnesses strip {% %} tags, which would join both titles into one
    # string, so the drawer's server-rendered title is checked here.
    response = http.get("/timetable-workspace/split/", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    html = response.content.decode()
    # The page really rendered in the requested language (the site defaults to en).
    assert f'<html lang="{language}" dir="{"rtl" if language == "ar" else "ltr"}"' in html
    buttons = _Buttons(html).by_id
    lock = buttons["twsDrawerLock"]
    assert lock["title"] == LOCK_HELP[language]
    assert "dir" not in lock
    # Only the Lock control carries it, and only in the page's own language.
    assert buttons["twsDrawerRemove"].get("title") != LOCK_HELP[language]
    assert LOCK_HELP["ar" if language == "en" else "en"] not in html


def _shared_module_globals() -> dict[str, str]:
    """Each shared timetable module's global, and the static file defining it."""
    found = {}
    for module in sorted((settings.BASE_DIR / "static" / "js").glob("timetable-*.js")):
        for name in re.findall(
            r"^\s*(?:window|globalThis)\.(Timetable[A-Z]\w*)\s*=",
            module.read_text(encoding="utf-8"),
            flags=re.M,
        ):
            found[name] = f"js/{module.name}"
    return found


@pytest.mark.parametrize(
    "path, page_script",
    [
        ("/timetable-workspace/", "js/page-timetable-workspace.js"),
        ("/timetable-workspace/split/", "js/page-timetable-workspace-split.js"),
    ],
)
def test_each_page_loads_every_shared_module_it_uses_before_its_own_script(path, page_script, http):
    # The jsdom harnesses load the modules themselves, so only the rendered
    # page proves that the template does.
    response = http.get(path)
    assert response.status_code == 200
    loaded = [
        source.split("?")[0]
        for source in re.findall(
            rf'<script\b[^>]*\bsrc="{re.escape(settings.STATIC_URL)}([^"]+)"',
            response.content.decode(),
        )
    ]
    assert page_script in loaded, loaded
    before = loaded[: loaded.index(page_script)]
    text = (settings.BASE_DIR / "static" / page_script).read_text(encoding="utf-8")
    used = {
        name: module
        for name, module in _shared_module_globals().items()
        if re.search(rf"\b{name}\b", text)
    }
    assert "TimetableSectionTeacher" in used, sorted(used)
    missing = {name: module for name, module in used.items() if module not in before}
    assert missing == {}, f"{path} uses these before loading them: {missing}"


def test_the_workspace_job_card_is_a_monitor_that_offers_no_legacy_rebuild(http, settings):
    # Optimise ▾ Full rebuild is the workspace's rebuild; the card's own
    # single-strategy one, and its "Run again", are not offered there.
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    settings.TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED = True
    response = http.get("/timetable-workspace/")
    assert response.status_code == 200
    config = json.loads(response.context["pr8_config_json"])
    assert config["offerRebuild"] is False
    assert '"offerRebuild": false' in response.content.decode()
