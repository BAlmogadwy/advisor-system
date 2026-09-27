"""Fix with fewest moves repairs a linked exam as one exam that weighs its courses.

The repair sees each link as one exam. Moving it moves every member, so on
every level of the objective - exams unseated, exams moved, distance moved - it
costs as many courses as it has. The floor behind "the fewest possible" is
weighted the same way, and is brute-forced here on small boards.
"""

import itertools
import random

import pytest

from core.services.exam_min_change import _move_lower_bound, find_violations, repair_minimum_change

PERIODS = 2


def _clash(*pairs):
    adj: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        adj.setdefault(a, {})[b] = 1
        adj.setdefault(b, {})[a] = 1
    return adj


def _repair(placements, adj, *, weights=None, protected=None, buckets=None, slots=4, periods=2):
    return repair_minimum_change(
        placements=placements,
        adj=adj,
        slot_count=slots,
        periods_per_day=periods,
        plan_term_buckets=buckets,
        protected=protected,
        weights=weights,
    )


# ── the three weighted levels ───────────────────────────────────────────────


def test_a_single_exam_steps_aside_rather_than_a_three_course_link():
    """Both could move one period. Unweighted, the tie-break keeps S (earlier
    in code order) and moves U; moving U really moves three courses."""
    placements, adj = {"S": 0, "U": 0}, _clash(("S", "U"))
    assert _repair(placements, adj).moved == ["U"]
    result = _repair(placements, adj, weights={"U": 3})
    assert result.moved == ["S"]
    assert result.proven_minimal is True


def test_a_single_exam_leaves_the_board_rather_than_a_link():
    """One slot for two exams that clash: one must go to OVERFLOW."""
    placements, adj = {"S": 0, "U": 0}, _clash(("S", "U"))
    assert _repair(placements, adj, slots=1, periods=1).unseated == ["U"]
    assert _repair(placements, adj, slots=1, periods=1, weights={"U": 2}).unseated == ["S"]


def test_distance_is_weighed_too():
    """U (two courses) clashes with S1 and S2 at slot 3. Moving U costs two
    courses a move, and so does moving S1 and S2 - a tie. Distance decides:
    U's nearest legal slot is two away (F2 and F4 hold 2 and 4), so moving it
    carries four course-periods against the singles' two.

    Unweighted, moving U is one move against two. With the move level weighted
    but not distance, the tie-break would take U, earliest in code order, to
    slot 1."""
    adj = _clash(("A", "S1"), ("A", "S2"), ("A", "F2"), ("A", "F4"))
    placements = {"A": 3, "S1": 3, "S2": 3, "F2": 2, "F4": 4}
    frozen = {"F2", "F4"}
    assert _repair(placements, adj, protected=frozen, slots=6, periods=6).moved == ["A"]
    result = _repair(placements, adj, protected=frozen, slots=6, periods=6, weights={"A": 2})
    assert result.moved == ["S1", "S2"]
    assert {result.placements["S1"], result.placements["S2"]} <= {2, 4}


def test_a_weighted_repair_claims_the_fewest_possible_only_in_courses():
    placements, adj = {"S": 0, "U": 0}, _clash(("S", "U"))
    result = _repair(placements, adj, weights={"U": 3}, protected={"S"})
    assert result.moved == ["U"]
    assert result.proven_minimal is True, "Three course moves, and three is the floor"
    assert _move_lower_bound({"S"}, placements, adj, None, PERIODS, {"U": 3}) == 3


@pytest.mark.parametrize(
    ("same_day", "protected", "weights", "floor"),
    [
        (["A", "B"], set(), {"A": 3}, 1),
        (["A", "B"], set(), {"A": 3, "B": 2}, 2),
        (["A", "B", "C"], set(), {"A": 3, "B": 2}, 3),
        (["A", "B", "C"], {"C"}, {"A": 3, "B": 2}, 5),
    ],
    ids=["keep-the-heavy", "keep-the-heavier", "all-but-the-heaviest", "frozen-holds-the-day"],
)
def test_a_crowded_day_needs_all_its_free_weight_but_the_heaviest(
    same_day, protected, weights, floor
):
    placements = {code: index % PERIODS for index, code in enumerate(same_day)}
    buckets = {("AI", 1): set(same_day)}
    assert _move_lower_bound(protected, placements, {}, buckets, PERIODS, weights) == floor


def _brute_force(placements, adj, buckets, protected, weights, slot_count):
    """Every legal placement of the free damaged exams, ranked as the repair
    ranks them: weighted moves, weighted distance, then earliest slots in code
    order. Returns the best board and the fewest weighted moves of ANY free
    exams (the true minimum the floor may never exceed)."""
    damaged, _ = find_violations(placements, adj, buckets, PERIODS)
    scope = sorted(damaged - protected)
    free = sorted(set(placements) - protected)
    weight = lambda code: weights.get(code, 1)  # noqa: E731
    best = None
    for slots in itertools.product(range(slot_count), repeat=len(scope)):
        board = {**placements, **dict(zip(scope, slots, strict=True))}
        if find_violations(board, adj, buckets, PERIODS)[1]:
            continue
        rank = (
            sum(weight(code) for code in scope if board[code] != placements[code]),
            sum(weight(code) * abs(board[code] - placements[code]) for code in scope),
            slots,
        )
        if best is None or rank < best[0]:
            best = (rank, board)
    fewest = None
    for slots in itertools.product(range(slot_count), repeat=len(free)):
        board = {**placements, **dict(zip(free, slots, strict=True))}
        if not find_violations(board, adj, buckets, PERIODS)[1]:
            moved = sum(weight(code) for code in free if board[code] != placements[code])
            fewest = moved if fewest is None else min(fewest, moved)
    return (best and best[1]), fewest


def test_the_weighted_repair_and_its_floor_match_brute_force():
    """On small weighted boards: the floor never exceeds the true minimum, a
    repair claimed minimal is the true minimum, and the board chosen is exactly
    the one the repair's own ranking picks by brute force."""
    rng = random.Random(20260927)
    names = ["A", "B", "C", "D", "E"]
    compared = claimed = 0
    while compared < 80:
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.35])
        buckets = {("AI", 1): set(rng.sample(names, 3))} if rng.random() < 0.5 else None
        placements = {name: rng.randrange(4) for name in names}
        protected = set(rng.sample(names, rng.randrange(3)))
        weights = {name: rng.choice([1, 1, 2, 3]) for name in names}
        damaged, breaches = find_violations(placements, adj, buckets, PERIODS)
        if not breaches or not 1 <= len(damaged - protected) <= 3:
            continue
        expected, fewest = _brute_force(placements, adj, buckets, protected, weights, 4)
        if expected is None or fewest is None:
            continue
        floor = _move_lower_bound(protected, placements, adj, buckets, PERIODS, weights)
        case = (placements, sorted(adj.items()), buckets, protected, weights)
        assert floor <= fewest, case
        result = _repair(placements, adj, weights=weights, protected=protected, buckets=buckets)
        assert result.widened is False, case
        assert result.placements == expected, case
        if result.proven_minimal:
            claimed += 1
            assert sum(weights[code] for code in result.moved) == fewest, case
        compared += 1
    assert claimed >= 20, "Too few boards exercised the claim"
