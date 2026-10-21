"""Marking a section "mainly for programme" from the workspace (R7 write, R6).

The mark is a guarded, audited manual write: the planner-job guard, the
published / ownership / shared-section refusals every manual edit has, a
programme the scenario actually has, and one audit row per real change. A lock
freezes only days and times, so a locked section may be marked. With the flag
off the endpoint refuses and no placement carries the new keys.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings

from core.models import (
    AuditLog,
    DeliveryBoard,
    PlannerJob,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_STUDENT, ROLE_SUPER_ADMIN
from tests._section_programme_fixture import build_marked_scenario

pytestmark = pytest.mark.django_db

ON = override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True)


def _client(role, name):
    user = User.objects.create_user(username=name)
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def http():
    return _client(ROLE_SUPER_ADMIN, "marks-admin")


@pytest.fixture
def board():
    scenario, board, placements = build_marked_scenario(mark="")
    return board, placements


def _mark(client, placement, value):
    return client.post(
        f"/ops/tw/placements/{placement.id}/programme-preference/",
        json.dumps({"preferred_programme": value}),
        content_type="application/json",
    )


def _stored(placement):
    return TermSection.objects.get(pk=placement.term_section_id).preferred_programme


def _audits():
    return [
        json.loads(row.details_json)
        for row in AuditLog.objects.filter(action="tw.section.programme_preference").order_by("id")
    ]


@ON
def test_a_mark_is_saved_normalised_and_audited_once_per_change(http, board) -> None:
    _board, placements = board
    s2 = placements[("AI236", "S2")]
    response = _mark(http, s2, " ai2 ")
    assert response.status_code == 200, response.content
    assert response.json() == {
        "ok": True,
        "term_section_id": s2.term_section_id,
        "preferred_programme": "AI2",
        "changed": True,
    }
    assert _stored(s2) == "AI2"
    assert _mark(http, s2, "AI2").json()["changed"] is False
    assert _mark(http, s2, "").json()["changed"] is True
    assert _stored(s2) == ""
    audits = _audits()
    assert [(row["from"], row["to"]) for row in audits] == [("", "AI2"), ("AI2", "")]
    assert audits[0] | {"from": None, "to": None} == {
        "scenario_id": s2.board.scenario_id,
        "placement_id": s2.id,
        "term_section_id": s2.term_section_id,
        "course_key": "AI236",
        "section": "S2",
        "from": None,
        "to": None,
    }


@ON
def test_a_general_advisor_may_mark_and_a_student_may_not(board) -> None:
    _board, placements = board
    s2 = placements[("AI236", "S2")]
    assert _mark(_client(ROLE_STUDENT, "marks-student"), s2, "AI2").status_code == 403
    assert _stored(s2) == ""
    assert _mark(_client(ROLE_GENERAL_ADVISOR, "marks-advisor"), s2, "AI2").status_code == 200
    assert _stored(s2) == "AI2"


@ON
def test_a_locked_section_may_be_marked(http, board) -> None:
    _board, placements = board
    s2 = placements[("AI236", "S2")]
    SectionPlacement.objects.filter(pk=s2.pk).update(is_locked=True)
    assert _mark(http, s2, "DS").status_code == 200
    assert _stored(s2) == "DS"


@ON
def test_a_published_scenario_is_refused(http, board) -> None:
    board_row, placements = board
    TimetableScenario.objects.filter(pk=board_row.scenario_id).update(status="published")
    response = _mark(http, placements[("AI236", "S2")], "AI2")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    assert _audits() == []


@ON
def test_a_section_this_scenario_does_not_own_is_marked_where_it_is_owned(http, board) -> None:
    board_row, _placements = board
    other = TimetableScenario.objects.create(name="Owner elsewhere")
    sections = [
        TermSection.objects.create(
            course_key="ENGL214", course_code="ENGL214", course_number="214", section="M1"
        ),
        TermSection.objects.create(
            scenario=other,
            course_key="MATH105",
            course_code="MATH105",
            course_number="105",
            section="S1",
        ),
    ]
    for section in sections:
        placed = SectionPlacement.objects.create(
            board=board_row,
            term_section=section,
            day="WED",
            start_time="09:00",
            end_time="10:15",
            room="R2",
        )
        response = _mark(http, placed, "AI2")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SECTION_NOT_SCENARIO_OWNED"
        assert TermSection.objects.get(pk=section.pk).preferred_programme == ""


@ON
def test_a_section_another_scenario_also_places_is_refused(http, board) -> None:
    _board, placements = board
    s2 = placements[("AI236", "S2")]
    elsewhere = DeliveryBoard.objects.create(
        scenario=TimetableScenario.objects.create(name="Copy holder"), label="Other"
    )
    SectionPlacement.objects.create(
        board=elsewhere,
        term_section_id=s2.term_section_id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )
    response = _mark(http, s2, "AI2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLACEMENT_EXTERNALLY_OWNED"
    assert _stored(s2) == ""


@ON
def test_only_the_scenarios_programmes_may_be_chosen(http, board) -> None:
    board_row, placements = board
    s2 = placements[("AI236", "S2")]
    for bad in ("CS", 5, None, ["AI2"]):
        response = _mark(http, s2, bad)
        assert response.status_code == 400, bad
        error = response.json()["error"]
        assert error["code"] == "INVALID_PROGRAMME"
        assert error["details"]["allowed"] == ["AI2", "DS"]
    # A programme the scenario seats (a shared course) counts, not only its own.
    Student.objects.filter(student_id=880001).update(program="CYS")
    assert _mark(http, s2, "CYS").status_code == 200


@ON
def test_a_running_planner_job_holds_the_scenario(http, board) -> None:
    board_row, placements = board
    PlannerJob.objects.create(
        id=uuid4(), scenario_id=board_row.scenario_id, mode="optimise_current", status="running"
    )
    response = _mark(http, placements[("AI236", "S2")], "AI2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_BUSY"
    assert _stored(placements[("AI236", "S2")]) == ""


def test_with_the_flag_off_the_endpoint_refuses(http, board) -> None:
    _board, placements = board
    response = _mark(http, placements[("AI236", "S2")], "AI2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FEATURE_DISABLED"
    assert _stored(placements[("AI236", "S2")]) == ""


def test_placements_carry_the_mark_only_under_the_flag(http, board) -> None:
    board_row, placements = board
    TermSection.objects.filter(pk=placements[("AI236", "S2")].term_section_id).update(
        preferred_programme="AI2"
    )
    global_section = TermSection.objects.create(
        course_key="ENGL214", course_code="ENGL214", course_number="214", section="M1"
    )
    SectionPlacement.objects.create(
        board=board_row,
        term_section=global_section,
        day="WED",
        start_time="09:00",
        end_time="10:15",
        room="R2",
    )
    off = http.get(f"/ops/tw/boards/{board_row.id}/").json()["placements"]
    assert all("preferred_programme" not in row for row in off)
    assert all("preferred_programme_editable" not in row for row in off)
    with ON:
        rows = http.get(f"/ops/tw/boards/{board_row.id}/").json()["placements"]
    seen = {
        (row["course_key"], row["section"]): (
            row["preferred_programme"],
            row["preferred_programme_editable"],
        )
        for row in rows
    }
    assert seen == {
        ("AI236", "S1"): ("", True),
        ("AI236", "S2"): ("AI2", True),
        ("IS124", "S1"): ("", True),
        ("ENGL214", "M1"): ("", False),
    }
