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


def is_enabled() -> bool:
    """``TIMETABLE_SECTION_PROGRAMME_PREFERENCE``, for callers outside the
    ``timetable_*`` modules (the scheduler bridge)."""
    from core.services.timetable_flags import is_section_programme_preference_enabled

    return is_section_programme_preference_enabled()


def section_marks(scenario_id: int, *, source_tag: str) -> dict[tuple[str, str], str]:
    """``{(course_key, section label): programme}`` for the scenario's marked
    sections written by one engine (``source_tag``).

    Read by the scheduler bridge whatever the flag says: it re-creates its own
    sections on every Generate and must carry the marks across, because a mark
    is data a department entered, not behaviour.
    """
    from core.models import TermSection

    rows = (
        TermSection.objects.filter(scenario_id=scenario_id, source_tag=source_tag)
        .exclude(preferred_programme="")
        .values_list("course_key", "section", "preferred_programme")
    )
    return {(key, label): normalise_programme(mark) for key, label, mark in rows}
