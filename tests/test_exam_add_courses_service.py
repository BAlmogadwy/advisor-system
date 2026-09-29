"""Adding courses: the pure parts - codes, the request, kept rooms and the Fix model.

No database: these pin the rules the HTTP tests rely on, one at a time.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from core.services import exam_add_courses as adding
from core.services.exam_add_courses import AddCoursesError, assign_add_codes
from core.services.exam_kept_rooms import kept_room_packs
from core.services.exam_min_change import _build_model
from tests.exam_locks_fixtures import saved_run


def _live(*rows):
    return {
        identity: {"course_code": code, "source_course_code": source}
        for identity, code, source in rows
    }


# ── the code an added course takes ───────────────────────────────────────────


def test_an_added_course_keeps_its_live_code_when_no_exam_uses_it():
    live = _live(("a", "CS112 (1)", "CS112"), ("b", "CS112 (2)", "CS112"))
    assert assign_add_codes(["CS112 (1)"], live, {"a"}) == {"b": "CS112 (2)"}


def test_a_live_code_an_existing_exam_holds_is_renumbered_past_every_code_in_use():
    # The run saved identity B as "CS112 (2)". Live renumbering now gives B
    # "(1)" and C "(2)": C cannot take "(2)", nor "(1)", which is B's today.
    live = _live(("B", "CS112 (1)", "CS112"), ("C", "CS112 (2)", "CS112"))
    assert assign_add_codes(["CS112 (2)"], live, {"B"}) == {"C": "CS112 (3)"}


def test_the_code_does_not_depend_on_which_other_courses_are_chosen():
    live = _live(
        ("B", "CS112 (1)", "CS112"),
        ("C", "CS112 (2)", "CS112"),
        ("D", "CS112 (3)", "CS112"),
    )
    codes = assign_add_codes(["CS112 (2)"], live, {"B"})
    # Whatever is added with it, each course gets the code the list showed.
    assert codes == {"C": "CS112 (4)", "D": "CS112 (3)"}
    assert len(set(codes.values())) == len(codes)


# ── the request ──────────────────────────────────────────────────────────────


def test_only_identities_count_and_the_order_is_kept():
    assert adding.parse_added_courses(
        [{"course_identity": " b ", "course_code": "ignored"}, {"course_identity": "a"}]
    ) == ["b", "a"]


@pytest.mark.parametrize("raw", [None, {}, "x", [{"course_identity": 1}]])
def test_a_request_that_is_not_a_list_of_identities_is_refused(raw):
    with pytest.raises(AddCoursesError) as refused:
        adding.parse_added_courses(raw)
    assert refused.value.code == adding.INVALID


def test_too_many_courses_are_refused():
    with pytest.raises(AddCoursesError) as refused:
        adding.parse_added_courses(
            [{"course_identity": f"c{n}"} for n in range(adding.MAX_ADDED + 1)]
        )
    assert refused.value.field == "added_courses"


def _clean(run, **changes):
    entries = [{**entry, "course_identity": entry["course_identity"]} for entry in run["schedule"]]
    days = list(dict.fromkeys(slot["day"] for slot in run["slots"]))
    periods = list(dict.fromkeys(slot["period"] for slot in run["slots"]))
    arguments = {
        "source": run,
        "entries": entries,
        "pinned": run["pinned"],
        "linked_exams": run["linked_exams"],
        "exam_locks": run.get("exam_locks") or [],
        "days": days,
        "periods": periods,
        "max_per_day": 2,
        "thin_conflict_threshold": 0,
        "assign_rooms": True,
        **changes,
    }
    adding.require_clean_board(**arguments)


def test_the_saved_board_is_clean_and_overflow_is_one_place():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00"), "BB202": None})
    _clean(run)
    moved = deepcopy(run["schedule"])
    for entry in moved:
        if entry["day"] == "OVERFLOW":
            entry["period"] = "Extra-99"
    _clean(run, entries=moved)


def test_pins_are_compared_as_a_set_not_as_a_list():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00"), "BB202": ("W1-Mon", "08:00-10:00")})
    run["pinned"] = [
        {"course_code": "AA101", "day": "W1-Sun", "period": "08:00-10:00"},
        {"course_code": "BB202", "day": "W1-Mon", "period": "08:00-10:00"},
    ]
    _clean(run, pinned=list(reversed(run["pinned"])))
    with pytest.raises(AddCoursesError) as refused:
        _clean(run, pinned=run["pinned"][:1])
    assert refused.value.field == "pinned"


def test_the_term_comes_from_the_section_rows():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00")})
    assert adding.source_term(run) == {("1448", "1")}
    for entry in run["schedule"]:
        entry["term"] = "5"  # the stray study-plan term of older runs
    assert not adding.term_changed(run, ("1448", "1"))
    assert adding.term_changed(run, ("1449", "1"))
    assert not adding.term_changed(run, None)


# ── rooms kept where nothing changed ─────────────────────────────────────────

ROOMS = [
    {"room_code": f"R{gender}-{code}", "capacity": 40, "section": gender}
    for code in ("AA101", "BB202")
    for gender in ("M", "F")
]


def _entries(run):
    return [
        {**deepcopy(entry), "rooms": []} for entry in run["schedule"] if entry["day"] != "OVERFLOW"
    ]


def _kept(run, entries=None, sections=None, rooms=ROOMS):
    return kept_room_packs(
        source=run,
        schedule_entries=_entries(run) if entries is None else entries,
        section_enrollment=deepcopy(run["section_enrollment"]) if sections is None else sections,
        rooms=rooms,
    )


def test_an_unchanged_board_keeps_every_pack_verbatim():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00"), "BB202": ("W1-Mon", "08:00-10:00")})
    kept = _kept(run)
    assert set(kept) == {(0, "M"), (0, "F"), (2, "M"), (2, "F")}
    by_code = {entry["course_code"]: entry for entry in run["schedule"]}
    for (_slot, gender), rows in kept.items():
        for code, saved in rows.items():
            expected = [room for room in by_code[code]["rooms"] if room["gender"] == gender]
            assert json.dumps(saved) == json.dumps(expected)


def test_a_board_an_exam_joined_is_allocated_again():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00"), "BB202": ("W1-Mon", "08:00-10:00")})
    entries = _entries(run)
    for entry in entries:
        if entry["course_code"] == "BB202":
            entry.update(day="W1-Sun", period="08:00-10:00", slot_index=0)
    assert _kept(run, entries) == {}


def test_changed_registrations_reallocate_only_that_cohort():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00")})
    sections = deepcopy(run["section_enrollment"])
    for row in sections["AA101"]:
        if row["gender"] == "F":
            row["student_count"] += 1
            row["membership_fingerprint"] = "changed"
    assert set(_kept(run, sections=sections)) == {(0, "M")}


def test_a_room_gone_or_now_too_small_reallocates_its_board():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00")})
    assert set(_kept(run, rooms=[room for room in ROOMS if room["room_code"] != "RF-AA101"])) == {
        (0, "M")
    }
    small = [{**room, "capacity": 3} if room["room_code"] == "RM-AA101" else room for room in ROOMS]
    assert set(_kept(run, rooms=small)) == {(0, "F")}
    moved = [
        {**room, "section": "F"} if room["room_code"] == "RM-AA101" else room for room in ROOMS
    ]
    assert set(_kept(run, rooms=moved)) == {(0, "F")}


def test_rows_that_do_not_seat_every_group_are_never_kept():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00")})
    for room in run["schedule"][0]["rooms"]:
        if room["gender"] == "M":
            room["section_parts"][0]["student_count"] -= 1
    assert set(_kept(run)) == {(0, "F")}


def test_a_run_saved_without_rooms_keeps_nothing():
    run = saved_run({"AA101": ("W1-Sun", "08:00-10:00")})
    run["assign_rooms"] = False
    assert _kept(run) == {}
    assert (
        kept_room_packs(source=None, schedule_entries=[], section_enrollment={}, rooms=ROOMS) == {}
    )


# ── Fix asks the solver exactly what it asked before ─────────────────────────


def _proto(**kwargs):
    adj = {"A": {"B": 1}, "B": {"A": 1}}
    built = _build_model(
        ["A", "B"], {"A": 0, "B": 0, "C": 1}, adj, {("P", 1): {"A", "C"}}, 4, 2, **kwargs
    )
    return str(built.model.proto)


def test_the_set_form_of_overflow_is_the_old_model_when_it_names_every_exam_or_none():
    assert _proto(allow_unseated=True) == _proto(allow_unseated=frozenset({"A", "B"}))
    assert _proto(allow_unseated=False) == _proto(allow_unseated=frozenset())


# ── the greedy counts seats ──────────────────────────────────────────────────


def _greedy(seats):
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate(
            (day, period) for day in ("D1", "D2", "D3") for period in ("P1", "P2")
        )
    ]
    existing = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
    enrolled = {code: {number} for number, code in enumerate([*existing, "N"])}
    return adding.place_with_greedy(
        existing=existing,
        new_codes=["N"],
        adj={},
        slots=slots,
        enrolled_sets=enrolled,
        max_per_day=2,
        plan_term_buckets={},
        course_buckets={},
        credit_map={},
        locked=[],
        closed_slots=frozenset(),
        seats=seats,
    )


def test_the_greedy_takes_the_first_slot_on_a_tie_and_seats_move_it_off_a_full_one():
    # Every slot ties on Build's own scoring, so the first one wins - and A
    # already fills most of slot 0's women's seats.
    assert _greedy(None) == {"N": 0}
    ledger = adding.SeatLedger(
        {"A": {"F": 90}, "N": {"F": 40}}, {"F": 100, "M": 100}, {"A": 0, "B": 1}
    )
    assert _greedy(ledger) == {"N": 1}
