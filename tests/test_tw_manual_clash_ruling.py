"""Owner ruling 2026-10-20: a manual edit is allowed with any clash.

"For the manual move all the moves should be allowed even with any type of
clash, because all that will be seen and the one doing the move sees the
clash." Every manual write path saves a clashing edit and returns the clash:
room double-bookings (same board and across boards), instructor
double-bookings, same-course and shared-student overlaps. What still refuses
an edit is not a clash: a lock without override, another scenario owning the
section, a published scenario, a malformed or duplicate placement.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN

pytestmark = pytest.mark.django_db
SUN_9 = {"day": "SUN", "start_time": "09:00", "end_time": "10:15"}


@pytest.fixture
def http():
    user = User.objects.create_user(username="clash-ruling-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def post(http, path, payload):
    return http.post(path, json.dumps(payload), content_type="application/json")


def section(scenario, code, label="S1", instructor=""):
    ts = TermSection.objects.create(
        scenario=scenario,
        course_key=code,
        course_code=code,
        course_number=code,
        course_name=code,
        section=label,
        registered_count=20,
        source_tag="test",
    )
    if instructor:
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time="13:00", end_time="14:15", instructor=instructor
        )
    return ts


def place(board, ts, room="R1", **slot):
    return SectionPlacement.objects.create(
        board=board, term_section=ts, room=room, **{**SUN_9, **slot}
    )


@pytest.fixture
def clash_board():
    """SUN 09:00 is taken on this board by AI101-S1 (Dr A, in R1, sharing
    students with AI102) and AI102-S2, and across boards by CS200 in R5."""
    scenario = TimetableScenario.objects.create(name="Ruling", academic_year="1448", term="1")
    board = DeliveryBoard.objects.create(scenario=scenario, label="T1", program="AI")
    other = DeliveryBoard.objects.create(scenario=scenario, label="T3", program="AI")
    busy = section(scenario, "AI101", instructor="Dr A")
    place(board, busy, room="R1")
    place(board, section(scenario, "AI102", "S2"), room="")
    place(other, section(scenario, "CS200"), room="R5")
    for student_id in (1, 2, 3, 4, 5, 6):
        Student.objects.create(student_id=student_id, name=f"S{student_id}", program="AI")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["AI101", "AI102"],
        )
        for code in ("AI101", "AI102"):
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario,
                student_id=student_id,
                course_key=code,
                course_code=code,
                primary_term=1,
                status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                source="test",
            )
    return scenario, board, other


def _clashes(validation: dict) -> dict:
    return {
        "critical": validation["critical_count"],
        "instructor": len(validation["instructor_clashes"]),
        "same_course": sum(1 for o in validation["overlaps"] if o["shared_students"] is None),
        "rooms": sorted(c["scope"] for c in validation["room_clashes"]),
    }


def test_a_move_onto_every_kind_of_clash_is_saved_and_reported(http, clash_board) -> None:
    scenario, board, _other = clash_board
    # AI102-S1 (a sibling of S2), taught by Dr A too, moves into SUN 09:00 in R1.
    mover = place(board, section(scenario, "AI102", instructor="Dr A"), room="R2", day="TUE")
    response = post(http, f"/ops/tw/placements/{mover.id}/move/", {**SUN_9, "room": "R1"})
    assert response.status_code == 200, response.content
    mover.refresh_from_db()
    assert (mover.day, mover.start_time, mover.room) == ("SUN", "09:00", "R1")
    clashes = _clashes(response.json()["validation"])
    assert clashes["instructor"] == 1
    assert clashes["same_course"] == 1
    assert clashes["rooms"] == ["same_board"]
    assert clashes["critical"] >= 2
    assert response.json()["validation"]["valid"] is False


def test_a_room_change_into_a_room_taken_on_another_board_is_saved(http, clash_board) -> None:
    scenario, board, _other = clash_board
    row = place(board, section(scenario, "AI150"), room="R7")
    row.is_locked = True
    row.save(update_fields=["is_locked"])
    response = post(http, f"/ops/tw/placements/{row.id}/move/", {**SUN_9, "room": "R5"})
    assert response.status_code == 200, response.content
    row.refresh_from_db()
    assert (row.room, row.is_locked) == ("R5", True)
    clashes = response.json()["validation"]["room_clashes"]
    assert [(c["scope"], c["section"]) for c in clashes] == [("cross_board", "CS200-S1")]


def test_placing_an_existing_section_onto_a_clash_is_saved(http, clash_board) -> None:
    scenario, board, _other = clash_board
    ts = section(scenario, "AI102", "S3", instructor="Dr A")
    response = post(
        http,
        "/ops/tw/placements/create/",
        {"board_id": board.id, "term_section_id": ts.id, **SUN_9, "room": "R5"},
    )
    assert response.status_code == 201, response.content
    assert SectionPlacement.objects.filter(term_section=ts, day="SUN", room="R5").exists()
    clashes = _clashes(response.json()["validation"])
    assert clashes["instructor"] == 1 and clashes["same_course"] == 1
    assert clashes["rooms"] == ["cross_board"]


def test_creating_a_planned_section_onto_a_clash_is_saved(http, clash_board) -> None:
    _scenario, board, _other = clash_board
    response = post(
        http,
        "/ops/tw/placements/create-planned/",
        {
            "board_id": board.id,
            "course_code": "AI102",
            "section_label": "S9",
            "meetings": [{**SUN_9, "room": "R1"}],
        },
    )
    assert response.status_code == 201, response.content
    assert SectionPlacement.objects.filter(
        term_section__section="S9", day="SUN", start_time="09:00", room="R1"
    ).exists()
    validation = response.json()["validation"]
    assert validation["valid"] is False and validation["critical_count"] >= 1
    item = validation["items"][0]
    assert [c["scope"] for c in item["room_clashes"]] == ["same_board"]


def test_refusals_that_are_not_clashes_still_hold(http, clash_board) -> None:
    scenario, board, _other = clash_board
    locked = place(board, section(scenario, "AI160"), room="R8", day="WED")
    locked.is_locked = True
    locked.save(update_fields=["is_locked"])
    response = post(http, f"/ops/tw/placements/{locked.id}/move/", SUN_9)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PLACEMENT_LOCKED"
    scenario.status = "published"
    scenario.save(update_fields=["status"])
    free = place(board, section(scenario, "AI170"), room="R8", day="THU")
    response = post(http, f"/ops/tw/placements/{free.id}/move/", SUN_9)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
