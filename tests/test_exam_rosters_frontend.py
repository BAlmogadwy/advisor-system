"""Student lists in a DOM: the page, the course drawer and the card link.

Both pages are rendered by their real views - English, and Arabic when it is
asked for - and every JSON answer the scripts read is made by the real
endpoints on the export fixture's saved run (tests/exam_student_export_fixture.py),
before and after the lists change under it. The Node suites
(tests/frontend/exam-rosters.test.cjs and exam-roster-drawer.test.cjs) then
run the real scripts against those answers in jsdom: no server, browser or
network. Refusals the fixture cannot produce (a gate, a failed audit) are
answered in the suites with the shapes tests/test_exam_roster_endpoints.py
pins.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from core.models import Student
from core.services import exam_roster_view as rv
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.exam_source_factory import scraped_exam_registration
from tests.exam_student_export_fixture import ALL_IDS, build_population, build_saved_run

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
#: A student enrolled after the save: MATH101 M1 then differs from the run.
LATE_STUDENT = 4401099
#: Searches the suites type; each answer is the real endpoint's.
SEARCHES = ["4401", "44010", "4402", "math", "testname", "student 440200", "zzzz"]


@pytest.fixture(autouse=True)
def _fresh_cache():
    rv.ROSTER_VIEW_CACHE.clear()
    yield
    rv.ROSTER_VIEW_CACHE.clear()


def _post(client, url, body, **extra):
    return client.post(url, json.dumps(body), content_type="application/json", **extra)


def _answers(client, run) -> dict:
    """Every answer the page and the drawer ask for, from the real views."""
    index = client.get(reverse("exam_roster_index", args=[run.pk])).json()
    detail_url = reverse("exam_roster_detail", args=[run.pk])
    lookup_url = reverse("exam_roster_lookup", args=[run.pk])
    scopes = [{"kind": "course", "exam": exam["code"]} for exam in index["exams"]]
    scopes += [
        {"kind": "room", "slot_index": room["slot_index"], "room_code": room["room_code"]}
        for room in index["rooms"]
    ]
    scopes += [
        {
            "kind": "section",
            "exam": item["exam"],
            "section_key": item["section_key"],
            "gender": item["gender"],
        }
        for item in index["not_assigned"]
    ]
    rosters = {}
    for scope in scopes:
        answer = _post(client, detail_url, {"scope": scope}).json()
        assert answer["ok"], answer
        rosters[json.dumps(scope, sort_keys=True)] = answer
    lookups = {
        str(sid): _post(client, lookup_url, {"student_id": sid}).json()
        for sid in [*ALL_IDS, 1234567]
    }
    searches = {query: _post(client, lookup_url, {"query": query}).json() for query in SEARCHES}
    preflight = _post(client, reverse("exam_student_export_preflight", args=[run.pk]), {}).json()
    return {
        "index": index,
        "rosters": rosters,
        "lookups": lookups,
        "searches": searches,
        "preflight": preflight,
    }


def _changed_answers(client, run) -> dict:
    """The navigator and MATH101 once a student has joined M1 after the save."""
    from core.models import Course

    Student.objects.create(student_id=LATE_STUDENT, name="LATE JOINER", program="CS", section="M")
    scraped_exam_registration(
        LATE_STUDENT, Course.objects.get(course_code="MATH101"), section_label="M1"
    )
    rv.ROSTER_VIEW_CACHE.clear()
    index = client.get(reverse("exam_roster_index", args=[run.pk])).json()
    course = _post(
        client,
        reverse("exam_roster_detail", args=[run.pk]),
        {"scope": {"kind": "course", "exam": "MATH101"}},
    ).json()
    assert course["counts"]["no_seat"] == 1 and course["counts"]["changed"] == 31
    return {"index": index, "course": course}


def _committee(client, django_user_model):
    ensure_role_groups()
    user = django_user_model.objects.create_user(username="lists.committee", password="x-Pass-1")
    user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
    client.force_login(user)
    return client


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize("suite", ["exam-rosters", "exam-roster-drawer"])
def test_student_lists_frontend(tmp_path, client, django_user_model, settings, language, suite):
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Student lists DOM tests require Node.js and the locked npm ci dependencies")
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    build_population()
    run = build_saved_run()
    committee = _committee(client, django_user_model)
    headers = {"HTTP_ACCEPT_LANGUAGE": language}
    pages = {
        "rosters": committee.get(reverse("exam_rosters_page"), {"run": run.pk}, **headers),
        "empty": committee.get(reverse("exam_rosters_page"), {"run": run.pk + 999}, **headers),
        "timetable": committee.get(reverse("exam_timetable_page"), **headers),
    }
    assert pages["rosters"].status_code == 200 and pages["timetable"].status_code == 200
    fixture = {
        "language": language,
        "run": run.pk,
        "pages": {name: str(tmp_path / f"{name}.html") for name in pages},
        "timetable": {
            "detail": committee.get(reverse("exam_timetable_detail", args=[run.pk])).json(),
            "list": committee.get(reverse("exam_timetable_list")).json(),
            "filters": committee.get(reverse("exam_timetable_filters")).json(),
            "courses": _post(
                committee,
                reverse("exam_timetable_preview_courses"),
                {"programs": ["AI", "CS", "CS2", "IS"], "sections": ["F", "M"]},
            ).json(),
        },
        **_answers(committee, run),
    }
    fixture["changed"] = _changed_answers(committee, run)
    for name, response in pages.items():
        Path(fixture["pages"][name]).write_text(response.content.decode(), encoding="utf-8")
    data = tmp_path / "answers.json"
    data.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [node, "--test", f"tests/frontend/{suite}.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "EXAM_ROSTERS_FIXTURE": str(data), "EXAM_TEST_LANGUAGE": language},
        capture_output=True,
        text=True,
        encoding="utf-8",
        # A guard against a hung run, not a budget.
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
