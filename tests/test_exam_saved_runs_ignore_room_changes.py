"""Changing the rooms changes no saved exam timetable (owner decision 2, 2026-09-28).

The exam timetable now seats by ``Room.exam_capacity``. A saved timetable
keeps the seats, building and floor each of its rooms had when it was built
(``room_capacity``, ``building`` and ``floor`` on every room row), and every
way of reading a saved run reads those - never the live rooms table. So runs
are built and saved on rooms whose exam seats equal their capacity, and each
saved-run output is snapshotted: the history list, the run detail the page
loads, the page itself, the master Excel, the Department files (EN/AR), the
student-data export (EN/AR, every layout), the Student lists (navigator,
room / course / section scopes, the student lookup) and a copy of the run.
Then several rooms' exam seats change (up, down, to none), one room's teaching
capacity, one room's building and floor, and two rooms are added - and every
output must come back byte for byte, the saved rows untouched, and the
deploy-time ``normalise_exam_runs`` must have nothing to do.

What does change, on purpose: a Check of a saved run reads the room inventory
afresh, so it reports that the inputs changed since the save - the room
inventory really did - and a Save of the old review is refused. Neither
writes anything.
"""

from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.db.models import F
from django.urls import reverse

from core import models
from core.models import ExamTimetableRun, Room
from core.services import exam_room_allocation
from core.services import exam_roster_view as rv
from core.services.exam_department_export import (
    department_export_options,
    export_department_workbooks,
)
from core.services.exam_rosters import build_roster_model
from core.services.exam_run_schema import EXAM_RUN_SCHEMA_VERSION, load_normalised_run
from core.services.exam_student_export import (
    EmptyScope,
    parse_export_options,
    prepare_export,
    render_export,
)
from core.services.exam_timetable import build_exam_timetable, export_exam_timetable_xlsx
from tests import exam_linked_parity_corpus as base
from tests import exam_linked_rooms_corpus as corpus

pytestmark = pytest.mark.django_db

#: Keys of a Student lists answer that say when the lists were read, not what they hold.
_WHEN = ("checked_at", "cached", "lists_checked_at")
_CSRF = (
    re.compile(r'(djCsrfToken = ")[^"]*(")'),
    re.compile(r'(name="csrfmiddlewaretoken" value=")[^"]*(")'),
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    """No period another test roomed, and no Student lists another test read."""
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    rv.ROSTER_VIEW_CACHE.clear()
    yield
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    rv.ROSTER_VIEW_CACHE.clear()


@pytest.fixture
def admin(client, django_user_model, monkeypatch, settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setattr("core.authz._rate_buckets", {})
    client.force_login(django_user_model.objects.create_superuser(username="rooms-saved"))
    return client


# ── building and saving ─────────────────────────────────────────────────────


def _loaded_payload(run: ExamTimetableRun, label: str) -> dict:
    """What the page sends to Check or Save a loaded run, unchanged."""
    data = load_normalised_run(run)
    return {
        "label": label,
        "previous_run_id": run.pk,
        "base_schedule": data["schedule"],
        "days": corpus.POPULATION_DAYS,
        "periods": corpus.POPULATION_PERIODS,
        "max_per_day": 2,
        "selected_courses": data["courses"],
        "assign_rooms": True,
        "thin_conflict_threshold": 0,
        "editor_revision": 1,
    }


def _check(client, payload: dict) -> dict:
    response = client.post(
        reverse("exam_timetable_draft_impact"), payload, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    return response.json()


def _save(client, payload: dict, fingerprint: str, status: int = 200) -> dict:
    response = client.post(
        reverse("exam_timetable_build"),
        {**payload, "mode": "save_loaded_changes", "expected_input_fingerprint": fingerprint},
        content_type="application/json",
    )
    assert response.status_code == status, response.content
    return response.json()


def _saved_runs(client) -> list[ExamTimetableRun]:
    """A build with rooms and the invigilator pass; its board checked and saved
    as Save saves it; a build with a linked exam sharing rooms; a copy."""
    corpus.create_rooms_population(models)
    assert not Room.objects.exclude(exam_capacity=F("capacity")).exists()
    api = SimpleNamespace(build_exam_timetable=build_exam_timetable)
    result, built = corpus.build_population_run(api, models)

    payload = _loaded_payload(built, "Checked and saved")
    check = _check(client, payload)
    assert check["source_inputs_changed"] is False
    saved = _save(client, payload, check["input_fingerprint"])

    linked = build_exam_timetable(
        "Linked",
        days=corpus.POPULATION_DAYS,
        periods=corpus.POPULATION_PERIODS,
        max_per_day=2,
        programs=["AI", "CS", "IS"],
        seed=7,
        assign_rooms=True,
        rebalance_invigilators=True,
        thin_conflict_threshold=0,
        # The two DATA STRUCTURES exams, one sitting: they may share rooms.
        linked_exams=[
            {
                "members": [
                    {"course_identity": entry["course_identity"]}
                    for entry in result["schedule"]
                    if entry["course_code"] in {"AI212", "AI225"}
                ]
            }
        ],
    )
    assert linked["status"] == "ok", linked

    copied = _post_json(client, reverse("exam_timetable_copy", args=[built.pk]), {})
    assert copied.status_code == 201, copied.content
    ids = [built.pk, saved["run_id"], linked["run_id"], copied.json()["run_id"]]
    return list(ExamTimetableRun.objects.filter(pk__in=ids).order_by("pk"))


def _change_the_rooms() -> None:
    """Exam seats down, to none and up; a teaching capacity; a building and
    floor; and two new rooms, one given exam seats and one not."""
    changes = {"M12-0": 3, "M25-1": 0, "F25-0": 1, "F35-0": 60, "M50-0": 80}
    for code, exam_seats in changes.items():
        assert Room.objects.filter(room_code=code).update(exam_capacity=exam_seats) == 1
    assert Room.objects.filter(room_code="F12-1").update(capacity=200) == 1
    assert Room.objects.filter(room_code="M35-1").update(building="Annex", floor=9) == 1
    Room.objects.create(room_code="M-NEW", capacity=45, section="M", building="New", floor=1)
    Room.objects.create(
        room_code="F-NEW", capacity=45, exam_capacity=10, section="F", building="New", floor=1
    )
    assert Room.objects.exclude(exam_capacity=F("capacity")).count() == 7


# ── every output of a saved run ─────────────────────────────────────────────


def _content(response) -> bytes:
    assert response.status_code == 200, response
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content


def _page(client, url: str) -> str:
    text = _content(client.get(url)).decode("utf-8")
    for pattern in _CSRF:
        text = pattern.sub(r"\1\2", text)
    return text


def _lists_answer(response) -> dict:
    body = json.loads(_content(response))
    for key in _WHEN:
        body.pop(key, None)
    return body


def _post_json(client, url: str, payload: dict):
    return client.post(url, json.dumps(payload), content_type="application/json")


def _student_exports(run: ExamTimetableRun, *, every_layout: bool) -> dict[str, str]:
    """The student-data export from the service, with a fixed clock and
    reference (the endpoint stamps both): the whole run in English and Arabic,
    and with ``every_layout`` one file, Summaries only, flagged rows, and a
    day, a room and a section, the languages taken in turn."""
    model = build_roster_model(run, now=corpus.FIXED_AT)
    layouts = [
        ("en", "all", {"scope": {"kind": "all"}, "one_file_per_group": True}),
        ("ar", "all", {"scope": {"kind": "all"}, "one_file_per_group": True}),
    ]
    if every_layout:
        slot, room = sorted(model.saved.rooms)[0]
        exam, groups = sorted(model.saved.groups.items())[0]
        section_key, gender = sorted(groups)[0]
        section = {"exam": exam, "section_key": section_key, "gender": gender}
        layouts += [
            ("en", "one file", {"scope": {"kind": "all"}, "one_file_per_group": False}),
            ("ar", "summaries", {"scope": {"kind": "all"}, "contents": "summary"}),
            ("en", "flagged", {"scope": {"kind": "all"}, "rows": "flagged"}),
            ("ar", "day", {"scope": {"kind": "day", "day": corpus.POPULATION_DAYS[0]}}),
            ("en", "room", {"scope": {"kind": "room", "slot_index": slot, "room_code": room}}),
            ("ar", "section", {"scope": {"kind": "section", **section}}),
        ]
    exports = {}
    for language, name, layout in layouts:
        key = f"student data {language} {name}"
        options = parse_export_options({**layout, "language": language}, model)
        try:
            prepared = prepare_export(
                model,
                options,
                generated_by="rooms",
                generated_role="EXAM_COMMITTEE",
                now=corpus.FIXED_AT,
            )
        except EmptyScope:  # a choice nobody sits is an answer too
            exports[key] = "empty"
            continue
        content, filename, _type = render_export(
            prepared, reference="EXR-ROOMS001", audit_hash="0" * 64
        )
        exports[key] = base.digest({"name": filename, "parts": corpus.archive_parts(content)})
    return exports


def _outputs(client, runs: list[ExamTimetableRun]) -> dict[str, object]:
    """Every output of every run. The first build and the linked build - the
    two whose rooms were chosen by the allocator on their own boards - also
    get every student-data layout and a Department file per group; the Save
    and the copy of the first build get the rest."""
    rv.ROSTER_VIEW_CACHE.clear()
    out: dict[str, object] = {
        "history": _content(client.get(reverse("exam_timetable_list"))),
        "page": _page(client, reverse("exam_timetable_page")),
    }
    for run in runs:
        key = f"run {run.label}"
        every_layout = run.label in {"Rooms parity", "Linked"}
        out[f"{key}: detail"] = _content(
            client.get(reverse("exam_timetable_detail", args=[run.pk]))
        )
        out[f"{key}: master excel"] = corpus.workbook_parts(
            _content(client.get(reverse("exam_timetable_export", args=[run.pk])))
        )
        path = export_exam_timetable_xlsx(run.pk)
        try:
            out[f"{key}: master excel (service)"] = corpus.workbook_parts(path.read_bytes())
        finally:
            path.unlink(missing_ok=True)
        for language in ("en", "ar"):
            options = department_export_options(load_normalised_run(run), language)
            out[f"{key}: department options {language}"] = _content(
                client.get(
                    reverse("exam_department_options", args=[run.pk]), {"language": language}
                )
            )
            request = {
                "departments": [profile["id"] for profile in options["departments"]],
                "genders": options["genders"],
                "language": language,
                "dates": {"Sun": "2026-12-06", "Mon": "2026-12-07", "Tue": "2026-12-08"},
            }
            out[f"{key}: department files {language}"] = corpus.archive_parts(
                _content(
                    _post_json(client, reverse("exam_department_export", args=[run.pk]), request)
                )
            )
        for language, gender in (("en", "M"), ("ar", "F")) if every_layout else ():
            options = department_export_options(load_normalised_run(run), language)
            content, name, _type = export_department_workbooks(
                run,
                {
                    "departments": [profile["id"] for profile in options["departments"]],
                    "genders": [gender],
                    "language": language,
                    "dates": {},
                },
            )
            out[f"{key}: department files {language} {gender}"] = {
                "name": name,
                "parts": corpus.archive_parts(content),
            }
        student = _student_exports(run, every_layout=every_layout)
        out.update({f"{key}: {name}": value for name, value in student.items()})

        # The Student lists: navigator, every room, every course, a section, a lookup.
        out[f"{key}: lists page"] = _page(client, f"{reverse('exam_rosters_page')}?run={run.pk}")
        index = _lists_answer(client.get(reverse("exam_roster_index", args=[run.pk])))
        out[f"{key}: lists index"] = index
        detail = reverse("exam_roster_detail", args=[run.pk])
        view, _cached = rv.ROSTER_VIEW_CACHE.get(run)
        scopes = [
            {"kind": "room", "slot_index": slot, "room_code": code}
            for slot, code in sorted(view.model.saved.rooms)
        ]
        scopes += [{"kind": "course", "exam": code} for code in sorted(view.model.saved.groups)]
        code, groups = sorted(view.model.saved.groups.items())[0]
        section_key, gender = sorted(groups)[0]
        scopes.append(
            {"kind": "section", "exam": code, "section_key": section_key, "gender": gender}
        )
        for scope in scopes:
            out[f"{key}: lists {json.dumps(scope, sort_keys=True)}"] = _lists_answer(
                _post_json(client, detail, {"scope": scope})
            )
        lookup = reverse("exam_roster_lookup", args=[run.pk])
        for student_id in (880000, 880001, 880077, 880149):
            out[f"{key}: lookup {student_id}"] = _lists_answer(
                _post_json(client, lookup, {"student_id": student_id})
            )
        out[f"{key}: search"] = _lists_answer(_post_json(client, lookup, {"query": "88001"}))

        # A copy holds the saved bytes and answers as the run does.
        copy = _post_json(client, reverse("exam_timetable_copy", args=[run.pk]), {"label": "Copy"})
        assert copy.status_code == 201, copy.content
        body = copy.json()
        copied = ExamTimetableRun.objects.get(pk=body.pop("run_id"))
        assert copied.result_json == run.result_json
        body.pop("created_at")
        out[f"{key}: copy"] = body
        copied.delete()
    return out


def _stored(runs: list[ExamTimetableRun]) -> dict[int, tuple]:
    return {
        row.pk: (row.label, row.created_at, row.result_json)
        for row in ExamTimetableRun.objects.filter(pk__in=[run.pk for run in runs])
    }


def test_changing_rooms_changes_no_saved_timetable(admin):
    runs = _saved_runs(admin)
    assert len(runs) == 4
    # Every saved run seated students, so every output below reads room rows.
    for run in runs:
        rows = [row for entry in json.loads(run.result_json)["schedule"] for row in entry["rooms"]]
        assert any(row["room_code"] != "UNASSIGNED" for row in rows), run.label
    stored = _stored(runs)
    before = _outputs(admin, runs)

    _change_the_rooms()
    # The change reaches a room these runs seat students in.
    seated = {
        row["room_code"]
        for run in runs
        for entry in json.loads(run.result_json)["schedule"]
        for row in entry["rooms"]
    }
    assert {"M12-0", "F25-0", "F35-0", "M35-1"} & seated
    after = _outputs(admin, runs)

    assert after.keys() == before.keys()
    changed = sorted(name for name in before if after[name] != before[name])
    assert not changed, f"saved-run outputs changed with the rooms: {changed}"
    assert _stored(runs) == stored

    # The deploy-time normaliser finds nothing to do and writes nothing.
    report = io.StringIO()
    call_command("normalise_exam_runs", stdout=report)
    assert "No rows needed migration." in report.getvalue()
    assert f"Skipped {ExamTimetableRun.objects.count()} row(s) already at v" in report.getvalue()
    assert _stored(runs) == stored
    assert all(
        json.loads(text)["schema_version"] == EXAM_RUN_SCHEMA_VERSION
        for _label, _at, text in stored.values()
    )


def test_a_check_of_a_saved_run_reports_the_changed_rooms_and_writes_nothing(admin):
    runs = _saved_runs(admin)
    built = runs[0]
    payload = _loaded_payload(built, "After the rooms changed")
    unchanged = _check(admin, payload)
    assert unchanged["source_inputs_changed"] is False
    stored, count = _stored(runs), ExamTimetableRun.objects.count()

    _change_the_rooms()
    check = _check(admin, payload)
    assert check["source_inputs_changed"] is True
    assert check["input_fingerprint"] != unchanged["input_fingerprint"]
    # A Save reviewed against the old inputs is refused.
    refused = _save(admin, payload, unchanged["input_fingerprint"], status=409)
    assert refused["error_code"] == "inputs_changed"
    assert ExamTimetableRun.objects.count() == count
    assert _stored(runs) == stored
    # The new Check seats by the new exam seats: no room past them, none in a no-seat room.
    exam_seats = dict(Room.objects.values_list("room_code", "exam_capacity"))
    seated: dict[tuple[int, str], int] = {}
    for entry in check["schedule"]:
        for row in entry["rooms"]:
            if row["room_code"] != "UNASSIGNED":
                assert row["room_capacity"] == exam_seats[row["room_code"]]
                slot = (entry["slot_index"], row["room_code"])
                seated[slot] = seated.get(slot, 0) + row["student_count"]
    assert all(count <= exam_seats[code] for (_slot, code), count in seated.items())
    assert not {code for _slot, code in seated if exam_seats[code] == 0}
