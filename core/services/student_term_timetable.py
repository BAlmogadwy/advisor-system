"""One student's term timetable, read the way the student is shown it.

WHAT THIS RETURNS

Two snapshots of one ``(academic_year, term)``, already separated: what the
registrar recorded, and the department's latest forecast (a staff WORKING mapping
outranks the imported EXPECTED plan, see
:func:`core.services.timetable_snapshots.forecast_rows`). Both have been through
the cohort rule, and both carry one row per MEETING, exactly as
:func:`core.services.student_sections.get_student_term_baseline` produces them.

WHY A SERVICE AND NOT THE VIEW

The student home screen used to read this inline. Anything that later shows a
student "your expected timetable" -- or keeps a record of what they were shown --
has to show the same rows that card shows. Two copies of the read would agree
until one of them was changed.

WHY IT RAISES

The inline read caught every error and continued with an empty list, so "the
timetable store could not be read" and "no timetable has been prepared for you"
became the same value. A caller that must not confuse the two had no way to tell
them apart. :func:`read_student_term_timetable` raises
:class:`TimetableUnavailable` instead; the home screen catches it and degrades
exactly as it did before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.services.student_helpers import normalize_code
from core.services.student_sections import (
    get_student_term_baseline,
    prefer_arabic_timetable_course_names,
    section_gender,
    student_gender,
)
from core.services.timetable_provenance import baseline_sections
from core.services.timetable_snapshots import Snapshot, forecast_rows, select


class TimetableUnavailable(Exception):
    """The term's timetable could not be read. Never the same as "no timetable"."""


def visible_to_cohort(rows: Iterable[Mapping[str, Any]], cohort: str) -> list[dict[str, Any]]:
    """The rows a student of ``cohort`` may be shown.

    Their own cohort's sections (``M1`` for an ``M`` student) and genuinely shared
    labels stay. Every other labelled section goes -- the other cohort's and the
    other branch's (``YM``/``YF``) alike, because :func:`section_gender` reports
    the latter as a sentinel that is never blank. A blank ``cohort`` means none is
    recorded, and keeps every row: the reader has already removed the other branch.
    """
    if not cohort:
        return [dict(row) for row in rows]
    return [
        dict(row) for row in rows if section_gender(str(row.get("section") or "")) in ("", cohort)
    ]


def _course_codes(rows: list[dict[str, Any]]) -> frozenset[str]:
    """The distinct course codes of ``rows``, counted once per section."""
    return frozenset(
        normalize_code(row.get("course_code") or "")
        for row in baseline_sections(rows)
        if normalize_code(row.get("course_code") or "")
    )


@dataclass(frozen=True)
class StudentTermTimetable:
    """The registrar's record and the latest forecast for one student and term."""

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

    @property
    def registered_codes(self) -> frozenset[str]:
        return _course_codes(self.registered_rows)

    @property
    def forecast_codes(self) -> frozenset[str]:
        return _course_codes(self.forecast_rows)

    @property
    def expected_not_registered(self) -> list[str]:
        """Planned and not recorded, sorted.

        Empty unless BOTH snapshots exist: with no registration on file, every
        planned course would be listed, and would read as an accusation rather
        than a difference.
        """
        registered, forecast = self.registered_codes, self.forecast_codes
        return sorted(forecast - registered) if registered and forecast else []

    @property
    def registered_not_expected(self) -> list[str]:
        """Recorded and not planned, sorted. Empty unless both snapshots exist."""
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

    Raises :class:`TimetableUnavailable` when the rows cannot be read. Only the
    read itself is converted: an error in the cohort lookup or the Arabic-name
    swap propagates unchanged, as it did from the inline code this replaces.
    """
    year, term_text = str(academic_year), str(term)
    try:
        rows = get_student_term_baseline(student_id, year, term_text, snapshot=Snapshot.ANY)
    except Exception as exc:  # noqa: BLE001 -- converted, never swallowed
        raise TimetableUnavailable(
            f"timetable for student {student_id} in {year}/{term_text} could not be read"
        ) from exc

    # One cohort rule, applied before these rows feed ANY fact a caller derives:
    # the panels, the hours card and the recommendation exclusion must all see the
    # same evidence.
    visible = visible_to_cohort(rows, student_gender(student_id))
    if prefer_arabic_names:
        visible = prefer_arabic_timetable_course_names(visible)

    # Split by PROVENANCE CLASS. A staff mapping (``planner``,
    # ``auto_from_studying``) is a forecast, never a registration.
    return StudentTermTimetable(
        academic_year=year,
        term=term_text,
        registered_rows=[dict(row) for row in select(visible, Snapshot.REGISTERED)],
        forecast_rows=[dict(row) for row in forecast_rows(visible)],
    )
