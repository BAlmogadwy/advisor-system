"""Room candidates report the quality audit's own room rule.

The Change room dialog must say, before a save, the room rows Issues will
show after it. Issues read the quality audit (``build_meeting_demands`` and
the recovery evaluator's ``_original_room_report``); room candidates had a
rule of their own. The rule now lives in one module,
``core.services.timetable_room_audit``: the audit calls it, and room
candidates call it when asked (``?audit=1``).

- **S1** the legacy oracle: without the flag, the view, bulk clean rooms and
  repair answer exactly as at ``a5d8f96``, with the same query counts
  (``tests/fixtures/tw_room_audit_legacy.json``, recorded by
  ``tests/record_tw_room_audit_legacy.py``; ids mapped to fixture symbols).
- **S2** the flag's cost; **S3**/**S4** parity with the audit after a real
  save (room only, then a slot change); **S5** the current room; **S6** the
  audit refactored without a change; **S7** where the audit and the legacy
  rule differ; **S8** meetings the audit cannot judge; **S9** cost shape;
  **S10** layering; **S11** internal callers; **S12** the view flag;
  **S13** the rows a save moves; **S14** the listing past the 60 cap.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    Student,
    TermSection,
)
from core.services import timetable_course_policy as course_policy
from core.services import timetable_exact_rooming as exact_rooming
from core.services import timetable_recovery_rooming as recovery_rooming
from core.services import timetable_repair as repair
from core.services import timetable_room_audit as rule
from core.services import timetable_workspace as tws
from core.services.timetable_exact_rooming import ExactRoomingError, build_meeting_demands
from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
from core.services.timetable_scenario_quality import _rooms, build_scenario_quality
from tests import tw_room_audit_support as support
from tests.one_teacher_support import place

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parents[1]

#: Queries the flag adds beyond the oracle's: the copies read, plus the board
#: gender of every other board in the meeting the request had not read (2 for
#: a board with students, 1 for one without). 1 for everything else.
EXTRA_QUERIES = {
    "AI302@AI": 2,
    "AI303@AI": 2,
    "AI304@AI": 2,
    "AI304@DS": 3,
    "AI305@AI": 2,
    "AI305@DS": 3,
    "AI312@AI->TUE 09:30 10:45": 2,
}


@pytest.fixture
def scenario():
    return support.build_fixture()


@pytest.fixture
def oracle() -> dict:
    return json.loads(support.ORACLE.read_text(encoding="utf-8"))


@pytest.fixture
def client(scenario):
    return support.admin_client()


@pytest.fixture
def teacher_off(settings):
    support.set_teacher_mode(settings, False)
    return settings


def _canonical_audit(audit: dict) -> dict:
    """The audit's outputs in an id-independent order.

    ``build_meeting_demands`` sorts meetings by ``str`` of a key that starts
    with the term section's id, so their order (and the report's sorted demand
    ids) follow the autoincrement values. Everything else is compared as is.
    """
    rooms = dict(audit["rooms"])
    rooms["details"] = sorted(rooms["details"], key=lambda d: d["demand_id"])
    return {
        "demands": sorted(audit["demands"], key=lambda d: d["demand_id"]),
        "original_room_report": {
            key: sorted(value) if key.endswith("_demand_ids") else value
            for key, value in audit["original_room_report"].items()
        },
        "rooms": rooms,
    }


def _assert_same(produced, expected, where: str) -> None:
    assert produced == expected, where
    assert support.dumps(produced) == support.dumps(expected), f"{where}: key order"


def _without_audit(body: dict) -> dict:
    """``body`` as the flag-off answer would be: no ``audit`` keys."""
    out = dict(body)
    out["target"] = {k: v for k, v in body["target"].items() if k != "audit"}
    out["candidates"] = [{k: v for k, v in c.items() if k != "audit"} for c in body["candidates"]]
    return out


def _subjects():
    own = [((code, board), None) for code, board in support.OWN_SLOT_SUBJECTS]
    return [*own, *support.OTHER_SLOT_SUBJECTS]


def _answer(client, code: str, board: str, slot=None, extra: str = "audit=1") -> dict:
    placement = support.placement_of(code, board)
    return support.get_json(client, support.room_candidates_url(placement, slot, extra))[1]


def _room_after_save(scenario, placement_id: int):
    """The audit's meeting holding ``placement_id`` now, and its quality detail."""
    demand = next(d for d in build_meeting_demands(scenario.id) if placement_id in d.placement_ids)
    details = {d["demand_id"]: d for d in _rooms(scenario.id)["details"]}
    return demand, details.get(demand.demand_id, {"reasons": []})


def _placements(scenario) -> dict[int, tuple]:
    return {
        row[0]: row[1:]
        for row in SectionPlacement.objects.filter(board__scenario=scenario).values_list(
            "id", "day", "start_time", "end_time", "room"
        )
    }


def _check_parity(scenario, client, placement, answer: dict, payload_for) -> int:
    """Save each listed room in a savepoint and hold the answer to the audit.

    S3/S4: ``quality_reasons`` are the meeting's quality reasons after the save
    (``clash`` aside); the requirement and compatibility are the audit's own;
    without a split, ``clash`` is there exactly when the room is occupied.
    S13: the rows the save changed are ``moving_placement_ids``.
    """
    target = answer["target"]["audit"]
    assert target["assessed"] is True
    checked = 0
    for candidate in answer["candidates"]:
        code = candidate["room_code"]
        choice = candidate["audit"]
        with support.rolled_back():
            before = _placements(scenario)
            response = client.post(
                f"/ops/tw/placements/{placement.id}/move/",
                json.dumps(payload_for(code)),
                content_type="application/json",
            )
            assert response.status_code == 200, response.content
            after = _placements(scenario)
            demand, detail = _room_after_save(scenario, placement.id)
        where = f"{placement.term_section.course_code}@{placement.board.label} -> {code}"
        if code != str(placement.room).strip() or target["save"] == "slot_change":
            changed = sorted(pid for pid in before if before[pid] != after[pid])
            assert changed == target["moving_placement_ids"], where
        assert choice["quality_reasons"] == [r for r in detail["reasons"] if r != "clash"], where
        assert (
            target["required_type"],
            target["required_capacity"],
            target["required_gender"],
        ) == (demand.required_type, demand.required_capacity, demand.required_gender), where
        assert target["placement_ids"] == list(demand.placement_ids), where
        assert choice["compatible"] == (code.upper() in demand.compatible_rooms), where
        split = len(rule.actual_rooms(demand.incumbent_rooms)) > 1
        assert ("split" in choice["reasons"]) == split, where
        assert ("unroomed_copy" in choice["reasons"]) == ("missing" in detail["reasons"]), where
        if "split" not in choice["reasons"]:
            assert ("clash" in detail["reasons"]) == bool(candidate["occupied_by"]), where
        checked += 1
    return checked


def _extra_course(scenario, code: str, demand: tuple[int, int], copies, slot) -> None:
    """A course outside the oracle's corpus (the oracle stays as recorded):
    a budget row ``(total_demand, planned_sections)``, section ``S1`` and one
    copy per ``(board symbol, room)`` at ``slot``."""
    total, planned = demand
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=support.course_key(code),
        course_code=code,
        course_name=f"COURSE {code}",
        department="AI",
        credit_hours=3,
        planned_sections=planned,
        max_per_section=40,
        total_demand=total,
        programme_term=3,
    )
    section = TermSection.objects.create(
        scenario=scenario,
        course_key=support.course_key(code),
        course_code=code,
        course_number=code,
        course_name=f"COURSE {code}",
        section="S1",
        available_capacity=30,
        source_tag="tw_auto",
    )
    labels = {symbol: label for symbol, label, _program in support.BOARDS}
    for board, room in copies:
        place(
            DeliveryBoard.objects.get(scenario=scenario, label=labels[board]),
            section,
            [slot],
            room=room,
        )


# ── S1: the legacy oracle ─────────────────────────────────────────────────


def test_s1_without_the_flag_every_answer_is_the_oracle(settings, scenario, client, oracle):
    produced = support.run_legacy_corpus(client, settings, scenario)
    for mode, _on in support.TEACHER_MODES:
        assert produced[mode]["room_candidates"].keys() == oracle[mode]["room_candidates"].keys()
        for name, entry in oracle[mode]["room_candidates"].items():
            got = produced[mode]["room_candidates"][name]
            _assert_same(got["response"], entry["response"], f"{mode} {name}")
            assert got["queries"] == entry["queries"], f"{mode} {name} queries"
        for part in ("bulk_preview", "bulk_apply"):
            _assert_same(produced[mode][part]["result"], oracle[mode][part]["result"], part)
            assert produced[mode][part]["queries"] == oracle[mode][part]["queries"], part
        _assert_same(produced[mode]["repair"], oracle[mode]["repair"], "repair")
    _assert_same(_canonical_audit(produced["audit"]), _canonical_audit(oracle["audit"]), "audit")


def test_s1_the_oracle_does_not_depend_on_id_values(settings, oracle):
    """Ids shifted by rows created and deleted first: the same answers."""
    sc = support.build_fixture()
    sc.delete()
    Room.objects.all().delete()
    Student.objects.all().delete()
    scenario = support.build_fixture()
    produced = support.run_legacy_corpus(support.admin_client(), settings, scenario)
    for mode, _on in support.TEACHER_MODES:
        for name, entry in oracle[mode]["room_candidates"].items():
            _assert_same(
                produced[mode]["room_candidates"][name]["response"], entry["response"], name
            )
    _assert_same(_canonical_audit(produced["audit"]), _canonical_audit(oracle["audit"]), "audit")


# ── S2: the flag's cost ───────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [mode for mode, _on in support.TEACHER_MODES])
def test_s2_the_flag_adds_only_the_audit_keys_and_its_queries(
    mode, settings, scenario, client, oracle
):
    support.set_teacher_mode(settings, dict(support.TEACHER_MODES)[mode])
    table = support.symbols(scenario)
    for (code, board), slot in _subjects():
        name = support.subject_name(code, board, slot)
        entry = oracle[mode]["room_candidates"][name]
        placement = support.placement_of(code, board)
        queries, body = support.get_json(
            client, support.room_candidates_url(placement, slot, "audit=1")
        )
        assert body["target"]["audit"]["assessed"] is True, name
        assert all("audit" in c for c in body["candidates"]), name
        # 12 rooms: nothing ranks past the cap, so nothing is added.
        _assert_same(support.normalise(_without_audit(body), table), entry["response"], name)
        assert queries == entry["queries"] + EXTRA_QUERIES.get(name, 1), name


# ── S3/S4/S13: parity with the audit after a real save ────────────────────


@pytest.mark.parametrize("subject", support.OWN_SLOT_SUBJECTS, ids="@".join)
def test_s3_a_room_only_save_gets_the_audit_answer_it_was_given(
    subject, teacher_off, scenario, client
):
    placement = support.placement_of(*subject)
    answer = _answer(client, *subject)
    assert answer["target"]["audit"]["save"] == "room_only"
    checked = _check_parity(scenario, client, placement, answer, lambda code: {"room": code})
    assert checked == len(support.ROOMS)


def test_s3_a_copy_spelled_differently_is_the_same_meeting(teacher_off, scenario, client):
    """The DS copies of AI302 (same room) and AI303 (another room) are stored
    as "sun": each is the audit's meeting (and the candidates' same class),
    but not a row the room-only save moves, which reads the slot as stored."""
    SectionPlacement.objects.filter(
        term_section__course_code__in=["AI302", "AI303"], board__label="DS T3"
    ).update(day="sun")
    for subject in (("AI302", "AI"), ("AI302", "DS"), ("AI303", "AI"), ("AI303", "DS")):
        placement = support.placement_of(*subject)
        answer = _answer(client, *subject)
        target = answer["target"]["audit"]
        assert target["save"] == "room_only"
        assert len(target["placement_ids"]) == 2
        assert target["moving_placement_ids"] == [placement.id]
        assert [k["placement_id"] for k in target["kept"]] != [placement.id]
        checked = _check_parity(scenario, client, placement, answer, lambda code: {"room": code})
        assert checked == len(support.ROOMS)


def test_s3_a_copy_online_on_its_own_board_is_not_part_of_the_meeting(
    teacher_off, scenario, client
):
    """AI314 is online on the DS board only: the audit leaves the DS copy out,
    so its empty room neither splits the class nor leaves it unroomed."""
    _extra_course(
        scenario, "AI314", (20, 1), (("AI", "L40"), ("DS", "")), ("TUE", "11:00", "12:15")
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="AI314",
        course_name="COURSE AI314",
        programme_term=3,
        credit_hours=3,
        is_online=True,
    )
    placement = support.placement_of("AI314", "AI")
    answer = _answer(client, "AI314", "AI")
    target = answer["target"]["audit"]
    assert target["placement_ids"] == target["moving_placement_ids"] == [placement.id]
    assert target["kept"] == []
    assert [board["board_label"] for board in target["boards"]] == ["AI T3"]
    assert _by_code(answer)["L40"]["audit"]["reasons"] == []
    assert _by_code(answer)["L40"]["audit"]["quality_reasons"] == []
    checked = _check_parity(scenario, client, placement, answer, lambda code: {"room": code})
    assert checked == len(support.ROOMS)


def test_s3_a_kept_copy_in_a_small_room_is_not_the_chosen_room_s_shortage(
    teacher_off, scenario, client
):
    """AI313 needs 33 seats; its DS copy stays in L30. Whatever room the AI copy
    gets, Issues say "Room too small" (the meeting holds L30), but only a room
    short on its own reads ``capacity`` and ``undersized``."""
    _extra_course(
        scenario, "AI313", (30, 1), (("AI", "L40"), ("DS", "L30")), ("TUE", "11:00", "12:15")
    )
    placement = support.placement_of("AI313", "AI")
    answer = _answer(client, "AI313", "AI")
    target = answer["target"]["audit"]
    assert (target["required_capacity"], target["capacity_source"]) == (33, "budget")
    assert [kept["room"] for kept in target["kept"]] == ["L30"]
    rooms = _by_code(answer)
    big = rooms["L60"]["audit"]
    assert (big["compatible"], big["undersized"], big["reasons"]) == (True, False, ["split"])
    assert big["quality_reasons"] == ["undersized", "incompatible"]
    joined = rooms["L30"]["audit"]  # the kept copy's own room: one room, too small
    assert (joined["undersized"], joined["reasons"]) == (True, ["capacity"])
    assert joined["quality_reasons"] == ["undersized"]
    checked = _check_parity(scenario, client, placement, answer, lambda code: {"room": code})
    assert checked == len(support.ROOMS)


def _payload_for(slot):
    """The move's payload for a room: the room alone, or with ``slot``."""
    if slot is None:
        return lambda code: {"room": code}
    day, start, end = slot
    return lambda code: {"day": day, "start_time": start, "end_time": end, "room": code}


@pytest.mark.parametrize(
    "subject, slot",
    [
        (("AI312", "AI"), None),
        (("AI312", "DS"), None),
        (("AI312", "AI"), ("TUE", "13:00", "14:15")),
    ],
    ids=["AI@own", "DS@own", "AI->TUE 13:00"],
)
def test_s3_a_copy_at_another_time_of_the_same_day_is_another_meeting(
    subject, slot, teacher_off, scenario, client
):
    """AI312's copies are one section on one day at two times (AI 08:00, DS
    09:30): two meetings. At its own slot, or moved to a third time of that
    day, a copy is its meeting alone: the other copy is not in the need, not
    kept, splits nothing and adds no board."""
    placement = support.placement_of(*subject)
    answer = _answer(client, *subject, slot=slot)
    target = answer["target"]["audit"]
    assert target["save"] == ("room_only" if slot is None else "slot_change")
    assert target["placement_ids"] == target["moving_placement_ids"] == [placement.id]
    assert target["kept"] == []
    assert [board["board_id"] for board in target["boards"]] == [placement.board_id]
    assert not any("split" in c["audit"]["reasons"] for c in answer["candidates"])
    checked = _check_parity(scenario, client, placement, answer, _payload_for(slot))
    assert checked == len(support.ROOMS)


def test_s13_unroomed_and_unassigned_copies_move_together(teacher_off, scenario, client):
    """ "" and UNASSIGNED are both "no room": a save moves both copies."""
    SectionPlacement.objects.filter(term_section__course_code="AI304", board__label="AI T3").update(
        room="UNASSIGNED"
    )
    placement = support.placement_of("AI304", "AI")
    answer = _answer(client, "AI304", "AI")
    target = answer["target"]["audit"]
    assert target["moving_placement_ids"] == target["placement_ids"]
    assert target["kept"] == [] and target["current"] is None
    _check_parity(scenario, client, placement, answer, lambda code: {"room": code})


@pytest.mark.parametrize("subject, slot", support.OTHER_SLOT_SUBJECTS, ids=lambda v: str(v))
def test_s4_a_slot_change_gets_the_audit_answer_it_was_given(
    subject, slot, teacher_off, scenario, client
):
    placement = support.placement_of(*subject)
    answer = _answer(client, *subject, slot=slot)
    target = answer["target"]["audit"]
    assert target["save"] == "slot_change"
    assert target["moving_placement_ids"] == [placement.id]
    day, start, end = slot
    checked = _check_parity(
        scenario,
        client,
        placement,
        answer,
        lambda code: {"day": day, "start_time": start, "end_time": end, "room": code},
    )
    assert checked == len(support.ROOMS)


def test_s4_the_meeting_joined_and_the_new_duration_decide(teacher_off, scenario, client):
    joined = _answer(client, "AI312", "AI", slot=("TUE", "09:30", "10:45"))["target"]["audit"]
    ds = support.placement_of("AI312", "DS")
    assert [k["placement_id"] for k in joined["kept"]] == [ds.id]
    assert joined["kept"][0]["room"] == "L30D"
    lecture = _answer(client, "AI309", "AI", slot=("MON", "16:00", "17:15"))["target"]["audit"]
    assert _answer(client, "AI309", "AI")["target"]["audit"]["required_type"] == "lab"
    assert lecture["required_type"] == "lecture"
    # Cut to 75 minutes by its end alone: a time change, and a lecture.
    shorter = _answer(client, "AI309", "AI", slot=("MON", "11:00", "12:15"))["target"]["audit"]
    assert (shorter["save"], shorter["required_type"]) == ("slot_change", "lecture")


@pytest.mark.parametrize(
    "slot",
    [
        ("SUN", "09:30", "11:20"),  # the end alone
        ("SUN", "09:00", "10:45"),  # the start alone
        ("MON", "09:30", "10:45"),  # the day alone
    ],
    ids=["end", "start", "day"],
)
def test_s4_one_part_of_the_slot_changed_is_a_slot_change(slot, teacher_off, scenario, client):
    """The move view reads a change to any one of day, start and end as a time
    change (``time_changed``): AI302's AI copy (SUN 09:30-10:45, its DS copy
    in the same room) leaves the DS copy's meeting and moves alone."""
    placement = support.placement_of("AI302", "AI")
    answer = _answer(client, "AI302", "AI", slot=slot)
    target = answer["target"]["audit"]
    assert target["save"] == "slot_change"
    assert target["placement_ids"] == target["moving_placement_ids"] == [placement.id]
    assert target["kept"] == []
    checked = _check_parity(scenario, client, placement, answer, _payload_for(slot))
    assert checked == len(support.ROOMS)


# ── S5: the current room ──────────────────────────────────────────────────


def test_s5_the_current_room_is_the_audit_answer_today(teacher_off, scenario, client):
    details = {d["demand_id"]: d for d in _rooms(scenario.id)["details"]}
    demands = build_meeting_demands(scenario.id)
    seen_unknown = False
    for code, board in support.OWN_SLOT_SUBJECTS:
        placement = support.placement_of(code, board)
        current = _answer(client, code, board)["target"]["audit"]["current"]
        if not tws._room_key(placement.room):
            assert current is None, (code, board)
            continue
        demand = next(d for d in demands if placement.id in d.placement_ids)
        reasons = details.get(demand.demand_id, {"reasons": []})["reasons"]
        assert current["quality_reasons"] == [r for r in reasons if r != "clash"], (code, board)
        assert current["compatible"] == (placement.room.upper() in demand.compatible_rooms)
        seen_unknown |= placement.room == "GHOST1"
    assert seen_unknown
    ghost = _answer(client, "AI305", "DS")["target"]["audit"]["current"]
    assert ghost == {
        "compatible": False,
        "undersized": False,
        "reasons": ["split"],
        "programme_board_ids": [],
        "quality_reasons": ["incompatible"],
    }


# ── S6: the audit, refactored without a change ────────────────────────────


def test_s6_the_audit_outputs_are_the_oracle(scenario, oracle):
    table = support.symbols(scenario)
    produced = {
        "demands": support.normalise(
            [asdict(d) for d in build_meeting_demands(scenario.id)], table
        ),
        "original_room_report": support.normalise(
            build_recovery_room_evaluator(scenario.id).original_room_report, table
        ),
        "rooms": support.normalise(_rooms(scenario.id), table),
    }
    _assert_same(_canonical_audit(produced), _canonical_audit(oracle["audit"]), "audit")


def test_s6_the_gender_conflict_still_fails_the_audit_closed(scenario):
    _second_gender_board(scenario)
    with pytest.raises(ExactRoomingError, match=r"AI302\|S1 on SUN 09:30 spans boards with"):
        build_meeting_demands(scenario.id)
    assert build_scenario_quality(scenario.id)["rooms"]["assessed"] is False


# ── S7: where the audit and the legacy rule differ ────────────────────────


def _by_code(answer: dict) -> dict[str, dict]:
    return {c["room_code"]: c for c in answer["candidates"]}


def test_s7_the_audit_rule_not_the_legacy_one(teacher_off, scenario, client):
    ids = {b.label[:2]: b.id for b in DeliveryBoard.objects.filter(scenario=scenario)}
    single = _by_code(_answer(client, "AI301", "AI"))
    legacy_fit = ("fits_type", "fits_gender", "fits_capacity", "department_fit")
    for code, reasons in (
        ("LX40", ["programme"]),  # (a) no department: fits the legacy pool
        ("LN40", ["gender"]),  # (b) no gender: fits the legacy gender
        ("L25", ["capacity"]),  # (c) 25 seats: the legacy need is 25, the audit's 26
        ("LT40", ["type"]),  # (k) a type of spaces: "lecture" to the legacy rule
    ):
        assert all(single[code][key] for key in legacy_fit), code
        assert single[code]["audit"]["reasons"] == reasons, code
    assert single["L25"]["audit"]["undersized"] is True
    assert single["L25"]["audit"]["compatible"] is True

    shared = _by_code(_answer(client, "AI302", "AI"))
    # (c) 30 seats are short of the legacy 35 and enough for the audit's 22.
    assert shared["L30"]["fits_capacity"] is False
    assert shared["L30"]["audit"]["reasons"] == [] and shared["L30"]["audit"]["compatible"]
    # (d) the pool of every board: L25 serves AI only, L30D DS only.
    assert shared["L25"]["department_fit"] is True
    assert shared["L25"]["audit"]["programme_board_ids"] == [ids["DS"]]
    assert shared["L30D"]["audit"]["programme_board_ids"] == [ids["AI"]]
    assert shared["LX40"]["audit"]["programme_board_ids"] == [ids["AI"], ids["DS"]]

    # (e) a board with no programme: every room is outside it.
    nowhere = _answer(client, "AI310", "NP")
    assert all(c["department_fit"] for c in nowhere["candidates"])
    for candidate in nowhere["candidates"]:
        assert "programme" in candidate["audit"]["reasons"], candidate["room_code"]
        assert candidate["audit"]["programme_board_ids"] == [ids["NP"]]
    assert nowhere["target"]["audit"]["boards"] == [
        {"board_id": ids["NP"], "board_label": "NP T3", "programmes": []}
    ]

    # (l) and the default need: no demand needs 0 seats; no budget row, 40.
    empty = _answer(client, "AI308", "AI")
    assert empty["target"]["audit"]["required_capacity"] == 0
    assert not any("capacity" in c["audit"]["reasons"] for c in empty["candidates"])
    unbudgeted = _answer(client, "AI306", "AI")["target"]["audit"]
    assert (unbudgeted["required_capacity"], unbudgeted["capacity_source"]) == (40, "default")
    planned_none = _answer(client, "AI307", "AI")["target"]["audit"]
    assert (planned_none["required_capacity"], planned_none["capacity_source"]) == (44, "budget")


def test_s7_the_answer_shape(teacher_off, scenario, client):
    split = _answer(client, "AI303", "AI")
    target = split["target"]["audit"]
    ai, ds = (support.placement_of("AI303", board) for board in ("AI", "DS"))
    assert list(target) == [
        "assessed",
        "save",
        "required_type",
        "required_capacity",
        "capacity_source",
        "required_gender",
        "boards",
        "placement_ids",
        "moving_placement_ids",
        "kept",
        "current",
    ]
    assert target["boards"] == [
        {"board_id": ai.board_id, "board_label": "AI T3", "programmes": ["AI"]},
        {"board_id": ds.board_id, "board_label": "DS T3", "programmes": ["DS"]},
    ]
    assert target["kept"] == [
        {"placement_id": ds.id, "board_id": ds.board_id, "board_label": "DS T3", "room": "L30D"}
    ]
    assert (target["placement_ids"], target["moving_placement_ids"]) == ([ai.id, ds.id], [ai.id])
    rooms = _by_code(split)
    assert list(rooms["L40"]) == [*list(rooms["L40"])[:-1], "audit"]
    assert rooms["LX40"]["audit"] == {
        "compatible": False,
        "undersized": False,
        "reasons": ["programme", "split"],
        "programme_board_ids": [ai.board_id, ds.board_id],
        "quality_reasons": ["incompatible"],
    }
    unroomed = _by_code(_answer(client, "AI304", "AI"))["L40"]["audit"]
    assert unroomed["reasons"] == ["unroomed_copy"]
    assert unroomed["quality_reasons"] == ["missing"]
    small = _by_code(_answer(client, "AI307", "AI"))["LT40"]["audit"]
    assert small["reasons"] == ["type", "capacity"]
    assert small["quality_reasons"] == ["undersized", "incompatible"]


def test_s7_a_policy_course_in_a_shared_room_reads_its_budget(
    teacher_off, scenario, client, monkeypatch
):
    """The legacy fields read no budget for a policy course (its type is fixed),
    so the audit reads the budget itself. No policy uses a shared room today:
    the English one is made to here."""
    monkeypatch.setattr(
        course_policy,
        "ENGLISH_INTENSIVE_POLICY",
        dataclasses.replace(course_policy.ENGLISH_INTENSIVE_POLICY, uses_shared_room=True),
    )
    _extra_course(scenario, "ENG101", (24, 1), (("AI", "L40"),), ("TUE", "11:00", "12:15"))
    placement = support.placement_of("ENG101", "AI")
    answer = _answer(client, "ENG101", "AI")
    target = answer["target"]["audit"]
    (demand,) = [d for d in build_meeting_demands(scenario.id) if placement.id in d.placement_ids]
    assert (target["required_capacity"], target["capacity_source"]) == (26, "budget")
    assert target["required_capacity"] == demand.required_capacity
    assert _by_code(answer)["L25"]["audit"]["reasons"] == ["capacity"]
    # The flag's cost here: the copies, and the budget the request skipped.
    off, _ = support.get_json(client, support.room_candidates_url(placement))
    on, _ = support.get_json(client, support.room_candidates_url(placement, None, "audit=1"))
    assert on == off + 2


# ── S8: meetings the audit cannot judge ───────────────────────────────────


def _assert_not_assessed(body: dict, reason: str) -> None:
    assert body["target"]["audit"] == {"assessed": False, "reason": reason}
    assert not any("audit" in c for c in body["candidates"])


def _second_gender_board(scenario) -> DeliveryBoard:
    """A female board holding a copy of AI302 (label S1) at its slot."""
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="AIF T3", nominal_term=3, program="AI", display_order=9
    )
    Student.objects.create(student_id=9101, name="F1", section="F")
    BoardStudentLink.objects.create(board=board, student_id=9101)
    place(
        board, TermSection.objects.get(course_code="AI302"), [("SUN", "09:30", "10:45")], room="L40"
    )
    return board


def test_s8_duplicate_room_codes_are_not_assessed(teacher_off, scenario, client):
    Room.objects.create(room_code="l40 ", capacity=40, room_type="lecture", section="F")
    plain = _answer(client, "AI301", "AI", extra="")
    body = _answer(client, "AI301", "AI")
    _assert_not_assessed(body, "duplicate_room_codes")
    assert _without_audit(body) == plain  # no listing past the cap either
    assert build_scenario_quality(scenario.id)["rooms"]["assessed"] is False


def test_s8_boards_that_disagree_on_gender_are_not_assessed(teacher_off, scenario, client):
    _second_gender_board(scenario)
    _assert_not_assessed(_answer(client, "AI302", "AI"), "boards_disagree_on_gender")


def test_s8_a_course_online_only_by_its_display_code_is_not_assessed(teacher_off, scenario, client):
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI390",
        course_name="ONLINE",
        programme_term=3,
        credit_hours=3,
        is_online=True,
    )
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="AI390::ONLINE",
        course_code="AI",
        course_number="390",
        course_name="ONLINE",
        section="M1",
    )
    board = DeliveryBoard.objects.get(scenario=scenario, label="AI T3")
    place(board, section, [("TUE", "13:00", "14:15")], room="L40")
    placement = SectionPlacement.objects.get(term_section=section)
    body = support.get_json(client, support.room_candidates_url(placement, None, "audit=1"))[1]
    assert body["target"]["is_online"] is False and body["candidates"]  # rooms, to the legacy rule
    _assert_not_assessed(body, "online")


def test_s8_an_unknown_day_or_a_reversed_interval_is_not_assessed(teacher_off, scenario, client):
    _assert_not_assessed(
        _answer(client, "AI301", "AI", slot=("XYZ", "08:00", "09:15")), "unknown_day"
    )
    _assert_not_assessed(
        _answer(client, "AI301", "AI", slot=("SUN", "10:00", "09:00")), "invalid_interval"
    )
    _assert_not_assessed(
        _answer(client, "AI301", "AI", slot=("SUN", "10:00", "10:00")), "invalid_interval"
    )
    # The legacy rule reads 9:00:00 as 09:00; the audit's HH:MM is "9:00:", which
    # it cannot parse (a 422 to the quality audit, never a 500 here).
    _assert_not_assessed(
        _answer(client, "AI301", "AI", slot=("SUN", "9:00:00", "10:15")), "invalid_interval"
    )


def test_s8_a_meeting_that_takes_no_room_still_answers_the_flag(teacher_off, scenario, client):
    """A policy course with no shared room and a course online on its board by
    its course code answer before any room is read; ``?audit=1`` still gets
    ``target.audit``, and without it the answer has no ``audit`` key."""
    _extra_course(scenario, "ENG101", (24, 1), (("AI", "L40"),), ("TUE", "11:00", "12:15"))
    _extra_course(scenario, "AI315", (20, 1), (("DS", "L40"),), ("TUE", "13:00", "14:15"))
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="AI315",
        course_name="COURSE AI315",
        programme_term=3,
        credit_hours=3,
        is_online=True,
    )
    for (code, board), flag, reason in (
        (("ENG101", "AI"), "no_room_required", "no_room_required"),
        (("AI315", "DS"), "is_online", "online"),
    ):
        plain = _answer(client, code, board, extra="")
        body = _answer(client, code, board)
        assert plain["target"][flag] is True and plain["candidates"] == [], code
        assert "audit" not in plain["target"], code
        assert body["target"].pop("audit") == {"assessed": False, "reason": reason}, code
        assert body == plain, code


# ── S9: cost shape ────────────────────────────────────────────────────────


def _filler_rooms(count: int, capacity: int = 36, prefix: str = "FIT") -> None:
    for n in range(1, count + 1):
        Room.objects.create(
            room_code=f"{prefix}{n:02d}",
            capacity=capacity,
            room_type="lecture",
            section="M",
            department="AI,DS",
        )


def test_s9_the_audit_costs_the_same_for_5_or_60_more_rooms(
    teacher_off, scenario, client, monkeypatch
):
    calls = {"room_misfits": 0, "meeting_room_need": 0, "audit_room_inventory": 0, "choice": 0}

    def counting(name, real):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)

        return wrapper

    for name in ("room_misfits", "meeting_room_need", "audit_room_inventory"):
        monkeypatch.setattr(rule, name, counting(name, getattr(rule, name)))
    monkeypatch.setattr(
        rule.MeetingRoomAudit, "choice", counting("choice", rule.MeetingRoomAudit.choice)
    )
    placement = support.placement_of("AI302", "AI")
    url = support.room_candidates_url(placement, None, "audit=1")

    def measure():
        for name in calls:
            calls[name] = 0
        with CaptureQueriesContext(connection) as ctx:
            body = json.loads(client.get(url).content)
        room_reads = [q for q in ctx.captured_queries if 'FROM "rooms"' in q["sql"]]
        return len(ctx.captured_queries), len(room_reads), dict(calls), body

    _filler_rooms(5, prefix="FEW")
    few, few_room_reads, few_calls, few_body = measure()
    _filler_rooms(60)
    many, many_room_reads, many_calls, many_body = measure()
    assert few == many
    assert few_room_reads == many_room_reads == 1
    rooms = len(support.ROOMS)
    for got, total in ((few_calls, rooms + 5), (many_calls, rooms + 65)):
        assert got["room_misfits"] == total  # once per code
        assert got["meeting_room_need"] == got["audit_room_inventory"] == 1
        assert got["choice"] == total + 1  # every candidate, and the current room
    assert few_body["summary"]["total"] == rooms + 5
    assert many_body["summary"]["total"] == rooms + 65


# ── S10: layering ─────────────────────────────────────────────────────────


def test_s10_the_rule_module_is_light_and_owns_the_moved_names():
    snippet = (
        "import json, sys, django\n"
        "django.setup()\n"
        "import core.services.timetable_room_audit\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'ortools'"
        " or m in ('core.services.timetable_workspace', 'core.services.timetable_exact_rooming',"
        " 'core.services.timetable_recovery_rooming'))))\n"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings"}
    done = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
    assert exact_rooming.ExactRoomingError is rule.ExactRoomingError
    assert exact_rooming._hhmm is rule.hhmm and exact_rooming._to_min is rule.to_min
    assert recovery_rooming._DAYS is rule.AUDIT_DAYS
    with pytest.raises(rule.ExactRoomingError, match="Un-parseable placement time"):
        exact_rooming._to_min("9h")


# ── S11: internal callers ─────────────────────────────────────────────────


def test_s11_internal_callers_never_ask_for_the_audit(teacher_off, scenario, monkeypatch):
    signature = inspect.signature(tws.preview_placement_room_candidates)
    assert signature.parameters["include_audit"].default is False
    seen = []
    real = tws.preview_placement_room_candidates

    def spy(*args, **kwargs):
        seen.append(kwargs.get("include_audit", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(tws, "preview_placement_room_candidates", spy)
    monkeypatch.setattr(repair, "preview_placement_room_candidates", spy)
    tws.preview_bulk_clean_room_assignments(scenario.id)
    with support.rolled_back():
        tws.apply_bulk_clean_room_assignments(scenario.id)
    placement = support.placement_of("AI301", "AI")
    repair._select_room_for_candidate(placement, "SUN", "08:00", "09:15")
    assert seen and not any(seen)


# ── S12: the view flag ────────────────────────────────────────────────────


def test_s12_only_audit_1_turns_it_on(teacher_off, scenario, client, oracle):
    table = support.symbols(scenario)
    expected = oracle["one_teacher_off"]["room_candidates"]["AI301@AI"]["response"]
    for extra in ("audit=0", "audit=true", "audit=yes", "audit=", "audit=1%20", "audit=01"):
        body = _answer(client, "AI301", "AI", extra=extra)
        _assert_same(support.normalise(body, table), expected, extra)
    assert _answer(client, "AI301", "AI")["target"]["audit"]["assessed"] is True


# ── S14: the listing past the cap ─────────────────────────────────────────


def test_s14_past_the_cap_every_room_not_refused_for_gender_is_listed(
    teacher_off, scenario, client, monkeypatch
):
    _filler_rooms(60)  # 36 seats: each fits the legacy 35 and ranks first
    # Refused for gender and for something else too: still refused.
    Room.objects.create(
        room_code="LFLAB", capacity=40, room_type="lab", section="F", department="AI,DS"
    )
    Room.objects.create(
        room_code="LFOUT", capacity=40, room_type="lecture", section="F", department="CS"
    )
    gender_refused = {"LF40": ["gender"], "LN40": ["gender"]}
    gender_refused |= {"LFLAB": ["type", "gender"], "LFOUT": ["gender", "programme"]}
    plain = _answer(client, "AI302", "AI", extra="")
    audited = _answer(client, "AI302", "AI")
    plain_codes = [c["room_code"] for c in plain["candidates"]]
    audited_codes = [c["room_code"] for c in audited["candidates"]]
    # The legacy listing: the cap, then the current room.
    assert plain_codes[-1] == "L40" and len(plain_codes) == tws._ROOM_CANDIDATE_LIMIT + 1
    # The audit listing starts with exactly the legacy one...
    assert _without_audit(audited)["candidates"][: len(plain_codes)] == plain["candidates"]
    assert audited["summary"] == plain["summary"]
    # ...then every other room in rank order, but the ones refused for gender.
    monkeypatch.setattr(tws, "_ROOM_CANDIDATE_LIMIT", 10**6)
    everything = _answer(client, "AI302", "AI")["candidates"]
    ranked = [c["room_code"] for c in everything]
    assert audited_codes[len(plain_codes) :] == [
        code for code in ranked if code not in plain_codes and code not in gender_refused
    ]
    for code in ("BUSY40", "L30"):  # in use and fitting; short to the legacy rule only
        assert code in audited_codes and code not in plain_codes
    for code, reasons in gender_refused.items():
        assert _by_code({"candidates": everything})[code]["audit"]["reasons"] == reasons, code
        assert code not in audited_codes and code not in plain_codes, code
    assert all("gender" not in c["audit"]["reasons"] for c in audited["candidates"][61:])
