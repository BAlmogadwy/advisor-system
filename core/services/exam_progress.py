"""How far a running exam timetable action has got, for whoever is watching.

The pipelines call ``current()`` and report through whatever it returns. Outside
a background job that is a reporter that does nothing, so every synchronous
caller - the views, the management commands, the tests - runs exactly as it
did. A background job installs a real reporter for its own thread only: the
reporter lives in a ``ContextVar``, so gunicorn's other request threads never
see it, and a thread pool does not copy it into its workers.

Stages are named, never timed. A stage that can count its work reports a real
fraction through a counter handed to the loop that does the work; a stage that
cannot says nothing more than its name. A percentage invented from elapsed time
would be the one thing on the page that is certainly wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: Counted work: ``tick(done, total)``.
Counter = Callable[[int, int], None]


class JobCancelled(Exception):
    """The registrar cancelled the job; unwind without saving anything.

    Deliberately not a ``ValueError`` - the views turn those into 400s - and
    not a ``RoomAllocationTimeout``, which the invigilator pass absorbs and
    reports as a truncated search.
    """


class ExamProgress:
    """The reporter every pipeline sees when nobody is watching: it does nothing."""

    def stage(self, key: str) -> None:
        """The action has moved on to stage ``key``."""

    def counter(self, key: str) -> Counter:
        """A tick for the loop that does stage ``key``'s countable work."""
        return _ignore

    def check_cancelled(self) -> None:
        """Raise ``JobCancelled`` if the registrar has asked to stop."""


def _ignore(done: int, total: int) -> None:
    return None


_SILENT = ExamProgress()
_current: ContextVar[ExamProgress] = ContextVar("exam_progress", default=_SILENT)


def current() -> ExamProgress:
    return _current.get()


@contextmanager
def reporting(progress: ExamProgress) -> Iterator[ExamProgress]:
    """Install ``progress`` for the calling thread for the duration of the block."""
    token = _current.set(progress)
    try:
        yield progress
    finally:
        _current.reset(token)
