"""'Mainly for programme' marks on scenario sections (owner wish 2026-10-21).

A section a department marks "mainly for AI2" takes AI2's students first, and
students of other programmes prefer the course's other sections. The mark is
stored on ``TermSection.preferred_programme`` and honoured by both seaters:
``timetable_student_assignment`` (the classic seater every screen and export
reads) and ``scheduler.seating`` (Generate's exact confirmation).

Deliberately not a ``timetable_*`` module: the scheduler bridge imports from
here, and the scheduler subsystem stays out of the original planner's modules.
"""

from __future__ import annotations


def normalise_programme(value: object | None) -> str:
    """A programme code as stored and compared: stripped, upper-case, "" for none.

    Matches how the seaters read a student's programme (``Student.program``),
    so " ai2 " on a mark and "AI2" on a student are the same programme.
    """
    return str(value or "").strip().upper()
