"""Generate: bring a teacher on five working days down to four, when the board allows it.

The day-off policy (``timetable_optimisation_policy``) keeps every teacher's week at four days or
fewer where possible, but Generate does not choose its times with teachers in mind: the layout is
built first and the teachers are picked on it, and the improvement stages then trim five-day
weeks only as far as a single section move or a bounded chain-2 neighbourhood (a handful of
alternative patterns per section) reaches. A teacher whose way to four days is a PAIR of his own
sections moving together, to patterns the chain search never lists, stays on five.

This pass runs once, on the winning board, after the improvement stages and before the board is
stored. For each teacher on five days, in order, it enumerates every way of moving ONE, then TWO,
of his own campus sections to another pattern of their family (same meeting lengths) that leaves
his days at four or fewer, smallest disturbance first, and evaluates them with the project's own
evaluator under the run's day-off policy. A move is kept only when

* the policy admits it (``evaluation_is_better`` inside the run's ``day_off_scope``): the four
  hard student positions no worse, no new breach, no fifth day anywhere, no hard-rule or
  teaching-time violation added (the teacher going from five days to four is what makes the
  move a gain; the policy ranks it above the soft part of the student score);
* the teacher's days really fall; and
* no other teacher's working days rise (a teacher of a moved section keeps the days he had).

The pass is bounded by counts, never by the clock: ``MAX_EVALUATIONS`` student evaluations
(shared out between the teachers still on five days) and ``MAX_CONSIDERED`` candidates tried; the
board it ends on is a function of the board it starts from. A wall-clock ceiling
(``CEILING_SECONDS``) only stops a machine too slow to finish, and the run says so (the stop is
recorded as a wall ceiling, "not repeatable"). It never blocks and never fails the run: when no
move qualifies the board is returned as it was, and the report says so.
"""

from __future__ import annotations

import heapq
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from core.services.instructor_availability import EMPTY_AVAILABILITY, InstructorAvailability
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    SectionState,
    StudentProfile,
    TimetableEvaluationResult,
    TimetableMove,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_constraints import (
    before_meetings_of,
    move_adds_availability_violation,
    move_exceeds_instructor_daily_cap,
    move_introduces_day_run,
    move_introduces_instructor_clash,
    move_introduces_same_course_overlap,
)
from core.services.timetable_flags import is_section_day_spread_enabled
from core.services.timetable_optimisation_policy import (
    MAX_WORKING_DAYS,
    evaluation_is_better,
    instructor_schedule_metrics,
    schedule_metrics_of,
)
from core.services.timetable_pr4_instructor import (
    get_instructor_daily_cap,
    is_instructor_clash_enabled,
    is_instructor_daily_cap_enabled,
)
from core.services.timetable_room_repair import PatternNotInCatalog, apply_move_to_grid
from core.services.timetable_work_budget import (
    COMPLETED,
    WALL_CEILING,
    WORK_LIMIT,
    record_search_stop,
)

logger = logging.getLogger(__name__)

#: Student evaluations the whole pass may spend (each is one full student-assignment evaluation).
MAX_EVALUATIONS = 300
#: Candidates the whole pass may try (apply, gate, evaluate or refuse), evaluated or not.
MAX_CONSIDERED = 20_000
#: Candidates of one size kept per teacher, smallest disturbance first (bounds memory).
MAX_CANDIDATES = 20_000
#: Hard ceiling on the pass for a machine too slow to finish. Production runs one planner job
#: at a time and never stops a running job, so every stage needs a limit of its own.
CEILING_SECONDS = 120.0
#: The checkpoint (job cancellation) and the clock are looked at every this many candidates.
CHECK_EVERY = 25
#: The most sections moved together. One section alone, or two.
MAX_MOVED_SECTIONS = 2

Candidate = list[tuple[SectionState, CanonicalPattern]]


def _is_campus(section: SectionState) -> bool:
    return not (section.is_online or section.delivery_is_online)


def _days_by_teacher(evaluation: TimetableEvaluationResult) -> dict[str, int]:
    """Working days per teacher of an evaluation, string keys."""
    days = schedule_metrics_of(evaluation).get("working_days_by_instructor") or {}
    return {str(i): int(d) for i, d in days.items()}


def _teacher_order(teacher: str) -> tuple[int, int, str]:
    """Numeric ids in numeric order ("9" before "10"), anything else after them."""
    return (0, int(teacher), teacher) if teacher.isdigit() else (1, 0, teacher)


def _moves_of(candidate: Candidate) -> list[TimetableMove]:
    return [
        TimetableMove(
            move_type="repattern",
            section_id_a=section.section_id,
            from_pattern_id_a=section.pattern_id,
            to_pattern_id_a=pattern.pattern_id,
        )
        for section, pattern in candidate
    ]


def _alternatives(
    section: SectionState, pattern_catalog: dict[str, list[CanonicalPattern]]
) -> list[CanonicalPattern]:
    """Every other pattern of the section's family with the same meeting lengths."""
    durations = sorted(m.end_min - m.start_min for m in section.meetings)
    return [
        pattern
        for pattern in pattern_catalog.get(section.pattern_family, [])
        if pattern.pattern_id != section.pattern_id
        and pattern.pattern_family == section.pattern_family
        and sorted(m.end_min - m.start_min for m in pattern.meetings) == durations
    ]


def _disturbance(section: SectionState, pattern: CanonicalPattern) -> int:
    """How many of the pattern's meetings are not where the section's meetings are now."""
    here = {(m.day, m.start_min, m.end_min) for m in section.meetings}
    return sum(1 for m in pattern.meetings if (m.day, m.start_min, m.end_min) not in here)


def _order(candidate: Candidate) -> tuple:
    return (
        sum(_disturbance(section, pattern) for section, pattern in candidate),
        tuple((section.section_id, pattern.pattern_id) for section, pattern in candidate),
    )


def teachers_on_five_days(
    sections: Iterable[SectionState], section_instructor_ids: dict[str, frozenset[int]]
) -> list[str]:
    """The teachers working more than ``MAX_WORKING_DAYS`` days on ``sections``, in id order."""
    days = instructor_schedule_metrics(sections, section_instructor_ids).get(
        "working_days_by_instructor"
    )
    return sorted(
        (str(i) for i, d in (days or {}).items() if d > MAX_WORKING_DAYS), key=_teacher_order
    )


def _smallest(candidates: Iterator[Candidate], counted: list[int]) -> list[Candidate]:
    """The ``MAX_CANDIDATES`` smallest-disturbance candidates (sorted), counting every one."""

    def counting() -> Iterator[Candidate]:
        for candidate in candidates:
            counted[0] += 1
            yield candidate

    return heapq.nsmallest(MAX_CANDIDATES, counting(), key=_order)


def _candidates(
    teacher: str,
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    locked: set[str],
    days_now: dict[str, int],
) -> tuple[list[Candidate], list[Candidate], dict[str, int]]:
    """Moves of one, then two, of the teacher's campus sections that leave him on at most
    ``MAX_WORKING_DAYS`` days and raise no other teacher's days, smallest disturbance first.

    Returns ``(singles, pairs, totals)``: the kept candidates (at most ``MAX_CANDIDATES`` of
    each size) and how many of each size qualified in all.
    """
    by_teacher: dict[str, list[str]] = defaultdict(list)
    for sid, section in sections_by_id.items():
        if _is_campus(section):
            for iid in section_instructor_ids.get(sid, ()):
                by_teacher[str(iid)].append(sid)
    base_days = {
        sid: frozenset(m.day for m in section.meetings) for sid, section in sections_by_id.items()
    }
    mine_all = by_teacher.get(teacher, [])

    def rest_of_week(moved: set[str]) -> frozenset[int]:
        """The teacher's days from the sections that stay where they are."""
        days: set[int] = set()
        for sid in mine_all:
            if sid not in moved:
                days |= base_days[sid]
        return frozenset(days)

    def others_keep_their_days(moved: dict[str, CanonicalPattern]) -> bool:
        affected = {
            str(o)
            for sid in moved
            for o in section_instructor_ids.get(sid, ())
            if str(o) != teacher
        }
        for person in affected:
            days: set[int] = set()
            for sid in by_teacher.get(person, ()):
                days |= moved[sid].days_used if sid in moved else base_days[sid]
            if len(days) > days_now.get(person, 0):
                return False
        return True

    movable = sorted(sid for sid in mine_all if sid not in locked)
    alternatives = {sid: _alternatives(sections_by_id[sid], pattern_catalog) for sid in movable}
    movable = [sid for sid in movable if alternatives[sid]]

    def singles() -> Iterator[Candidate]:
        for sid in movable:
            rest = rest_of_week({sid})
            for pattern in alternatives[sid]:
                if len(rest | pattern.days_used) <= MAX_WORKING_DAYS and others_keep_their_days(
                    {sid: pattern}
                ):
                    yield [(sections_by_id[sid], pattern)]

    def pairs() -> Iterator[Candidate]:
        for i, first in enumerate(movable):
            for second in movable[i + 1 :]:
                rest = rest_of_week({first, second})
                for p1 in alternatives[first]:
                    with_first = rest | p1.days_used
                    if len(with_first) > MAX_WORKING_DAYS:
                        continue
                    for p2 in alternatives[second]:
                        if len(with_first | p2.days_used) <= MAX_WORKING_DAYS and (
                            others_keep_their_days({first: p1, second: p2})
                        ):
                            yield [(sections_by_id[first], p1), (sections_by_id[second], p2)]

    totals = {"single": 0, "pair": 0}
    counted = [0]
    one = _smallest(singles(), counted)
    totals["single"] = counted[0]
    counted[0] = 0
    two = _smallest(pairs(), counted) if MAX_MOVED_SECTIONS >= 2 else []
    totals["pair"] = counted[0]
    return one, two, totals


def _apply(
    moves: list[TimetableMove],
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
) -> list[Any]:
    """Apply the moves in order; roll back and re-raise when one fails."""
    snapshots: list[Any] = []
    try:
        for move in moves:
            snapshots.append(apply_move_to_grid(move, sections_by_id, pattern_catalog))
    except Exception:
        _rollback(snapshots, sections_by_id)
        raise
    return snapshots


def _rollback(snapshots: list[Any], sections_by_id: dict[str, SectionState]) -> None:
    for snap in reversed(snapshots):
        for s in snap.snapshots:
            section = sections_by_id[s.section_id]
            section.pattern_id = s.old_pattern_id
            section.meetings = list(s.old_meetings)
            section.assigned_room_id = s.old_room_id


def _breaks_a_hard_rule(
    snapshots: list[Any],
    moved_ids: set[str],
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]],
    availability: InstructorAvailability,
) -> bool:
    """The structural gates every optional move passes (the chain search's own)."""
    if move_introduces_same_course_overlap(sections_by_id, moved_ids):
        return True
    before = before_meetings_of([s for snap in snapshots for s in snap.snapshots])
    if is_section_day_spread_enabled() and move_introduces_day_run(sections_by_id, before):
        return True
    if availability and move_adds_availability_violation(
        sections_by_id, section_instructor_ids, availability, before
    ):
        return True
    if is_instructor_daily_cap_enabled() and move_exceeds_instructor_daily_cap(
        sections_by_id, section_instructor_ids, get_instructor_daily_cap(), moved_ids
    ):
        return True
    return bool(
        is_instructor_clash_enabled()
        and move_introduces_instructor_clash(sections_by_id, section_instructor_ids, moved_ids)
    )


def repair_five_day_teachers(
    *,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
    student_profiles: dict[str, StudentProfile],
    course_rigidity: dict[str, float],
    section_instructor_ids: dict[str, frozenset[int]] | None,
    locked_section_ids: set[str] | None = None,
    course_tiers: dict[str, str] | None = None,
    availability: InstructorAvailability = EMPTY_AVAILABILITY,
    max_evaluations: int = MAX_EVALUATIONS,
    max_considered: int = MAX_CONSIDERED,
    ceiling_seconds: float = CEILING_SECONDS,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[TimetableEvaluationResult | None, dict[str, Any]]:
    """One bounded pass over the teachers on five days (see the module docstring).

    ``sections_by_id`` is the evaluated winner's board, mutated in place when a move is kept.
    ``checkpoint`` (the job's cancellation check) is called every ``CHECK_EVERY`` candidates and
    is the one thing allowed to raise out of the pass. Returns ``(evaluation, report)``: the
    evaluation of the board as it now stands, or None when nothing was evaluated (no teacher on
    five days, or no teachers at all) and the board is untouched.
    """
    report: dict[str, Any] = {"before": [], "after": [], "teachers": [], "evaluations": 0}
    if not section_instructor_ids or not sections_by_id:
        return None, report
    five = teachers_on_five_days(sections_by_id.values(), section_instructor_ids)
    report["before"] = list(five)
    report["after"] = list(five)
    if not five:
        return None, report

    started = time.perf_counter()
    locked = set(locked_section_ids or ())

    def evaluate() -> TimetableEvaluationResult:
        return evaluate_generated_timetable_candidate(
            candidate_id="five_day_test",
            generated_sections=list(sections_by_id.values()),
            student_profiles=student_profiles,
            course_rigidity=course_rigidity,
            section_instructor_ids=section_instructor_ids,
            course_tiers=course_tiers,
        )

    current = evaluate()
    evaluations = considered = 0
    stop = COMPLETED
    for index, teacher in enumerate(five):
        if stop != COMPLETED:
            break
        days_now = _days_by_teacher(current)
        if days_now.get(teacher, 0) <= MAX_WORKING_DAYS:
            continue  # an earlier repair already brought him down
        singles, pairs, totals = _candidates(
            teacher, sections_by_id, section_instructor_ids, pattern_catalog, locked, days_now
        )
        # What is left of the evaluations is shared out between the teachers still to come.
        share = -(-(max_evaluations - evaluations) // (len(five) - index))
        spent = 0
        entry: dict[str, Any] = {
            "instructor_id": teacher,
            "days_before": days_now[teacher],
            "candidates": totals,
            "evaluated": 0,
            "skipped_not_in_catalog": 0,
            "skipped_hard_rule": 0,
            "kept": None,
        }
        report["teachers"].append(entry)
        for tier_candidates in (singles, pairs):
            best: TimetableEvaluationResult | None = None
            best_candidate: Candidate | None = None
            best_days: dict[str, int] = {}
            for candidate in tier_candidates:
                if spent >= share:
                    break
                if considered >= max_considered:
                    stop = WORK_LIMIT
                    break
                if considered % CHECK_EVERY == 0:
                    if checkpoint is not None:
                        checkpoint()
                    if time.perf_counter() - started > ceiling_seconds:
                        stop = WALL_CEILING
                        break
                considered += 1
                moves = _moves_of(candidate)
                try:
                    snapshots = _apply(moves, sections_by_id, pattern_catalog)
                except (PatternNotInCatalog, KeyError):
                    entry["skipped_not_in_catalog"] += 1
                    continue
                moved_ids = {section.section_id for section, _pattern in candidate}
                try:
                    if _breaks_a_hard_rule(
                        snapshots, moved_ids, sections_by_id, section_instructor_ids, availability
                    ):
                        entry["skipped_hard_rule"] += 1
                        continue
                    test = evaluate()
                    evaluations += 1
                    spent += 1
                    entry["evaluated"] += 1
                    after_days = _days_by_teacher(test)
                    if (
                        after_days.get(teacher, days_now[teacher]) < days_now[teacher]
                        and all(after_days.get(o, 0) <= d for o, d in days_now.items())
                        and evaluation_is_better(
                            test,
                            best or current,
                            day_ceilings=schedule_metrics_of(current),
                            feasibility_ceilings=current,
                        )
                    ):
                        best, best_candidate, best_days = test, candidate, after_days
                finally:
                    _rollback(snapshots, sections_by_id)
            if best is not None and best_candidate is not None:
                _apply(_moves_of(best_candidate), sections_by_id, pattern_catalog)
                entry["kept"] = {
                    "moves": [
                        {"section_id": section.section_id, "to_pattern_id": pattern.pattern_id}
                        for section, pattern in best_candidate
                    ],
                    "days_after": best_days[teacher],
                    "score_before": list(current.lexicographic_score),
                    "score_after": list(best.lexicographic_score),
                    "unresolved_before": len(current.unresolved_student_ids),
                    "unresolved_after": len(best.unresolved_student_ids),
                }
                current = best
                break
            if stop != COMPLETED or spent >= share:
                break

    report["evaluations"] = evaluations
    report["considered"] = considered
    report["after"] = teachers_on_five_days(sections_by_id.values(), section_instructor_ids)
    if stop == COMPLETED and evaluations >= max_evaluations:
        stop = WORK_LIMIT
    record_search_stop(
        "five_day_repair",
        stop,
        work=considered,
        work_limit=max_considered,
        seconds=time.perf_counter() - started,
        ceiling_seconds=ceiling_seconds,
        detail={"evaluations": evaluations, "max_evaluations": max_evaluations},
    )
    return current, report
