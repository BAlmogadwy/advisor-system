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

from core.models import Course, Student
from core.services import exam_roster_view as rv
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.exam_source_factory import scraped_exam_registration
from tests.exam_student_export_fixture import ALL_IDS, MALE_AI, build_population, build_saved_run

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
#: A student enrolled after the save, with a LOWER ID than every member of
#: MATH101 M1: seats go in student ID order, so the student left without a
#: seat is an original member (the highest ID), not the late one.
LATE_STUDENT = 4401000
#: A section new since the save: no room was ever sized for its students.
NEW_SECTION = "M9"
NEW_STUDENTS = (4401201, 4401202)
#: A male student whose only MATH101 section is labelled F1: the lists name no
#: usable section for him, and the build rooms his group ("Section not
#: recorded", Male) - in a second saved run, so the first one's answers stay.
UNMAPPED_MALE = 4401031
#: Searches the suites type; each answer is the real endpoint's.
SEARCHES = ["4401", "44010", "4402", "4402003", "math", "testname", "zzzz"]


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
    """Every answer once the lists changed after the save (a new build).

    A low-ID student joined MATH101 M1 (31 now, rooms for 30) and a section
    new since the save, M9, has two students: three MATH101 students have no
    seat. Every other exam's sections still match, but the run's check is
    "changed" for all of them.
    """
    math = Course.objects.get(course_code="MATH101")
    Student.objects.create(student_id=LATE_STUDENT, name="LATE JOINER", program="CS", section="M")
    scraped_exam_registration(LATE_STUDENT, math, section_label="M1")
    for sid in NEW_STUDENTS:
        Student.objects.create(student_id=sid, name=f"NEW SECTION {sid}", program="AI", section="M")
        scraped_exam_registration(sid, math, section_label=NEW_SECTION)
    rv.ROSTER_VIEW_CACHE.clear()
    index = client.get(reverse("exam_roster_index", args=[run.pk])).json()
    scopes = [{"kind": "course", "exam": exam["code"]} for exam in index["exams"]]
    scopes += [
        {"kind": "room", "slot_index": room["slot_index"], "room_code": room["room_code"]}
        for room in index["rooms"]
        if room["slot_index"] == 0
    ]
    scopes += [
        {key: item[key] for key in ("exam", "section_key", "gender")} | {"kind": "section"}
        for item in index["no_seat"] + index["not_assigned"]
    ]
    detail = reverse("exam_roster_detail", args=[run.pk])
    rosters = {}
    for scope in scopes:
        answer = _post(client, detail, {"scope": scope}).json()
        assert answer["ok"], answer
        rosters[json.dumps(scope, sort_keys=True)] = answer
    course = rosters[json.dumps({"kind": "course", "exam": "MATH101"}, sort_keys=True)]
    assert course["counts"]["no_seat"] == 3 and course["counts"]["changed"] == 33
    no_seat = [row["student_id"] for row in course["rows"] if row["room_basis"] == "no_seat"]
    assert no_seat == [MALE_AI[-1], *NEW_STUDENTS], "an original member, then the new section"
    assert [(item["section"], item["no_seat"]) for item in index["no_seat"]] == [
        ("M1", 1),
        (NEW_SECTION, 2),
    ]
    return {"index": index, "rosters": rosters}


def _unmapped_answers(client) -> dict:
    """A second saved run whose MATH101 has a roomed "Section not recorded" Male group."""
    Student.objects.create(
        student_id=UNMAPPED_MALE, name="UNRECORDED SECTION", program="CS", section="M"
    )
    scraped_exam_registration(
        UNMAPPED_MALE, Course.objects.get(course_code="MATH101"), section_label="F1"
    )
    second = build_saved_run(label="Unrecorded section fixture")
    index = client.get(reverse("exam_roster_index", args=[second.pk])).json()
    detail = reverse("exam_roster_detail", args=[second.pk])
    course = _post(client, detail, {"scope": {"kind": "course", "exam": "MATH101"}}).json()
    lookup = _post(
        client, reverse("exam_roster_lookup", args=[second.pk]), {"student_id": UNMAPPED_MALE}
    ).json()
    row = next(row for row in lookup["rows"] if row["exam"] == "MATH101")
    assert (row["group"], row["section_status"], row["room_basis"]) == ("M", "missing", "whole")
    search = _post(
        client, reverse("exam_roster_lookup", args=[second.pk]), {"query": str(UNMAPPED_MALE)}
    ).json()
    room = _post(
        client, detail, {"scope": {"kind": "room", "slot_index": 0, "room_code": row["room"]}}
    ).json()
    return {
        "student": UNMAPPED_MALE,
        "room": row["room"],
        "index": index,
        "course": course,
        "search": search,
        "lookup": lookup,
        "roomList": room,
    }


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
    if suite == "exam-rosters":
        fixture["unmapped"] = _unmapped_answers(committee)
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
