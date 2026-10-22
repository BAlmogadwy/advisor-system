"""What a placement payload says about its room, whatever the flags.

The workspace's issue model tones a card only for a reason the publish check
shares. So each payload carries the check's own facts: ``is_online`` (online
for its board), ``needs_room`` (not online, and no meeting policy or one that
uses a shared room), ``room_state`` (``assigned`` / ``unset`` / ``unassigned``,
exact as the check filters it) and ``shared_with_other_scenario``. A board's
payload reads meetings, boards and shared sections once, not per placement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core import timetable_workspace_views as views
from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="payload-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _board(name="Payload") -> DeliveryBoard:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name=name)
    return DeliveryBoard.objects.create(
        scenario=scenario, label="Term 2", nominal_term=2, program="AI", display_order=1
    )


def _place(board, code, room, section="S1", start="09:00") -> SectionPlacement:
    ts = TermSection.objects.create(
        scenario=board.scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        section=section,
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time=start, end_time="10:15", room=room
    )
    return SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time=start, end_time="10:15", room=room
    )


def _payload(http, board) -> dict[int, dict]:
    response = http.get(f"/ops/tw/boards/{board.id}/")
    assert response.status_code == 200, response.content
    return {row["id"]: row for row in response.json()["placements"]}


def test_room_state_is_exact_as_the_publish_check_filters_it(http) -> None:
    board = _board()
    real, blank, marker = (
        _place(board, "CS101", "A101"),
        _place(board, "CS102", ""),
        _place(board, "CS103", "UNASSIGNED"),
    )
    rows = _payload(http, board)
    assert [rows[p.id]["room_state"] for p in (real, blank, marker)] == [
        "assigned",
        "unset",
        "unassigned",
    ]
    assert all(
        rows[p.id]["needs_room"] and not rows[p.id]["is_online"] for p in (real, blank, marker)
    )
    assert not any(rows[p.id]["shared_with_other_scenario"] for p in (real, blank, marker))


def test_an_online_course_needs_no_room(http) -> None:
    board = _board()
    ProgrammeRequirement.objects.create(
        program="AI", course_code="GS101", course_name="GS101", programme_term=2, is_online=True
    )
    online = _place(board, "GS101", "")
    row = _payload(http, board)[online.id]
    assert (row["is_online"], row["needs_room"], row["room_state"]) == (True, False, "unset")


def test_a_policy_that_uses_no_shared_room_needs_no_room(http, monkeypatch) -> None:
    board = _board()
    english = _place(board, "ENGL101", "UNASSIGNED")
    shared_room = _place(board, "ENGL102", "UNASSIGNED")
    policies = {
        "ENGL101": SimpleNamespace(uses_shared_room=False),
        "ENGL102": SimpleNamespace(uses_shared_room=True),
    }
    monkeypatch.setattr(views, "section_policy", lambda ts: policies.get(ts.course_code))
    monkeypatch.setattr(views, "policy_payload", lambda policy, meetings: {})
    rows = _payload(http, board)
    assert (rows[english.id]["needs_room"], rows[shared_room.id]["needs_room"]) == (False, True)


def test_a_section_another_scenario_also_places_is_marked_shared(http) -> None:
    board = _board()
    shared = _place(board, "CS101", "A101")
    own = _place(board, "CS102", "A102")
    other = _board("Other")
    SectionPlacement.objects.create(
        board=other,
        term_section=shared.term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="A101",
    )
    rows = _payload(http, board)
    assert (
        rows[shared.id]["shared_with_other_scenario"],
        rows[own.id]["shared_with_other_scenario"],
    ) == (True, False)


def test_a_board_reads_its_meetings_once_however_many_placements(http) -> None:
    def queries(count: int) -> int:
        board = _board(f"Q{count}")
        for i in range(count):
            _place(board, f"CS{100 + i}", "A101", start="09:00")
        with CaptureQueriesContext(connection) as ctx:
            _payload(http, board)
        return len(ctx.captured_queries)

    assert queries(2) == queries(12)
