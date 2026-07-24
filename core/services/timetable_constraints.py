"""One interval-aware home for the hard scheduling rules.

Historically each optimise stage (greedy, local search, chain search, CP-SAT,
SA) re-implemented the same hard rules, and the copies drifted — most sharply,
instructor-clash keyed on exact ``start_min`` while same-course overlap already
used true interval overlap. So an instructor teaching a 10:30–11:45 lecture and
a 10:45–12:25 lab (overlapping intervals, different starts — the lecture/lab
grids interleave by design) was genuinely double-booked yet invisible to every
clash gate.

This module is the single interval-aware implementation. It is pure — no DB, no
Django models — and operates on the in-memory ``SectionState`` map and the
``{section_id: frozenset[instructor_id]}`` mapping the stages already build.
Each rule offers two forms:

- **whole-board** (``has_*`` / ``count_*``) — for evaluation and repair signals.
- **delta** (``move_introduces_*``) — checks only the *moved* sections against
  the rest, so a pre-existing unrelated violation never blocks an unrelated
  improving move (the whole-board-absolute gates used to paralyse local/chain
  search on any already-imperfect board).

See ``docs/CONSTRAINT-ENGINE-DOR.md``. PR-2a covers instructor clash; the daily
cap, same-course, and room rules migrate onto this module in PR-2b–d.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.services.timetable_same_course import (
    has_same_course_overlap as _windows_have_overlap,
)
from core.services.timetable_same_course import (
    make_meeting_window,
)

# An instructor time window: (day, start_min, end_min, section_id).
_Window = tuple[int, int, int, str]


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True iff [a_start, a_end) and [b_start, b_end) overlap.

    The half-open interval test same-course already relies on
    (``timetable_same_course.meeting_gap_or_overlap``): touching end-to-start
    (a_end == b_start) is *not* an overlap.
    """
    return a_start < b_end and b_start < a_end


def _instructor_windows(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]],
) -> dict[int, list[_Window]]:
    """Group every instructor's meeting windows across all their sections."""
    windows: dict[int, list[_Window]] = {}
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            bucket = windows.setdefault(iid, [])
            for meeting in sec.meetings:
                bucket.append((meeting.day, meeting.start_min, meeting.end_min, section_id))
    return windows


def _overlapping_pair_list(windows: list[_Window]) -> list[tuple[_Window, _Window]]:
    """The window pairs on the same day whose intervals overlap.

    Windows belonging to the *same* section are ignored — a section's own
    meetings are its schedule, not a double-booking of the instructor. Only
    cross-section overlaps are an instructor clash.

    This is the primitive: ``_overlapping_pairs`` is ``len()`` of it, so the
    count and the detail can never disagree about what a clash is. (The whole
    point of this module is that the copies used to drift.)
    """
    ordered = sorted(windows)
    pairs: list[tuple[_Window, _Window]] = []
    for i in range(len(ordered)):
        day_i, start_i, end_i, sec_i = ordered[i]
        for j in range(i + 1, len(ordered)):
            day_j, start_j, end_j, sec_j = ordered[j]
            if day_j != day_i:
                break  # sorted by day → no further same-day windows
            if sec_i == sec_j:
                continue
            if _intervals_overlap(start_i, end_i, start_j, end_j):
                pairs.append((ordered[i], ordered[j]))
    return pairs


def _overlapping_pairs(windows: list[_Window]) -> int:
    """Count of overlapping cross-section window pairs."""
    return len(_overlapping_pair_list(windows))


def has_instructor_clash(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
) -> bool:
    """True if any instructor is double-booked — two sessions of *different*
    sections whose times overlap on the same day (interval overlap, not just an
    identical start). Early-exits on the first clash, so it is cheap inside a
    move-evaluation loop. Cross-course double-booking (an instructor teaching two
    different courses at once) is exactly what this catches.
    """
    if not section_instructor_ids:
        return False
    for windows in _instructor_windows(sections_by_id, section_instructor_ids).values():
        ordered = sorted(windows)
        for i in range(len(ordered)):
            day_i, start_i, end_i, sec_i = ordered[i]
            for j in range(i + 1, len(ordered)):
                day_j, start_j, end_j, sec_j = ordered[j]
                if day_j != day_i:
                    break
                if sec_i == sec_j:
                    continue
                if _intervals_overlap(start_i, end_i, start_j, end_j):
                    return True
    return False


def count_instructor_clashes(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
) -> int:
    """Number of overlapping cross-section window pairs across all instructors —
    0 means clash-free. A monotonic repair signal (fewer is better)."""
    if not section_instructor_ids:
        return 0
    return sum(
        _overlapping_pairs(windows)
        for windows in _instructor_windows(sections_by_id, section_instructor_ids).values()
    )


def list_instructor_clashes(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
) -> list[dict[str, Any]]:
    """Detail form of :func:`count_instructor_clashes` — one row per clashing pair.

    ``len(list_instructor_clashes(...)) == count_instructor_clashes(...)`` always
    (both derive from ``_overlapping_pair_list``). Used by the persist-time
    backstop to report *which* instructor is double-booked where, so a violation
    is actionable rather than just a number.
    """
    if not section_instructor_ids:
        return []
    rows: list[dict[str, Any]] = []
    windows_by_instr = _instructor_windows(sections_by_id, section_instructor_ids)
    for instructor_id in sorted(windows_by_instr):
        for (day, start_a, end_a, sec_a), (_d, start_b, end_b, sec_b) in _overlapping_pair_list(
            windows_by_instr[instructor_id]
        ):
            rows.append(
                {
                    "instructor_id": instructor_id,
                    "day": day,
                    "sections": [sec_a, sec_b],
                    "windows": [[start_a, end_a], [start_b, end_b]],
                }
            )
    return rows


def move_introduces_instructor_clash(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
    moved_section_ids: Iterable[str],
) -> bool:
    """Delta form: True iff a *moved* section now overlaps another of its
    instructor's sessions.

    Only overlaps that involve at least one moved section are considered, so a
    pre-existing clash elsewhere on the board does not reject an unrelated
    improving move. Use this in local/chain search instead of the whole-board
    ``has_instructor_clash`` (which rejects every move while any violation
    exists anywhere).
    """
    if not section_instructor_ids:
        return False
    moved = set(moved_section_ids)
    if not moved:
        return False
    windows_by_instr = _instructor_windows(sections_by_id, section_instructor_ids)
    for windows in windows_by_instr.values():
        ordered = sorted(windows)
        for i in range(len(ordered)):
            day_i, start_i, end_i, sec_i = ordered[i]
            for j in range(i + 1, len(ordered)):
                day_j, start_j, end_j, sec_j = ordered[j]
                if day_j != day_i:
                    break
                if sec_i == sec_j:
                    continue
                if (sec_i in moved or sec_j in moved) and _intervals_overlap(
                    start_i, end_i, start_j, end_j
                ):
                    return True
    return False


# ── Instructor daily-session cap ──────────────────────────────────────────
# A cap on sessions per (instructor, day) — labs + lectures. Unlike clash this
# is a per-day COUNT, so no interval logic is involved; the engine owns it for a
# single source + a delta form (below) that mirrors the clash rule.


def _instructor_day_counts(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]],
) -> dict[tuple[int, int], int]:
    """Sessions per (instructor_id, day) across all sections."""
    counts: dict[tuple[int, int], int] = {}
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            for meeting in sec.meetings:
                key = (iid, meeting.day)
                counts[key] = counts.get(key, 0) + 1
    return counts


def exceeds_instructor_daily_cap(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
    cap: int,
) -> bool:
    """True if any (instructor, day) would hold more than ``cap`` sessions.

    A section taught by N instructors counts toward each of those N. Duck-typed
    over the in-memory section states, cheap enough for a move-evaluation loop.
    """
    if not section_instructor_ids:
        return False
    return any(
        c > cap for c in _instructor_day_counts(sections_by_id, section_instructor_ids).values()
    )


def count_instructor_daily_overloads(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
    cap: int,
) -> int:
    """Total over-cap sessions = Σ over (instructor, day) of ``max(0, count-cap)``.

    A side-band diagnostic (not part of the lexicographic score); 0 means every
    instructor-day is within the cap.
    """
    if not section_instructor_ids:
        return 0
    return sum(
        max(0, c - cap)
        for c in _instructor_day_counts(sections_by_id, section_instructor_ids).values()
    )


def move_exceeds_instructor_daily_cap(
    sections_by_id: Mapping[str, Any],
    section_instructor_ids: Mapping[str, Iterable[int]] | None,
    cap: int,
    moved_section_ids: Iterable[str],
) -> bool:
    """Delta form: True iff a *moved* section now lands on an (instructor, day)
    cell whose total session count exceeds ``cap``.

    Only (instructor, day) cells a moved section actually occupies are checked,
    so a pre-existing over-cap on a cell this move doesn't touch never rejects an
    unrelated improving move (mirrors ``move_introduces_instructor_clash``).
    """
    if not section_instructor_ids:
        return False
    touched: set[tuple[int, int]] = set()
    for sid in moved_section_ids:
        sec = sections_by_id.get(sid)
        if sec is None:
            continue
        for iid in section_instructor_ids.get(sid, ()):  # type: ignore[union-attr]
            for meeting in sec.meetings:
                touched.add((iid, meeting.day))
    if not touched:
        return False
    counts = _instructor_day_counts(sections_by_id, section_instructor_ids)
    return any(counts.get(cell, 0) > cap for cell in touched)


# ── Same-course overlap ───────────────────────────────────────────────────
# Sibling sections of one course must not overlap in time. Registrar convention:
# one instructor teaches all sections of a course, so overlapping siblings are an
# instructor clash even when the instructor_id field is blank. Already
# interval-aware via the timetable_same_course window primitives; the engine owns
# the board-level + delta forms so every stage shares one home.


def _same_course_windows_by_course(sections_by_id: Mapping[str, Any]) -> dict[str, list]:
    """Group every section's meeting windows by course_code."""
    by_course: dict[str, list] = {}
    for sec in sections_by_id.values():
        bucket = by_course.setdefault(sec.course_code, [])
        for meeting in sec.meetings:
            bucket.append(
                make_meeting_window(
                    sec.course_code, meeting.day, meeting.start_min, meeting.end_min, sec.section_id
                )
            )
    return by_course


def has_same_course_overlap(sections_by_id: Mapping[str, Any]) -> bool:
    """True iff two sections of the same course overlap in time (interval)."""
    return any(
        _windows_have_overlap(windows)
        for windows in _same_course_windows_by_course(sections_by_id).values()
    )


def move_introduces_same_course_overlap(
    sections_by_id: Mapping[str, Any],
    moved_section_ids: Iterable[str],
) -> bool:
    """Delta form: True iff a *moved* section now overlaps a same-course sibling.

    Only overlaps involving a moved section (within the courses those sections
    belong to) are considered, so a pre-existing same-course overlap elsewhere on
    the board does not reject an unrelated improving move.
    """
    moved = set(moved_section_ids)
    if not moved:
        return False
    by_course = _same_course_windows_by_course(sections_by_id)
    moved_courses = {sections_by_id[sid].course_code for sid in moved if sid in sections_by_id}
    for course in moved_courses:
        windows = by_course.get(course, [])
        for i in range(len(windows)):
            wa = windows[i]
            for j in range(i + 1, len(windows)):
                wb = windows[j]
                if wa.section_key == wb.section_key:
                    continue
                if (wa.section_key in moved or wb.section_key in moved) and (
                    wa.day == wb.day
                    and _intervals_overlap(wa.start_min, wa.end_min, wb.start_min, wb.end_min)
                ):
                    return True
    return False


__all__ = [
    "count_instructor_clashes",
    "count_instructor_daily_overloads",
    "exceeds_instructor_daily_cap",
    "has_instructor_clash",
    "has_same_course_overlap",
    "list_instructor_clashes",
    "move_exceeds_instructor_daily_cap",
    "move_introduces_instructor_clash",
    "move_introduces_same_course_overlap",
]
