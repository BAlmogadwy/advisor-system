"""Manual move endpoint: a lock freezes time, not room; external sections stay frozen."""

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN

pytestmark = pytest.mark.django_db
SLOT = {"day": "SUN", "start_time": "09:00", "end_time": "10:15"}


@pytest.fixture
def http():
    user = User.objects.create_user(username="locked-room-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def move(http, row, payload):
    return http.post(
        f"/ops/tw/placements/{row.id}/move/", json.dumps(payload), content_type="application/json"
    )


def place(board, section, *, room="R1", locked=False, day="SUN", start="09:00", end="10:15"):
    return SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
        is_locked=locked,
    )


def state(*rows):
    return [
        SectionPlacement.objects.values_list(
            "day", "start_time", "end_time", "room", "is_locked"
        ).get(pk=row.pk)
        for row in rows
    ]


@pytest.fixture
def scenario():
    return TimetableScenario.objects.create(name="Owned")


@pytest.fixture
def board(scenario):
    return DeliveryBoard.objects.create(scenario=scenario, label="T5")


@pytest.fixture
def section(scenario):
    return TermSection.objects.create(
        scenario=scenario, course_key="AI111", course_code="AI111", section="S1"
    )


@pytest.mark.parametrize("payload", [{**SLOT, "room": "R2"}, {"room": "R2"}])
def test_locked_room_only_change_keeps_lock_and_time_without_override(
    http, board, section, payload
):
    row = place(board, section, locked=True)
    response = move(http, row, payload)
    assert response.status_code == 200, response.content
    assert response.json()["placement"]["is_locked"] is True
    assert state(row) == [("SUN", "09:00", "10:15", "R2", True)]


@pytest.mark.parametrize(
    "change",
    [
        {"day": "MON"},
        {"start_time": "10:30", "end_time": "11:45"},
        {"end_time": "10:30"},
        {"pattern_id": "ANY"},
    ],
)
def test_locked_time_change_still_requires_override(http, board, section, change):
    row = place(board, section, locked=True)
    response = move(http, row, {**SLOT, "room": "R2", **change})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PLACEMENT_LOCKED"
    assert state(row) == [("SUN", "09:00", "10:15", "R1", True)]


def test_locked_time_change_with_override_is_unchanged(http, board, section):
    row = place(board, section, locked=True)
    response = move(http, row, {**SLOT, "day": "MON", "override": True})
    assert response.status_code == 200, response.content
    assert state(row) == [("MON", "09:00", "10:15", "R1", True)]


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {**SLOT, "room": "R2"},
        {**SLOT, "room": "R2", "override": True},
        {**SLOT, "day": "MON"},
        {**SLOT, "day": "MON", "override": True},
    ],
)
def test_externally_owned_section_stays_frozen(http, board, section, locked, payload):
    other = TimetableScenario.objects.create(name="Other owner")
    other_board = DeliveryBoard.objects.create(scenario=other, label="Other")
    mine = place(board, section, locked=locked)
    theirs = place(other_board, section)
    TermSectionMeeting.objects.create(term_section=section, room="R1", instructor="T", **SLOT)
    before = list(TermSectionMeeting.objects.values())
    response = move(http, mine, payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLACEMENT_EXTERNALLY_OWNED"
    assert state(mine, theirs) == [
        ("SUN", "09:00", "10:15", "R1", locked),
        ("SUN", "09:00", "10:15", "R1", False),
    ]
    assert list(TermSectionMeeting.objects.values()) == before


def test_room_only_change_reaches_every_board_copy_and_canonical_meeting(
    http, scenario, board, section
):
    second = DeliveryBoard.objects.create(scenario=scenario, label="T7")
    anchor = place(board, section, locked=True)
    copy = place(second, section)
    elsewhere = place(second, section, day="TUE")
    TermSectionMeeting.objects.create(
        term_section=section, room="R1", instructor="T", building="B", **SLOT
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="TUE", start_time="09:00", end_time="10:15", room="R1"
    )
    response = move(http, anchor, {**SLOT, "room": "R2"})
    assert response.status_code == 200, response.content
    assert {row["id"] for row in response.json()["placements"]} == {anchor.id, copy.id}
    assert state(anchor, copy, elsewhere) == [
        ("SUN", "09:00", "10:15", "R2", True),
        ("SUN", "09:00", "10:15", "R2", False),
        ("TUE", "09:00", "10:15", "R1", False),
    ]
    assert set(TermSectionMeeting.objects.values_list("day", "room", "instructor", "building")) == {
        ("SUN", "R2", "T", "B"),
        ("TUE", "R1", "", ""),
    }
    assert move(http, anchor, {**SLOT, "room": "R1"}).status_code == 200
    assert {row[3] for row in state(anchor, copy)} == {"R1"}
    assert TermSectionMeeting.objects.get(day="SUN").room == "R1"


def test_room_only_change_collapses_room_divergent_meetings(http, board, section):
    row = place(board, section, locked=True)
    for room in ("R1", "R2"):
        TermSectionMeeting.objects.create(term_section=section, room=room, instructor="T", **SLOT)
    assert move(http, row, {**SLOT, "room": "R3"}).status_code == 200
    assert list(TermSectionMeeting.objects.values_list("room", "instructor")) == [("R3", "T")]


def test_room_only_change_on_global_section_leaves_its_meetings(http, board):
    section = TermSection.objects.create(course_key="AI112", course_code="AI112", section="S1")
    row = place(board, section, locked=True)
    TermSectionMeeting.objects.create(term_section=section, room="R1", **SLOT)
    assert move(http, row, {**SLOT, "room": "R2"}).status_code == 200
    assert state(row)[0][3] == "R2"
    assert TermSectionMeeting.objects.get().room == "R1"


def test_room_only_change_still_reports_room_clashes(http, scenario, board, section):
    second = DeliveryBoard.objects.create(scenario=scenario, label="T7")
    occupant = TermSection.objects.create(
        scenario=scenario, course_key="AI113", course_code="AI113", section="S1"
    )
    other = place(second, occupant, room="R2")
    row = place(board, section, locked=True)
    response = move(http, row, {**SLOT, "room": "R2"})
    assert response.status_code == 200, response.content
    clashes = response.json()["validation"]["room_clashes"]
    assert [(item["id"], item["scope"]) for item in clashes] == [(other.id, "cross_board")]


def test_published_scenario_rejects_a_locked_room_only_change(http, scenario, board, section):
    row = place(board, section, locked=True)
    scenario.status = "published"
    scenario.save(update_fields=["status"])
    response = move(http, row, {**SLOT, "room": "R2"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    assert state(row) == [("SUN", "09:00", "10:15", "R1", True)]
