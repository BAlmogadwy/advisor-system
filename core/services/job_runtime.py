"""What background solver work in this process shares: one solver at a time.

The web service runs one gunicorn worker on half a CPU with 512 MB. A CP-SAT
solve on the real exam roster takes 120-190 MB above the interpreter, and the
exam jobs, the timetable planner's jobs and the exam page's Check each start
one. Two at once split the CPU - and the wall-clock room deadlines with it -
and three can exhaust the memory, and an OOM kill takes every in-flight
request with it. So they take turns on one lock.

The lock is per process. It cannot see the Telegram worker (a separate process
that runs no solver) or an overlapping instance during a deploy; the exam jobs'
one-active-job database constraint is what holds across instances.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_SOLVER = threading.BoundedSemaphore(1)
_WAIT_STEP_SECONDS = 0.5


class SolverBusy(Exception):
    """Another solve holds the lock and the caller chose not to wait."""


@contextmanager
def solver_slot(*, wait: bool = True, give_up: Callable[[], bool] | None = None) -> Iterator[None]:
    """Hold the process's one solver slot for the duration of the block.

    ``wait=False`` is for a request thread, which should answer "busy" at once
    rather than hold one of four request threads for a minute. ``give_up`` is
    for a background job waiting its turn: asked every half second, it lets a
    cancelled job, or one caught by a shutdown, stop waiting instead of
    starting its work the moment the slot frees.
    """
    if not wait:
        acquired = _SOLVER.acquire(blocking=False)
    elif give_up is None:
        acquired = _SOLVER.acquire()
    else:
        acquired = _SOLVER.acquire(timeout=_WAIT_STEP_SECONDS)
        while not acquired and not give_up():
            acquired = _SOLVER.acquire(timeout=_WAIT_STEP_SECONDS)
    if not acquired:
        raise SolverBusy
    try:
        yield
    finally:
        _SOLVER.release()
