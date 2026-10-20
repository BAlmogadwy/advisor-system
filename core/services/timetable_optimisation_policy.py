"""Classic Generate/Optimise selection policy, separate from public score layouts.

The 6/7/9-element student score remains a reporting contract. Hard student
coverage is protected component by component; instructor campus working days
and actual idle minutes then lead the quality decision.

With ``TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY`` the strict per-instructor day and
idle veto gives way to the owner's day-off rulings R1-R5 (2026-10-19): see
``DayOffPolicy`` at the end of this module.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import inf, isfinite
from typing import Any, ClassVar

from django.conf import settings

from core.services.timetable_assignment_models import SectionState, TimetableEvaluationResult


def is_instructor_priority_enabled() -> bool:
    return bool(getattr(settings, "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED", True))


def instructor_schedule_metrics(
    sections: Iterable[SectionState],
    section_instructor_ids: Mapping[str, frozenset[int]] | None,
) -> dict:
    """Count campus days and gaps in the UNION of each instructor's meetings.

    Overlapping/nested sessions must not create phantom idle time. Online
    sections neither create nor bridge a campus gap, matching the evaluator.
    String instructor keys survive JSON persistence without changing identity.
    """
    intervals: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    instructors = {str(i) for ids in (section_instructor_ids or {}).values() for i in ids}
    for section in sections:
        if section.delivery_is_online or section.is_online:
            continue
        for instructor in (section_instructor_ids or {}).get(section.section_id, ()):
            for meeting in section.meetings:
                intervals[(str(instructor), meeting.day)].append(
                    (meeting.start_min, meeting.end_min)
                )
    days = dict.fromkeys(sorted(instructors), 0)
    idle_by_instructor = dict.fromkeys(sorted(instructors), 0)
    for (instructor_key, _day), meetings in intervals.items():
        days[instructor_key] += 1
        end = None
        for start, finish in sorted(meetings):
            if end is not None:
                idle_by_instructor[instructor_key] += max(0, start - end)
            end = max(end or 0, finish)
    return {
        "working_days": sum(days.values()),
        "idle_minutes": sum(idle_by_instructor.values()),
        "idle_minutes_by_instructor": idle_by_instructor,
        "working_days_by_instructor": days,
        "affected_instructors": len(instructors),
    }


def schedule_metrics_of(evaluation: TimetableEvaluationResult) -> dict:
    return (evaluation.quality_score or {}).get("instructor_schedule") or {}


def instructor_focus_courses(
    sections: Iterable[SectionState],
    section_instructor_ids: Mapping[str, frozenset[int]] | None,
    limit: int = 10,
) -> list[str]:
    """Target busy instructor weeks even when student seating has no hotspots."""
    if not is_instructor_priority_enabled() or not section_instructor_ids:
        return []
    by_instructor = defaultdict(list)
    for section in sections:
        if not (section.delivery_is_online or section.is_online):
            for instructor in section_instructor_ids.get(section.section_id, ()):
                by_instructor[instructor].append(section)
    course_priorities: dict[str, tuple[int, int]] = {}
    for instructor, taught in by_instructor.items():
        metrics = instructor_schedule_metrics(
            taught, {s.section_id: frozenset({instructor}) for s in taught}
        )
        priority = (metrics["working_days"], metrics["idle_minutes"])
        for section in taught:
            course_priorities[section.course_code] = max(
                priority, course_priorities.get(section.course_code, (0, 0))
            )
    return sorted(
        course_priorities,
        key=lambda course: (-course_priorities[course][0], -course_priorities[course][1], course),
    )[: max(0, limit)]


def evaluation_order_key(evaluation: TimetableEvaluationResult) -> tuple:
    """Rank comparable evaluations without changing their serialized scores."""
    score = tuple(evaluation.lexicographic_score)
    metrics = schedule_metrics_of(evaluation)
    if not is_instructor_priority_enabled() or not metrics.get("affected_instructors"):
        return score
    return (
        score[:4]
        + (
            int(metrics["working_days"]),
            int(metrics["idle_minutes"]),
        )
        + score[4:]
    )


def instructor_day_regressions(before: Mapping, after: Mapping) -> list[dict]:
    """No instructor may pay for another instructor's shorter working week."""
    before_days = before.get("working_days_by_instructor") or {}
    after_days = after.get("working_days_by_instructor") or {}
    return [
        {
            "metric": "instructor_working_days",
            "label": f"Instructor {instructor} working days",
            "instructor_id": str(instructor),
            "before": int(days),
            "after": int(after_days.get(str(instructor), after_days.get(instructor, 0))),
            "delta": int(after_days.get(str(instructor), after_days.get(instructor, 0)))
            - int(days),
        }
        for instructor, days in before_days.items()
        if int(after_days.get(str(instructor), after_days.get(instructor, 0))) > int(days)
    ]


def _measured_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _instructor_metric_regressions(
    before: Mapping,
    after: Mapping,
    *,
    total_key: str,
    individual_key: str,
    metric: str,
    label: str,
) -> list[dict]:
    before_values = {str(i): value for i, value in (before.get(individual_key) or {}).items()}
    after_values = {str(i): value for i, value in (after.get(individual_key) or {}).items()}
    required = {
        str(i)
        for metrics in (before, after)
        for key in ("working_days_by_instructor", "idle_minutes_by_instructor")
        for i in (metrics.get(key) or {})
    }
    regressions = []
    for instructor in sorted(required):
        old = _measured_nonnegative_int(before_values.get(instructor))
        new = _measured_nonnegative_int(after_values.get(instructor))
        if old is None or new is None:
            regressions.append(
                {
                    "metric": f"{metric}_evidence",
                    "label": f"Instructor {instructor} {label} evidence missing",
                    "instructor_id": instructor,
                    "before": old,
                    "after": new,
                    "delta": None,
                }
            )
        elif new > old:
            regressions.append(
                {
                    "metric": metric,
                    "label": f"Instructor {instructor} {label}",
                    "instructor_id": instructor,
                    "before": old,
                    "after": new,
                    "delta": new - old,
                }
            )
    old_total = _measured_nonnegative_int(before.get(total_key))
    new_total = _measured_nonnegative_int(after.get(total_key))
    total_metric = metric.replace("instructor_", "instructor_total_", 1)
    known_teachers = (
        required or before.get("affected_instructors") or after.get("affected_instructors")
    )
    complete_totals = all(
        _measured_nonnegative_int(metrics.get("affected_instructors")) == len(required)
        and all(_measured_nonnegative_int(values.get(i)) is not None for i in required)
        and total == sum(values[i] for i in required)
        for metrics, values, total in (
            (before, before_values, old_total),
            (after, after_values, new_total),
        )
    )
    if known_teachers and (
        not required or old_total is None or new_total is None or not complete_totals
    ):
        regressions.append(
            {
                "metric": f"{total_metric}_evidence",
                "label": f"Total instructor {label} evidence missing",
                "before": old_total,
                "after": new_total,
                "delta": None,
            }
        )
    elif old_total is not None and new_total is not None and new_total > old_total:
        regressions.append(
            {
                "metric": total_metric,
                "label": f"Total instructor {label}",
                "before": old_total,
                "after": new_total,
                "delta": new_total - old_total,
            }
        )
    return regressions


def instructor_idle_regressions(before: Mapping, after: Mapping) -> list[dict]:
    """Protect every teacher's union idle and its total; unknown is not zero.

    Independent of the priority flag so secondary quality passes can enforce
    an explicitly requested no-regression contract even in legacy mode.
    """
    return _instructor_metric_regressions(
        before,
        after,
        total_key="idle_minutes",
        individual_key="idle_minutes_by_instructor",
        metric="instructor_idle_minutes",
        label="idle minutes",
    )


def instructor_schedule_regressions(before: Mapping, after: Mapping) -> list[dict]:
    """Strict day and idle ceilings for optional changes, including evidence."""
    return _instructor_metric_regressions(
        before,
        after,
        total_key="working_days",
        individual_key="working_days_by_instructor",
        metric="instructor_working_days",
        label="working days",
    ) + instructor_idle_regressions(before, after)


def evaluation_is_better(
    candidate: TimetableEvaluationResult,
    incumbent: TimetableEvaluationResult,
    *,
    day_ceilings: Mapping | None = None,
    idle_ceilings: Mapping | None = None,
    feasibility_ceilings: TimetableEvaluationResult | None = None,
) -> bool:
    """Accept quality gains only inside hard coverage, individual day and idle gates.

    The default ceilings ratchet against the incumbent. A caller comparing
    alternative moves in one iteration must pin feasibility to that iteration's
    board with ``feasibility_ceilings=current`` and its day metrics with
    ``day_ceilings=schedule_metrics_of(current)``. Otherwise a tentative winner
    could tighten a hard ceiling and reject a stronger valid alternative solely
    because of enumeration order. Ranking always compares against incumbent.

    Inside a run's ``day_off_scope`` the day-off policy's per-move relation
    decides instead, with ``feasibility_ceilings`` (else the incumbent) as the
    board in hand; the ``day_ceilings``/``idle_ceilings`` vetoes do not apply.
    """
    policy = current_day_off_policy()
    if policy is not None:
        base = feasibility_ceilings if feasibility_ceilings is not None else incumbent
        return policy.step(candidate, incumbent, base=base)
    if not is_instructor_priority_enabled():
        return bool(candidate.lexicographic_score < incumbent.lexicographic_score)
    feasibility = feasibility_ceilings if feasibility_ceilings is not None else incumbent
    old_score, new_score = feasibility.lexicographic_score, candidate.lexicographic_score
    if len(old_score) != len(new_score) or len(incumbent.lexicographic_score) != len(new_score):
        return False
    if any(new > old for old, new in zip(old_score[:4], new_score[:4], strict=True)):
        return False
    if candidate.instructor_overload_count > feasibility.instructor_overload_count:
        return False
    candidate_metrics = schedule_metrics_of(candidate)
    incumbent_metrics = schedule_metrics_of(incumbent)
    ceiling_metrics = day_ceilings if day_ceilings is not None else incumbent_metrics
    idle_metrics = idle_ceilings if idle_ceilings is not None else ceiling_metrics
    # Missing instructor evidence is not evidence of a zero-day/zero-idle board.
    # All evaluations within a search must retain the same resolved teachers.
    required_metrics = (
        incumbent_metrics,
        ceiling_metrics,
        idle_metrics,
        schedule_metrics_of(feasibility),
    )
    required_instructors = {
        str(instructor)
        for metrics in required_metrics
        for instructor in (metrics.get("working_days_by_instructor") or {})
    }
    candidate_instructors = {
        str(instructor)
        for instructor in (candidate_metrics.get("working_days_by_instructor") or {})
    }
    if any(metrics.get("affected_instructors") for metrics in required_metrics) and (
        not candidate_metrics.get("affected_instructors")
        or not {"working_days", "idle_minutes"}.issubset(candidate_metrics)
        or not required_instructors.issubset(candidate_instructors)
    ):
        return False
    if instructor_day_regressions(ceiling_metrics, candidate_metrics):
        return False
    if instructor_idle_regressions(idle_metrics, candidate_metrics):
        return False
    return evaluation_order_key(candidate) < evaluation_order_key(incumbent)


# ── Day-off policy: the owner's rulings R1-R5 (2026-10-19) ───────────────────
#
# R1 The teaching week is SUN-THU. Every instructor keeps at least one day off
#    wherever possible (at most 4 working days); no optional change ADDS a 5th.
# R2 A SPARE day off (an instructor on 3 working days or fewer) may be given up,
#    never above 4 days, only for a student gain: the full score strictly better
#    and the four hard positions each no worse.
# R3 Whoever gives up a spare day may gain at most 60 idle minutes per day given
#    up, against the run's start, cumulatively and absolutely.
# R4 One instructor's idle may rise only if the TOTAL instructor idle strictly
#    falls. This replaces the strict per-instructor idle ceiling.
# R5 Compaction spends student gap only on a FIRST day off (5 -> 4 days); see
#    ``timetable_instructor_compaction``. Likewise the day trade's idle
#    allowance (255) covers only an instructor's first day off; a second or
#    third day off may raise their idle only under R4 (owner, 2026-10-20).
# Days and idle are judged per person against the run's start A, so a change of
# who teaches what between stages is judged on the same ledger.

TEACHING_WEEK = 5  # R1: SUN-THU
MAX_WORKING_DAYS = 4  # R1: at least one day off
SPARE_DAY_IDLE = 60  # R3: idle minutes per spare day given up
_HARD = 4  # the hard student positions, each a gate


def is_day_off_policy_active() -> bool:
    """The day-off rulings replace the strict veto: the flag AND instructor priority."""
    return (
        bool(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY", False))
        and is_instructor_priority_enabled()
    )


def _count(value: object) -> int | None:
    """A measured count: a non-negative int (a bool is not one)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _maps(raw_days: object, raw_idle: object) -> tuple[dict[str, int], dict[str, int]] | None:
    """Days and idle by instructor (string keys), or None: a non-int, bool or
    negative value, two keys for one instructor, or different instructors."""
    if not isinstance(raw_days, Mapping) or not isinstance(raw_idle, Mapping):
        return None
    days: dict[str, int] = {}
    idle: dict[str, int] = {}
    for target, raw in ((days, raw_days), (idle, raw_idle)):
        for teacher, value in raw.items():
            count = _count(value)
            if count is None or str(teacher) in target:
                return None
            target[str(teacher)] = count
    return (days, idle) if set(days) == set(idle) else None


def _per_teacher(metrics: object) -> tuple[dict[str, int], dict[str, int]] | None:
    """Validated ``(days, idle)`` of one schedule measurement, or None when its
    evidence is incomplete (see ``_maps``), a total is not its per-instructor
    sum, or the head count is not the number of instructors."""
    if not isinstance(metrics, Mapping):
        return None
    maps = _maps(
        metrics.get("working_days_by_instructor"), metrics.get("idle_minutes_by_instructor")
    )
    if maps is None:
        return None
    days, idle = maps
    if _count(metrics.get("working_days")) != sum(days.values()):
        return None
    if _count(metrics.get("idle_minutes")) != sum(idle.values()):
        return None
    if "affected_instructors" in metrics and _count(metrics["affected_instructors"]) != len(days):
        return None
    return days, idle


def _saved_day_idle() -> int:
    """The existing day trade's idle allowance per saved day (``teacher_bounds``)."""
    from core.services.timetable_pr4_instructor import (
        get_instructor_day_trade_config,
        is_instructor_day_trade_active,
    )

    if not is_instructor_day_trade_active():
        return 0
    return max(0, int(get_instructor_day_trade_config()["idle_allowance"]))


@dataclass(frozen=True)
class DayOffAnchor:
    """The run's start A, frozen when the run starts.

    ``days``/``idle`` cover every instructor of the run. ``padded``: with one
    teacher per section an instructor a measurement does not mention teaches
    nothing on that board and reads 0; under the union charging a missing
    instructor is missing evidence, as in the strict gates. ``saved_idle`` is
    the existing day trade's idle allowance per day saved (0 unless active).
    """

    days: dict[str, int]
    idle: dict[str, int]
    total_idle: int
    padded: bool
    saved_idle: int

    @classmethod
    def from_metrics(
        cls, metrics: Mapping, *, padded: bool, universe: Iterable = ()
    ) -> DayOffAnchor | None:
        """A from one measurement (None when its evidence is incomplete)."""
        per = _per_teacher(metrics)
        if per is None:
            return None
        days, idle = per
        if padded:
            for teacher in universe:
                days.setdefault(str(teacher), 0)
                idle.setdefault(str(teacher), 0)
        return cls(
            days=dict(sorted(days.items())),
            idle=dict(sorted(idle.items())),
            total_idle=sum(idle.values()),
            padded=padded,
            saved_idle=_saved_day_idle(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": dict(self.days),
            "idle": dict(self.idle),
            "total_idle": self.total_idle,
            "padded": self.padded,
            "saved_idle": self.saved_idle,
        }

    @classmethod
    def from_dict(cls, data: object) -> DayOffAnchor | None:
        if not isinstance(data, Mapping):
            return None
        maps = _maps(data.get("days"), data.get("idle"))
        padded, saved_idle = data.get("padded"), _count(data.get("saved_idle"))
        if maps is None or not isinstance(padded, bool) or saved_idle is None:
            return None
        days, idle = maps
        if _count(data.get("total_idle")) != sum(idle.values()):
            return None
        return cls(
            days=dict(sorted(days.items())),
            idle=dict(sorted(idle.items())),
            total_idle=sum(idle.values()),
            padded=padded,
            saved_idle=saved_idle,
        )

    def start_bounds(self) -> dict[str, tuple[int, int, int]]:
        """The run-start re-pick's bounds (C5): ``day_off_bounds`` at A."""
        return day_off_bounds(self, self.days, self.idle)


def _idle_allowance(start: DayOffAnchor, before: int, after: int) -> int:
    """Idle minutes an instructor may gain against the run's start: 60 a spare
    day given up (R3); the day trade's allowance for a FIRST day off (5 working
    days -> 4 or fewer); nothing for a second or third day off, whose idle may
    rise only when the total falls (R4; owner, 2026-10-20)."""
    if after > before:
        return SPARE_DAY_IDLE * (after - before)
    if before > MAX_WORKING_DAYS >= after:
        return start.saved_idle
    return 0


def day_off_bounds(
    start: DayOffAnchor, days: Mapping[str, int], idle: Mapping[str, int]
) -> dict[str, tuple[int, int, int]]:
    """``teacher_bounds`` (days, idle, idle per day saved) for a teacher re-pick
    on a board where each instructor works ``days`` with ``idle``: no
    instructor's days rise; idle up to what the ledger lets them hold there (a
    spare day given up earlier keeps its R3 minutes); and a day saved below that
    earns the day trade's allowance only while it would be a first day off."""
    return {
        i: (
            days[i],
            max(idle[i], start.idle[i] + _idle_allowance(start, before, days[i])),
            start.saved_idle if days[i] > MAX_WORKING_DAYS else 0,
        )
        for i, before in start.days.items()
    }


@dataclass(frozen=True)
class DayOffMeasure:
    """One board against the run's start: each instructor's days, idle and the
    part of their idle rise an allowance covers, the breach vector
    V = (R1, R2, R3, R4) and the idle rises nothing sanctions (E)."""

    days: dict[str, int]
    idle: dict[str, int]
    covered: dict[str, int]
    breaches: tuple[int, int, int, int]
    unsanctioned: int
    student_gain: bool

    @property
    def fives(self) -> int:
        """Instructors with no day off."""
        return sum(1 for days in self.days.values() if days > MAX_WORKING_DAYS)

    @property
    def idle_adjusted(self) -> int:
        """Idle net of the R3 and saved-day allowances actually used."""
        return sum(self.idle[i] - self.covered[i] for i in self.idle)

    @property
    def total_days(self) -> int:
        return sum(self.days.values())

    @property
    def total_idle(self) -> int:
        return sum(self.idle.values())

    def instructor_key(self) -> tuple[int, int, int, int]:
        return (self.fives, self.idle_adjusted, self.total_days, self.total_idle)


def _hard_no_worse(new: tuple, old: tuple) -> bool:
    return all(n <= o for n, o in zip(new[:_HARD], old[:_HARD], strict=True))


def _student_gain(score: tuple, start: tuple) -> bool:
    """R2's student gain: the full score strictly better, the hard positions
    each no worse."""
    return len(score) == len(start) and _hard_no_worse(score, start) and score < start


def _no_new_breach(new: DayOffMeasure, old: DayOffMeasure) -> bool:
    """The ratchet: no component of V grows."""
    return all(n <= o for n, o in zip(new.breaches, old.breaches, strict=True))


def _adds_fifth_day(new: DayOffMeasure, old: DayOffMeasure) -> bool:
    return any(days > MAX_WORKING_DAYS >= old.days[i] for i, days in new.days.items())


def _days_rise(new: DayOffMeasure, old: DayOffMeasure) -> bool:
    return any(days > old.days[i] for i, days in new.days.items())


@dataclass(frozen=True)
class DayOffPolicy:
    """The one acceptance relation of a run: its start A and the start's score.

    ``key`` ranks the four hard student positions, then instructors with no day
    off (R1), then instructor idle net of the R3 and saved-day allowances, then
    the rest of the student score, then total working days (spare days) and raw
    idle. ``core`` judges a stage's output against its input: hard positions,
    overloads and V can only shrink, no 5th day is added, and the key falls (a
    strict partial order, so committed stages compose). ``step`` judges a move
    inside a scan: ``core`` against the board in hand, plus R2 per move (a day
    given up must buy a strictly better score). ``swap_ok`` judges a change of
    who teaches what on one layout, where the student score is fixed.
    """

    VERSION: ClassVar[int] = 1

    anchor: DayOffAnchor
    score: tuple[int, ...]
    source: str

    def measure(self, metrics: object, score: Iterable[int]) -> DayOffMeasure | None:
        """``metrics`` against A, judged for R2 with ``score``; None when the
        evidence is incomplete or charges an instructor outside the run (one
        padded with zeros teaches nothing, so it is the same as absent)."""
        per = _per_teacher(metrics)
        if per is None:
            return None
        start = self.anchor
        outside = set(per[0]) - set(start.days)
        if outside and (not start.padded or any(per[0][i] or per[1][i] for i in outside)):
            return None
        if not start.padded and set(per[0]) != set(start.days):
            return None
        days = {i: per[0].get(i, 0) for i in start.days}
        idle = {i: per[1].get(i, 0) for i in start.days}
        covered: dict[str, int] = {}
        fifth = given_up = over_allowance = unsanctioned = 0
        for i, before in start.days.items():
            rise = idle[i] - start.idle[i]
            gave = max(0, days[i] - before)
            covered[i] = min(max(0, rise), _idle_allowance(start, before, days[i]))
            if days[i] > MAX_WORKING_DAYS >= before:
                fifth += 1
            if gave:
                over_allowance += max(0, rise - covered[i])
            else:
                unsanctioned += max(0, rise - covered[i])
            given_up += gave
        gain = _student_gain(tuple(score), self.score)
        total = sum(idle.values())
        breaches = (
            fifth,
            given_up if given_up and not gain else 0,
            over_allowance,
            total - start.total_idle + 1 if unsanctioned and total >= start.total_idle else 0,
        )
        return DayOffMeasure(days, idle, covered, breaches, unsanctioned, gain)

    @staticmethod
    def _key(score: tuple, measured: DayOffMeasure | None) -> tuple:
        if measured is None:  # a board without evidence never wins
            return score[:_HARD] + (inf, inf) + score[_HARD:] + (inf, inf)
        return (
            score[:_HARD]
            + (measured.fives, measured.idle_adjusted)
            + score[_HARD:]
            + (measured.total_days, measured.total_idle)
        )

    def key(self, evaluation: TimetableEvaluationResult) -> tuple:
        score = tuple(evaluation.lexicographic_score)
        return self._key(score, self.measure(schedule_metrics_of(evaluation), score))

    def core(self, out: TimetableEvaluationResult, inp: TimetableEvaluationResult) -> bool:
        """A stage's output against its input (stage boundaries, persist gates)."""
        new, old = tuple(out.lexicographic_score), tuple(inp.lexicographic_score)
        if len(new) != len(old) or not _hard_no_worse(new, old):
            return False
        if out.instructor_overload_count > inp.instructor_overload_count:
            return False
        after = self.measure(schedule_metrics_of(out), new)
        before = self.measure(schedule_metrics_of(inp), old)
        if after is None or before is None:
            return False
        if not _no_new_breach(after, before) or _adds_fifth_day(after, before):
            return False
        return self._key(new, after) < self._key(old, before)

    def step(
        self,
        candidate: TimetableEvaluationResult,
        incumbent: TimetableEvaluationResult,
        *,
        base: TimetableEvaluationResult,
    ) -> bool:
        """One move inside a scan: gated against ``base`` (the board in hand),
        ranked against ``incumbent`` (the scan's tentative winner or ``base``)."""
        new, here = tuple(candidate.lexicographic_score), tuple(base.lexicographic_score)
        if len(new) != len(here) or len(incumbent.lexicographic_score) != len(new):
            return False
        if not _hard_no_worse(new, here):
            return False
        if candidate.instructor_overload_count > base.instructor_overload_count:
            return False
        after = self.measure(schedule_metrics_of(candidate), new)
        before = self.measure(schedule_metrics_of(base), here)
        if after is None or before is None:
            return False
        if not _no_new_breach(after, before) or _adds_fifth_day(after, before):
            return False
        if _days_rise(after, before) and not new < here:
            return False  # R2 per move: a day given up buys a strictly better score
        return self._key(new, after) < self.key(incumbent)

    def keeps_breaches(
        self, out: TimetableEvaluationResult, inp: TimetableEvaluationResult
    ) -> bool:
        """The ratchet alone: complete evidence and no component of V grows."""
        after = self.measure(schedule_metrics_of(out), out.lexicographic_score)
        before = self.measure(schedule_metrics_of(inp), inp.lexicographic_score)
        return after is not None and before is not None and _no_new_breach(after, before)

    def swap_ok(self, new: Mapping, old: Mapping, *, score: Iterable[int]) -> bool:
        """Map ``new`` against map ``old``, both measured on one layout whose
        student score is ``score``: no breach grows, no instructor's days rise
        (with the score fixed a day given up buys nothing, so no day-neutral
        swap), and (fives, idle net of allowances, days, idle) is no worse."""
        fixed = tuple(score)
        after, before = self.measure(new, fixed), self.measure(old, fixed)
        if after is None or before is None:
            return False
        if not _no_new_breach(after, before) or _days_rise(after, before):
            return False
        return after.instructor_key() <= before.instructor_key()

    def swap_bounds(self, metrics: Mapping) -> dict[str, tuple[int, int, int]] | None:
        """``teacher_bounds`` for a re-pick on the board ``metrics`` measures:
        each instructor's days there, and idle up to what the ledger lets them
        hold (a spare day given up earlier keeps its R3 minutes). At A this is
        ``teacher_bounds(A)``."""
        measured = self.measure(metrics, self.score)
        if measured is None:
            return None
        return day_off_bounds(self.anchor, measured.days, measured.idle)

    def final_check(self, after: Mapping, score: Iterable[int]) -> tuple[list[dict], dict]:
        """The runner's final gate: one blocking row per non-zero component of
        V (or one evidence row), and the per-instructor report."""
        measured = self.measure(after, tuple(score))
        start = self.anchor
        if measured is None:
            row = {
                "metric": "instructor_day_off_evidence",
                "label": "Instructor day-off evidence missing",
                "before": None,
                "after": None,
                "delta": None,
            }
            return [row], {"version": self.VERSION, "evidence_complete": False}
        teachers = {
            i: {
                "days_before": before,
                "days_after": measured.days[i],
                "days_given_up": max(0, measured.days[i] - before),
                "days_saved": max(0, before - measured.days[i]),
                "idle_before": start.idle[i],
                "idle_after": measured.idle[i],
                "idle_change": measured.idle[i] - start.idle[i],
                "idle_covered": measured.covered[i],
            }
            for i, before in start.days.items()
        }
        fifth, spare, over, shortfall = measured.breaches
        rows: list[dict] = []
        if fifth:
            rows.append(
                {
                    "metric": "instructor_fifth_working_day",
                    "label": "Instructors given a fifth working day",
                    "before": 0,
                    "after": fifth,
                    "delta": fifth,
                    "instructor_ids": sorted(
                        i
                        for i, t in teachers.items()
                        if t["days_after"] > MAX_WORKING_DAYS >= t["days_before"]
                    ),
                }
            )
        if spare:
            rows.append(
                {
                    "metric": "instructor_spare_day_without_student_gain",
                    "label": "Spare days off given up without a student gain",
                    "before": 0,
                    "after": spare,
                    "delta": spare,
                    "instructor_ids": sorted(i for i, t in teachers.items() if t["days_given_up"]),
                }
            )
        if over:
            rows.append(
                {
                    "metric": "instructor_spare_day_idle_over_allowance",
                    "label": f"Idle minutes over {SPARE_DAY_IDLE} per spare day given up",
                    "before": 0,
                    "after": over,
                    "delta": over,
                    "instructor_ids": sorted(
                        i
                        for i, t in teachers.items()
                        if t["days_given_up"] and t["idle_change"] > t["idle_covered"]
                    ),
                }
            )
        if shortfall:
            rows.append(
                {
                    "metric": "instructor_idle_rise_not_offset",
                    "label": "Instructor idle rose while total instructor idle did not fall",
                    "before": start.total_idle,
                    "after": measured.total_idle,
                    "delta": measured.total_idle - start.total_idle,
                    "instructor_ids": sorted(
                        i
                        for i, t in teachers.items()
                        if not t["days_given_up"] and t["idle_change"] > t["idle_covered"]
                    ),
                }
            )
        report = {
            "version": self.VERSION,
            "evidence_complete": True,
            "teachers": teachers,
            "unsanctioned_idle_rise": measured.unsanctioned,
            "total_idle_before": start.total_idle,
            "total_idle_after": measured.total_idle,
            "five_day_before": sum(1 for d in start.days.values() if d > MAX_WORKING_DAYS),
            "five_day_after": measured.fives,
            "student_gain": measured.student_gain,
            "breaches": dict(
                zip(
                    ("fifth_day", "spare_day_no_gain", "spare_day_idle", "idle_not_offset"),
                    measured.breaches,
                    strict=True,
                )
            ),
        }
        return rows, report

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "anchor": self.anchor.to_dict(),
            "score": list(self.score),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: object) -> DayOffPolicy | None:
        """The policy a result carries, or None when it is missing or malformed."""
        if not isinstance(data, Mapping) or data.get("version") != cls.VERSION:
            return None
        anchor = DayOffAnchor.from_dict(data.get("anchor"))
        score, source = data.get("score"), data.get("source")
        if anchor is None or not isinstance(score, list | tuple) or not isinstance(source, str):
            return None
        if any(isinstance(v, bool) or not isinstance(v, int) for v in score):
            return None
        return cls(anchor=anchor, score=tuple(score), source=source)


_DAY_OFF: ContextVar[DayOffPolicy | None] = ContextVar("day_off_policy", default=None)


@contextmanager
def day_off_scope(policy: DayOffPolicy | None) -> Iterator[None]:
    """Inside, ``evaluation_is_better`` and ``stage_is_better`` judge by
    ``policy``; ``None`` suspends an outer policy (candidate generation and the
    placers keep their own rules)."""
    token = _DAY_OFF.set(policy)
    try:
        yield
    finally:
        _DAY_OFF.reset(token)


def current_day_off_policy() -> DayOffPolicy | None:
    return _DAY_OFF.get()


def stage_is_better(out: TimetableEvaluationResult, inp: TimetableEvaluationResult) -> bool:
    """A stage boundary: ``core`` inside a day-off scope, else the unchanged
    ``evaluation_is_better``. A step relation does not compose across a stage,
    so boundaries never use it."""
    policy = current_day_off_policy()
    if policy is not None:
        return policy.core(out, inp)
    return evaluation_is_better(out, inp)
