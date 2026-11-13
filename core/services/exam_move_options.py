"""Where else one exam could sit, and what each place would do to the board.

Clicking an exam on the exam timetable lists the other open periods it could
move to, best first. Each place is judged the way "Optimize" judges a board
(``exam_optimise.score``), level by level and in the same order, with one
level added for the seats - Optimize does not count them:

  1. rule breaks: a student's two exams in one period, two exams of one
     (programme, term) on one day;
  2. exams left in OVERFLOW;
  3. students short of an exam seat in a period, counted by cohort over the
     whole room inventory (``exam_add_courses.seat_demand``/``seat_capacity``,
     the seats Add courses counts): a shortfall is certain, a fit is not - the
     rooming decides, and Check after the move rooms the board for real;
  4. invigilators over the busiest day the board has now (an estimate from
     the rooms the saved run gave each exam, as Optimize estimates them);
  5. students over the exams-per-day limit;
  6. students with a heavy-credit day;
  7. students with two or more exams on one day;
  8. the spacing penalty between exams of one (programme, term).

A place is "better" when the board with the exam there is better at the first
level that differs, "same" when no level differs, and "worse" otherwise.
Nothing is moved and nothing is saved: the registrar picks a place, the page
moves the exam exactly as a drag would, and Check counts the result.

Only the students who sit the exam and the (programme, term) pairs holding it
can change levels 5 to 8, so those are counted for them alone and added to the
whole board's count; levels 1 to 4 are counted over the whole board for every
place. A place costs about a millisecond on the real 172-exam board.

A linked exam is one exam here, as it is for Optimize: the board's units.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from core.services.exam_optimise import Board, Score, _bucket_pairs, _profiles, score

#: The levels a place is judged by, most important first.
LEVELS = (
    "rule_breaks",
    "unseated",
    "seats_short",
    "staff_excess",
    "over_limit",
    "heavy_day",
    "multi_exam_day",
    "spacing",
)

BETTER = "better"
SAME = "same"
WORSE = "worse"


class ExamNotMovable(ValueError):
    """The exam is pinned or locked: no other place is open to it."""


@dataclass(frozen=True)
class Seats:
    """Exam seats per cohort, and the students each exam seats per cohort.

    ``need`` is keyed by the board's exams (a link's members summed).
    A cohort with no seats at all - a section of unknown gender - is never
    roomed anywhere, so moving it changes nothing and it is not counted.
    """

    capacity: Mapping[str, int]
    need: Mapping[str, Mapping[str, int]]

    def short(self, load: Mapping[str, int]) -> int:
        """Students one period cannot seat, whatever the rooming does."""
        return sum(
            max(0, int(load.get(cohort, 0)) - int(seats)) for cohort, seats in self.capacity.items()
        )

    def loads(self, placements: Mapping[str, int]) -> dict[int, dict[str, int]]:
        loads: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for exam, slot in placements.items():
            for cohort, students in (self.need.get(exam) or {}).items():
                loads[slot][cohort] += int(students)
        return loads


@dataclass(frozen=True)
class MoveOption:
    """One place for the exam: the board's levels with it there, and how they changed."""

    slot: int
    verdict: str
    levels: dict[str, int]
    changes: dict[str, int]
    #: Exams in that period sharing students with this one: (exam, students),
    #: most shared first. Each is a rule break.
    clashes_with: tuple[tuple[str, int], ...] = ()
    #: Exams of one of its (programme, term)s on that day. Each day is a rule break.
    same_day_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoveOptions:
    exam: str
    #: The board's levels as it is.
    current: dict[str, int]
    #: Every open place but the exam's own, best first.
    options: list[MoveOption]


def _levels(found: Score, seats_short: int) -> dict[str, int]:
    levels = found.as_dict()
    levels["seats_short"] = seats_short
    return {level: levels[level] for level in LEVELS}


def _key(levels: Mapping[str, int]) -> tuple[int, ...]:
    return tuple(levels[level] for level in LEVELS)


def rank_move_options(
    board: Board,
    exam: str,
    placements: Mapping[str, int],
    day_is_heavy: Callable[[list[int]], bool],
    *,
    seats: Seats | None = None,
) -> MoveOptions:
    """Every open place ``exam`` could move to on ``placements``, best first.

    ``placements`` is the board as the registrar sees it: seated exams only,
    so an exam in OVERFLOW is absent. A pinned or locked exam has no other
    place (``ExamNotMovable``); a locked cell is no place for any exam.
    """
    if exam not in board.exams:
        raise ValueError(f"Exam {exam} is not on this board.")
    if exam in board.fixed:
        raise ExamNotMovable(
            "This exam is pinned or in a locked cell, so it stays where it is. "
            "Unpin or unlock it to see other times."
        )
    here = placements.get(exam)
    near_profiles = [
        profile for profile in _profiles(board) if any(sat == exam for sat, _credit in profile[0])
    ]
    near_pairs = [pair for pair in _bucket_pairs(board) if exam in pair[:2]]
    whole = score(board, placements, day_is_heavy)
    near = score(board, placements, day_is_heavy, _profiles_of=near_profiles, _pairs_of=near_pairs)

    loads = seats.loads(placements) if seats else {}
    need = (seats.need.get(exam) or {}) if seats else {}
    seats_short = sum(seats.short(load) for load in loads.values()) if seats else 0
    current = _levels(whole, seats_short)

    def seat_change(slot: int) -> int:
        if not seats or not need:
            return 0
        change = 0
        for where, sign in ((slot, 1), (here, -1)):
            if where is None:
                continue
            load = loads.get(where, {})
            after = {
                cohort: load.get(cohort, 0) + sign * need.get(cohort, 0)
                for cohort in seats.capacity
            }
            change += seats.short(after) - seats.short(load)
        return change

    mates = board.adj.get(exam, {})
    bucket_mates = sorted(
        {mate for members in board.buckets.values() if exam in members for mate in members} - {exam}
    )
    options: list[MoveOption] = []
    for slot in range(board.slot_count):
        if slot == here or slot in board.closed_slots:
            continue
        moved = {**placements, exam: slot}
        local = score(board, moved, day_is_heavy, _profiles_of=near_profiles, _pairs_of=near_pairs)
        # Levels 1-4 are the whole board's already; 5-8 change only for the
        # students and pairs counted in ``local``.
        found = Score(
            rule_breaks=local.rule_breaks,
            unseated=local.unseated,
            staff_excess=local.staff_excess,
            over_limit=whole.over_limit - near.over_limit + local.over_limit,
            heavy_day=whole.heavy_day - near.heavy_day + local.heavy_day,
            multi_exam_day=whole.multi_exam_day - near.multi_exam_day + local.multi_exam_day,
            spacing=whole.spacing - near.spacing + local.spacing,
        )
        levels = _levels(found, seats_short + seat_change(slot))
        key, now = _key(levels), _key(current)
        verdict = BETTER if key < now else SAME if key == now else WORSE
        day = board.day_of(slot)
        options.append(
            MoveOption(
                slot=slot,
                verdict=verdict,
                levels=levels,
                changes={level: levels[level] - current[level] for level in LEVELS},
                clashes_with=tuple(
                    sorted(
                        (
                            (mate, int(shared))
                            for mate, shared in mates.items()
                            if moved.get(mate) == slot
                        ),
                        key=lambda item: (-item[1], item[0]),
                    )
                ),
                same_day_with=tuple(
                    mate
                    for mate in bucket_mates
                    if mate in moved and board.day_of(moved[mate]) == day
                ),
            )
        )
    options.sort(key=lambda option: (_key(option.levels), option.slot))
    return MoveOptions(exam=exam, current=current, options=options)
