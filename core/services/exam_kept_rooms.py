"""Rooms a saved board keeps when courses are added to its timetable.

Adding courses changes some boards: a new exam arrives, an existing one steps
aside. Every other board stays as the committee left it, rooms included. A
board is one (slot, cohort) pack - the unit the room allocator solves - and a
pack keeps its saved room rows verbatim only when all of this holds:

* the same exams sit in it, by code and identity: none arrived, none left;
* each exam's live section rows for that cohort are its saved ones (section,
  cohort, members and count): the saved rooms were sized for exactly them;
* every saved room still exists, for that cohort, and still seats everyone
  the saved rows put in it;
* the saved rows seat every section group whole (the Student lists' rule).

Anything else is allocated afresh, exactly as a Check would. A locked cell
never reaches this: its rooms are restored by ``ExamLocks``. A later Check of
the new run re-rooms every pack, as it would for any saved run.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from core.services.exam_locks import NO_LOCKS, ExamLocks
from core.services.exam_room_allocation import normalized_rooms

OVERFLOW = "OVERFLOW"
UNASSIGNED = "UNASSIGNED"

#: (slot index, cohort) -> course code -> that course's saved room rows.
KeptRooms = dict[tuple[int, str], dict[str, list[dict]]]


def _cohort(value: Any) -> str:
    return str(value or "U").upper()


def _identity(entry: Mapping[str, Any]) -> str:
    return str(entry.get("course_identity") or entry.get("course_code") or "")


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(row.get("section_key", "")),
        _cohort(row.get("gender")),
        str(row.get("membership_fingerprint", "")),
        int(row.get("student_count", 0) or 0),
    )


def kept_room_packs(
    *,
    source: Mapping[str, Any] | None,
    schedule_entries: list[dict],
    section_enrollment: Mapping[str, list[dict]],
    rooms: list[dict],
    locks: ExamLocks = NO_LOCKS,
) -> KeptRooms:
    """The packs of ``schedule_entries`` whose saved rooms in ``source`` still hold."""
    if not isinstance(source, Mapping) or source.get("assign_rooms") is not True:
        return {}
    inventory = {room["room_code"]: room for room in normalized_rooms(rooms)}
    saved_sections = source.get("section_enrollment") or {}
    saved_entries: dict[str, Mapping[str, Any]] = {}
    saved_packs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for entry in source.get("schedule") or []:
        if not isinstance(entry, dict) or entry.get("day") == OVERFLOW:
            continue
        code = str(entry.get("course_code", ""))
        saved_entries[code] = entry
        for row in saved_sections.get(code) or []:
            if isinstance(row, dict):
                saved_packs[
                    (
                        str(entry.get("day", "")),
                        str(entry.get("period", "")),
                        _cohort(row.get("gender")),
                    )
                ].add(code)

    packs: dict[tuple[int, str], set[str]] = defaultdict(set)
    cell_of: dict[int, tuple[str, str]] = {}
    by_code: dict[str, dict] = {}
    for entry in schedule_entries:
        if entry.get("day") == OVERFLOW or (locks and locks.holds(entry)):
            continue
        code = entry["course_code"]
        by_code[code] = entry
        cell_of[int(entry["slot_index"])] = (str(entry["day"]), str(entry["period"]))
        for row in section_enrollment.get(code) or []:
            packs[(int(entry["slot_index"]), _cohort(row.get("gender")))].add(code)

    kept: KeptRooms = {}
    for (slot, cohort), codes in sorted(packs.items()):
        day, period = cell_of[slot]
        if saved_packs.get((day, period, cohort)) != codes:
            continue
        rows_by_code: dict[str, list[dict]] = {}
        seated_in: dict[str, int] = defaultdict(int)
        whole = True
        for code in sorted(codes):
            saved = saved_entries.get(code)
            if saved is None or _identity(saved) != _identity(by_code[code]):
                whole = False
                break
            live_rows = [
                row
                for row in section_enrollment.get(code) or []
                if _cohort(row.get("gender")) == cohort
            ]
            saved_rows = [
                row
                for row in saved_sections.get(code) or []
                if isinstance(row, dict) and _cohort(row.get("gender")) == cohort
            ]
            if sorted(map(_row_key, live_rows)) != sorted(map(_row_key, saved_rows)):
                whole = False
                break
            # Room groups are numbered per section across the whole course; a
            # section with a group in another cohort would be renumbered.
            keys = {str(row.get("section_key", "")) for row in live_rows}
            elsewhere = {
                str(row.get("section_key", ""))
                for row in section_enrollment.get(code) or []
                if _cohort(row.get("gender")) != cohort
            }
            if keys & elsewhere:
                whole = False
                break
            rooms_rows = [
                room
                for room in saved.get("rooms") or []
                if isinstance(room, dict) and _cohort(room.get("gender")) == cohort
            ]
            seated: dict[str, int] = defaultdict(int)
            for room in rooms_rows:
                parts = room.get("section_parts")
                if not isinstance(parts, list):
                    whole = False
                    break
                for part in parts:
                    if not isinstance(part, dict) or not isinstance(part.get("student_count"), int):
                        whole = False
                        break
                    seated[str(part.get("section_key", ""))] += part["student_count"]
                if room.get("room_code") != UNASSIGNED:
                    seated_in[str(room.get("room_code", ""))] += int(
                        room.get("student_count", 0) or 0
                    )
            expected = {
                str(row.get("section_key", "")): int(row.get("student_count", 0) or 0)
                for row in saved_rows
            }
            if not whole or not rooms_rows or dict(seated) != expected:
                whole = False
                break
            rows_by_code[code] = rooms_rows
        if not whole:
            continue
        if any(
            (room := inventory.get(room_code)) is None
            or room["section"] != cohort
            or room["capacity"] < students
            for room_code, students in seated_in.items()
        ):
            continue
        kept[(slot, cohort)] = rows_by_code
    return kept
