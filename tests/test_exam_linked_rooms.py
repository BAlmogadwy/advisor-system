"""Linked courses may share a room - only where that saves rooms or seats.

Decision 3: same-gender sections of linked courses may share a room, and the
allocator shares only when it saves rooms or seats; there is no per-link
option. A shared room never takes a student over its capacity, never mixes
the male and female cohorts, never costs an invigilator the separate rooms
did not need, and never holds a course that is not in the link. Rows stay
per real course, each with its own section parts, and a shared room's rows
name the other courses in it and the room's whole head-count.
"""

import random
from collections import defaultdict

import pytest

from core.services import exam_room_allocation
from core.services.exam_room_allocation import RoomAllocationContext, allocate_period
from core.services.exam_timetable import _room_invigilators_needed, assign_rooms_to_schedule
from core.services.linked_exams import NO_LINKS, resolve_linked_exams


@pytest.fixture(autouse=True)
def _fresh_room_cache():
    """A period another test allocated must not answer for these boards."""
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    yield
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()


def _demand(code, label, count, *, gender="M", owner=None):
    demand = {
        "course_code": code,
        "course_identity": code,
        "section": label,
        "section_key": f"term-section:{code}:{label}",
        "gender": gender,
        "mapping_status": "mapped",
        "student_count": count,
        "preferred_room": "",
    }
    if owner:
        demand["room_owner"] = owner
    return demand


def _room(code, capacity, gender="M"):
    return {"room_code": code, "capacity": capacity, "section": gender}


def _allocate(demands, rooms, **kwargs):
    return allocate_period(demands, rooms, RoomAllocationContext(), **kwargs)


def _occupants(rows):
    """{room: {course: students}} of the seated rows."""
    rooms: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        if row["room_code"] != "UNASSIGNED":
            seated = rooms[row["room_code"]]
            seated[row["course_code"]] = seated.get(row["course_code"], 0) + row["student_count"]
    return dict(rooms)


def _links(*groups):
    codes = sorted({code for group in groups for code in group})
    return resolve_linked_exams(
        [{"members": [{"course_identity": code} for code in group]} for group in groups],
        {code: {"course_identity": code} for code in codes},
    )


# ── the allocator ────────────────────────────────────────────────────────────


def test_linked_courses_share_a_room_when_that_saves_one():
    demands = [_demand("AI212", "M1", 4, owner="L"), _demand("AI225", "M2", 10, owner="L")]
    rooms = [_room("R-20a", 20), _room("R-20b", 20)]
    shared = _occupants(_allocate(demands, rooms))
    assert shared == {"R-20a": {"AI212": 4, "AI225": 10}}
    # The same courses unlinked keep a room each.
    alone = [{k: v for k, v in d.items() if k != "room_owner"} for d in demands]
    assert len(_occupants(_allocate(alone, rooms))) == 2


def test_linked_courses_keep_their_own_rooms_when_sharing_saves_nothing():
    """Two rooms either way, the same seats: mixing the courses buys nothing."""
    demands = [
        _demand("AI212", "M1", 6, owner="L"),
        _demand("AI212", "M2", 6, owner="L"),
        _demand("AI225", "M3", 6, owner="L"),
    ]
    rooms = [_room("R-12a", 12), _room("R-12b", 12)]
    occupants = _occupants(_allocate(demands, rooms))
    assert len(occupants) == 2
    assert all(len(courses) == 1 for courses in occupants.values())


def test_a_share_that_needs_an_extra_invigilator_is_refused():
    """External courses need our staff only past 30 students a room: two rooms
    of 20 and 18 need nobody, one room of 38 needs one of us."""
    demands = [
        _demand("PHYS103 (1)", "M1", 20, owner="L"),
        _demand("PHYS103 (2)", "M2", 18, owner="L"),
    ]
    rooms = [_room("R-40", 40), _room("R-25a", 25), _room("R-25b", 25)]
    staffed = _occupants(_allocate(demands, rooms, room_staff=_room_invigilators_needed))
    assert all(len(courses) == 1 for courses in staffed.values())
    assert len(staffed) == 2
    # Nothing counts staff here: the room it saves decides.
    assert _occupants(_allocate(demands, rooms)) == {"R-40": {"PHYS103 (1)": 20, "PHYS103 (2)": 18}}


def test_department_courses_share_where_staff_do_not_grow():
    """Department rooms need one of us each, or two from 30 students: one room
    of 38 needs two, as the two rooms did."""
    demands = [_demand("AI212", "M1", 20, owner="L"), _demand("AI225", "M2", 18, owner="L")]
    rooms = [_room("R-40", 40), _room("R-25a", 25), _room("R-25b", 25)]
    occupants = _occupants(_allocate(demands, rooms, room_staff=_room_invigilators_needed))
    assert occupants == {"R-40": {"AI212": 20, "AI225": 18}}


def test_seating_every_student_outranks_the_staff_a_share_costs():
    """One room for two external halves: sharing it seats everyone."""
    demands = [
        _demand("PHYS103 (1)", "M1", 20, owner="L"),
        _demand("PHYS103 (2)", "M2", 18, owner="L"),
    ]
    rows = _allocate(demands, [_room("R-40", 40)], room_staff=_room_invigilators_needed)
    assert _occupants(rows) == {"R-40": {"PHYS103 (1)": 20, "PHYS103 (2)": 18}}
    assert not [row for row in rows if row["room_code"] == "UNASSIGNED"]


def test_the_whole_section_search_seats_a_link_together():
    """Smallest-room-first leaves AI225 nowhere; only the search finds that
    the link fits whole in the big room and CS101 in the small one."""
    demands = [
        _demand("AI212", "M1", 19, owner="L"),
        _demand("AI225", "M2", 9, owner="L"),
        _demand("CS101", "M3", 16),
    ]
    rows = _allocate(demands, [_room("R-20", 20), _room("R-40", 40)])
    assert _occupants(rows) == {"R-40": {"AI212": 19, "AI225": 9}, "R-20": {"CS101": 16}}
    assert not [row for row in rows if row["room_code"] == "UNASSIGNED"]


def test_the_split_fallback_may_use_its_links_room():
    """Seats left in the link's room serve the rest of the link first."""
    demands = [_demand("AI212", "M1", 30, owner="L"), _demand("AI225", "M2", 15, owner="L")]
    rooms = [_room("R-40", 40), _room("R-10", 10)]
    split = exam_room_allocation._split_remaining([{0: 30}, {}], demands, rooms)
    assert split == [{0: 30}, {0: 10, 1: 5}]
    unlinked = [{k: v for k, v in d.items() if k != "room_owner"} for d in demands]
    assert exam_room_allocation._split_remaining([{0: 30}, {}], unlinked, rooms) == [
        {0: 30},
        {1: 10},
    ]


def test_the_search_keeps_a_link_unmixed_when_mixing_buys_nothing():
    """Asked for the sharing answer alone, two equal rooms: each course its own."""
    demands = [
        _demand("AI212", "M1", 6, owner="L"),
        _demand("AI212", "M2", 6, owner="L"),
        _demand("AI225", "M3", 6, owner="L"),
    ]
    rooms = [_room("R-12a", 12), _room("R-12b", 12)]
    allocation = exam_room_allocation._allocate(demands, rooms, RoomAllocationContext())
    held = [{demands[i]["course_code"] for i, a in enumerate(allocation) if r in a} for r in (0, 1)]
    assert sorted(map(sorted, held)) == [["AI212"], ["AI225"]]


def test_mixing_courses_ranks_below_rooms_and_seats_and_above_a_preferred_room():
    demands = [
        {**_demand("AI212", "M1", 5, owner="L"), "preferred_room": "R-a"},
        _demand("AI212", "M2", 5, owner="L"),
        _demand("AI225", "M3", 5, owner="L"),
    ]
    rooms = [_room("R-a", 20), _room("R-b", 20)]
    score = exam_room_allocation._score
    mixed_preferred = [{0: 5}, {1: 5}, {0: 5}]  # AI212 M1 in its room, beside AI225
    unmixed = [{1: 5}, {1: 5}, {0: 5}]  # AI212 together, M1 away from its room
    assert score(unmixed, demands, rooms) < score(mixed_preferred, demands, rooms)
    one_room = [{0: 5}, {0: 5}, {0: 5}]
    assert score(one_room, demands, rooms) < score(unmixed, demands, rooms)


def test_a_tie_keeps_the_courses_apart():
    demands = [_demand("AI212", "M1", 6, owner="L"), _demand("AI225", "M2", 6, owner="L")]
    rooms = [_room("R-12a", 12), _room("R-12b", 12)]
    wins = exam_room_allocation._sharing_wins
    # Same seats, rooms and room seats: nothing bought, nothing shared.
    assert not wins([{0: 6}, {1: 6}], [{0: 6}, {1: 6}], demands, rooms, None)
    assert wins([{0: 6}, {0: 6}], [{0: 6}, {1: 6}], demands, rooms, None)
    # Seating more students wins, whatever the staff.
    assert wins([{0: 6}, {0: 6}], [{0: 6}, {}], demands, rooms, lambda _courses, _n: 5)


def test_unlinked_courses_never_share_even_when_students_go_unseated():
    demands = [_demand("CS101", "M1", 5), _demand("IS102", "M2", 5)]
    rows = _allocate(demands, [_room("R-10", 10)])
    assert _occupants(rows) in ({"R-10": {"CS101": 5}}, {"R-10": {"IS102": 5}})
    assert sum(row["student_count"] for row in rows if row["room_code"] == "UNASSIGNED") == 5


def test_a_link_shares_within_one_cohort_only():
    """Even handed both cohorts at once, a room never holds men and women."""
    demands = [
        _demand("AI212", "M1", 4, owner="L"),
        _demand("AI225", "F1", 3, gender="F", owner="L"),
    ]
    occupants = _occupants(_allocate(demands, [_room("R-20a", 20), _room("R-20b", 20)]))
    assert sorted(occupants.values(), key=str) == [{"AI212": 4}, {"AI225": 3}]


def _staff(rows):
    return sum(
        _room_invigilators_needed(list(courses), sum(courses.values()))
        for courses in _occupants(rows).values()
    )


def _cost(rows):
    """Unseated students, split sections, fragments, rooms and room seats."""
    pieces: dict[str, int] = defaultdict(int)
    for row in rows:
        pieces[row["section_key"]] += 1
    used = {
        row["room_code"]: row["room_capacity"] for row in rows if row["room_code"] != "UNASSIGNED"
    }
    return (
        sum(row["student_count"] for row in rows if row["room_code"] == "UNASSIGNED"),
        sum(n > 1 for n in pieces.values()),
        sum(n - 1 for n in pieces.values()),
        len(used),
        sum(used.values()),
    )


@pytest.mark.parametrize("seed", range(40))
def test_a_link_never_leaves_a_period_worse_and_never_overfills_a_room(seed):
    """Random tight periods: two links and an unlinked course, one cohort."""
    rng = random.Random(6100 + seed)
    owners = {"A1": "LA", "A2": "LA", "B1": "LB", "B2": "LB", "B3": "LB", "C1": None}
    demands = [
        _demand(code, f"M{number}", rng.randint(1, 30), owner=owner)
        for code, owner in owners.items()
        for number in range(rng.randint(1, 2))
    ]
    rooms = [
        _room(f"R{size}-{copy}", size)
        for size in rng.sample([10, 15, 20, 30, 40, 60], rng.randint(1, 4))
        for copy in range(rng.randint(1, 3))
    ]
    capacity = {room["room_code"]: room["capacity"] for room in rooms}
    linked = _allocate(demands, rooms, room_staff=_room_invigilators_needed)
    alone = _allocate(
        [{k: v for k, v in d.items() if k != "room_owner"} for d in demands],
        rooms,
        room_staff=_room_invigilators_needed,
    )
    for room, courses in _occupants(linked).items():
        assert sum(courses.values()) <= capacity[room]
        assert len({owners[code] or code for code in courses}) == 1, (room, courses)
    assert _cost(linked) <= _cost(alone)
    if _cost(linked)[0] == _cost(alone)[0]:
        assert _staff(linked) <= _staff(alone)
    # Rows stay per real section: every student of every section is accounted for.
    for demand in demands:
        assert (
            sum(
                row["student_count"]
                for row in linked
                if row["section_key"] == demand["section_key"]
            )
            == demand["student_count"]
        )


# ── rooms on a board ─────────────────────────────────────────────────────────


def _entry(code, slot=0):
    return {
        "course_code": code,
        "course_identity": code,
        "course_name": code,
        "slot_index": slot,
        "day": "Sun",
        "period": ["08:00-10:00", "13:00-15:00"][slot],
    }


def _section(code, label, count, gender):
    return {
        "section": label,
        "section_key": f"term-section:{code}:{label}",
        "term_section_id": None,
        "mapping_status": "mapped",
        "mapping_source": "scraper_timetable",
        "student_count": count,
        "gender": gender,
        "preferred_room": "",
    }


def test_shared_rows_stay_per_course_and_name_the_room_they_share():
    entries = [_entry("AI212"), _entry("AI225"), _entry("CS101")]
    sections = {
        "AI212": [_section("AI212", "M1", 4, "M"), _section("AI212", "F1", 6, "F")],
        "AI225": [_section("AI225", "M2", 10, "M"), _section("AI225", "F2", 5, "F")],
        "CS101": [_section("CS101", "M3", 3, "M")],
    }
    rooms = [_room(f"M-20-{n}", 20, "M") for n in range(3)] + [
        _room(f"F-20-{n}", 20, "F") for n in range(3)
    ]
    assign_rooms_to_schedule(entries, sections, rooms, links=_links(("AI212", "AI225")))
    by_code = {entry["course_code"]: entry["rooms"] for entry in entries}
    for code, partner, gender, own, total in (
        ("AI212", "AI225", "M", 4, 14),
        ("AI212", "AI225", "F", 6, 11),
        ("AI225", "AI212", "M", 10, 14),
        ("AI225", "AI212", "F", 5, 11),
    ):
        [row] = [row for row in by_code[code] if row["gender"] == gender]
        assert row["room_shared_with"] == [partner]
        assert row["room_student_total"] == total
        assert row["student_count"] == own
        assert [part["student_count"] for part in row["section_parts"]] == [own]
        assert row["room_code"].startswith(gender)
    # CS101 is in no link: a room of its own, and its row claims no partner.
    [row] = by_code["CS101"]
    assert "room_shared_with" not in row and "room_student_total" not in row
    assert row["room_code"] not in {r["room_code"] for r in by_code["AI212"]}


def test_without_links_no_row_carries_shared_room_facts():
    entries = [_entry("AI212"), _entry("AI225")]
    sections = {
        "AI212": [_section("AI212", "M1", 4, "M")],
        "AI225": [_section("AI225", "M2", 10, "M")],
    }
    assign_rooms_to_schedule(entries, sections, [_room("M-20-0", 20), _room("M-20-1", 20)])
    rows = [row for entry in entries for row in entry["rooms"]]
    assert len({row["room_code"] for row in rows}) == 2
    assert not any("room_shared_with" in row or "room_student_total" in row for row in rows)


def test_a_linked_course_alone_in_a_cohort_is_roomed_as_if_unlinked():
    """AI225 has no women: AI212's women's rooms are exactly the unlinked ones.

    AI212 (2), in no link, ties AI212 for the smaller room, which the order
    of the period's demands decides - so that order must not move either.
    """
    sections = {
        "AI212": [_section("AI212", "F1", 10, "F"), _section("AI212", "F2", 9, "F")],
        "AI212 (2)": [_section("AI212 (2)", "F3", 10, "F")],
        "AI225": [_section("AI225", "M1", 10, "M")],
    }
    rooms = [_room(f"F-{size}", size, "F") for size in (10, 12, 20)] + [_room("M-20", 20)]
    boards = []
    for links in (NO_LINKS, _links(("AI212", "AI225"))):
        entries = [_entry("AI212"), _entry("AI212 (2)"), _entry("AI225")]
        assign_rooms_to_schedule(entries, sections, rooms, links=links)
        boards.append(entries)
    assert boards[0] == boards[1]


def test_only_a_pack_holding_two_courses_of_a_link_is_allocated_as_the_link(monkeypatch):
    """The allocator sees a room owner only where the link can share: the men
    (both courses) - never the women, where AI212 is alone."""
    seen = []

    def spy(demands, rooms, context, **kwargs):
        seen.append({(d["gender"], d["course_code"], d.get("room_owner")) for d in demands})
        return allocate_period(demands, rooms, context, **kwargs)

    monkeypatch.setattr("core.services.exam_timetable.allocate_period", spy)
    sections = {
        "AI212": [_section("AI212", "F1", 10, "F"), _section("AI212", "M1", 4, "M")],
        "AI225": [_section("AI225", "M2", 10, "M")],
    }
    rooms = [_room("F-20", 20, "F"), _room("M-20", 20), _room("M-12", 12)]
    assign_rooms_to_schedule(
        [_entry("AI212"), _entry("AI225")], sections, rooms, links=_links(("AI212", "AI225"))
    )
    assert seen == [
        {("F", "AI212", None)},
        {("M", "AI212", "AI212+AI225"), ("M", "AI225", "AI212+AI225")},
    ]


def test_a_period_no_link_can_share_is_allocated_once(monkeypatch):
    """Allocating both ways is for linked periods; any other costs one search."""
    calls = []
    real = exam_room_allocation._allocate

    def counted(demands, rooms, context):
        calls.append(len(demands))
        return real(demands, rooms, context)

    monkeypatch.setattr(exam_room_allocation, "_allocate", counted)
    rooms = [_room("R-20a", 20), _room("R-20b", 20)]
    _allocate([_demand("CS101", "M1", 4), _demand("IS102", "M2", 10)], rooms)
    _allocate([_demand("AI212", "M1", 4, owner="L"), _demand("CS101", "M2", 10)], rooms)
    assert calls == [2, 2]
    _allocate([_demand("AI212", "M1", 4, owner="L"), _demand("AI225", "M2", 10, owner="L")], rooms)
    assert calls == [2, 2, 2, 2]


def test_linked_courses_in_different_periods_never_share():
    """A link sits together; if a board ever held its members apart, each
    period's rooms would still be its own."""
    entries = [_entry("AI212", 0), _entry("AI225", 1)]
    sections = {
        "AI212": [_section("AI212", "M1", 4, "M")],
        "AI225": [_section("AI225", "M2", 10, "M")],
    }
    assign_rooms_to_schedule(
        entries, sections, [_room("M-20-0", 20)], links=_links(("AI212", "AI225"))
    )
    assert not any("room_shared_with" in row for entry in entries for row in entry["rooms"])
