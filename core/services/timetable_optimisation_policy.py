"""Classic Generate/Optimise selection policy, separate from public score layouts.

The 6/7/9-element student score remains a reporting contract. Hard student
coverage is protected component by component; instructor campus working days
and actual idle minutes then lead the quality decision.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from math import isfinite

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
    """
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
