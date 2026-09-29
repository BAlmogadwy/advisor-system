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

A linked exam reaches this module already collapsed to one exam (see
``core.services.linked_exams``): the caller passes each link's number of
courses as its ``weight``, so unseating, moving or displacing a link costs what
it costs the registrar - one course each. Members never share a literal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

#: Machine-independent work limit per solve, so the same board comes back on a
#: developer workstation and on the 0.5-CPU production instance. A wall-clock
#: limit would make the answer depend on how busy the host was.
_DETERMINISTIC_LIMIT = 2.0
_MIN_SOLVE_WORK = 0.05
#: Deterministic work for a whole repair, across every attempt and the sweep.
#: The heaviest measured on the real 167-course board was 1.18 (every single
#: drag, and 5 to 40 random drags at once), so this leaves about seven times
#: that; what it stops is the pathological board, not the registrar's.
_REPAIR_WORK_BUDGET = 8.0

ConflictGraph = dict[str, dict[str, int]]
Buckets = dict[tuple[str, int], set[str]]
Weights = Mapping[str, int]


def _weighted(weight: int, term):
    """``term`` counted ``weight`` times; left exactly as it was for a single course."""
    return term if weight == 1 else weight * term


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
    widened: bool = False


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
    #: One literal per slot an exam may legally take. A slot that a frozen
    #: neighbour or a frozen bucket-mate rules out has no literal at all.
    y: dict[tuple[str, int], cp_model.IntVar]
    unseated: dict[str, cp_model.IntVar]
    slot_count: int

    def slot_of(self, solver: cp_model.CpSolver, course: str) -> int:
        """Where ``solver`` put ``course``; ``slot_count`` - after every real
        slot - when it went to OVERFLOW."""
        if course in self.unseated and solver.value(self.unseated[course]):
            return self.slot_count
        return next(
            slot
            for slot in range(self.slot_count)
            if (course, slot) in self.y and solver.value(self.y[course, slot])
        )

    def at(self, course: str, slot: int) -> cp_model.IntVar:
        """The literal for ``course`` sitting at ``slot``, OVERFLOW included."""
        return self.unseated[course] if slot == self.slot_count else self.y[course, slot]


def _build_model(
    movable: list[str],
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    slot_count: int,
    periods_per_day: int,
    *,
    allow_unseated: bool | frozenset[str],
    closed_slots: frozenset[int] = frozenset(),
) -> _Model:
    """The repair over ``movable``; every other exam is a constant.

    A slot in ``closed_slots`` (a locked cell) gets no literal at all, exactly
    like a slot a frozen neighbour rules out: no exam may move into it.

    ``allow_unseated`` gives every movable exam an OVERFLOW literal when it is
    ``True``, none when ``False``, and - as a set - only the exams it names:
    adding courses may leave a NEW exam in OVERFLOW, never an existing one.

    A frozen exam never appears as a constraint: the slots and days it rules
    out are left out of each movable exam's domain before the model is built.
    On a widened repair of 113 exams that, with at-most-one constraints in
    place of linear ones, took the model from 88,000 constraints to 61,000 and
    each solve from 1.0s to 0.4s.
    """
    model = cp_model.CpModel()
    movable_set = set(movable)
    # In key order: the buckets come from an unordered query, and when a solve
    # stops at its work limit the answer can depend on constraint order.
    buckets = [
        members for _, members in sorted((plan_term_buckets or {}).items()) if members & movable_set
    ]

    y: dict[tuple[str, int], cp_model.IntVar] = {}
    unseated: dict[str, cp_model.IntVar] = {}
    for course in movable:
        taken = {
            placements[mate]
            for mate in adj.get(course, {})
            if mate not in movable_set and mate in placements
        }
        held_days = {
            _day_of(placements[mate], periods_per_day)
            for members in buckets
            if course in members
            for mate in members
            if mate not in movable_set and mate in placements
        }
        choices = []
        for slot in range(slot_count):
            if (
                slot not in taken
                and slot not in closed_slots
                and _day_of(slot, periods_per_day) not in held_days
            ):
                y[course, slot] = model.new_bool_var(f"y_{course}_{slot}")
                choices.append(y[course, slot])
        if allow_unseated is True or (
            isinstance(allow_unseated, frozenset) and course in allow_unseated
        ):
            unseated[course] = model.new_bool_var(f"ovf_{course}")
            choices.append(unseated[course])
        # With no legal slot and no escape this is empty, and an empty
        # exactly-one is infeasible - which is the truth about that exam.
        model.add_exactly_one(choices)

    # Rule A between two exams that are both moving: never the same slot.
    for course in movable:
        for mate in sorted(adj.get(course, {})):
            if mate in movable_set and mate > course:
                for slot in range(slot_count):
                    if (course, slot) in y and (mate, slot) in y:
                        model.add_at_most_one([y[course, slot], y[mate, slot]])

    # Rule B between moving bucket-mates: at most one of them on any day.
    days = -(-slot_count // periods_per_day)
    for members in buckets:
        moving = sorted(course for course in members if course in movable_set)
        if len(moving) < 2:
            continue
        for day in range(days):
            first = day * periods_per_day
            on_day = [
                y[course, slot]
                for course in moving
                for slot in range(first, min(slot_count, first + periods_per_day))
                if (course, slot) in y
            ]
            if len(on_day) > 1:
                model.add_at_most_one(on_day)
    return _Model(model, y, unseated, slot_count)


class _Budget:
    """Deterministic work shared by every solve in one repair.

    A per-solve limit alone does not bound a repair: up to four attempts of up
    to three levels each, then a sweep that may solve once per exam in scope.
    This caps the total, so no board can hold a request for minutes. What runs
    out keeps the best board already found, or reports that none was.
    """

    def __init__(self, total: float) -> None:
        self.remaining = total

    def limit(self) -> float:
        # A sliver of budget is treated as none: a solve given almost no work
        # can hand back its hint as FEASIBLE having spent nothing, and the
        # tie-break sweep would ask again forever.
        if self.remaining < _MIN_SOLVE_WORK:
            return 0.0
        return min(_DETERMINISTIC_LIMIT, self.remaining)

    def spend(self, seconds: float) -> None:
        self.remaining -= seconds


def _solve(
    model: cp_model.CpModel, objective: cp_model.LinearExprT, budget: _Budget
) -> tuple[cp_model.CpSolverStatus, cp_model.CpSolver | None]:
    """One solve, charged to ``budget``; UNKNOWN with no solver once it is spent."""
    limit = budget.limit()
    if limit <= 0:
        return cp_model.UNKNOWN, None
    model.minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = limit
    status = solver.solve(model)
    budget.spend(solver.deterministic_time)
    return status, solver


@dataclass
class _Attempt:
    """One solve of the model over a particular movable set."""

    built: _Model
    status: cp_model.CpSolverStatus
    solver: cp_model.CpSolver | None

    @property
    def found(self) -> bool:
        return self.solver is not None and self.status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def _hint_from(built: _Model, solver: cp_model.CpSolver) -> None:
    """Start the next solve from the last good board rather than from nothing."""
    built.model.clear_hints()
    for var in [*built.y.values(), *built.unseated.values()]:
        built.model.add_hint(var, solver.value(var))


def _lexicographic(
    built: _Model, levels: Iterable, budget: _Budget
) -> tuple[cp_model.CpSolverStatus, cp_model.CpSolver | None, bool]:
    """Minimise each level, then freeze it before minimising the next.

    A level that fails keeps the last good board instead of discarding it: that
    board satisfies every level frozen so far and every hard rule, so it is a
    legal repair - just not optimised on the levels that followed. Throwing it
    away turned a found repair into a reported failure.
    """
    best: cp_model.CpSolver | None = None
    best_status = cp_model.UNKNOWN
    proven = True
    for objective in levels:
        if best is not None:
            _hint_from(built, best)
        status, solver = _solve(built.model, objective, budget)
        if solver is None or status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if best is None:
                return status, solver, False
            return cp_model.FEASIBLE, best, False
        proven = proven and status == cp_model.OPTIMAL
        built.model.add(objective == int(solver.value(objective)))
        best, best_status = solver, status
    return best_status, best, proven


def _canonical(
    built: _Model, scope: list[str], solver: cp_model.CpSolver, budget: _Budget
) -> cp_model.CpSolver:
    """One board among the equal-cost optima, chosen by the board alone.

    Equal-cost repairs are common, and which one the solver reaches first
    depends on its search, not on the board - across 1,273 random boards the
    answer changed on 538. The rule: take the exams in code order and give each
    the earliest slot any optimal repair allows, given the ones already settled
    (OVERFLOW counts as after every slot).

    Applied one exam at a time that is one solve per exam in scope - 115
    solves and 30 seconds on a widened real repair, where almost every exam
    was already where the rule would put it. So each solve asks for the FIRST
    exam that could sit earlier while every exam before it stays where it is.
    Those before it are settled in bulk, since none of them could have moved
    earlier either, and the sweep costs one solve per exam that changes, plus
    the one that finds nothing left to change.

    Only a proven answer settles anything. When a solve stops at its work limit
    the board it returns is still optimal and earlier in the order, so it is
    kept, but nothing is settled on its word and the same question is asked
    again. When every solve is proven the board depends on the input alone;
    when one is not, it also depends on the solver's search - deterministic for
    one build of OR-Tools, but not promised across platforms.
    """
    count = built.slot_count
    at = {course: built.slot_of(solver, course) for course in scope}
    start = 0
    while start < len(scope):
        _hint_from(built, solver)
        trial = built.model.clone()
        options: list[tuple[int, int, cp_model.IntVar]] = []
        held = None  # true when every exam from ``start`` up to here stays put
        for index in range(start, len(scope)):
            course = scope[index]
            for slot in range(at[course]):
                if (course, slot) in built.y:
                    pick = trial.new_bool_var(f"earlier_{course}_{slot}")
                    trial.add_implication(pick, built.y[course, slot])
                    if held is not None:
                        trial.add_implication(pick, held)
                    options.append((index, slot, pick))
            stays = trial.new_bool_var(f"stays_{course}")
            trial.add_implication(stays, built.at(course, at[course]))
            if held is not None:
                trial.add_implication(stays, held)
            held = stays
        if not options:
            break
        trial.add_exactly_one([pick for _, _, pick in options])
        # Earliest exam first, then its earliest slot.
        rank = sum((index * (count + 1) + slot) * pick for index, slot, pick in options)
        status, found = _solve(trial, rank, budget)
        if found is None or status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # INFEASIBLE: no exam can sit earlier, so the board is canonical.
            # Out of work: keep the last board, which is still optimal.
            if status != cp_model.INFEASIBLE:
                logger.info(
                    "exam repair tie-break stopped unproven at exam %s of %s", start, len(scope)
                )
            break
        if status == cp_model.OPTIMAL:
            index, slot, _ = next(option for option in options if found.value(option[2]))
            for settled in scope[start:index]:
                built.model.add_bool_and([built.at(settled, at[settled])])
            built.model.add_bool_and([built.at(scope[index], slot)])
            start = index + 1
        solver = found
        at = {course: built.slot_of(solver, course) for course in scope}
    return solver


def _hint_escape(
    built: _Model,
    scope: list[str],
    damaged: set[str],
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    periods_per_day: int,
) -> None:
    """Start the escape solve from a board that is already legal.

    Every exam outside the damage keeps its slot - no two such exams break a
    rule together, or both would be damaged - and each damaged one, in code
    order, takes the nearest slot still legal, or OVERFLOW. Without a complete
    legal start the solver can spend its whole budget in presolve and return
    nothing, although a legal board always exists: on the real 167-course board
    with every exam movable, the escape model found nothing at a 2.0 work limit
    without this start, and a board with one exam unseated with it.
    """
    mates: dict[str, set[str]] = {course: set() for course in scope}
    for members in (plan_term_buckets or {}).values():
        for course in members & mates.keys():
            mates[course] |= members - {course}
    board = {
        course: placements[course]
        for course in scope
        if course not in damaged and (course, placements[course]) in built.y
    }
    for course in scope:
        if course in board:
            continue
        home = placements[course]
        for slot in sorted(range(built.slot_count), key=lambda slot: (abs(slot - home), slot)):
            day = _day_of(slot, periods_per_day)
            if (
                (course, slot) in built.y
                and not any(board.get(mate) == slot for mate in adj.get(course, {}))
                and not any(
                    mate in board and _day_of(board[mate], periods_per_day) == day
                    for mate in mates[course]
                )
            ):
                board[course] = slot
                break
    built.model.clear_hints()
    for (course, slot), var in built.y.items():
        built.model.add_hint(var, board.get(course) == slot)
    for course, var in built.unseated.items():
        built.model.add_hint(var, course not in board)


def _neighbourhood(
    courses: set[str],
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    protected: set[str],
) -> set[str]:
    """Unprotected exams that could step aside to make room for ``courses``.

    Their conflict neighbours and their bucket-mates: the only exams whose
    position can block one of ``courses`` from a slot or a day.
    """
    ring: set[str] = set()
    for course in courses:
        ring.update(mate for mate in adj.get(course, {}) if mate in placements)
    for members in (plan_term_buckets or {}).values():
        if members & courses:
            ring.update(mate for mate in members if mate in placements)
    return ring - courses - protected


def _move_lower_bound(
    protected: set[str],
    placements: dict[str, int],
    adj: ConflictGraph,
    plan_term_buckets: Buckets | None,
    periods_per_day: int,
    weights: Weights | None = None,
) -> int:
    """A floor no legal repair can go below: the proof behind "fewest possible".

    Each breach demands moves from its unprotected members - one for a clashing
    pair; for a bucket crowded onto one day, every free member if a frozen one
    holds the day, otherwise all but one. Breaches whose free members do not
    overlap cannot share those moves, so their demands add up. A repair that
    reaches this floor is globally minimal, whatever scope found it.

    One drag onto a frozen exam gives breaches with disjoint free members, so
    the floor is exact. Breaches crowded around one free exam share it, the
    floor drops, and a scope that moved more is correctly left unproven.

    With ``weights`` a move costs the exam's weight: a clashing pair needs its
    lighter free member moved, a crowded day all its free weight but the
    heaviest member's (or all of it, when a frozen exam holds the day).
    """
    weight = (weights or {}).get
    demands: list[tuple[frozenset[str], int]] = []
    for course in sorted(adj):
        if course not in placements:
            continue
        for mate in sorted(adj[course]):
            if mate > course and mate in placements and placements[mate] == placements[course]:
                free = frozenset(c for c in (course, mate) if c not in protected)
                if free:
                    demands.append((free, min(weight(c, 1) for c in free)))
    for members in (plan_term_buckets or {}).values():
        by_day: dict[int, list[str]] = {}
        for course in sorted(members):
            if course in placements:
                by_day.setdefault(_day_of(placements[course], periods_per_day), []).append(course)
        for same_day in by_day.values():
            if len(same_day) < 2:
                continue
            free = frozenset(c for c in same_day if c not in protected)
            if not free:
                continue
            held = len(free) < len(same_day)
            free_weight = sum(weight(c, 1) for c in free)
            demands.append(
                (free, free_weight if held else free_weight - max(weight(c, 1) for c in free))
            )
    used: set[str] = set()
    floor = 0
    for free, need in sorted(demands, key=lambda item: (len(item[0]), sorted(item[0]))):
        if used.isdisjoint(free):
            used |= free
            floor += need
    return floor


#: Rings of neighbours tried before an exam is sent to OVERFLOW. Freezing every
#: undamaged exam is what keeps a repair small, but it also forbids the obvious
#: fix of moving an innocent exam one slot to make room. Measured on every
#: possible single drag of the real 167-course board, 162 of 4,170 repairs sent
#: an exam to OVERFLOW although a legal repair existed. With one ring all 162
#: are repaired in 2 to 6 moves, none unseated, the slowest in 1.6s; one ring
#: also reached the true optimum on all 57 such cases on the 96-course board.
#: No real edit has needed a second ring. It stays as the last step before
#: OVERFLOW, and the work budget bounds what it can cost.
_MAX_RINGS = 2


def repair_minimum_change(
    *,
    placements: dict[str, int],
    adj: ConflictGraph,
    slot_count: int,
    periods_per_day: int,
    plan_term_buckets: Buckets | None = None,
    protected: set[str] | None = None,
    weights: Weights | None = None,
    closed_slots: frozenset[int] = frozenset(),
) -> MinChangeResult:
    """Move the fewest exams that makes ``placements`` legal.

    ``protected`` exams are frozen: the registrar's pins and the edits they have
    made. The search starts from the exams actually in a clash. If they cannot
    all be seated, it widens by a ring of their neighbours - exams that could
    step aside - before it will send anything to OVERFLOW.

    ``weights`` counts an exam as that many courses (a linked exam), on every
    level: unseated, moved and distance. An exam it does not name weighs one.

    ``closed_slots`` are locked cells: no exam moves into one. The exams in
    them are the caller's to protect; a breach among them alone stays, and is
    the caller's to report.
    """
    if slot_count <= 0 or periods_per_day <= 0:
        raise ValueError("A repair needs at least one slot and one period per day.")
    protected = set(protected or ())
    weight = (weights or {}).get
    damaged, breaches = find_violations(placements, adj, plan_term_buckets, periods_per_day)
    movable = damaged - protected
    result = MinChangeResult(
        placements=dict(placements),
        violations_before=breaches,
        violations_after=breaches,
        movable=len(movable),
    )
    if not movable:
        # Either the board is already legal, or every damaged exam is one the
        # registrar froze. Saying so is the honest answer; quietly moving an
        # innocent exam to compensate is not.
        return result

    def levels(built: _Model, scope: list[str]) -> list:
        # The user's ask first, then distance, so a one-period nudge never
        # scores like a fifteen-day exile.
        #
        # The two rarely pull apart here: every move costs at least one slot of
        # distance and staying costs nothing, so minimising distance already
        # minimises moves. Brute-forced across 893 random boards, distance alone
        # never moved more exams than this ordering. The move-count level stays
        # because it states the contract, and it costs nothing - but no test
        # pins its position, because on a damage-scoped repair nothing can.
        # An exam whose own slot is now ruled out has no literal there: it
        # moves in every repair, and counts as a move in every repair.
        #
        # A linked exam weighs as many courses as it has, on every level: were
        # the first left out, unseating a four-course link would cost no more
        # than unseating a single exam.
        moved = sum(
            _weighted(weight(course, 1), 1 - built.y[course, placements[course]])
            if (course, placements[course]) in built.y
            else weight(course, 1)
            for course in scope
        )
        distance = sum(
            _weighted(weight(course, 1), abs(slot - placements[course])) * var
            for (course, slot), var in built.y.items()
        )
        seats = (
            [sum(_weighted(weight(course, 1), var) for course, var in built.unseated.items())]
            if built.unseated
            else []
        )
        return [*seats, moved, distance]

    budget = _Budget(_REPAIR_WORK_BUDGET)

    def attempt(scope_set: set[str], *, allow_unseated: bool) -> _Attempt:
        scope = sorted(scope_set)
        built = _build_model(
            scope,
            placements,
            adj,
            plan_term_buckets,
            slot_count,
            periods_per_day,
            allow_unseated=allow_unseated,
            closed_slots=frozenset(closed_slots),
        )
        if allow_unseated:
            _hint_escape(built, scope, damaged, placements, adj, plan_term_buckets, periods_per_day)
        status, solver, proven = _lexicographic(built, levels(built, scope), budget)
        if solver is not None and not proven:
            logger.info(
                "exam repair stopped unproven over %s exams (unseating allowed: %s)",
                len(scope),
                allow_unseated,
            )
        return _Attempt(built, status, solver)

    # Hard rules only, over the exams in a clash.
    solved = attempt(movable, allow_unseated=False)
    rings = 0
    # Widen before giving up. INFEASIBLE and UNKNOWN both mean no legal board
    # was found at this scope; neither is a reason to reach for OVERFLOW yet.
    while not solved.found and rings < _MAX_RINGS:
        ring = _neighbourhood(movable, placements, adj, plan_term_buckets, protected)
        if not ring:
            break
        movable |= ring
        rings += 1
        solved = attempt(movable, allow_unseated=False)
    if not solved.found:
        # Only now may an exam leave the board. The escape model starts from a
        # legal board, so it fails only if the work budget is already spent.
        solved = attempt(movable, allow_unseated=True)

    result.movable = len(movable)
    if not solved.found or solved.solver is None:
        result.status = solved.solver.status_name(solved.status) if solved.solver else "UNKNOWN"
        result.proven_minimal = False
        logger.warning(
            "exam minimum-change repair could not solve: status=%s movable=%s breaches=%s",
            result.status,
            len(movable),
            breaches,
        )
        return result

    built = solved.built
    scope = sorted(movable)
    solver = _canonical(built, scope, solved.solver, budget)

    repaired = dict(placements)
    for course in scope:
        slot = built.slot_of(solver, course)
        if slot == slot_count:
            result.unseated.append(course)
            del repaired[course]
        else:
            repaired[course] = slot
    result.placements = repaired
    result.moved = sorted(c for c in scope if c in repaired and repaired[c] != placements[c])
    # Proven only by reaching a floor no legal repair can go below. The solver's
    # own OPTIMAL is not enough: it is optimal within the exams it was allowed
    # to move, and a board that was already illegal can be repaired more
    # cheaply by moving an exam outside that scope.
    result.proven_minimal = not result.unseated and sum(
        weight(course, 1) for course in result.moved
    ) == _move_lower_bound(protected, placements, adj, plan_term_buckets, periods_per_day, weights)
    result.status = "OPTIMAL" if result.proven_minimal else "FEASIBLE"
    result.widened = rings > 0
    _, result.violations_after = find_violations(repaired, adj, plan_term_buckets, periods_per_day)
    return result


# ── adding courses to a saved timetable ──────────────────────────────────────
#
# The registrar built a timetable from part of the courses, perfected it, and
# now adds the rest. Build's greedy places what fits around the saved board
# first, moving nothing (``core.services.exam_add_courses``). Only what it
# could not place reaches the solver below: the new exams start in OVERFLOW,
# and an existing exam moves only if that is the one way to seat one more.

#: Deterministic work for one Add, across every attempt, every level and the
#: sweep, sized by how many new exams the greedy left unplaced. The repair's
#: rule stands: several times the heaviest real board, so what it stops is a
#: pathological board, never the registrar's (numbers in the PR).
_ADD_WORK_BUDGET = 8.0
_ADD_WORK_PER_UNPLACED = 1.0
_ADD_WORK_CAP = 32.0
#: A ring is not started with less work than this left: it could spend it all
#: on placing and leave its moves unminimised.
_ADD_RING_RESERVE = 1.0


def add_work_budget(unplaced: int) -> float:
    """The work an Add may spend on ``unplaced`` exams the greedy could not seat."""
    return min(_ADD_WORK_CAP, _ADD_WORK_BUDGET + _ADD_WORK_PER_UNPLACED * max(0, unplaced))


@dataclass
class AddedExamsResult:
    """Where the new exams went, and which existing exams moved to make room.

    ``placements`` holds every existing exam that was placed and every new
    exam that found a slot. ``moved`` names existing exams only: placing a new
    exam is what was asked for, never a move.
    """

    placements: dict[str, int]
    placed_new: list[str] = field(default_factory=list)
    unplaced_new: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    rings: int = 0
    widened: bool = False
    status: str = "OPTIMAL"
    proven_minimal: bool = True
    #: The work ran out while a new exam was still unplaced and rings were left.
    search_stopped: bool = False
    attempts: int = 0
    work: float = 0.0
    #: Moves the final pass undid because a stopped solve had left them in.
    moved_back: int = 0


def _bucket_mates(plan_term_buckets: Buckets | None) -> dict[str, set[str]]:
    mates: dict[str, set[str]] = {}
    for members in (plan_term_buckets or {}).values():
        for course in members:
            mates.setdefault(course, set()).update(members - {course})
    return mates


def _legal_at(
    course: str,
    slot: int,
    board: Mapping[str, int],
    adj: ConflictGraph,
    mates: Mapping[str, set[str]],
    closed_slots: frozenset[int],
    periods_per_day: int,
) -> bool:
    """``course`` may sit at ``slot`` beside ``board`` (which must not hold it)."""
    if slot in closed_slots:
        return False
    if any(board.get(mate) == slot for mate in adj.get(course, {})):
        return False
    day = _day_of(slot, periods_per_day)
    return not any(
        mate in board and _day_of(board[mate], periods_per_day) == day
        for mate in mates.get(course, ())
    )


def _added_move_floor(
    new_units: Iterable[str],
    placements: Mapping[str, int],
    adj: ConflictGraph,
    mates: Mapping[str, set[str]],
    protected: set[str],
    closed_slots: frozenset[int],
    slot_count: int,
    periods_per_day: int,
    weights: Weights | None = None,
) -> int:
    """A floor no Add can go below: the proof behind "the fewest possible".

    A new exam is blocked when no open slot is free of it with every existing
    exam at home. Placing it moves at least one of its unprotected blockers:
    the slot it finally takes had a blocker at home, and that blocker has left
    it, so it moved - and a protected one never moves. Blocked exams whose
    blocker sets do not overlap cannot share a move, so their demands add up:
    the same disjoint packing the repair's floor uses.

    New exams competing with one another for one free slot are not counted,
    so a board where they do is correctly left unproven.
    """
    weight = (weights or {}).get
    demands: list[frozenset[str]] = []
    for course in sorted(new_units):
        blockers: set[str] = set()
        free = False
        for slot in range(slot_count):
            if slot in closed_slots:
                continue
            day = _day_of(slot, periods_per_day)
            here = {mate for mate in adj.get(course, {}) if placements.get(mate) == slot} | {
                mate
                for mate in mates.get(course, ())
                if mate in placements and _day_of(placements[mate], periods_per_day) == day
            }
            if not here:
                free = True
                break
            blockers |= here
        if not free:
            movable = frozenset(blocker for blocker in blockers if blocker not in protected)
            if movable:
                demands.append(movable)
    used: set[str] = set()
    floor = 0
    for movable in sorted(demands, key=lambda item: (len(item), sorted(item))):
        if used.isdisjoint(movable):
            used |= movable
            floor += min(weight(course, 1) for course in movable)
    return floor


@dataclass
class _Levels:
    """A lexicographic solve of every level, and how far it got."""

    status: cp_model.CpSolverStatus
    solver: cp_model.CpSolver | None
    proven: bool
    complete: bool


def _solve_levels(built: _Model, levels: Iterable, budget: _Budget) -> _Levels:
    """``_lexicographic``, also saying whether every level was solved.

    Only a board solved on every level is swept to its canonical form: the
    sweep keeps the levels already frozen, and one a stopped solve never froze
    could get worse under it.
    """
    best: cp_model.CpSolver | None = None
    best_status = cp_model.UNKNOWN
    proven = True
    for objective in levels:
        if best is not None:
            _hint_from(built, best)
        status, solver = _solve(built.model, objective, budget)
        if solver is None or status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _Levels(cp_model.FEASIBLE if best else status, best, False, False)
        proven = proven and status == cp_model.OPTIMAL
        built.model.add(objective == int(solver.value(objective)))
        best, best_status = solver, status
    return _Levels(best_status, best, proven, True)


def _hint_board(built: _Model, board: Mapping[str, int]) -> None:
    """Start from ``board``, legal by construction: an exam it lacks is in OVERFLOW."""
    built.model.clear_hints()
    for (course, slot), var in built.y.items():
        built.model.add_hint(var, board.get(course) == slot)
    for course, var in built.unseated.items():
        built.model.add_hint(var, course not in board)


def _seat_demand_of(
    seat_demand: Mapping[str, Mapping[str, int]] | None, course: str, gender: str
) -> int:
    return int(((seat_demand or {}).get(course) or {}).get(gender, 0) or 0)


def _seat_slacks(
    built: _Model,
    scope: list[str],
    home: Mapping[str, int],
    seat_demand: Mapping[str, Mapping[str, int]] | None,
    seat_capacity: Mapping[str, int] | None,
) -> list:
    """Seats over each (slot, cohort)'s capacity, as slack variables to minimise.

    Aggregate seats only - a necessary condition, never the rooming itself,
    which still decides. A slot the saved board already filled past capacity
    has that load as its limit: the solver is not rewarded for "fixing" what
    it was not asked to fix, and is charged for adding to it.
    """
    if not seat_demand or not seat_capacity:
        return []
    in_scope = set(scope)
    base: dict[tuple[int, str], int] = {}
    fixed: dict[tuple[int, str], int] = {}
    for course, slot in home.items():
        for gender in seat_capacity:
            seats = _seat_demand_of(seat_demand, course, gender)
            if not seats:
                continue
            base[slot, gender] = base.get((slot, gender), 0) + seats
            if course not in in_scope:
                fixed[slot, gender] = fixed.get((slot, gender), 0) + seats
    slacks = []
    for slot in range(built.slot_count):
        for gender in sorted(seat_capacity):
            terms = [
                (_seat_demand_of(seat_demand, course, gender), built.y[course, slot])
                for course in scope
                if (course, slot) in built.y and _seat_demand_of(seat_demand, course, gender)
            ]
            if not terms:
                continue
            limit = max(int(seat_capacity[gender]), base.get((slot, gender), 0)) - fixed.get(
                (slot, gender), 0
            )
            most = sum(seats for seats, _ in terms)
            if most <= limit:
                continue
            slack = built.model.new_int_var(0, most - limit, f"seats_{slot}_{gender}")
            built.model.add(sum(seats * var for seats, var in terms) - limit <= slack)
            slacks.append(slack)
    return slacks


def place_added_exams(
    *,
    placements: dict[str, int],
    new_units: Iterable[str],
    greedy: Mapping[str, int],
    adj: ConflictGraph,
    slot_count: int,
    periods_per_day: int,
    plan_term_buckets: Buckets | None = None,
    protected: set[str] | None = None,
    weights: Weights | None = None,
    closed_slots: frozenset[int] = frozenset(),
    day_cost: Callable[[str, int], int] | None = None,
    day_load: Mapping[int, int] | None = None,
    seat_demand: Mapping[str, Mapping[str, int]] | None = None,
    seat_capacity: Mapping[str, int] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    work_budget: float | None = None,
) -> AddedExamsResult:
    """Seat the new exams the greedy could not, moving as few existing exams as possible.

    ``placements`` is the saved board: every existing exam that has a slot,
    each at home. ``greedy`` holds the new exams Build's greedy already placed
    around it without moving anything; every other new exam starts in
    OVERFLOW. Existing exams never go to OVERFLOW: only a new exam may stay
    there, and only when no legal board found seats it.

    What is minimised, in order: new exams left unplaced; new exams the greedy
    had placed that would lose their slot (so one is never traded for
    another); existing exams moved, then how far (a linked exam weighs its
    courses); seats over capacity; new exams moved off the greedy's slot;
    students over the daily limit (``day_cost(exam, day)``); and the day's
    load - ``day_load`` holds the existing exams each day has, and the new
    ones are counted as they are placed - so exams without a home do not pile
    onto day one: first the busiest day, then the load each one lands on.

    ``protected`` exams never move, and nothing moves into ``closed_slots``.
    Attempt 0 moves no existing exam; each ring after it frees the unprotected
    neighbours and study-plan mates of what is still unplaced, and of the new
    exams beside it (a new exam holding the only slot can step aside only if
    its own neighbour does). One work budget covers every solve; what runs out
    keeps the best legal board found - the greedy's, at worst - and never
    fails the Add.
    """
    if slot_count <= 0 or periods_per_day <= 0:
        raise ValueError("Adding courses needs at least one slot and one period per day.")
    new = sorted(set(new_units))
    new_set = set(new)
    already = sorted(new_set & set(placements))
    if already:
        raise ValueError(f"Course {already[0]} is already in the timetable.")
    closed = frozenset(closed_slots)
    wrong = sorted(
        course
        for course, slot in greedy.items()
        if course not in new_set or not 0 <= slot < slot_count or slot in closed
    )
    if wrong:
        raise ValueError(f"Course {wrong[0]} has no legal greedy placement.")
    home = dict(placements)
    protected = set(protected or ()) - new_set
    weight = (weights or {}).get
    mates = _bucket_mates(plan_term_buckets)

    def cancelled() -> None:
        if check_cancelled is not None:
            check_cancelled()

    def key(board: Mapping[str, int]) -> tuple[int, int, int, int]:
        moved = [course for course in home if board.get(course) != home[course]]
        return (
            sum(1 for course in new if course not in board),
            sum(1 for course in greedy if course not in board),
            sum(weight(course, 1) for course in moved),
            sum(weight(course, 1) * abs(board[course] - home[course]) for course in moved),
        )

    board: dict[str, int] = {**home, **greedy}
    result = AddedExamsResult(placements=dict(board))
    if all(course in board for course in new):
        result.placed_new = sorted(greedy)
        return result

    budget = _Budget(
        work_budget
        if work_budget is not None
        else add_work_budget(sum(1 for course in new if course not in board))
    )
    work_at_start = budget.remaining

    def levels(built: _Model, scope: list[str], scope_existing: set[str]) -> list:
        y, ovf = built.y, built.unseated
        out: list = [sum(ovf[course] for course in new if course in ovf)]
        dropped = [ovf[course] for course in sorted(greedy) if course in ovf]
        if dropped:
            out.append(sum(dropped))
        if scope_existing:
            out.append(
                sum(
                    _weighted(weight(course, 1), 1 - y[course, home[course]])
                    if (course, home[course]) in y
                    else weight(course, 1)
                    for course in sorted(scope_existing)
                )
            )
            out.append(
                sum(
                    _weighted(weight(course, 1), abs(slot - home[course])) * var
                    for (course, slot), var in y.items()
                    if course in scope_existing and slot != home[course]
                )
            )
        seats = _seat_slacks(built, scope, home, seat_demand, seat_capacity)
        if seats:
            out.append(sum(seats))
        keep = [
            abs(slot - greedy[course]) * var
            for (course, slot), var in y.items()
            if course in greedy and slot != greedy[course]
        ]
        if keep:
            out.append(sum(keep))
        if day_cost is not None:
            over = [
                cost * var
                for (course, slot), var in y.items()
                if (cost := day_cost(course, _day_of(slot, periods_per_day)))
            ]
            if over:
                out.append(sum(over))
        if day_load is not None:
            # The busiest day, new exams counted as they are placed: without it
            # the sweep would put every new exam without a home on day one.
            days = -(-slot_count // periods_per_day)
            on_day: dict[int, list] = {}
            for (course, slot), var in y.items():
                if course in new_set:
                    on_day.setdefault(_day_of(slot, periods_per_day), []).append(var)
            if on_day:
                peak = built.model.new_int_var(
                    0, max(day_load.values(), default=0) + len(new), "busiest_day"
                )
                for day in range(days):
                    built.model.add(peak >= int(day_load.get(day, 0)) + sum(on_day.get(day, [])))
                out.append(peak)
            load = [
                count * var
                for (course, slot), var in y.items()
                if course in new_set
                and (count := int(day_load.get(_day_of(slot, periods_per_day), 0)))
            ]
            if load:
                out.append(sum(load))
        return [level for level in out if not isinstance(level, int)]

    def attempt(scope_existing: set[str], start: Mapping[str, int]):
        cancelled()
        scope = sorted(new_set | scope_existing)
        built = _build_model(
            scope,
            home,
            adj,
            plan_term_buckets,
            slot_count,
            periods_per_day,
            allow_unseated=frozenset(new),
            closed_slots=closed,
        )
        _hint_board(built, start)
        outcome = _solve_levels(built, levels(built, scope, scope_existing), budget)
        if outcome.solver is not None and not outcome.proven:
            logger.info("exam add stopped unproven over %s exams (%s new)", len(scope), len(new))
        return built, scope, outcome

    def board_of(built: _Model, scope: list[str], solver: cp_model.CpSolver) -> dict[str, int]:
        out = {course: slot for course, slot in home.items() if course not in scope}
        for course in scope:
            slot = built.slot_of(solver, course)
            if slot < slot_count:
                out[course] = slot
        return out

    scope_existing: set[str] = set()
    best: tuple | None = None
    exhausted = False
    found_any = False
    rings = 0
    while True:
        built, scope, outcome = attempt(scope_existing, board)
        result.attempts += 1
        if outcome.solver is not None and outcome.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            found_any = True
            solved = board_of(built, scope, outcome.solver)
            if key(solved) <= key(board):
                board, best = solved, (built, scope, outcome)
        else:
            exhausted = True
        if all(course in board for course in new) or rings >= _MAX_RINGS:
            break
        if budget.remaining < _ADD_RING_RESERVE:
            exhausted = True
            break
        still = {course for course in new if course not in board}
        beside = {
            course
            for course in new
            if course in board
            and any(mate in still for mate in (*adj.get(course, {}), *mates.get(course, ())))
        }
        ring = (
            _neighbourhood(
                still | scope_existing | beside,
                home,
                adj,
                plan_term_buckets,
                protected | new_set,
            )
            - scope_existing
        )
        if not ring:
            break
        scope_existing |= ring
        rings += 1

    if best is not None and best[2].complete:
        cancelled()
        built, scope, outcome = best
        swept = board_of(built, scope, _canonical(built, scope, outcome.solver, budget))
        if key(swept) <= key(board):
            board = swept

    # A stopped solve can leave out a new exam that fits without moving
    # anything, or leave an existing exam away from a home that is free again.
    # Both are settled here, in code order, without the solver.
    def seat_excess(course: str, slot: int) -> int:
        if not seat_demand or not seat_capacity:
            return 0
        total = 0
        for gender in sorted(seat_capacity):
            seats = _seat_demand_of(seat_demand, course, gender)
            if not seats:
                continue
            base = sum(
                _seat_demand_of(seat_demand, other, gender)
                for other, at in home.items()
                if at == slot
            )
            now = sum(
                _seat_demand_of(seat_demand, other, gender)
                for other, at in board.items()
                if at == slot
            )
            limit = max(int(seat_capacity[gender]), base)
            total += max(0, now + seats - limit) - max(0, now - limit)
        return total

    for course in new:
        if course in board:
            continue
        choices = [
            slot
            for slot in range(slot_count)
            if _legal_at(course, slot, board, adj, mates, closed, periods_per_day)
        ]
        if choices:
            board[course] = min(
                choices,
                key=lambda slot, course=course: (
                    seat_excess(course, slot),
                    day_cost(course, _day_of(slot, periods_per_day)) if day_cost else 0,
                    int((day_load or {}).get(_day_of(slot, periods_per_day), 0)),
                    slot,
                ),
            )
    changed = True
    while changed:
        changed = False
        for course in sorted(course for course in home if board[course] != home[course]):
            rest = {other: slot for other, slot in board.items() if other != course}
            if _legal_at(course, home[course], rest, adj, mates, closed, periods_per_day):
                board[course] = home[course]
                result.moved_back += 1
                changed = True

    result.placements = board
    result.placed_new = sorted(course for course in new if course in board)
    result.unplaced_new = sorted(course for course in new if course not in board)
    result.moved = sorted(course for course in home if board[course] != home[course])
    result.rings = rings
    result.widened = rings > 0
    result.search_stopped = bool(result.unplaced_new) and exhausted
    result.proven_minimal = not result.unplaced_new and sum(
        weight(course, 1) for course in result.moved
    ) == _added_move_floor(
        new, home, adj, mates, protected, closed, slot_count, periods_per_day, weights
    )
    result.status = "OPTIMAL" if result.proven_minimal else "FEASIBLE" if found_any else "UNKNOWN"
    result.work = round(work_at_start - budget.remaining, 4)
    if exhausted and result.unplaced_new:
        logger.warning(
            "exam add ran out of work: %s new exam(s) unplaced after %s ring(s)",
            len(result.unplaced_new),
            rings,
        )
    return result
