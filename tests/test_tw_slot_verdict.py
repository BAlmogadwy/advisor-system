"""Sub-PR 5d: one slot verdict for both slot previews (server).

With ``verdict=1`` the move preview (``/slot-candidates/``) and the planned
preview (``/planned-slot-candidates/``) say, per candidate, what the publish
check would say once the edit is saved: H15 for same-board students (with a
move's effect on overlaps it does not touch), blocked times flagged, a
same-board room clash critical, cross-board students a warning at the
threshold and info below, a teacher on another board a warning, every teacher
judged as the publish check judges it, in every teacher mode (review rounds 1
and 2). Without it both answers are byte for byte what they were, with the
same queries: the oracle
(``tests/fixtures/tw_slot_verdict_legacy.json``) was recorded from the code
before this change by ``tests/record_tw_slot_verdict_legacy.py``.

V1-V13 are the tests of the spec (docs/TIMETABLE-WORKSPACE-UI.md, 5d).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core import timetable_workspace_views as views
from core.models import (
    DeliveryBoard,
    Instructor,
    ScenarioSectionBudget,
    SectionInstructor,
    SectionPlacement,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import timetable_workspace as tws
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.request_lanes import LANE_MARKER
from core.services.section_teacher_assignment import TeacherAssignment
from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD
from core.services.timetable_slot_config import offered_move_columns
from core.services.timetable_workspace import (
    check_publish_readiness,
    create_planned_section_placements,
    detect_board_conflicts,
    detect_cross_board_conflicts,
    preview_placement_slot_candidates,
    preview_planned_section_slot_candidates,
    validate_placement,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)
from tests.tw_slot_verdict_support import (
    build_rich_fixture,
    demand,
    legacy_requests,
    normalise_ids,
    placement_of,
)

pytestmark = pytest.mark.django_db

ORACLE = json.loads(
    (Path(__file__).parent / "fixtures" / "tw_slot_verdict_legacy.json").read_text(encoding="utf-8")
)
T = HARD_OVERLAP_THRESHOLD
LECTURES = [
    {"label": "L1", "start": "09:00", "end": "10:15"},
    {"label": "L2", "start": "10:30", "end": "11:45"},
    {"label": "L3", "start": "13:00", "end": "14:15"},
]


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    return settings


@pytest.fixture
def http():
    user = User.objects.create_user(username="slot-verdict-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def _scenario(*, blocked=(), lectures=None, labs=None) -> tuple[TimetableScenario, DeliveryBoard]:
    sc, board = scenario("verdict")
    sc.slot_config = lectures if lectures is not None else LECTURES
    if labs is not None:
        sc.lab_slot_config = labs
    sc.blocked_slots = [{"day": day, "start": start} for day, start in blocked]
    sc.save()
    return sc, board


def _second_board(sc: TimetableScenario) -> DeliveryBoard:
    return DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="AI")


def _at(preview: dict, day: str, start: str) -> dict:
    return next(c for c in preview["candidates"] if (c["day"], c["start"]) == (day, start))


def _items(verdict: dict, *kinds: str) -> list[dict]:
    return [item for item in verdict["items"] if not kinds or item["issue_kind"] in kinds]


def _named(item: dict) -> tuple:
    other = item["other"] or {}
    return (
        item["issue_kind"],
        item["severity"],
        item["source"],
        f"{other.get('course_code')}-{other.get('section')}",
        other.get("board_label"),
        other.get("day"),
        other.get("start"),
    )


# ── V1: a same-board room clash is critical; the legacy tone is unchanged ──


def test_v1_a_same_board_room_clash_is_critical_and_the_legacy_tone_stays_risky(flags) -> None:
    sc, board = _scenario()
    mover = section(sc, "AI101", "S1")
    place(board, mover, [("SUN", "09:00", "10:15")], room="R1", instructor_name="Dr A")
    place(board, section(sc, "AI102", "S1"), [("MON", "13:00", "14:15")], room="r1 ")

    preview = preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True)
    candidate = _at(preview, "MON", "13:00")

    assert candidate["tone"] == "risky"
    assert (candidate["critical_count"], candidate["warning_count"]) == (0, 1)
    assert [e["kind"] for e in candidate["evidence"]] == ["room"]
    verdict = candidate["verdict"]
    assert verdict["severity"] == "critical"
    assert verdict["counts"] == {"critical": 1, "warning": 0, "info": 0}
    assert verdict["items"] == [
        {
            "issue_kind": "room",
            "severity": "critical",
            "source": "room",
            "meeting": None,
            "other": {
                "course_code": "AI102",
                "section": "S1",
                "board_id": board.id,
                "board_label": "Term 2",
                "day": "MON",
                "start": "13:00",
                "end": "14:15",
            },
            "room": "R1",
            "teacher": None,
            "students": None,
            "blocked_students": None,
        }
    ]


# ── V2: cross-board students: a warning at the threshold, info below ──────


def test_v2_cross_board_students_warn_at_the_threshold_and_inform_below(flags) -> None:
    rich = build_rich_fixture()
    preview = preview_placement_slot_candidates(rich.moving.id, include_verdict=True)

    at_20 = _at(preview, "WED", "10:30")["verdict"]
    at_19 = _at(preview, "THU", "10:30")["verdict"]

    assert [_named(item) for item in at_20["items"]] == [
        ("crossBoard", "warning", "cross_board_students", "AI555-S1", "Term 4", "WED", "10:30")
    ]
    assert at_20["items"][0]["students"] == T == 20
    assert (at_20["severity"], at_20["students"]) == ("warning", 20)
    assert [_named(item) for item in at_19["items"]] == [
        ("crossBoardMinor", "info", "cross_board_students", "AI556-S1", "Term 4", "THU", "10:30")
    ]
    assert at_19["items"][0]["students"] == 19
    assert at_19["severity"] == "info"
    # The legacy fields keep their own rule: 20 shared is critical there.
    assert _at(preview, "WED", "10:30")["tone"] == "avoid"


def test_v2_a_planned_meeting_follows_the_same_cross_board_rule(flags) -> None:
    """AI777 (planned, one meeting a week) shares 20 students with AI555 and 19
    with AI556, both on the other board."""
    rich = build_rich_fixture()
    preview = preview_planned_section_slot_candidates(
        rich.b1.id, course_code="AI777", course_key=rich.p, include_verdict=True
    )

    at_20 = _at(preview, "WED", "10:30")["verdict"]
    at_19 = _at(preview, "THU", "10:30")["verdict"]

    assert [(i["students"], *_named(i)) for i in at_20["items"]] == [
        (20, "crossBoard", "warning", "cross_board_students", "AI555-S1", "Term 4", "WED", "10:30")
    ]
    assert (at_20["severity"], at_20["counts"]) == (
        "warning",
        {"critical": 0, "warning": 1, "info": 0},
    )
    assert [(i["students"], *_named(i)) for i in at_19["items"]] == [
        (
            19,
            "crossBoardMinor",
            "info",
            "cross_board_students",
            "AI556-S1",
            "Term 4",
            "THU",
            "10:30",
        )
    ]
    assert (at_19["severity"], at_19["counts"]) == (
        "info",
        {"critical": 0, "warning": 0, "info": 1},
    )
    assert _at(preview, "WED", "10:30")["tone"] == "avoid"  # legacy: 20 shared is critical


# ── V3: same-board students follow H15 for the moved section's week ───────


def _h15_board(*, sibling: tuple[str, str, str] | None):
    """K-S1 (one meeting, SUN 09:00) moves; C has S1 at MON 13:00 and, with
    ``sibling``, S2 then. Exactly T students need both courses."""
    sc, board = _scenario()
    k_s1 = section(sc, "AI101", "S1")
    place(board, k_s1, [("SUN", "09:00", "10:15")], room="R1")
    place(board, section(sc, "AI202", "S1"), [("MON", "13:00", "14:15")], room="R2")
    if sibling:
        place(board, section(sc, "AI202", "S2"), [sibling], room="R3")
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))
    return placement_of(k_s1, "SUN")


@pytest.mark.parametrize(
    "sibling",
    [
        ("THU", "13:00", "14:15"),
        # The slot the meeting leaves: C-S2 is clear of K-S1 once it moves.
        ("SUN", "09:00", "10:15"),
    ],
    ids=["elsewhere", "where_the_meeting_was"],
)
def test_v3_a_clash_free_sibling_pair_keeps_a_shared_overlap_soft(sibling, flags) -> None:
    moving = _h15_board(sibling=sibling)
    candidate = _at(
        preview_placement_slot_candidates(moving.id, include_verdict=True), "MON", "13:00"
    )
    [item] = candidate["verdict"]["items"]
    assert _named(item) == (
        "softOverlap",
        "warning",
        "students",
        "AI202-S1",
        "Term 2",
        "MON",
        "13:00",
    )
    assert (item["students"], item["blocked_students"]) == (T, 0)
    assert candidate["tone"] == "avoid"  # legacy: 20 shared is critical whatever the siblings


def test_v3_the_moved_mask_taking_the_last_free_pair_is_critical(flags) -> None:
    moving = _h15_board(sibling=None)
    preview = preview_placement_slot_candidates(moving.id, include_verdict=True)
    # Now (SUN) the only K section misses the only C section: no clash.
    assert preview["current_verdict"]["items"] == []
    [item] = _at(preview, "MON", "13:00")["verdict"]["items"]
    assert _named(item) == ("time", "critical", "students", "AI202-S1", "Term 2", "MON", "13:00")
    assert (item["students"], item["blocked_students"]) == (T, T)
    assert detect_board_conflicts(moving.board_id)["summary"]["critical"] == 0


def test_v3_the_sections_other_meetings_are_part_of_its_week(flags) -> None:
    """K-S1 meets SUN 09:00 (moving) and THU 13:00; C-S1 meets MON 13:00 and
    C-S2 THU 13:00. Now K-S1 misses C-S1. Moved to MON 13:00, K-S1's week
    (its Thursday too) meets both: the new overlap blocks, and so does the
    Thursday one it did not touch."""
    sc, board = _scenario()
    k_s1 = section(sc, "AI101", "S1")
    place(board, k_s1, [("SUN", "09:00", "10:15"), ("THU", "13:00", "14:15")], room="R1")
    place(board, section(sc, "AI202", "S1"), [("MON", "13:00", "14:15")], room="R2")
    place(board, section(sc, "AI202", "S2"), [("THU", "13:00", "14:15")], room="R3")
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))

    preview = preview_placement_slot_candidates(placement_of(k_s1, "SUN").id, include_verdict=True)
    verdict = _at(preview, "MON", "13:00")["verdict"]

    assert sorted(_named(i) for i in verdict["items"]) == [
        ("time", "critical", "h15_effect", "AI202-S2", "Term 2", "THU", "13:00"),
        ("time", "critical", "students", "AI202-S1", "Term 2", "MON", "13:00"),
    ]
    assert preview["current_verdict"]["items"] == []


def test_v3_a_copy_on_another_board_stays_in_the_sections_week(flags, http) -> None:
    """K-S1 is shared: one meeting, SUN 09:00, on both boards. Moving b1's copy
    to MON 13:00 leaves b2's at SUN 09:00, so the section then meets on Sunday
    AND Monday: C-S1 (MON 13:00) and C-S2 (SUN 09:00, b1) both clash with it,
    no clash-free pair is left, and the overlap blocks -- as the publish check
    says once the move is saved."""
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    k_s1 = section(sc, "AI101", "S1")
    place(b1, k_s1, [("SUN", "09:00", "10:15")], room="R1")
    place(b2, k_s1, [("SUN", "09:00", "10:15")], room="R1")
    place(b1, section(sc, "AI202", "S1"), [("MON", "13:00", "14:15")], room="R2")
    place(b1, section(sc, "AI202", "S2"), [("SUN", "09:00", "10:15")], room="R3")
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))
    moving = SectionPlacement.objects.get(board=b1, term_section=k_s1)

    preview = preview_placement_slot_candidates(moving.id, include_verdict=True)
    [item] = _at(preview, "MON", "13:00")["verdict"]["items"]

    assert _named(item) == ("time", "critical", "students", "AI202-S1", "Term 2", "MON", "13:00")
    assert (item["students"], item["blocked_students"]) == (T, T)
    response = http.post(
        f"/ops/tw/placements/{moving.id}/move/",
        json.dumps({"day": "MON", "start_time": "13:00", "end_time": "14:15"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    [overlap] = detect_board_conflicts(b1.id)["overlaps"]
    assert (overlap["severity"], overlap["blocked_students"]) == ("critical", T)


# ── V4: a move's H15 effect on overlaps it does not touch ─────────────────


def _effect_board():
    """A move that takes the last clash-free pair of K and C away.

    K-S1 (moving, b1 SUN 09:00) and K-S2 (WED 10:30 on b1 and on b2, one
    section shared by both boards). C-S1 meets MON 13:00 and WED 10:30, on b1
    and on b2. Now K-S1 misses C-S1, so K-S2 x C-S1 on each board is a
    warning; moving K-S1 to MON 13:00 leaves no clash-free pair, and both
    untouched overlaps turn critical. T students need both.
    """
    sc, b1 = _scenario(blocked=[("THU", "13:00")])
    b2 = _second_board(sc)
    k_s1, k_s2, c_s1 = (
        section(sc, "AI101", "S1"),
        section(sc, "AI101", "S2"),
        section(sc, "AI202", "S1"),
    )
    place(b1, k_s1, [("SUN", "09:00", "10:15")], room="R1", instructor_name="Dr A")
    place(b1, k_s2, [("WED", "10:30", "11:45")], room="R2", instructor_name="Dr B")
    place(b2, k_s2, [("WED", "10:30", "11:45")], room="R2", instructor_name="Dr B")
    place(b1, c_s1, [("MON", "13:00", "14:15"), ("WED", "10:30", "11:45")], room="R3")
    place(b2, c_s1, [("MON", "13:00", "14:15"), ("WED", "10:30", "11:45")], room="R3")
    # A room clash on b1, and Dr A on b2, for V11's other candidates.
    place(b1, section(sc, "AI303", "S1"), [("TUE", "13:00", "14:15")], room="R1")
    place(
        b2,
        section(sc, "AI404", "S1"),
        [("THU", "13:30", "14:45")],
        room="R9",
        instructor_name="Dr A",
    )
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))
    # Five students need AI505 too: a soft overlap at SUN 13:00.
    place(b1, section(sc, "AI505", "S1"), [("SUN", "13:00", "14:15")], room="R5")
    demand(sc, "AI505", range(1, 6))
    return sc, b1, b2, placement_of(k_s1, "SUN")


def test_v4_a_move_flips_untouched_overlaps_on_this_board_and_another(flags) -> None:
    _sc, b1, b2, moving = _effect_board()
    before = {b.id: detect_board_conflicts(b.id)["summary"]["critical"] for b in (b1, b2)}
    assert before == {b1.id: 0, b2.id: 0}

    preview = preview_placement_slot_candidates(moving.id, include_verdict=True)
    verdict = _at(preview, "MON", "13:00")["verdict"]
    effects = [_named(item) for item in verdict["items"] if item["source"] == "h15_effect"]

    assert sorted(effects) == [
        ("time", "critical", "h15_effect", "AI202-S1", "Term 2", "WED", "10:30"),
        ("time", "critical", "h15_effect", "AI202-S1", "Term 4", "WED", "10:30"),
    ]
    for item in verdict["items"]:
        if item["source"] == "h15_effect":
            assert (item["students"], item["blocked_students"]) == (T, T)
    # The moving meeting's own overlap is a direct item, never an effect.
    assert ("time", "critical", "students", "AI202-S1", "Term 2", "MON", "13:00") in [
        _named(item) for item in verdict["items"]
    ]
    assert verdict["counts"]["critical"] == 3
    # A slot that keeps a clash-free pair has no effect, and where the meeting
    # is now there is nothing to flip.
    assert not [
        i for i in _at(preview, "SUN", "13:00")["verdict"]["items"] if i["source"] == "h15_effect"
    ]
    assert not [i for i in preview["current_verdict"]["items"] if i["source"] == "h15_effect"]


def test_v4_the_overlaps_the_moving_meeting_leaves_are_not_effects(flags) -> None:
    """K-S1 (SUN 09:00, moving) overlaps C-S1 (SUN 09:00 and MON 13:00) now,
    and misses C-S2 (MON 13:00). At MON 13:00 it meets both: two direct
    overlaps, and no effect -- its Sunday overlap goes with it."""
    sc, board = _scenario()
    k_s1 = section(sc, "AI101", "S1")
    place(board, k_s1, [("SUN", "09:00", "10:15")], room="R1")
    place(
        board,
        section(sc, "AI202", "S1"),
        [("SUN", "09:00", "10:15"), ("MON", "13:00", "14:15")],
        room="R2",
    )
    place(board, section(sc, "AI202", "S2"), [("MON", "13:00", "14:15")], room="R3")
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))

    preview = preview_placement_slot_candidates(placement_of(k_s1, "SUN").id, include_verdict=True)

    assert sorted(_named(i) for i in _at(preview, "MON", "13:00")["verdict"]["items"]) == [
        ("time", "critical", "students", "AI202-S1", "Term 2", "MON", "13:00"),
        ("time", "critical", "students", "AI202-S2", "Term 2", "MON", "13:00"),
    ]
    assert [_named(i) for i in preview["current_verdict"]["items"]] == [
        ("softOverlap", "warning", "students", "AI202-S1", "Term 2", "SUN", "09:00")
    ]


# ── V5: blocked times are flagged, never removed ──────────────────────────


def _blocked_board():
    """The meeting sits on a blocked time (SUN 09:00); SUN 10:30 is blocked too."""
    sc, board = _scenario(blocked=[("SUN", "09:00"), ("SUN", "10:30")])
    mover = section(sc, "AI101", "S1")
    place(board, mover, [("SUN", "09:00", "10:15")], room="R1")
    return placement_of(mover, "SUN")


def test_v5_blocked_times_are_flagged_for_a_candidate_and_for_the_current_slot(flags) -> None:
    moving = _blocked_board()
    preview = preview_placement_slot_candidates(moving.id, include_verdict=True)

    blocked = {"issue_kind": "blocked", "severity": "critical", "source": "blocked_slot"}
    current = preview["current_verdict"]
    assert current["severity"] == "critical"
    assert [{k: item[k] for k in blocked} for item in current["items"]] == [blocked]
    candidate = _at(preview, "SUN", "10:30")  # still offered
    assert [{k: item[k] for k in blocked} for item in candidate["verdict"]["items"]] == [blocked]
    assert candidate["verdict"]["items"][0]["other"] is None
    assert _at(preview, "SUN", "13:00")["verdict"]["severity"] == "ok"
    assert "own_section_meeting" not in current


# ── V6: the section's own other meeting slot ──────────────────────────────


def test_v6_the_sections_other_meeting_slot_is_flagged_and_ranked_last(flags) -> None:
    """K-S1 meets SUN and TUE 09:00 with no room and no teacher, so its TUE
    slot has one critical item (its own meeting); WED 09:00 has two (K-S2 and
    a blocked time) and would rank below TUE without the flag."""
    sc, board = _scenario(blocked=[("WED", "09:00")])
    k_s1 = section(sc, "AI101", "S1")
    place(board, k_s1, [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")], room="")
    place(board, section(sc, "AI101", "S2"), [("WED", "09:00", "10:15")], room="")

    preview = preview_placement_slot_candidates(placement_of(k_s1, "SUN").id, include_verdict=True)
    own = _at(preview, "TUE", "09:00")
    worse = _at(preview, "WED", "09:00")

    assert own["verdict"]["own_section_meeting"] is True
    assert own["verdict"]["counts"]["critical"] == 1
    assert worse["verdict"]["counts"]["critical"] == 2
    assert [c["verdict"]["own_section_meeting"] for c in preview["candidates"]].count(True) == 1
    assert own["verdict_rank"] == len(preview["candidates"])
    assert worse["verdict_rank"] == len(preview["candidates"]) - 1


# ── V7: the verdict rank is its own order; the list keeps the legacy one ──


def test_v7_an_ok_candidate_with_a_worse_score_ranks_above_a_critical_one(flags) -> None:
    moving = _blocked_board()
    legacy = preview_placement_slot_candidates(moving.id)
    preview = preview_placement_slot_candidates(moving.id, include_verdict=True)
    blocked, ok = _at(preview, "SUN", "10:30"), _at(preview, "THU", "13:00")

    assert blocked["score"] < ok["score"] and blocked["rank"] < ok["rank"]
    assert (blocked["verdict"]["severity"], ok["verdict"]["severity"]) == ("critical", "ok")
    assert ok["verdict_rank"] < blocked["verdict_rank"]
    assert [(c["day"], c["start"]) for c in preview["candidates"]] == [
        (c["day"], c["start"]) for c in legacy["candidates"]
    ]
    assert [c["rank"] for c in preview["candidates"]] == list(
        range(1, len(legacy["candidates"]) + 1)
    )
    assert sorted(c["verdict_rank"] for c in preview["candidates"]) == [
        c["rank"] for c in preview["candidates"]
    ]


# ── V8: counts cover every item; eight are listed ─────────────────────────


def test_v8_ten_overlaps_are_counted_eight_listed_and_two_more(flags) -> None:
    sc, board = _scenario()
    mover = section(sc, "AI100", "S1")
    place(board, mover, [("SUN", "09:00", "10:15")], room="R0")
    demand(sc, "AI100", range(1, 31))
    for n in range(1, 11):
        code = f"AI1{n:02d}"
        place(board, section(sc, code, "S1"), [("MON", "13:00", "14:15")], room=f"R{n}")
        demand(sc, code, range(1, n + 1))  # n students need both

    verdict = _at(
        preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True),
        "MON",
        "13:00",
    )["verdict"]

    assert verdict["counts"] == {"critical": 0, "warning": 10, "info": 0}
    assert len(verdict["items"]) == 8
    assert verdict["more"] == 2
    assert [item["students"] for item in verdict["items"]] == [10, 9, 8, 7, 6, 5, 4, 3]
    assert verdict["students"] == 10


def test_v8_items_are_ordered_by_severity_then_kind_then_students(flags) -> None:
    """Every item is found in the opposite order: on this board a room clash
    (AI110) before a teacher clash (AI111); on the other board AI112's room
    before its teacher, then 20, 25 and 3 shared students. The dialog shows
    the first items, so the order is the spec's: severity, kind, students."""
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    mover = section(sc, "AI100", "S1")
    place(b1, mover, [("SUN", "09:00", "10:15")], room="R1", instructor_name="Dr A")
    monday = [("MON", "13:00", "14:15")]
    place(b1, section(sc, "AI110", "S1"), monday, room="R1")
    place(b1, section(sc, "AI111", "S1"), monday, room="R7", instructor_name="Dr A")
    place(b2, section(sc, "AI112", "S1"), monday, room="R1", instructor_name="Dr A")
    for code, room, students in (("AI113", "R8", 20), ("AI114", "R9", 25), ("AI115", "R6", 3)):
        place(b2, section(sc, code, "S1"), monday, room=room)
        demand(sc, code, range(1, students + 1))
    demand(sc, "AI100", range(1, 31))

    verdict = _at(
        preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True),
        "MON",
        "13:00",
    )["verdict"]

    assert [(*_named(i)[:2], _named(i)[3], i["students"]) for i in verdict["items"]] == [
        ("instructor", "critical", "AI111-S1", None),
        ("room", "critical", "AI110-S1", None),
        ("instructorElsewhere", "warning", "AI112-S1", None),
        ("roomElsewhere", "warning", "AI112-S1", None),
        ("crossBoard", "warning", "AI114-S1", 25),
        ("crossBoard", "warning", "AI113-S1", 20),
        ("crossBoardMinor", "info", "AI115-S1", 3),
    ]
    assert (verdict["counts"], verdict["more"]) == ({"critical": 2, "warning": 4, "info": 1}, 0)


def test_v7_fewer_warnings_rank_first_whatever_the_legacy_score(flags) -> None:
    """MON 13:00 holds the room twice on the other board (two warnings, a low
    legacy score); TUE 13:00 meets T students' other course there (one
    warning, which the legacy score counts as critical). Same severity, no
    critical: the warning count decides, before the score."""
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    mover = section(sc, "AI100", "S1")
    place(b1, mover, [("SUN", "09:00", "10:15")], room="R1")
    place(b2, section(sc, "AI120", "S1"), [("MON", "13:00", "14:15")], room="R1")
    place(b2, section(sc, "AI121", "S1"), [("MON", "13:00", "14:15")], room="R1")
    place(b2, section(sc, "AI122", "S1"), [("TUE", "13:00", "14:15")], room="R5")
    demand(sc, "AI100", range(1, T + 1))
    demand(sc, "AI122", range(1, T + 1))

    preview = preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True)
    two, one = _at(preview, "MON", "13:00"), _at(preview, "TUE", "13:00")

    assert [_named(i)[0] for i in two["verdict"]["items"]] == ["roomElsewhere", "roomElsewhere"]
    assert [_named(i)[0] for i in one["verdict"]["items"]] == ["crossBoard"]
    for candidate, warnings in ((two, 2), (one, 1)):
        assert candidate["verdict"]["severity"] == "warning"
        assert candidate["verdict"]["counts"] == {"critical": 0, "warning": warnings, "info": 0}
    assert two["score"] < one["score"]
    assert one["verdict_rank"] < two["verdict_rank"]


# ── V9: the columns a manual move is offered ──────────────────────────────

_ONLINE_LABS = [
    {"label": "Lab 1", "start": "09:00", "end": "10:40"},
    {"label": "Policy lab", "start": "13:00", "end": "14:40", "policy_only": True},
    {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True},
    {"label": "Online 2", "start": "17:40", "end": "19:20", "online_only": True},
]
_POLICY_LECTURES = [
    *LECTURES,
    {"label": "Policy", "start": "14:30", "end": "15:45", "policy_only": True},
]


def _offer_board():
    sc, board = _scenario(lectures=_POLICY_LECTURES, labs=_ONLINE_LABS)
    online = section(sc, "GS101", "S1")
    place(board, online, [("SUN", "15:50", "17:30")], room="")
    lab = section(sc, "AI150", "S1")
    place(board, lab, [("MON", "09:00", "10:40")], room="L1")
    lecture = section(sc, "AI160", "S1")
    place(board, lecture, [("TUE", "09:00", "10:15")], room="R1")
    return (
        placement_of(online, "SUN"),
        placement_of(lab, "MON"),
        placement_of(lecture, "TUE"),
    )


def _starts(preview: dict) -> set[str]:
    return {c["start"] for c in preview["candidates"]}


def test_v9_a_manual_move_offers_evening_columns_only_to_a_meeting_in_one(flags, http) -> None:
    online, lab, lecture = _offer_board()

    manual = preview_placement_slot_candidates(online.id, offer_manual=True)
    assert _starts(manual) == {"09:00", "15:50", "17:40"}
    assert ("SUN", "15:50") not in {(c["day"], c["start"]) for c in manual["candidates"]}
    assert _starts(preview_placement_slot_candidates(online.id)) == {"09:00"}

    assert _starts(preview_placement_slot_candidates(lab.id, offer_manual=True)) == {"09:00"}
    assert _starts(preview_placement_slot_candidates(lecture.id, offer_manual=True)) == {
        "09:00",
        "10:30",
        "13:00",
    }
    # The view reads offer=manual, and nothing else turns it on.
    base = f"/ops/tw/placements/{online.id}/slot-candidates/"
    assert _starts(http.get(f"{base}?offer=manual").json()) == {"09:00", "15:50", "17:40"}
    assert _starts(http.get(f"{base}?offer=1").json()) == {"09:00"}


@pytest.mark.parametrize(
    ("meeting_start", "starts"),
    [
        ("15:50", ["09:00", "15:50", "17:40"]),
        ("17:40:00", ["09:00", "15:50", "17:40"]),  # sliced like the page's rule
        ("09:00", ["09:00"]),
        ("13:00", ["09:00"]),  # a policy column is never "an evening one"
        (None, ["09:00"]),
    ],
)
def test_v9_offered_move_columns_mirrors_the_pages_rule(meeting_start, starts) -> None:
    offered = offered_move_columns(_ONLINE_LABS, meeting_start)
    assert [slot["start"] for slot in offered] == starts
    assert all(not slot.get("policy_only") for slot in offered)


# ── V10: the legacy answer is unchanged; the verdict's cost is pinned ─────

#: The oracle each teacher mode answers as. The previews read the links only
#: through one teacher per section, so with the links off they answer as with
#: the links on and one teacher per section off (the oracle's one_teacher_off).
_ORACLE_MODE = {
    "links_off": "one_teacher_off",
    "links_on": "one_teacher_off",
    "one_teacher_on": "one_teacher_on",
}
#: The queries ``verdict=1`` adds to a preview request: the course links, as the
#: publish check reads them, with the links on and one teacher per section off
#: (linked_section_instructor_ids: programme requirements, course links with
#: their teachers' names, sections); none in the other modes.
_VERDICT_QUERIES = {"links_off": 0, "links_on": 3, "one_teacher_on": 0}


@pytest.mark.parametrize("mode", ["links_off", "links_on", "one_teacher_on"])
def test_v10_without_verdict_the_json_is_the_recorded_oracle(
    mode, flags, http, django_assert_num_queries
) -> None:
    _teacher_mode(flags, mode)
    rich = build_rich_fixture()
    oracle = ORACLE[_ORACLE_MODE[mode]]
    for name, url in legacy_requests(rich).items():
        with django_assert_num_queries(oracle["queries"][name]):
            response = http.get(url)
        assert response.status_code == 200
        got = normalise_ids(json.loads(response.content), rich.ids())
        # Byte for byte, key order included.
        assert json.dumps(got, ensure_ascii=False) == json.dumps(oracle[name], ensure_ascii=False)
        # Exactly: one query more or fewer fails.
        with django_assert_num_queries(oracle["queries"][name] + _VERDICT_QUERIES[mode]):
            with_verdict = http.get(f"{url}{'&' if '?' in url else '?'}verdict=1")
        body = with_verdict.json()
        assert body["candidates"] and all("verdict" in c for c in body["candidates"])


def test_v10_the_one_teacher_publish_map_is_built_only_for_a_verdict(flags, http) -> None:
    """With one teacher per section the verdict reads the map the publish check
    reads (``pr4_map``) from the assignment the preview has loaded; the legacy
    answers and the placement check never build it."""
    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = True
    rich = build_rich_fixture()
    requests = legacy_requests(rich).values()
    with mock.patch.object(
        TeacherAssignment, "pr4_map", autospec=True, side_effect=TeacherAssignment.pr4_map
    ) as built:
        for url in requests:
            assert http.get(url).status_code == 200
        validate_placement(
            board_id=rich.b1.id,
            day="MON",
            start_time="13:00",
            end_time="14:15",
            room="R101",
            term_section_id=rich.k_s1.id,
            exclude_placement_id=rich.moving.id,
        )
        assert built.call_count == 0
        for url in requests:
            assert http.get(f"{url}{'&' if '?' in url else '?'}verdict=1").status_code == 200
        assert built.call_count == len(requests)


def test_v10_both_views_keep_the_preview_lane() -> None:
    assert getattr(views.tw_placement_slot_candidates_view, LANE_MARKER, False) is True
    assert getattr(views.tw_board_planned_slot_candidates_view, LANE_MARKER, False) is True


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_v10_only_verdict_1_turns_the_verdict_on(value, flags, http) -> None:
    """``verdict=1`` and nothing else, in both views (as ``offer=manual``)."""
    moving = _blocked_board()
    move = f"/ops/tw/placements/{moving.id}/slot-candidates/"
    place_ = (
        f"/ops/tw/boards/{moving.board_id}/planned-slot-candidates/"
        "?course_code=AI101&section_label=S2"
    )

    for off in (http.get(f"{move}?verdict={value}"), http.get(f"{place_}&verdict={value}")):
        body = off.json()
        assert body["candidates"]
        assert not [c for c in body["candidates"] if "verdict" in c or "verdict_rank" in c]
        assert "current_verdict" not in body
    for on in (http.get(f"{move}?verdict=1"), http.get(f"{place_}&verdict=1")):
        assert all("verdict" in c for c in on.json()["candidates"])


# ── V11: parity with the publish check after a real move ──────────────────

_CONFLICT_BLOCKERS = {
    "board_critical_conflicts",
    "board_room_conflicts",
    "board_blocked_slot_placements",
    "board_sections_without_rooms",
}


def _pair_label(board_label: str, conflict: dict, moving_id: int) -> str:
    """The other side of a same-board pair, as the verdict names it."""
    index = 1 if conflict["ids"][0] == moving_id else 0
    return f"{conflict['sections'][index]}@{board_label}"


def _published_view(sc, moving_id: int, before_critical: set) -> dict[str, set]:
    """What the publish check says now, in the verdict's terms."""
    out: dict[str, set] = {
        k: set() for k in ("time", "softOverlap", "instructor", "room", "effect", "cross")
    }
    for board in DeliveryBoard.objects.filter(scenario=sc):
        conflicts = detect_board_conflicts(board.id)
        for overlap in conflicts["overlaps"]:
            if moving_id in overlap["ids"]:
                kind = "time" if overlap["severity"] == "critical" else "softOverlap"
                out[kind].add(_pair_label(board.label, overlap, moving_id))
            elif overlap["severity"] == "critical" and tuple(overlap["ids"]) not in before_critical:
                # A pair the move did not touch, critical now: an effect.
                c_side = next(s for s in overlap["sections"] if s.startswith("AI202"))
                out["effect"].add(f"{c_side}@{board.label}")
        for clash in conflicts["instructor_clashes"]:
            if moving_id in clash["ids"]:
                out["instructor"].add(_pair_label(board.label, clash, moving_id))
        for clash in conflicts["room_clashes"]:
            if moving_id in clash["ids"]:
                out["room"].add(_pair_label(board.label, clash, moving_id))
    for pair in detect_cross_board_conflicts(sc.id):
        if moving_id in pair["ids"]:
            mine_a = pair["placement_a_id"] == moving_id
            other = pair["section_b"] if mine_a else pair["section_a"]
            label = pair["board_b_label"] if mine_a else pair["board_a_label"]
            tone = "crossBoard" if pair["overlap_count"] >= T else "crossBoardMinor"
            out["cross"].add(f"{tone}:{other}@{label}")
    return out


def _verdict_view(verdict: dict) -> dict[str, set]:
    out: dict[str, set] = {
        k: set() for k in ("time", "softOverlap", "instructor", "room", "effect", "cross")
    }
    assert verdict["more"] == 0
    for item in verdict["items"]:
        other = item["other"] or {}
        label = f"{other.get('course_code')}-{other.get('section')}@{other.get('board_label')}"
        kind = item["issue_kind"]
        if item["source"] == "h15_effect":
            out["effect"].add(label)
        elif kind in {"crossBoard", "crossBoardMinor"}:
            out["cross"].add(f"{kind}:{label}")
        elif kind in out:
            out[kind].add(label)
    return out


def _expected_blockers(verdict: dict) -> set[str]:
    kinds = {item["issue_kind"] for item in verdict["items"] if item["severity"] == "critical"}
    codes = set()
    if kinds & {"time", "instructor"}:
        codes.add("board_critical_conflicts")
    if "room" in kinds:
        codes.add("board_room_conflicts")
    if "blocked" in kinds:
        codes.add("board_blocked_slot_placements")
    return codes


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        ("MON", "13:00", "14:15"),  # the flip: a direct critical, two effects, cross-board
        ("TUE", "13:00", "14:15"),  # a same-board room clash
        ("THU", "13:00", "14:15"),  # a blocked time and Dr A on the other board
        ("SUN", "13:00", "14:15"),  # a soft overlap
    ],
)
def test_v11_the_verdict_matches_the_publish_check_after_the_move(
    day, start, end, flags, http
) -> None:
    sc, _b1, _b2, moving = _effect_board()
    before_critical = {
        tuple(o["ids"])
        for b in DeliveryBoard.objects.filter(scenario=sc)
        for o in detect_board_conflicts(b.id)["overlaps"]
        if o["severity"] == "critical"
    }
    before_codes = {i["code"] for i in check_publish_readiness(sc.id)["blocker_items"]}
    assert not before_codes & _CONFLICT_BLOCKERS

    preview = http.get(f"/ops/tw/placements/{moving.id}/slot-candidates/?verdict=1").json()
    verdict = _at(preview, day, start)["verdict"]
    response = http.post(
        f"/ops/tw/placements/{moving.id}/move/",
        json.dumps({"day": day, "start_time": start, "end_time": end}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    assert _verdict_view(verdict) == _published_view(sc, moving.id, before_critical)
    after_codes = {i["code"] for i in check_publish_readiness(sc.id)["blocker_items"]}
    assert after_codes & _CONFLICT_BLOCKERS == _expected_blockers(verdict)
    if day == "THU":
        # A teacher on two boards is a warning: no blocker is invented for it.
        assert [_named(i)[:3] for i in _items(verdict, "instructorElsewhere")] == [
            ("instructorElsewhere", "warning", "cross_board_instructor")
        ]
        assert _expected_blockers(verdict) == {"board_blocked_slot_placements"}
    if day == "SUN":
        assert [_named(i)[:4] for i in verdict["items"]] == [
            ("softOverlap", "warning", "students", "AI505-S1")
        ]


# ── V11 for teachers: the publish check's teachers, names disagreeing ─────


def _teacher_mode(settings, mode: str) -> None:
    """``links_off``: names alone; ``links_on``: every linked teacher charged
    (one teacher per section off, the local default); ``one_teacher_on``: the
    one-teacher map, as in production."""
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = mode != "links_off"
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = mode == "one_teacher_on"


def _teacher_board():
    """Sections whose meeting rows name someone other than their teacher.

    Board ``Term 2``: K-S1 (AI201, moving, SUN 09:00) is pinned to Dr A, its
    course linked to Dr A and Dr B (co), and its row says Dr B. AI202-S1 (MON
    13:00) is pinned to Dr A, its row saying Dr Z; AI203-S1 (TUE 13:00) and
    AI204-S1 (WED 13:00) have no links, their rows saying Dr A and Dr B.
    AI210-S1 (TUE 10:30) is linked to Dr B alone, its row saying Dr Y. N-S1
    (AI208, no links, THU 09:00) also moves; its row says Dr Q, as does
    AI207-S3 (MON 10:30), which the map leaves to be decided (Dr A, AI207's
    one teacher, may hold two of its four sections: S1 and S2). Board ``Term
    4``: AI205-S1 (THU 13:00) pinned to Dr A, its row saying Dr C; AI206-S1
    (SUN 13:00), no links, Dr B. AI201, AI202, AI205 and AI207 are linked to
    Dr A.
    """
    names = {
        "AI201": "MOVER",
        "AI202": "PINNED",
        "AI203": "NAMED A",
        "AI204": "NAMED B",
        "AI205": "PINNED ELSEWHERE",
        "AI206": "NAMED ELSEWHERE",
        "AI207": "FOUR SECTIONS",
        "AI208": "SECOND MOVER",
        "AI210": "CO-TEACHER",
    }
    keys = {code: requirement("AI", code, name) for code, name in names.items()}
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    dr_a, dr_b = instructor("Dr A"), instructor("Dr B")
    for code in ("AI201", "AI202", "AI205", "AI207"):
        link("AI", code, dr_a)
    link("AI", "AI201", dr_b, role="co")
    link("AI", "AI210", dr_b)
    for code, key in keys.items():
        budget(sc, key, 4 if code == "AI207" else 1)
    for code in ("AI201", "AI202", "AI205"):
        _pin(sc, keys[code], "S1", "Dr A")

    def put(board, code, label, day, start, room, name):
        end = {"09:00": "10:15", "10:30": "11:45", "13:00": "14:15"}[start]
        ts = section(sc, keys[code], label)
        place(board, ts, [(day, start, end)], room=room, instructor_name=name)
        return ts

    mover = put(b1, "AI201", "S1", "SUN", "09:00", "R1", "Dr B")
    put(b1, "AI202", "S1", "MON", "13:00", "R2", "Dr Z")
    put(b1, "AI203", "S1", "TUE", "13:00", "R3", "Dr A")
    put(b1, "AI204", "S1", "WED", "13:00", "R4", "Dr B")
    second = put(b1, "AI208", "S1", "THU", "09:00", "R8", "Dr Q")
    put(b1, "AI207", "S1", "WED", "09:00", "R5", "Dr A")
    put(b1, "AI207", "S2", "TUE", "09:00", "R6", "Dr A")
    put(b1, "AI207", "S3", "MON", "10:30", "R7", "Dr Q")
    put(b1, "AI210", "S1", "TUE", "10:30", "R11", "Dr Y")
    put(b2, "AI205", "S1", "THU", "13:00", "R9", "Dr C")
    put(b2, "AI206", "S1", "SUN", "13:00", "R10", "Dr B")
    return sc, {"K": placement_of(mover, "SUN"), "N": placement_of(second, "THU")}


#: (mover, day, start) -> the verdict's teacher items, per mode.
_TEACHER_ITEMS = {
    "links_off": {
        ("K", "WED", "13:00"): [("instructor", "AI204-S1@Term 2", "Dr B")],
        ("K", "SUN", "13:00"): [("instructorElsewhere", "AI206-S1@Term 4", "Dr B")],
        ("N", "MON", "10:30"): [("instructor", "AI207-S3@Term 2", "Dr Q")],
    },
    # Every linked teacher, by id, named as linked; a row's name counts only
    # for a section with no links, and never matches a linked teacher.
    "links_on": {
        ("K", "MON", "13:00"): [("instructor", "AI202-S1@Term 2", "Dr A")],
        ("K", "TUE", "10:30"): [("instructor", "AI210-S1@Term 2", "Dr B")],
        ("K", "THU", "13:00"): [("instructorElsewhere", "AI205-S1@Term 4", "Dr A")],
    },
    "one_teacher_on": {
        ("K", "MON", "13:00"): [("instructor", "AI202-S1@Term 2", "Dr A")],
        ("K", "THU", "13:00"): [("instructorElsewhere", "AI205-S1@Term 4", "Dr A")],
    },
}


def _teacher_items(verdict: dict) -> list[tuple]:
    return [
        (
            item["issue_kind"],
            f"{item['other']['course_code']}-{item['other']['section']}"
            f"@{item['other']['board_label']}",
            item["teacher"],
        )
        for item in verdict["items"]
        if item["issue_kind"] in {"instructor", "instructorElsewhere"}
    ]


def _published_teacher_clashes(sc, moving_id: int) -> set[str]:
    return {
        _pair_label(board.label, clash, moving_id)
        for board in DeliveryBoard.objects.filter(scenario=sc)
        for clash in detect_board_conflicts(board.id)["instructor_clashes"]
        if moving_id in clash["ids"]
    }


@pytest.mark.parametrize("mode", ["links_off", "links_on", "one_teacher_on"])
@pytest.mark.parametrize(
    ("mover", "day", "start"),
    [
        ("K", "MON", "13:00"),  # one teacher by id; the rows name others
        ("K", "TUE", "13:00"),  # the row names K's teacher; it has no links
        ("K", "WED", "13:00"),  # the row names what K's row names
        ("K", "THU", "13:00"),  # one teacher by id, on the other board
        ("K", "SUN", "13:00"),  # K's row's name, on the other board
        ("K", "TUE", "10:30"),  # K's co-teacher's link; the rows name others
        ("N", "MON", "10:30"),  # a section to be decided, its row naming N's
    ],
)
def test_v11_the_verdicts_teachers_are_the_publish_checks(
    mode, mover, day, start, flags, http
) -> None:
    """The verdict's teacher items, then the publish check's clashes once the
    move is saved, in each teacher mode: the same pairs. ``links_on`` (one
    teacher per section off, the local default) charges every linked teacher,
    as ``build_section_instructor_ids`` does for the publish check."""
    _teacher_mode(flags, mode)
    sc, movers = _teacher_board()
    moving = movers[mover]
    end = {"10:30": "11:45", "13:00": "14:15"}[start]

    preview = http.get(f"/ops/tw/placements/{moving.id}/slot-candidates/?verdict=1").json()
    candidate = _at(preview, day, start)
    expected = _TEACHER_ITEMS[mode].get((mover, day, start), [])
    assert _teacher_items(candidate["verdict"]) == expected
    if mode == "one_teacher_on" and (mover, day, start) == ("K", "TUE", "13:00"):
        # The legacy test still matches K's teacher's name with the row's:
        # its fields are what they were; the verdict follows the publish check.
        assert [e["kind"] for e in candidate["evidence"]] == ["instructor"]

    response = http.post(
        f"/ops/tw/placements/{moving.id}/move/",
        json.dumps({"day": day, "start_time": start, "end_time": end}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert _published_teacher_clashes(sc, moving.id) == {
        label for kind, label, _teacher in expected if kind == "instructor"
    }


#: day -> the planned AI209-S1's teacher items at 13:00, per mode.
_PLANNED_TEACHER_ITEMS = {
    "links_off": {},
    "links_on": {
        "MON": [("instructor", "AI202-S1@Term 2", "Dr A")],
        "THU": [("instructorElsewhere", "AI205-S1@Term 4", "Dr A")],
    },
    "one_teacher_on": {
        "MON": [("instructor", "AI202-S1@Term 2", "Dr A")],
        "THU": [("instructorElsewhere", "AI205-S1@Term 4", "Dr A")],
    },
}


@pytest.mark.parametrize("mode", ["links_off", "links_on", "one_teacher_on"])
@pytest.mark.parametrize("day", ["MON", "TUE", "THU"])
def test_v12_a_planned_sections_teachers_are_the_publish_checks(mode, day, flags) -> None:
    """AI209-S1, not created yet, is linked and pinned to Dr A and has no
    meeting row. On the board of ``_teacher_board`` it shares Dr A with
    AI202-S1 (MON) and, on the other board, AI205-S1 (THU), by id; AI203-S1's
    row names Dr A (TUE), which the publish check never matches with a charged
    or linked teacher. With the links on and one teacher per section off the
    publish check charges it with its course's linked teachers once it is
    created; the verdict reads them before. Once placed, the publish check
    says what the verdict said."""
    _teacher_mode(flags, mode)
    sc, _movers = _teacher_board()
    planned = requirement("AI", "AI209", "PLANNED")
    link("AI", "AI209", Instructor.objects.get(full_name="Dr A"))
    budget(sc, planned, 1)
    _pin(sc, planned, "S1", "Dr A")
    board = DeliveryBoard.objects.get(scenario=sc, label="Term 2")

    preview = preview_planned_section_slot_candidates(
        board.id, course_code="AI209", course_key=planned, section_label="S1", include_verdict=True
    )
    candidate = _at(preview, day, "13:00")
    expected = _PLANNED_TEACHER_ITEMS[mode].get(day, [])
    assert _teacher_items(candidate["verdict"]) == expected
    if mode == "one_teacher_on" and day == "TUE":
        # The legacy test matches Dr A's name with AI203-S1's row.
        assert [e["kind"] for e in candidate["evidence"]] == ["instructor"]

    created = create_planned_section_placements(
        board.id,
        course_code="AI209",
        course_key=planned,
        course_name="PLANNED",
        section_label="S1",
        capacity=30,
        meetings=[{"day": day, "start_time": "13:00", "end_time": "14:15", "room": "R20"}],
    )
    [placement] = created["placements"]
    assert _published_teacher_clashes(sc, placement.id) == {
        label for kind, label, _teacher in expected if kind == "instructor"
    }


def test_a_teacher_clash_with_a_shared_section_is_one_item(flags) -> None:
    """S (Dr A) is shared: MON 13:00 on both boards. K's meeting (Dr A) at MON
    13:00 clashes with S on this board; S's copy on the other board is that
    same class, not a second clash. The planned preview's verdict (an unplaced
    K section whose row also says Dr A) says the same."""
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    mover = section(sc, "AI100", "S1")
    place(b1, mover, [("SUN", "09:00", "10:15")], room="R1", instructor_name="Dr A")
    shared = section(sc, "AI130", "S1")
    place(b1, shared, [("MON", "13:00", "14:15")], room="R5", instructor_name="Dr A")
    place(b2, shared, [("MON", "13:00", "14:15")], room="R5", instructor_name="Dr A")
    planned = section(sc, "AI100", "S2")
    TermSectionMeeting.objects.create(
        term_section=planned, day="SUN", start_time="13:00", end_time="14:15", instructor="Dr A"
    )

    move = preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True)
    place_ = preview_planned_section_slot_candidates(
        b1.id, course_code="AI100", section_label="S2", include_verdict=True
    )

    for preview in (move, place_):
        assert _teacher_items(_at(preview, "MON", "13:00")["verdict"]) == [
            ("instructor", "AI130-S1@Term 2", "Dr A")
        ]
    # At another time the copy alone is the teacher on another board.
    SectionPlacement.objects.filter(board=b2, term_section=shared).update(day="TUE")
    move = preview_placement_slot_candidates(placement_of(mover, "SUN").id, include_verdict=True)
    assert _teacher_items(_at(move, "TUE", "13:00")["verdict"]) == [
        ("instructorElsewhere", "AI130-S1@Term 4", "Dr A")
    ]


# ── V12: planned previews ─────────────────────────────────────────────────


def _planned_board(existing: str = "09:00"):
    """K-S1 meets SUN and TUE at ``existing``: at 09:00 it clashes with both C
    sections (C-S1 SUN 09:00 and MON 13:00, C-S2 TUE 09:00 and THU 13:00),
    at 10:30 with neither. T students need both, and a new two-meeting K
    section is planned. WED 13:00 is blocked."""
    end = {"09:00": "10:15", "10:30": "11:45"}[existing]
    sc, board = _scenario(blocked=[("WED", "13:00")])
    place(board, section(sc, "AI101", "S1"), [("SUN", existing, end), ("TUE", existing, end)])
    place(
        board,
        section(sc, "AI202", "S1"),
        [("SUN", "09:00", "10:15"), ("MON", "13:00", "14:15")],
        room="R2",
    )
    place(
        board,
        section(sc, "AI202", "S2"),
        [("TUE", "09:00", "10:15"), ("THU", "13:00", "14:15")],
        room="R3",
    )
    demand(sc, "AI101", range(1, T + 1))
    demand(sc, "AI202", range(1, T + 1))
    return board


def _pattern(preview: dict, *slots: tuple[str, str]) -> dict:
    return next(
        c
        for c in preview["candidates"]
        if [(m["day"], m["start"]) for m in c["meetings"]] == list(slots)
    )


def test_v12_planned_h15_is_judged_for_the_whole_pattern(flags) -> None:
    board = _planned_board()
    preview = preview_planned_section_slot_candidates(
        board.id, course_code="AI101", section_label="S2", limit=1000, include_verdict=True
    )
    # MON + THU 13:00 meets both C sections: with K-S1 clashing too, no pair
    # of sections is clash-free, so both overlaps block (meetings 0 and 1).
    blocking = _pattern(preview, ("MON", "13:00"), ("THU", "13:00"))["verdict"]
    assert sorted((i["meeting"], *_named(i)[:4]) for i in blocking["items"]) == [
        (0, "time", "critical", "students", "AI202-S1"),
        (1, "time", "critical", "students", "AI202-S2"),
    ]
    assert {i["blocked_students"] for i in blocking["items"]} == {T}
    # MON + WED 13:00 misses C-S2: a clash-free pair, so a soft overlap.
    soft = _pattern(preview, ("MON", "13:00"), ("WED", "13:00"))["verdict"]
    assert sorted((i["meeting"], *_named(i)[:4]) for i in soft["items"]) == [
        (0, "softOverlap", "warning", "students", "AI202-S1"),
        (1, "blocked", "critical", "blocked_slot", "None-None"),
    ]
    assert all(c["verdict_rank"] for c in preview["candidates"])
    assert "current_verdict" not in preview


def test_v12_an_existing_clash_free_section_keeps_a_new_pattern_soft(flags) -> None:
    """Adding a section never takes a clash-free pair away: with K-S1 clear of
    C, even the pattern that meets both C sections is a soft overlap."""
    board = _planned_board(existing="10:30")
    preview = preview_planned_section_slot_candidates(
        board.id, course_code="AI101", section_label="S2", limit=1000, include_verdict=True
    )
    verdict = _pattern(preview, ("MON", "13:00"), ("THU", "13:00"))["verdict"]
    assert sorted(
        (i["meeting"], *_named(i)[:4], i["blocked_students"]) for i in verdict["items"]
    ) == [
        (0, "softOverlap", "warning", "students", "AI202-S1", 0),
        (1, "softOverlap", "warning", "students", "AI202-S2", 0),
    ]


def test_v12_planned_items_carry_their_meeting_index_and_never_a_room(flags) -> None:
    board = _planned_board()
    preview = preview_planned_section_slot_candidates(
        board.id, course_code="AI101", section_label="S2", limit=1000, include_verdict=True
    )
    second = _pattern(preview, ("SUN", "13:00"), ("THU", "13:00"))["verdict"]
    assert [(i["meeting"], _named(i)[3]) for i in second["items"]] == [(1, "AI202-S2")]
    items = [i for c in preview["candidates"] for i in c["verdict"]["items"]]
    assert items and not [i for i in items if i["issue_kind"] in {"room", "roomElsewhere"}]
    assert not [i for i in items if i["source"] == "h15_effect"]


def _pin(sc, course_key: str, label: str, name: str) -> None:
    """The department fixes ``name`` as the section's one teacher."""
    SectionInstructor.objects.create(
        scenario=sc,
        course_key=course_key,
        section=label,
        instructor=Instructor.objects.get(full_name=name),
        source="department",
    )


@pytest.mark.parametrize("mode", ["links_off", "links_on", "one_teacher_on"])
def test_v12_a_planned_teacher_on_another_board_is_a_warning(mode, flags) -> None:
    """K-S3 and AI666-S1 share Dr A: by their rows' names with the links off,
    by their courses' links (AI111: Dr A and Dr B; AI666: Dr A) with the links
    on, and by the one-teacher map with one teacher per section."""
    _teacher_mode(flags, mode)
    rich = build_rich_fixture()
    if mode == "one_teacher_on":
        # K-S3 is one of the course's sections, pinned to Dr A, whom the map
        # charges with AI666-S1 too: one teacher, by id, whatever K-S3's row
        # says (Dr B).
        ScenarioSectionBudget.objects.filter(scenario=rich.sc, course_key=rich.k).update(
            planned_sections=3
        )
        _pin(rich.sc, rich.k, "S3", "Dr A")
        TermSectionMeeting.objects.filter(
            term_section__scenario=rich.sc,
            term_section__course_key=rich.k,
            term_section__section="S3",
        ).update(instructor="Dr B")
    preview = preview_planned_section_slot_candidates(
        rich.b1.id, course_code="AI111", course_key=rich.k, section_label="S3", include_verdict=True
    )
    verdict = _pattern(preview, ("SUN", "13:00"), ("TUE", "13:00"))["verdict"]
    assert [(i["meeting"], i["teacher"], *_named(i)) for i in verdict["items"]] == [
        (
            1,
            "Dr A",
            "instructorElsewhere",
            "warning",
            "cross_board_instructor",
            "AI666-S1",
            "Term 4",
            "TUE",
            "13:00",
        )
    ]
    # Single-meeting candidates name no meeting.
    single = preview_planned_section_slot_candidates(
        rich.b1.id, course_code="AI777", course_key=rich.p, include_verdict=True
    )
    assert {i["meeting"] for c in single["candidates"] for i in c["verdict"]["items"]} == {None}
    assert _at(single, "WED", "13:00")["verdict"]["severity"] == "critical"


# ── V13: the planned preview scores each cell once ────────────────────────


@pytest.mark.parametrize("include_verdict", [False, True])
def test_v13_a_cell_is_scored_once_however_many_patterns_use_it(include_verdict, flags) -> None:
    rich = build_rich_fixture()
    cell = ("SUN", "10:30", "11:45")  # no placement meets then
    assert not SectionPlacement.objects.filter(
        board__scenario=rich.sc, day=cell[0], start_time=cell[1]
    ).exists()
    with mock.patch.object(tws, "_time_mask", wraps=tws._time_mask) as spy:
        preview = preview_planned_section_slot_candidates(
            rich.b1.id,
            course_code="AI111",
            course_key=rich.k,
            section_label="S3",
            limit=1000,
            include_verdict=include_verdict,
        )
    using = [
        c
        for c in preview["candidates"]
        if ("SUN", "10:30") in {(m["day"], m["start"]) for m in c["meetings"]}
    ]
    assert len(using) == 4  # SUN with each other day
    assert [call.args for call in spy.call_args_list].count(cell) == 1


def test_v13_a_window_in_both_column_lists_is_scored_as_each_kind(flags) -> None:
    rich = build_rich_fixture()
    preview = preview_planned_section_slot_candidates(
        rich.b1.id, course_code="AI777", course_key=rich.p, section_label="S1"
    )
    both = [
        c
        for c in preview["candidates"]
        if (c["day"], c["start"], c["end"]) == ("SUN", "13:00", "14:15")
    ]
    assert sorted(c["kind"] for c in both) == ["lab", "lect"]
    lect, lab = sorted(both, key=lambda c: c["kind"] != "lect")
    assert lab["score"] == lect["score"] + 35


def test_v13_a_column_listed_twice_still_gives_two_candidates(flags) -> None:
    """The memo's answer is never changed by a caller: a window the column
    list repeats (saved before columns were validated) is two candidates
    with their own ranks, as before the memo."""
    rich = build_rich_fixture()
    TimetableScenario.objects.filter(id=rich.sc.id).update(
        slot_config=[*LECTURES, LECTURES[0]], lab_slot_config=[LECTURES[1]]
    )
    preview = preview_planned_section_slot_candidates(
        rich.b1.id, course_code="AI777", course_key=rich.p, section_label="S1"
    )
    twice = [c for c in preview["candidates"] if (c["day"], c["start"]) == ("SUN", "09:00")]
    assert len(twice) == 2
    assert twice[0]["rank"] != twice[1]["rank"]
    assert len({c["rank"] for c in preview["candidates"]}) == len(preview["candidates"])


@pytest.mark.parametrize("mode", ["one_teacher_off", "one_teacher_on"])
def test_v13_the_planned_json_without_verdict_is_the_oracle(mode, flags, http) -> None:
    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = mode == "one_teacher_on"
    rich = build_rich_fixture()
    requests = legacy_requests(rich)
    for name in ("planned_single", "planned_pattern"):
        got = normalise_ids(json.loads(http.get(requests[name]).content), rich.ids())
        assert json.dumps(got) == json.dumps(ORACLE[mode][name])


# ── The section's own copies and policy sections ──────────────────────────


def test_a_copy_of_the_section_at_that_very_slot_is_the_same_class(flags) -> None:
    """K-S1's Sunday meeting moves to MON 13:00, where its copy on the other
    board already sits (in R1, with Dr A): one class, so no room or teacher
    clash with itself. Its copy at THU 13:30 is at another time, so another
    class; so is its copy that starts at TUE 13:00 but ends at 14:40, and
    another section (MON 10:30)."""
    sc, b1 = _scenario()
    b2 = _second_board(sc)
    k_s1 = section(sc, "AI101", "S1")
    place(b1, k_s1, [("SUN", "09:00", "10:15")], room="R1", instructor_name="Dr A")
    place(
        b2,
        k_s1,
        [("MON", "13:00", "14:15"), ("THU", "13:30", "14:45"), ("TUE", "13:00", "14:40")],
        room="R1",
        instructor_name="Dr A",
    )
    place(
        b2,
        section(sc, "AI303", "S1"),
        [("MON", "10:30", "11:45")],
        room="R1",
        instructor_name="Dr A",
    )

    preview = preview_placement_slot_candidates(placement_of(k_s1, "SUN").id, include_verdict=True)
    copy = _at(preview, "MON", "13:00")
    assert copy["verdict"]["items"] == []
    assert copy["verdict"]["own_section_meeting"] is False  # the copy is on another board
    assert [e["kind"] for e in copy["evidence"]] == ["cross_board_room"]  # legacy unchanged
    for day, start, clashing in (
        ("MON", "10:30", "AI303-S1"),
        ("THU", "13:00", "AI101-S1"),
        ("TUE", "13:00", "AI101-S1"),
    ):
        verdict = _at(preview, day, start)["verdict"]
        assert sorted(_named(i)[:4] for i in verdict["items"]) == [
            ("instructorElsewhere", "warning", "cross_board_instructor", clashing),
            ("roomElsewhere", "warning", "cross_board_room", clashing),
        ]


def test_a_policy_section_answers_with_the_planned_verdict(flags) -> None:
    from core.services.timetable_section_policy import create_policy_section_bundle

    sc, board = _scenario()
    english = section(sc, "ENG101", "M1")
    rows = create_policy_section_bundle(
        board, english, day="SUN", start_time="09:00", end_time="12:00"
    )
    place(board, section(sc, "AI303", "S1"), [("WED", "13:00", "14:15")], room="R3")
    demand(sc, "ENG101", range(1, 6))
    demand(sc, "AI303", range(1, 6))
    legacy = preview_placement_slot_candidates(rows[0].id, offer_manual=True)
    preview = preview_placement_slot_candidates(rows[0].id, include_verdict=True, offer_manual=True)

    assert [c["pattern_id"] for c in preview["candidates"]] == [
        c["pattern_id"] for c in legacy["candidates"]
    ]
    assert legacy == preview_placement_slot_candidates(rows[0].id)  # offer=manual: no effect
    assert all(len(c["meetings"]) == 4 for c in preview["candidates"])
    assert sorted(c["verdict_rank"] for c in preview["candidates"]) == list(
        range(1, len(preview["candidates"]) + 1)
    )
    wednesday = [
        (c["pattern_id"], i["meeting"], *_named(i)[:4])
        for c in preview["candidates"]
        for i in c["verdict"]["items"]
    ]
    pm = next(c for c in preview["candidates"] if c["pattern_id"].endswith("_PM"))
    index = [m["day"] for m in pm["meetings"]].index("WED")
    assert wednesday == [
        (pm["pattern_id"], index, "softOverlap", "warning", "students", "AI303-S1")
    ]
    assert "current_verdict" not in preview
