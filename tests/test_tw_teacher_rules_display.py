"""Slice C (server half): the workspace SHOWS teaching-time rule violations.

Display only: a violation is a warning, never a refusal, and with the flag off or no
rule binding a charged teacher every answer is byte-identical.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import DeliveryBoard, Instructor, ProgrammeRequirement, SectionPlacement
from core.services import timetable_instructor_availability as helper
from core.services import timetable_workspace as tws
from core.services.course_identity import planner_course_key
from core.services.instructor_availability import (
    KIND_UNAVAILABLE,
    TimeRule,
    compile_rules,
    mask_from_days,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from tests import one_teacher_support as ots
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS, rule
from tests.test_instructor_availability_engine import (
    _engine_row,
    _english_board,
    _rules_outside_the_universe,
    _sunday,
)


@pytest.fixture
def av_on(settings):
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


def build_ruled(*, ruled: str = "a"):
    """AI111 (2 sections): S1 placed SUN+TUE 13:00-14:15, S2 not placed, both charged
    to Dr A by an engine row; Dr B is eligible but never charged. ``ruled`` names the
    teacher with the "no mornings" rule."""
    sc, board = ots.scenario("ruled", programs=("AI",))
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI111",
        course_name="INTRO",
        programme_term=2,
        credit_hours=3,
    )
    key = planner_course_key("AI111", "INTRO")
    ots.budget(sc, key, 2, code="AI111")
    a, b = ots.instructor("Dr A"), ots.instructor("Dr B")
    ots.link("AI", "AI111", a, role="primary")
    ots.link("AI", "AI111", b, role="co")
    s1 = ots.section(sc, key, "S1")
    s2 = ots.section(sc, key, "S2")
    ots.place(board, s1, [("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")])
    _engine_row(sc, key, "S1", a)
    _engine_row(sc, key, "S2", a)
    if ruled:
        rule({"a": a, "b": b}[ruled], KIND_UNAVAILABLE, end="13:00")
    return sc, board, key, s1, s2, a, b


def _plain_checker(charged, exempt=frozenset()):
    ruled = compile_rules([TimeRule(1, KIND_UNAVAILABLE, mask_from_days(("SUN",)), 0, 13 * 60)])
    return helper.RuleChecker(ruled, charged, exempt)


# ── Commit 1: the helper's display methods ────────────────────────────────


@pytest.mark.django_db
def test_meeting_blocked_judges_one_meeting_without_the_bundle(av_on) -> None:
    """H-1: an English section's writers' check expands to the whole bundle; the
    display judges each physical meeting alone."""
    sc, _key, a = _english_board("h-1")
    checker = helper.display_rule_checker(sc.id)
    sunday = _sunday(sc, _key)

    assert checker.blocked(sunday, "SUN", "13:00", "16:00") == (a.pk,)  # the bundle has THU
    assert checker.meeting_blocked((a.pk,), "SUN", "13:00", "16:00") == ()
    assert checker.meeting_blocked((a.pk,), "THU", "13:00", "16:00") == (a.pk,)
    assert checker.meeting_blocked((a.pk,), "THU", "12:00", "13:00") == ()  # touching
    assert checker.meeting_blocked((a.pk,), "THU", "bad", "16:00") == ()  # does not read
    assert checker.meeting_blocked((), "THU", "13:00", "16:00") == ()


def test_section_ids_follow_the_charged_teacher_and_the_exemption() -> None:
    """H-2: only a CHARGED, ruled teacher counts; an exempt section has none."""
    checker = _plain_checker(
        {"K_S1": frozenset({1}), "K_S3": frozenset({1}), "K_S4": frozenset({2})},
        frozenset({("K", "S3")}),
    )
    assert checker.section_ids("K", "S1") == (1,)
    assert checker.section_ids("K", "TBD") == ()  # charged to nobody
    assert checker.section_ids("K", "S4") == ()  # charged teacher has no rule
    assert checker.section_ids("K", "S3") == ()  # online on every board
    assert checker.section_ids("K", "S3", exempt=False) == (1,)  # the caller decides
    assert checker.section_ids("K", "S1", exempt=True) == ()


@pytest.mark.django_db
def test_binds_only_when_a_charged_teacher_has_a_rule(av_on) -> None:
    """H-3: a rule on an eligible-but-never-charged teacher makes a checker exist
    but does not bind; the display door then answers None."""
    sc, *_rest = build_ruled(ruled="b")
    assert helper.section_rule_checker(sc.id).binds is False
    assert helper.display_rule_checker(sc.id) is None

    from core.models import InstructorTimeRule

    InstructorTimeRule.objects.all().delete()
    ruled_a = Instructor.objects.get(full_name="Dr A")
    rule(ruled_a, KIND_UNAVAILABLE, end="13:00")
    checker = helper.display_rule_checker(sc.id)
    assert checker is not None and checker.binds
    assert checker.ruled_charged == frozenset({ruled_a.pk})


@pytest.mark.django_db
def test_teacher_names_are_read_once_per_checker(av_on) -> None:
    """H-4."""
    sc, _board, _key, _s1, _s2, a, _b = build_ruled()
    checker = helper.display_rule_checker(sc.id)
    with CaptureQueriesContext(connection) as captured:
        assert checker.teacher_name(a.pk) == "Dr A"
        assert checker.teacher_name(a.pk) == "Dr A"
        assert checker.teacher_name(999999) == ""
    assert sum('FROM "instructors"' in q["sql"] for q in captured) == 1


# ── Commit 2: the door, /conflicts/ and the export ────────────────────────


@pytest.fixture
def http():
    user = User.objects.create_user(username="teacher-rules-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def conflicts(http, board) -> dict:
    response = http.get(f"/ops/tw/boards/{board.id}/conflicts/")
    assert response.status_code == 200, response.content
    return response.json()


def move(http, placement, day: str, start: str, end: str):
    return http.post(
        f"/ops/tw/placements/{placement.id}/move/",
        data=json.dumps({"day": day, "start_time": start, "end_time": end}),
        content_type="application/json",
    )


def placement_of(s1, day: str) -> SectionPlacement:
    return SectionPlacement.objects.get(term_section=s1, day=day)


def second_board(sc, s1, label: str = "Term 4", order: int = 2) -> DeliveryBoard:
    """S1 also placed on a second board at the same times (a shared meeting)."""
    board = DeliveryBoard.objects.create(
        scenario=sc, label=label, nominal_term=4, program="AI", display_order=order
    )
    ots.place(board, s1, [("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")])
    return board


@pytest.mark.django_db
def test_conflicts_list_a_meeting_outside_its_teachers_times(av_on, http) -> None:
    """C-1."""
    _sc, board, _key, s1, _s2, a, _b = build_ruled()
    sun = placement_of(s1, "SUN")
    before = conflicts(http, board)
    assert before["teacher_rule_violations"] == []  # 13:00 is a legal time

    assert move(http, sun, "SUN", "09:00", "10:15").status_code == 200
    data = conflicts(http, board)

    assert data["teacher_rule_violations"] == [
        {
            "ids": [sun.id],
            "term_section_id": s1.id,
            "instructor_id": a.pk,
            "instructor": "Dr A",
            "section": "AI111-S1",
            "day": "SUN",
            "start": "09:00",
            "end": "10:15",
            "detail": "Dr A: SUN 09:00-10:15",
        }
    ]
    assert data["summary"]["warning"] == before["summary"]["warning"] + 1
    assert data["summary"]["critical"] == before["summary"]["critical"]
    keys = list(data)
    assert keys.index("room_clashes") < keys.index("teacher_rule_violations")
    assert keys.index("teacher_rule_violations") < keys.index("summary")


@pytest.mark.django_db
def test_each_board_lists_its_own_copy_of_a_shared_meeting(av_on, http) -> None:
    """C-2: each board lists its own placement ids, sorted by day then start."""
    sc, board, _key, s1, _s2, _a, _b = build_ruled()
    other = second_board(sc, s1)
    SectionPlacement.objects.filter(term_section=s1, day="SUN").update(
        start_time="09:00", end_time="10:15"
    )
    SectionPlacement.objects.filter(term_section=s1, day="TUE", board=board).update(
        day="MON", start_time="08:00", end_time="09:15"
    )
    SectionPlacement.objects.filter(term_section=s1, day="TUE", board=other).update(
        start_time="11:00", end_time="12:15"
    )

    for board_row in (board, other):
        rows = conflicts(http, board_row)["teacher_rule_violations"]
        own = set(SectionPlacement.objects.filter(board=board_row).values_list("id", flat=True))
        assert {r["ids"][0] for r in rows} == own
        order = [("SUN", "MON", "TUE").index(r["day"]) for r in rows]
        assert order == sorted(order)
    assert [r["day"] for r in conflicts(http, board)["teacher_rule_violations"]] == ["SUN", "MON"]


@pytest.mark.django_db
def test_a_tbd_section_has_no_row_and_the_list_is_empty_not_absent(av_on, http) -> None:
    """C-3: S2 (a TBD section: no engine row, its only teacher Dr B is eligible and
    not ruled) sits at a forbidden time; Dr A's S1 is at a legal one."""
    from core.models import SectionInstructor

    sc, board, _key, _s1, s2, _a, _b = build_ruled()
    SectionInstructor.objects.filter(scenario=sc, section="S2").delete()
    ots.place(board, s2, [("MON", "09:00", "10:15")])

    data = conflicts(http, board)

    assert data["teacher_rule_violations"] == []
    assert "teacher_rule_violations" in data


STATES = (
    "flag_off_with_rules",
    "flag_on_no_rules",
    "flag_on_rules_outside_the_universe",
    "flag_on_rule_on_an_uncharged_teacher",
)


def enter_state(settings, state: str, a, b) -> None:
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = state != "flag_off_with_rules"
    if state == "flag_off_with_rules":
        rule(a, KIND_UNAVAILABLE, end="13:00")
    elif state == "flag_on_rules_outside_the_universe":
        _rules_outside_the_universe()
    elif state == "flag_on_rule_on_an_uncharged_teacher":
        rule(b, KIND_UNAVAILABLE, end="13:00")


def fetch(http, url):
    with CaptureQueriesContext(connection) as captured:
        response = http.get(url)
    assert response.status_code == 200, response.content
    return json.dumps(response.json(), ensure_ascii=False), len(captured)


#: What the door adds to a flag-on request while no rule binds, MEASURED against the
#: same request with the door patched out (the engine's own rule read serves it).
DOOR_COST = {
    "flag_on_no_rules": 0,
    "flag_on_rules_outside_the_universe": 0,
    "flag_on_rule_on_an_uncharged_teacher": 0,
}

#: What the door adds to a readiness request with a rule binding and violations on
#: every board, MEASURED: the same for 2 and 4 boards (bounded per request).
DISPLAY_COST = 4  # names + the online exemption's placed rows, boards and budgets


@pytest.mark.django_db
@pytest.mark.parametrize("state", STATES)
def test_conflicts_are_byte_identical_while_no_rule_binds(av_on, http, monkeypatch, state) -> None:
    """C-4."""
    _sc, board, _key, _s1, _s2, a, b = build_ruled(ruled=None)
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    url = f"/ops/tw/boards/{board.id}/conflicts/"
    base_json, base_queries = fetch(http, url)

    enter_state(av_on, state, a, b)
    state_json, state_queries = fetch(http, url)

    assert state_json == base_json
    assert "teacher_rule_violations" not in state_json
    if state == "flag_off_with_rules":
        assert state_queries == base_queries
        return
    monkeypatch.setattr(tws, "_display_rule_checker", lambda _sid: None)
    _patched_json, patched_queries = fetch(http, url)
    print("MEASURED", state, state_queries, patched_queries)
    assert state_queries - patched_queries == DOOR_COST[state]


def morning(s1) -> None:
    """Every placement of S1 (on every board) at a forbidden morning time."""
    SectionPlacement.objects.filter(term_section=s1).update(start_time="09:00", end_time="10:15")


def sql_reads(captured, needle: str) -> int:
    return sum(
        needle in q["sql"] and q["sql"].lstrip().upper().startswith("SELECT") for q in captured
    )


@pytest.mark.django_db
@pytest.mark.parametrize("extra_boards", [1, 3])
def test_the_display_costs_a_bounded_number_of_reads_per_request(
    av_on, http, monkeypatch, extra_boards
) -> None:
    """C-5: with a rule binding and violations on every board, the readiness request
    reads the rule table, the teacher rows and the teacher names once, and the door
    adds a fixed number of reads however many boards there are."""
    sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    for index in range(extra_boards):
        second_board(sc, s1, label=f"Term {index + 4}", order=index + 2)
    morning(s1)
    url = f"/ops/tw/scenarios/{sc.id}/readiness/"
    http.get(url)  # warm the session and the content types

    with CaptureQueriesContext(connection) as captured:
        assert http.get(url).status_code == 200
    monkeypatch.setattr(tws, "_display_rule_checker", lambda _sid: None)
    with CaptureQueriesContext(connection) as patched:
        assert http.get(url).status_code == 200

    assert sql_reads(captured, '"instructor_time_rules"') == 1
    assert sql_reads(captured, '"section_instructors"') == 1
    assert (
        sql_reads(captured, 'AS "pk", "instructors"."full_name" AS "full_name" FROM "instructors"')
        == 1
    )
    print("MEASURED C-5", extra_boards, len(captured) - len(patched))
    assert len(captured) - len(patched) == DISPLAY_COST


@pytest.mark.django_db
def test_a_manual_move_into_a_forbidden_time_is_saved_and_then_shown(av_on, http, settings) -> None:
    """C-6 (conflicts half): the move answers exactly as with the flag off, is saved,
    and the conflicts list then shows it."""
    sc, board, _key, s1, _s2, _a, _b = build_ruled()
    sun = placement_of(s1, "SUN")
    on = move(http, sun, "SUN", "09:00", "10:15")
    assert on.status_code == 200, on.content
    assert SectionPlacement.objects.get(pk=sun.pk).start_time == "09:00"
    assert [r["ids"] for r in conflicts(http, board)["teacher_rule_violations"]] == [[sun.id]]

    # The same move with the flag off (a fresh scenario) is answered alike.
    SectionPlacement.objects.filter(pk=sun.pk).update(start_time="13:00", end_time="14:15")
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    off = move(http, sun, "SUN", "09:00", "10:15")
    assert off.status_code == 200
    assert sorted(off.json()) == sorted(on.json())
    assert off.json()["validation"] == on.json()["validation"]
    assert "teacher_rule_violations" not in conflicts(http, board)


def cells(response) -> dict[str, list[str]]:
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(b"".join(response.streaming_content)))
    return {
        ws.title: [
            (row[0].value, row[2].value if len(row) > 2 else None)
            for row in ws.iter_rows()
            if row[0].value
        ]
        for ws in workbook.worksheets
    }


@pytest.mark.django_db
def test_the_export_lists_a_meeting_outside_teaching_times_under_its_board(av_on, http) -> None:
    """C-7: a line under the board's conflicts, and the summary's Warnings column
    carries the same count."""
    sc, board, _key, s1, _s2, _a, _b = build_ruled()
    SectionPlacement.objects.filter(term_section=s1, day="SUN").update(
        start_time="09:00", end_time="10:15"
    )
    expected = conflicts(http, board)["summary"]["warning"]

    response = http.get(f"/ops/tw/scenarios/{sc.id}/export.xlsx")
    assert response.status_code == 200
    sheets = cells(response)

    lines = [pair for rows in sheets.values() for pair in rows if pair[0] == "TEACHING TIME: Dr A"]
    assert lines == [("TEACHING TIME: Dr A", "AI111-S1 SUN 09:00-10:15")]
    assert expected == 1


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["graph/summary/", "graph/view/"])
def test_a_multi_board_view_reads_the_map_once_per_request_not_per_board(av_on, http, path) -> None:
    """R5: with the flag on (no rule) a scenario-wide view reads the teacher rows once
    per request however many boards there are, and its query count grows with the
    boards no faster than with the flag off (which has no request scope at all)."""
    sc, _board, _key, s1, _s2, _a, _b = build_ruled(ruled=None)
    url = f"/ops/tw/scenarios/{sc.id}/{path}"
    http.get(url)  # warm up

    def counts() -> tuple[int, int, int]:
        av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
        with CaptureQueriesContext(connection) as on:
            assert http.get(url).status_code == 200
        av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
        with CaptureQueriesContext(connection) as off:
            assert http.get(url).status_code == 200
        return len(on), len(off), sql_reads(on, '"section_instructors"')

    on_one, off_one, reads_one = counts()
    for index in range(2):
        second_board(sc, s1, label=f"Term {index + 4}", order=index + 2)
    on_three, off_three, reads_three = counts()

    assert reads_one == reads_three == 1
    assert on_three - on_one <= off_three - off_one
    assert on_three < off_three  # measured: the scope also spares the flag-on request reads


@pytest.mark.django_db
def test_the_display_door_never_touches_the_writers_checker_and_sees_a_write(av_on) -> None:
    """R7: inside one scope the door reuses its checker while the memoised map is the
    same object, rebuilds it once a write changed the layout, and never fills the
    writers' cached checker. A display read after a write lists the new violation."""
    from core.services.section_teacher_assignment import assignment_scope, load_assignment

    sc, board, _key, s1, _s2, a, _b = build_ruled()
    with assignment_scope(sc.id):
        first = helper.display_rule_checker(sc.id)
        assert helper.display_rule_checker(sc.id) is first  # same map: reused
        assert tws.detect_board_conflicts(board.id)["teacher_rule_violations"] == []

        SectionPlacement.objects.filter(term_section=s1, day="SUN").update(
            start_time="09:00", end_time="10:15"
        )
        load_assignment(sc.id, incomplete="collect")  # the validated read of the layout
        assert helper.display_rule_checker(sc.id) is not first  # a write: rebuilt
        second = tws.detect_board_conflicts(board.id)
        assert [r["instructor_id"] for r in second["teacher_rule_violations"]] == [a.pk]
        holder = helper._holder(sc.id)
        assert holder is not None and holder.checker is None and holder.display is not None


# ── Commit 3: publish readiness ───────────────────────────────────────────

RULE_CODE = "board_teacher_rule_violations"


def readiness(http, sc) -> dict:
    response = http.get(f"/ops/tw/scenarios/{sc.id}/readiness/")
    assert response.status_code == 200, response.content
    return response.json()["readiness"]


def rule_items(report: dict) -> list[dict]:
    return [i for i in report["warning_items"] if i["code"] == RULE_CODE]


@pytest.mark.django_db
def test_readiness_warns_once_per_physical_meeting_and_never_blocks(av_on, http, settings) -> None:
    """R-1: a meeting shared by two boards is one warning, on the first board, and
    nothing about it blocks."""
    sc, _board, _key, s1, s2, _a, _b = build_ruled()
    other = second_board(sc, s1)
    ots.place(other, s2, [("MON", "09:00", "10:15")])  # a second, board-4-only violation
    SectionPlacement.objects.filter(term_section=s1, day="SUN").update(
        start_time="09:00", end_time="10:15"
    )

    on = readiness(http, sc)
    item, later = rule_items(on)
    assert (later["board_label"], later["count"]) == ("Term 4", 1)

    assert item["board_label"] == "Term 2"
    assert item["count"] == 1
    assert item["params"] == {"teachers": ["Dr A"]}
    text = "Board “Term 2”: 1 meeting(s) outside the teacher’s teaching times."
    assert on["warnings"][on["warning_items"].index(item)] == text
    assert on["warning_items"][-2:] == [item, later]  # after every other stage, in board order

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    off = readiness(http, sc)
    assert rule_items(off) == []
    assert [i["code"] for i in on["blocker_items"]] == [i["code"] for i in off["blocker_items"]]
    assert on["ready"] == off["ready"]
    assert [i for i in on["warning_items"] if i["code"] != RULE_CODE] == off["warning_items"]


@pytest.mark.django_db
def test_board_warnings_count_only_conflict_warnings(av_on, http, monkeypatch) -> None:
    """R-2: a non-blocking overlap warning plus one violation: ``board_warnings``
    counts the overlap only; with the violation alone there is no such item."""
    sc, board, _key, s1, _s2, _a, _b = build_ruled()
    SectionPlacement.objects.filter(term_section=s1, day="SUN").update(
        start_time="09:00", end_time="10:15"
    )
    assert [i for i in readiness(http, sc)["warning_items"] if i["code"] == "board_warnings"] == []

    real = tws.detect_board_conflicts

    def with_an_overlap_warning(board_id):
        result = real(board_id)
        result["overlaps"].append({"ids": [1, 2], "severity": "warning", "same_course": False})
        result["summary"]["warning"] += 1
        return result

    monkeypatch.setattr(tws, "detect_board_conflicts", with_an_overlap_warning)
    [item] = [i for i in readiness(http, sc)["warning_items"] if i["code"] == "board_warnings"]
    assert item["count"] == 1
    assert len(rule_items(readiness(http, sc))) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("state", STATES)
def test_readiness_is_byte_identical_while_no_rule_binds(av_on, http, state) -> None:
    """R-3."""
    sc, _board, _key, _s1, _s2, a, b = build_ruled(ruled=None)
    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    url = f"/ops/tw/scenarios/{sc.id}/readiness/"
    base_json, base_queries = fetch(http, url)
    enter_state(av_on, state, a, b)
    state_json, state_queries = fetch(http, url)

    assert state_json == base_json
    assert RULE_CODE not in state_json
    if state == "flag_off_with_rules":
        assert state_queries == base_queries


@pytest.mark.django_db
def test_publish_reads_each_thing_once_and_adds_a_fixed_cost(av_on, http, monkeypatch) -> None:
    """C-5 (publish half): a refused publish (a board without placements blocks it)
    reads the rule table, the teacher rows and the names once, and the door adds the
    same fixed number of reads as for the readiness view."""
    sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    DeliveryBoard.objects.create(
        scenario=sc, label="Empty", nominal_term=6, program="AI", display_order=3
    )
    SectionPlacement.objects.filter(term_section=s1).update(start_time="09:00", end_time="10:15")
    url = f"/ops/tw/scenarios/{sc.id}/publish/"

    def publish():
        with CaptureQueriesContext(connection) as captured:
            response = http.post(url, data="{}", content_type="application/json")
        assert response.status_code == 400, response.content
        return captured

    publish()  # warm up
    captured = publish()
    monkeypatch.setattr(tws, "_display_rule_checker", lambda _sid: None)
    patched = publish()

    assert sql_reads(captured, '"instructor_time_rules"') == 1
    assert sql_reads(captured, '"section_instructors"') == 1
    assert (
        sql_reads(captured, 'AS "pk", "instructors"."full_name" AS "full_name" FROM "instructors"')
        == 1
    )
    assert len(captured) - len(patched) == DISPLAY_COST


@pytest.mark.django_db
def test_a_forbidden_move_leaves_readiness_ready_and_shows_the_item(av_on, http) -> None:
    """C-6 (readiness half, owner ruling): the move is saved; readiness is as ready as
    before and now carries the warning."""
    sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    before = readiness(http, sc)
    assert move(http, placement_of(s1, "SUN"), "SUN", "09:00", "10:15").status_code == 200
    after = readiness(http, sc)

    assert after["ready"] == before["ready"]
    assert after["blocker_items"] == before["blocker_items"]
    assert len(rule_items(after)) == 1 and rule_items(before) == []


# ── Commit 4: the two previews and the student-outcome preview ────────────


def move_preview(http, placement, query: str = "verdict=1&offer=manual") -> dict:
    response = http.get(f"/ops/tw/placements/{placement.id}/slot-candidates/?{query}")
    assert response.status_code == 200, response.content
    return response.json()


def by_slot(preview: dict) -> dict[tuple[str, str], dict]:
    return {(c["day"], c["start"]): c for c in preview["candidates"]}


def rule_kinds(candidate: dict) -> list[dict]:
    return [i for i in candidate["verdict"]["items"] if i["issue_kind"] == "teacherRule"]


@pytest.mark.django_db
def test_the_move_verdict_flags_a_forbidden_slot_as_a_warning_and_keeps_it(
    av_on, http, settings
) -> None:
    """V-1."""
    _sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    sun = placement_of(s1, "SUN")
    on = move_preview(http, sun)
    slots = by_slot(on)

    forbidden = slots[("SUN", "09:00")]
    assert rule_kinds(forbidden) == [
        {
            "issue_kind": "teacherRule",
            "severity": "warning",
            "source": "teacher_rule",
            "meeting": None,
            "other": None,
            "room": None,
            "teacher": "Dr A",
            "students": None,
            "blocked_students": None,
        }
    ]
    assert forbidden["verdict"]["severity"] == "warning"
    assert forbidden["teacher_rule_blocked"] is True
    assert rule_kinds(slots[("SUN", "14:30")]) == [] or ("SUN", "14:30") not in slots
    assert all("teacher_rule_blocked" not in c for c in on["candidates"] if not rule_kinds(c))

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = False
    off = move_preview(http, sun)
    assert set(by_slot(off)) == set(slots)  # flagged, never removed

    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = True
    assert move(http, sun, "SUN", "09:00", "10:15").status_code == 200
    moved = move_preview(http, placement_of(s1, "SUN"))
    assert [i["issue_kind"] for i in moved["current_verdict"]["items"]] == ["teacherRule"]


@pytest.mark.django_db
def test_a_forbidden_slot_ranks_below_an_otherwise_equal_slot(av_on, http) -> None:
    """V-2."""
    _sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    slots = by_slot(move_preview(http, placement_of(s1, "SUN")))
    allowed = [c for (d, s), c in slots.items() if d == "WED" and s >= "13:00"]
    forbidden = slots[("WED", "09:00")]
    assert allowed and rule_kinds(forbidden)
    assert all(forbidden["verdict_rank"] > c["verdict_rank"] for c in allowed)


def planned_preview(http, board, section: str, key: str, extra: str = "verdict=1") -> dict:
    response = http.get(
        f"/ops/tw/boards/{board.id}/planned-slot-candidates/"
        f"?course_code=AI111&course_key={key}&section_label={section}&{extra}&limit=400"
    )
    assert response.status_code == 200, response.content
    return response.json()


def morning_meetings(candidate: dict) -> list[int]:
    return [i for i, m in enumerate(candidate["meetings"]) if m["start"] < "13:00"]


@pytest.mark.django_db
def test_the_place_verdict_tags_the_forbidden_meeting_of_a_pattern(av_on, http) -> None:
    """V-3: S2 (an existing section with a stored teacher) offered whole patterns."""
    _sc, board, key, _s1, _s2, _a, _b = build_ruled()
    patterns = [
        c for c in planned_preview(http, board, "S2", key)["candidates"] if c.get("meetings")
    ]
    assert patterns
    flagged = [c for c in patterns if morning_meetings(c)]
    clean = [c for c in patterns if not morning_meetings(c)]
    assert flagged and clean
    for candidate in flagged:
        items = rule_kinds(candidate)
        assert sorted(i["meeting"] for i in items) == morning_meetings(candidate)
        assert candidate["teacher_rule_blocked"] is True
    for candidate in clean:
        assert rule_kinds(candidate) == [] and "teacher_rule_blocked" not in candidate


@pytest.mark.django_db
def test_a_new_label_gets_no_teacher_rule_item_and_no_field(av_on, http) -> None:
    """V-3b (R2): creation re-derives the teacher, so the label's current pick is
    not who the created section will be charged to."""
    _sc, board, key, _s1, _s2, _a, _b = build_ruled()
    preview = planned_preview(http, board, "S9", key)
    assert preview["candidates"]
    assert all(
        rule_kinds(c) == [] and "teacher_rule_blocked" not in c for c in preview["candidates"]
    )


@pytest.mark.django_db
def test_a_created_section_is_judged_like_its_verdict(av_on, http) -> None:
    """P-2: create S2 at a forbidden morning; the conflicts list shows it (its stored
    teacher is Dr A), exactly where the verdict said so."""
    _sc, board, key, _s1, s2, _a, _b = build_ruled()
    patterns = [
        c for c in planned_preview(http, board, "S2", key)["candidates"] if c.get("meetings")
    ]
    target = next(c for c in patterns if morning_meetings(c))
    assert rule_kinds(target)

    response = http.post(
        "/ops/tw/placements/create-planned/",
        json.dumps(
            {
                "board_id": board.id,
                "course_code": "AI111",
                "course_key": key,
                "section_label": "S2",
                "capacity": 30,
                "meetings": [
                    {"day": m["day"], "start_time": m["start"], "end_time": m["end"]}
                    for m in target["meetings"]
                ],
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    rows = [
        r for r in conflicts(http, board)["teacher_rule_violations"] if r["section"] == "AI111-S2"
    ]
    assert len(rows) == len(morning_meetings(target))


@pytest.mark.django_db
def test_an_online_course_is_not_flagged_where_it_would_be_exempt(av_on, http) -> None:
    """V-4: S2's course is online for the board and S2 has no copy elsewhere: it is not
    bound there; with a campus copy on a board where the course is not online it is."""
    sc, board, key, _s1, _s2, a, _b = build_ruled()
    ProgrammeRequirement.objects.filter(course_code="AI111").update(is_online=True)
    preview = planned_preview(http, board, "S2", key)
    assert all(
        rule_kinds(c) == [] and "teacher_rule_blocked" not in c for c in preview["candidates"]
    )


@pytest.mark.django_db
def test_the_legacy_field_is_the_writers_check(av_on, http) -> None:
    """V-5: for every candidate of the plain move preview, the field is exactly what the
    bulk writers' ``checker.blocked`` says."""
    sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    sun = SectionPlacement.objects.select_related("board", "term_section").get(
        term_section=s1, day="SUN"
    )
    checker = helper.display_rule_checker(sc.id)
    plain = move_preview(http, sun, "")
    assert plain["candidates"]
    for candidate in plain["candidates"]:
        expected = bool(
            checker.blocked(sun, candidate["day"], candidate["start"], candidate["end"])
        )
        assert ("teacher_rule_blocked" in candidate) == expected
    assert any("teacher_rule_blocked" in c for c in plain["candidates"])


# ── V-6: while no rule binds the previews answer the recorded oracle ──────

from tests.test_tw_slot_verdict import ORACLE, _teacher_mode  # noqa: E402
from tests.test_tw_slot_verdict import flags as slot_flags  # noqa: E402,F401
from tests.tw_slot_verdict_support import (  # noqa: E402
    build_rich_fixture,
    legacy_requests,
    normalise_ids,
)

#: The reads a flag-on state adds to the recorded flag-off count, MEASURED, per
#: request: the engine's own rule read (one SELECT of ``instructor_time_rules``)
#: for the move preview, and that plus the layout fingerprint read the planned
#: preview's new request scope makes at its first map read for the planned one.
ORACLE_EXTRA = {
    "flag_on_no_rules": {"move": 1, "planned_single": 2, "planned_pattern": 2},
    "flag_on_rules_outside_the_universe": {"move": 1, "planned_single": 2, "planned_pattern": 2},
    # The spare teacher is one more eligible teacher the ENGINE reads (not the door):
    # that state is compared with the door patched out instead (DOOR_COST 0).
    "flag_on_rule_on_an_uncharged_teacher": None,
}


def _rich_state(settings, state: str) -> None:
    _teacher_mode(settings, "one_teacher_on")
    settings.TIMETABLE_INSTRUCTOR_AVAILABILITY = state != "flag_off_with_rules"
    if state == "flag_off_with_rules":
        rule(Instructor.objects.get(full_name="Dr A"), KIND_UNAVAILABLE, end="13:00")
    elif state == "flag_on_rules_outside_the_universe":
        _rules_outside_the_universe()
    elif state == "flag_on_rule_on_an_uncharged_teacher":
        spare = ots.instructor("Dr Spare")  # eligible for AI333 (taught by Dr D), never charged
        ots.link("AI", "AI333", spare, role="co")
        rule(spare, KIND_UNAVAILABLE, end="13:00")


@pytest.mark.django_db
@pytest.mark.parametrize("state", STATES)
def test_previews_answer_the_recorded_oracle_while_no_rule_binds(
    slot_flags,  # noqa: F811
    http,
    monkeypatch,
    state,
) -> None:
    """V-6 (and F13): JSON byte for byte as recorded; flag off costs exactly the
    recorded queries; flag on costs the measured extra (``ORACLE_EXTRA``)."""
    rich = build_rich_fixture()
    _rich_state(slot_flags, state)
    oracle = ORACLE["one_teacher_on"]
    if state == "flag_on_rule_on_an_uncharged_teacher":
        assert helper.display_rule_checker(rich.sc.id) is None  # nothing charged binds
    verdict = {}
    for name, url in legacy_requests(rich).items():
        with CaptureQueriesContext(connection) as captured:
            response = http.get(url)
        assert response.status_code == 200
        got = normalise_ids(json.loads(response.content), rich.ids())
        assert json.dumps(got, ensure_ascii=False) == json.dumps(oracle[name], ensure_ascii=False)
        if ORACLE_EXTRA.get(state, 0) is None:
            with monkeypatch.context() as patch:
                patch.setattr(tws, "_display_rule_checker", lambda _sid: None)
                with CaptureQueriesContext(connection) as patched:
                    http.get(url)
            assert len(captured) == len(patched)  # the door adds nothing
        else:
            extra = ORACLE_EXTRA[state][name] if state in ORACLE_EXTRA else 0
            assert len(captured) == oracle["queries"][name] + extra
        verdict[name] = http.get(f"{url}{'&' if '?' in url else '?'}verdict=1").json()
    VERDICTS.setdefault(state, verdict)
    if len(VERDICTS) == len(STATES):  # every state answered: the verdicts are equal
        first = json.dumps(normalise_ids(VERDICTS[STATES[0]], rich.ids()), sort_keys=True)
        assert all(
            json.dumps(normalise_ids(v, rich.ids()), sort_keys=True) == first
            for v in VERDICTS.values()
        )


VERDICTS: dict[str, dict] = {}


@pytest.mark.django_db
@pytest.mark.parametrize("target", [("SUN", "09:00", "10:15"), ("SUN", "14:30", "15:45")])
def test_the_verdict_matches_conflicts_and_readiness_after_a_manual_move(
    av_on, http, target
) -> None:
    """P-1: the preview's verdict, the conflicts list and the readiness warning agree,
    and a warning kind never becomes a blocker."""
    sc, board, _key, s1, _s2, _a, _b = build_ruled()
    sun = placement_of(s1, "SUN")
    day, start, end = target
    candidate = by_slot(move_preview(http, sun))[(day, start)]
    in_verdict = bool(rule_kinds(candidate))
    blockers_before = readiness(http, sc)["blocker_items"]

    assert move(http, sun, day, start, end).status_code == 200

    in_conflicts = any(
        r["ids"] == [sun.id] for r in conflicts(http, board)["teacher_rule_violations"]
    )
    after = readiness(http, sc)
    in_readiness = bool(rule_items(after))
    assert in_verdict == in_conflicts == in_readiness == (start == "09:00")
    assert after["blocker_items"] == blockers_before


def _outcome_stubs(monkeypatch) -> None:
    """The student-outcome pipeline needs real student data; the rule enrichment is
    independent of it, so the heavy evaluator calls are stubbed."""
    from types import SimpleNamespace

    from core.services import timetable_move_outcome as tmo

    stubs = {
        "build_student_profiles_for_scenario": lambda _sid: ["profile"],
        "build_section_states_for_scenario": lambda _sid: [object()],
        "build_course_rigidity_for_scenario": lambda _sid: {},
        "evaluate_generated_timetable_candidate": lambda **_k: SimpleNamespace(quality_score={}),
        "apply_candidate_moves": lambda sections, _placements, _moves: sections,
        "student_statuses": lambda *_a, **_k: {},
        "summarise_evaluation": lambda *_a, **_k: {},
        "compare_evaluations": lambda **_k: {},
        "outcome_tone": lambda _outcome: "neutral",
        "outcome_badge": lambda _outcome: "neutral",
    }
    for name, stub in stubs.items():
        monkeypatch.setattr(tmo, name, stub)
    monkeypatch.setattr(tmo.ssa, "build_sections_by_id", lambda _sections: {})


@pytest.mark.django_db
def test_the_outcome_preview_marks_a_pair_whose_companion_slot_is_forbidden(
    av_on, monkeypatch
) -> None:
    """R3: a pair candidate moves a SIBLING section too; the anchor's slot is legal
    but the companion lands in a forbidden morning, so the row is marked. With the
    door returning None the rows carry no such key."""
    from core.services import timetable_move_outcome as tmo

    _sc, _board, _key, s1, _s2, _a, _b = build_ruled()
    sun, tue = placement_of(s1, "SUN"), placement_of(s1, "TUE")
    _outcome_stubs(monkeypatch)
    pair = {"placement_id": tue.id, "day": "TUE", "start": "09:00", "end": "10:15"}
    moves = [
        {"day": "SUN", "start": "13:00", "end": "14:15", "pair": pair},
        {"day": "SUN", "start": "14:30", "end": "15:45"},
    ]

    rows = tmo.preview_placement_student_outcome_candidates(sun.id, candidate_moves=moves)[
        "candidates"
    ]
    marked = {(r["day"], r["start"]): r.get("teacher_rule_blocked") for r in rows}
    assert marked == {("SUN", "13:00"): True, ("SUN", "14:30"): None}

    monkeypatch.setattr(tws, "_display_rule_checker", lambda _sid: None)
    rows = tmo.preview_placement_student_outcome_candidates(sun.id, candidate_moves=moves)[
        "candidates"
    ]
    assert all("teacher_rule_blocked" not in r for r in rows)


@pytest.mark.django_db
def test_an_english_section_previews_judge_the_move_by_its_whole_bundle(av_on, http) -> None:
    """V-5 (policy path): the move preview of an English section answers through the
    planned preview's patterns; each candidate's field is exactly the writers'
    ``checker.blocked`` of the anchor's move."""
    sc, key, a = _english_board("v-5 english")
    sunday = _sunday(sc, key)
    checker = helper.display_rule_checker(sc.id)
    preview = move_preview(http, sunday, "")
    assert preview["candidates"]
    for candidate in preview["candidates"]:
        expected = bool(
            checker.blocked(sunday, candidate["day"], candidate["start"], candidate["end"])
        )
        assert ("teacher_rule_blocked" in candidate) == expected
    assert any("teacher_rule_blocked" in c for c in preview["candidates"])


def test_every_tbd_reason_the_server_sends_has_words():
    """T-1: the shared JS words every reason the server can send as a TBD teacher."""
    import re
    from pathlib import Path

    from core.services.section_teacher_assignment import (
        AVAILABILITY_REASON,
        REASON_INCOMPLETE_PATTERN,
        TBD_REASONS,
    )

    source = Path("static/js/timetable-section-teacher.js").read_text(encoding="utf-8")
    body = source.split("const REASONS = {", 1)[1].split("};", 1)[0]
    keys = set(re.findall(r"^    (\w+): ", body, flags=re.MULTILINE))
    assert keys >= set(TBD_REASONS) | {AVAILABILITY_REASON, REASON_INCOMPLETE_PATTERN}
