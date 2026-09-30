"""The exam optimiser: it finds the best board, and never makes one worse.

Pure engine tests - no database. Small boards are checked against a brute-force
oracle that tries every placement; larger ones against the rules no answer may
break.
"""

from __future__ import annotations

import itertools
import random

import pytest

from core.services import exam_optimise
from core.services.exam_optimise import Board, Score, optimise_board, score, spacing_penalty
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


def _optimise(board: Board, **kwargs):
    return optimise_board(board, day_is_heavy=is_heavy_credit_day, **kwargs)


def _score(board: Board, placements) -> Score:
    return score(board, placements, is_heavy_credit_day)


def _breaks_hard_rule(board: Board, placements: dict[str, int], exam: str) -> bool:
    """``exam`` clashes with a student's other exam or shares a day with a bucket-mate."""
    slot = placements[exam]
    if slot in board.closed_slots:
        return True
    if any(placements.get(mate) == slot for mate in board.adj.get(exam, {})):
        return True
    for members in board.buckets.values():
        if exam in members and any(
            mate != exam
            and mate in placements
            and board.day_of(placements[mate]) == slot // board.periods_per_day
            for mate in members
        ):
            return True
    return False


def _oracle(board: Board) -> tuple[Score, int]:
    """The best (score, moves) over every legal board, by trying them all."""
    movable = sorted(exam for exam in board.exams if exam not in board.fixed)
    given = dict(board.current)
    before = _score(board, given)
    legal_given = not before.rule_breaks
    cap = (
        before.spacing if legal_given and not before.unseated and not before.staff_excess else None
    )
    best = (before, 0)
    for choice in itertools.product([None, *range(board.slot_count)], repeat=len(movable)):
        placements = {exam: slot for exam, slot in given.items() if exam in board.fixed}
        placements.update(
            {exam: slot for exam, slot in zip(movable, choice, strict=True) if slot is not None}
        )
        if any(
            _breaks_hard_rule(board, placements, exam) for exam in movable if exam in placements
        ):
            continue
        found = _score(board, placements)
        if cap is not None and (found.unseated or found.staff_excess or found.spacing > cap):
            continue
        moves = sum(
            board.weight(exam) for exam in movable if placements.get(exam) != given.get(exam)
        )
        best = min(best, (found, moves))
    return best


def _random_board(
    rng: random.Random, *, exams: int, movable: int, days: int, periods: int
) -> Board:
    names = [f"E{index:02d}" for index in range(exams)]
    slot_count = days * periods
    closed = frozenset(rng.sample(range(slot_count), rng.randint(0, 2)))
    open_slots = [slot for slot in range(slot_count) if slot not in closed]
    fixed = frozenset(rng.sample(names, exams - movable))
    current = {}
    for name in names:
        # A fixed exam may sit anywhere, a closed slot included: it was locked there.
        current[name] = rng.choice(range(slot_count) if name in fixed else open_slots)
    for name in rng.sample(sorted(set(names) - fixed), rng.randint(0, 1)):
        del current[name]  # an exam in OVERFLOW
    sittings = {}
    for student in range(rng.randint(8, 30)):
        sat = rng.sample(names, rng.randint(1, min(4, exams)))
        sittings[student] = [(name, rng.choice([2, 3, 4])) for name in sat]
    adj: dict[str, dict[str, int]] = {}
    for sat in sittings.values():
        for (a, _), (b, _) in itertools.combinations(sat, 2):
            # Not every shared student is a clash the board forbids: thin courses.
            if rng.random() < 0.8:
                adj.setdefault(a, {})[b] = adj.setdefault(b, {}).setdefault(a, 0) + 1
                adj[b][a] = adj[a][b]
    buckets = {
        ("P", term): set(rng.sample(names, rng.randint(2, 3))) for term in range(rng.randint(0, 2))
    }
    staff = {name: {"total": rng.randint(0, 3)} for name in names}
    limits = {"total": rng.randint(2, 6)} if rng.random() < 0.6 else {}
    return Board(
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
        staff=staff,
        staff_limits=limits,
    )


# 152, 565 and 976: boards where the students gain nothing and only the
# spacing can - a solve that stopped at "no gain" left them untidied.
@pytest.mark.parametrize("seed", [*range(60), 152, 343, 565, 976])
def test_a_small_board_gets_the_best_board_there_is(seed):
    rng = random.Random(seed)
    board = _random_board(rng, exams=6, movable=rng.randint(1, 4), days=3, periods=2)
    before = _score(board, board.current)
    best, best_moves = _oracle(board)

    result = _optimise(board)

    assert result.proven
    assert result.before == before
    if best < before:
        assert result.improved
        assert result.after == best == _score(board, result.placements)
        moved = sum(board.weight(exam) for exam in result.moved)
        assert moved == best_moves
    else:
        assert not result.improved
        assert result.placements == dict(board.current)
        assert result.after == before
        assert result.moved == []


@pytest.mark.parametrize("seed", range(25))
def test_a_large_board_is_never_worse_and_never_breaks_a_rule(seed):
    rng = random.Random(1000 + seed)
    board = _random_board(rng, exams=14, movable=10, days=4, periods=3)
    before = _score(board, board.current)

    result = _optimise(board, rounds=25, neighbourhood=4, seed=seed)

    assert not result.proven
    assert result.after <= before
    assert result.after == _score(board, result.placements)
    assert result.improved == (result.after < before)
    for exam in board.fixed:
        assert result.placements.get(exam) == board.current.get(exam)
    if not result.improved:
        assert result.placements == dict(board.current)
        return
    given = dict(board.current)
    for exam in sorted(set(board.exams) - board.fixed):
        if exam in result.placements and result.placements[exam] != given.get(exam):
            assert not _breaks_hard_rule(board, result.placements, exam), exam
    assert result.moved == sorted(
        exam
        for exam in set(board.exams) - board.fixed
        if result.placements.get(exam) != given.get(exam)
    )


def _legal_board(
    rng: random.Random, *, exams: int, days: int, periods: int, terms: int | None = None
) -> Board:
    """A board that seats every exam and breaks no rule, all exams movable.

    ``terms``: that many (programme, term)s of three exams each, so spacing matters.
    """
    while True:
        board = _random_board(rng, exams=exams, movable=exams, days=days, periods=periods)
        changes: dict = {"staff_limits": {}, "closed_slots": frozenset()}
        if terms is not None:
            changes["buckets"] = {
                ("P", term): set(rng.sample(list(board.exams), 3)) for term in range(terms)
            }
        board = _board(**{**board.__dict__, **changes})
        placements: dict[str, int] = {}
        for exam in board.exams:
            slots = list(range(board.slot_count))
            rng.shuffle(slots)
            for slot in slots:
                placements[exam] = slot
                if not _breaks_hard_rule(board, placements, exam):
                    break
                del placements[exam]
        board = _board(**{**board.__dict__, "current": placements})
        if len(placements) == exams and not _score(board, placements).rule_breaks:
            return board


@pytest.mark.parametrize("seed", range(40))
def test_a_legal_small_board_gets_the_best_board_with_spacing_no_worse(seed):
    board = _legal_board(random.Random(3000 + seed), exams=5, days=4, periods=1)
    before = _score(board, board.current)
    best, best_moves = _oracle(board)

    result = _optimise(board)

    assert result.proven
    assert result.after == best
    assert result.after.spacing <= before.spacing
    assert sum(board.weight(exam) for exam in result.moved) == best_moves


@pytest.mark.parametrize("seed", range(40))
def test_the_search_never_lets_spacing_get_worse_on_a_legal_board(seed):
    board = _legal_board(random.Random(5000 + seed), exams=18, days=6, periods=2, terms=6)
    before = _score(board, board.current)

    result = _optimise(board, rounds=40, neighbourhood=5, seed=seed)

    assert result.after.spacing <= before.spacing
    assert result.after.rule_breaks == 0 and result.after.unseated == 0
    assert result.after <= before


def test_every_solve_is_one_worker_seeded_and_limited_by_work_not_by_the_clock(monkeypatch):
    solvers = []
    real = exam_optimise.cp_model.CpSolver

    def recorded():
        solver = real()
        solvers.append(solver)
        return solver

    monkeypatch.setattr(exam_optimise.cp_model, "CpSolver", recorded)
    board = _random_board(random.Random(7), exams=14, movable=11, days=4, periods=3)
    _optimise(board, rounds=4, neighbourhood=4, seed=31)

    assert solvers
    for solver in solvers:
        assert solver.parameters.num_search_workers == 1
        assert solver.parameters.random_seed == 31
        assert solver.parameters.max_deterministic_time == exam_optimise._SOLVE_WORK
        assert solver.parameters.max_time_in_seconds == float("inf")


def test_a_neighbourhood_of_nothing_is_a_neighbourhood_of_one():
    board = _random_board(random.Random(7), exams=8, movable=6, days=4, periods=3)
    result = _optimise(board, rounds=3, neighbourhood=0)
    assert result.after <= result.before


def test_the_same_seed_gives_the_same_board_and_another_seed_may_not():
    board = _random_board(random.Random(7), exams=14, movable=11, days=4, periods=3)
    first = _optimise(board, rounds=20, neighbourhood=4, seed=3)
    again = _optimise(board, rounds=20, neighbourhood=4, seed=3)
    assert first.placements == again.placements
    assert first.after == again.after


def _two_exams_one_day() -> Board:
    """Ten students sit X and Y, both on day 0; day 1 and day 2 are empty."""
    return _board(
        exams=["X", "Y"],
        current={"X": 0, "Y": 1},
        adj={"X": {"Y": 10}, "Y": {"X": 10}},
        sittings={student: [("X", 3), ("Y", 3)] for student in range(10)},
    )


def test_students_are_spared_a_day_with_two_exams():
    result = _optimise(_two_exams_one_day())
    assert result.before.multi_exam_day == 10
    assert result.after.multi_exam_day == 0
    assert result.improved
    # One move is enough; the second exam stays where it was.
    assert len(result.moved) == 1


def test_a_fixed_exam_never_moves_even_when_it_is_the_one_in_the_way():
    # Only Y may move, and Y is the exam a tie-break would have kept.
    board = _two_exams_one_day()
    board = _board(**{**board.__dict__, "fixed": frozenset({"X"})})
    result = _optimise(board)
    assert result.placements["X"] == 0
    assert result.moved == ["Y"]
    assert result.after.multi_exam_day == 0

    both = _board(**{**board.__dict__, "fixed": frozenset({"X", "Y"})})
    held = _optimise(both)
    assert not held.improved
    assert held.proven
    assert held.movable == 0
    assert held.placements == {"X": 0, "Y": 1}


def test_no_exam_enters_a_closed_slot():
    # Every slot of the other days is closed: nothing can be done.
    board = _two_exams_one_day()
    board = _board(**{**board.__dict__, "closed_slots": frozenset({2, 3, 4, 5})})
    result = _optimise(board)
    assert not result.improved
    assert result.placements == {"X": 0, "Y": 1}

    # One open slot on day 2: that is where the moved exam goes.
    one_open = _board(**{**board.__dict__, "closed_slots": frozenset({2, 3, 4})})
    result = _optimise(one_open)
    assert result.improved
    assert sorted(result.placements.values()) in ([0, 5], [1, 5])


def test_a_board_that_cannot_be_bettered_comes_back_exactly_as_given():
    board = _board(
        exams=["X", "Y"],
        current={"X": 0, "Y": 4},
        sittings={1: [("X", 3), ("Y", 3)]},
    )
    result = _optimise(board)
    assert not result.improved
    assert result.placements == {"X": 0, "Y": 4}
    assert result.placements is not board.current
    assert result.before == result.after == Score()


def test_an_exam_in_overflow_is_seated_before_any_student_is_spared():
    # Z is off the board. Seating it puts its students on a two-exam day, and
    # that is still the better board: an unseated exam outweighs everything.
    board = _board(
        exams=["X", "Z"],
        current={"X": 0},
        fixed=frozenset({"X"}),
        slot_count=2,
        periods_per_day=2,
        sittings={student: [("X", 3), ("Z", 3)] for student in range(5)},
        adj={"X": {"Z": 5}, "Z": {"X": 5}},
    )
    result = _optimise(board)
    assert result.before.unseated == 1
    assert result.after.unseated == 0
    assert result.after.multi_exam_day == 5
    assert result.placements == {"X": 0, "Z": 1}


def test_an_exam_with_no_legal_slot_stays_in_overflow():
    board = _board(
        exams=["X", "Z"],
        current={"X": 0},
        fixed=frozenset({"X"}),
        slot_count=1,
        periods_per_day=1,
        sittings={1: [("X", 3), ("Z", 3)]},
        adj={"X": {"Z": 1}, "Z": {"X": 1}},
    )
    result = _optimise(board)
    assert not result.improved
    assert "Z" not in result.placements


def test_a_movable_exam_that_breaks_a_rule_is_moved_or_unseated_never_left():
    # X and Y clash in slot 0 and neither is fixed: the given board is illegal.
    board = _board(
        exams=["X", "Y"],
        current={"X": 0, "Y": 0},
        adj={"X": {"Y": 1}, "Y": {"X": 1}},
        sittings={1: [("X", 3), ("Y", 3)]},
    )
    result = _optimise(board)
    assert result.before.rule_breaks == 1
    assert result.after.rule_breaks == 0
    assert result.improved
    assert result.placements["X"] != result.placements["Y"]


def test_ending_a_clash_is_better_even_when_it_costs_the_students_a_shared_day():
    # X clashes with the fixed F in slot 0. The one other open slot puts X on
    # the day of G, which X's ten students also sit: worse for them, and right.
    board = _board(
        exams=["F", "G", "X"],
        current={"F": 0, "G": 2, "X": 0},
        fixed=frozenset({"F", "G"}),
        slot_count=4,
        periods_per_day=2,
        closed_slots=frozenset({1}),
        adj={"X": {"F": 1, "G": 10}, "F": {"X": 1}, "G": {"X": 10}},
        sittings={0: [("X", 3), ("F", 3)], **{n: [("X", 3), ("G", 3)] for n in range(1, 11)}},
    )
    result = _optimise(board)
    assert result.improved
    assert result.placements["X"] == 3
    assert (result.before.rule_breaks, result.after.rule_breaks) == (1, 0)
    assert result.after.multi_exam_day > result.before.multi_exam_day


def test_the_invigilator_limit_outranks_the_students():
    # Day 0 holds X and Y (two invigilators each, limit 4); P sits alone on
    # day 1 and needs three. Sparing the ten students means moving X or Y to
    # day 1 or 2; day 1 would then need five.
    board = _board(
        exams=["P", "X", "Y"],
        current={"P": 2, "X": 0, "Y": 1},
        fixed=frozenset({"P", "X"}),
        slot_count=4,
        periods_per_day=2,
        adj={"X": {"Y": 10}, "Y": {"X": 10}},
        sittings={student: [("X", 3), ("Y", 3)] for student in range(10)},
        staff={"P": {"total": 3}, "X": {"total": 2}, "Y": {"total": 2}},
        staff_limits={"total": 4},
    )
    held = _optimise(board)
    assert not held.improved
    assert held.after.staff_excess == 0
    assert held.after.multi_exam_day == 10

    # With the limit lifted, the students win.
    free = _optimise(_board(**{**board.__dict__, "staff_limits": {}}))
    assert free.improved
    assert free.after.multi_exam_day == 0
    assert board.day_of(free.placements["Y"]) == 1


def test_spacing_may_not_get_worse_on_a_legal_board():
    # B1 and B2 are one (programme, term), four days apart. Sparing the one
    # student a two-exam day by moving B1 next to B2 would cost spacing.
    board = _board(
        exams=["B1", "B2", "S"],
        current={"B1": 0, "B2": 8, "S": 1},
        fixed=frozenset({"B2", "S"}),
        slot_count=10,
        periods_per_day=2,
        closed_slots=frozenset({2, 3, 4, 5}),
        buckets={("P", 1): {"B1", "B2"}},
        adj={"B1": {"S": 1}, "S": {"B1": 1}},
        sittings={1: [("B1", 3), ("S", 3)]},
    )
    result = _optimise(board)
    # The only other open days are 3 (one day from B2) and 4 (B2's own day).
    assert not result.improved
    assert result.after.spacing == 0
    assert result.after.multi_exam_day == 1


def test_two_sittings_of_one_linked_exam_count_as_two_exams_that_day():
    # One student is registered in two members of link L.
    board = _board(
        exams=["L", "X"],
        current={"L": 0, "X": 2},
        sittings={1: [("L", 3), ("L", 4)], 2: [("L", 3), ("X", 3)]},
        adj={"L": {"X": 1}, "X": {"L": 1}},
    )
    before = _score(board, board.current)
    assert before.multi_exam_day == 1
    assert before.heavy_day == 1
    result = _optimise(board)
    # No move can separate the two sittings: nothing is better.
    assert not result.improved


def test_the_score_counts_what_the_qa_report_counts():
    board = _board(
        exams=["A", "B", "C", "D"],
        current={"A": 0, "B": 1, "C": 0, "D": 3},
        max_per_day=1,
        buckets={("P", 1): {"A", "D"}, ("P", 2): {"A", "D", "B"}},
        sittings={
            1: [("A", 4), ("B", 4)],  # heavy, over the limit of one, two exams
            2: [("A", 4), ("B", 2)],  # two exams, over the limit, not heavy
            3: [("A", 3), ("D", 3)],  # different days
            4: [("C", 3)],
        },
        staff={"A": {"total": 3}, "B": {"total": 2}, "C": {"total": 1}, "D": {"total": 9}},
        staff_limits={"total": 4},
        weights={"B": 2},
    )
    found = _score(board, board.current)
    assert found == Score(
        # A and B are one (programme, term) on one day.
        rule_breaks=1,
        unseated=0,
        staff_excess=(3 + 2 + 1 - 4) + (9 - 4),
        over_limit=2,
        heavy_day=1,
        multi_exam_day=2,
        # A-B share a bucket and a day; a day apart, A-D share two and B-D one.
        spacing=spacing_penalty(0) + 3 * spacing_penalty(1),
    )
    assert _score(board, {"A": 0, "C": 0, "D": 3}).unseated == 2


def test_progress_is_reported_and_a_stop_keeps_nothing():
    board = _random_board(random.Random(11), exams=14, movable=11, days=4, periods=3)
    seen: list[tuple[int, int]] = []
    _optimise(
        board, rounds=6, neighbourhood=4, on_round=lambda done, total: seen.append((done, total))
    )
    assert seen[0] == (0, 6)
    assert all(total == 6 for _done, total in seen)
    assert [done for done, _total in seen] == list(range(len(seen)))

    class Stop(Exception):
        pass

    def stop(done: int, total: int) -> None:
        if done == 2:
            raise Stop

    with pytest.raises(Stop):
        _optimise(board, rounds=6, neighbourhood=4, on_round=stop)


def test_the_search_stops_after_enough_rounds_without_a_better_board(monkeypatch):
    monkeypatch.setattr(exam_optimise, "_PATIENCE", 3)
    # Nothing can improve: X and Y already sit on different days.
    names = [f"N{index:02d}" for index in range(8)]
    board = _board(
        exams=names,
        current={name: index * 2 for index, name in enumerate(names)},
        slot_count=16,
        periods_per_day=2,
    )
    result = _optimise(board, rounds=50, neighbourhood=3)
    assert result.rounds == 3
    assert not result.improved


def test_spacing_between_two_movable_exams_may_not_get_worse_either():
    # B1 and B2 are one (programme, term), four days apart, and both may move.
    # The student's two-exam day ends only if B1 sits on day 3, next to B2.
    board = _board(
        exams=["B1", "B2", "S"],
        current={"B1": 0, "B2": 8, "S": 1},
        fixed=frozenset({"S"}),
        slot_count=10,
        periods_per_day=2,
        closed_slots=frozenset({2, 3, 4, 5, 9}),
        # B2 shares a term with S too, so it can never take B1's day.
        buckets={("P", 1): {"B1", "B2"}, ("P", 2): {"B2", "S"}},
        adj={"B1": {"S": 1}, "S": {"B1": 1}},
        sittings={1: [("B1", 3), ("S", 3)]},
    )
    result = _optimise(board)
    assert not result.improved
    assert result.placements == {"B1": 0, "B2": 8, "S": 1}


def test_a_worse_board_from_a_solve_is_never_kept(monkeypatch):
    # Whatever a solve hands back, only a strictly better board replaces the
    # best so far - at every round, not only at the end.
    board = _two_exams_one_day()
    monkeypatch.setattr(exam_optimise._Model, "board_of", lambda self, solver: {"X": 0})
    search = exam_optimise._Search(board, is_heavy_credit_day, seed=0)
    search.improve(["X", "Y"])
    assert search.best == {"X": 0, "Y": 1}
    assert not _optimise(board).improved


def test_moves_are_counted_from_the_board_given_not_from_the_last_board_found():
    # X drifted to slot 2 for no gain; the next solve of X brings it home.
    board = _board(exams=["X"], current={"X": 0}, sittings={})
    for drifted in range(1, 6):
        search = exam_optimise._Search(board, is_heavy_credit_day, seed=0)
        search.best = {"X": drifted}
        search.improve(["X"])
        assert search.best == {"X": 0}, drifted


def test_seating_the_one_exam_is_fewer_moves_than_swapping_two():
    # Z is in OVERFLOW and the one free slot suits it. Unseating Y to seat Z
    # there would score the same (weights equal) and move two exams, not one.
    board = _board(
        exams=["F", "Y", "Z"],
        current={"F": 0, "Y": 1},
        fixed=frozenset({"F"}),
        slot_count=3,
        periods_per_day=3,
    )
    result = _optimise(board)
    assert result.placements == {"F": 0, "Y": 1, "Z": 2}
    assert result.moved == ["Z"]


def test_the_spacing_cap_counts_the_exams_a_solve_leaves_alone():
    # C and D, a day apart, already use the whole spacing allowance. Freeing A
    # alone, the one way to spare its student is to sit A a day from B - and
    # that would push the board's spacing past what it was given with.
    board = _board(
        exams=["A", "B", "C", "D", "S"],
        current={"A": 0, "S": 1, "C": 4, "D": 6, "B": 10},
        slot_count=12,
        periods_per_day=2,
        closed_slots=frozenset({2, 3, 5, 7, 11}),
        buckets={("P", 1): {"A", "B"}, ("P", 2): {"C", "D"}},
        adj={"A": {"S": 1}, "S": {"A": 1}},
        sittings={1: [("A", 3), ("S", 3)]},
    )
    search = exam_optimise._Search(board, is_heavy_credit_day, seed=0)
    assert search.spacing_cap == spacing_penalty(1)
    search.improve(["A"])
    assert search.best == dict(board.current)
