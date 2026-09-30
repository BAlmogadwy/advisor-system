"""Re-place the movable exams of a board to the best board a search can find.

"Optimize current timetable" used to re-colour the board with the greedy
scheduler: one exam at a time, hardest first, never revisiting a choice, and
then a pass that moved exams again to flatten the invigilators. On the real
1448 board (122 exams, 87 of them movable, 27 open periods) every press left
411 to 510 students with two exams on one day. This module improves the board
it is given instead, and minimises, in this order:

  1. exams left in OVERFLOW;
  2. invigilators over the daily limit, summed over days;
  3. students over the exams-per-day limit;
  4. students with a heavy-credit day;
  5. students with two or more exams on one day;
  6. the spacing penalty between exams of one (programme, term);
  7. exams moved from where they were.

How: a few exams at a time are freed - the ones in the students' way, the exams
that share students with them, whole days at a time, and some at random - and
CP-SAT finds better places for those, every other exam staying put. One such
solve is small enough to do its work in a fraction of a second on one worker;
hundreds of them, each starting from the best board so far, do what one solve
of the whole board could not do in the work a web instance can spare: on that
board one solve of everything, given three minutes, reached 98 to 150 students
with two exams on one day, and 500 small solves reach about 85 in half the time.

What is not free is a CONSTANT in each model, never a variable held in place by
a constraint, and a closed slot has no literal at all; so a pinned or locked
exam cannot drift, and no exam can enter a locked cell.

The answer is never worse than the board it was given. Every board is compared
by ``score`` - the exact count, not a model's - and a board is kept only when
it is strictly better. When the given board already seats every exam legally,
the spacing between a (programme, term)'s exams may not get worse either:
sparing students a shared day must not crowd their exams onto neighbouring days.

A linked exam reaches this module already collapsed to one exam (see
``core.services.linked_exams``), as it reaches ``exam_min_change``. A student
registered in two members of one link has two sittings of that exam.
"""

from __future__ import annotations

import itertools
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

ConflictGraph = Mapping[str, Mapping[str, int]]
Buckets = Mapping[tuple[str, int], set[str]]
#: One student's exams: (exam, the credit the student sits in it).
Sittings = Sequence[tuple[str, int]]
Profile = tuple[tuple[tuple[str, int], ...], int]

#: The penalty for two exams of one (programme, term) this many days apart,
#: exactly as the greedy scheduler scores it: next day 100, two days 30, three 10.
_SPACING = (100, 100, 30, 10)

#: How many exams one solve frees, and how many solves one optimisation makes.
#: Both are counts, not seconds, so the same board comes back on a developer
#: workstation and on the 0.5-CPU production instance.
NEIGHBOURHOOD = 12
DEFAULT_ROUNDS = 500
#: Rounds without a better board before the search stops early.
_PATIENCE = 150
#: Machine-independent work for one solve. On the real board a solve given
#: twenty times this found nothing more: what a neighbourhood has to give, it
#: gives at once.
_SOLVE_WORK = 0.05


def spacing_penalty(gap: int) -> int:
    """What two exams of one (programme, term) cost ``gap`` days apart."""
    return _SPACING[gap] if gap < len(_SPACING) else 0


@dataclass(frozen=True, order=True)
class Score:
    """How good a board is; a lower tuple is a better board, level by level."""

    unseated: int = 0
    staff_excess: int = 0
    over_limit: int = 0
    heavy_day: int = 0
    multi_exam_day: int = 0
    spacing: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "unseated": self.unseated,
            "staff_excess": self.staff_excess,
            "over_limit": self.over_limit,
            "heavy_day": self.heavy_day,
            "multi_exam_day": self.multi_exam_day,
            "spacing": self.spacing,
        }


@dataclass(frozen=True)
class Board:
    """One board and the rules it is judged by.

    ``current`` holds the seated exams only: an exam in OVERFLOW is absent.
    ``fixed`` exams never move. ``sittings`` lists, per student, every exam the
    student sits and the credit they sit in it. ``staff`` is what each exam
    needs of each kind of invigilator (any keys; the same keys as
    ``staff_limits``, the most of each kind one day may use).
    """

    exams: Sequence[str]
    current: Mapping[str, int]
    fixed: frozenset[str]
    adj: ConflictGraph
    buckets: Buckets
    slot_count: int
    periods_per_day: int
    sittings: Mapping[int, Sittings]
    max_per_day: int = 2
    closed_slots: frozenset[int] = frozenset()
    weights: Mapping[str, int] = field(default_factory=dict)
    staff: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    staff_limits: Mapping[str, int] = field(default_factory=dict)

    def day_of(self, slot: int) -> int:
        return slot // self.periods_per_day

    @property
    def day_count(self) -> int:
        return -(-self.slot_count // self.periods_per_day)

    def weight(self, exam: str) -> int:
        return self.weights.get(exam, 1)


@dataclass
class OptimiseResult:
    """The board found, and how it compares with the board given.

    ``placements`` is the board to use: the new one when ``improved``, else the
    one given, exactly. ``proven`` is True only when the whole board was solved
    at once to proven optimality - the best board there is, not the best found.
    """

    placements: dict[str, int]
    before: Score
    after: Score
    moved: list[str] = field(default_factory=list)
    improved: bool = False
    proven: bool = False
    movable: int = 0
    rounds: int = 0


def _profiles(board: Board) -> list[Profile]:
    """Students grouped by what they sit: (their sittings, how many students)."""
    counts: dict[tuple[tuple[str, int], ...], int] = defaultdict(int)
    for _student, sits in board.sittings.items():
        if len(sits) >= 2:
            counts[tuple(sorted(sits))] += 1
    return sorted(counts.items())


def _bucket_pairs(board: Board) -> list[tuple[str, str, int]]:
    """Every two exams sharing a (programme, term), and how many they share."""
    shared: dict[tuple[str, str], int] = defaultdict(int)
    known = set(board.exams)
    for _key, members in sorted(board.buckets.items()):
        for first, second in itertools.combinations(sorted(members & known), 2):
            shared[first, second] += 1
    return [(first, second, count) for (first, second), count in sorted(shared.items())]


def _pair_spacing(board: Board, placements: Mapping[str, int], pairs) -> int:
    return sum(
        count * spacing_penalty(abs(board.day_of(placements[a]) - board.day_of(placements[b])))
        for a, b, count in pairs
        if a in placements and b in placements
    )


def _crowded_days(board: Board, placements: Mapping[str, int], sits: Sittings) -> list[list]:
    """The days these students sit two or more exams: each day's (exam, credit)s."""
    by_day: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for exam, credit in sits:
        if exam in placements:
            by_day[board.day_of(placements[exam])].append((exam, credit))
    return [sat for _day, sat in sorted(by_day.items()) if len(sat) >= 2]


def score(
    board: Board,
    placements: Mapping[str, int],
    day_is_heavy: Callable[[list[int]], bool],
    *,
    _profiles_of: list[Profile] | None = None,
    _pairs_of: list[tuple[str, str, int]] | None = None,
) -> Score:
    """The exact score of ``placements``, counted the way the QA report counts."""
    unseated = sum(board.weight(exam) for exam in board.exams if exam not in placements)

    staff_excess = 0
    if board.staff_limits:
        load: dict[tuple[int, str], int] = defaultdict(int)
        for exam, slot in placements.items():
            for kind, need in board.staff.get(exam, {}).items():
                load[board.day_of(slot), kind] += need
        staff_excess = sum(
            max(0, used - board.staff_limits[kind])
            for (_day, kind), used in load.items()
            if kind in board.staff_limits
        )

    over_limit = heavy_day = multi_exam_day = 0
    for sits, students in _profiles(board) if _profiles_of is None else _profiles_of:
        crowded = _crowded_days(board, placements, sits)
        if crowded:
            multi_exam_day += students
        if any(len(sat) > board.max_per_day for sat in crowded):
            over_limit += students
        if any(day_is_heavy([credit for _exam, credit in sat]) for sat in crowded):
            heavy_day += students

    pairs = _bucket_pairs(board) if _pairs_of is None else _pairs_of
    spacing = _pair_spacing(board, placements, pairs)
    return Score(unseated, staff_excess, over_limit, heavy_day, multi_exam_day, spacing)


def _legal(board: Board, placements: Mapping[str, int], movable: Sequence[str]) -> bool:
    """Every movable exam is seated, and none of them breaks a hard rule.

    Two fixed exams may clash or share a day - that was decided by hand - but a
    movable exam may do neither, with a fixed exam or with another movable one.
    """
    moving = set(movable)
    if any(exam not in placements or placements[exam] in board.closed_slots for exam in movable):
        return False
    for exam in movable:
        for mate in board.adj.get(exam, {}):
            if placements.get(mate) == placements[exam]:
                return False
    for members in board.buckets.values():
        on_day: dict[int, list[str]] = defaultdict(list)
        for exam in members:
            if exam in placements:
                on_day[board.day_of(placements[exam])].append(exam)
        if any(len(sat) > 1 and moving.intersection(sat) for sat in on_day.values()):
            return False
    return True


class _Model:
    """A board as a CP-SAT model over its free exams; every other exam is a constant."""

    def __init__(
        self,
        board: Board,
        day_is_heavy: Callable[[list[int]], bool],
        *,
        home: Mapping[str, int],
        profiles: list[Profile],
        pairs: list[tuple[str, str, int]],
    ) -> None:
        self.board = board
        self.model = cp_model.CpModel()
        self.movable = sorted(exam for exam in board.exams if exam not in board.fixed)
        self._moving = set(self.movable)
        self.y: dict[tuple[str, int], cp_model.IntVar] = {}
        self.unseated: dict[str, cp_model.IntVar] = {}
        self._on: dict[tuple[str, int], cp_model.IntVar] = {}
        self._same: dict[tuple[str, str], cp_model.IntVar | int] = {}
        self._seats()
        self._hard_rules()
        self.legal = self._legal_level()
        self.students = self._student_level(profiles, day_is_heavy)
        self.spacing = self._spacing_level(pairs)
        # Moved from where the board was given, not from the last board found.
        self.moves = sum(
            board.weight(exam) * (1 - self.y[exam, home[exam]])
            for exam in self.movable
            if (exam, home.get(exam, -1)) in self.y
        )

    # ── variables ────────────────────────────────────────────────────────

    def _seats(self) -> None:
        """One literal per slot an exam may take; a slot a fixed exam or a lock
        rules out has none. Every free exam may also leave the board."""
        board = self.board
        buckets = [members for _, members in sorted(board.buckets.items())]
        for exam in self.movable:
            taken = {
                board.current[mate]
                for mate in board.adj.get(exam, {})
                if mate not in self._moving and mate in board.current
            }
            held_days = {
                board.day_of(board.current[mate])
                for members in buckets
                if exam in members
                for mate in members
                if mate not in self._moving and mate in board.current
            }
            choices = []
            by_day: dict[int, list[cp_model.IntVar]] = defaultdict(list)
            for slot in range(board.slot_count):
                day = board.day_of(slot)
                if slot in taken or slot in board.closed_slots or day in held_days:
                    continue
                literal = self.model.new_bool_var(f"y_{exam}_{slot}")
                self.y[exam, slot] = literal
                choices.append(literal)
                by_day[day].append(literal)
            self.unseated[exam] = self.model.new_bool_var(f"out_{exam}")
            self.model.add_exactly_one([*choices, self.unseated[exam]])
            for day, literals in by_day.items():
                if len(literals) == 1:
                    self._on[exam, day] = literals[0]
                else:
                    on = self.model.new_bool_var(f"on_{exam}_{day}")
                    self.model.add(sum(literals) == on)
                    self._on[exam, day] = on

    def on(self, exam: str, day: int) -> cp_model.IntVar | int:
        """``exam`` sits on ``day``: a literal, or a constant for a fixed exam."""
        if exam in self._moving:
            return self._on.get((exam, day), 0)
        slot = self.board.current.get(exam)
        return int(slot is not None and self.board.day_of(slot) == day)

    def _hard_rules(self) -> None:
        board = self.board
        # Two exams that share a student, both free: never the same slot.
        for exam in self.movable:
            for mate in sorted(board.adj.get(exam, {})):
                if mate in self._moving and mate > exam:
                    for slot in range(board.slot_count):
                        if (exam, slot) in self.y and (mate, slot) in self.y:
                            self.model.add_at_most_one([self.y[exam, slot], self.y[mate, slot]])
        # Two free exams of one (programme, term): never the same day.
        for _key, members in sorted(board.buckets.items()):
            moving = sorted(exam for exam in members if exam in self._moving)
            if len(moving) < 2:
                continue
            for day in range(board.day_count):
                on_day = [self._on[exam, day] for exam in moving if (exam, day) in self._on]
                if len(on_day) > 1:
                    self.model.add_at_most_one(on_day)

    # ── levels ───────────────────────────────────────────────────────────

    def _legal_level(self):
        """Exams off the board, then invigilators over the daily limit."""
        board = self.board
        excess = []
        for kind, limit in sorted(board.staff_limits.items()):
            held: dict[int, int] = defaultdict(int)
            for exam, slot in board.current.items():
                if exam not in self._moving:
                    held[board.day_of(slot)] += board.staff.get(exam, {}).get(kind, 0)
            for day in range(board.day_count):
                terms = [
                    (board.staff.get(exam, {}).get(kind, 0), self._on[exam, day])
                    for exam in self.movable
                    if (exam, day) in self._on and board.staff.get(exam, {}).get(kind, 0)
                ]
                most = held[day] + sum(need for need, _ in terms)
                floor = max(0, held[day] - limit)
                if not terms or most <= limit:
                    # Nothing free can change this day; what the fixed exams
                    # alone put over the limit is the same on every board.
                    continue
                over = self.model.new_int_var(floor, most - limit, f"over_{kind}_{day}")
                self.model.add(over >= held[day] + sum(need * on for need, on in terms) - limit)
                excess.append(over - floor)
        # One exam left out always outweighs every invigilator over a limit.
        ceiling = 1 + sum(
            need
            for exam in self.movable
            for kind, need in board.staff.get(exam, {}).items()
            if kind in board.staff_limits
        )
        return ceiling * sum(
            board.weight(exam) * self.unseated[exam] for exam in self.movable
        ) + sum(excess)

    def _same_day(self, first: str, second: str) -> cp_model.IntVar | int:
        """The two exams sit on one day: a literal, or a constant."""
        if first == second:
            # Two sittings of one exam - two members of one link - always are,
            # unless the exam is off the board; that is counted as the QA does.
            if first in self._moving:
                return self.unseated[first].negated()
            return int(first in self.board.current)
        key = (first, second) if first < second else (second, first)
        if key in self._same:
            return self._same[key]
        board = self.board
        a, b = key
        value: cp_model.IntVar | int
        if a not in self._moving and b not in self._moving:
            value = int(
                a in board.current
                and b in board.current
                and board.day_of(board.current[a]) == board.day_of(board.current[b])
            )
        elif a not in self._moving or b not in self._moving:
            still, mover = (a, b) if a not in self._moving else (b, a)
            value = (
                self.on(mover, board.day_of(board.current[still])) if still in board.current else 0
            )
        else:
            shared = [
                day
                for day in range(board.day_count)
                if (a, day) in self._on and (b, day) in self._on
            ]
            if not shared:
                value = 0
            else:
                value = self.model.new_bool_var(f"same_{a}_{b}")
                for day in shared:
                    self.model.add(value >= self._on[a, day] + self._on[b, day] - 1)
        self._same[key] = value
        return value

    def _any_of(self, literals: list, cache: dict) -> cp_model.IntVar | int:
        """A literal that is true when any of ``literals`` is; shared between
        the students whose day depends on the same pairs of exams."""
        if any(isinstance(literal, int) and literal == 1 for literal in literals):
            return 1
        live = {literal.index: literal for literal in literals if not isinstance(literal, int)}
        if not live:
            return 0
        if len(live) == 1:
            return next(iter(live.values()))
        key = tuple(sorted(live))
        if key not in cache:
            any_true = self.model.new_bool_var("")
            for _index, literal in sorted(live.items()):
                self.model.add_implication(literal, any_true)
            cache[key] = any_true
        return cache[key]

    def _student_level(self, profiles: list[Profile], day_is_heavy):
        """Students over the daily limit, then with a heavy day, then with two exams a day."""
        board = self.board
        cache: dict = {}
        over_terms: list = []
        heavy_terms: list = []
        multi_terms: list = []
        total = 0
        for sits, students in profiles:
            total += students
            pairs = [
                (self._same_day(a, b), day_is_heavy([credit_a, credit_b]))
                for (a, credit_a), (b, credit_b) in itertools.combinations(sits, 2)
            ]
            multi_terms.append(students * self._any_of([same for same, _ in pairs], cache))
            # A day is heavy by its two largest credits, so by one of its pairs.
            heavy_terms.append(
                students * self._any_of([same for same, heavy in pairs if heavy], cache)
            )
            if len(sits) > board.max_per_day:
                over_terms.append(students * self._over_limit(sits))
        # Each level outweighs everything below it.
        step = total + 1
        return step * step * sum(over_terms) + step * sum(heavy_terms) + sum(multi_terms)

    def _over_limit(self, sits: Sittings) -> cp_model.IntVar | int:
        """These students sit more than the daily limit on some day."""
        board = self.board
        counts: dict[str, int] = defaultdict(int)
        for exam, _credit in sits:
            counts[exam] += 1
        literal: cp_model.IntVar | None = None
        for day in range(board.day_count):
            held = 0
            terms = []
            for exam, times in sorted(counts.items()):
                on = self.on(exam, day)
                if isinstance(on, int):
                    held += times * on
                else:
                    terms.append((times, on))
            most = held + sum(times for times, _ in terms)
            if most <= board.max_per_day:
                continue
            if held > board.max_per_day:
                return 1
            if literal is None:
                literal = self.model.new_bool_var("")
            self.model.add(
                held + sum(times * on for times, on in terms)
                <= board.max_per_day + (most - board.max_per_day) * literal
            )
        return 0 if literal is None else literal

    def _spacing_level(self, pairs: list[tuple[str, str, int]]):
        """The spacing penalty of every pair with a free exam in it."""
        board = self.board
        terms: list = []
        reach = len(_SPACING) - 1
        for a, b, count in pairs:
            if a not in self._moving and b not in self._moving:
                continue  # a constant: it is in the score, and cannot change
            if a not in self._moving or b not in self._moving:
                still, mover = (a, b) if a not in self._moving else (b, a)
                if still not in board.current:
                    continue
                held = board.day_of(board.current[still])
                for day in range(board.day_count):
                    cost = spacing_penalty(abs(day - held))
                    if cost and (mover, day) in self._on:
                        terms.append(count * cost * self._on[mover, day])
                continue
            # Both free: one literal per distance the penalty still reaches,
            # each true when the two sit within that many days of each other.
            for distance in range(1, reach + 1):
                step = spacing_penalty(distance) - spacing_penalty(distance + 1)
                if not step:
                    continue
                near = None
                for day in range(board.day_count):
                    if (a, day) not in self._on:
                        continue
                    around = [
                        self._on[b, other]
                        for other in range(day - distance, day + distance + 1)
                        if (b, other) in self._on
                    ]
                    if not around:
                        continue
                    if near is None:
                        near = self.model.new_bool_var("")
                    self.model.add(near >= self._on[a, day] + sum(around) - 1)
                if near is not None:
                    terms.append(count * step * near)
        return sum(terms)

    # ── boards ───────────────────────────────────────────────────────────

    def hint(self, placements: Mapping[str, int]) -> None:
        """Start the next solve from ``placements``."""
        self.model.clear_hints()
        for exam in self.movable:
            slot = placements.get(exam)
            seated = slot is not None and (exam, slot) in self.y
            self.model.add_hint(self.unseated[exam], int(not seated))
            for other in range(self.board.slot_count):
                if (exam, other) in self.y:
                    self.model.add_hint(self.y[exam, other], int(seated and other == slot))

    def board_of(self, solver: cp_model.CpSolver) -> dict[str, int]:
        """The whole board ``solver`` found: every other exam where it was."""
        placements = {
            exam: slot for exam, slot in self.board.current.items() if exam not in self._moving
        }
        for (exam, slot), literal in self.y.items():
            if solver.value(literal):
                placements[exam] = slot
        return placements


def _solve(built: _Model, objective, seed: int):
    """One solve of ``objective``: (solver, proven optimal), or (None, False)."""
    built.model.minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = _SOLVE_WORK
    status = solver.solve(built.model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, False
    return solver, status == cp_model.OPTIMAL


class _Search:
    """One optimisation: the board so far, and what judging it needs."""

    def __init__(self, board: Board, day_is_heavy, seed: int) -> None:
        self.board = board
        self.day_is_heavy = day_is_heavy
        self.seed = seed
        self.movable = sorted(exam for exam in board.exams if exam not in board.fixed)
        self.profiles = _profiles(board)
        self.pairs = _bucket_pairs(board)
        self._profiles_with: dict[str, list[int]] = defaultdict(list)
        for index, (sits, _students) in enumerate(self.profiles):
            for exam in sorted({exam for exam, _credit in sits}):
                self._profiles_with[exam].append(index)
        self._pairs_with: dict[str, list[int]] = defaultdict(list)
        for index, (a, b, _count) in enumerate(self.pairs):
            self._pairs_with[a].append(index)
            self._pairs_with[b].append(index)
        self.home = dict(board.current)
        self.best = dict(board.current)
        self.best_score = self.score(self.best)
        #: Set when the given board seats every exam legally: spacing may not
        #: get worse than this.
        self.spacing_cap: int | None = None
        if (
            _legal(board, self.best, self.movable)
            and self.best_score.unseated == 0
            and self.best_score.staff_excess == 0
        ):
            self.spacing_cap = self.best_score.spacing

    def score(self, placements: Mapping[str, int]) -> Score:
        return score(
            self.board,
            placements,
            self.day_is_heavy,
            _profiles_of=self.profiles,
            _pairs_of=self.pairs,
        )

    def moves(self, placements: Mapping[str, int]) -> int:
        return sum(
            self.board.weight(exam)
            for exam in self.movable
            if placements.get(exam) != self.home.get(exam)
        )

    def heat(self) -> dict[str, int]:
        """How many students each movable exam gives a day of two or more exams."""
        heat: dict[str, int] = defaultdict(int)
        moving = set(self.movable)
        for sits, students in self.profiles:
            for sat in _crowded_days(self.board, self.best, sits):
                for exam in sorted({exam for exam, _credit in sat}):
                    if exam in moving:
                        heat[exam] += students
        return heat

    def neighbourhood(self, rng: random.Random, size: int, kind: int) -> list[str]:
        """The exams the next solve frees, around an exam in the students' way
        (the more students, the likelier; any movable exam when none is).

        Kind 0: that exam, the movable exams it shares most students with, and
        the rest at random. Kind 1: every movable exam of its day and of two
        other days, so whole days can trade exams - the one way to move an exam
        when every day is at its invigilator limit. Kind 2: all at random, so an
        exam nobody blames can still make room.
        """
        heat = self.heat() if kind != 2 else {}
        if heat:
            hot = sorted(heat)
            start = rng.choices(hot, weights=[heat[exam] for exam in hot])[0]
        else:
            start = rng.choice(self.movable)
        free = [start]
        if kind == 0:
            moving = set(self.movable)
            mates = sorted(
                (mate for mate in self.board.adj.get(start, {}) if mate in moving),
                key=lambda mate: (-self.board.adj[start][mate], mate),
            )
            free.extend(mates[: size // 2])
        elif kind == 1:
            by_day: dict[int, list[str]] = defaultdict(list)
            for exam in self.movable:
                if exam in self.best and exam != start:
                    by_day[self.board.day_of(self.best[exam])].append(exam)
            own = self.board.day_of(self.best[start]) if start in self.best else None
            others = sorted(day for day in by_day if day != own)
            days = ([own] if own in by_day else []) + rng.sample(others, min(2, len(others)))
            on_days = [exam for day in days for exam in by_day[day]]
            free.extend(rng.sample(on_days, min(len(on_days), size - 1)))
        chosen = set(free)
        rest = [exam for exam in self.movable if exam not in chosen]
        free.extend(rng.sample(rest, min(len(rest), size - len(free))))
        return sorted(free[:size])

    def improve(self, free: list[str]) -> bool:
        """Solve ``free`` exactly around the board so far; True when every
        level was proven. The board so far changes only if the new one is better."""
        freed = set(free)
        sub = replace(
            self.board,
            current=self.best,
            fixed=frozenset(exam for exam in self.board.exams if exam not in freed),
        )
        touched = sorted({index for exam in free for index in self._profiles_with.get(exam, ())})
        pairs = [
            self.pairs[index]
            for index in sorted({i for exam in free for i in self._pairs_with.get(exam, ())})
        ]
        built = _Model(
            sub,
            self.day_is_heavy,
            home=self.home,
            profiles=[self.profiles[index] for index in touched],
            pairs=pairs,
        )
        if self.spacing_cap is not None and not isinstance(built.spacing, int):
            elsewhere = self.best_score.spacing - _pair_spacing(self.board, self.best, pairs)
            built.model.add(built.spacing <= self.spacing_cap - elsewhere)
        built.hint(self.best)
        found: dict[str, int] | None = None
        proven = True
        steps = 1 + sum(self.board.weight(exam) for exam in free)
        for level in (built.legal, built.students, steps * built.spacing + built.moves):
            if isinstance(level, int):
                continue  # nothing free can change this level
            if level is not built.legal and level is not built.students and found == self.best:
                # The students are no better off here, so the board stays as
                # it is: spacing and moves are tidied only behind a real gain.
                break
            solver, optimal = _solve(built, level, self.seed)
            if solver is None:
                proven = False
                break
            proven = proven and optimal
            built.model.add(level <= int(solver.value(level)))
            found = built.board_of(solver)
            built.hint(found)
        if found is None:
            return False
        found_score = self.score(found)
        if (found_score, self.moves(found)) < (self.best_score, self.moves(self.best)):
            self.best, self.best_score = found, found_score
        return proven


def optimise_board(
    board: Board,
    *,
    day_is_heavy: Callable[[list[int]], bool],
    rounds: int = DEFAULT_ROUNDS,
    seed: int = 0,
    on_round: Callable[[int, int], None] | None = None,
    neighbourhood: int = NEIGHBOURHOOD,
) -> OptimiseResult:
    """The best board found for ``board``'s movable exams in ``rounds`` solves.

    ``on_round(done, total)`` is told before each solve, for a job that reports
    its progress; it may raise to stop the optimisation, and nothing is kept.
    """
    search = _Search(board, day_is_heavy, seed)
    before = search.best_score
    result = OptimiseResult(placements=dict(board.current), before=before, after=before)
    result.movable = len(search.movable)
    if not search.movable:
        result.proven = True
        return result

    def tick(done: int, total: int) -> None:
        if on_round is not None:
            on_round(done, total)

    if len(search.movable) <= neighbourhood:
        # Small enough to solve whole: the answer is the best board there is.
        tick(0, 1)
        result.proven = search.improve(search.movable)
        result.rounds = 1
    else:
        rng = random.Random(seed)
        quiet = 0
        for number in range(rounds):
            tick(number, rounds)
            reached = (search.best_score, search.moves(search.best))
            search.improve(search.neighbourhood(rng, neighbourhood, number % 3))
            result.rounds = number + 1
            quiet = 0 if (search.best_score, search.moves(search.best)) < reached else quiet + 1
            if quiet >= _PATIENCE:
                break

    if search.best_score < before:
        result.placements = search.best
        result.after = search.best_score
        result.improved = True
        result.moved = sorted(
            exam for exam in search.movable if search.best.get(exam) != search.home.get(exam)
        )
    return result
