"""Multi-start exam-timetable runner with mechanical Pareto candidate selection.

Why this exists
---------------
The greedy scheduler is sensitive to course-ordering tie-breaks (handled
via seed banding inside ``schedule()``). One seed → one timetable.
Different seeds explore different parts of the soft-cost surface.

Running N seeded builds and surfacing the 4 best candidates per
registrar-actionable axis is the cheapest stochastic-improvement layer
we can ship without changing the algorithm. It is the first thing the
peer-review session locked into SHIP because it has the highest
leverage-to-effort and unlocks the empirical data needed for the
"local search go/no-go" decision later in the plan.

Four candidate roles
--------------------
- ``"recommended"`` — best overall lex tuple. Order matches registrar
  priorities: zero hard violations → fewest overflow → fewest
  students-over-limit → fewest heavy-day → tighter credit/exam load.
- ``"lowest_overflow"`` — fewest courses landing on the OVERFLOW
  virtual day.
- ``"lowest_overload"`` — fewest students exceeding the per-day cap.
- ``"best_room_feasibility"`` — *mechanical* (peer-review locked):
  fewest ``UNASSIGNED`` room sections → fewest multi-sitting sections
  → highest avg utilisation. ``buildings_per_slot`` is telemetry-only
  and never enters this ranking until evidence promotes it (see the
  EXPLICIT NO list in ``runtime/exam_timetable_improvement_plan.md``).

The same seed can win multiple roles. The runner persists each unique
winning seed exactly once and records every role it won in
``payload["multistart"]["role_winners"]`` so the UI can show "this
candidate IS the best of everything" rather than four duplicate cards.

Pin-and-rebuild signal capture
------------------------------
When ``previous_run_id`` is provided, the runner computes per-candidate
``movement_count`` (placements that differ vs the previous run) and
``courses_moved`` (the actual list). This is the cheap implementation
of the "pin-and-rebuild instability" 3–6 month watch item from the
peer-review plan — implemented here because the data is free at multi-
start time and we want the signal *before* the pain forces a real
incremental scheduler later.

Time budget
-----------
``run_multistart`` respects ``time_budget_s``: as soon as the budget
elapses (and at least one candidate has completed), the seed loop
exits. The Pareto selector then runs over whatever finished. The
budget is checked between builds, not inside them — a single build
that overruns is allowed to finish.

Persistence policy
------------------
Only the unique-seed Pareto winners become ``ExamTimetableRun`` rows.
Explored-but-not-selected runs are discarded. Each persisted row's
label carries a discriminator like
``"<base_label> :: candidate=lowest_overflow+best_room_feasibility seed=N"``
so the history panel renders them addressably without schema changes.

Feature flag
------------
``TIMETABLE_EXAM_MULTISTART_ENABLED`` (Django setting, default
``False``). The view layer dispatches on ``is_multistart_enabled()``;
when off, the existing single-run build path is unaffected.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from django.conf import settings

from core.models import ExamTimetableRun
from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    load_normalised_run,
    stamp_schema_version,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

CandidateRole = Literal[
    "recommended",
    "lowest_overflow",
    "lowest_overload",
    "best_room_feasibility",
]

ALL_CANDIDATE_ROLES: tuple[CandidateRole, ...] = (
    "recommended",
    "lowest_overflow",
    "lowest_overload",
    "best_room_feasibility",
)
"""The four roles, in display order. The Pareto selector returns one
candidate per role; a single candidate may map to multiple roles."""


@dataclass(frozen=True)
class CandidateMetrics:
    """The numeric summary used for ranking. Frozen so it is hashable
    and safe to compare across candidates."""

    overflow_count: int
    students_over_limit_count: int
    heavy_day_students: int
    same_slot_conflicts: int
    bucket_day_violations: int
    unassigned_room_sections: int
    multi_sitting_sections: int
    avg_utilisation: float
    max_credit_load_per_day: int
    max_exams_per_day: int


@dataclass(frozen=True)
class MultistartCandidate:
    """One seeded build, evaluated and held in memory before persistence.

    ``role`` is ``None`` during exploration and set when this candidate
    wins one of the Pareto roles. ``payload`` is the full result dict
    returned by ``build_exam_timetable(persist=False)``.
    """

    seed: int
    metrics: CandidateMetrics
    payload: dict[str, Any]
    role_winners: tuple[CandidateRole, ...] = ()
    movement_count: int | None = None
    courses_moved: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultistartReport:
    """Top-level result returned to the view layer.

    ``candidates_by_role`` maps each role to its winner; the same
    candidate object may appear under multiple roles. ``run_ids`` maps
    seed → persisted ``ExamTimetableRun.id`` so the view can build
    "load this candidate" links without re-querying.
    """

    candidates_by_role: dict[CandidateRole, MultistartCandidate]
    run_ids: dict[int, int]
    total_runs: int
    runs_completed: int
    elapsed_seconds: float
    feasibility_error: dict[str, Any] | None
    previous_run_id: int | None


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def is_multistart_enabled() -> bool:
    """Read the multi-start feature flag.

    Default ``False`` — the existing single-run build path stays the
    default until the 4-candidate UI is proven against real registrar
    workflows.
    """
    return bool(getattr(settings, "TIMETABLE_EXAM_MULTISTART_ENABLED", False))


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------


def _extract_metrics(payload: dict[str, Any]) -> CandidateMetrics:
    """Pull comparable numeric metrics from a build result dict.

    Defensive against missing keys: every metric falls back to a safe
    zero/0.0 default rather than ``KeyError``-ing. New metrics added in
    future schema versions can be wired in here without breaking the
    contract — old payloads simply read 0 for the new field, which
    means they neither win nor lose on it (neutral participation).
    """
    qa = payload.get("qa") or {}
    schedule = payload.get("schedule") or []

    # Overflow = courses placed on the OVERFLOW virtual day.
    overflow_count = sum(1 for e in schedule if e.get("day") == "OVERFLOW")

    # Multi-sitting only lights up when step 3 lands the multi-sitting tile;
    # for now every candidate reads 0 (neutral participation) and the
    # tie-breaker effectively skips this dimension. The default is the
    # critical part — without it this would crash on every step-2 build.
    multi_sitting = int(qa.get("multi_sitting_sections", 0))

    return CandidateMetrics(
        overflow_count=overflow_count,
        students_over_limit_count=int(qa.get("students_over_limit_per_day", 0)),
        heavy_day_students=int(qa.get("heavy_day_students", 0)),
        same_slot_conflicts=int(qa.get("conflict_count", 0)),
        bucket_day_violations=int(qa.get("bucket_day_violations_count", 0)),
        unassigned_room_sections=int(qa.get("unassigned_room_sections", 0)),
        multi_sitting_sections=multi_sitting,
        avg_utilisation=float(qa.get("avg_utilization", 0.0)),
        max_credit_load_per_day=int(qa.get("max_credit_load_per_day", 0)),
        max_exams_per_day=int(qa.get("max_exams_per_day_per_student", 0)),
    )


# ---------------------------------------------------------------------------
# Pareto ranking — one ``_<role>_key`` function per role.
#
# Ranking is "smaller tuple wins". For "higher is better" metrics like
# ``avg_utilisation`` we negate so smaller still wins. The trailing
# ``c.seed`` provides a deterministic tie-break so identical-metric
# candidates always resolve the same way (reproducibility).
# ---------------------------------------------------------------------------


def _recommended_key(c: MultistartCandidate) -> tuple[int | float, ...]:
    """Lex order matching registrar priorities.

    Hard violations (same_slot, bucket_day) come first because they are
    *errors*, not soft costs. Then overflow (a course on OVERFLOW is a
    registrar-visible failure even if the algorithm "succeeded"). Then
    students_over_limit (the soft cap most-watched in QA). Then
    heavy-day. Then tighter credit/exam load.
    """
    m = c.metrics
    return (
        m.same_slot_conflicts,
        m.bucket_day_violations,
        m.overflow_count,
        m.students_over_limit_count,
        m.heavy_day_students,
        m.max_credit_load_per_day,
        m.max_exams_per_day,
        c.seed,
    )


def _lowest_overflow_key(c: MultistartCandidate) -> tuple[int | float, ...]:
    """Minimise overflow first; break ties by hard violations and
    overload so the registrar isn't offered a candidate that fixed
    overflow by accepting a same-slot clash."""
    m = c.metrics
    return (
        m.overflow_count,
        m.same_slot_conflicts + m.bucket_day_violations,
        m.students_over_limit_count,
        c.seed,
    )


def _lowest_overload_key(c: MultistartCandidate) -> tuple[int | float, ...]:
    """Minimise students-over-limit first; tie-break on heavy-day,
    multi-sitting, and hard violations. Multi-sitting is included so a
    candidate that fixes overload by splitting sections doesn't quietly
    win over one that fixes it via better day-spread."""
    m = c.metrics
    return (
        m.students_over_limit_count,
        m.heavy_day_students,
        m.multi_sitting_sections,
        m.same_slot_conflicts + m.bucket_day_violations,
        c.seed,
    )


def _best_room_feasibility_key(c: MultistartCandidate) -> tuple[int | float, ...]:
    """Mechanical room-feasibility ranking — peer-review locked.

    Tier 1: fewest UNASSIGNED room sections (the worst registrar pain).
    Tier 2: fewest multi-sitting sections (a section split across
    multiple time-slots is operationally heavier than one fitting
    cleanly).
    Tier 3: highest avg_utilisation (negated so smaller-tuple-wins).
    Hard violations included as a final guard against degenerate
    candidates that "look room-good" but broke a constraint.
    """
    m = c.metrics
    return (
        m.unassigned_room_sections,
        m.multi_sitting_sections,
        -m.avg_utilisation,
        m.same_slot_conflicts + m.bucket_day_violations,
        c.seed,
    )


_ROLE_KEY_FUNCS = {
    "recommended": _recommended_key,
    "lowest_overflow": _lowest_overflow_key,
    "lowest_overload": _lowest_overload_key,
    "best_room_feasibility": _best_room_feasibility_key,
}


def select_pareto_candidates(
    candidates: list[MultistartCandidate],
) -> dict[CandidateRole, MultistartCandidate]:
    """Pick the role-optimal candidate for each of the 4 roles.

    Returns an empty dict when no candidates were provided. The same
    candidate may win multiple roles — callers detect this by walking
    the returned dict.
    """
    if not candidates:
        return {}
    return {role: min(candidates, key=key_fn) for role, key_fn in _ROLE_KEY_FUNCS.items()}


# ---------------------------------------------------------------------------
# Pin-and-rebuild signal capture
# ---------------------------------------------------------------------------


def _baseline_placements(previous_run_id: int) -> dict[str, tuple[str, int]] | None:
    """Load the previous run's ``course_code -> (day, slot_index)`` map.

    Returns ``None`` if the previous run can't be loaded or its payload
    is non-OK; movement-count metadata is then omitted from candidates,
    not faked. Defensive — never raises.
    """
    try:
        previous_run = ExamTimetableRun.objects.get(id=previous_run_id)
    except ExamTimetableRun.DoesNotExist:
        return None

    previous_payload = load_normalised_run(previous_run)
    if previous_payload.get("status") != "ok":
        return None

    schedule = previous_payload.get("schedule", []) or []
    return {
        e["course_code"]: (e["day"], int(e["slot_index"]))
        for e in schedule
        if e.get("course_code") and "day" in e and "slot_index" in e
    }


def _movement_vs_baseline(
    candidate_payload: dict[str, Any],
    baseline: dict[str, tuple[str, int]],
) -> tuple[int, list[str]]:
    """Count placements that differ from the baseline.

    A course is "moved" if its (day, slot_index) differs from the
    baseline, OR if it appears in only one of the two schedules.
    Sorted alphabetically for deterministic output.
    """
    schedule = candidate_payload.get("schedule") or []
    current: dict[str, tuple[str, int]] = {
        e["course_code"]: (e["day"], int(e["slot_index"]))
        for e in schedule
        if e.get("course_code") and "day" in e and "slot_index" in e
    }
    moved: list[str] = []
    for course, placement in current.items():
        if baseline.get(course) != placement:
            moved.append(course)
    # Courses in baseline but not current are also "moved" (dropped).
    for course in baseline:
        if course not in current:
            moved.append(course)
    moved.sort()
    return len(moved), moved


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _candidate_label(base: str, role_winners: list[CandidateRole], seed: int) -> str:
    role_str = "+".join(role_winners) if role_winners else "explored"
    return f"{base} :: candidate={role_str} seed={seed}"


def run_multistart(
    *,
    label: str,
    days: list[str],
    periods: list[str],
    max_per_day: int = 2,
    programs: list[str] | None = None,
    sections: list[str] | None = None,
    selected_courses: list[str] | None = None,
    pinned: list[dict] | None = None,
    seeds: Iterable[int] | None = None,
    n_runs: int = 20,
    time_budget_s: float = 12.0,
    assign_rooms: bool = True,
    rebalance_invigilators: bool = True,
    thin_conflict_threshold: int = 0,
    previous_run_id: int | None = None,
) -> MultistartReport:
    """Run multiple seeded builds, pick 4 Pareto candidates, persist them.

    Parameters mirror ``build_exam_timetable`` plus:

    - ``seeds``: explicit list of seeds to try. When omitted, uses
      ``range(n_runs)``.
    - ``n_runs``: target build count when ``seeds`` is None.
    - ``time_budget_s``: stop the seed loop when this many seconds
      elapse, provided at least one candidate has completed. The
      Pareto selector then runs over whatever finished.
    - ``previous_run_id``: when set, each persisted candidate's payload
      gains ``multistart.movement_count`` and ``multistart.courses_moved``
      vs the previous run — the cheap pin-and-rebuild signal.

    Returns a ``MultistartReport``. Persists 1-4 ``ExamTimetableRun``
    rows (one per unique winning seed). Explored-but-not-selected runs
    are not persisted.

    On all-feasibility-failure (every seed hit a bucket violation)
    returns an empty report with ``feasibility_error`` set to the
    error dict from the first failing run.
    """
    # Imported lazily to avoid a top-level circular import: this module is
    # imported by core.exam_views, which itself imports build_exam_timetable.
    from core.services.exam_timetable import build_exam_timetable

    if seeds is None:
        seeds = range(max(1, n_runs))
    seed_list = list(seeds)
    if not seed_list:
        seed_list = [0]

    started_at = time.monotonic()
    deadline = started_at + max(0.0, time_budget_s)

    candidates: list[MultistartCandidate] = []
    feasibility_error: dict[str, Any] | None = None

    for seed in seed_list:
        if candidates and time.monotonic() >= deadline:
            break

        result = build_exam_timetable(
            label=label,
            days=days,
            periods=periods,
            max_per_day=max_per_day,
            programs=programs,
            sections=sections,
            selected_courses=selected_courses,
            pinned=pinned,
            seed=seed,
            assign_rooms=assign_rooms,
            rebalance_invigilators=rebalance_invigilators,
            thin_conflict_threshold=thin_conflict_threshold,
            persist=False,
        )

        if result.get("status") == "feasibility_error":
            if feasibility_error is None:
                feasibility_error = result
            continue

        metrics = _extract_metrics(result)
        candidates.append(MultistartCandidate(seed=seed, metrics=metrics, payload=result))

    elapsed = time.monotonic() - started_at

    if not candidates:
        return MultistartReport(
            candidates_by_role={},
            run_ids={},
            total_runs=len(seed_list),
            runs_completed=0,
            elapsed_seconds=elapsed,
            feasibility_error=feasibility_error,
            previous_run_id=previous_run_id,
        )

    by_role = select_pareto_candidates(candidates)

    # Compute the role_winners list per seed (one candidate may win
    # multiple roles).
    winners_by_seed: dict[int, list[CandidateRole]] = {}
    for role, candidate in by_role.items():
        winners_by_seed.setdefault(candidate.seed, []).append(role)
    # Stable order: ALL_CANDIDATE_ROLES order.
    for seed in winners_by_seed:
        winners_by_seed[seed].sort(key=ALL_CANDIDATE_ROLES.index)

    # Pin-and-rebuild baseline (loaded once, reused for every candidate).
    baseline = _baseline_placements(previous_run_id) if previous_run_id is not None else None

    # Persist each unique winning seed exactly once.
    run_ids: dict[int, int] = {}
    annotated_payloads: dict[int, dict[str, Any]] = {}

    for seed, role_winners in winners_by_seed.items():
        candidate = next(c for c in candidates if c.seed == seed)
        annotated = dict(candidate.payload)

        movement_count: int | None = None
        courses_moved: list[str] = []
        if baseline is not None:
            movement_count, courses_moved = _movement_vs_baseline(candidate.payload, baseline)

        annotated["multistart"] = {
            "schema_version": EXAM_RUN_SCHEMA_VERSION,
            "seed": seed,
            "role_winners": list(role_winners),
            "previous_run_id": previous_run_id,
            "movement_count": movement_count,
            "courses_moved": courses_moved,
        }
        stamp_schema_version(annotated)

        run = ExamTimetableRun.objects.create(
            label=_candidate_label(label, role_winners, seed),
            result_json=json.dumps(annotated, ensure_ascii=False),
        )
        run_ids[seed] = run.id
        annotated["run_id"] = run.id
        annotated_payloads[seed] = annotated

    # Re-bind by_role to candidates with persisted run_ids + multistart
    # metadata so callers see the final shape.
    final_by_role: dict[CandidateRole, MultistartCandidate] = {}
    for role, candidate in by_role.items():
        seed = candidate.seed
        winners = tuple(winners_by_seed[seed])
        movement = annotated_payloads[seed]["multistart"]["movement_count"]
        moved = annotated_payloads[seed]["multistart"]["courses_moved"]
        final_by_role[role] = MultistartCandidate(
            seed=seed,
            metrics=candidate.metrics,
            payload=annotated_payloads[seed],
            role_winners=winners,
            movement_count=movement,
            courses_moved=tuple(moved),
        )

    return MultistartReport(
        candidates_by_role=final_by_role,
        run_ids=run_ids,
        total_runs=len(seed_list),
        runs_completed=len(candidates),
        elapsed_seconds=elapsed,
        feasibility_error=None,
        previous_run_id=previous_run_id,
    )


# ---------------------------------------------------------------------------
# Convenience: report -> JSON-friendly dict for the view layer.
# ---------------------------------------------------------------------------


def report_to_dict(report: MultistartReport) -> dict[str, Any]:
    """Render a ``MultistartReport`` as a JSON-serialisable dict.

    Used by the view layer to ship the candidate summary back to the
    browser. The ``payload`` of each candidate is included verbatim so
    the UI can render the schedule grid without a second round-trip;
    callers that only need the metrics summary can read
    ``["candidates"][role]["metrics"]`` directly.
    """
    out: dict[str, Any] = {
        "total_runs": report.total_runs,
        "runs_completed": report.runs_completed,
        "elapsed_seconds": round(report.elapsed_seconds, 3),
        "previous_run_id": report.previous_run_id,
        "feasibility_error": report.feasibility_error,
        "run_ids": dict(report.run_ids),
        "candidates": {},
    }
    for role, candidate in report.candidates_by_role.items():
        out["candidates"][role] = {
            "seed": candidate.seed,
            "role_winners": list(candidate.role_winners),
            "movement_count": candidate.movement_count,
            "courses_moved": list(candidate.courses_moved),
            "metrics": {
                "overflow_count": candidate.metrics.overflow_count,
                "students_over_limit_count": candidate.metrics.students_over_limit_count,
                "heavy_day_students": candidate.metrics.heavy_day_students,
                "same_slot_conflicts": candidate.metrics.same_slot_conflicts,
                "bucket_day_violations": candidate.metrics.bucket_day_violations,
                "unassigned_room_sections": candidate.metrics.unassigned_room_sections,
                "multi_sitting_sections": candidate.metrics.multi_sitting_sections,
                "avg_utilisation": candidate.metrics.avg_utilisation,
                "max_credit_load_per_day": candidate.metrics.max_credit_load_per_day,
                "max_exams_per_day": candidate.metrics.max_exams_per_day,
            },
            "payload": cast(dict[str, Any], candidate.payload),
        }
    return out
