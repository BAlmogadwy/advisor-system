"""The scheduler, the feasibility check, the invigilator pass and QA keep a link whole.

A link is examined as one sitting: same day, same period, pinned and sent to
OVERFLOW together. Everything else stays per real course - the load and credit
metrics, the rooms, the rosters. The last tests are randomised: several links
per board, pins, too few slots, students in two linked courses, and not one
stage may ever come back with a link split.
"""

import functools
import itertools
import random

import pytest

from core.services import exam_timetable
from core.services.exam_room_allocation import RoomAllocationContext
from core.services.exam_timetable import (
    _build_qa,
    _rebalance_invigilators_pass,
    apply_thin_conflict_policy,
    assign_rooms_to_schedule,
    attach_exam_relaxation_qa,
    build_conflict_graph,
    check_linked_bucket_feasibility,
    schedule_linked,
)
from core.services.linked_exams import NO_LINKS, LinkedExamsError, resolve_linked_exams

SLOTS = [
    {"index": 0, "day": "Sun", "period": "P1"},
    {"index": 1, "day": "Sun", "period": "P2"},
    {"index": 2, "day": "Mon", "period": "P1"},
    {"index": 3, "day": "Mon", "period": "P2"},
]


def links_of(*groups, codes=None):
    """Links between plain codes, where each code is its own identity."""
    every = set(codes or ()) | {code for group in groups for code in group}
    meta = {code: {"course_identity": code} for code in every}
    raw = [{"members": [{"course_identity": code} for code in group]} for group in groups]
    return resolve_linked_exams(raw, meta)


def placed(entries):
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in entries}


# ── the scheduler ────────────────────────────────────────────────────────────


def test_every_member_of_a_link_is_placed_at_one_slot():
    enrolled = {"A": {1, 2}, "B": {3}, "C": {1, 3}, "D": {4}}
    _, adj = build_conflict_graph(enrolled)
    entries = schedule_linked(
        ["A", "B", "C", "D"], adj, SLOTS, links=links_of(("A", "B")), enrolled_sets=enrolled
    )
    where = placed(entries)
    assert where["A"] == where["B"]
    assert where["C"] != where["A"], "C shares a student with each member, so with the link"
    assert entries == sorted(entries, key=lambda entry: (entry["slot_index"], entry["course_code"]))


def test_a_pin_on_one_member_fixes_the_whole_link():
    pins = [{"course_code": "B", "day": "Mon", "period": "P2"}]
    entries = schedule_linked(
        ["A", "B", "C"], {}, SLOTS, links=links_of(("A", "B")), pinned=pins, enrolled_sets={}
    )
    assert placed(entries)["A"] == placed(entries)["B"] == ("Mon", "P2")


def test_a_link_nothing_can_seat_goes_to_overflow_whole_under_one_extra():
    """Two slots, both held by a course that shares a student with each member."""
    two_slots = SLOTS[:2]
    enrolled = {"A": {1}, "B": {2}, "X": {1, 2}, "Y": {1, 2}}
    _, adj = build_conflict_graph(enrolled)
    pins = [
        {"course_code": "X", "day": "Sun", "period": "P1"},
        {"course_code": "Y", "day": "Sun", "period": "P2"},
    ]
    entries = schedule_linked(
        ["A", "B", "X", "Y"],
        adj,
        two_slots,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        pinned=pins,
    )
    overflow = [entry for entry in entries if entry["day"] == "OVERFLOW"]
    assert [entry["course_code"] for entry in overflow] == ["A", "B"]
    assert {entry["period"] for entry in overflow} == {"Extra-2"}
    assert {entry["slot_index"] for entry in overflow} == {2}


def test_progress_is_counted_in_real_courses():
    calls = []
    schedule_linked(
        ["A", "B", "C", "D", "E"],
        {},
        SLOTS,
        links=links_of(("A", "B", "C")),
        enrolled_sets={code: {1} for code in "ABCDE"},
        on_placed=lambda done, total: calls.append((done, total)),
    )
    assert calls[0] == (0, 5) and calls[-1] == (5, 5)
    assert {total for _, total in calls} == {5}
    assert [done for done, _ in calls] == sorted(done for done, _ in calls)


def test_a_mixed_credit_link_is_scored_on_what_each_student_sits():
    """U links A (3 credits: students 1-3) with B (4 credits: student 4).

    Sunday already holds X (4 credits) for students 1-3; Monday holds Y (4
    credits) for student 4. Each student's real pair is what counts: Sunday
    costs three (4, 3) pairs = 90, Monday one (4, 4) pair = 100, so U belongs
    on Sunday. Scored as if everyone sat U's heaviest member, Sunday would cost
    three (4, 4) pairs = 300 and U would go to Monday.
    """
    enrolled = {"A": {1, 2, 3}, "B": {4}, "X": {1, 2, 3}, "Y": {4}}
    _, adj = build_conflict_graph(enrolled)
    pins = [
        {"course_code": "X", "day": "Sun", "period": "P1"},
        {"course_code": "Y", "day": "Mon", "period": "P1"},
    ]
    entries = schedule_linked(
        ["A", "B", "X", "Y"],
        adj,
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        pinned=pins,
        credit_map={"A": 3, "B": 4, "X": 4, "Y": 4},
    )
    assert placed(entries)["A"] == placed(entries)["B"] == ("Sun", "P2")


def test_a_placed_mixed_credit_link_leaves_each_student_the_credit_they_sit():
    """The link A (3 credits, students 1-3) + B (4) is pinned first, to Sunday.

    X (4 credits, students 1-3) then weighs Sunday, where each of its students
    already sits a 3-credit paper - three (4, 3) pairs = 90 - against Monday,
    where Y (4) gives student 1 a (4, 4) pair = 100. Recorded as the link's
    heaviest member, Sunday would cost 300 and X would go to Monday.
    """
    enrolled = {"A": {1, 2, 3}, "B": {4}, "X": {1, 2, 3}, "Y": {1}}
    _, adj = build_conflict_graph(enrolled)
    pins = [
        {"course_code": "A", "day": "Sun", "period": "P1"},
        {"course_code": "Y", "day": "Mon", "period": "P1"},
    ]
    entries = schedule_linked(
        ["A", "B", "X", "Y"],
        adj,
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        pinned=pins,
        credit_map={"A": 3, "B": 4, "X": 4, "Y": 4},
    )
    assert placed(entries)["X"] == ("Sun", "P2")


def test_a_link_counts_every_members_students_on_their_day():
    """Student 1 (in A) already sits X on Sunday, and one exam a day is the limit.

    The link must see A's students: Monday, though busier, keeps student 1 to
    one exam a day. Blind to them it would take quiet Sunday P2.
    """
    enrolled = {"A": {1}, "B": {2}, "X": {1}, "Y": {3}, "Z": {4}}
    _, adj = build_conflict_graph(enrolled)
    pins = [
        {"course_code": "X", "day": "Sun", "period": "P1"},
        {"course_code": "Y", "day": "Mon", "period": "P1"},
        {"course_code": "Z", "day": "Mon", "period": "P2"},
    ]
    entries = schedule_linked(
        ["A", "B", "X", "Y", "Z"],
        adj,
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        max_per_day=1,
        pinned=pins,
    )
    assert placed(entries)["A"][0] == placed(entries)["B"][0] == "Mon"


def test_a_uniform_link_is_scored_at_its_members_credit():
    """A and B are both 4-credit exams. Beside X (2 credits) on Sunday a
    student's pair scores (4, 2) = 0; beside Y (3) on Monday, (4, 3) = 30.

    Scored at the 3-credit default instead, both days tie at 5 and the
    lighter Monday would win.
    """
    enrolled = {"A": {1}, "B": {2}, "X": {1}, "Y": {2}, "W": {9}}
    _, adj = build_conflict_graph(enrolled)
    pins = [
        {"course_code": "X", "day": "Sun", "period": "P1"},
        {"course_code": "W", "day": "Sun", "period": "P2"},
        {"course_code": "Y", "day": "Mon", "period": "P1"},
    ]
    entries = schedule_linked(
        ["A", "B", "W", "X", "Y"],
        adj,
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        pinned=pins,
        credit_map={"A": 4, "B": 4, "X": 2, "Y": 3, "W": 3},
    )
    assert placed(entries)["A"] == placed(entries)["B"] == ("Sun", "P2")


def test_a_link_keeps_off_the_day_of_a_bucket_mate():
    """C shares B's study-plan term and sits on Sunday. The link may not join
    it there, though Monday is busier. Without B's bucket the link would take
    quiet Sunday P2 - two exams of one term on one day."""
    buckets = {("AI", 1): {"B", "C"}}
    course_buckets = {"B": [("AI", 1)], "C": [("AI", 1)]}
    enrolled = {"A": {1}, "B": {2}, "C": {3}, "Y": {4}, "Z": {5}}
    pins = [
        {"course_code": "C", "day": "Sun", "period": "P1"},
        {"course_code": "Y", "day": "Mon", "period": "P1"},
        {"course_code": "Z", "day": "Mon", "period": "P2"},
    ]
    entries = schedule_linked(
        ["A", "B", "C", "Y", "Z"],
        {},
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        plan_term_buckets=buckets,
        course_buckets=course_buckets,
        pinned=pins,
    )
    assert placed(entries)["A"][0] == placed(entries)["B"][0] == "Mon"


def test_a_link_keeps_the_slot_its_largest_member_prefers():
    """Optimise hands the scheduler each exam's current slot as its preference.

    B, the larger member, prefers Monday P2 and A Sunday P2: the link keeps
    B's slot. Nothing else pulls it anywhere - left without a preference, the
    unit would take the first free slot.
    """
    enrolled = {"A": {1}, "B": {2, 3}}
    entries = schedule_linked(
        ["A", "B"],
        {},
        SLOTS,
        links=links_of(("A", "B")),
        enrolled_sets=enrolled,
        preferred_slots={"A": 1, "B": 3},
    )
    assert placed(entries)["A"] == placed(entries)["B"] == ("Mon", "P2")


def test_a_linked_course_outside_the_selection_is_refused():
    with pytest.raises(ValueError, match="not selected"):
        schedule_linked(["A", "C"], {}, SLOTS, links=links_of(("A", "B"), codes="C"))


def test_a_link_needs_one_day_of_its_bucket_not_one_per_member():
    buckets = {("AI", 1): {"A", "B", "C", "D"}}
    assert check_linked_bucket_feasibility(buckets, 3) != []
    assert check_linked_bucket_feasibility(buckets, 3, links=links_of(("A", "B"))) == []
    violations = check_linked_bucket_feasibility(buckets, 2, links=links_of(("A", "B")))
    assert violations == [
        {
            "program": "AI",
            "programme_term": 1,
            "bucket_size": 3,
            "num_days": 2,
            "courses": ["A", "B", "C", "D"],
        }
    ]


def test_pins_on_a_link_reserve_one_day_for_it():
    buckets = {("AI", 1): {"A", "B", "C"}}
    pins = [{"course_code": "A", "day": "Sun", "period": "P1"}]
    assert check_linked_bucket_feasibility(buckets, 2, pins, links_of(("A", "B"))) == []


def test_a_pinned_link_shares_its_pinned_day_like_any_pinned_exam():
    """The registrar pinned the link (through A) and C to one day: a deliberate
    override, so that day serves both, and D needs the other - two days do.
    Read as pins on real courses, the link's pin would count for nothing."""
    buckets = {("AI", 1): {"A", "B", "C", "D"}}
    pins = [
        {"course_code": "A", "day": "Sun", "period": "P1"},
        {"course_code": "C", "day": "Sun", "period": "P2"},
    ]
    assert check_linked_bucket_feasibility(buckets, 2, pins, links_of(("A", "B"))) == []
    assert check_linked_bucket_feasibility(buckets, 1, pins, links_of(("A", "B"))) != []


# ── the invigilator pass ─────────────────────────────────────────────────────


def _hot_sunday():
    """Four 50-student exams on Sunday (8 invigilators) against one on Monday (2).

    The link A+B is the largest unit on the hot day, so it is tried first, and
    moving it - both members - makes the days 4 and 6: a flatter board.
    """
    entries = [
        {"course_code": code, "day": day, "period": "P1", "slot_index": index}
        for code, day, index in [
            ("A", "Sun", 0),
            ("B", "Sun", 0),
            ("C", "Sun", 0),
            ("E", "Sun", 0),
            ("D", "Mon", 2),
        ]
    ]
    sections = {
        code: [{"section": "F", "gender": "F", "student_count": 50, "preferred_room": ""}]
        for code in "ABCDE"
    }
    rooms = [{"room_code": f"F-{index}", "capacity": 50, "section": "F"} for index in range(5)]
    return entries, sections, rooms


def test_the_invigilator_pass_moves_a_link_whole():
    entries, sections, rooms = _hot_sunday()
    links = links_of(("A", "B"), codes="CDE")
    moved = _rebalance_invigilators_pass(entries, sections, rooms, SLOTS, {}, {}, {}, links=links)
    assert moved == 1
    assert placed(entries)["A"] == placed(entries)["B"] == ("Mon", "P1")
    assert placed(entries)["C"] == placed(entries)["E"] == ("Sun", "P1")


def test_a_link_is_pinned_when_any_member_is():
    entries, sections, rooms = _hot_sunday()
    links = links_of(("A", "B"), codes="CDE")
    moved = _rebalance_invigilators_pass(
        entries, sections, rooms, SLOTS, {}, {}, {}, pinned_courses={"B"}, links=links
    )
    assert moved > 0, "Something else moves instead"
    assert placed(entries)["A"] == placed(entries)["B"] == ("Sun", "P1")


def test_a_link_may_not_move_onto_a_slot_one_member_clashes_with():
    """D on Monday shares a student with B only. The link may not join it, and
    takes Monday's other period instead."""
    entries, sections, rooms = _hot_sunday()
    adj = {"B": {"D": 1}, "D": {"B": 1}}
    links = links_of(("A", "B"), codes="CDE")
    _rebalance_invigilators_pass(entries, sections, rooms, SLOTS, adj, {}, {}, links=links)
    assert placed(entries)["A"] == placed(entries)["B"] == ("Mon", "P2")


def test_a_link_may_not_move_onto_a_day_a_member_shares_with_a_bucket_mate():
    entries, sections, rooms = _hot_sunday()
    buckets = {("AI", 1): {"B", "D"}}
    course_buckets = {"B": [("AI", 1)], "D": [("AI", 1)]}
    links = links_of(("A", "B"), codes="CDE")
    moved = _rebalance_invigilators_pass(
        entries, sections, rooms, SLOTS, {}, buckets, course_buckets, links=links
    )
    assert moved > 0, "Something else moves instead"
    assert placed(entries)["A"] == placed(entries)["B"] == ("Sun", "P1"), "Monday holds D"


def _pass_board(placement, sizes):
    """Exams at the given slots, one female section each, and rooms to spare."""
    entries = [
        {
            "course_code": code,
            "day": SLOTS[slot]["day"],
            "period": SLOTS[slot]["period"],
            "slot_index": slot,
        }
        for code, slot in placement
    ]
    sections = {
        code: [{"section": "F", "gender": "F", "student_count": sizes[code], "preferred_room": ""}]
        for code, _slot in placement
    }
    rooms = [{"room_code": f"F-{index}", "capacity": 60, "section": "F"} for index in range(8)]
    return entries, sections, rooms


def test_a_link_is_as_large_as_all_its_members():
    """The pass tries the largest exam on the hot day first.

    Sunday: the link A+B (30 + 30 students, four invigilators) and C and E (50
    each, two apiece); Monday: D. Moving the link or C flattens the days alike,
    so what moves is what is tried first: the link, sixty students - though
    each of its members is smaller than C.
    """
    entries, sections, rooms = _pass_board(
        [("A", 0), ("B", 0), ("C", 0), ("E", 0), ("D", 2)],
        {"A": 30, "B": 30, "C": 50, "E": 50, "D": 50},
    )
    links = links_of(("A", "B"), codes="CDE")
    assert _rebalance_invigilators_pass(entries, sections, rooms, SLOTS, {}, {}, {}, links=links)
    assert placed(entries)["A"][0] == placed(entries)["B"][0] == "Mon"
    assert placed(entries)["C"] == placed(entries)["E"] == ("Sun", "P1")


def _cold_link():
    """Four 50-student exams on Sunday; the link A+B alone on Monday P1."""
    return _pass_board(
        [("C", 0), ("E", 0), ("F", 0), ("G", 0), ("A", 2), ("B", 2)],
        {"A": 30, "B": 30, "C": 50, "E": 50, "F": 50, "G": 50},
    )


def test_an_exam_may_not_move_onto_a_link_one_member_clashes_with():
    """C shares a student with B, so Monday P1 - the link's - is closed to it."""
    entries, sections, rooms = _cold_link()
    adj = {"B": {"C": 1}, "C": {"B": 1}}
    links = links_of(("A", "B"), codes="CEFG")
    _rebalance_invigilators_pass(entries, sections, rooms, SLOTS, adj, {}, {}, links=links)
    assert placed(entries)["C"] == ("Mon", "P2")
    assert placed(entries)["A"] == placed(entries)["B"] == ("Mon", "P1")


def test_an_exam_may_not_move_onto_the_day_of_a_linked_bucket_mate():
    """C shares B's study-plan term, so Monday is closed to it; E goes instead."""
    entries, sections, rooms = _cold_link()
    buckets = {("AI", 1): {"B", "C"}}
    course_buckets = {"B": [("AI", 1)], "C": [("AI", 1)]}
    links = links_of(("A", "B"), codes="CEFG")
    moved = _rebalance_invigilators_pass(
        entries, sections, rooms, SLOTS, {}, buckets, course_buckets, links=links
    )
    assert moved > 0
    assert placed(entries)["C"] == ("Sun", "P1")
    assert placed(entries)["E"][0] == "Mon"


def test_the_invigilator_pass_refuses_a_board_that_arrives_split():
    """It moves a link by the members it finds on the hot day, so a split board
    would stay split; no caller may hand it one."""
    entries, sections, rooms = _hot_sunday()
    entries[1].update(day="Mon", slot_index=2)
    with pytest.raises(LinkedExamsError, match="same day and period"):
        _rebalance_invigilators_pass(
            entries, sections, rooms, SLOTS, {}, {}, {}, links=links_of(("A", "B"), codes="CDE")
        )


# ── what QA reports ──────────────────────────────────────────────────────────


def _entry(code, slot):
    return {
        "course_code": code,
        "course_identity": code,
        "slot_index": slot,
        "day": SLOTS[slot]["day"],
        "period": SLOTS[slot]["period"],
    }


def test_a_student_in_two_linked_courses_is_a_linked_exam_clash():
    """Two registrations, two papers, one time: a real clash, but not one any
    repair may fix by separating the courses - so it has its own kind."""
    enrolled = {"A": {1, 2}, "B": {1, 3}, "C": {2}, "D": {2}}
    entries = [_entry("A", 0), _entry("B", 0), _entry("C", 1), _entry("D", 1)]
    links = links_of(("A", "B"), codes="CD")
    qa = _build_qa(enrolled, entries, links=links)
    attach_exam_relaxation_qa(qa, enrolled, entries, 0, [], links=links)
    kinds = [(row["kind"], row["courses"]) for row in qa["manual_override_details"]]
    assert kinds == [("linked_same_slot", ["A", "B"]), ("same_slot", ["C", "D"])]
    assert qa["hard_conflict_count"] == 2
    assert qa["conflict_count"] == 2, "Still counted as the clash it is"


def test_a_clash_reaching_beyond_the_link_is_an_ordinary_clash():
    enrolled = {"A": {1}, "B": {1}, "X": {1}}
    entries = [_entry("A", 0), _entry("B", 0), _entry("X", 0)]
    links = links_of(("A", "B"), codes="X")
    qa = _build_qa(enrolled, entries, links=links)
    attach_exam_relaxation_qa(qa, enrolled, entries, 0, [], links=links)
    assert [row["kind"] for row in qa["manual_override_details"]] == ["same_slot"]


def test_without_links_the_same_clash_is_an_ordinary_one():
    enrolled = {"A": {1}, "B": {1}}
    entries = [_entry("A", 0), _entry("B", 0)]
    qa = _build_qa(enrolled, entries)
    attach_exam_relaxation_qa(qa, enrolled, entries, 0, [])
    assert [row["kind"] for row in qa["manual_override_details"]] == ["same_slot"]


def test_linked_bucket_mates_count_once_on_their_day():
    buckets = {("AI", 1): {"A", "B", "C"}}
    links = links_of(("A", "B"), codes="C")
    together = [_entry("A", 0), _entry("B", 0), _entry("C", 2)]
    assert (
        _build_qa({}, together, plan_term_buckets=buckets, links=links)["bucket_day_violations"]
        == []
    )
    assert _build_qa({}, together, plan_term_buckets=buckets)["bucket_day_violations_count"] == 1
    crowded = [_entry("A", 0), _entry("B", 0), _entry("C", 1)]
    rows = _build_qa({}, crowded, plan_term_buckets=buckets, links=links)["bucket_day_violations"]
    assert [row["courses"] for row in rows] == [["A", "B", "C"]], "Every course on the day is named"


def test_daily_load_and_credit_stay_per_real_course():
    enrolled = {"A": {1}, "B": {1}}
    entries = [_entry("A", 0), _entry("B", 0)]
    qa = _build_qa(enrolled, entries, credit_map={"A": 4, "B": 4}, links=links_of(("A", "B")))
    assert qa["max_exams_per_day_per_student"] == 2
    assert qa["heavy_day_students"] == 1


# ── randomised: no stage ever splits a link ──────────────────────────────────


def _random_board(seed: int) -> dict:
    """Several links, pins that agree within each link, and slots to spare or not."""
    rng = random.Random(seed)
    codes = [f"E{index:02d}" for index in range(rng.randint(6, 12))]
    shuffled = rng.sample(codes, len(codes))
    groups, cursor = [], 0
    for _ in range(rng.randint(1, 3)):
        size = rng.randint(2, 3)
        if cursor + size > len(shuffled):
            break
        groups.append(tuple(sorted(shuffled[cursor : cursor + size])))
        cursor += size
    days = ["Sun", "Mon", "Tue"][: rng.randint(1, 3)]
    periods = ["P1", "P2"][: rng.randint(1, 2)]
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate(itertools.product(days, periods))
    ]
    students = list(range(1, 40))
    # Some students sit two courses of one link: a clash no solver may "fix".
    enrolled = {code: set(rng.sample(students, rng.randint(1, 8))) for code in codes}
    _, adj = build_conflict_graph(enrolled)
    adj, _thin = apply_thin_conflict_policy(enrolled, adj, rng.choice([0, 1]))
    buckets: dict = {}
    for number in range(rng.randint(0, 2)):
        buckets[("P", number)] = set(rng.sample(codes, rng.randint(2, 3)))
    course_buckets: dict = {}
    for key, members in sorted(buckets.items()):
        for code in sorted(members):
            course_buckets.setdefault(code, []).append(key)
    pinned = []
    for group in groups:
        if rng.random() < 0.3:
            slot = rng.choice(slots)
            for code in rng.sample(group, rng.randint(1, len(group))):
                pinned.append({"course_code": code, "day": slot["day"], "period": slot["period"]})
    loose = [code for code in codes if not any(code in group for group in groups)]
    if loose and rng.random() < 0.5:
        slot = rng.choice(slots)
        pinned.append({"course_code": loose[0], "day": slot["day"], "period": slot["period"]})
    return {
        "codes": codes,
        "groups": groups,
        "links": links_of(*groups, codes=codes),
        "slots": slots,
        "enrolled": enrolled,
        "adj": adj,
        "buckets": buckets,
        "course_buckets": course_buckets,
        "pinned": pinned,
        "credits": {code: rng.choice([2, 3, 4]) for code in codes},
        "seed": rng.choice([None, rng.randint(0, 999)]),
    }


def _check_pins(board, entries):
    where = placed(entries)
    for pin in board["pinned"]:
        assert where[pin["course_code"]] == (pin["day"], pin["period"])


@pytest.mark.parametrize("seed", range(60))
def test_no_schedule_splits_a_link(seed):
    board = _random_board(seed)
    entries = schedule_linked(
        board["codes"],
        board["adj"],
        board["slots"],
        links=board["links"],
        enrolled_sets=board["enrolled"],
        plan_term_buckets=board["buckets"],
        course_buckets=board["course_buckets"],
        pinned=board["pinned"],
        credit_map=board["credits"],
        seed=board["seed"],
    )
    assert sorted(entry["course_code"] for entry in entries) == sorted(board["codes"])
    assert board["links"].together(entries), board["groups"]
    _check_pins(board, entries)
    # And the exams a link does not own are exactly as clash-free as before:
    # no two units that share a student sit together unless OVERFLOW forced it.
    unit_adj = board["links"].adjacency(board["adj"])
    slot_of = {board["links"].unit(e["course_code"]): e for e in entries}
    pinned_units = {board["links"].unit(pin["course_code"]) for pin in board["pinned"]}
    for unit, neighbours in unit_adj.items():
        for mate in neighbours:
            if {unit, mate} & pinned_units or slot_of[unit]["day"] == "OVERFLOW":
                continue
            assert slot_of[unit]["slot_index"] != slot_of[mate]["slot_index"]
    # Nor does a study-plan term sit twice in a day - a link counting once -
    # unless every exam that day was pinned there.
    unit_buckets, _ = board["links"].buckets(board["buckets"], None)
    for mates in unit_buckets.values():
        by_day: dict = {}
        for unit in mates:
            if slot_of[unit]["day"] != "OVERFLOW":
                by_day.setdefault(slot_of[unit]["day"], set()).add(unit)
        for day, units in by_day.items():
            assert len(units) < 2 or units <= pinned_units, (day, units)


def _lopsided_pass(seed: int):
    """A board piled onto its first day, then the invigilator pass run over it.

    Sparse registrations and plenty of rooms leave the pass free to move almost
    anything, so it really does move links - which the balanced boards the
    scheduler makes seldom ask of it. Some units wait in OVERFLOW, a link under
    one shared Extra-n, as the scheduler and Fix leave them.
    """
    rng = random.Random(9000 + seed)
    # Its own stream, so the units on the board are drawn exactly as before.
    spill = random.Random(7700 + seed)
    board = _random_board(seed)
    enrolled = {code: set(rng.sample(range(1, 400), rng.randint(2, 6))) for code in board["codes"]}
    _, adj = build_conflict_graph(enrolled)
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate(
            itertools.product(["Sun", "Mon", "Tue", "Wed"][: rng.randint(2, 4)], ["P1", "P2"])
        )
    ]
    links = board["links"]
    entries = []
    pinned_units = set()
    extra = itertools.count(len(slots))
    for unit in links.units(board["codes"]):
        slot = rng.choice(slots[:2]) if rng.random() < 0.75 else rng.choice(slots)
        if rng.random() < 0.15:
            pinned_units.add(unit)
        if spill.random() < 0.15:
            index = next(extra)
            slot = {"index": index, "day": "OVERFLOW", "period": f"Extra-{index}"}
        for code in links.members_of(unit):
            entries.append({"course_code": code, "slot_index": slot["index"], **slot})
            del entries[-1]["index"]
    # Pin one member of a pinned link, or the whole of a single exam.
    pinned_courses = {links.members_of(unit)[-1] for unit in pinned_units}
    sections = {
        code: [
            {
                "section": f"{gender}1",
                "section_key": f"k:{code}:{gender}",
                "gender": gender,
                "student_count": rng.choice([12, 35, 60]),
                "preferred_room": "",
            }
            for gender in ("F", "M")
        ]
        for code in board["codes"]
    }
    rooms = [
        {"room_code": f"{gender}{size}-{copy}", "capacity": size, "section": gender}
        for gender in ("F", "M")
        for size in (40, 70)
        for copy in range(12)
    ]
    context = RoomAllocationContext.for_periods(64)
    assign_rooms_to_schedule(entries, sections, rooms, allocation_context=context)
    before = placed(entries)
    moves = _rebalance_invigilators_pass(
        entries,
        sections,
        rooms,
        slots,
        adj,
        board["buckets"],
        board["course_buckets"],
        max_trials=60,
        pinned_courses=pinned_courses,
        allocation_context=context,
        enrolled_sets=enrolled,
        credit_map=board["credits"],
        links=links,
    )
    return board, links, pinned_courses, before, placed(entries), moves


@pytest.mark.parametrize("seed", range(40))
def test_no_invigilator_pass_splits_a_link(seed):
    board, links, pinned_courses, before, after, _moves = _lopsided_pass(seed)
    assert links.together(
        [
            {"course_code": code, "day": day, "period": period}
            for code, (day, period) in after.items()
        ]
    ), board["groups"]
    frozen = links.units_of(pinned_courses)
    for code, where in after.items():
        if links.unit(code) in frozen:
            assert where == before[code], f"{code} belongs to a pinned link and moved"
        if before[code][0] == "OVERFLOW":
            # The pass balances days; it never seats a waiting exam, and a
            # waiting link keeps its one Extra-n.
            assert where == before[code], f"{code} left OVERFLOW"


def test_the_invigilator_pass_boards_really_move_links():
    """Without this the pass test could pass on boards where no link ever moved."""
    moved_links = moved_singles = waiting_links = 0
    for seed in range(40):
        _board, links, _pinned, before, after, _moves = _lopsided_pass(seed)
        changed = {code for code in after if after[code] != before[code]}
        moved_links += any(links.unit(code) != code for code in changed)
        moved_singles += any(links.unit(code) == code for code in changed)
        waiting_links += any(
            links.unit(code) != code and where[0] == "OVERFLOW" for code, where in before.items()
        )
    assert moved_links >= 8 and moved_singles >= 8, (moved_links, moved_singles)
    assert waiting_links >= 5, waiting_links


def test_the_randomised_boards_really_do_hold_the_hard_cases():
    """Without this the property tests could pass on boards too easy to fail."""
    overflow = shared = pinned_links = passes = 0
    for seed in range(60):
        board = _random_board(seed)
        entries = schedule_linked(
            board["codes"],
            board["adj"],
            board["slots"],
            links=board["links"],
            enrolled_sets=board["enrolled"],
            plan_term_buckets=board["buckets"],
            course_buckets=board["course_buckets"],
            pinned=board["pinned"],
        )
        overflow += any(
            entry["day"] == "OVERFLOW"
            and board["links"].unit(entry["course_code"]) != entry["course_code"]
            for entry in entries
        )
        shared += any(
            len(set.intersection(*(board["enrolled"][code] for code in group))) > 0
            for group in board["groups"]
        )
        pinned_links += any(
            board["links"].unit(pin["course_code"]) != pin["course_code"] for pin in board["pinned"]
        )
        passes += len(board["groups"]) > 1
    assert overflow >= 5 and shared >= 5 and pinned_links >= 5 and passes >= 5


def test_no_links_leaves_the_pass_exactly_as_it_was():
    """The default and an explicit NO_LINKS run the very same search."""
    runs = []
    for extra in ({}, {"links": NO_LINKS}):
        entries, sections, rooms = _hot_sunday()
        moves = functools.partial(_rebalance_invigilators_pass, **extra)(
            entries, sections, rooms, SLOTS, {}, {}, {}
        )
        runs.append((moves, entries))
    assert runs[0] == runs[1]
    assert exam_timetable.schedule_linked(["A"], {}, SLOTS) == exam_timetable.schedule(
        ["A"], {}, SLOTS
    )
