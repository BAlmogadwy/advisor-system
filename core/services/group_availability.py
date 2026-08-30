"""
core/services/group_availability.py

Group availability / common-free-slot finder.

Given a set of student IDs, aggregate each student's registered weekly schedule
for the term and report how many resolved students are already busy in each
standard teaching slot. Only authoritative registrar evidence contributes;
expected plans and working mappings are deliberately ignored. Free cells
describe only the registered schedule data that was loaded; unresolved or
partially timed records remain visible as non-blocking coverage warnings.

Data source: each student's registered sections for the term — their
``StudentTermSection`` rows joined to the section's ``TermSectionMeeting``
times. A section may be global (imported) or owned by a planning scenario;
either way it contributes only when the link's canonical snapshot class is
``REGISTRAR``. This answers "when does the registrar say these students are
busy?" without treating a forecast or staff draft as an actual commitment.

The busy/free decision is computed by overlapping each student's meeting times
against the canonical lecture/lab grids and an uninterrupted ten-minute
timeline, so it stays correct even when a meeting (e.g. a 100-minute lab, or an
imported off-grid section) does not start exactly on a standard slot boundary.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from core.models import Student, StudentTermSection
from core.services.student_sections import (
    OTHER_BRANCH_SECTION_COHORT,
    section_gender,
    snapshot_class_filter,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
    placeable_slots,
)
from core.services.timetable_snapshots import SnapshotClass, classify_source

# Safety bound on a single group query — registrar groups are small; this guards
# against an accidental paste of the entire cohort.
MAX_STUDENTS = 600

# Cap occupant detail per cell to keep the payload bounded for large groups.
# ``busy_count`` is always exact; only the per-cell occupant list is capped.
_OCCUPANT_CAP = 80


def _timeline_slots() -> list[dict[str, str]]:
    """Return an uninterrupted 10-minute grid from 09:00 through 18:30."""

    def hhmm(total_minutes: int) -> str:
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    return [
        {
            "label": f"{hhmm(start)}-{hhmm(start + 10)}",
            "start": hhmm(start),
            "end": hhmm(start + 10),
        }
        for start in range(9 * 60, 18 * 60 + 30, 10)
    ]


TIMELINE_SLOTS = _timeline_slots()

# Group Availability deliberately extends the curated teaching grids to the
# requested 18:30 boundary without changing the global automatic-placement
# defaults. The late lab option is a 100-minute alternative ending at 18:30;
# like the existing post-lab lecture variants, it may overlap another option.
GROUP_LECTURE_SLOTS = [
    *DEFAULT_SLOTS,
    {"label": "17:15-18:30", "start": "17:15", "end": "18:30"},
]
GROUP_LAB_SLOTS = [
    *placeable_slots(DEFAULT_LAB_SLOTS),
    {"label": "Lab 6", "start": "16:50", "end": "18:30"},
]


def _section_course_identity(term_section: object) -> str:
    """Return the full course identity, including a safe legacy fallback."""
    key = str(getattr(term_section, "course_key", "") or "").strip()
    if key:
        return key
    code = str(getattr(term_section, "course_code", "") or "").strip()
    number = str(getattr(term_section, "course_number", "") or "").strip()
    if code and number and number.casefold() != code.casefold():
        return f"{code}{number}"
    return code or number


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
    snapshot_classes_by_student: dict[int, str] | None = None,
) -> dict:
    """Build one busy/free grid over WEEKDAYS x slots.

    Each cell reports how many distinct students are busy in that slot and the
    (capped) list of occupants so the registrar can see who/what conflicts.
    """
    snapshot_classes_by_student = snapshot_classes_by_student or {}
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
                                    "snapshot_class": snapshot_classes_by_student.get(sid, ""),
                                }
                            )

            free = not busy_students
            if free:
                free_for_all += 1
            cells_by_day[day].append(
                {
                    "busy_count": len(busy_students),
                    "free": free,
                    # The legacy field remains for existing consumers. The new
                    # name is explicit that an unresolved student's unknown
                    # timetable never blocks calculation for everyone else.
                    "free_for_resolved": free,
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
        "free_for_resolved_count": free_for_all,
    }


def _load_schedule_data(
    student_ids: list[int], academic_year: str, term: str
) -> tuple[
    dict[int, list[tuple[str, int, int, str, str]]],
    set[int],
    dict[int, str],
    dict[int, int],
]:
    """Load each student's weekly meetings for the term in one prefetch query.

    Reads ``StudentTermSection`` joined to their ``TermSectionMeeting`` times.
    Sections are included regardless of scenario ownership — the student is booked
    at those times either way.

    Only rows canonically classified as :class:`SnapshotClass.REGISTRAR` are
    used. Expected plans and working mappings never contribute, including for a
    student who has no registrar row. Cohort/branch filtering is deliberately
    applied first so an invalid opposite-cohort row cannot survive merely because
    its source is authoritative.

    Returns ``(meetings_by_student, enrolled_ids, snapshot_classes_by_student,
    unscheduled_section_counts)``. ``enrolled_ids`` is the set of students with
    at least one registered section this term (used to distinguish "no schedule"
    from an unknown ID). Provenance is exposed only for students with at least
    one usable meeting. Unscheduled counts are distinct registered sections with
    no usable weekday meeting, allowing partial coverage to remain non-blocking
    while still being reported truthfully.
    """
    rows = (
        StudentTermSection.objects.filter(
            student_id__in=student_ids,
            academic_year=str(academic_year),
            term=str(term),
        )
        # The other branch's sections are not this student's commitments. Stored
        # links to them survive from earlier scrapes, and without this filter a
        # student whose timetable screen shows nothing was still booked solid
        # here - two screens contradicting each other about the same person.
        .exclude(term_section__section__istartswith="YM")
        .exclude(term_section__section__istartswith="YF")
        .select_related("term_section")
        .prefetch_related("term_section__meetings")
    )

    # Match get_student_term_baseline's canonical cohort rule. A known M/F
    # student keeps their own local cohort plus genuinely shared sections. A
    # student whose cohort is blank keeps both local cohorts so incomplete
    # profile data does not erase a schedule. YM/YF is another branch and is
    # rejected for everyone. This filtering remains before source selection so
    # the accepted row set is first made truthful for the student's cohort.
    student_cohorts = {
        sid: cohort
        for sid, raw_cohort in Student.objects.filter(student_id__in=student_ids).values_list(
            "student_id", "section"
        )
        if (cohort := str(raw_cohort or "").strip().upper()) in ("M", "F")
    }
    by_student: dict[int, list[StudentTermSection]] = defaultdict(list)
    for sts in rows:
        section_cohort = section_gender(str(sts.term_section.section or ""))
        if section_cohort == OTHER_BRANCH_SECTION_COHORT:
            continue
        student_cohort = student_cohorts.get(sts.student_id, "")
        if student_cohort and section_cohort and section_cohort != student_cohort:
            continue
        by_student[sts.student_id].append(sts)
    registered: list[StudentTermSection] = []
    for student_rows in by_student.values():
        registered.extend(
            row for row in student_rows if classify_source(row.source) is SnapshotClass.REGISTRAR
        )

    meetings_by_student: dict[int, list[tuple[str, int, int, str, str]]] = defaultdict(list)
    enrolled: set[int] = set()
    registered_section_ids: dict[int, set[int]] = defaultdict(set)
    scheduled_section_ids: dict[int, set[int]] = defaultdict(set)
    for sts in registered:
        enrolled.add(sts.student_id)
        ts = sts.term_section
        registered_section_ids[sts.student_id].add(ts.id)
        course = _section_course_identity(ts)
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
                scheduled_section_ids[sts.student_id].add(ts.id)
                meetings_by_student[sts.student_id].append(
                    (day, start_min, end_min, course, section)
                )
    snapshot_classes_by_student = {sid: "registrar" for sid in meetings_by_student}
    unscheduled_section_counts = {
        sid: len(section_ids - scheduled_section_ids.get(sid, set()))
        for sid, section_ids in registered_section_ids.items()
    }
    return (
        meetings_by_student,
        enrolled,
        snapshot_classes_by_student,
        unscheduled_section_counts,
    )


def _load_meetings_by_student(
    student_ids: list[int], academic_year: str, term: str
) -> tuple[dict[int, list[tuple[str, int, int, str, str]]], set[int]]:
    """Compatibility wrapper returning the historical two-item result."""
    meetings_by_student, enrolled, _snapshot_classes, _unscheduled_counts = _load_schedule_data(
        student_ids, academic_year, term
    )
    return meetings_by_student, enrolled


def resolve_current_term() -> tuple[str, str]:
    """Return the latest registered ``(academic_year, term)``.

    Mirrors the exam-timetable convention (``build_enrolled_sets`` orders by
    ``-academic_year, -term``) so "current timetable" means the same thing
    across screens, without the caller having to pick a term. Returns
    ``("", "")`` when there is no registered timetable data at all.

    Term discovery follows the same registered-only contract as schedule loading.
    An expected-plan-only future term must not make the page ignore the latest
    term for which the registrar actually recorded schedules.
    """
    latest = (
        StudentTermSection.objects.filter(snapshot_class_filter(SnapshotClass.REGISTRAR))
        .order_by("-academic_year", "-term")
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

    (
        meetings_by_student,
        enrolled,
        snapshot_classes_by_student,
        unscheduled_section_counts,
    ) = _load_schedule_data(ordered_ids, year, term_s)

    students: list[dict] = []
    not_found: list[int] = []
    no_schedule: list[int] = []
    partial_schedule: list[int] = []

    for sid in ordered_ids:
        meetings = meetings_by_student.get(sid, [])
        exists = sid in meta
        is_enrolled = sid in enrolled

        if not exists and not is_enrolled:
            not_found.append(sid)
        elif not meetings:
            no_schedule.append(sid)

        unscheduled_section_count = unscheduled_section_counts.get(sid, 0)
        if meetings and unscheduled_section_count:
            partial_schedule.append(sid)

        info = meta.get(sid) or {}
        students.append(
            {
                "student_id": sid,
                "name": info.get("name") or "",
                "program": info.get("program") or "",
                "found": exists or is_enrolled,
                "meeting_count": len(meetings),
                "snapshot_class": snapshot_classes_by_student.get(sid, ""),
                "unscheduled_section_count": unscheduled_section_count,
            }
        )

    resolved_count = sum(1 for student in students if student["meeting_count"] > 0)
    unresolved_count = len(ordered_ids) - resolved_count
    snapshot_class_counts = {"registrar": 0, "expected": 0, "working": 0}
    for student in students:
        snapshot_class = student["snapshot_class"]
        if student["meeting_count"] > 0 and snapshot_class in snapshot_class_counts:
            snapshot_class_counts[snapshot_class] += 1

    return {
        "academic_year": year,
        "term": term_s,
        "weekdays": list(WEEKDAYS),
        "requested_count": len(ordered_ids),
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
        "coverage_complete": unresolved_count == 0 and not partial_schedule,
        "snapshot_class_counts": snapshot_class_counts,
        "not_found": not_found,
        "no_schedule": no_schedule,
        "partial_schedule": partial_schedule,
        "partial_schedule_count": len(partial_schedule),
        "students": students,
        "grids": {
            "lecture": _build_grid(
                GROUP_LECTURE_SLOTS,
                meetings_by_student,
                snapshot_classes_by_student,
            ),
            # Online-only windows are not lab availability: nothing in the
            # estate is open then, so offering them as free cells would be
            # inviting somebody to book a room that does not exist.
            "lab": _build_grid(
                GROUP_LAB_SLOTS,
                meetings_by_student,
                snapshot_classes_by_student,
            ),
            # Fine-grained diagnostic view: every ten-minute interval is
            # represented, including the midday gap intentionally absent from
            # the curated lecture/lab placement grids.
            "timeline": _build_grid(
                TIMELINE_SLOTS,
                meetings_by_student,
                snapshot_classes_by_student,
            ),
        },
    }
