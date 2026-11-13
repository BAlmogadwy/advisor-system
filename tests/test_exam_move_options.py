"""The other times one exam could move to, ranked as Optimize ranks a board.

Pure engine tests - no database. Every place is checked against a full recount
of the board with the exam moved there: the ranking counts only the students
and pairs the move can change, and must still agree with the whole count.
"""

from __future__ import annotations

import itertools
import random
import re
from pathlib import Path

import pytest
from django.conf import settings

from core.services.exam_move_options import (
    BETTER,
    LEVELS,
    SAME,
    WORSE,
    ExamNotMovable,
    Seats,
    rank_move_options,
)
from core.services.exam_optimise import Board, Score, score
from core.services.exam_timetable import is_heavy_credit_day


def _board(**overrides) -> Board:
    base = {
        "exams": ["A", "B"],
        "current": {"A": 0, "B": 1},
        "fixed": frozenset(),
        "adj": {},
        "buckets": {},
        "slot_count": 6,
        "periods_per_day": 2,
        "sittings": {},
    }
    base.update(overrides)
    return Board(**base)


def _rank(board: Board, exam: str, placements=None, seats: Seats | None = None):
    return rank_move_options(
        board,
        exam,
        board.current if placements is None else placements,
        is_heavy_credit_day,
        seats=seats,
    )


def _recount(board: Board, placements, seats: Seats | None) -> dict[str, int]:
    """The whole board counted afresh, every level, the seats included."""
    levels = score(board, placements, is_heavy_credit_day).as_dict()
    levels["seats_short"] = (
        sum(seats.short(load) for load in seats.loads(placements).values()) if seats else 0
    )
    return {level: levels[level] for level in LEVELS}


def _key(levels) -> tuple[int, ...]:
    return tuple(levels[level] for level in LEVELS)


def _random_board(rng: random.Random) -> tuple[Board, Seats | None]:
    exams = rng.randint(3, 9)
    names = [f"E{index:02d}" for index in range(exams)]
    periods = rng.randint(1, 3)
    slot_count = rng.randint(2, 5) * periods
    # At least one slot stays open, so a movable exam has somewhere to sit.
    closed = frozenset(rng.sample(range(slot_count), rng.randint(0, min(2, slot_count - 1))))
    open_slots = [slot for slot in range(slot_count) if slot not in closed]
    fixed = frozenset(rng.sample(names, rng.randint(0, exams - 1)))
    current = {
        name: rng.choice(range(slot_count) if name in fixed else open_slots) for name in names
    }
    for name in rng.sample(sorted(set(names) - fixed), rng.randint(0, 1)):
        del current[name]  # an exam in OVERFLOW
    sittings = {}
    for student in range(rng.randint(5, 40)):
        sat = rng.sample(names, rng.randint(1, min(4, exams)))
        sittings[student] = [(name, rng.choice([2, 3, 4, 5])) for name in sat]
    adj: dict[str, dict[str, int]] = {}
    for sat in sittings.values():
        for (a, _), (b, _) in itertools.combinations(sat, 2):
            # Not every shared student is a clash the board forbids: thin courses.
            if rng.random() < 0.8:
                adj.setdefault(a, {})[b] = adj.setdefault(b, {}).setdefault(a, 0) + 1
                adj[b][a] = adj[a][b]
    buckets = {
        ("P", term): set(rng.sample(names, rng.randint(2, min(4, exams))))
        for term in range(rng.randint(0, 3))
    }
    kinds = ("M", "F", "total")
    board = Board(
        exams=names,
        current=current,
        fixed=fixed,
        adj=adj,
        buckets=buckets,
        slot_count=slot_count,
        periods_per_day=periods,
        sittings=sittings,
        max_per_day=rng.choice([1, 2]),
        closed_slots=closed,
        weights={name: rng.choice([1, 1, 2]) for name in names},
        staff={name: {kind: rng.randint(0, 3) for kind in kinds} for name in names},
        staff_limits={kind: rng.randint(1, 6) for kind in kinds} if rng.random() < 0.7 else {},
    )
    seats = None
    if rng.random() < 0.7:
        seats = Seats(
            capacity={"M": rng.randint(0, 60), "F": rng.randint(0, 60)},
            need={
                name: {cohort: rng.randint(0, 40) for cohort in ("M", "F", "U")} for name in names
            },
        )
    return board, seats


@pytest.mark.parametrize("seed", range(120))
def test_every_place_scores_as_a_full_recount_of_the_board_with_the_exam_there(seed):
    rng = random.Random(seed)
    board, seats = _random_board(rng)
    placements = dict(board.current)
    for exam in board.exams:
        if exam in board.fixed:
            continue
        ranked = _rank(board, exam, placements, seats)
        current = _recount(board, placements, seats)
        assert ranked.current == current
        here = placements.get(exam)
        assert [option.slot for option in sorted(ranked.options, key=lambda o: o.slot)] == [
            slot
            for slot in range(board.slot_count)
            if slot != here and slot not in board.closed_slots
        ], "every open place but its own, no locked cell"
        for option in ranked.options:
            moved = {**placements, exam: option.slot}
            expected = _recount(board, moved, seats)
            assert option.levels == expected, (exam, option.slot)
            assert option.changes == {level: expected[level] - current[level] for level in LEVELS}
            verdict = (
                BETTER
                if _key(expected) < _key(current)
                else SAME
                if _key(expected) == _key(current)
                else WORSE
            )
            assert option.verdict == verdict
        assert [(_key(o.levels), o.slot) for o in ranked.options] == sorted(
            (_key(o.levels), o.slot) for o in ranked.options
        ), "best first, then calendar order"


def test_the_levels_are_optimizes_in_its_order_with_the_seats_after_the_overflow():
    optimise_order = list(Score.__dataclass_fields__)
    assert [level for level in LEVELS if level != "seats_short"] == optimise_order
    assert LEVELS.index("seats_short") == LEVELS.index("unseated") + 1


def test_the_page_names_every_level_in_the_servers_order():
    script = (Path(settings.BASE_DIR) / "static/js/page-exam-timetable.js").read_text("utf-8")
    listed = re.search(r"const MOVE_TEXT = \{\n  levels: \[([^\]]*)\]", script)
    assert listed, "MOVE_TEXT.levels is where the page reads the order"
    assert tuple(re.findall(r"'([a-z_]+)'", listed.group(1))) == LEVELS
    named = re.search(r"\n  level: \{\n(.*?)\n  \},", script, re.S)
    assert named
    # Every level but the spacing, which is said as a direction, has a name.
    assert set(re.findall(r"^\s+([a-z_]+): \(\) =>", named.group(1), re.M)) == set(LEVELS) - {
        "spacing"
    }


def test_a_pinned_or_locked_exam_has_no_other_place_and_an_unknown_exam_is_refused():
    board = _board(fixed=frozenset({"A"}))
    with pytest.raises(ExamNotMovable):
        _rank(board, "A")
    with pytest.raises(ValueError) as unknown:
        _rank(board, "Z")
    assert not isinstance(unknown.value, ExamNotMovable)
    assert [option.slot for option in _rank(board, "B").options] == [0, 2, 3, 4, 5]


def test_an_exam_in_overflow_is_offered_every_open_place_and_seating_it_is_better():
    board = _board(current={"B": 1}, closed_slots=frozenset({4}), weights={"A": 2})
    ranked = _rank(board, "A")
    assert [option.slot for option in ranked.options] == [0, 1, 2, 3, 5]
    assert ranked.current["unseated"] == 2
    assert all(option.changes["unseated"] == -2 for option in ranked.options)
    assert {option.verdict for option in ranked.options} == {BETTER}


def test_the_levels_decide_in_the_order_optimize_decides_them():
    # One student sits A and B. Days: slots 0-1, 2-3, 4-5.
    board = _board(
        exams=["A", "B", "C"],
        current={"A": 0, "B": 1, "C": 2},
        adj={"A": {"B": 1}, "B": {"A": 1}},
        sittings={1: [("A", 3), ("B", 3)]},
        staff={"A": {"total": 3}, "B": {"total": 1}, "C": {"total": 3}},
        staff_limits={"total": 4},
    )
    ranked = {option.slot: option for option in _rank(board, "A").options}
    # Off the shared day, beside C: the student is spared, the invigilators are not.
    assert ranked[3].changes["multi_exam_day"] == -1
    assert ranked[3].changes["staff_excess"] == 2
    assert ranked[3].verdict == WORSE
    # Spared and within the busiest day: better.
    assert ranked[4].changes == {**dict.fromkeys(LEVELS, 0), "multi_exam_day": -1}
    assert ranked[4].verdict == BETTER
    # Into B's period: a clash outranks everything below it.
    assert ranked[1].changes["rule_breaks"] == 1
    assert ranked[1].verdict == WORSE
    assert ranked[1].clashes_with == (("B", 1),)


def test_ending_a_clash_is_better_even_when_the_students_keep_their_shared_day():
    board = _board(
        exams=["A", "B"],
        current={"A": 0, "B": 0},
        adj={"A": {"B": 2}, "B": {"A": 2}},
        sittings={1: [("A", 3), ("B", 3)], 2: [("A", 3), ("B", 3)]},
    )
    ranked = {option.slot: option for option in _rank(board, "A").options}
    # Two papers in one period were already two exams that day.
    assert ranked[1].changes == {**dict.fromkeys(LEVELS, 0), "rule_breaks": -1}
    assert ranked[1].verdict == BETTER
    assert ranked[2].changes["multi_exam_day"] == -2
    assert ranked[2].verdict == BETTER
    assert _rank(board, "A").options[0].slot == 2, "the best place comes first"


def test_a_period_short_of_seats_is_worse_and_says_by_how_many():
    # 100 seats for men. B already seats 80 men in slot 2; A seats 30.
    seats = Seats(capacity={"M": 100, "F": 50}, need={"A": {"M": 30}, "B": {"M": 80}})
    board = _board(current={"A": 0, "B": 2})
    ranked = {option.slot: option for option in _rank(board, "A", seats=seats).options}
    assert ranked[2].changes["seats_short"] == 10
    assert ranked[2].levels["seats_short"] == 10
    assert ranked[2].verdict == WORSE
    assert ranked[1].changes["seats_short"] == 0
    assert ranked[1].verdict == SAME


def test_leaving_an_overfull_period_gives_its_seats_back():
    seats = Seats(capacity={"M": 100}, need={"A": {"M": 30}, "B": {"M": 90}})
    board = _board(current={"A": 0, "B": 0})
    ranked = _rank(board, "A", seats=seats)
    assert ranked.current["seats_short"] == 20
    assert {option.changes["seats_short"] for option in ranked.options} == {-20}
    assert {option.verdict for option in ranked.options} == {BETTER}


def test_a_cohort_no_room_can_seat_is_never_counted_short():
    # Sections of unknown gender have no room anywhere; moving them changes nothing.
    seats = Seats(capacity={"M": 10, "F": 10}, need={"A": {"U": 500}, "B": {"U": 500}})
    board = _board(current={"A": 0, "B": 2})
    ranked = _rank(board, "A", seats=seats)
    assert ranked.current["seats_short"] == 0
    assert {option.changes["seats_short"] for option in ranked.options} == {0}


def test_the_exams_a_place_would_clash_with_or_share_a_day_with_are_named():
    board = _board(
        exams=["A", "B", "C", "D"],
        current={"A": 0, "B": 2, "C": 2, "D": 3},
        adj={"A": {"B": 2, "C": 5}, "B": {"A": 2}, "C": {"A": 5}},
        buckets={("P", 1): {"A", "D"}},
        sittings={},
    )
    ranked = {option.slot: option for option in _rank(board, "A").options}
    assert ranked[2].clashes_with == (("C", 5), ("B", 2)), "most shared first"
    assert ranked[2].same_day_with == ("D",), "slots 2 and 3 are one day"
    assert ranked[3].clashes_with == ()
    assert ranked[3].same_day_with == ("D",)
    assert ranked[4].clashes_with == () and ranked[4].same_day_with == ()
