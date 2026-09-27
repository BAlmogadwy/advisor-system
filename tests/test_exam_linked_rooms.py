"""Linked courses may share a room - only where that saves rooms or seats.

Decision 3: same-gender sections of linked courses may share a room, and the
allocator shares only when it saves rooms or seats; there is no per-link
option. A shared room never takes a student over its capacity, never mixes
the male and female cohorts, never costs an invigilator the separate rooms
did not need, and never holds a course that is not in the link - nor an
online exam beside one sat in person. A link never leaves a period worse
than the same period allocated without it. Rows stay per real course, each
with its own section parts, and a shared room's rows name the other courses
in it and the room's whole head-count.
"""

import random
from collections import defaultdict

import pytest

from core.services import exam_room_allocation
from core.services.exam_room_allocation import RoomAllocationContext, allocate_period
from core.services.exam_timetable import (
    _build_room_qa,
    _room_invigilators_needed,
    assign_rooms_to_schedule,
)
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


def _unlinked(demands):
    return [{k: v for k, v in d.items() if k != "room_owner"} for d in demands]


_POOL = ("AI212", "BIO100", "CS150", "DS222", "IS210", "MATH101", "PHYS103", "STAT201")
_SIZES = (5, 8, 10, 12, 15, 20, 25, 29, 30, 31, 35, 40, 45, 55)
_CAPACITIES = (8, 10, 12, 15, 20, 25, 28, 30, 32, 35, 40, 50, 60, 80)


@pytest.mark.parametrize("seed", range(30))
def test_a_link_never_leaves_a_period_worse_and_never_overfills_a_room(seed):
    """Random tight periods: two links and unlinked courses, one cohort.

    Link ids are their members' codes joined, as in a build, so an unlinked
    course often sorts between a link's members; sizes repeat, so ties are
    common; some sections ask for a room. Each board is compared with the
    same period allocated with no link at all.
    """
    rng = random.Random(9100 + seed)
    codes = rng.sample(_POOL, 6)
    owners = dict.fromkeys(codes)
    for group in (codes[0:2], codes[2 : 2 + rng.choice([2, 3])]):
        owners.update(dict.fromkeys(group, "+".join(sorted(group))))
    rooms = exam_room_allocation.normalized_rooms(
        [_room(f"R{n:02d}", rng.choice(_CAPACITIES)) for n in range(rng.randint(2, 7))]
    )
    demands = []
    for code in codes:
        for number in range(rng.randint(1, 3)):
            demand = _demand(code, f"M{number + 1}", rng.choice(_SIZES), owner=owners[code])
            if rng.random() < 0.15:
                demand["preferred_room"] = rng.choice(rooms)["room_code"]
            demands.append(demand)
    capacity = {room["room_code"]: room["capacity"] for room in rooms}
    linked = _allocate(demands, rooms, room_staff=_room_invigilators_needed)
    alone = _allocate(_unlinked(demands), rooms, room_staff=_room_invigilators_needed)
    for room, courses in _occupants(linked).items():
        assert sum(courses.values()) <= capacity[room]
        assert len({owners[code] or code for code in courses}) == 1, (room, courses)
    assert _cost(linked) <= _cost(alone)
    if _cost(linked)[0] == _cost(alone)[0]:
        assert _staff(linked) <= _staff(alone)
    if any(len(courses) > 1 for courses in _occupants(linked).values()):
        # A share seats more students, or saves a room or room seats (decision 3).
        assert _cost(linked)[0] < _cost(alone)[0] or _cost(linked)[3:] < _cost(alone)[3:]
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


def test_a_link_that_shares_no_room_leaves_the_period_as_it_was():
    """Sorted by link, STAT201's sections would come ahead of COE111's and
    GS150's - an order a period without links never uses. Nothing is worth
    sharing here, so the period must be exactly the one it is without links:
    four rooms and 220 seats, never five rooms and 240 seats."""
    rooms = exam_room_allocation.normalized_rooms(
        [
            _room(code, size)
            for code, size in (
                ("R00", 32),
                ("R01", 8),
                ("R02", 15),
                ("R03", 20),
                ("R04", 80),
                ("R05", 80),
                ("R06", 28),
            )
        ]
    )
    first, second = "AI300+STAT201", "COE111+GS150"
    demands = [
        _demand("STAT201", "S0", 15, owner=first),
        _demand("STAT201", "S1", 25, owner=first),
        _demand("STAT201", "S2", 5, owner=first),
        _demand("AI300", "S0", 8, owner=first),
        {**_demand("AI300", "S1", 15, owner=first), "preferred_room": "R04"},
        _demand("COE111", "S0", 25, owner=second),
        _demand("GS150", "S0", 29, owner=second),
    ]
    linked = _allocate(demands, rooms, room_staff=_room_invigilators_needed)
    alone = _allocate(_unlinked(demands), rooms, room_staff=_room_invigilators_needed)
    assert _cost(alone) == (0, 0, 0, 4, 220)
    assert _unlinked(linked) == alone


def test_an_unlinked_course_keeps_the_seats_it_has_without_the_link():
    """A three-course link in a period too small for everyone. Sharing seats
    no more students and saves no room or seat, so the period stays as it is
    without the link: PHYS103 keeps its 32 seats and nobody needs an
    invigilator the unlinked period did not."""
    rooms = exam_room_allocation.normalized_rooms(
        [_room("R00", 80), _room("R01", 80), _room("R02", 32)]
    )
    link = "CS101+CS102+STAT201"
    demands = [
        {**_demand("CS102", "S0", 10, owner=link), "preferred_room": "R01"},
        _demand("CS102", "S1", 40, owner=link),
        _demand("CS102", "S2", 8, owner=link),
        {**_demand("STAT201", "S0", 12, owner=link), "preferred_room": "R00"},
        _demand("STAT201", "S1", 29, owner=link),
        _demand("STAT201", "S2", 55, owner=link),
        _demand("CS101", "S0", 30, owner=link),
        _demand("CS101", "S1", 20, owner=link),
        _demand("CS101", "S2", 30, owner=link),
        _demand("PHYS103", "S0", 35),
    ]
    linked = _allocate(demands, rooms, room_staff=_room_invigilators_needed)
    alone = _allocate(_unlinked(demands), rooms, room_staff=_room_invigilators_needed)
    assert _unlinked(linked) == alone
    assert _occupants(linked)["R02"] == {"PHYS103": 32}
    assert _staff(linked) == _staff(alone) == 4


def _whole_section_board():
    demands = [
        _demand("AI212", "M1", 13, owner="L"),
        _demand("AI212", "M2", 9, owner="L"),
        _demand("AI225", "M1", 28, owner="L"),
        _demand("AI225", "M2", 9, owner="L"),
    ]
    return demands, [_room("R0", 20), _room("R1", 20), _room("R2", 30)]


def test_a_share_that_only_keeps_a_section_whole_is_refused():
    """Decision 3: a share must save rooms or seats. Sharing R1 would keep
    AI225's 28 whole, in the same three rooms and 70 seats - no room and no
    seat saved - so each course keeps rooms of its own, as without the link."""
    demands, rooms = _whole_section_board()
    # What is at stake: the sharing answer keeps every section whole.
    sharing = exam_room_allocation._allocate(demands, rooms, RoomAllocationContext())
    assert exam_room_allocation._score(sharing, demands, rooms)[:5] == (0, 0, 0, 3, 70)
    linked = _allocate(demands, rooms, room_staff=_room_invigilators_needed)
    alone = _allocate(_unlinked(demands), rooms, room_staff=_room_invigilators_needed)
    assert _cost(alone) == (0, 1, 1, 3, 70)
    assert _unlinked(linked) == alone
    assert all(len(courses) == 1 for courses in _occupants(linked).values())


def test_a_share_must_save_a_room_or_a_room_seat_and_split_no_more_sections():
    wins = exam_room_allocation._sharing_wins
    demands, rooms = _whole_section_board()
    # R0 20, R1 20, R2 30. Without the link: AI212 in R2, AI225's 28 over R0 and R1.
    separate = [{2: 13}, {2: 9}, {0: 17, 1: 11}, {1: 9}]
    # Sharing R1 keeps every section whole in the same rooms and seats: nothing saved.
    assert not wins([{0: 13}, {1: 9}, {2: 28}, {1: 9}], separate, demands, rooms, None)
    pair = [_demand("AI212", "M1", 10, owner="L"), _demand("AI225", "M2", 6, owner="L")]
    small = [_room("R-16", 16), _room("R-12", 12), _room("R-4", 4)]
    apart = [{0: 10}, {1: 6}]  # two rooms, 28 seats
    # Sharing R-12 with the 4-seat room saves 12 seats but splits AI225.
    assert not wins([{1: 10}, {1: 2, 2: 4}], apart, pair, small, None)
    # One room for both saves a room and splits nothing.
    assert wins([{0: 10}, {0: 6}], apart, pair, small, None)


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


@pytest.mark.parametrize(
    ("sections", "rooms"),
    [
        # No room is worth sharing: MATH101 stays whole in a 60 and BIO100 in
        # the 20, never split over a 20 and a 40 with BIO100 in a spare 60.
        (
            {"AI212": [40], "BIO100": [15], "CS150": [35], "MATH101": [35, 15]},
            [20, 40, 40, 60, 60],
        ),
        # A share would seat nobody more, in the same rooms and seats, for
        # two more invigilators.
        (
            {"AI212": [35, 30], "BIO100": [40], "CS150": [15, 35], "MATH101": [25, 10]},
            [30, 40, 60],
        ),
    ],
)
def test_a_link_never_costs_a_board_rooms_seats_or_staff(sections, rooms):
    """The link's id sorts its members apart from BIO100 and CS150 between them."""
    enrolment = {
        code: [_section(code, f"M{n + 1}", size, "M") for n, size in enumerate(sizes)]
        for code, sizes in sections.items()
    }
    inventory = [_room(f"M-{size}-{n}", size) for n, size in enumerate(rooms)]
    boards, qas = [], []
    for links in (NO_LINKS, _links(("AI212", "MATH101"))):
        with exam_room_allocation._CACHE_LOCK:
            exam_room_allocation._CACHE.clear()
        entries = [_entry(code) for code in sections]
        assign_rooms_to_schedule(entries, enrolment, inventory, links=links)
        boards.append(entries)
        qa = _build_room_qa(entries, inventory, links=links)
        qas.append(
            (
                sum(row["student_count"] for row in qa["unassigned_room_sections"]),
                qa["rooms_used"],
                qa["total_capacity_used"],
                qa["invigilators_total"],
            )
        )
    assert boards[0] == boards[1]
    assert qas[0] == qas[1]


def _online(entry):
    return {**entry, "is_online": True}


def test_an_online_member_of_a_link_never_shares_a_room_with_an_in_person_one():
    """GS150 is sat online, AI212 in person: one room would do, but a room is
    online or not, so each keeps a room of its own - as with no link at all."""
    sections = {
        "AI212": [_section("AI212", "M1", 4, "M")],
        "GS150": [_section("GS150", "M2", 10, "M")],
    }
    rooms = [_room("M-20-0", 20), _room("M-20-1", 20)]
    boards = []
    for links in (NO_LINKS, _links(("AI212", "GS150"))):
        entries = [_entry("AI212"), _online(_entry("GS150"))]
        assign_rooms_to_schedule(entries, sections, rooms, links=links)
        boards.append(entries)
    assert boards[0] == boards[1]
    assert len({row["room_code"] for entry in boards[1] for row in entry["rooms"]}) == 2


def test_the_online_members_of_a_link_share_a_room_with_each_other():
    """GS150 and GS160 are both sat online: one online room holds both, and
    AI212, in person, keeps its own."""
    sections = {
        "AI212": [_section("AI212", "M1", 4, "M")],
        "GS150": [_section("GS150", "M2", 10, "M")],
        "GS160": [_section("GS160", "M3", 5, "M")],
    }
    rooms = [_room(f"M-20-{n}", 20) for n in range(3)]
    entries = [_entry("AI212"), _online(_entry("GS150")), _online(_entry("GS160"))]
    assign_rooms_to_schedule(entries, sections, rooms, links=_links(("AI212", "GS150", "GS160")))
    by_code = {entry["course_code"]: entry["rooms"] for entry in entries}
    [gs150], [gs160], [ai212] = by_code["GS150"], by_code["GS160"], by_code["AI212"]
    assert gs150["room_code"] == gs160["room_code"] != ai212["room_code"]
    assert (gs150["room_shared_with"], gs150["room_student_total"]) == (["GS160"], 15)
    assert "room_shared_with" not in ai212
