"""Fix with fewest moves repairs a linked exam as one exam that weighs its courses.

The repair sees each link as one exam. Moving it moves every member, so on
every level of the objective - exams unseated, exams moved, distance moved - it
costs as many courses as it has. The floor behind "the fewest possible" is
weighted the same way, and is brute-forced here on small boards.
"""

import itertools
import random

import pytest

from core import exam_views
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


# ── the view: collapse, repair, expand ──────────────────────────────────────

DAYS = ["Sun", "Mon"]
PERIOD_NAMES = ["08:00-10:00", "13:00-15:00"]


def _entry(code, slot):
    if slot is None:
        return {
            "course_code": code,
            "course_identity": code,
            "course_name": f"{code} name",
            "day": "OVERFLOW",
            "period": "Extra-4",
            "slot_index": 4,
        }
    return {
        "course_code": code,
        "course_identity": code,
        "course_name": f"{code} name",
        "day": DAYS[slot // 2],
        "period": PERIOD_NAMES[slot % 2],
        "slot_index": slot,
    }


def _where(slot):
    return (DAYS[slot // 2], PERIOD_NAMES[slot % 2])


def _link(*codes):
    return {"members": [{"course_identity": code} for code in codes]}


def _fix(monkeypatch, board, adj, *, links, source=None, pinned=None, carried=None, enrolled=None):
    """Drive the view's Fix path with a controlled graph, as test_exam_min_change does."""
    captured = {}

    def inputs(base_entries, days, periods, *_args, **_kwargs):
        return exam_views._LoadedSolverInputs(
            meta_by_course={entry["course_code"]: entry for entry in base_entries},
            course_list=sorted(entry["course_code"] for entry in base_entries),
            enrolled_sets=enrolled or {},
            adj=adj,
            plan_term_buckets={},
            course_buckets={},
            credit_map={},
            slots=[
                {"index": index, "day": day, "period": period}
                for index, (day, period) in enumerate(itertools.product(days, periods))
            ],
        )

    def rebuild(**kwargs):
        captured.update(kwargs)
        return {"schedule": kwargs["schedule_raw"], **(kwargs.get("extra") or {})}

    monkeypatch.setattr(exam_views, "_loaded_solver_inputs", inputs)
    monkeypatch.setattr(exam_views, "_rebuild_loaded_schedule", rebuild)
    source = source or {entry["course_code"]: (entry["day"], entry["period"]) for entry in board}
    result = exam_views._minimum_change_schedule(
        label="Fix",
        days=DAYS,
        periods=PERIOD_NAMES,
        max_per_day=2,
        schedule_raw=board,
        selected_courses=[entry["course_code"] for entry in board],
        pinned=pinned or [],
        assign_rooms=False,
        seed=None,
        thin_conflict_threshold=0,
        source_placements=source,
        carried_protection=carried,
        linked_exams=links,
    )
    return result, captured


def _placements(entries):
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in entries}


def test_a_link_moves_whole_and_every_member_is_reported(monkeypatch):
    """X was dragged onto the link's slot, so X is the registrar's; the link
    steps aside, and the report names both of its courses."""
    board = [_entry("A", 0), _entry("B", 0), _entry("X", 0)]
    result, captured = _fix(
        monkeypatch,
        board,
        _clash(("B", "X")),
        links=[_link("A", "B")],
        source={"A": _where(0), "B": _where(0), "X": _where(3)},
    )
    after = _placements(captured["schedule_raw"])
    assert after["A"] == after["B"] != after["X"] == _where(0)
    moves = result["minimum_change"]["moves"]
    assert [move["course_code"] for move in moves] == ["A", "B"]
    assert {(move["from"]["day"], move["to"]["day"]) for move in moves} == {("Sun", "Sun")}
    assert captured["linked_exams"] == [
        {
            "members": [
                {"course_identity": "A", "course_code": "A"},
                {"course_identity": "B", "course_code": "B"},
            ]
        }
    ]


def test_a_link_is_the_registrars_if_any_member_is(monkeypatch):
    """B was moved by hand with its partner: the link is protected, X moves."""
    board = [_entry("A", 1), _entry("B", 1), _entry("X", 1)]
    result, captured = _fix(
        monkeypatch,
        board,
        _clash(("A", "X")),
        links=[_link("A", "B")],
        source={"A": _where(1), "B": _where(2), "X": _where(1)},
    )
    after = _placements(captured["schedule_raw"])
    assert after["A"] == after["B"] == _where(1)
    assert [move["course_code"] for move in result["minimum_change"]["moves"]] == ["X"]
    assert captured["extra"]["minimum_change_protected"] == ["B"], "Real courses, as before"


def test_a_pinned_member_freezes_its_link(monkeypatch):
    board = [_entry("A", 1), _entry("B", 1), _entry("X", 1)]
    _result, captured = _fix(
        monkeypatch,
        board,
        _clash(("A", "X")),
        links=[_link("A", "B")],
        pinned=[{"course_code": "B", "day": "Sun", "period": PERIOD_NAMES[1]}],
    )
    after = _placements(captured["schedule_raw"])
    assert after["A"] == after["B"] == _where(1) != after["X"]


def test_an_unseated_link_leaves_the_board_under_one_extra(monkeypatch):
    """Every slot is held by a frozen exam the link clashes with."""
    frozen = ["F0", "F1", "F2", "F3"]
    board = [_entry(code, slot) for slot, code in enumerate(frozen)]
    board += [_entry("A", 0), _entry("B", 0), _entry("K", None)]
    adj = _clash(*[(member, code) for member in ("A", "B") for code in frozen])
    result, captured = _fix(monkeypatch, board, adj, links=[_link("A", "B")], carried=frozen)
    overflow = {
        entry["course_code"]: (entry["period"], entry["slot_index"])
        for entry in captured["schedule_raw"]
        if entry["day"] == "OVERFLOW"
    }
    assert overflow == {"A": ("Extra-5", 5), "B": ("Extra-5", 5), "K": ("Extra-4", 4)}
    assert result["minimum_change"]["unseated"] == ["A", "B"]


def test_students_in_two_linked_courses_are_reported_not_repaired(monkeypatch):
    """Their clash is real, but separating linked courses is not a repair."""
    board = [_entry("A", 0), _entry("B", 0)]
    result, captured = _fix(
        monkeypatch,
        board,
        _clash(("A", "B")),
        links=[_link("A", "B")],
        enrolled={"A": {1, 2}, "B": {2, 3}},
    )
    assert result == {
        "saved": False,
        "minimum_change": {
            "moves": [],
            "unseated": [],
            "already_overflow": 0,
            "violations_before": 0,
            "violations_after": 0,
            "protected_count": 0,
            "widened": False,
            "proven_minimal": True,
            "status": "OPTIMAL",
            "linked_clash_students": 1,
        },
    }
    assert captured == {}, "Nothing to save"


def test_a_split_link_is_refused_before_anything_moves(monkeypatch):
    board = [_entry("A", 0), _entry("B", 1)]
    with pytest.raises(ValueError, match="same day and period"):
        _fix(monkeypatch, board, {}, links=[_link("A", "B")])


@pytest.mark.parametrize("seed", range(40))
def test_no_fix_splits_a_link(monkeypatch, seed):
    """Random edited boards with links, pins, frozen exams and crowded slots."""
    rng = random.Random(6100 + seed)
    names = [f"N{index}" for index in range(rng.randint(5, 9))]
    shuffled = rng.sample(names, len(names))
    groups = [tuple(shuffled[0:2]), tuple(shuffled[2 : 2 + rng.randint(2, 3)])]
    adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.5])
    board, source = [], {}
    for group in groups:
        slot = None if rng.random() < 0.1 else rng.randrange(4)
        for code in group:
            board.append(_entry(code, slot))
    for code in names:
        if not any(code in group for group in groups):
            board.append(_entry(code, None if rng.random() < 0.1 else rng.randrange(4)))
    for entry in board:
        moved_by_hand = rng.random() < 0.2
        source[entry["course_code"]] = (
            _where(rng.randrange(4)) if moved_by_hand else (entry["day"], entry["period"])
        )
    placed = [entry for entry in board if entry["day"] != "OVERFLOW"]
    pinned = []
    if placed and rng.random() < 0.4:
        anchor = rng.choice(placed)
        pinned = [
            {"course_code": entry["course_code"], "day": entry["day"], "period": entry["period"]}
            for entry in placed
            if entry["course_code"] == anchor["course_code"]
        ]
    result, captured = _fix(
        monkeypatch,
        board,
        adj,
        links=[_link(*group) for group in groups],
        source=source,
        pinned=pinned,
        carried=rng.sample(names, rng.randint(0, 2)),
    )
    after = captured.get("schedule_raw", board)
    for group in groups:
        where = {
            ("OVERFLOW",) if e["day"] == "OVERFLOW" else (e["day"], e["period"])
            for e in after
            if e["course_code"] in group
        }
        assert len(where) == 1, (seed, group, after)
        slots = {e["slot_index"] for e in after if e["course_code"] in group}
        assert len(slots) == 1, "One shared Extra-n, or one real slot"
    frozen = {pin["course_code"] for pin in pinned}
    for entry in after:
        if entry["course_code"] in frozen:
            assert (entry["day"], entry["period"]) == (pinned[0]["day"], pinned[0]["period"])
    reported = {move["course_code"] for move in result["minimum_change"]["moves"]}
    for group in groups:
        assert not set(group) & reported or set(group) <= reported, "A link is reported whole"
