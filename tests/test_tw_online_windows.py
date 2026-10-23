"""Stage 3, B4: the online-only windows, as the server says them.

The owner's rule (2026-07-28, ``timetable_autoplace.DEFAULT_LAB_SLOTS``): only
online courses meet in the three online-only windows; nothing in the estate is
open in the evening. Two server truths follow, which the pages read (D-7):

* **A meeting of a course that is not online for its board, drawn in an
  online-only window, is a publish-readiness warning**,
  ``online_window_room_class``. It never blocks publishing, and no write is
  refused for it (a manual move is never refused for a clash). Which window a
  meeting is in is the grid's own rule (``timetable_slot_config.online_window``):
  the first column of its kind with its start, never a mere overlap of times.
  An online course anywhere is not marked (owner question Q2, its default (a)).
* **The board read lists the board's online courses** (``online_course_codes``),
  placed or not, by the rule every placement's ``is_online`` uses.

Every test runs twice: with the defaults, and under the three flags production
runs with (render.yaml).
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_section_policy import create_policy_section_bundle
from core.services.timetable_workspace import check_publish_readiness

pytestmark = pytest.mark.django_db

# The flags production runs with (render.yaml).
PRODUCTION_FLAGS = {
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
    "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": True,
    "TIMETABLE_SECTION_PROGRAMME_PREFERENCE": True,
}

TAIL = "a window kept for online courses"


@pytest.fixture(params=["defaults", "production flags"], autouse=True)
def flags(request, settings):
    if request.param == "production flags":
        for name, value in PRODUCTION_FLAGS.items():
            setattr(settings, name, value)
    return request.param


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="online-window-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _online(program: str, code: str) -> None:
    ProgrammeRequirement.objects.create(
        program=program, course_code=code, course_name=code, programme_term=2, is_online=True
    )


@pytest.fixture
def ws():
    """The default columns (Labs 1-5 and the three online windows), two boards.

    Term 2 is AI's; Term 4 is shared by AI and AI2. GS104 is online for AI,
    GSE2 for AI2 only. ENG101 is flagged online too, but it is under a course
    meeting policy, so it is never an online course (``OnlineCourseLookup``).
    """
    sc = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        gender="M",
        programs=["AI", "AI2"],
        name="Online windows",
        slot_config=[],
        lab_slot_config=[],
    )
    term2 = DeliveryBoard.objects.create(
        scenario=sc, label="Term 2", nominal_term=2, program="AI", display_order=1
    )
    term4 = DeliveryBoard.objects.create(
        scenario=sc, label="Term 4", nominal_term=4, program="AI,AI2", display_order=2
    )
    _online("AI", "GS104")
    _online("AI2", "GSE2")
    _online("AI", "ENG101")
    return sc, term2, term4


def place(board, code, section, day, start, end, room="LAB1") -> SectionPlacement:
    ts, _ = TermSection.objects.get_or_create(
        scenario=board.scenario,
        course_key=code,
        section=section,
        defaults={"course_code": code, "course_number": code},
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day=day, start_time=start, end_time=end, room=room
    )
    return SectionPlacement.objects.create(
        board=board, term_section=ts, day=day, start_time=start, end_time=end, room=room
    )


def daytime(board) -> SectionPlacement:
    """A lab in Lab 1, so the board is not empty (an empty board blocks publishing)."""
    return place(board, "CS900", "S1", "SUN", "09:00", "10:40", room="LAB9")


def item(board, placement, code, section, day, label, start, end) -> dict:
    return {
        "code": "online_window_room_class",
        "board_id": board.id,
        "board_label": board.label,
        "count": None,
        "params": {
            "placement_id": placement.id,
            "course_code": code,
            "section": section,
            "day": day,
            "slot_label": label,
            "start": start,
            "end": end,
        },
    }


def online_items(readiness) -> list[dict]:
    return [i for i in readiness["warning_items"] if i["code"] == "online_window_room_class"]


# ── the warning ─────────────────────────────────────────────────────────────


def test_a_lab_in_an_online_window_is_one_warning_and_the_scenario_still_publishes(ws, http):
    sc, term2, term4 = ws
    daytime(term4)
    lab = place(term2, "CS114", "S1", "SUN", "17:40", "19:20")

    readiness = check_publish_readiness(sc.id)

    assert readiness["blockers"] == [] and readiness["blocker_items"] == []
    assert readiness["warnings"] == [
        f"Board 'Term 2': CS114 S1 meets in Online 2 (SUN 17:40-19:20), {TAIL}"
    ]
    assert readiness["warning_items"] == [
        item(term2, lab, "CS114", "S1", "SUN", "Online 2", "17:40", "19:20")
    ]
    assert readiness["ready"] is True
    # The endpoints say the same, and publishing is not refused for it.
    body = http.get(f"/ops/tw/scenarios/{sc.id}/readiness/").json()
    assert body["readiness"]["warning_items"] == readiness["warning_items"]
    assert body["summary"] == {"ready": True, "blockers": 0, "warnings": 1}
    response = http.post(
        f"/ops/tw/scenarios/{sc.id}/publish/", data="{}", content_type="application/json"
    )
    assert response.status_code == 200, response.content
    sc.refresh_from_db()
    assert sc.status == "published"


def test_a_move_into_a_window_is_never_refused_and_is_then_a_warning(ws, http):
    sc, term2, term4 = ws
    daytime(term4)
    lab = place(term2, "CS114", "S1", "THU", "09:00", "10:40")
    assert online_items(check_publish_readiness(sc.id)) == []

    response = http.post(
        f"/ops/tw/placements/{lab.id}/move/",
        {"day": "THU", "start_time": "19:30", "end_time": "21:10", "room": "LAB1"},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    lab.refresh_from_db()
    assert (lab.day, lab.start_time, lab.end_time) == ("THU", "19:30", "21:10")
    readiness = check_publish_readiness(sc.id)
    assert online_items(readiness) == [
        item(term2, lab, "CS114", "S1", "THU", "Online 3", "19:30", "21:10")
    ]
    assert readiness["ready"] is True


def test_an_online_course_is_not_marked_in_a_window_or_a_daytime_column(ws):
    # Owner question Q2, default (a): an online course in a daytime column is
    # not marked; in its window it is where the rule wants it.
    sc, term2, term4 = ws
    place(term2, "GS104", "S1", "MON", "17:40", "19:20", room="")
    place(term2, "GS104", "S2", "TUE", "14:45", "16:25", room="")
    place(term4, "GSE2", "S1", "WED", "19:30", "21:10", room="")
    place(term4, "gse2 ", "S2", "THU", "15:50", "17:30", room="")  # the code as stored

    assert online_items(check_publish_readiness(sc.id)) == []


def test_a_window_is_the_column_the_grid_draws_in_not_an_overlap_of_times(ws):
    sc, term2, _ = ws
    place(term2, "CS201", "S1", "SUN", "16:30", "18:10")  # Lab 5: overlaps Online 1 and 2
    place(term2, "CS202", "S1", "MON", "15:50", "17:05")  # a lecture: no lecture column at 15:50
    place(term2, "CS203", "S1", "FRI", "19:30", "21:10")  # no teaching day: off the grid
    place(term2, "CS204", "S1", "TUE", "19:00", "20:40")  # no column starts at 19:00
    short = place(term2, "CS205", "S1", "WED", "17:40", "19:05")  # 85 minutes: a lab
    late = place(term2, "CS206", "S1", "THU", "19:30:00", "21:10:00")  # stored with seconds

    assert online_items(check_publish_readiness(sc.id)) == [
        item(term2, short, "CS205", "S1", "WED", "Online 2", "17:40", "19:05"),
        item(term2, late, "CS206", "S1", "THU", "Online 3", "19:30", "21:10"),
    ]


def test_a_course_online_only_for_another_programme_is_marked_on_this_board(ws):
    # GSE2 is AI2's online course: on Term 4 (AI and AI2) it is online, on
    # Term 2 (AI only) it is a class that needs a room, as the room checks say.
    sc, term2, term4 = ws
    on_ai = place(term2, "GSE2", "S1", "SUN", "15:50", "17:30")
    place(term4, "GSE2", "S2", "SUN", "15:50", "17:30", room="")

    assert online_items(check_publish_readiness(sc.id)) == [
        item(term2, on_ai, "GSE2", "S1", "SUN", "Online 1", "15:50", "17:30")
    ]


def test_an_english_meeting_is_never_in_a_window(ws):
    # An English bundle has its own table, whatever its times; a broken one
    # is the pattern check's blocker, not this warning.
    sc, term2, term4 = ws
    daytime(term4)
    english = TermSection.objects.create(
        scenario=sc, course_code="ENG101", course_key="ENG101", section="M1"
    )
    rows = create_policy_section_bundle(
        term2, english, day="SUN", start_time="09:00", end_time="12:00"
    )
    rows[-1].start_time, rows[-1].end_time = "19:30", "21:10"
    rows[-1].save()

    readiness = check_publish_readiness(sc.id)

    assert online_items(readiness) == []
    assert [i["code"] for i in readiness["blocker_items"]] == ["invalid_section_pattern"]


def test_a_lecture_column_flagged_online_only_is_a_window_too(ws):
    sc, term2, _ = ws
    sc.slot_config = [
        {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
        {"label": "Evening", "start": "18:00", "end": "19:15", "online_only": True},
    ]
    sc.save()
    lecture = place(term2, "CS301", "S1", "MON", "18:00", "19:15")
    place(term2, "CS302", "S1", "MON", "18:00", "19:40")  # a lab: no lab column at 18:00

    assert online_items(check_publish_readiness(sc.id)) == [
        item(term2, lecture, "CS301", "S1", "MON", "Evening", "18:00", "19:15")
    ]


def test_warnings_follow_the_board_order_then_day_time_course_and_section(ws):
    sc, term2, term4 = ws
    # Created out of order; Term 4 is created first and listed second.
    d = place(term4, "CS402", "S1", "SUN", "15:50", "17:30")
    c = place(term2, "CS212", "S2", "MON", "17:40", "19:20")
    b = place(term2, "CS212", "S1", "MON", "17:40", "19:20")
    a = place(term2, "CS299", "S1", "SUN", "19:30", "21:10")
    e = place(term2, "CS211", "S1", "MON", "17:40", "19:20")
    term4.display_order, term2.display_order = 2, 1
    term4.save()
    term2.save()

    readiness = check_publish_readiness(sc.id)

    assert online_items(readiness) == [
        item(term2, a, "CS299", "S1", "SUN", "Online 3", "19:30", "21:10"),
        item(term2, e, "CS211", "S1", "MON", "Online 2", "17:40", "19:20"),
        item(term2, b, "CS212", "S1", "MON", "Online 2", "17:40", "19:20"),
        item(term2, c, "CS212", "S2", "MON", "Online 2", "17:40", "19:20"),
        item(term4, d, "CS402", "S1", "SUN", "Online 1", "15:50", "17:30"),
    ]
    # Parallel to the strings, one for one.
    assert readiness["warnings"][-5:] == [
        f"Board 'Term 2': CS299 S1 meets in Online 3 (SUN 19:30-21:10), {TAIL}",
        f"Board 'Term 2': CS211 S1 meets in Online 2 (MON 17:40-19:20), {TAIL}",
        f"Board 'Term 2': CS212 S1 meets in Online 2 (MON 17:40-19:20), {TAIL}",
        f"Board 'Term 2': CS212 S2 meets in Online 2 (MON 17:40-19:20), {TAIL}",
        f"Board 'Term 4': CS402 S1 meets in Online 1 (SUN 15:50-17:30), {TAIL}",
    ]
    assert len(readiness["warnings"]) == len(readiness["warning_items"])


def test_the_warning_comes_after_every_other_stage(ws):
    sc, term2, term4 = ws
    daytime(term4)
    # The same course twice at one time: a blocker (and nothing on the board's
    # warnings) before the online-window warning.
    place(term2, "CS114", "S1", "SUN", "17:40", "19:20", room="LAB1")
    place(term2, "CS114", "S2", "SUN", "17:40", "19:20", room="LAB2")

    readiness = check_publish_readiness(sc.id)

    assert [i["code"] for i in readiness["blocker_items"]] == ["board_critical_conflicts"]
    assert [i["code"] for i in readiness["warning_items"]] == [
        "online_window_room_class",
        "online_window_room_class",
    ]
    assert readiness["ready"] is False


def test_the_stage_reads_nothing_more_from_the_database(ws):
    # It reuses the scenario's placements, its columns and the online lookup
    # the room checks already filled: a meeting in a window costs no query.
    sc, term2, term4 = ws
    daytime(term4)
    labs = [place(term2, f"CS50{n}", "S1", "SUN", "09:00", "10:40") for n in range(3)]

    def reads() -> int:
        with CaptureQueriesContext(connection) as ctx:
            check_publish_readiness(sc.id)
        return len(ctx.captured_queries)

    in_lab_1 = reads()
    windows = [("MON", "15:50", "17:30"), ("TUE", "17:40", "19:20"), ("WED", "19:30", "21:10")]
    for lab, (day, start, end) in zip(labs, windows, strict=True):
        lab.day, lab.start_time, lab.end_time = day, start, end
        lab.save()

    assert len(online_items(check_publish_readiness(sc.id))) == 3
    assert reads() == in_lab_1


# ── the board read ──────────────────────────────────────────────────────────


def test_the_board_read_lists_the_boards_online_courses(ws, http):
    sc, term2, term4 = ws
    _online("AI", " gs152 ")  # stored untrimmed and in lower case
    plain = DeliveryBoard.objects.create(scenario=sc, label="No programme", display_order=3)
    place(term2, "GS104", "S1", "MON", "17:40", "19:20", room="")

    read = {b.label: http.get(f"/ops/tw/boards/{b.id}/").json() for b in (term2, term4, plain)}

    # Placed or not; English (under a course meeting policy) never.
    assert read["Term 2"]["online_course_codes"] == ["GS104", "GS152"]
    assert read["Term 4"]["online_course_codes"] == ["GS104", "GS152", "GSE2"]
    assert read["No programme"]["online_course_codes"] == []
    # The placement says the same as the list.
    [gs104] = read["Term 2"]["placements"]
    assert gs104["is_online"] is True
    assert gs104["course_code"] in read["Term 2"]["online_course_codes"]
