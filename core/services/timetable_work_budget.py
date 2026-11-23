"""Deterministic work budgets for the timetable searches.

A search that stops on the wall clock ends on a different board every time the machine is a
little busier or quicker, whatever its seed: the seed fixes the moves it tries in order, the clock
decides how many of them it gets to try. A :class:`WorkBudget` stops a search on a count of
evaluated candidates instead, so the board a run ends on is a function of (stored board, flags,
seed) alone. The wall-clock limit stays, as a hard ceiling for a machine too slow to finish the
work in time (production's job sweep marks a planner job stale after 45 minutes); when the
ceiling is what ends a search, the run says so (:func:`repeatability_report`), it never ends
quietly on a board that another run would not reproduce.

``max_work`` of ``None`` or ``0`` means no work cap: the search stops on its own convergence or on
the ceiling, as it did before budgets existed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# Why a search ended: it ran out of candidates ("completed"), spent its work budget
# ("work_limit": repeatable), or the wall-clock ceiling stopped it ("wall_ceiling": the board
# depends on how fast the machine ran).
COMPLETED = "completed"
WORK_LIMIT = "work_limit"
WALL_CEILING = "wall_ceiling"

_stops: ContextVar[list[dict[str, Any]] | None] = ContextVar("timetable_search_stops", default=None)


@contextmanager
def capture_search_stops() -> Iterator[list[dict[str, Any]]]:
    """Collect every search stop recorded inside the block (one list per run, never shared)."""
    stops: list[dict[str, Any]] = []
    token = _stops.set(stops)
    try:
        yield stops
    finally:
        _stops.reset(token)


def record_search_stop(
    stage: str,
    reason: str,
    *,
    work: int,
    work_limit: int | None,
    seconds: float,
    ceiling_seconds: float | None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Note how one search ended; kept only while a run is capturing (see above).

    A stage that runs many small searches (one room solve per candidate) is one row per
    (stage, reason) with a ``count``, so a run's record stays a handful of lines.
    """
    stops = _stops.get()
    row = None
    if stops is not None:
        row = next((s for s in stops if s["stage"] == stage and s["reason"] == reason), None)
    if row is None:
        row = {
            "stage": stage,
            "reason": reason,
            "count": 0,
            "work": 0,
            "work_limit": int(work_limit) if work_limit else None,
            "seconds": 0.0,
            "ceiling_seconds": None
            if ceiling_seconds is None
            else round(float(ceiling_seconds), 3),
            **(detail or {}),
        }
        if stops is not None:
            stops.append(row)
    row["count"] += 1
    row["work"] += int(work)
    row["seconds"] = round(row["seconds"] + float(seconds), 3)
    return row


def repeatability_report(stops: list[dict[str, Any]]) -> dict[str, Any]:
    """``repeatable`` is False when a wall-clock ceiling ended any search of the run."""
    ceilings = [s["stage"] for s in stops if s["reason"] == WALL_CEILING]
    return {"repeatable": not ceilings, "ceilings_hit": ceilings, "searches": list(stops)}


class WorkBudget:
    """Counts work units and says when a search must stop.

    ``spend()`` after each evaluated candidate; ``exhausted()`` wherever the search used to look
    at the clock. The work check comes first, so a search that reaches its work limit at the same
    moment the ceiling passes still counts as a repeatable stop.
    """

    def __init__(
        self,
        stage: str,
        *,
        max_work: int | None,
        ceiling_seconds: float | None,
        clock: Callable[[], float] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.detail = detail
        self.max_work = int(max_work) if max_work and max_work > 0 else None
        # None: no ceiling. 0 is a real ceiling: a zero budget stops the search at once.
        self.ceiling_seconds = None if ceiling_seconds is None else max(0.0, float(ceiling_seconds))
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self.work = 0
        self.reason: str | None = None

    def spend(self, units: int = 1) -> None:
        self.work += units

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    def exhausted(self) -> bool:
        if self.reason in (WORK_LIMIT, WALL_CEILING):
            return True
        if self.max_work is not None and self.work >= self.max_work:
            self.reason = WORK_LIMIT
        elif self.ceiling_seconds is not None and self.elapsed >= self.ceiling_seconds:
            self.reason = WALL_CEILING
        return self.reason is not None

    def finish(self) -> dict[str, Any]:
        """Record how the search ended (a search that never hit a limit COMPLETED)."""
        return record_search_stop(
            self.stage,
            self.reason or COMPLETED,
            work=self.work,
            work_limit=self.max_work,
            seconds=self.elapsed,
            ceiling_seconds=self.ceiling_seconds,
            detail=self.detail,
        )


def cpsat_stop_reason(
    status: int, solver: Any, det_limit: float | None, wall_limit: float | None = None
) -> str:
    """How a CP-SAT solve ended: proven (optimal or infeasible), on its deterministic-time limit,
    on the wall-clock limit (it ran to within 5 % of it), or by itself before either (the solver
    gave up early, which it does the same way on every machine: COMPLETED)."""
    from ortools.sat.python import cp_model

    if status in (cp_model.OPTIMAL, cp_model.INFEASIBLE):
        return COMPLETED
    if det_limit and solver.deterministic_time >= det_limit * 0.999:
        return WORK_LIMIT
    if wall_limit is None or solver.wall_time >= wall_limit * 0.95:
        return WALL_CEILING
    return COMPLETED


def record_cpsat_stop(
    stage: str,
    status: int,
    solver: Any,
    *,
    wall_limit: float,
    det_limit: float | None = None,
) -> str:
    """Record how one CP-SAT solve ended under ``stage`` (see :func:`cpsat_stop_reason`)."""
    reason = cpsat_stop_reason(status, solver, det_limit, wall_limit)
    record_search_stop(
        stage,
        reason,
        work=1,
        work_limit=None,
        seconds=solver.wall_time,
        ceiling_seconds=wall_limit,
    )
    return reason


def deterministic_limit(solver: Any, wall_seconds: float, setting: str) -> float | None:
    """Give a CP-SAT solve a deterministic-time limit worth ``wall_seconds`` of its kind of model
    (the ``setting`` named: deterministic time per wall second, calibrated per kind of model;
    0 = none, the wall clock alone ends it). With one worker, which is what production runs, a
    solve that stops on this limit returns the same solution on every machine; with several the
    solver is not repeatable whatever the limit (and its deterministic time adds up across
    workers), so no limit is set and the wall clock ends it, as before. Returns the limit set, or
    None."""
    from django.conf import settings

    per_second = float(getattr(settings, setting, 0) or 0)
    if per_second <= 0 or wall_seconds <= 0 or solver.parameters.num_workers != 1:
        return None
    limit = per_second * wall_seconds
    solver.parameters.max_deterministic_time = limit
    return limit
