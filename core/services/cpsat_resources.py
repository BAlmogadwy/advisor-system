"""How much of this host one timetable CP-SAT solve may take: workers and model size.

Production is one gunicorn worker on a 0.5-CPU, 512 MB Render instance that
idles at about 215 MB. On 2026-10-12 three Builds in a row (1448 Term 2, DS+AI
male) were killed there for running out of memory, each about two minutes in.
The cause was the "optimal" candidate of the V2 sweep: ``solve_board`` builds a
quadratic model - one product variable and three constraints for every pair of
options of every pair of sections that share students - and on a 17-section
board that is 142,563 variables and 432,711 constraints. Building it took the
process from 130 MB to 330 MB, and loading it into the solver to 800 MB, with
8, 2 or 1 workers alike. It then ran out of time with nothing placed, and the
board fell back to the greedy placement it would have had anyway.

So a model is admitted by its size, not by hope: ``BudgetedCpModel`` counts
what is built and stops at ``TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS``, before the
memory is spent. A solve that is turned away reports what a solve that ran out
of time reports - no placements - so every caller already falls back.

The worker count matters less (on the smaller 71k-element board, one worker
peaked 83 MB above the model and eight 245 MB) but eight workers on half a CPU
only share it, so a solve uses no more workers than the host has CPUs.
CP-SAT's own ``max_memory_in_mb`` is no substitute: set to 350 it did not stop
the 800 MB solve above.
"""

from __future__ import annotations

import logging
import math
import os
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

#: The most CP-SAT workers any timetable solve asks for.
MAX_WORKERS = 8
_DEFAULT_MAX_MODEL_ELEMENTS = 40_000
_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


class ModelTooLarge(Exception):
    """A model grew past the element budget while it was being built."""

    def __init__(self, budget: int) -> None:
        super().__init__(f"the CP-SAT model grew past {budget} variables and constraints")
        self.budget = budget


def max_model_elements() -> int:
    """The element budget; 0 or less turns the guard off."""
    return int(getattr(settings, "TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS", _DEFAULT_MAX_MODEL_ELEMENTS))


class BudgetedCpModel(cp_model.CpModel):
    """A ``CpModel`` that raises ``ModelTooLarge`` once it holds more than
    ``budget`` variables and constraints.

    Each variable and each constraint made through the builders below counts
    one, checked as it is made, so the memory spent before the stop is bounded
    by the budget itself. ``add_implication`` is counted through
    ``add_bool_and``; hints and the objective are not counted.
    """

    def __init__(self, budget: int | None = None) -> None:
        # Set before the base constructor, which reads every attribute of the
        # instance (``elements`` included) to install its CamelCase aliases.
        self._budget = max_model_elements() if budget is None else budget
        self._elements = 0
        super().__init__()

    @property
    def elements(self) -> int:
        return self._elements

    def _grow(self) -> None:
        self._elements += 1
        if 0 < self._budget < self._elements:
            raise ModelTooLarge(self._budget)

    def new_bool_var(self, name):  # type: ignore[no-untyped-def]
        self._grow()
        return super().new_bool_var(name)

    def new_int_var(self, lb, ub, name):  # type: ignore[no-untyped-def]
        self._grow()
        return super().new_int_var(lb, ub, name)

    def add(self, ct):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add(ct)

    def add_exactly_one(self, *expressions):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_exactly_one(*expressions)

    def add_at_most_one(self, *expressions):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_at_most_one(*expressions)

    def add_bool_or(self, *literals):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_bool_or(*literals)

    def add_bool_and(self, *literals):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_bool_and(*literals)

    def add_max_equality(self, target, *expressions):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_max_equality(target, *expressions)

    def add_min_equality(self, target, *expressions):  # type: ignore[no-untyped-def]
        self._grow()
        return super().add_min_equality(target, *expressions)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


@lru_cache(maxsize=1)
def host_cpus() -> int:
    """CPUs this process may use: its cgroup CPU quota, rounded up, when it has
    one; else the CPUs it is allowed to run on. Never less than one."""
    quota: float | None = None
    v2 = _read(_CGROUP_V2_CPU_MAX)
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota = int(parts[0]) / int(parts[1])
            except (ValueError, ZeroDivisionError):
                quota = None
    else:
        q, p = _read(_CGROUP_V1_QUOTA), _read(_CGROUP_V1_PERIOD)
        try:
            if q is not None and p is not None and int(q) > 0:
                quota = int(q) / int(p)
        except (ValueError, ZeroDivisionError):
            quota = None
    try:
        allowed = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        allowed = os.cpu_count() or 1
    if quota is not None and quota > 0:
        allowed = min(allowed, math.ceil(quota))
    cpus = max(1, allowed)
    logger.info("CP-SAT host CPUs: %d (cgroup quota %s)", cpus, quota)
    return cpus


def cpsat_workers(requested: int = MAX_WORKERS) -> int:
    """Workers for one solve: ``TIMETABLE_CPSAT_WORKERS`` when set above zero,
    else the host's CPUs; never more than ``requested``."""
    configured = int(getattr(settings, "TIMETABLE_CPSAT_WORKERS", 0) or 0)
    available = configured if configured > 0 else host_cpus()
    return max(1, min(requested, available))
