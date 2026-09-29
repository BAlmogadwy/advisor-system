"""Adding courses to a saved board: the engine, on boards built by hand.

``place_added_exams`` seats the new exams Build's greedy could not, moving as
few existing exams as it can. Every result is checked against the invariants
the owner was promised, and a brute force over tiny boards checks that "the
fewest possible" is never claimed falsely. Solver output is never pinned as a
golden: CP-SAT may break ties differently on another platform.
"""

from __future__ import annotations

import itertools
import random

import pytest

from core.services import exam_min_change as engine
from core.services.exam_add_courses import (
    SeatLedger,
    day_cost_function,
    explain_unplaced,
)
from core.services.exam_min_change import (
    _added_move_floor,
    _bucket_mates,
    _build_model,
    _legal_at,
    find_violations,
    place_added_exams,
)

SLOTS = 6
PER_DAY = 2  # three days of two periods: slots 0-1, 2-3, 4-5


def _clash(*pairs):
    adj: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        adj.setdefault(a, {})[b] = 1
        adj.setdefault(b, {})[a] = 1
    return adj


def _add(placements, new, adj, *, greedy=None, buckets=None, protected=None, **kwargs):
    kwargs.setdefault("slot_count", SLOTS)
    kwargs.setdefault("periods_per_day", PER_DAY)
    return place_added_exams(
        placements=dict(placements),
        new_units=list(new),
        greedy=dict(greedy or {}),
        adj=adj,
        plan_term_buckets=buckets,
        protected=set(protected or ()),
        **kwargs,
    )


def _assert_invariants(
    result, placements, new, adj, *, buckets=None, protected=(), closed=(), ppd=PER_DAY
):
    """What every Add promises, whatever the solver chose."""
    board = result.placements
    # Existing exams are never unplaced, and only unprotected ones ever move.
    assert set(placements) <= set(board)
    for course in protected:
        if course in placements:
            assert board[course] == placements[course], course
    assert set(result.moved) == {c for c in placements if board[c] != placements[c]}
    assert not set(result.moved) & set(protected)
    assert not set(result.moved) & set(new)
    # New exams are placed or listed, never both.
    assert set(result.placed_new) | set(result.unplaced_new) == set(new)
    assert not set(result.placed_new) & set(result.unplaced_new)
    assert set(result.placed_new) == {c for c in new if c in board}
    # No new or moved exam in a closed slot.
    for course in [*result.placed_new, *result.moved]:
        assert board[course] not in set(closed), course
    # The board breaks no rule the saved board did not already break.
    _, before = find_violations(dict(placements), adj, buckets, ppd)
    _, after = find_violations(board, adj, buckets, ppd)
    assert after == before


# ── the common case: room on the board ───────────────────────────────────────


def test_a_free_slot_means_nothing_moves_and_that_is_proven():
    placements = {"A": 0, "B": 1, "C": 2}
    adj = _clash(("N", "A"), ("N", "B"))
    result = _add(placements, ["N"], adj)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.placed_new == ["N"] and result.unplaced_new == []
    assert result.moved == []
    assert {c: result.placements[c] for c in placements} == placements
    assert result.proven_minimal and result.status == "OPTIMAL"


def test_what_the_greedy_placed_is_kept_without_any_solve(monkeypatch):
    def no_solver(*args, **kwargs):  # pragma: no cover - reached only by a regression
        raise AssertionError("no new exam was left unplaced: nothing to solve")

    monkeypatch.setattr(engine, "_build_model", no_solver)
    placements = {"A": 0, "B": 1}
    result = _add(placements, ["N", "M"], {}, greedy={"N": 3, "M": 5})
    assert result.placements == {"A": 0, "B": 1, "N": 3, "M": 5}
    assert result.proven_minimal and result.attempts == 0


def test_new_exams_that_clash_with_each_other_both_find_slots_without_moves():
    placements = {"A": 0}
    adj = _clash(("N", "M"), ("N", "A"), ("M", "A"))
    result = _add(placements, ["M", "N"], adj)
    _assert_invariants(result, placements, ["M", "N"], adj)
    assert result.unplaced_new == [] and result.moved == []
    assert result.placements["N"] != result.placements["M"]


def test_the_greedy_order_is_repaired_among_new_exams_before_anything_existing_moves():
    # The greedy put M in the only slot N can use; attempt 0 swaps the new
    # exams alone, so no existing exam moves.
    placements = {"A": 0, "B": 1, "C": 2, "D": 3}
    adj = _clash(*[("N", x) for x in "ABCD"], ("N", "M"))
    result = _add(placements, ["M", "N"], adj, greedy={"M": 4}, slot_count=6)
    _assert_invariants(result, placements, ["M", "N"], adj)
    assert result.unplaced_new == [] and result.moved == []


# ── making room ──────────────────────────────────────────────────────────────


def test_an_exam_moves_only_when_that_is_the_one_way_to_seat_the_new_one():
    # N clashes with every exam, one in each slot: no slot is free. One exam
    # sharing another slot (any but E and Z, which clash) frees one.
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    result = _add(placements, ["N"], adj)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == []
    assert len(result.moved) == 1
    assert result.proven_minimal and result.status == "OPTIMAL"
    assert result.widened and not result.search_stopped


def test_a_new_exam_whose_room_is_made_far_from_it_is_still_placed():
    """Room made two steps away from the unplaced exam (reviewer's board 2836).

    N1 can sit only where E0, E1 and E2 are; N0 needs E1's slot. Seating both
    moves E0, E1 and E2 - E2 is no neighbour of N0 and was never offered to
    move by the rings of neighbours this search replaced: they left N0 out and
    called the board final.
    """
    placements = {"E0": 0, "E1": 0, "E2": 0, "E3": 2, "E4": 2, "E5": 3, "E6": 3, "E7": 2}
    adj = _clash(
        ("E0", "E5"),
        ("E0", "E6"),
        ("E0", "N0"),
        ("E0", "N1"),
        ("E1", "E4"),
        ("E1", "N0"),
        ("E1", "N1"),
        ("E2", "E5"),
        ("E2", "N1"),
        ("E3", "N1"),
        ("E4", "E6"),
        ("E4", "N0"),
        ("E4", "N1"),
        ("E5", "E7"),
        ("E7", "N1"),
    )
    buckets = {("P", 0): {"E3", "N0"}}
    protected = {"E3", "E4", "E5", "E7"}
    closed = frozenset({1})
    weights = {"E3": 3, "E4": 2, "E5": 2}
    result = _add(
        placements,
        ["N0", "N1"],
        adj,
        buckets=buckets,
        protected=protected,
        closed_slots=closed,
        weights=weights,
        slot_count=4,
    )
    _assert_invariants(
        result, placements, ["N0", "N1"], adj, buckets=buckets, protected=protected, closed=closed
    )
    assert result.unplaced_new == []
    assert sorted(result.moved) == ["E0", "E1", "E2"]
    assert result.proven_minimal and not result.search_stopped


def test_a_placing_solve_that_stops_unproven_is_not_the_answer(monkeypatch):
    """Every solve at one solve's usual limit stops with nothing proven.

    Attempt 0's word is not final: the solve over every exam that may move
    gets the rest of the work for placing - more than the usual limit - and
    seats the exam; the board is proven by the floor and nothing says the
    search stopped.
    """
    real = engine._solve

    def starved(model, objective, budget, *, cap=None, keep=0.0):
        if cap is None:
            return engine.cp_model.UNKNOWN, None
        return real(model, objective, budget, cap=cap, keep=keep)

    monkeypatch.setattr(engine, "_solve", starved)
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    result = _add(placements, ["N"], adj)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == [] and len(result.moved) == 1
    assert result.widened and result.proven_minimal and not result.search_stopped


def test_a_new_exam_left_out_by_a_stopped_search_says_the_search_stopped(monkeypatch):
    """The wide solve itself stops unproven: the exam is out, and not called impossible."""
    real = engine._solve

    def stopped(model, objective, budget, *, cap=None, keep=0.0):
        if cap is not None and cap > engine._DETERMINISTIC_LIMIT:
            return engine.cp_model.UNKNOWN, None
        return real(model, objective, budget, cap=cap, keep=keep)

    monkeypatch.setattr(engine, "_solve", stopped)
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    result = _add(placements, ["N"], adj)
    assert result.unplaced_new == ["N"] and result.moved == []
    assert result.widened and result.search_stopped and not result.proven_minimal


@pytest.mark.parametrize("work", [12.0, 32.0])
def test_when_every_solve_needs_all_its_work_the_wide_solve_splits_it_as_promised(
    monkeypatch, work
):
    """A tight board, where every solve spends all the work it is allowed.

    Every board the other tests use is solved long before its limit, so how
    the work is shared never shows there. Here each solve is charged its whole
    allowance, as on the tight boards built from real scopes, and the wide
    solve must share it as its docstring says:

    - placing gets at least half of what the lower levels leave, and never
      less than one solve's usual limit;
    - the moves get everything but the reserve - more than one solve's usual
      limit. Held to that limit they moved 13 -> 19 and 31 -> 44 exams on two
      of those boards, placing as many;
    - the reserve is left for the levels below (seats, the day limit, the
      load). Moves that ate it left the day limit worse (128 -> 133 and
      172 -> 202 students over it), or never reached it.
    """
    models: dict[int, tuple[object, frozenset[str]]] = {}
    real_build = engine._build_model

    def build(scope, *args, **kwargs):
        built = real_build(scope, *args, **kwargs)
        # The model is kept alive, so no later model can take its id.
        models[id(built.model)] = (built.model, frozenset(scope))
        return built

    real_solve = engine._solve
    calls: list[tuple[frozenset[str], float, float]] = []

    def takes_all_it_is_given(model, objective, budget, *, cap=None, keep=0.0):
        before = budget.remaining
        allowed = budget.limit(cap, keep)
        status, solver = real_solve(model, objective, budget, cap=cap, keep=keep)
        if solver is not None:
            budget.spend(max(0.0, allowed - solver.deterministic_time))
        calls.append((models.get(id(model), (None, frozenset()))[1], before, allowed))
        return status, solver

    monkeypatch.setattr(engine, "_build_model", build)
    monkeypatch.setattr(engine, "_solve", takes_all_it_is_given)
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    result = _add(
        placements,
        ["N"],
        adj,
        work_budget=work,
        # One level below the moves: students over the daily limit on day one.
        day_cost=lambda course, day: 1 if day == 0 else 0,
    )
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == [] and len(result.moved) == 1
    assert result.widened

    reserve = engine._ADD_LOWER_RESERVE
    one_solve = engine._DETERMINISTIC_LIMIT
    # The wide solve's model holds the existing exams: placing, the moves,
    # how far, then the levels below, in that order.
    wide = [(before, allowed) for scope, before, allowed in calls if "A" in scope]
    assert len(wide) >= 4, calls
    (at_placing, placing), (at_moves, moves), (at_distance, _), (_, below) = wide[:4]
    spare = at_placing - reserve
    assert spare / 2 > one_solve, "enough work for the split to matter"
    assert placing >= spare / 2 - 1e-9
    assert at_placing - placing >= reserve - 1e-9
    assert moves > one_solve
    assert moves == pytest.approx(at_moves - reserve)
    assert at_distance >= reserve - 1e-9
    assert below > 0, "the reserve reached the level below the moves"


def test_a_protected_blocker_never_moves_even_when_it_is_the_cheapest():
    # "ZZ" sorts last on purpose: a protection that held only by code order
    # would pass with a first-named exam.
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "ZZ": 5}
    adj = _clash(*[("N", x) for x in ["A", "B", "C", "D", "E", "ZZ"]], ("A", "B"))
    result = _add(placements, ["N"], adj, protected={"ZZ"})
    _assert_invariants(result, placements, ["N"], adj, protected={"ZZ"})
    assert result.placements["ZZ"] == 5
    assert "ZZ" not in result.moved


def test_when_every_blocker_is_protected_the_new_exam_stays_out_with_that_reason():
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
    adj = _clash(*[("N", x) for x in "ABCDEF"])
    protected = set("ABCDEF")
    result = _add(placements, ["N"], adj, protected=protected)
    _assert_invariants(result, placements, ["N"], adj, protected=protected)
    assert result.unplaced_new == ["N"] and result.moved == []
    # Nothing may move, so attempt 0 was the whole question - and it is proven.
    assert result.proven_minimal and not result.search_stopped
    why = explain_unplaced(
        "N",
        board=result.placements,
        adj=adj,
        plan_term_buckets=None,
        protected=protected,
        closed_slots=frozenset(),
        slot_count=SLOTS,
        periods_per_day=PER_DAY,
        search_stopped=result.search_stopped,
        members_of=lambda unit: (unit,),
    )
    assert why["reason"] == "blocked_by_fixed_exams"
    assert why["blocked_by"]["fixed"] == ["A", "B", "C", "D", "E"]


def test_no_existing_exam_is_ever_sent_to_overflow_to_seat_a_new_one():
    # Seating N would need B out of the timetable altogether: never.
    placements = {"A": 0, "B": 1}
    adj = _clash(("N", "A"), ("N", "B"), ("A", "B"))
    result = _add(placements, ["N"], adj, slot_count=2, periods_per_day=2)
    assert set(result.placements) >= {"A", "B"}
    assert result.unplaced_new == ["N"]


# ── locked cells, links and study-plan days ──────────────────────────────────


def test_a_locked_cell_is_never_taken_and_the_reason_says_so():
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    adj = _clash(*[("N", x) for x in "ABCDE"])
    closed = frozenset({5})
    protected = set("ABCDE")
    result = _add(placements, ["N"], adj, closed_slots=closed, protected=protected)
    _assert_invariants(result, placements, ["N"], adj, protected=protected, closed=closed)
    assert result.unplaced_new == ["N"]
    why = explain_unplaced(
        "N",
        board=result.placements,
        adj=adj,
        plan_term_buckets=None,
        protected=protected,
        closed_slots=closed,
        slot_count=SLOTS,
        periods_per_day=PER_DAY,
        search_stopped=False,
        members_of=lambda unit: (unit,),
    )
    assert why["reason"] == "only_locked_periods_free"
    assert why["blocked_by"]["locked_slots"] == 1


def test_a_linked_exam_moves_whole_and_weighs_its_courses():
    # "A+B" is a link of two courses: moving it costs 2, so the solver moves
    # the single exam C instead when either would do.
    placements = {"A+B": 0, "C": 1, "D": 2, "E": 3, "F": 4, "G": 5}
    adj = _clash(*[("N", x) for x in ["A+B", "C", "D", "E", "F", "G"]], ("D", "E"))
    result = _add(placements, ["N"], adj, weights={"A+B": 2})
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == []
    assert result.moved and "A+B" not in result.moved


def test_a_link_is_moved_when_it_is_the_only_exam_that_can_step_aside():
    placements = {"A+B": 0, "C": 1, "D": 2, "E": 3, "F": 4, "G": 5}
    adj = _clash(*[("N", x) for x in ["A+B", "C", "D", "E", "F", "G"]])
    protected = {"C", "D", "E", "F", "G"}
    result = _add(placements, ["N"], adj, weights={"A+B": 2}, protected=protected)
    _assert_invariants(result, placements, ["N"], adj, protected=protected)
    assert result.moved == ["A+B"]
    assert result.placements["N"] == 0
    assert result.proven_minimal


def test_a_new_exam_never_lands_on_a_day_its_study_plan_mate_holds():
    placements = {"A": 0, "B": 2}
    buckets = {("CS", 3): {"A", "B", "N"}}
    result = _add(placements, ["N"], {}, buckets=buckets)
    _assert_invariants(result, placements, ["N"], {}, buckets=buckets)
    assert result.placements["N"] in {4, 5}


def test_a_study_term_with_an_exam_on_every_day_is_named_as_the_reason():
    placements = {"A": 0, "B": 2, "C": 4}
    buckets = {("CS", 3): {"A", "B", "C", "N"}}
    protected = {"A", "B", "C"}
    result = _add(placements, ["N"], {}, buckets=buckets, protected=protected)
    assert result.unplaced_new == ["N"]
    why = explain_unplaced(
        "N",
        board=result.placements,
        adj={},
        plan_term_buckets=buckets,
        protected=protected,
        closed_slots=frozenset(),
        slot_count=SLOTS,
        periods_per_day=PER_DAY,
        search_stopped=False,
        members_of=lambda unit: (unit,),
    )
    assert why["reason"] == "bucket_days_full"
    assert why["blocked_by"]["bucket"] == [
        {"program": "CS", "programme_term": 3, "courses": ["A", "B", "C"]}
    ]


def _why(unit, board, *, adj=None, buckets=None, protected=(), closed=(), stopped=False):
    return explain_unplaced(
        unit,
        board=board,
        adj=adj or {},
        plan_term_buckets=buckets,
        protected=set(protected),
        closed_slots=frozenset(closed),
        slot_count=SLOTS,
        periods_per_day=PER_DAY,
        search_stopped=stopped,
        members_of=lambda unit: (unit,),
    )


def test_two_study_terms_filling_the_days_only_together_are_not_called_full():
    # AI term 1 holds days 0 and 1, CS term 2 holds day 2: neither term alone
    # has an exam on every day, so either could still give one up.
    board = {"A": 0, "B": 2, "C": 4}
    buckets = {("AI", 1): {"A", "B", "N"}, ("CS", 2): {"C", "N"}}
    assert _why("N", board, buckets=buckets)["reason"] == "blocked_by_clashes"
    assert _why("N", board, buckets=buckets, stopped=True)["reason"] == "search_limit"


def test_the_study_term_that_is_full_is_named_first_and_is_a_proof():
    board = {"A": 0, "B": 2, "C": 4, "D": 1}
    buckets = {("AI", 1): {"D", "N"}, ("CS", 2): {"A", "B", "C", "N"}}
    # One term with an exam on every day: no search could change that.
    why = _why("N", board, buckets=buckets, stopped=True)
    assert why["reason"] == "bucket_days_full"
    assert [bucket["program"] for bucket in why["blocked_by"]["bucket"]] == ["CS", "AI"]


def test_a_stopped_search_is_never_presented_as_an_impossibility():
    # N clashes with A to E in slots 0-4, which could move; slot 5 is locked.
    board = {course: slot for slot, course in enumerate("ABCDE")}
    adj = _clash(*[("N", x) for x in "ABCDE"])
    assert _why("N", board, adj=adj, closed={5}, stopped=True)["reason"] == "search_limit"
    assert _why("N", board, adj=adj, closed={5})["reason"] == "only_locked_periods_free"
    # Every blocker fixed: that is a proof, and the locked period the one way in.
    fixed = set("ABCDE")
    assert (
        _why("N", board, adj=adj, closed={5}, protected=fixed, stopped=True)["reason"]
        == "only_locked_periods_free"
    )
    assert _why("N", board, adj=adj, protected=fixed, stopped=True)["reason"] == (
        "search_limit"  # slot 5 is open and free: the search stopped short of it
    )


def test_a_study_plan_mate_steps_to_another_day_to_make_room():
    # N's term already has A on day 0 and B on day 1; day 2 is full of
    # exams N clashes with. One move - either - makes room.
    placements = {"A": 0, "B": 2, "X": 4, "Y": 5}
    buckets = {("CS", 3): {"A", "B", "N"}}
    adj = _clash(("N", "X"), ("N", "Y"))
    result = _add(placements, ["N"], adj, buckets=buckets)
    _assert_invariants(result, placements, ["N"], adj, buckets=buckets)
    assert result.unplaced_new == []
    assert len(result.moved) == 1


# ── what is chosen among legal slots ─────────────────────────────────────────


def test_the_day_cap_level_picks_the_day_with_fewest_students_over_the_limit():
    placements = {"A": 0, "B": 1}
    students = {"A": {1, 2}, "B": {1, 2}, "N": {1, 2}}
    board = dict(placements)
    cost = day_cost_function(board, students, 2, PER_DAY)
    # Day 0 already has two exams for students 1 and 2.
    assert cost("N", 0) == 2 and cost("N", 1) == 0 and cost("N", 2) == 0
    # The load alone would choose day 0, the emptiest: students come first.
    result = _add(placements, ["N"], {}, day_cost=cost, day_load={0: 0, 1: 5, 2: 5})
    assert result.placements["N"] // PER_DAY != 0
    assert _add(placements, ["N"], {}, day_load={0: 0, 1: 5, 2: 5}).placements["N"] // PER_DAY == 0


def test_a_new_exam_the_greedy_placed_stays_where_the_greedy_put_it():
    # N1 fits anywhere; the sweep alone would pull it to slot 0.
    placements = {"A": 0}
    adj = _clash(("N2", "A"))
    result = _add(placements, ["N1", "N2"], adj, greedy={"N1": 5})
    assert result.placements["N1"] == 5
    assert result.unplaced_new == []


def test_new_exams_without_a_home_are_spread_rather_than_piled_onto_day_one():
    # Four new exams that clash with nothing: the load level spreads them.
    new = ["N1", "N2", "N3", "N4"]
    adj = _clash(("N1", "N2"), ("N3", "N4"), ("N1", "N3"))
    result = _add({}, new, adj, day_load={})
    days = [result.placements[c] // PER_DAY for c in new]
    assert len(set(days)) > 1


def test_a_greedy_seat_is_never_traded_for_another_new_exam():
    # N1 (placed by the greedy) and N2 both want slot 5 only. L1 ties: one of
    # them is out either way. The greedy's own placement stays.
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    adj = _clash(*[(n, x) for n in ("N1", "N2") for x in "ABCDE"], ("N1", "N2"))
    protected = set("ABCDE")
    result = _add(placements, ["N1", "N2"], adj, greedy={"N2": 5}, protected=protected)
    assert result.placements.get("N2") == 5
    assert result.unplaced_new == ["N1"]


def test_a_saved_exam_never_moves_to_keep_the_greedys_choice_of_new_exam():
    """Two of three new exams fit either way: the greedy's N0 and N1 need E1
    moved, N1 and N2 need nothing moved. Placing as many, with no move, wins:
    the greedy's seat for N0 was arbitrary, the registrar's E1 is not.
    """
    placements = {"E0": 0, "E1": 4, "E2": 1, "E3": 1, "E4": 2}
    adj = _clash(
        ("E0", "E1"),
        ("E0", "E3"),
        ("E1", "E3"),
        *[(n, e) for n in ("N0", "N1") for e in ("E0", "E1", "E2", "E3", "E4")],
        *[("N2", e) for e in ("E0", "E2", "E3", "E4")],
        ("N0", "N1"),
        ("N0", "N2"),
        ("N1", "N2"),
    )
    buckets = {("P", 0): {"N0", "N2", "E3"}}
    new = ["N0", "N1", "N2"]
    closed = frozenset({5})
    kwargs = dict(
        buckets=buckets,
        protected={"E0"},
        closed_slots=closed,
        weights={"E3": 3, "E4": 2},
        slot_count=6,
        periods_per_day=3,
    )
    result = _add(placements, new, adj, greedy={"N0": 3}, **kwargs)
    _assert_invariants(
        result, placements, new, adj, buckets=buckets, protected={"E0"}, closed=closed, ppd=3
    )
    assert len(result.placed_new) == 2
    assert result.moved == []
    assert result.proven_minimal
    # Without the greedy's seat the answer is the same board: the seat bought nothing.
    assert _add(placements, new, adj, **kwargs).placements == result.placements


def test_seats_are_counted_so_a_big_course_goes_where_its_cohort_has_room():
    placements = {"A": 0}
    demand = {"A": {"F": 90}, "N": {"F": 40}}
    result = _add(
        placements,
        ["N"],
        {},
        seat_demand=demand,
        seat_capacity={"F": 100, "M": 100},
        work_budget=4.0,
    )
    assert result.placements["N"] != 0


def test_the_greedy_ledger_charges_only_the_seats_a_course_adds_past_capacity():
    ledger = SeatLedger({"A": {"F": 90}, "N": {"F": 40}, "M": {"F": 30}}, {"F": 100}, {"A": 0})
    assert ledger.cost("N", 0) == 30
    assert ledger.cost("N", 1) == 0
    ledger.take("N", 1)
    assert ledger.cost("M", 1) == 0
    ledger.take("M", 1)
    # 70 seated in slot 1 now; 40 more would pass 100 by 10.
    assert ledger.cost("N", 1) == 10


# ── determinism, budget, cancellation ────────────────────────────────────────


def test_the_order_courses_are_asked_for_in_changes_nothing():
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[(n, x) for n in ("N", "M") for x in "ABCDEZ"], ("E", "Z"), ("N", "M"))
    first = _add(placements, ["N", "M"], adj)
    second = _add(placements, ["M", "N"], adj)
    again = _add(placements, ["N", "M"], adj)
    assert first.placements == second.placements == again.placements
    assert first.moved == second.moved


def test_out_of_work_keeps_a_legal_board_and_says_the_search_stopped(monkeypatch):
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    # Below the smallest solve: nothing is ever handed to the solver.
    result = _add(placements, ["N"], adj, work_budget=engine._MIN_SOLVE_WORK / 2)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == ["N"]
    assert result.moved == []
    assert result.status == "UNKNOWN"
    assert result.search_stopped
    why = explain_unplaced(
        "N",
        board=result.placements,
        adj=adj,
        plan_term_buckets=None,
        protected=set(),
        closed_slots=frozenset(),
        slot_count=SLOTS,
        periods_per_day=PER_DAY,
        search_stopped=result.search_stopped,
        members_of=lambda unit: (unit,),
    )
    assert why["reason"] == "search_limit"


def test_a_stopped_solve_never_leaves_an_existing_exam_moved_for_nothing(monkeypatch):
    """A solve stopped after its first level can move exams it did not need to.

    The final pass puts back every existing exam whose home is free again.
    """
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))
    boards = iter(
        [
            {**placements},  # attempt 0: N still out
            {**placements, "A": 1, "C": 3, "N": 0},  # the wide solve: C moved for nothing
        ]
    )

    class Stopped:
        def __init__(self, built, board):
            self.built, self.board = built, board

        def value(self, var):
            for (course, slot), literal in self.built.y.items():
                if literal is var:
                    return int(self.board.get(course) == slot)
            for course, literal in self.built.unseated.items():
                if literal is var:
                    return int(course not in self.board)
            raise AssertionError("unknown variable")

    def stopped(built, levels, budget, **kwargs):
        board = next(boards, None)
        if board is None:
            return engine._Levels(engine.cp_model.UNKNOWN, None, False, False)
        return engine._Levels(engine.cp_model.FEASIBLE, Stopped(built, board), False, False)

    monkeypatch.setattr(engine, "_solve_levels", stopped)
    result = _add(placements, ["N"], adj)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.placements["N"] == 0
    assert result.moved == ["A"]
    assert result.moved_back == 1
    assert result.proven_minimal


def test_a_cancelled_job_stops_between_solves():
    placements = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Z": 5}
    adj = _clash(*[("N", x) for x in "ABCDEZ"], ("E", "Z"))

    class Stop(Exception):
        pass

    def stop():
        raise Stop

    with pytest.raises(Stop):
        _add(placements, ["N"], adj, check_cancelled=stop)


def test_only_the_named_exams_get_an_overflow_literal():
    built = _build_model(
        ["A", "N"], {"A": 0}, {}, None, SLOTS, PER_DAY, allow_unseated=frozenset({"N"})
    )
    assert set(built.unseated) == {"N"}
    assert set(_build_model(["A"], {"A": 0}, {}, None, 2, 2, allow_unseated=True).unseated) == {"A"}
    assert not _build_model(["A"], {"A": 0}, {}, None, 2, 2, allow_unseated=False).unseated


def test_bad_input_is_refused():
    with pytest.raises(ValueError, match="already in the timetable"):
        _add({"N": 0}, ["N"], {})
    with pytest.raises(ValueError, match="greedy"):
        _add({}, ["N"], {}, greedy={"N": 9})
    with pytest.raises(ValueError, match="greedy"):
        _add({}, ["N"], {}, greedy={"N": 1}, closed_slots=frozenset({1}))


# ── the floor behind "the fewest possible" ───────────────────────────────────


def test_a_new_exam_with_a_free_slot_demands_no_move():
    assert _added_move_floor(["N"], {"A": 0}, _clash(("N", "A")), {}, set(), frozenset(), 2, 2) == 0


def test_a_blocked_new_exam_demands_one_move_among_its_blockers():
    adj = _clash(("N", "A"), ("N", "B"))
    assert _added_move_floor(["N"], {"A": 0, "B": 1}, adj, {}, set(), frozenset(), 2, 2) == 1
    # Its only free blocker weighs two: the floor is two.
    assert (
        _added_move_floor(
            ["N"], {"A": 0, "B": 1}, adj, {}, {"B"}, frozenset(), 2, 2, weights={"A": 2}
        )
        == 2
    )


def test_blocked_exams_sharing_a_blocker_count_it_once():
    adj = _clash(("N", "A"), ("N", "B"), ("M", "A"), ("M", "B"))
    floor = _added_move_floor(["M", "N"], {"A": 0, "B": 1}, adj, {}, set(), frozenset(), 2, 2)
    assert floor == 1


def test_blocked_exams_with_disjoint_blockers_add_up():
    adj = _clash(("N", "A"), ("N", "B"), ("M", "C"), ("M", "D"))
    placements = {"A": 0, "B": 1, "C": 0, "D": 1}
    assert _added_move_floor(["M", "N"], placements, adj, {}, set(), frozenset(), 2, 2) == 2


# ── the brute force ──────────────────────────────────────────────────────────


def _oracle(placements, new, adj, buckets, protected, closed, slot_count, ppd, weights):
    """The true lexicographic minimum of (new exams unplaced, weight moved).

    Legal by the engine's own rule: every moved or new exam may sit where it
    is beside the rest of the board (``_legal_at``). A count of breaches would
    accept a moved exam that trades one breach for another, and would call the
    engine's correct refusal of it "not the fewest".
    """
    mates = _bucket_mates(buckets)
    movable = sorted(course for course in placements if course not in protected)
    best = None
    choices_existing = [
        [placements[c]] + [s for s in range(slot_count) if s != placements[c] and s not in closed]
        for c in movable
    ]
    choices_new = [[None] + [s for s in range(slot_count) if s not in closed] for _ in new]
    for existing in itertools.product(*choices_existing):
        board = dict(placements)
        board.update(zip(movable, existing, strict=True))
        changed = [c for c, s in zip(movable, existing, strict=True) if s != placements[c]]
        moved = sum((weights or {}).get(c, 1) for c in changed)
        if best is not None and moved > best[1] and best[0] == 0:
            continue
        for chosen in itertools.product(*choices_new):
            full = dict(board)
            for course, slot in zip(new, chosen, strict=True):
                if slot is not None:
                    full[course] = slot
            placed = [course for course, slot in zip(new, chosen, strict=True) if slot is not None]
            if not all(
                _legal_at(
                    course,
                    full[course],
                    {other: at for other, at in full.items() if other != course},
                    adj,
                    mates,
                    frozenset(closed),
                    ppd,
                )
                for course in [*changed, *placed]
            ):
                continue
            key = (len(new) - len(placed), moved)
            if best is None or key < best:
                best = key
    return best


def _random_board(rng):
    """A tiny saved board, mostly legal, with new exams that clash with much of it."""
    slot_count = rng.choice([4, 4, 6])
    ppd = 2
    existing = [f"E{i}" for i in range(rng.randint(4, 8))]
    new = [f"N{i}" for i in range(rng.randint(1, 2))]
    adj: dict[str, dict[str, int]] = {}
    for a, b in itertools.combinations(existing + new, 2):
        odds = 0.2 if a in existing and b in existing else 0.75
        if rng.random() < odds:
            adj.setdefault(a, {})[b] = 1
            adj.setdefault(b, {})[a] = 1
    buckets = {}
    if rng.random() < 0.5:
        members = existing + new
        buckets[("P", 1)] = set(rng.sample(members, min(len(members), rng.randint(2, 3))))
    placements: dict[str, int] = {}
    for course in existing:
        legal = [
            slot
            for slot in range(slot_count)
            if not find_violations({**placements, course: slot}, adj, buckets, ppd)[1]
        ]
        placements[course] = rng.choice(legal or list(range(slot_count)))
    # The view protects every exam in a rule break the saved board already has.
    damaged, _ = find_violations(placements, adj, buckets, ppd)
    protected = set(damaged)
    for course in existing:
        if rng.random() < 0.15:
            protected.add(course)
    # A locked cell holds its exams, which are protected; these are empty.
    closed = frozenset(
        slot
        for slot in range(slot_count)
        if rng.random() < 0.15 and slot not in placements.values()
    )
    weights = {existing[0]: 2} if rng.random() < 0.3 else None
    # Keep the brute force small: at most three exams it may move.
    free = [course for course in existing if course not in protected]
    protected.update(free[3:])
    # Half the boards come with Build's greedy: some new exams already seated,
    # each legally, first fit in a random order - its choice is arbitrary.
    greedy: dict[str, int] = {}
    if rng.random() < 0.5:
        mates = _bucket_mates(buckets)
        seated = dict(placements)
        for course in rng.sample(new, len(new)):
            fits = [
                slot
                for slot in range(slot_count)
                if _legal_at(course, slot, seated, adj, mates, closed, ppd)
            ]
            if fits and rng.random() < 0.8:
                seated[course] = greedy[course] = rng.choice(fits)
    return placements, new, adj, buckets, protected, closed, slot_count, ppd, weights, greedy


def test_every_tiny_board_gets_the_true_fewest_against_a_brute_force():
    """The engine's (unplaced, weight moved) is the brute force's, on every board.

    Not only "never claimed falsely": a board that places fewer than possible,
    or moves more than needed, fails - which is what an Add that stopped early,
    or ranked the greedy's seat above the registrar's exams, did.
    """
    checked = needed_moves = with_greedy = 0
    for number in range(400):
        rng = random.Random(9100 + number)
        board_args = _random_board(rng)
        placements, new, adj, buckets, protected, closed, slots, ppd, weights, greedy = board_args
        result = place_added_exams(
            placements=dict(placements),
            new_units=new,
            greedy=dict(greedy),
            adj=adj,
            slot_count=slots,
            periods_per_day=ppd,
            plan_term_buckets=buckets,
            protected=set(protected),
            weights=weights,
            closed_slots=closed,
        )
        board = result.placements
        # Legal, and the promises held.
        assert set(placements) <= set(board)
        for course in protected:
            assert board[course] == placements[course]
        for course in [*result.placed_new, *result.moved]:
            assert board[course] not in closed
        _, before = find_violations(dict(placements), adj, buckets, ppd)
        _, after = find_violations(board, adj, buckets, ppd)
        assert after == before, number
        moved_weight = sum((weights or {}).get(c, 1) for c in result.moved)
        engine_key = (len(result.unplaced_new), moved_weight)
        oracle = _oracle(placements, new, adj, buckets, protected, closed, slots, ppd, weights)
        assert not result.search_stopped, number
        assert engine_key == oracle, (number, engine_key, oracle)
        assert result.proven_minimal, number
        checked += 1
        needed_moves += oracle[1] > 0
        with_greedy += bool(greedy)
    assert checked == 400
    # Not vacuous: many boards need moves, and many start from a greedy.
    assert needed_moves > 60
    assert with_greedy > 100


def _medium_board(seed):
    """Forty saved exams on three days of three periods, fourteen new ones."""
    rng = random.Random(seed)
    slot_count, ppd = 9, 3
    existing = [f"E{i:02d}" for i in range(40)]
    new = [f"N{i:02d}" for i in range(14)]
    adj: dict[str, dict[str, int]] = {}
    for a, b in itertools.combinations(existing + new, 2):
        if rng.random() < (0.18 if a in existing and b in existing else 0.3):
            adj.setdefault(a, {})[b] = 1
            adj.setdefault(b, {})[a] = 1
    buckets = {("P", k): set(rng.sample(existing + new, 3)) for k in range(6)}
    mates = _bucket_mates(buckets)
    placements: dict[str, int] = {}
    for course in existing:
        fits = [
            slot
            for slot in range(slot_count)
            if _legal_at(course, slot, placements, adj, mates, frozenset(), ppd)
        ]
        placements[course] = rng.choice(fits or list(range(slot_count)))
    damaged, _ = find_violations(placements, adj, buckets, ppd)
    protected = set(damaged) | {course for course in existing if rng.random() < 0.1}
    return placements, new, adj, buckets, protected, slot_count, ppd


def _fewest(placements, new, adj, buckets, protected, slot_count, ppd):
    """(unplaced, moved) at their proven optimum, every unprotected exam free to move."""
    movable = sorted([c for c in placements if c not in protected] + new)
    built = _build_model(
        movable, placements, adj, buckets, slot_count, ppd, allow_unseated=frozenset(new)
    )
    levels = [
        sum(built.unseated[c] for c in new),
        sum(
            1 - built.y[c, placements[c]] if (c, placements[c]) in built.y else 1
            for c in movable
            if c in placements
        ),
    ]
    values = []
    for objective in levels:
        built.model.minimize(objective)
        solver = engine.cp_model.CpSolver()
        solver.parameters.num_workers = 8
        solver.parameters.max_time_in_seconds = 60
        assert solver.solve(built.model) == engine.cp_model.OPTIMAL
        values.append(int(solver.value(objective)))
        built.model.add(objective == values[-1])
    return tuple(values)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 6, 9])
def test_boards_too_big_for_the_brute_force_get_the_proven_fewest_moves(seed):
    """Fifty-four exams on nine slots: every new exam fits, but only if saved
    exams make room - two to five of them. The engine's answer is the proven
    optimum of a separate solve over every exam, and it says it is proven.
    """
    placements, new, adj, buckets, protected, slot_count, ppd = _medium_board(seed)
    result = place_added_exams(
        placements=dict(placements),
        new_units=new,
        greedy={},
        adj=adj,
        slot_count=slot_count,
        periods_per_day=ppd,
        plan_term_buckets=buckets,
        protected=set(protected),
    )
    _assert_invariants(result, placements, new, adj, buckets=buckets, protected=protected, ppd=ppd)
    fewest = _fewest(placements, new, adj, buckets, protected, slot_count, ppd)
    assert fewest[1] >= 2, "a board that needs moves"
    assert (len(result.unplaced_new), len(result.moved)) == fewest
    assert result.proven_minimal and not result.search_stopped


def test_a_bucket_mates_helper_lists_every_mate_once():
    assert _bucket_mates({("P", 1): {"A", "B"}, ("P", 2): {"A", "C"}}) == {
        "A": {"B", "C"},
        "B": {"A"},
        "C": {"A"},
    }


def test_out_of_work_still_seats_a_new_exam_that_fits_without_moving_anything():
    placements = {"A": 0, "B": 1}
    adj = _clash(("N", "A"), ("N", "B"))
    result = _add(placements, ["N"], adj, work_budget=engine._MIN_SOLVE_WORK / 2)
    _assert_invariants(result, placements, ["N"], adj)
    assert result.unplaced_new == [] and result.moved == []
    assert result.placements["N"] not in {0, 1}
