"""Every solver honours a lock: invariants, never a solver's placements.

A locked cell is CLOSED. Its exams stay where they are, with their rooms, and
nothing else enters it; they still count for their students as fixed context.
These tests assert exactly that, on every board of the linked-exams parity
corpus and on small boards built to make each rule the only thing deciding.
They never record where a solver put an exam: CP-SAT and the reservoir tie
break differ across platforms, invariants do not.
"""

import functools
import json
import random

import pytest

from core.services import exam_min_change, exam_timetable
from core.services.exam_locks import ExamLocks
from core.services.exam_min_change import _move_lower_bound, find_violations, repair_minimum_change
from core.services.exam_room_allocation import RoomAllocationContext
from core.services.linked_exams import NO_LINKS, resolve_linked_exams
from tests import exam_linked_parity_corpus as corpus


def _locks(
    entries: list[dict], cells: set[tuple[str, str]], days, periods, sections=None
) -> ExamLocks:
    """The locks of ``cells``, holding what ``entries`` place there with their rooms."""
    held = {
        entry["course_code"]: entry
        for entry in entries
        if (entry.get("day"), entry.get("period")) in cells
    }
    return ExamLocks(
        canonical=tuple({"day": day, "period": period} for day, period in sorted(cells)),
        cells=frozenset(cells),
        placements={code: (entry["day"], entry["period"]) for code, entry in held.items()},
        identities={code: code for code in held},
        rooms_text={
            code: json.dumps(entry.get("rooms") or [], ensure_ascii=False)
            for code, entry in held.items()
        },
        sections_text={
            code: json.dumps((sections or {}).get(code, []), ensure_ascii=False) for code in held
        },
        operations_text={code: "[]" for code in held},
        day_order=tuple(days),
        period_order=tuple(periods),
    )


def _where(entries) -> dict[str, tuple[str, str]]:
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in entries}


def _assert_locks_held(entries, locks: ExamLocks) -> None:
    """I1: every locked exam at its cell. I2: nothing else in a locked cell.
    I3: no locked exam in OVERFLOW (I1 implies it; said for the reader)."""
    where = _where(entries)
    for code, cell in locks.placements.items():
        assert where[code] == cell, f"locked {code} moved"
        assert cell[0] != "OVERFLOW"
    intruders = sorted(
        code for code, cell in where.items() if cell in locks.cells and code not in locks.placements
    )
    assert not intruders, f"{intruders} entered a locked cell"


# ── the greedy scheduler (Build, Optimise, multistart) ──────────────────────


def _random_locks(board, entries, rng) -> set[tuple[str, str]]:
    days, periods = board["days"], board["periods"]
    cells = {
        (day, period)
        for day in rng.sample(days, rng.randint(0, max(0, len(days) - 1)))
        for period in periods
    }
    cells |= {
        (slot["day"], slot["period"])
        for slot in rng.sample(board["slots"], min(len(board["slots"]), rng.randint(0, 2)))
    }
    return cells


def _scheduler_cases():
    for number, board in enumerate(corpus.pure_boards()):
        _conflicts, adj, _thin = corpus.graph(
            board, exam_timetable.build_conflict_graph, exam_timetable.apply_thin_conflict_policy
        )
        first = exam_timetable.schedule(
            board["courses"],
            adj,
            board["slots"],
            enrolled_sets=board["enrolled"],
            max_per_day=board["max_per_day"],
            plan_term_buckets=board["plan_term_buckets"],
            course_buckets=board["course_buckets"],
            pinned=board["pinned"],
            credit_map=board["credit_map"],
            seed=board["seed"],
        )
        rng = random.Random(9100 + number)
        cells = _random_locks(board, first, rng)
        yield number, board, adj, first, cells, rng


def test_the_scheduler_keeps_every_lock_on_every_corpus_board():
    checked = 0
    for _number, board, adj, first, cells, _rng in _scheduler_cases():
        locks = _locks(first, cells, board["days"], board["periods"])
        pins = [
            pin
            for pin in board["pinned"]
            if pin["course_code"] not in locks.placements
            and (pin["day"], pin["period"]) not in cells
        ]
        entries = exam_timetable.schedule(
            board["courses"],
            adj,
            board["slots"],
            enrolled_sets=board["enrolled"],
            max_per_day=board["max_per_day"],
            plan_term_buckets=board["plan_term_buckets"],
            course_buckets=board["course_buckets"],
            pinned=pins,
            credit_map=board["credit_map"],
            preferred_slots=board["preferred"],
            seed=board["seed"],
            locked=locks.pins(),
            closed_slots=locks.closed_slots(board["slots"]),
        )
        assert sorted(entry["course_code"] for entry in entries) == sorted(board["courses"])
        _assert_locks_held(entries, locks)
        where = _where(entries)
        for pin in pins:
            assert where[pin["course_code"]] == (pin["day"], pin["period"])
        checked += bool(locks.placements)
    assert checked >= 30, "the corpus hardly locked anything"


def test_the_linked_scheduler_keeps_every_lock_and_every_link():
    checked = 0
    for _number, board, adj, first, cells, rng in _scheduler_cases():
        locks = _locks(first, cells, board["days"], board["periods"])
        # Links inside one locked cell, or wholly outside every lock.
        by_cell: dict[tuple[str, str], list[str]] = {}
        for code, cell in locks.placements.items():
            by_cell.setdefault(cell, []).append(code)
        free = sorted(code for code in board["courses"] if code not in locks.placements)
        groups = [sorted(codes)[:2] for codes in by_cell.values() if len(codes) >= 2]
        if len(free) >= 2:
            groups.append(rng.sample(free, 2))
        pins = [
            pin
            for pin in board["pinned"]
            if pin["course_code"] not in locks.placements
            and (pin["day"], pin["period"]) not in cells
            and not any(pin["course_code"] in group for group in groups)
        ]
        meta = {code: {"course_identity": code} for code in board["courses"]}
        links = resolve_linked_exams(
            [{"members": [{"course_identity": code} for code in group]} for group in groups],
            meta,
        )
        entries = exam_timetable.schedule_linked(
            board["courses"],
            adj,
            board["slots"],
            links=links,
            enrolled_sets=board["enrolled"],
            max_per_day=board["max_per_day"],
            plan_term_buckets=board["plan_term_buckets"],
            course_buckets=board["course_buckets"],
            pinned=pins,
            credit_map=board["credit_map"],
            seed=board["seed"],
            locked=locks.pins(),
            closed_slots=locks.closed_slots(board["slots"]),
        )
        _assert_locks_held(entries, locks)
        assert links.together(entries)
        checked += bool(links) and bool(locks.placements)
    assert checked >= 20


def test_the_scheduler_refuses_a_course_both_pinned_and_locked():
    slots = corpus._slots(["D1", "D2"], ["P1"])
    with pytest.raises(ValueError, match="both pinned and locked"):
        exam_timetable.schedule(
            ["A", "B"],
            {},
            slots,
            pinned=[{"course_code": "A", "day": "D1", "period": "P1"}],
            locked=[{"course_code": "A", "day": "D1", "period": "P1"}],
        )


def test_a_locked_exam_counts_for_its_students_when_the_rest_is_placed():
    """Rule 3. D2 is the busier day, so day load alone sends B to D1's free
    period; only A's students - locked on D1, max one exam a day - send B to D2."""
    slots = corpus._slots(["D1", "D2"], ["P1", "P2"])
    enrolled = {"A": {1, 2, 3}, "B": {1, 2, 3, 4}, "X": {10}, "Y": {11}}
    adj = {"A": {"B": 3}, "B": {"A": 3}}
    entries = exam_timetable.schedule(
        ["A", "B", "X", "Y"],
        adj,
        slots,
        enrolled_sets=enrolled,
        max_per_day=1,
        pinned=[
            {"course_code": "X", "day": "D2", "period": "P1"},
            {"course_code": "Y", "day": "D2", "period": "P2"},
        ],
        credit_map=None,
        locked=[{"course_code": "A", "day": "D1", "period": "P1"}],
        closed_slots=frozenset({0}),
    )
    where = _where(entries)
    assert where["A"] == ("D1", "P1")
    assert where["B"][0] == "D2"


def test_a_fully_locked_day_takes_no_new_exam_of_a_study_plan_term():
    """Feasibility: with W1 locked and holding one bucket-mate, the two free
    mates need two open days apart from it; one is all there is."""
    buckets = {("CS", 1): {"A", "B", "C"}}
    pinned = [{"course_code": "A", "day": "W1", "period": "P1"}]
    days, periods = ["W1", "W2", "W3"], ["P1"]
    assert exam_timetable.check_bucket_feasibility(buckets, 3, pinned=pinned) == []
    locks = ExamLocks(
        canonical=({"day": "W2"},),
        cells=frozenset({("W2", "P1")}),
        day_order=tuple(days),
        period_order=tuple(periods),
    )
    open_days = locks.open_days(days, periods)
    assert open_days == {"W1", "W3"}
    violations = exam_timetable.check_bucket_feasibility(
        buckets, 3, pinned=pinned, open_days=open_days
    )
    assert [row["courses"] for row in violations] == [["A", "B", "C"]]
    # Every day open is master's count, exactly.
    for fixed in ([], pinned):
        assert exam_timetable.check_bucket_feasibility(
            buckets, 3, pinned=fixed, open_days=set(days)
        ) == exam_timetable.check_bucket_feasibility(buckets, 3, pinned=fixed)


# ── rooms (rule 2) and the invigilator pass ─────────────────────────────────

ROOMS = [
    {"room_code": f"R{gender}{size}-{copy}", "capacity": size, "section": gender}
    for gender in ("M", "F")
    for size in (12, 20, 40)
    for copy in range(6)
]


def _section(code: str, count: int, gender: str = "M") -> list[dict]:
    return [
        {
            "section": f"{gender}1",
            "section_key": f"term-section:{code}",
            "gender": gender,
            "mapping_status": "mapped",
            "student_count": count,
            "preferred_room": "",
        }
    ]


def _entry(code: str, slots, index: int) -> dict:
    slot = slots[index]
    return {
        "course_code": code,
        "course_identity": code,
        "slot_index": index,
        "day": slot["day"],
        "period": slot["period"],
    }


def _hot_board():
    """D1 holds four department exams (four staff), D2 one: D2's free period is
    the only place the pass can move one to, since every D1 exam shares a
    student with Z. Student 100 already sits two exams on D1, so the move
    leaves every student-load aggregate as it was."""
    days, periods = ["D1", "D2"], ["P1", "P2"]
    slots = corpus._slots(days, periods)
    entries = [
        _entry("CS1", slots, 0),
        _entry("CS2", slots, 0),
        _entry("CS3", slots, 1),
        _entry("CS4", slots, 1),
        _entry("CSZ", slots, 2),
    ]
    enrolled = {
        "CS1": {1, 100},
        "CS2": {2, 101},
        "CS3": {3, 100},
        "CS4": {4, 101},
        "CSZ": {1, 2, 3, 4},
    }
    _conflicts, adj = exam_timetable.build_conflict_graph(enrolled)
    sections = {code: _section(code, 10) for code in enrolled}
    return days, periods, slots, entries, enrolled, adj, sections


def _pass(entries, sections, slots, adj, enrolled, **extra) -> int:
    return exam_timetable._rebalance_invigilators_pass(
        entries,
        sections,
        ROOMS,
        slots,
        adj,
        {},
        {},
        pinned_courses=set(),
        allocation_context=RoomAllocationContext.for_periods(16),
        enrolled_sets=enrolled,
        credit_map=None,
        max_per_day=2,
        caller="locks-test",
        **extra,
    )


def test_the_pass_moves_an_exam_into_the_free_period_when_nothing_is_locked():
    _days, _periods, slots, entries, enrolled, adj, sections = _hot_board()
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS)
    assert _pass(entries, sections, slots, adj, enrolled) >= 1
    assert any(_where(entries)[code] == ("D2", "P2") for code in ("CS1", "CS2", "CS3", "CS4"))


def test_the_pass_never_moves_an_exam_into_a_locked_empty_cell():
    days, periods, slots, entries, enrolled, adj, sections = _hot_board()
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS)
    locks = _locks(entries, {("D2", "P2")}, days, periods, sections)
    assert dict(locks.placements) == {}
    before = _where(entries)
    assert _pass(entries, sections, slots, adj, enrolled, locks=locks) == 0
    assert _where(entries) == before
    _assert_locks_held(entries, locks)


def test_a_fully_locked_busiest_day_keeps_its_exams_and_their_rooms():
    days, periods, slots, entries, enrolled, adj, sections = _hot_board()
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS)
    locks = _locks(entries, {("D1", "P1"), ("D1", "P2")}, days, periods, sections)
    saved = {code: locks.rooms_text[code] for code in locks.placements}
    assert _pass(entries, sections, slots, adj, enrolled, locks=locks) == 0
    _assert_locks_held(entries, locks)
    for entry in entries:
        if entry["course_code"] in saved:
            assert json.dumps(entry["rooms"], ensure_ascii=False) == saved[entry["course_code"]]


def test_the_pass_keeps_every_lock_on_every_corpus_board():
    checked = 0
    for number, board in enumerate(corpus.pure_boards()):
        _conflicts, adj, _thin = corpus.graph(
            board, exam_timetable.build_conflict_graph, exam_timetable.apply_thin_conflict_policy
        )
        entries = corpus.board_entries(board)
        sections = corpus._sections(board)
        context = RoomAllocationContext.for_periods(64)
        exam_timetable.assign_rooms_to_schedule(
            entries, sections, corpus.ROOMS, allocation_context=context
        )
        rng = random.Random(9300 + number)
        cells = _random_locks(board, entries, rng)
        locks = _locks(entries, cells, board["days"], board["periods"], sections)
        saved = dict(locks.rooms_text)
        exam_timetable._rebalance_invigilators_pass(
            entries,
            sections,
            corpus.ROOMS,
            board["slots"],
            adj,
            board["plan_term_buckets"],
            board["course_buckets"],
            max_trials=40,
            pinned_courses=set(),
            allocation_context=context,
            enrolled_sets=board["enrolled"],
            credit_map=board["credit_map"],
            max_per_day=board["max_per_day"],
            caller="locks-corpus",
            locks=locks,
        )
        _assert_locks_held(entries, locks)
        for entry in entries:
            if entry["course_code"] in saved:
                assert json.dumps(entry["rooms"], ensure_ascii=False) == saved[entry["course_code"]]
        checked += bool(locks.placements)
    assert checked >= 30


def test_rooming_never_allocates_a_locked_exam_and_restores_its_saved_rows(monkeypatch):
    days, periods, slots, entries, _enrolled, _adj, sections = _hot_board()
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS)
    # A saved room the allocator would never choose: the biggest, for ten.
    for entry in entries:
        if entry["course_code"] == "CS1":
            entry["rooms"][0].update(room_code="RM40-5", room_capacity=40)
    locks = _locks(entries, {("D1", "P1")}, days, periods, sections)
    asked: list[str] = []
    real = exam_timetable.allocate_period

    def spy(demands, rooms, context, **kwargs):
        asked.extend(demand["course_code"] for demand in demands)
        return real(demands, rooms, context, **kwargs)

    monkeypatch.setattr(exam_timetable, "allocate_period", spy)
    for entry in entries:
        entry["rooms"] = []
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS, locks=locks)
    assert not set(asked) & set(locks.placements)
    assert {"CS3", "CS4", "CSZ"} <= set(asked)
    for code in locks.placements:
        entry = next(entry for entry in entries if entry["course_code"] == code)
        assert json.dumps(entry["rooms"], ensure_ascii=False) == locks.rooms_text[code]
    assert (
        next(entry for entry in entries if entry["course_code"] == "CS1")["rooms"][0]["room_code"]
        == "RM40-5"
    )
    # Only unlocked slots are sized into the wall budget.
    assert exam_timetable.period_cohort_count(entries, sections, NO_LINKS, locks) == 2
    assert exam_timetable.period_cohort_count(entries, sections, NO_LINKS) == 3


def test_rooming_refuses_an_unlocked_exam_found_in_a_locked_cell():
    days, periods, slots, entries, _enrolled, _adj, sections = _hot_board()
    exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS)
    locks = _locks(entries, {("D1", "P1")}, days, periods, sections)
    entries.append(_entry("CS9", slots, 0))
    sections["CS9"] = _section("CS9", 5)
    with pytest.raises(RuntimeError, match="locked cell"):
        exam_timetable.assign_rooms_to_schedule(entries, sections, ROOMS, locks=locks)


# ── "Fix with fewest moves" ─────────────────────────────────────────────────


def test_a_repair_never_moves_an_exam_into_a_closed_slot():
    """B clashes with pinned A. Its only legal slot is closed, and C, which
    could step aside, is boxed in: B must leave the board, never enter the
    closed slot. Open the slot and it is the one-move repair."""
    adj = corpus._clash(("A", "B"), ("B", "C"), ("A", "C"))
    placements = {"A": 0, "B": 0, "C": 2}
    kwargs = {
        "placements": placements,
        "adj": adj,
        "slot_count": 3,
        "periods_per_day": 1,
        "protected": {"A"},
    }
    opened = repair_minimum_change(**kwargs)
    assert (opened.placements, opened.moved) == ({"A": 0, "B": 1, "C": 2}, ["B"])
    closed = repair_minimum_change(**kwargs, closed_slots=frozenset({1}))
    assert closed.unseated == ["B"]
    assert 1 not in closed.placements.values()
    assert closed.placements == {"A": 0, "C": 2}


def test_every_repair_keeps_locked_exams_and_closed_slots():
    proven = 0
    for number, board in enumerate(corpus.repair_boards(find_violations)):
        rng = random.Random(9500 + number)
        closed = rng.randrange(board["slot_count"])
        locked = {code for code, slot in board["placements"].items() if slot == closed}
        protected = set(board["protected"]) | locked
        result = repair_minimum_change(
            placements=board["placements"],
            adj=board["adj"],
            slot_count=board["slot_count"],
            periods_per_day=board["periods_per_day"],
            plan_term_buckets=board["buckets"],
            protected=protected,
            closed_slots=frozenset({closed}),
        )
        for code in locked:
            assert result.placements.get(code) == closed, f"board {number}: locked {code} moved"
            assert code not in result.moved and code not in result.unseated
        for code in result.moved:
            assert result.placements[code] != closed, f"board {number}: {code} entered the lock"
        if result.proven_minimal:
            # Proven means the move count reached the floor; the sweep that
            # picks among equal repairs may stop unproven, so only the count
            # is asserted, never which slots.
            assert len(result.moved) == _move_lower_bound(
                protected,
                board["placements"],
                board["adj"],
                board["buckets"],
                board["periods_per_day"],
            )
            proven += 1
    assert proven >= 50


def test_a_repair_without_closed_slots_is_the_same_call():
    board = next(iter(corpus.repair_boards(find_violations)))
    base = {key: board[key] for key in ("placements", "adj", "slot_count", "periods_per_day")}
    plain = repair_minimum_change(
        **base, plan_term_buckets=board["buckets"], protected=board["protected"]
    )
    empty = functools.partial(repair_minimum_change, closed_slots=frozenset())(
        **base, plan_term_buckets=board["buckets"], protected=board["protected"]
    )
    assert plain == empty
    assert exam_min_change._build_model.__kwdefaults__["closed_slots"] == frozenset()


# ── multistart's movement report ─────────────────────────────────────────────


def test_multistart_counts_movement_by_day_and_period_when_there_are_locks():
    """A Build with locks may add a period: every later slot number shifts, and a
    locked exam that never moved would read as moved by its slot number."""
    from core.services.exam_multistart import _movement_vs_baseline

    candidate = {
        "schedule": [
            {"course_code": "A", "day": "Sun", "period": "08:00-10:00", "slot_index": 0},
            {"course_code": "B", "day": "Mon", "period": "08:00-10:00", "slot_index": 3},
        ]
    }
    by_slot = {"A": ("Sun", 0), "B": ("Mon", 2)}
    by_time = {"A": ("Sun", "08:00-10:00"), "B": ("Mon", "08:00-10:00")}
    assert _movement_vs_baseline(candidate, by_slot) == (1, ["B"])
    assert _movement_vs_baseline(candidate, by_time, by_time=True) == (0, [])
