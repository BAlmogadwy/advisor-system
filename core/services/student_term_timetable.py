"""One student's term timetable, read the way the student is shown it.

WHAT THIS RETURNS

Two snapshots of one ``(academic_year, term)``, already separated: what the
registrar recorded, and the department's latest forecast (a staff WORKING mapping
outranks the imported EXPECTED plan, see
:func:`core.services.timetable_snapshots.forecast_rows`). Both carry one row per
MEETING, as :func:`core.services.student_sections.get_student_term_baseline`
produces them, with the course names swapped to Arabic when the caller asks.

THE COHORT RULE IS THE READER'S

``get_student_term_baseline`` applies the canonical cohort rule before any row
leaves it: another cohort's sections and the other branch's (YM/YF) never reach
this module. ``core.services.student_sections`` says that rule must not be
re-implemented anywhere else, so nothing here filters a second time. The home
screen used to, with a private copy that could only re-remove rows the reader had
already removed.

WHERE THIS SITS

The student home screen used to read this inline. Anything that later shows a
student "your expected timetable" -- or keeps a record of what they were shown --
has to show the same rows that card shows, so it reads through here. The same
reader has other consumers that keep their own shapes:
:mod:`core.services.academic_state` folds the rows into per-course evidence and
keeps every snapshot class, for the adviser; the chat's timetable capability reads
``Snapshot.EFFECTIVE``. This module is the student screens' read: rows per meeting,
the registrar's record beside the latest forecast.

WHY IT RAISES

The inline read caught every error and continued with an empty list, so "the
timetable store could not be read" and "no timetable has been prepared for you"
became the same value. :func:`read_student_term_timetable` raises
:class:`TimetableUnavailable` instead; the home screen catches it and still
degrades to "no timetable".
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from core.services.student_helpers import normalize_code
from core.services.student_sections import prefer_arabic_timetable_course_names
from core.services.timetable_snapshots import Snapshot
from core.services.timetable_snapshots import forecast_rows as latest_forecast
from core.services.timetable_snapshots import select as select_snapshot


class TimetableUnavailable(Exception):
    """The term's timetable could not be read. Never the same as "no timetable".

    Raised for ANY error from the reader, including a defect inside it (a
    ``KeyError`` as much as a lost database connection): the screens have always
    degraded on both, and telling them apart is the reader's business. The
    original error is always chained as ``__cause__``.
    """


def _course_codes(rows: list[dict[str, Any]]) -> frozenset[str]:
    """The distinct normalised course codes in ``rows``; blank codes are ignored."""
    return frozenset(code for row in rows if (code := normalize_code(row.get("course_code") or "")))


@dataclass
class StudentTermTimetable:
    """The registrar's record and the latest forecast for one student and term.

    A read result, not a value to keep: the row lists are the caller's own copies,
    and the derived code sets are computed once, from the rows as they were read.
    """

    academic_year: str
    term: str
    #: Registrar evidence only. One row per meeting.
    registered_rows: list[dict[str, Any]]
    #: The department's latest forecast: WORKING if any, else EXPECTED. Never
    #: registrar rows. One row per meeting.
    forecast_rows: list[dict[str, Any]]

    @classmethod
    def empty(cls, academic_year: object, term: object) -> StudentTermTimetable:
        """A term with nothing in it, for a caller that has chosen to degrade."""
        return cls(
            academic_year=str(academic_year),
            term=str(term),
            registered_rows=[],
            forecast_rows=[],
        )

    @property
    def has_registration(self) -> bool:
        return bool(self.registered_rows)

    @property
    def has_forecast(self) -> bool:
        return bool(self.forecast_rows)

    @cached_property
    def registered_codes(self) -> frozenset[str]:
        return _course_codes(self.registered_rows)

    @cached_property
    def forecast_codes(self) -> frozenset[str]:
        return _course_codes(self.forecast_rows)

    @property
    def forecast_not_registered(self) -> list[str]:
        """In the forecast and not recorded by the registrar, sorted.

        Empty unless BOTH snapshots exist: with no registration on file, every
        planned course would be listed, and would read as an accusation rather
        than a difference.
        """
        registered, forecast = self.registered_codes, self.forecast_codes
        return sorted(forecast - registered) if registered and forecast else []

    @property
    def registered_not_forecast(self) -> list[str]:
        """Recorded and not in the forecast, sorted. Empty unless both exist."""
        registered, forecast = self.registered_codes, self.forecast_codes
        return sorted(registered - forecast) if registered and forecast else []


def read_student_term_timetable(
    student_id: int,
    academic_year: object,
    term: object,
    *,
    prefer_arabic_names: bool = False,
) -> StudentTermTimetable:
    """Read one student's term as they are shown it.

    Raises :class:`TimetableUnavailable` when the reader fails. Only the read is
    converted: an error in the Arabic-name swap propagates as itself, as it did
    from the inline code this replaces.
    """
    # Imported at the read boundary, as academic_state does, so tests and
    # instrumentation that wrap the established reader also see this consumer.
    from core.services.student_sections import get_student_term_baseline

    year, term_text = str(academic_year), str(term)
    try:
        rows = get_student_term_baseline(student_id, year, term_text, snapshot=Snapshot.ANY)
    except Exception as exc:  # noqa: BLE001 -- converted, never swallowed
        raise TimetableUnavailable(
            f"timetable for student {student_id} in {year}/{term_text} could not be read"
        ) from exc

    if prefer_arabic_names:
        rows = prefer_arabic_timetable_course_names(rows)

    # Split by PROVENANCE CLASS. A staff mapping (``planner``,
    # ``auto_from_studying``) is a forecast, never a registration.
    return StudentTermTimetable(
        academic_year=year,
        term=term_text,
        registered_rows=[dict(row) for row in select_snapshot(rows, Snapshot.REGISTERED)],
        forecast_rows=[dict(row) for row in latest_forecast(rows)],
    )
