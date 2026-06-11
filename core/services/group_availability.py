"""
core/services/group_availability.py

Group availability / common-free-slot finder.

Given a set of student IDs, aggregate each student's CURRENT weekly schedule
(their actual registered sections for the term) and report, for every standard
teaching slot, how many of the students are already busy. A slot that is free
for ALL students in the group is a conflict-free candidate for opening a new
course section for that group.

Data source: each student's registered sections for the term — their
``StudentTermSection`` rows joined to the section's ``TermSectionMeeting``
times. A section may be global (imported) or owned by a planning scenario;
either way the student is booked at those times, so we read all of a student's
term sections regardless of scenario. This answers "when are these students
actually busy this term", which is what the registrar needs before opening a
new section.

The busy/free decision is computed by overlapping each student's real meeting
times against the canonical lecture and lab slot grids, so it stays correct
even when a meeting (e.g. a 100-minute lab, or an imported off-grid section)
does not start exactly on a standard slot boundary.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from core.models import Student, StudentTermSection
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
)

# Safety bound on a single group query — registrar groups are small; this guards
# against an accidental paste of the entire cohort.
MAX_STUDENTS = 600

# Cap occupant detail per cell to keep the payload bounded for large groups.
# ``busy_count`` is always exact; only the per-cell occupant list is capped.
_OCCUPANT_CAP = 80


def _hhmm_to_min(value: object) -> int | None:
    """Parse an ``"HH:MM"`` string into minutes since midnight.

    Returns ``None`` for empty or unparseable values so callers can skip rows
    that carry no usable time (e.g. sections with no scheduled meeting yet).
    """
    text = str(value or "")
    if ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        return None


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open interval overlap test: ``[a_start, a_end) ∩ [b_start, b_end)``."""
    return a_start < b_end and b_start < a_end


def normalise_student_ids(raw_ids: Iterable[object]) -> list[int]:
    """De-duplicate and coerce an iterable of IDs to an ordered list of ints.

    Order of first appearance is preserved; non-numeric entries are dropped;
    the result is truncated to :data:`MAX_STUDENTS`.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in raw_ids:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        ordered.append(sid)
    return ordered[:MAX_STUDENTS]


def _build_grid(
    slots: list[dict],
    meetings_by_student: dict[int, list[tuple[str, int, int, str, str]]],
) -> dict:
    """Build one busy/free grid (lecture or lab) over WEEKDAYS x slots.

    Each cell reports how many distinct students are busy in that slot and the
    (capped) list of occupants so the registrar can see who/what conflicts.
    """
    cells_by_day: dict[str, list[dict]] = {day: [] for day in WEEKDAYS}
    free_for_all = 0

    for day in WEEKDAYS:
        for slot in slots:
            slot_start = _hhmm_to_min(slot.get("start"))
            slot_end = _hhmm_to_min(slot.get("end"))
            busy_students: set[int] = set()
            occupants: list[dict] = []
            seen_occ: set[tuple[int, str, str]] = set()

            if slot_start is not None and slot_end is not None:
                for sid, meetings in meetings_by_student.items():
                    for m_day, m_start, m_end, course, section in meetings:
                        if m_day != day:
                            continue
                        if not _intervals_overlap(m_start, m_end, slot_start, slot_end):
                            continue
                        busy_students.add(sid)
                        key = (sid, course, section)
                        if key not in seen_occ:
                            seen_occ.add(key)
                            occupants.append(
                                {
                                    "student_id": sid,
                                    "course_code": course,
                                    "section": section,
                                }
                            )

            free = not busy_students
            if free:
                free_for_all += 1
            cells_by_day[day].append(
                {
                    "busy_count": len(busy_students),
                    "free": free,
                    "occupants": occupants[:_OCCUPANT_CAP],
                    "occupants_truncated": max(0, len(occupants) - _OCCUPANT_CAP),
                }
            )

    return {
        "slots": [
            {"label": s.get("label", ""), "start": s.get("start", ""), "end": s.get("end", "")}
            for s in slots
        ],
        "cells": cells_by_day,
        "free_for_all_count": free_for_all,
    }


def _load_meetings_by_student(
    student_ids: list[int], academic_year: str, term: str
) -> tuple[dict[int, list[tuple[str, int, int, str, str]]], set[int]]:
    """Load each student's weekly meetings for the term in one prefetch query.

    Reads ``StudentTermSection`` (the student's registered sections) joined to
    their ``TermSectionMeeting`` times. Sections are included regardless of
    scenario ownership — the student is booked at those times either way.

    Returns ``(meetings_by_student, enrolled_ids)`` where ``enrolled_ids`` is
    the set of students with at least one registered section this term (used to
    distinguish "no schedule" from an unknown ID).
    """
    rows = (
        StudentTermSection.objects.filter(
            student_id__in=student_ids,
            academic_year=str(academic_year),
            term=str(term),
        )
        .select_related("term_section")
        .prefetch_related("term_section__meetings")
    )
    meetings_by_student: dict[int, list[tuple[str, int, int, str, str]]] = defaultdict(list)
    enrolled: set[int] = set()
    for sts in rows:
        enrolled.add(sts.student_id)
        ts = sts.term_section
        course = str(ts.course_code or ts.course_key or "")
        section = str(ts.section or "")
        for meeting in ts.meetings.all():
            day = str(meeting.day or "").upper()
            start_min = _hhmm_to_min(meeting.start_time)
            end_min = _hhmm_to_min(meeting.end_time)
            if (
                day in WEEKDAYS
                and start_min is not None
                and end_min is not None
                and end_min > start_min
            ):
                meetings_by_student[sts.student_id].append(
                    (day, start_min, end_min, course, section)
                )
    return meetings_by_student, enrolled


def resolve_current_term() -> tuple[str, str]:
    """Return the current ``(academic_year, term)`` — the latest one present in
    ``StudentTermSection``.

    Mirrors the exam-timetable convention (``build_enrolled_sets`` orders by
    ``-academic_year, -term``) so "current timetable" means the same thing
    across screens, without the caller having to pick a term. Returns
    ``("", "")`` when there is no registration data at all.
    """
    latest = (
        StudentTermSection.objects.order_by("-academic_year", "-term")
        .values_list("academic_year", "term")
        .first()
    )
    if not latest:
        return "", ""
    return str(latest[0] or ""), str(latest[1] or "")


def compute_group_availability(
    student_ids: Iterable[object],
    academic_year: str | None = None,
    term: str | None = None,
) -> dict:
    """Aggregate a group's weekly busy slots and the conflict-free candidates.

    Parameters
    ----------
    student_ids:
        Iterable of student IDs (ints or numeric strings). De-duplicated and
        capped to :data:`MAX_STUDENTS`.
    academic_year, term:
        Optional term override. When omitted, the students' current term is
        auto-detected via :func:`resolve_current_term` — the screen reads
        "their current timetable" without asking the user to pick a term.

    Returns a JSON-serialisable dict — see the module docstring and the
    ``group_availability`` view for the consumed shape.
    """
    ordered_ids = normalise_student_ids(student_ids)
    if academic_year and term:
        year, term_s = str(academic_year), str(term)
    else:
        year, term_s = resolve_current_term()

    meta = {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=ordered_ids).values(
            "student_id", "name", "program"
        )
    }

    meetings_by_student, enrolled = _load_meetings_by_student(ordered_ids, year, term_s)

    students: list[dict] = []
    not_found: list[int] = []
    no_schedule: list[int] = []

    for sid in ordered_ids:
        meetings = meetings_by_student.get(sid, [])
        exists = sid in meta
        is_enrolled = sid in enrolled

        if not exists and not is_enrolled:
            not_found.append(sid)
        elif not meetings:
            no_schedule.append(sid)

        info = meta.get(sid) or {}
        students.append(
            {
                "student_id": sid,
                "name": info.get("name") or "",
                "program": info.get("program") or "",
                "found": exists or is_enrolled,
                "meeting_count": len(meetings),
            }
        )

    return {
        "academic_year": year,
        "term": term_s,
        "weekdays": list(WEEKDAYS),
        "requested_count": len(ordered_ids),
        "resolved_count": sum(1 for s in students if s["meeting_count"] > 0),
        "not_found": not_found,
        "no_schedule": no_schedule,
        "students": students,
        "grids": {
            "lecture": _build_grid(DEFAULT_SLOTS, meetings_by_student),
            "lab": _build_grid(DEFAULT_LAB_SLOTS, meetings_by_student),
        },
    }
