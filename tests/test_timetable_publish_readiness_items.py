"""Structured publish blockers/warnings and the endpoints that carry them.

``check_publish_readiness`` keeps its English ``blockers`` / ``warnings`` strings
byte for byte and adds parallel ``blocker_items`` / ``warning_items`` (a stable
``code``, the board, a headline ``count`` and ``params``) so a client can word
each in Arabic or English and link it to its board. Every branch of the check is
pinned here, string and item together, one scenario per branch so the whole
list is asserted, not "contains".
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import section_teacher_assignment as sta
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD
from core.services.timetable_section_policy import create_policy_section_bundle
from core.services.timetable_workspace import check_publish_readiness
from tests.one_teacher_support import FLAG_ON, budget, instructor, link, place, requirement, section
from tests.one_teacher_support import scenario as one_teacher_scenario

pytestmark = pytest.mark.django_db

NETS = "AI201::NEURAL_NETWORKS"
ALGEBRA = "MATH201::LINEAR_ALGEBRA"


def _scenario(**fields) -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448", term="2", name="Readiness items", **fields
    )


def _board(scenario: TimetableScenario, label: str = "Term 2", term: int = 2, order: int = 1):
    return DeliveryBoard.objects.create(
        scenario=scenario, label=label, nominal_term=term, program="AI", display_order=order
    )


def _place(
    board: DeliveryBoard,
    key: str,
    section_label: str = "S1",
    day: str = "SUN",
    start: str = "09:00",
    end: str = "10:15",
    room: str = "R1",
) -> SectionPlacement:
    code = key.split("::", 1)[0]
    ts, _ = TermSection.objects.get_or_create(
        scenario=board.scenario,
        course_key=key,
        section=section_label,
        defaults={"course_code": code, "course_number": code},
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day=day, start_time=start, end_time=end, room=room
    )
    return SectionPlacement.objects.create(
        board=board, term_section=ts, day=day, start_time=start, end_time=end, room=room
    )


def _need(scenario: TimetableScenario, keys: tuple[str, ...], students: int) -> None:
    for key in keys:
        for sid in range(1, students + 1):
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario,
                student_id=sid,
                course_key=key,
                course_code=key.split("::", 1)[0],
                primary_term=4,
            )


def _plan(scenario: TimetableScenario, code: str, planned: int, credit: int = 3, term: int = 2):
    return ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=code,
        course_code=code,
        department="AI",
        credit_hours=credit,
        planned_sections=planned,
        max_per_section=40,
        total_demand=20,
        programme_term=term,
    )


def _item(code, board=None, count=None, **params):
    return {
        "code": code,
        "board_id": board.id if board else None,
        "board_label": board.label if board else None,
        "count": count,
        "params": params,
    }


def _assert_exactly(readiness, blockers, blocker_items, warnings=(), warning_items=()):
    """Strings and items both, whole lists, same length and order."""
    assert readiness["blockers"] == list(blockers)
    assert readiness["blocker_items"] == list(blocker_items)
    assert readiness["warnings"] == list(warnings)
    assert readiness["warning_items"] == list(warning_items)
    assert readiness["ready"] is (not blockers)


# ── B1: every branch of check_publish_readiness ──────────────────────────


def test_no_boards_is_one_item_with_no_board():
    scenario = _scenario()

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["No boards in this scenario"],
        [_item("no_boards")],
    )


def test_board_without_placements():
    scenario = _scenario()
    board = _board(scenario, "Term 4", 4)

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Board 'Term 4' has no placements"],
        [_item("board_no_placements", board)],
    )


def test_same_course_overlap_is_a_critical_conflict_on_its_board():
    scenario = _scenario()
    board = _board(scenario)
    _place(board, NETS, "S1", room="R1")
    _place(board, NETS, "S2", room="R2")

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Board 'Term 2': 1 critical conflicts"],
        [_item("board_critical_conflicts", board, count=1)],
    )


def test_placements_on_blocked_slots_name_the_slots_once_each():
    scenario = _scenario(
        blocked_slots=[{"day": "SUN", "start": "09:00"}, {"day": "MON", "start": "09:00"}]
    )
    board = _board(scenario)
    # Two courses on one blocked cell and one on another: three placements,
    # two distinct slots. A free cell is not listed.
    _place(board, NETS, day="SUN", room="R1")
    _place(board, ALGEBRA, day="SUN", room="R2")
    _place(board, "AI202::VISION", day="MON", room="R3")
    _place(board, "AI203::NLP", day="TUE", room="R4")

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Board 'Term 2': 3 placement(s) on blocked slots"],
        [_item("board_blocked_slot_placements", board, count=3, slots=["MON 09:00", "SUN 09:00"])],
    )


def test_room_clash_blocks_and_suppresses_the_matching_board_warning():
    scenario = _scenario()
    board = _board(scenario)
    _place(board, NETS, room="R1")
    _place(board, ALGEBRA, room="r1")  # rooms compare case-insensitively

    readiness = check_publish_readiness(scenario.id)

    _assert_exactly(
        readiness,
        ["Board 'Term 2': 1 room conflicts"],
        [_item("board_room_conflicts", board, count=1)],
    )


def test_sections_still_without_rooms():
    scenario = _scenario()
    board = _board(scenario)
    _place(board, NETS, day="SUN", room="UNASSIGNED")
    _place(board, ALGEBRA, day="MON", room="UNASSIGNED")
    _place(board, "AI202::VISION", day="TUE", room="R1")

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Board 'Term 2': 2 sections without rooms"],
        [_item("board_sections_without_rooms", board, count=2)],
    )


def test_missing_required_section_carries_the_course_and_its_numbers():
    scenario = _scenario()
    board = _board(scenario)
    _plan(scenario, "CS113", planned=3, term=5)
    _place(board, "CS113", "S1")

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Missing required section: CS113 needs 2 more (planned 3, used 1)"],
        [
            _item(
                "missing_required_section",
                count=2,
                course_code="CS113",
                course_key="CS113",
                programme_term=5,
                planned_sections=3,
                used_sections=1,
            )
        ],
    )


def test_incomplete_pattern_links_to_the_board_the_section_sits_on():
    scenario = _scenario()
    first = _board(scenario, "Term 2", 2, 1)
    second = _board(scenario, "Term 4", 4, 2)
    _plan(scenario, "AI101", planned=1)
    _place(first, "AI101", "S1", room="R1")
    # A 4-credit course needs three meetings; one is placed, on the second board.
    _plan(scenario, "CS401", planned=1, credit=4, term=4)
    _place(second, "CS401", "S1", day="MON", start="09:00", end="10:40", room="LAB1")

    _assert_exactly(
        check_publish_readiness(scenario.id),
        ["Incomplete section pattern: CS401-S1 has 1/3 meetings"],
        [
            _item(
                "incomplete_section_pattern",
                second,
                count=2,
                course_code="CS401",
                course_key="CS401",
                section="S1",
                placed_meetings=1,
                expected_meetings=3,
            )
        ],
    )


def test_invalid_english_pattern_links_to_its_board_and_keeps_the_policy_text():
    scenario = _scenario(gender="M", programs=["AI"])
    board = _board(scenario, "English", 1)
    english = TermSection.objects.create(
        scenario=scenario, course_code="ENG101", course_key="ENG101", section="M1"
    )
    rows = create_policy_section_bundle(
        board, english, day="SUN", start_time="09:00", end_time="12:00"
    )
    rows[-1].start_time, rows[-1].end_time = "13:00", "16:00"
    rows[-1].save()

    readiness = check_publish_readiness(scenario.id)

    [text] = readiness["blockers"]
    prefix = "Invalid English section pattern ENG101-M1: "
    assert text.startswith(prefix)
    assert readiness["blocker_items"] == [
        _item(
            "invalid_section_pattern",
            board,
            course_code="ENG101",
            section="M1",
            detail=text[len(prefix) :],
        )
    ]
    assert readiness["blocker_items"][0]["params"]["detail"]


def test_warning_only_overlap_is_a_board_warning_and_does_not_block():
    scenario = _scenario()
    board = _board(scenario, "Term 4", 4)
    _need(scenario, (NETS, ALGEBRA), 25)
    _place(board, NETS, "S1", day="SUN")
    _place(board, ALGEBRA, "S1", day="SUN", room="R2")
    _place(board, NETS, "S2", day="MON")  # the 25 can route round the clash

    _assert_exactly(
        check_publish_readiness(scenario.id),
        [],
        [],
        ["Board 'Term 4': 1 warnings"],
        [_item("board_warnings", board, count=1)],
    )


@pytest.mark.parametrize("students, flagged", [(HARD_OVERLAP_THRESHOLD, True), (19, False)])
def test_cross_board_overlap_warns_from_the_threshold(students, flagged):
    scenario = _scenario()
    left = _board(scenario, "Term 2", 2, 1)
    right = _board(scenario, "Term 4", 4, 2)
    _need(scenario, (NETS, ALGEBRA), students)
    _place(left, NETS, day="SUN", room="R1")
    _place(right, ALGEBRA, day="SUN", room="R2")

    readiness = check_publish_readiness(scenario.id)

    expected = (
        [
            _item(
                "cross_board_conflicts",
                count=1,
                threshold=HARD_OVERLAP_THRESHOLD,
            )
        ]
        if flagged
        else []
    )
    assert readiness["warning_items"] == expected
    assert readiness["warnings"] == (
        [f"1 cross-board conflicts with {HARD_OVERLAP_THRESHOLD}+ shared students"]
        if flagged
        else []
    )
    assert readiness["ready"] is True


def test_items_stay_parallel_to_the_strings_across_every_stage():
    scenario = _scenario(blocked_slots=[{"day": "WED", "start": "09:00"}])
    empty = _board(scenario, "Empty", 1, 1)
    busy = _board(scenario, "Busy", 2, 2)
    _plan(scenario, "AI301", planned=2, term=2)
    _plan(scenario, "AI302", planned=1, term=2)  # never placed
    _place(busy, "AI301", "S1", day="SUN", room="R1")
    _place(busy, "AI301", "S2", day="SUN", room="R2")  # same course, same time
    _place(busy, ALGEBRA, day="WED", room="UNASSIGNED")  # blocked cell, no room

    readiness = check_publish_readiness(scenario.id)

    # Per board in board order, then the scenario-wide stages, in code order.
    assert [(i["code"], i["board_label"]) for i in readiness["blocker_items"]] == [
        ("board_no_placements", "Empty"),
        ("board_critical_conflicts", "Busy"),
        ("board_blocked_slot_placements", "Busy"),
        ("board_sections_without_rooms", "Busy"),
        ("missing_required_section", None),
    ]
    assert readiness["blockers"] == [
        "Board 'Empty' has no placements",
        "Board 'Busy': 1 critical conflicts",
        "Board 'Busy': 1 placement(s) on blocked slots",
        "Board 'Busy': 1 sections without rooms",
        "Missing required section: AI302 needs 1 more (planned 1, used 0)",
    ]
    assert len(readiness["blockers"]) == len(readiness["blocker_items"])
    assert empty.id == readiness["blocker_items"][0]["board_id"]


# ── B2 / B3: the endpoints ───────────────────────────────────────────────


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="readiness-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _publish(http, scenario):
    return http.post(
        f"/ops/tw/scenarios/{scenario.id}/publish/", data="{}", content_type="application/json"
    )


def test_readiness_endpoint_returns_the_items_with_the_strings(http):
    scenario = _scenario()
    board = _board(scenario, "Term 4", 4)

    response = http.get(f"/ops/tw/scenarios/{scenario.id}/readiness/")

    assert response.status_code == 200
    body = response.json()
    assert body["readiness"]["blockers"] == ["Board 'Term 4' has no placements"]
    assert body["readiness"]["blocker_items"] == [_item("board_no_placements", board)]
    assert body["readiness"]["warning_items"] == []
    assert body["summary"] == {"ready": False, "blockers": 1, "warnings": 0}


@pytest.mark.parametrize("blockers, noun", [(1, "blocker"), (2, "blockers")])
def test_refused_publish_counts_its_blockers_and_carries_the_items(http, blockers, noun):
    scenario = _scenario()
    boards = [_board(scenario, f"Term {n}", n, n) for n in range(1, blockers + 1)]

    response = _publish(http, scenario)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "PUBLISH_BLOCKED"
    assert error["message"] == f"Cannot publish: {blockers} {noun}"
    details = error["details"]
    assert details["blockers"] == [
        f"Board 'Term {n}' has no placements" for n in range(1, blockers + 1)
    ]
    assert details["blocker_items"] == [_item("board_no_placements", b) for b in boards]
    assert details["warnings"] == [] and details["warning_items"] == []
    scenario.refresh_from_db()
    assert scenario.status != "published"


def test_refusal_message_is_about_the_blockers_not_critical_issues(http):
    # A missing section is not a "critical issue"; the old message said it was.
    scenario = _scenario()
    board = _board(scenario)
    _plan(scenario, "CS113", planned=2)
    _place(board, "CS113", "S1")

    error = _publish(http, scenario).json()["error"]

    assert "critical" not in error["message"]
    assert error["message"] == "Cannot publish: 1 blocker"
    assert [i["code"] for i in error["details"]["blocker_items"]] == ["missing_required_section"]


def test_ready_scenario_publishes(http):
    scenario = _scenario()
    board = _board(scenario)
    _place(board, NETS)

    response = _publish(http, scenario)

    assert response.status_code == 200, response.content
    scenario.refresh_from_db()
    assert scenario.status == "published"


# ── B2: one teacher map per request ─────────────────────────────────────


@pytest.fixture
def one_teacher(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)


def _two_board_one_teacher_scenario(*, with_blocker: bool):
    sc, first = one_teacher_scenario()
    second = DeliveryBoard.objects.create(
        scenario=sc, label="Term 4", nominal_term=4, program="AI", display_order=2
    )
    key_a = requirement("AI", "AI201", "COURSE ONE")
    key_b = requirement("AI", "AI202", "COURSE TWO")
    link("AI", "AI201", instructor("Dr A"))
    link("AI", "AI202", instructor("Dr B"))
    budget(sc, key_a, 1)
    budget(sc, key_b, 2 if with_blocker else 1)  # one section never placed
    place(first, section(sc, key_a, "S1"), [("SUN", "09:00", "10:15")], room="R1")
    place(second, section(sc, key_b, "S1"), [("MON", "09:00", "10:15")], room="R2")
    sta.prepare_section_teachers(sc.id, phase="layout")
    return sc


def _teacher_row_reads(ctx) -> int:
    return sum(
        1
        for q in ctx.captured_queries
        if "section_instructors" in q["sql"] and q["sql"].lstrip().upper().startswith("SELECT")
    )


def test_readiness_reads_the_teacher_rows_once_for_all_boards(http, one_teacher):
    sc = _two_board_one_teacher_scenario(with_blocker=False)

    with CaptureQueriesContext(connection) as ctx:
        response = http.get(f"/ops/tw/scenarios/{sc.id}/readiness/")

    assert response.status_code == 200
    assert response.json()["readiness"]["ready"] is True
    assert _teacher_row_reads(ctx) == 1  # not once per board


@pytest.mark.parametrize("with_blocker, status", [(False, 200), (True, 400)])
def test_publish_reads_the_teacher_rows_once_for_all_boards(
    http, one_teacher, with_blocker, status
):
    sc = _two_board_one_teacher_scenario(with_blocker=with_blocker)

    with CaptureQueriesContext(connection) as ctx:
        response = _publish(http, sc)

    assert response.status_code == status, response.content
    assert _teacher_row_reads(ctx) == 1
