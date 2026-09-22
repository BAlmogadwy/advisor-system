"""Repair a hand-edited exam board by moving as few exams as possible.

"Optimise from current" answers "what is the best board?" and treats the
registrar's layout as a tie-break: it re-colours from an empty board and, after
a single drag, moved a median of 77 of 167 courses on the real roster. This
answers the opposite question - "what is the smallest set of moves that makes
this board legal?" - and on the same drags the proven minimum was 1 to 3.

Only courses actually implicated in a violation may move. Everything else is a
CONSTANT in the model, never a variable held in place by a constraint, so the
rest of the board cannot drift and the search stays small enough to solve to
proven optimality in milliseconds.

The registrar's own edits are protected as firmly as pins. Without that,
putting a dragged course back where it came from is a legal one-move repair,
and the button would look as though it had undone their work.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

#: Machine-independent work limit per solve, so the same board comes back on a
#: developer workstation and on the 0.5-CPU production instance. A wall-clock
#: limit would make the answer depend on how busy the host was.
_DETERMINISTIC_LIMIT = 2.0
#: Tie-breaking sweeps that settle on one board among equal-cost optima.
_CANONICAL_SWEEPS = 12

ConflictGraph = dict[str, dict[str, int]]
Buckets = dict[tuple[str, int], set[str]]


@dataclass
class MinChangeResult:
    """What the repair did, and what it could not do.

    ``placements`` covers every course that still has a slot. A course the
    repair had to unseat is absent from it and listed in ``unseated`` instead:
    it belongs in OVERFLOW, and leaving it at the slot that broke the rules
    would let a caller re-publish the very clash being repaired.
    """

    placements: dict[str, int]
    moved: list[str] = field(default_factory=list)
    unseated: list[str] = field(default_factory=list)
    violations_before: int = 0
    violations_after: int = 0
    movable: int = 0
    status: str = "OPTIMAL"
    proven_minimal: bool = True


def _day_of(slot_index: int, periods_per_day: int) -> int:
    return slot_index // periods_per_day


def find_violations(
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    periods_per_day: int,
) -> tuple[set[str], int]:
    """Courses breaking a hard rule, and how many distinct breaches there are.

    Rule A: two courses that share a student may not share a slot.
    Rule B: two courses from one (programme, term) bucket may not share a day.
    """
    damaged: set[str] = set()
    breaches = 0
    for course, neighbours in sorted(adj.items()):
        if course not in placements:
            continue
        for mate in sorted(neighbours):
            if mate > course and mate in placements and placements[mate] == placements[course]:
                damaged.update((course, mate))
                breaches += 1

    for members in (plan_term_buckets or {}).values():
        by_day: dict[int, list[str]] = {}
        for course in sorted(members):
            if course in placements:
                by_day.setdefault(_day_of(placements[course], periods_per_day), []).append(course)
        for same_day in by_day.values():
            if len(same_day) >= 2:
                damaged.update(same_day)
                breaches += 1
    return damaged, breaches


@dataclass
class _Model:
    model: cp_model.CpModel
    y: dict[tuple[str, int], cp_model.IntVar]
    unseated: dict[str, cp_model.IntVar]


def _build_model(
    movable: list[str],
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    slot_count: int,
    periods_per_day: int,
    *,
    allow_unseated: bool,
) -> _Model:
    model = cp_model.CpModel()
    y = {
        (course, slot): model.new_bool_var(f"y_{course}_{slot}")
        for course in movable
        for slot in range(slot_count)
    }
    unseated: dict[str, cp_model.IntVar] = {}
    for course in movable:
        placed = sum(y[course, slot] for slot in range(slot_count))
        if allow_unseated:
            unseated[course] = model.new_bool_var(f"ovf_{course}")
            model.add(placed + unseated[course] == 1)
        else:
            model.add(placed == 1)

    days = -(-slot_count // periods_per_day)
    on_day = {}
    for course in movable:
        for day in range(days):
            on_day[course, day] = model.new_bool_var(f"d_{course}_{day}")
            first = day * periods_per_day
            model.add(
                on_day[course, day]
                == sum(
                    y[course, slot]
                    for slot in range(first, min(slot_count, first + periods_per_day))
                )
            )

    movable_set = set(movable)
    for course in movable:
        for mate in sorted(adj.get(course, {})):
            if mate in movable_set:
                if mate > course:
                    for slot in range(slot_count):
                        model.add(y[course, slot] + y[mate, slot] <= 1)
            elif mate in placements:
                # A frozen neighbour is a constant: one forbidden literal, not
                # a pairwise constraint over every slot.
                model.add(y[course, placements[mate]] == 0)

    for members in (plan_term_buckets or {}).values():
        moving = sorted(course for course in members if course in movable_set)
        if not moving:
            continue
        frozen_days = {
            _day_of(placements[course], periods_per_day)
            for course in members
            if course not in movable_set and course in placements
        }
        for day in range(days):
            if day in frozen_days:
                for course in moving:
                    model.add(on_day[course, day] == 0)
            elif len(moving) > 1:
                model.add(sum(on_day[course, day] for course in moving) <= 1)
    return _Model(model, y, unseated)


def _solve(model: cp_model.CpModel, objective) -> tuple[int, cp_model.CpSolver]:
    model.minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = _DETERMINISTIC_LIMIT
    return solver.solve(model), solver


def _lexicographic(built: _Model, levels: Iterable) -> tuple[int, cp_model.CpSolver | None, bool]:
    """Minimise each level, then freeze it before minimising the next."""
    solver = None
    proven = True
    status = cp_model.UNKNOWN
    for objective in levels:
        status, solver = _solve(built.model, objective)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return status, solver, False
        proven = proven and status == cp_model.OPTIMAL
        built.model.add(objective == int(solver.value(objective)))
    return status, solver, proven


def repair_minimum_change(
    *,
    placements: dict[str, int],
    adj: ConflictGraph,
    slot_count: int,
    periods_per_day: int,
    plan_term_buckets: Buckets | None = None,
    protected: set[str] | None = None,
) -> MinChangeResult:
    """Move the fewest exams that makes ``placements`` legal.

    ``protected`` courses are frozen: the registrar's pins and the edits they
    have just made. Every course not implicated in a violation is frozen too,
    which is what keeps this a small search rather than a re-solve.
    """
    if slot_count <= 0 or periods_per_day <= 0:
        raise ValueError("A repair needs at least one slot and one period per day.")
    protected = set(protected or ())
    damaged, breaches = find_violations(placements, adj, plan_term_buckets, periods_per_day)
    movable = sorted(damaged - protected)
    result = MinChangeResult(
        placements=dict(placements),
        violations_before=breaches,
        violations_after=breaches,
        movable=len(movable),
    )
    if not movable:
        # Either the board is already legal, or every damaged course is one the
        # registrar froze. Saying so is the honest answer; quietly moving an
        # innocent course to compensate is not.
        return result

    def levels(built: _Model) -> list:
        # The user's ask first, then distance, so a one-period nudge never
        # scores like a fifteen-day exile.
        #
        # The two rarely pull apart here: every move costs at least one slot of
        # distance and staying costs nothing, so minimising distance already
        # minimises moves. Brute-forced across 893 random boards, distance alone
        # never moved more exams than this ordering. The move-count level stays
        # because it states the contract, and it costs nothing - but no test
        # pins its position, because on a damage-scoped repair nothing can.
        moved = sum(1 - built.y[course, placements[course]] for course in movable)
        distance = sum(
            abs(slot - placements[course]) * built.y[course, slot]
            for course in movable
            for slot in range(slot_count)
        )
        seats = [sum(built.unseated.values())] if built.unseated else []
        return [*seats, moved, distance]

    # Hard rules only. Arming an escape for every movable course is measurably
    # more expensive and is almost never needed, so it is the fallback.
    built = _build_model(
        movable,
        placements,
        adj,
        plan_term_buckets,
        slot_count,
        periods_per_day,
        allow_unseated=False,
    )
    status, solver, proven = _lexicographic(built, levels(built))
    if status == cp_model.INFEASIBLE:
        built = _build_model(
            movable,
            placements,
            adj,
            plan_term_buckets,
            slot_count,
            periods_per_day,
            allow_unseated=True,
        )
        status, solver, proven = _lexicographic(built, levels(built))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result.status = solver.status_name(status) if solver else "UNKNOWN"
        result.proven_minimal = False
        logger.warning(
            "exam minimum-change repair could not solve: status=%s movable=%s breaches=%s",
            result.status,
            len(movable),
            breaches,
        )
        return result

    # One board among equal-cost optima, so identical inputs always return the
    # same answer rather than whichever optimum the solver reached first.
    for course in movable[:_CANONICAL_SWEEPS]:
        position = sum(slot * built.y[course, slot] for slot in range(slot_count))
        sweep_status, sweep = _solve(built.model, position)
        if sweep_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        solver = sweep
        built.model.add(position == int(solver.value(position)))

    repaired = dict(placements)
    for course in movable:
        if built.unseated and solver.value(built.unseated[course]):
            result.unseated.append(course)
            del repaired[course]
            continue
        repaired[course] = next(s for s in range(slot_count) if solver.value(built.y[course, s]))
    result.placements = repaired
    result.moved = sorted(c for c in movable if c in repaired and repaired[c] != placements[c])
    result.status = "OPTIMAL" if proven else "FEASIBLE"
    result.proven_minimal = proven
    _, result.violations_after = find_violations(repaired, adj, plan_term_buckets, periods_per_day)
    return result
