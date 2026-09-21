"""Section-first room allocation conserves real sections before resorting to splits."""

import json
import random
from collections import Counter
from copy import deepcopy

import pytest

from core.services import exam_room_allocation
from core.services.exam_timetable import assign_rooms_to_schedule


@pytest.fixture(autouse=True)
def isolated_allocation_cache():
    """A previous test's cached solution must not hide solver or ordering regressions."""
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    yield
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()


def _section(label, count, *, gender="F", preferred="", key=None):
    return {
        "section": label,
        "section_key": key or f"official:{label}",
        "mapping_status": "mapped" if gender in {"M", "F"} else "missing",
        "mapping_source": "scraper_timetable" if gender in {"M", "F"} else "",
        "student_count": count,
        "gender": gender,
        "preferred_room": preferred,
    }


def _room(code, capacity, gender="F"):
    return {"room_code": code, "capacity": capacity, "section": gender}


def _entry(code, *, slot=0, identity=None):
    return {
        "course_code": code,
        "course_identity": identity or f"{code}|official course",
        "course_name": identity or code,
        "slot_index": slot,
        "day": "Sun",
        "period": ["08:00-10:00", "10:30-12:30", "13:00-15:00"][slot],
    }


def _allocate(enrollment, rooms, *, entries=None, seed=None):
    entries = entries if entries is not None else [_entry(code) for code in enrollment]
    original_enrollment, original_rooms = deepcopy(enrollment), deepcopy(rooms)
    original_positions = {
        entry["course_identity"]: (entry["slot_index"], entry["day"], entry["period"])
        for entry in entries
    }
    assert assign_rooms_to_schedule(entries, enrollment, rooms, seed=seed) is entries
    assert enrollment == original_enrollment
    assert rooms == original_rooms
    assert {
        entry["course_identity"]: (entry["slot_index"], entry["day"], entry["period"])
        for entry in entries
    } == original_positions
    _assert_conserved(entries, enrollment, rooms)
    return {entry["course_code"]: entry["rooms"] for entry in entries}


def _assert_conserved(entries, enrollment, rooms):
    capacities = {room["room_code"]: room for room in rooms}
    expected, actual = Counter(), Counter()
    occupied = set()
    for entry in entries:
        code = entry["course_code"]
        if entry["day"] == "OVERFLOW":
            assert entry["rooms"] == []
            continue
        for section in enrollment[code]:
            expected[(code, section["section_key"], section["section"], section["gender"])] += (
                section["student_count"]
            )
        for allocation in entry["rooms"]:
            assert allocation["student_count"] > 0
            parts = allocation["section_parts"]
            assert sum(part["student_count"] for part in parts) == allocation["student_count"]
            assert all(part["gender"] == allocation["gender"] for part in parts)
            for part in parts:
                actual[(code, part["section_key"], part["section"], part["gender"])] += part[
                    "student_count"
                ]
            if allocation["room_code"] == "UNASSIGNED":
                assert allocation["room_capacity"] == 0
                continue
            room = capacities[allocation["room_code"]]
            assert room["section"] == allocation["gender"]
            assert allocation["room_capacity"] == room["capacity"]
            assert allocation["student_count"] <= room["capacity"]
            key = (entry["slot_index"], allocation["room_code"])
            assert key not in occupied, "Two courses cannot occupy the same room in one period"
            occupied.add(key)
    assert actual == expected


def _assert_whole_and_assigned(allocation, enrollment):
    for code, rooms in allocation.items():
        assert all(room["room_code"] != "UNASSIGNED" for room in rooms)
        occurrences = Counter(
            (part["section_key"], part["gender"])
            for room in rooms
            for part in room["section_parts"]
        )
        assert occurrences == Counter(
            (section["section_key"], section["gender"]) for section in enrollment[code]
        )
        assert all(
            part["room_group_count"] == 1 for room in rooms for part in room["section_parts"]
        )


@pytest.mark.parametrize("seed", [None, 0, 2, 19])
def test_largest_section_keeps_54_room_before_ds113_consolidation(seed):
    enrollment = {
        "DS113": [_section("F26", 26), _section("F23", 23)],
        "CS113": [_section("F48", 48)],
    }
    allocation = _allocate(
        enrollment, [_room("R54", 54), _room("R26", 26), _room("R23", 23)], seed=seed
    )
    _assert_whole_and_assigned(allocation, enrollment)
    assert {(r["room_code"], r["student_count"]) for r in allocation["CS113"]} == {("R54", 48)}
    assert {(r["room_code"], r["student_count"]) for r in allocation["DS113"]} == {
        ("R26", 26),
        ("R23", 23),
    }


def test_same_course_sections_consolidate_when_large_room_is_unneeded():
    enrollment = {"DS113": [_section("F26", 26), _section("F23", 23)]}
    allocation = _allocate(enrollment, [_room("R54", 54), _room("R26", 26), _room("R23", 23)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert len(allocation["DS113"]) == 1
    assert allocation["DS113"][0]["room_code"] == "R54"
    assert allocation["DS113"][0]["student_count"] == 49


def test_preferred_room_cannot_take_capacity_needed_by_a_larger_section():
    enrollment = {
        "SMALL": [_section("F1", 26, preferred="R54")],
        "LARGE": [_section("F2", 48)],
        "THIRD": [_section("F3", 23)],
    }
    allocation = _allocate(enrollment, [_room("R54", 54), _room("R26", 26), _room("R23", 23)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert allocation["LARGE"][0]["room_code"] == "R54"
    assert allocation["SMALL"][0]["room_code"] == "R26"


def test_preferred_room_is_honored_between_equally_suitable_rooms():
    enrollment = {"DS113": [_section("F01", 26, preferred="Z-PREFERRED")]}
    allocation = _allocate(enrollment, [_room("A-EQUAL", 30), _room("Z-PREFERRED", 30)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert allocation["DS113"][0]["room_code"] == "Z-PREFERRED"


def test_global_section_priority_does_not_finish_one_course_before_the_next():
    enrollment = {
        "FIRST": [_section("F60", 60), _section("F20", 20, preferred="R50")],
        "SECOND": [_section("F50", 50)],
    }
    allocation = _allocate(enrollment, [_room("R60", 60), _room("R50", 50), _room("R20", 20)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert allocation["SECOND"][0]["room_code"] == "R50"
    assert {r["room_code"] for r in allocation["FIRST"]} == {"R60", "R20"}


def test_repair_swaps_another_course_to_consolidate_without_splitting():
    enrollment = {
        "COMBINED": [_section("F40", 40), _section("F20", 20)],
        "OTHER": [_section("F35", 35)],
    }
    allocation = _allocate(enrollment, [_room("R60", 60), _room("R40", 40)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert len(allocation["COMBINED"]) == 1
    assert allocation["COMBINED"][0]["room_code"] == "R60"
    assert allocation["COMBINED"][0]["student_count"] == 60
    assert allocation["OTHER"][0]["room_code"] == "R40"


def test_repair_tries_alternative_section_combinations_before_split():
    enrollment = {
        "CS113": [_section(f"F{i}", count) for i, count in enumerate([30, 25, 15, 10, 10, 10])]
    }
    allocation = _allocate(enrollment, [_room("R50A", 50), _room("R50B", 50)])
    _assert_whole_and_assigned(allocation, enrollment)
    assert len(allocation["CS113"]) == 2
    assert sorted(
        sorted(part["student_count"] for part in room["section_parts"])
        for room in allocation["CS113"]
    ) == [[10, 10, 30], [10, 15, 25]]


def test_shared_code_distinct_identities_never_share_room():
    enrollment = {
        "CS111 (1)": [_section("F1", 26)],
        "CS111 (2)": [_section("F1", 23)],
    }
    entries = [
        _entry("CS111 (1)", identity="CS111|fundamentals of programming"),
        _entry("CS111 (2)", identity="CS111|programming i"),
    ]
    allocation = _allocate(
        enrollment, [_room("R54", 54), _room("R26", 26), _room("R23", 23)], entries=entries
    )
    _assert_whole_and_assigned(allocation, enrollment)
    assert len(allocation["CS111 (1)"]) == len(allocation["CS111 (2)"]) == 1
    assert allocation["CS111 (1)"][0]["room_code"] != allocation["CS111 (2)"][0]["room_code"]


def test_gender_cohorts_remain_separate_and_unknown_is_not_guessed():
    enrollment = {
        "CS113": [
            _section("F01", 17),
            _section("M01", 19, gender="M"),
            _section("", 3, gender="U", key="unmapped:U:missing"),
        ]
    }
    allocation = _allocate(
        enrollment, [_room("F40", 40), _room("M40", 40, "M"), _room("U40", 40, "U")]
    )
    by_gender = {room["gender"]: room for room in allocation["CS113"]}
    assert by_gender["F"]["room_code"] == "F40"
    assert by_gender["M"]["room_code"] == "M40"
    assert by_gender["U"]["room_code"] == "UNASSIGNED"


@pytest.mark.parametrize("count, assigned, unassigned", [(76, 76, 0), (100, 80, 20)])
def test_unavoidable_split_uses_available_capacity_without_losing_students(
    count, assigned, unassigned
):
    enrollment = {"CS113": [_section("F01/+شعبة", count)]}
    allocation = _allocate(enrollment, [_room("R54", 54), _room("R26", 26)])
    assigned_rooms = [r for r in allocation["CS113"] if r["room_code"] != "UNASSIGNED"]
    assert len(assigned_rooms) == 2
    assert sum(r["student_count"] for r in assigned_rooms) == assigned
    assert (
        sum(r["student_count"] for r in allocation["CS113"] if r["room_code"] == "UNASSIGNED")
        == unassigned
    )
    assert {part["section"] for r in allocation["CS113"] for part in r["section_parts"]} == {
        "F01/+شعبة"
    }


def test_rooms_are_reusable_in_another_period_and_overflow_has_no_allocation():
    enrollment = {code: [_section("F01", 26)] for code in ("FIRST", "SECOND", "OVER")}
    entries = [_entry("FIRST"), _entry("SECOND", slot=1), {**_entry("OVER"), "day": "OVERFLOW"}]
    allocation = _allocate(enrollment, [_room("R26", 26)], entries=entries)
    assert allocation["FIRST"][0]["room_code"] == allocation["SECOND"][0]["room_code"] == "R26"
    assert allocation["OVER"] == []


def test_allocation_is_deterministic_across_input_order_and_scheduler_seed():
    enrollment = {
        "TIED-B": [_section("F2", 18), _section("F1", 18)],
        "TIED-A": [_section("F3", 18), _section("F4", 18)],
        "SMALL": [_section("F5", 9)],
    }
    rooms = [
        _room("R36B", 36),
        _room("R36A", 36),
        _room("R18B", 18),
        _room("R18A", 18),
        _room("R9", 9),
    ]
    reference = None
    for seed in [None, 0, 1, 7, 21]:
        # Each variant must solve from cold state, not simply reuse the first result.
        with exam_room_allocation._CACHE_LOCK:
            exam_room_allocation._CACHE.clear()
        rng = random.Random(seed)
        codes = list(enrollment)
        rng.shuffle(codes)
        varied_enrollment = {code: deepcopy(enrollment[code]) for code in codes}
        for sections in varied_enrollment.values():
            rng.shuffle(sections)
        varied_rooms = deepcopy(rooms)
        rng.shuffle(varied_rooms)
        allocation = _allocate(varied_enrollment, varied_rooms, seed=seed)
        _assert_whole_and_assigned(allocation, enrollment)
        serialized = json.dumps(allocation, sort_keys=True)
        if reference is None:
            reference = serialized
        assert serialized == reference


def test_repeated_allocation_replaces_previous_rooms_without_duplicates():
    enrollment = {"DS113": [_section("F01", 26), _section("F1", 23)]}
    rooms = [_room("R54", 54), _room("R26", 26), _room("R23", 23)]
    entries = [_entry("DS113")]
    first = deepcopy(_allocate(enrollment, rooms, entries=entries, seed=0))
    second = _allocate(enrollment, rooms, entries=entries, seed=99)
    assert second == first


def test_missing_room_inventory_keeps_every_section_explicitly_unassigned():
    enrollment = {"DS113": [_section("F01", 26), _section("F1", 23)]}
    allocation = _allocate(enrollment, [])
    assert all(room["room_code"] == "UNASSIGNED" for room in allocation["DS113"])
    assert sum(room["student_count"] for room in allocation["DS113"]) == 49


@pytest.mark.parametrize("mutation", ["membership", "capacity", "preference"])
def test_cache_invalidates_on_authoritative_allocation_inputs(mutation):
    enrollment = {
        "DS113": [{**_section("F01", 26, preferred="R30A"), "membership_fingerprint": "first"}]
    }
    rooms = [_room("R30A", 30), _room("R30B", 30)]
    first_context = exam_room_allocation.RoomAllocationContext()
    assign_rooms_to_schedule([_entry("DS113")], enrollment, rooms, allocation_context=first_context)
    assert first_context.periods_solved == 1
    if mutation == "membership":
        enrollment["DS113"][0]["membership_fingerprint"] = "different-students-same-count"
    elif mutation == "capacity":
        rooms[0]["capacity"] = 27
    else:
        enrollment["DS113"][0]["preferred_room"] = "R30B"
    fresh_context = exam_room_allocation.RoomAllocationContext()
    entries = [_entry("DS113")]
    assign_rooms_to_schedule(entries, enrollment, rooms, allocation_context=fresh_context)
    _assert_conserved(entries, enrollment, rooms)
    assert fresh_context.periods_solved == 1
    assert fresh_context.cache_hits == 0
    if mutation == "capacity":
        assert entries[0]["rooms"][0]["room_capacity"] == 27
    elif mutation == "preference":
        assert entries[0]["rooms"][0]["room_code"] == "R30B"


def test_moving_exam_only_recomputes_source_and_destination_periods():
    enrollment = {code: [_section("F01", 10)] for code in ("MOVE", "STAY", "DEST", "UNAFFECTED")}
    rooms = [_room("R10A", 10), _room("R10B", 10)]
    entries = [_entry("MOVE"), _entry("STAY"), _entry("DEST", slot=1), _entry("UNAFFECTED", slot=2)]
    initial_context = exam_room_allocation.RoomAllocationContext()
    assign_rooms_to_schedule(entries, enrollment, rooms, allocation_context=initial_context)
    assert initial_context.periods_solved == 3
    unaffected = deepcopy(entries[-1]["rooms"])
    entries[0].update(slot_index=1, period="10:30-12:30")
    moved_context = exam_room_allocation.RoomAllocationContext()
    assign_rooms_to_schedule(entries, enrollment, rooms, allocation_context=moved_context)
    _assert_conserved(entries, enrollment, rooms)
    assert moved_context.periods_solved == 2
    assert moved_context.cache_hits == 1
    assert entries[-1]["rooms"] == unaffected


def test_cached_results_are_detached_from_output_mutation():
    enrollment = {"DS113": [_section("F01", 26)]}
    rooms = [_room("R30", 30)]
    entries = [_entry("DS113")]
    original = deepcopy(_allocate(enrollment, rooms, entries=entries))
    entries[0]["rooms"][0]["student_count"] = 999
    entries[0]["rooms"][0]["section_parts"][0]["section"] = "Corrupted by caller"
    assert _allocate(enrollment, rooms, entries=entries) == original


def test_wall_timeout_does_not_cache_degraded_partial_allocation():
    enrollment = {
        "COMBINED": [_section("F40", 40), _section("F20", 20)],
        "OTHER": [_section("F35", 35)],
    }
    rooms = [_room("R60", 60), _room("R40", 40)]
    expired_context = exam_room_allocation.RoomAllocationContext(deadline=0)
    with pytest.raises(exam_room_allocation.RoomAllocationTimeout):
        assign_rooms_to_schedule(
            [_entry(code) for code in enrollment],
            enrollment,
            rooms,
            allocation_context=expired_context,
        )
    assert not exam_room_allocation._CACHE
    allocation = _allocate(enrollment, rooms)
    _assert_whole_and_assigned(allocation, enrollment)


def test_joint_repair_is_deterministic_from_cold_cache_with_shuffled_inputs():
    enrollment = {
        "REPAIR": [_section(f"F{i}", count) for i, count in enumerate([30, 25, 15, 10, 10, 10])]
    }
    rooms = [_room("R50A", 50), _room("R50B", 50)]
    reference = None
    for seed in [None, 1, 19]:
        with exam_room_allocation._CACHE_LOCK:
            exam_room_allocation._CACHE.clear()
        rng = random.Random(seed)
        shuffled = deepcopy(enrollment)
        rng.shuffle(shuffled["REPAIR"])
        rng.shuffle(rooms)
        entries = [_entry("REPAIR")]
        context = exam_room_allocation.RoomAllocationContext()
        assign_rooms_to_schedule(entries, shuffled, rooms, seed=seed, allocation_context=context)
        assert context.searches > 0
        assert context.cache_hits == 0
        _assert_conserved(entries, enrollment, rooms)
        allocation = {entry["course_code"]: entry["rooms"] for entry in entries}
        _assert_whole_and_assigned(allocation, enrollment)
        serialized = json.dumps(allocation, sort_keys=True)
        if reference is None:
            reference = serialized
        assert serialized == reference
