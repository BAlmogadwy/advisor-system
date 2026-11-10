"""A course a scenario teaches as two 100-minute meetings a week, on the workspace server.

Owner, 2026-11-10: a 3-credit course may be set, per scenario, to "2x100" - two
meetings a week on two different days, each in a 100-minute column (the lab-grid
times), in a LECTURE room. The scenario's budget row stores it
(``ScenarioSectionBudget.meeting_pattern``) and every engine reads it from there.
These tests pin what the workspace server makes of it:

* the budget rows name each course's weekly shape, the choices it may take and
  the minutes every engine plans it with;
* the Place dialog's preview offers a configured course whole 2 x 100 patterns,
  each meeting naming its lecture room, and changes nothing for any other course
  - CS112-style twins included, since a course is CODE::NAME, never the code;
* the page's own creates are held to the shape (``FULL_SECTION_PATTERN_REQUIRED``),
  an importer that states its count never is;
* Check warns about a placed section of another shape and about a scenario with
  no 100-minute column to place the course in, and blocks nothing;
* the weekly-meetings endpoint and Add course store it, audited;
* a course switched to 2 x 100 whose old sections are removed comes back as
  2 x 100 in lecture rooms when the missing sections are placed.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    AuditLog,
    DeliveryBoard,
    PlannerJob,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_autoplace import auto_place_board
from core.services.timetable_workspace import (
    MeetingShapeMismatch,
    check_publish_readiness,
    compute_scenario_budget,
    create_planned_section_placements,
    infer_required_meeting_count,
    preview_planned_section_slot_candidates,
)

pytestmark = pytest.mark.django_db

STAT = "STAT103::PRINCIPLES_OF_STATISTICS"
PROG_I = "CS112::PROGRAMMING_I"
PROG_II = "CS112::PROGRAMMING_II"
DEEP = "AI401::DEEP_LEARNING"
ENGLISH = "ENG101::ENGLISH_LANGUAGE_SKILLS_I"

# The default grid's 100-minute columns an automatic placer may use; the three
# online-only windows (15:50, 17:40, 19:30) are never among them.
DAYTIME_100 = {
    ("09:00", "10:40"),
    ("10:45", "12:25"),
    ("13:00", "14:40"),
    ("14:45", "16:25"),
    ("16:30", "18:10"),
}
TWO_BY_100 = (("SUN", "09:00", "10:40"), ("TUE", "09:00", "10:40"))
TWO_BY_75 = (("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15"))
MEETING_PATTERN_URL = "/ops/tw/scenarios/{}/budget/meeting-pattern/"


def _scenario(name: str = "Weekly shape", **fields) -> TimetableScenario:
    return TimetableScenario.objects.create(academic_year="1448", term="2", name=name, **fields)


def _board(scenario, label: str = "Term 2", term: int = 2, order: int = 1) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario, label=label, nominal_term=term, program="AI", display_order=order
    )


def _budget(
    scenario,
    key: str,
    *,
    credit: int = 3,
    pattern: str = "",
    planned: int = 1,
    capacity: int = 40,
    term: int = 2,
) -> ScenarioSectionBudget:
    code = key.split("::", 1)[0]
    return ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=key,
        course_code=code,
        course_name=key.split("::", 1)[-1].replace("_", " "),
        department="AI",
        credit_hours=credit,
        planned_sections=planned,
        max_per_section=capacity,
        total_demand=20 * planned,
        programme_term=term,
        meeting_pattern=pattern,
    )


def _section(scenario, key: str, label: str) -> TermSection:
    code = key.split("::", 1)[0]
    return TermSection.objects.create(
        scenario=scenario,
        course_key=key,
        course_code=code,
        course_number=code,
        course_name=key,
        section=label,
        source_tag="tw_auto",
    )


def _place(board, ts, meetings, *, locked: bool = False, room: str = "") -> list[int]:
    ids = []
    for day, start, end in meetings:
        ids.append(
            SectionPlacement.objects.create(
                board=board,
                term_section=ts,
                day=day,
                start_time=start,
                end_time=end,
                room=room,
                is_locked=locked,
            ).id
        )
        TermSectionMeeting.objects.get_or_create(
            term_section=ts, day=day, start_time=start, end_time=end, room=room, instructor=""
        )
    return ids


def _row(scenario, key: str) -> dict:
    return next(row for row in compute_scenario_budget(scenario.id) if row["course_key"] == key)


def _meetings(rows) -> list[dict]:
    return [
        {"day": day, "start_time": start, "end_time": end, "room": ""} for day, start, end in rows
    ]


def _minutes(start: str, end: str) -> int:
    def at(text: str) -> int:
        hours, minutes = text.split(":")
        return int(hours) * 60 + int(minutes)

    return at(end) - at(start)


@pytest.fixture
def http() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="weekly-shape-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _post(client: Client, url: str, payload: dict):
    return client.post(url, json.dumps(payload), content_type="application/json")


# ── the budget rows ────────────────────────────────────────────────────────


def test_every_budget_row_names_its_weekly_shape_its_choices_and_its_minutes():
    sc = _scenario()
    _budget(sc, STAT, pattern="2x100")
    _budget(sc, PROG_II)
    _budget(sc, DEEP, credit=4)
    # An English bundle wins over any shape in every engine, even one stored
    # for it (the database allows the value; only the API refuses it).
    _budget(sc, ENGLISH, pattern="2x100")

    shape_keys = (
        "meeting_pattern",
        "meeting_pattern_label",
        "meeting_pattern_label_ar",
        "meeting_pattern_choices",
        "meeting_durations",
        "required_meetings_per_section",
        "requires_full_section_pattern",
    )
    rows = {
        key: {k: _row(sc, key)[k] for k in shape_keys} for key in (STAT, PROG_II, DEEP, ENGLISH)
    }

    assert rows[STAT] == {
        "meeting_pattern": "2x100",
        "meeting_pattern_label": "2 × 100 min",
        "meeting_pattern_label_ar": "٢ × ١٠٠ دقيقة",
        "meeting_pattern_choices": ["", "2x100"],
        "meeting_durations": [100, 100],
        # Its first section is a whole pattern, never one cell.
        "required_meetings_per_section": 2,
        "requires_full_section_pattern": True,
    }
    assert rows[PROG_II] == {
        "meeting_pattern": "",
        "meeting_pattern_label": "",
        "meeting_pattern_label_ar": "",
        "meeting_pattern_choices": ["", "2x100"],
        "meeting_durations": [75, 75],
        # Unchanged: nothing placed yet reads one meeting (the one-cell drop).
        "required_meetings_per_section": 1,
        "requires_full_section_pattern": False,
    }
    assert rows[DEEP] == {
        "meeting_pattern": "",
        "meeting_pattern_label": "",
        "meeting_pattern_label_ar": "",
        "meeting_pattern_choices": [""],
        "meeting_durations": [75, 75, 100],
        "required_meetings_per_section": 3,
        "requires_full_section_pattern": True,
    }
    assert rows[ENGLISH] == {
        "meeting_pattern": "",
        "meeting_pattern_label": "",
        "meeting_pattern_label_ar": "",
        "meeting_pattern_choices": [""],
        "meeting_durations": [180, 180, 180, 180],
        "required_meetings_per_section": 4,
        "requires_full_section_pattern": True,
    }


def test_a_stored_shape_its_credit_hours_do_not_fit_reads_as_the_credit_default():
    sc = _scenario()
    _budget(sc, DEEP, credit=4, pattern="2x100")  # written past the API's check

    row = _row(sc, DEEP)

    assert (row["meeting_pattern"], row["meeting_durations"]) == ("", [75, 75, 100])
    assert row["required_meetings_per_section"] == 3


def _reads(call) -> int:
    with CaptureQueriesContext(connection) as ctx:
        call()
    return len(ctx.captured_queries)


def test_the_budget_rows_read_no_query_per_course():
    """The shape comes from the budget row already read, whatever the number of courses."""

    sc = _scenario()
    # Something placed: with nothing placed at all, the count of placed meetings
    # (an empty map) is read again for every course without a shape - older
    # behaviour this feature leaves as it was.
    _place(_board(sc), _section(sc, PROG_II, "S1"), TWO_BY_75)
    _budget(sc, STAT, pattern="2x100")
    one_course = _reads(lambda: compute_scenario_budget(sc.id))

    for number in range(4):
        _budget(sc, f"MATH10{number}::ALGEBRA_{number}", pattern="2x100")
    _budget(sc, DEEP, credit=4)
    _budget(sc, ENGLISH)

    assert _reads(lambda: compute_scenario_budget(sc.id)) == one_course


# ── how many meetings make a section ───────────────────────────────────────


def test_a_configured_course_needs_its_shapes_meetings_whatever_is_placed():
    sc = _scenario()
    board = _board(sc)
    # A section of three meetings would otherwise teach the page that the
    # course meets three times a week.
    _place(board, _section(sc, STAT, "S1"), [*TWO_BY_75, ("THU", "09:00", "10:15")])

    def count(pattern: str, key: str = STAT, credits: int = 3) -> int:
        return infer_required_meeting_count(
            sc.id, key, fallback_credit_hours=credits, meeting_pattern=pattern
        )

    assert count("2x100") == 2
    assert count("") == 3  # unchanged: read from the sections placed
    assert count("2x100", key=PROG_II) == 2  # nothing placed: the shape, not one cell
    assert count("", key=PROG_II) == 1
    assert count("2x100", credits=4) == 3  # a shape its credits do not fit is no shape
    assert count("2x100", key=ENGLISH) == 4  # the English bundle first


# ── the Place dialog's preview ─────────────────────────────────────────────


def _preview(board, key: str, *, code: str | None = None, **kwargs) -> dict:
    return preview_planned_section_slot_candidates(
        board.id,
        course_code=code or key.split("::", 1)[0],
        course_key=key,
        section_label="S1",
        **kwargs,
    )


def test_a_configured_course_is_offered_two_100_minute_lectures_on_two_days():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    answer = _preview(board, STAT)

    # The request block keeps its keys: no shape field is added to it.
    assert answer["request"] == {
        "course_code": "STAT103",
        "course_key": STAT,
        "section_label": "S1",
        "credit_hours": 3,
        "max_per_section": 40,
        "required_meetings_per_section": 2,
        "requires_full_section_pattern": True,
    }
    candidates = answer["candidates"]
    # Ten pairs of days, each in one of the five daytime 100-minute columns.
    assert answer["summary"]["total"] == len(candidates) == 50
    windows = set()
    for candidate in candidates:
        assert candidate["is_pattern"] is True and candidate["required_meetings"] == 2
        assert "room_type" not in candidate  # the room is each meeting's
        meetings = candidate["meetings"]
        assert len(meetings) == 2
        assert meetings[0]["day"] != meetings[1]["day"]
        for meeting in meetings:
            # ``kind`` is the column grid; the room is a lecture room.
            assert (meeting["kind"], meeting["room_type"]) == ("lab", "lecture")
            assert _minutes(meeting["start"], meeting["end"]) == 100
            windows.add((meeting["start"], meeting["end"]))
    assert windows == DAYTIME_100  # never an online-only evening window


def test_a_twins_preview_is_the_same_whether_or_not_its_sibling_is_configured():
    """CS112 is two courses: setting PROGRAMMING I changes nothing for PROGRAMMING II."""

    sc = _scenario()
    board = _board(sc)
    sibling = _budget(sc, PROG_I)
    _budget(sc, PROG_II)
    before = json.dumps(_preview(board, PROG_II))

    sibling.meeting_pattern = "2x100"
    sibling.save()
    after = _preview(board, PROG_II)

    assert json.dumps(after) == before
    assert after["request"]["required_meetings_per_section"] == 1
    assert {c["kind"] for c in after["candidates"]} == {"lect", "lab"}
    assert not any("room_type" in c for c in after["candidates"])
    # ... and PROGRAMMING I itself is offered its own shape.
    assert _preview(board, PROG_I)["request"]["required_meetings_per_section"] == 2


def test_a_bare_code_request_takes_the_shape_only_from_the_only_row_of_its_code():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")
    _budget(sc, PROG_I, pattern="2x100", capacity=40)  # the first CS112 row
    _budget(sc, PROG_II, capacity=27)

    # STAT103 is one course in this scenario: its bare code is that course.
    unique = _preview(board, "STAT103")
    assert unique["request"]["required_meetings_per_section"] == 2
    assert {m["room_type"] for c in unique["candidates"] for m in c["meetings"]} == {"lecture"}

    # CS112 is two: the bare code still reads the first row's numbers, as it
    # always has, but never its weekly shape.
    twin = _preview(board, "CS112")
    assert twin["request"]["max_per_section"] == 40
    assert twin["request"]["required_meetings_per_section"] == 1
    assert not any("room_type" in c for c in twin["candidates"])


def test_the_bare_code_fallback_still_costs_one_query():
    """The key-hit path reads what it read; the fallback, one query, as before."""

    sc = _scenario()
    board = _board(sc)
    _budget(sc, PROG_II)

    def reads(key: str) -> int:
        return _reads(lambda: _preview(board, key, code="CS112"))

    assert reads("CS112") == reads(PROG_II) + 1


# ── the page's own creates ─────────────────────────────────────────────────


def _create(board, key: str, rows, *, label: str = "S1", code: str | None = None, **kwargs):
    return create_planned_section_placements(
        board.id,
        course_code=code or key.split("::", 1)[0],
        course_key=key,
        course_name=key,
        section_label=label,
        capacity=40,
        meetings=_meetings(rows),
        **kwargs,
    )


def test_a_page_create_of_a_configured_course_takes_two_100_minute_meetings():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    result = _create(board, STAT, TWO_BY_100)

    assert result["required_meetings"] == 2
    assert sorted((p.day, p.start_time, p.end_time) for p in result["placements"]) == sorted(
        TWO_BY_100
    )


def test_a_page_create_in_another_shape_is_refused_and_writes_nothing():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    with pytest.raises(MeetingShapeMismatch) as refused:
        _create(board, STAT, TWO_BY_75)

    assert str(refused.value) == (
        "STAT103 meets 2 × 100 min a week in this scenario; these meetings are 75 + 75 min. "
        "Choose one of its 2 × 100 min patterns."
    )
    assert isinstance(refused.value, ValueError)
    assert not TermSection.objects.filter(scenario=sc).exists()
    assert not SectionPlacement.objects.filter(board=board).exists()


def test_an_importer_that_states_its_count_is_never_refused_for_a_shape():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    result = _create(board, STAT, TWO_BY_75, required_meetings=2)

    assert len(result["placements"]) == 2


def test_a_twin_and_an_ambiguous_bare_code_are_not_held_to_the_shape():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, PROG_I, pattern="2x100")
    _budget(sc, PROG_II)
    one_cell = (("SUN", "09:00", "10:15"),)

    assert len(_create(board, PROG_II, one_cell)["placements"]) == 1
    # Bare CS112 is two courses: no shape is guessed for it.
    assert len(_create(board, "CS112", one_cell, label="S7")["placements"]) == 1


def test_a_bare_code_of_one_course_is_held_to_its_shape():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    with pytest.raises(MeetingShapeMismatch):
        _create(board, "STAT103", TWO_BY_75)


def test_the_create_view_answers_a_shape_refusal_with_the_code_that_rereads_patterns(http):
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")

    def create(rows, label="S1"):
        return _post(
            http,
            "/ops/tw/placements/create-planned/",
            {
                "board_id": board.id,
                "course_code": "STAT103",
                "course_key": STAT,
                "section_label": label,
                "capacity": 40,
                "meetings": [{"day": d, "start_time": s, "end_time": e} for d, s, e in rows],
            },
        )

    refused = create(TWO_BY_75)
    assert refused.status_code == 400
    assert refused.json()["error"] == {
        "code": "FULL_SECTION_PATTERN_REQUIRED",
        "message": (
            "STAT103 meets 2 × 100 min a week in this scenario; these meetings are 75 + 75 min. "
            "Choose one of its 2 × 100 min patterns."
        ),
        "details": {
            "course_code": "STAT103",
            "meeting_pattern": "2x100",
            "expected_durations": [100, 100],
            "placed_minutes": [75, 75],
        },
    }
    # One cell is the count's refusal, read as before.
    short = create((("SUN", "09:00", "10:40"),))
    assert (short.status_code, short.json()["error"]["code"]) == (
        400,
        "FULL_SECTION_PATTERN_REQUIRED",
    )
    assert not SectionPlacement.objects.filter(board=board).exists()
    assert create(TWO_BY_100).status_code == 201


# ── Check ──────────────────────────────────────────────────────────────────


def _shape_items(readiness: dict) -> list[dict]:
    return [
        item
        for item in readiness["warning_items"]
        if item["code"] in {"section_meeting_pattern_mismatch", "meeting_pattern_no_window"}
    ]


def _mismatch(board, section: str, placement_ids, placed=(75, 75)) -> dict:
    return {
        "code": "section_meeting_pattern_mismatch",
        "board_id": board.id,
        "board_label": board.label,
        "count": None,
        "params": {
            "course_code": "STAT103",
            "course_key": STAT,
            "section": section,
            "placement_ids": list(placement_ids),
            "meeting_pattern": "2x100",
            "expected_label": "2 × 100 min",
            "expected_durations": [100, 100],
            "placed_minutes": list(placed),
        },
    }


def test_a_75_plus_75_section_of_a_configured_course_is_a_warning_and_blocks_nothing():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100", planned=2)
    s1 = _place(board, _section(sc, STAT, "S1"), TWO_BY_75, room="R1")
    _place(board, _section(sc, STAT, "S2"), (("MON", "13:00", "14:40"), ("WED", "13:00", "14:40")))

    readiness = check_publish_readiness(sc.id)

    assert readiness["blockers"] == []
    assert readiness["ready"] is True
    assert readiness["warnings"] == [
        "Board 'Term 2': STAT103 S1 meets 75 + 75 min a week; this scenario teaches "
        "STAT103 as 2 × 100 min"
    ]
    assert readiness["warning_items"] == [_mismatch(board, "S1", s1)]


def test_the_same_section_unconfigured_is_not_warned_about():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, planned=1)
    _place(board, _section(sc, STAT, "S1"), TWO_BY_75, room="R1")

    assert _shape_items(check_publish_readiness(sc.id)) == []


def test_a_section_with_fewer_meetings_than_the_shape_is_only_incomplete():
    sc = _scenario()
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")
    _place(board, _section(sc, STAT, "S1"), (("SUN", "09:00", "10:15"),), room="R1")

    readiness = check_publish_readiness(sc.id)

    assert [i["code"] for i in readiness["blocker_items"]] == ["incomplete_section_pattern"]
    assert readiness["blocker_items"][0]["params"]["expected_meetings"] == 2
    assert _shape_items(readiness) == []


def test_a_section_on_two_boards_is_one_warning_naming_every_placement():
    sc = _scenario()
    term2 = _board(sc)
    term4 = _board(sc, "Term 4", 4, order=2)
    _budget(sc, STAT, pattern="2x100")
    shared = _section(sc, STAT, "S1")
    on_term4 = _place(term4, shared, TWO_BY_75, room="R1")
    on_term2 = _place(term2, shared, TWO_BY_75, room="R1")

    items = _shape_items(check_publish_readiness(sc.id))

    # Shown on the first board in display order; each meeting counted once.
    assert items == [_mismatch(term2, "S1", [*on_term2, *on_term4])]


def test_a_course_with_no_100_minute_column_to_be_placed_in_is_a_warning():
    online_only = [
        {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True},
    ]
    sc = _scenario(lab_slot_config=online_only)
    board = _board(sc)
    _budget(sc, STAT, pattern="2x100")
    _budget(sc, PROG_II)
    _place(board, _section(sc, PROG_II, "S1"), TWO_BY_75, room="R1")

    readiness = check_publish_readiness(sc.id)

    assert _shape_items(readiness) == [
        {
            "code": "meeting_pattern_no_window",
            "board_id": None,
            "board_label": None,
            "count": None,
            "params": {
                "course_code": "STAT103",
                "course_key": STAT,
                "meeting_pattern": "2x100",
                "expected_label": "2 × 100 min",
                "expected_durations": [100, 100],
            },
        }
    ]
    assert (
        "STAT103 meets 2 × 100 min a week, and this scenario has no in-person 100-minute "
        "column to place it in"
    ) in readiness["warnings"]
    # The default grid has five such columns.
    sc.lab_slot_config = []
    sc.save()
    assert _shape_items(check_publish_readiness(sc.id)) == []


def test_the_shape_checks_read_nothing_more_from_the_database():
    """Check reuses the budget rows, the scenario row and the placements it read already."""

    sc = _scenario()
    board = _board(sc)
    budget = _budget(sc, STAT, planned=2)
    _place(board, _section(sc, STAT, "S1"), TWO_BY_75, room="R1")
    _place(board, _section(sc, STAT, "S2"), (("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")))
    standard = _reads(lambda: check_publish_readiness(sc.id))

    budget.meeting_pattern = "2x100"
    budget.save()

    assert len(_shape_items(check_publish_readiness(sc.id))) == 2
    assert _reads(lambda: check_publish_readiness(sc.id)) == standard


# ── the weekly-meetings endpoint ───────────────────────────────────────────


def test_the_endpoint_is_for_advisors_and_answers_post_only(http):
    sc = _scenario()
    _budget(sc, STAT)
    url = MEETING_PATTERN_URL.format(sc.id)
    body = {"course_key": STAT, "meeting_pattern": "2x100"}

    assert _post(Client(), url, body).status_code == 302  # to the login page
    plain = Client()
    plain.force_login(User.objects.create_user(username="no-role"))
    refused = _post(plain, url, body)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "ROLE_REQUIRED")
    assert http.get(url).status_code == 405
    assert ScenarioSectionBudget.objects.get(course_key=STAT).meeting_pattern == ""


def test_the_endpoint_writes_nothing_on_a_published_or_busy_scenario(http):
    sc = _scenario()
    _budget(sc, STAT)
    url = MEETING_PATTERN_URL.format(sc.id)
    body = {"course_key": STAT, "meeting_pattern": "2x100"}

    sc.status = "published"
    sc.save()
    published = _post(http, url, body)
    assert (published.status_code, published.json()["error"]["code"]) == (
        400,
        "SCENARIO_PUBLISHED",
    )

    sc.status = "draft"
    sc.save()
    PlannerJob.objects.create(id=uuid4(), scenario=sc, mode="optimise_current", status="queued")
    busy = _post(http, url, body)
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "SCENARIO_BUSY")
    assert ScenarioSectionBudget.objects.get(course_key=STAT).meeting_pattern == ""


def test_the_endpoint_refuses_an_unknown_course_and_an_unfit_value(http):
    sc = _scenario()
    _budget(sc, STAT)
    _budget(sc, DEEP, credit=4)
    _budget(sc, ENGLISH)  # a 3-credit row, so only its English bundle refuses
    url = MEETING_PATTERN_URL.format(sc.id)

    def refused(payload) -> tuple[int, str, str]:
        response = _post(http, url, payload)
        error = response.json()["error"]
        return response.status_code, error["code"], error["message"]

    assert refused({"course_key": "NOPE101", "meeting_pattern": "2x100"}) == (
        404,
        "NOT_FOUND",
        "NOPE101 is not in this scenario's section budget.",
    )
    assert refused({"meeting_pattern": "2x100"})[:2] == (404, "NOT_FOUND")
    assert refused({"course_key": STAT, "meeting_pattern": "3x50"}) == (
        400,
        "VALIDATION_MEETING_PATTERN",
        "Unknown weekly meeting pattern: '3x50'.",
    )
    assert refused({"course_key": DEEP, "meeting_pattern": "2x100"}) == (
        400,
        "VALIDATION_MEETING_PATTERN",
        "AI401 has 4 credit hours; 2 × 100 min a week is only for 3-credit courses.",
    )
    assert refused({"course_key": ENGLISH, "meeting_pattern": "2x100"}) == (
        400,
        "VALIDATION_MEETING_PATTERN",
        "ENG101 meets in its fixed English weekly pattern; its weekly meetings cannot be changed.",
    )
    assert refused({"course_key": STAT}) == (
        400,
        "VALIDATION_MEETING_PATTERN",
        'meeting_pattern is required ("" for the credit default).',
    )
    assert set(ScenarioSectionBudget.objects.values_list("meeting_pattern", flat=True)) == {""}
    assert not AuditLog.objects.filter(action="tw.scenario.budget.meeting_pattern").exists()


def test_setting_a_shape_lists_the_sections_left_in_another_shape(http):
    sc = _scenario()
    term2 = _board(sc)
    term4 = _board(sc, "Term 4", 4, order=2)
    other = _scenario("Another scenario")
    elsewhere = _board(other)
    _budget(sc, STAT, planned=5)

    s1 = _section(sc, STAT, "S1")
    s1_ids = _place(term2, s1, TWO_BY_75, locked=True)
    s2 = _section(sc, STAT, "S2")  # on two boards: one class, each meeting once
    s2_ids = _place(term2, s2, (("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")))
    s2_ids += _place(term4, s2, (("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")))
    _place(term2, _section(sc, STAT, "S3"), (("MON", "09:00", "10:40"), ("WED", "09:00", "10:40")))
    _place(term2, _section(sc, STAT, "S4"), (("THU", "09:00", "10:15"),))  # incomplete
    s5 = _section(other, STAT, "S5")  # another scenario's section, placed here too
    s5_ids = _place(term2, s5, (("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")))
    _place(elsewhere, s5, (("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")))

    response = _post(
        http, MEETING_PATTERN_URL.format(sc.id), {"course_key": STAT, "meeting_pattern": "2x100"}
    )

    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in ("ok", "course_key", "meeting_pattern", "previous", "changed")} == {
        "ok": True,
        "course_key": STAT,
        "meeting_pattern": "2x100",
        "previous": "",
        "changed": True,
    }
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key=STAT).meeting_pattern == (
        "2x100"
    )
    stat_row = next(row for row in body["budget"] if row["course_key"] == STAT)
    assert (stat_row["meeting_pattern"], stat_row["meeting_durations"]) == ("2x100", [100, 100])

    def listed(ts, board_ids, ids, *, locked=False, external=False) -> dict:
        return {
            "term_section_id": ts.id,
            "course_key": STAT,
            "course_code": "STAT103",
            "section": ts.section,
            "board_ids": board_ids,
            "placement_ids": ids,
            "placed_minutes": [75, 75],
            "locked": locked,
            "externally_owned": external,
        }

    assert body["mismatched_sections"] == [
        listed(s1, [term2.id], s1_ids, locked=True),
        listed(s2, [term2.id, term4.id], s2_ids),
        listed(s5, [term2.id], s5_ids, external=True),
    ]
    audit = AuditLog.objects.get(action="tw.scenario.budget.meeting_pattern")
    assert json.loads(audit.details_json) == {
        "scenario_id": sc.id,
        "course_key": STAT,
        "old": "",
        "new": "2x100",
        "mismatched": 3,
    }

    # The same value again changes nothing. Back to the default, the sections
    # now in another shape are the 2 x 100 ones: S3 (Check never warns about them).
    again = _post(
        http, MEETING_PATTERN_URL.format(sc.id), {"course_key": STAT, "meeting_pattern": "2x100"}
    ).json()
    assert (again["previous"], again["changed"]) == ("2x100", False)
    back = _post(
        http, MEETING_PATTERN_URL.format(sc.id), {"course_key": STAT, "meeting_pattern": ""}
    ).json()
    assert (back["previous"], back["meeting_pattern"], back["changed"]) == ("2x100", "", True)
    s3 = TermSection.objects.get(scenario=sc, course_key=STAT, section="S3")
    assert [
        (item["section"], item["placed_minutes"], item["locked"])
        for item in back["mismatched_sections"]
    ] == [(s3.section, [100, 100], False)]
    # No placement moved.
    assert SectionPlacement.objects.filter(term_section__in=[s1, s2]).count() == 6


def test_an_older_row_stored_without_a_course_key_is_found_by_its_code(http):
    sc = _scenario()
    # bulk_create skips the model's save(), which would fill the key.
    ScenarioSectionBudget.objects.bulk_create(
        [ScenarioSectionBudget(scenario=sc, course_key=None, course_code="STAT103", credit_hours=3)]
    )

    response = _post(
        http,
        MEETING_PATTERN_URL.format(sc.id),
        {"course_key": "STAT103", "meeting_pattern": "2x100"},
    )

    assert response.status_code == 200, response.content
    assert ScenarioSectionBudget.objects.get(scenario=sc).meeting_pattern == "2x100"


# ── Add course ─────────────────────────────────────────────────────────────


def test_a_new_course_is_added_with_its_weekly_meetings(http):
    sc = _scenario()
    url = f"/ops/tw/scenarios/{sc.id}/budget/"

    response = _post(
        http,
        url,
        {"course_code": "stat103", "credit_hours": 3, "sections": 2, "meeting_pattern": "2x100"},
    )

    assert response.status_code == 200, response.content
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key="STAT103").meeting_pattern == (
        "2x100"
    )
    row = response.json()["budget"][0]
    assert (row["meeting_pattern"], row["required_meetings_per_section"]) == ("2x100", 2)
    audit = AuditLog.objects.get(action="tw.scenario.budget.add")
    assert json.loads(audit.details_json) == {
        "scenario_id": sc.id,
        "course_key": "STAT103",
        "sections": 2,
        "meeting_pattern": "2x100",
    }

    plain = _post(http, url, {"course_code": "MATH101", "credit_hours": 3})
    assert plain.status_code == 200
    assert (
        ScenarioSectionBudget.objects.get(scenario=sc, course_key="MATH101").meeting_pattern == ""
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"course_code": "AI401", "credit_hours": 4, "meeting_pattern": "2x100"},
            "AI401 has 4 credit hours; 2 × 100 min a week is only for 3-credit courses.",
        ),
        (
            {"course_code": "AI402", "credit_hours": 3, "meeting_pattern": "weekly"},
            "Unknown weekly meeting pattern: 'weekly'.",
        ),
    ],
)
def test_a_new_course_with_an_unfit_pattern_is_refused(http, payload, message):
    sc = _scenario()

    response = _post(http, f"/ops/tw/scenarios/{sc.id}/budget/", payload)

    assert response.status_code == 400
    assert response.json()["error"] == {"code": "VALIDATION_BUDGET", "message": message}
    assert not sc.section_budgets.exists()


def test_more_sections_of_an_existing_course_keep_its_weekly_meetings(http):
    sc = _scenario()
    budget = _budget(sc, STAT, pattern="2x100", planned=1)
    url = f"/ops/tw/scenarios/{sc.id}/budget/"

    assert _post(http, url, {"course_key": STAT, "sections": 1}).status_code == 200
    assert _post(http, url, {"course_key": STAT, "meeting_pattern": "2X100"}).status_code == 200
    assert _post(http, url, {"course_key": STAT, "meeting_pattern": None}).status_code == 200
    budget.refresh_from_db()
    assert (budget.planned_sections, budget.meeting_pattern) == (4, "2x100")

    refused = _post(http, url, {"course_key": STAT, "meeting_pattern": ""})
    assert refused.status_code == 400
    assert refused.json()["error"] == {
        "code": "VALIDATION_BUDGET",
        "message": "Change the weekly meetings with the course's weekly-meetings control.",
    }
    budget.refresh_from_db()
    assert (budget.planned_sections, budget.meeting_pattern) == (4, "2x100")
    added = [
        json.loads(row.details_json)["meeting_pattern"]
        for row in AuditLog.objects.filter(action="tw.scenario.budget.add").order_by("id")
    ]
    assert added == ["2x100", "2x100", "2x100"]


# ── place what is missing, after the change ────────────────────────────────


def _reshaped() -> tuple[TimetableScenario, DeliveryBoard]:
    """The owner's way to re-shape a course, up to re-placing it: two 2 x 75
    sections, the course set to 2 x 100, its sections removed (what Remove does:
    the sections and their meeting rows stay, the placements go)."""

    sc = _scenario()
    board = _board(sc)
    for code in ("LEC1", "LEC2", "LEC3", "LEC4"):
        Room.objects.create(room_code=code, capacity=60, room_type="lecture", department="AI")
    Room.objects.create(room_code="LAB1", capacity=60, room_type="lab", department="AI")
    budget = _budget(sc, STAT, planned=2)
    for label, day_pair in (("S1", ("SUN", "TUE")), ("S2", ("MON", "WED"))):
        _create(
            board,
            STAT,
            [(day, "09:00", "10:15") for day in day_pair],
            label=label,
            required_meetings=2,
        )
    budget.meeting_pattern = "2x100"
    budget.save()
    SectionPlacement.objects.filter(board=board).delete()  # what Remove does
    return sc, board


def _reshaped_and_placed_again() -> tuple[TimetableScenario, dict]:
    """Re-shaped, then the missing sections placed (the compact greedy, Place
    missing's engine)."""

    sc, board = _reshaped()
    return sc, auto_place_board(board.id, strategy="compact")


def _placed(sc, label: str) -> list[tuple[str, str, str, str]]:
    return sorted(
        SectionPlacement.objects.filter(
            term_section__scenario=sc, term_section__course_key=STAT, term_section__section=label
        ).values_list("day", "start_time", "end_time", "room")
    )


def test_placing_the_missing_sections_makes_them_two_100_minute_lectures():
    sc, result = _reshaped_and_placed_again()

    assert result["placed"] == 2
    lecture_rooms = set(
        Room.objects.filter(room_type="lecture").values_list("room_code", flat=True)
    )
    for label in ("S1", "S2"):
        placed = _placed(sc, label)
        assert len(placed) == 2
        assert placed[0][0] != placed[1][0]
        assert {(start, end) for _day, start, end, _room in placed} <= DAYTIME_100
        assert {room for *_times, room in placed} <= lecture_rooms


def test_a_section_placed_again_keeps_no_meeting_rows_of_its_old_pattern():
    """The greedy reuses a freed label's section; its old 2 x 75 rows must go."""
    sc, _result = _reshaped_and_placed_again()

    for label in ("S1", "S2"):
        ts = TermSection.objects.get(scenario=sc, course_key=STAT, section=label)
        assert sorted(
            TermSectionMeeting.objects.filter(term_section=ts).values_list(
                "day", "start_time", "end_time"
            )
        ) == [(day, start, end) for day, start, end, _room in _placed(sc, label)]


def test_a_section_the_cpsat_solver_places_again_keeps_no_meeting_rows_of_its_old_pattern():
    """The CP-SAT writer (the 'optimal' strategy's) reuses a freed label the same way."""
    from core.services.timetable_solver import persist_solver_result, solve_board

    sc, board = _reshaped()
    result = solve_board(board.id, time_limit_seconds=10)
    assert result["status"] in {"optimal", "feasible"}, result.get("status")
    persist_solver_result(board.id, result)

    for label in ("S1", "S2"):
        placed = _placed(sc, label)
        assert len(placed) == 2
        assert {(start, end) for _day, start, end, _room in placed} <= DAYTIME_100
        ts = TermSection.objects.get(scenario=sc, course_key=STAT, section=label)
        assert sorted(
            TermSectionMeeting.objects.filter(term_section=ts).values_list(
                "day", "start_time", "end_time"
            )
        ) == [(day, start, end) for day, start, end, _room in placed]
