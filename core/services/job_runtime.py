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
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_SOLVER = threading.BoundedSemaphore(1)
_WAIT_STEP_SECONDS = 0.5

#: What holds the slot now, so a caller turned away can be told what it waits
#: for: a timetable-planner run takes twelve to fifteen minutes, an exam job one
#: or two, a Check a few seconds.
HOLDER_EXAM_JOB = "exam_job"
#: An exam action run inside its request: a client that did not ask for a job.
HOLDER_EXAM_SYNC = "exam_sync"
HOLDER_PLANNER = "planner"
HOLDER_CHECK = "check"
HOLDER_MULTISTART = "multistart"
_holder_lock = threading.Lock()
_holder: tuple[str, float] | None = None


class SolverBusy(Exception):
    """Another solve holds the lock and the caller chose not to wait."""

    def __init__(self, holder: dict | None = None) -> None:
        super().__init__("the solver is busy")
        self.holder = holder


def solver_holder() -> dict | None:
    """What holds the solver and for how long, or None if it is free."""
    with _holder_lock:
        if _holder is None:
            return None
        kind, since = _holder
    return {"kind": kind, "seconds": int(time.monotonic() - since)}


def shutting_down() -> bool:
    """Best effort: is this process's interpreter exiting?

    ``_SHUTTING_DOWN`` is set first thing in ``threading._shutdown``, before the
    exit hooks that join a pool's threads; the main thread is only marked
    stopped after them. Either means background work must not start.
    """
    return bool(getattr(threading, "_SHUTTING_DOWN", False)) or not (
        threading.main_thread().is_alive()
    )


@contextmanager
def solver_slot(
    *,
    holder: str,
    wait: bool = True,
    give_up: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Hold the process's one solver slot for the duration of the block.

    ``holder`` says what is solving, for whoever is turned away meanwhile.
    ``wait=False`` is for a request thread, which should answer "busy" at once
    rather than hold one of four request threads for a minute. ``give_up`` is
    for a background job waiting its turn: asked every half second, it lets a
    cancelled job, or one caught by a shutdown, stop waiting instead of
    starting its work the moment the slot frees.
    """
    global _holder
    if not wait:
        acquired = _SOLVER.acquire(blocking=False)
    elif give_up is None:
        acquired = _SOLVER.acquire()
    else:
        acquired = _SOLVER.acquire(timeout=_WAIT_STEP_SECONDS)
        while not acquired and not give_up():
            acquired = _SOLVER.acquire(timeout=_WAIT_STEP_SECONDS)
    if not acquired:
        raise SolverBusy(solver_holder())
    with _holder_lock:
        _holder = (holder, time.monotonic())
    try:
        yield
    finally:
        with _holder_lock:
            _holder = None
        _SOLVER.release()
