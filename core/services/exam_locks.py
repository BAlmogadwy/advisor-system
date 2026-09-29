"""Exam locks: days and periods of one exam timetable the committee has closed.

A lock is ``{"day": D}`` (every period of D) or ``{"day": D, "period": P}``
(one cell), named by the timetable header's own labels, as pins are. A locked
cell is CLOSED: nothing new is placed in it and nothing in it moves out. The
exams in it keep their rooms and invigilators exactly, and still count for
their students as fixed context when anything else is placed.

What a lock holds is what the SOURCE run saved (the run the request was
loaded from, ``previous_run_id``), never the draft on the page. Rooms exist
only in saved runs - every loaded action throws away the rooms the client
sends - so a locked exam's rooms have exactly one source, and no locked cell
ever reaches the room allocator. With them the locked courses' saved section
rows are kept too (``section_enrollment`` and the operations snapshot), so the
saved run stays one consistent snapshot that the Student lists can read;
live registrations still drive the student rules, the QA and a drift report.

Locks are saved per timetable (``result_json["exam_locks"]``) only when there
is at least one, so a run without locks is byte for byte what it was before
locks existed. With ``NO_LOCKS`` every hook in the solvers leaves its input
exactly as it is.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from core.services.linked_exams import NO_LINKS, LinkedExams

#: The refusal codes; each ``ExamLocksError`` carries one.
INVALID = "exam_locks_invalid"
OUTSIDE = "exam_locks_outside_timetable"
REPEATED = "exam_locks_repeated"
SOURCE_REQUIRED = "exam_locks_source_required"
SOURCE_INCOMPLETE = "exam_locks_source_incomplete"
SCOPE_CHANGED = "exam_locks_scope_changed"
TERM_CHANGED = "exam_locks_term_changed"
COURSE_NOT_SELECTED = "exam_locks_course_not_selected"
LINK_OUTSIDE = "exam_locks_link_outside"
LINK_ROOM_SHARED = "exam_locks_link_room_shared"
PINNED_ELSEWHERE = "exam_locks_pinned_elsewhere"
PIN_IN_LOCKED_CELL = "exam_locks_pin_in_locked_cell"
MOVED_OUT = "exam_locks_moved_out"
MOVED_IN = "exam_locks_moved_in"
CELL_UNSAVED = "exam_locks_cell_unsaved"

OVERFLOW = "OVERFLOW"
UNASSIGNED = "UNASSIGNED"
#: Operations-snapshot fields taken live, not frozen: who teaches a section is
#: not part of what a lock promises (exams, rooms, invigilators), and a stale
#: name would reach the department files silently.
_LIVE_OPERATIONS_FIELDS = ("instructors", "instructor_source", "instructor_status")


class ExamLocksError(ValueError):
    """A lock request that cannot be honoured, the field that says so, and where."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        field: str,
        courses: Iterable[str] | None = None,
        cell: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.courses = sorted(courses) if courses is not None else None
        self.cell = dict(cell) if cell is not None else None


def _cell(day: str, period: str | None) -> dict[str, str]:
    return {"day": day} if period is None else {"day": day, "period": period}


def _where(day: str, period: str | None) -> str:
    return day if period is None else f"{day} {period}"


def _dumps(value: Any) -> str:
    # The bytes a saved run is written with (``json.dumps(result, ensure_ascii=False)``).
    return json.dumps(value, ensure_ascii=False)


def _identity_of(code: str, meta: Mapping[str, Any]) -> str:
    return str(meta.get("course_identity") or meta.get("source_course_code") or code)


@dataclass(frozen=True)
class ExamLocks:
    """The locks of one request, resolved against its header, courses and source run.

    ``cells`` are the closed (day, period) cells of the CURRENT header.
    ``placements`` maps each locked course, by its display code in this
    request, to its cell. The frozen parts are kept as the JSON text they were
    read from and parsed on every use, so the value stays immutable across the
    invigilator pass's repacks and a restored list serialises back to the exact
    bytes it was saved with.
    """

    canonical: tuple[dict[str, str], ...] = ()
    cells: frozenset[tuple[str, str]] = frozenset()
    placements: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    identities: Mapping[str, str] = field(default_factory=dict)
    rooms_text: Mapping[str, str] = field(default_factory=dict)
    sections_text: Mapping[str, str] = field(default_factory=dict)
    operations_text: Mapping[str, str] = field(default_factory=dict)
    positions: Mapping[tuple[str, str], int] = field(default_factory=dict)
    day_order: tuple[str, ...] = ()
    period_order: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.canonical)

    # ── where ────────────────────────────────────────────────────────────

    def closed_slots(self, slots: Iterable[Mapping[str, Any]]) -> frozenset[int]:
        """The slot indices of every locked cell: no other exam may take one."""
        if not self:
            return frozenset()
        return frozenset(
            int(slot["index"]) for slot in slots if (slot["day"], slot["period"]) in self.cells
        )

    def open_days(self, days: Iterable[str], periods: Iterable[str]) -> set[str]:
        """Days with at least one period a new exam may still take."""
        periods = list(periods)
        return {day for day in days if any((day, period) not in self.cells for period in periods)}

    def pins(self) -> list[dict[str, str]]:
        """The locked courses as fixed placements, in code order."""
        return [
            {"course_code": code, "day": day, "period": period}
            for code, (day, period) in sorted(self.placements.items())
        ]

    def holds(self, entry: Mapping[str, Any]) -> bool:
        """``entry`` is a locked course, sitting at its locked cell."""
        where = self.placements.get(str(entry.get("course_code", "")))
        return where is not None and where == (entry.get("day"), entry.get("period"))

    def in_locked_cell(self, entry: Mapping[str, Any]) -> bool:
        return (entry.get("day"), entry.get("period")) in self.cells

    # ── frozen parts ─────────────────────────────────────────────────────

    def restore_rooms(self, entry: dict) -> None:
        """The saved room rows, verbatim: never re-solved, never re-annotated."""
        entry["rooms"] = json.loads(self.rooms_text[entry["course_code"]])

    def freeze_sections(
        self,
        section_enrollment: dict[str, list[dict]],
        operations_sections: dict[str, list[dict]],
    ) -> list[dict]:
        """Put back the locked courses' saved section rows; return the live drift.

        A locked course's rooms were sized for its saved section rows. Keeping
        those rows keeps the run one snapshot: the Student lists refuse a run
        whose room parts do not add up to its section groups. The live rows
        are compared first, and every group whose membership changed since the
        save is reported (``registrations_changed``) - never re-roomed.
        """
        drift: list[dict] = []
        for code, (day, period) in sorted(self.placements.items()):
            frozen = json.loads(self.sections_text[code])
            live_rows = section_enrollment.get(code, [])
            saved = {(row["section_key"], row["gender"]): row for row in frozen}
            live = {(row["section_key"], row["gender"]): row for row in live_rows}
            for key in sorted(set(saved) | set(live)):
                before, after = saved.get(key), live.get(key)
                if (
                    before is not None
                    and after is not None
                    and before.get("membership_fingerprint") == after.get("membership_fingerprint")
                ):
                    continue
                saved_count = int((before or {}).get("student_count", 0) or 0)
                live_count = int((after or {}).get("student_count", 0) or 0)
                change = (
                    "new"
                    if before is None
                    else "gone"
                    if after is None
                    else "grew"
                    if live_count > saved_count
                    else "shrank"
                    if live_count < saved_count
                    else "swapped"
                )
                drift.append(
                    {
                        "kind": "registrations_changed",
                        "day": day,
                        "period": period,
                        "course_code": code,
                        "section": str((before or after or {}).get("section", "")),
                        "section_key": key[0],
                        "gender": key[1],
                        "saved_count": saved_count,
                        "live_count": live_count,
                        "change": change,
                    }
                )
            section_enrollment[code] = frozen
            if code in operations_sections:
                live_ops = {
                    (row.get("section_key"), row.get("gender")): row
                    for row in operations_sections[code]
                }
                rows = json.loads(self.operations_text[code])
                for row in rows:
                    now = live_ops.get((row.get("section_key"), row.get("gender")))
                    if now is not None:
                        for live_field in _LIVE_OPERATIONS_FIELDS:
                            if live_field in now:
                                row[live_field] = now[live_field]
                operations_sections[code] = rows
        return drift

    # ── defence ──────────────────────────────────────────────────────────

    def require_board(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """A solved board must hold every lock: a breach here is a bug, never a refusal."""
        if not self:
            return
        seen: set[str] = set()
        for entry in entries:
            code = str(entry.get("course_code", ""))
            seen.add(code)
            if code in self.placements and not self.holds(entry):
                raise RuntimeError(f"Locked exam {code} left its locked cell.")
            if code not in self.placements and self.in_locked_cell(entry):
                raise RuntimeError(f"Exam {code} was placed in a locked cell.")
        missing = sorted(set(self.placements) - seen)
        if missing:
            raise RuntimeError(f"Locked exam {missing[0]} is missing from the board.")

    # ── saved forms ──────────────────────────────────────────────────────

    def saved(self) -> list[dict[str, str]]:
        """The locks as a run saves them, and as a request may send them back."""
        return [dict(item) for item in self.canonical]

    def fingerprint_block(self) -> dict[str, Any]:
        """What the input fingerprint adds for a board with locks."""
        return {
            "cells": sorted([day, period] for day, period in self.cells),
            "rooms": {code: json.loads(self.rooms_text[code]) for code in sorted(self.rooms_text)},
        }

    def slot_order(self, day: str, period: str) -> tuple[int, int]:
        day_index = self.day_order.index(day) if day in self.day_order else len(self.day_order)
        period_index = (
            self.period_order.index(period)
            if period in self.period_order
            else len(self.period_order)
        )
        return day_index, period_index


NO_LOCKS = ExamLocks()


# ── request shape ────────────────────────────────────────────────────────────


def require_lock_list(raw: Any) -> list:
    """What a request sent as ``exam_locks``: a list, or the refusal naming the field.

    For the HTTP boundary, where a missing key and an explicit ``null`` differ:
    a missing key means "the source run's locks", ``[]`` means "no locks", and
    anything else - ``null`` included - is refused rather than read as either.
    """
    if not isinstance(raw, list):
        raise ExamLocksError(
            "Locked days and periods must be a list.", code=INVALID, field="exam_locks"
        )
    return raw


def _parse_items(
    raw: list, days: list[str], periods: list[str]
) -> list[tuple[int, str, str | None]]:
    """``(request index, day, period or None)`` per lock, refused in request order."""
    items: list[tuple[int, str, str | None]] = []
    day_locks: set[str] = set()
    cell_locks: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        where = f"exam_locks[{index}]"
        if (
            not isinstance(item, dict)
            or set(item) - {"day", "period"}
            or not isinstance(item.get("day"), str)
            or not item["day"].strip()
            or (
                "period" in item
                and (not isinstance(item["period"], str) or not item["period"].strip())
            )
        ):
            raise ExamLocksError(
                "Each lock must name a day of this timetable, and may name one of its periods.",
                code=INVALID,
                field=where,
            )
        day = item["day"].strip()
        period = item["period"].strip() if "period" in item else None
        if day not in days or (period is not None and period not in periods):
            raise ExamLocksError(
                f"{_where(day, period)} is not in this timetable. Unlock it, or add it back.",
                code=OUTSIDE,
                field=where,
                cell=_cell(day, period),
            )
        repeated = (
            day in day_locks or any(locked_day == day for locked_day, _ in cell_locks)
            if period is None
            else day in day_locks or (day, period) in cell_locks
        )
        if repeated:
            raise ExamLocksError(
                f"{_where(day, period)} is locked twice.",
                code=REPEATED,
                field=where,
                cell=_cell(day, period),
            )
        if period is None:
            day_locks.add(day)
        else:
            cell_locks.add((day, period))
        items.append((index, day, period))
    return items


def _saved_lock_sets(raw: Any) -> tuple[set[str], set[tuple[str, str]]]:
    """The day and cell locks a saved run holds, read leniently: it was validated when saved."""
    day_locks: set[str] = set()
    cell_locks: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("day"), str):
            continue
        if isinstance(item.get("period"), str):
            cell_locks.add((item["day"], item["period"]))
        elif "period" not in item:
            day_locks.add(item["day"])
    return day_locks, cell_locks


def _covered(day: str, period: str, day_locks: set[str], cell_locks: set[tuple[str, str]]) -> bool:
    return day in day_locks or (day, period) in cell_locks


# ── the source run ───────────────────────────────────────────────────────────


def _roster_refusal(source: Mapping[str, Any]) -> str | None:
    """Why the Student lists could not read the source run, or None when they can.

    A locked course's rooms and section rows are carried into every run saved
    from it; if they could not be read as they stand, the new run could not be
    either. This is the Student lists' own saved-run check, so the two can
    never disagree.
    """
    # Imported here: the roster reader imports the timetable pipeline, which
    # imports this module.
    from core.services.exam_rosters import ExportRefused, _saved_run

    run: Any = SimpleNamespace(result_json=_dumps(dict(source)), pk=0, label="", created_at=None)
    try:
        _saved_run(run)
    except ExportRefused as refused:
        return refused.reason or refused.code
    return None


def _require_complete_source_course(
    code: str,
    entry: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    field_name: str,
    cell: dict[str, str],
    shown_as: str | None = None,
) -> tuple[list, list, list]:
    """A locked course's saved rooms, section rows and operations rows, all consistent.

    ``code`` is the course's code in the source; ``shown_as`` its code today,
    which a refusal names.
    """
    named = shown_as or code

    def incomplete() -> ExamLocksError:
        # A fixed-time Check and Save rebuilds the rooms and section rows at
        # the same exam times; a Build would place every exam again.
        return ExamLocksError(
            f"{named} in {_where(cell['day'], cell.get('period'))} has no complete saved rooms. "
            "Check and save this timetable with rooms, then lock it.",
            code=SOURCE_INCOMPLETE,
            field=field_name,
            courses=[named],
            cell=cell,
        )

    rows = (source.get("section_enrollment") or {}).get(code)
    snapshot = source.get("operations_snapshot")
    courses = snapshot.get("courses") if isinstance(snapshot, dict) else None
    operations = (courses or {}).get(code) if isinstance(courses, dict) else None
    sections = operations.get("sections") if isinstance(operations, dict) else None
    rooms = entry.get("rooms")
    if (
        not isinstance(rows, list)
        or not rows
        or not isinstance(sections, list)
        or not isinstance(rooms, list)
        or not rooms
    ):
        raise incomplete()
    expected: dict[tuple[str, str], int] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("section_key"), str)
            or not row["section_key"]
            or not isinstance(row.get("student_count"), int)
        ):
            raise incomplete()
        expected[(row["section_key"], str(row.get("gender", "")))] = row["student_count"]
    seated: dict[tuple[str, str], int] = defaultdict(int)
    for room in rooms:
        if not isinstance(room, dict) or not isinstance(room.get("section_parts"), list):
            raise incomplete()
        for part in room["section_parts"]:
            if not isinstance(part, dict) or not isinstance(part.get("student_count"), int):
                raise incomplete()
            key = (str(part.get("section_key", "")), str(part.get("gender", room.get("gender"))))
            seated[key] += part["student_count"]
    # Every saved group is seated (or UNASSIGNED) exactly: the Student lists'
    # ``room_parts_total`` rule, and a group with no rows at all is not seated.
    if dict(seated) != expected:
        raise incomplete()
    return rooms, rows, sections


def _renumbered_partners(rooms: list, current_of: Mapping[str, str | None]) -> list:
    """Saved room rows with each sharing partner under its code of today.

    A copy: the source run is never changed. Rows no renumbering touches keep
    their saved bytes exactly; a renumbered list is sorted again, as the
    allocator writes it and the department files check it.
    """
    copied = json.loads(_dumps(rooms))
    for room in copied:
        partners = room.get("room_shared_with") if isinstance(room, dict) else None
        if not isinstance(partners, list):
            continue
        now = [current_of.get(str(partner)) or str(partner) for partner in partners]
        if now != partners:
            room["room_shared_with"] = sorted(now)
    return copied


def resolve_exam_locks(
    raw: Any,
    *,
    days: list[str],
    periods: list[str],
    courses: Mapping[str, Mapping[str, Any]],
    source: Mapping[str, Any] | None,
    pinned: list[dict] | None = None,
    links: LinkedExams = NO_LINKS,
    board: Iterable[Mapping[str, Any]] | None = None,
    scope: tuple[list[str] | None, list[str] | None] | None = None,
    assign_rooms: bool = True,
    current_term: tuple[str, str] | None = None,
) -> ExamLocks:
    """Validate a request's locks against its header, courses, pins, links and source.

    ``courses`` maps each selected display code to its metadata (or schedule
    entry): a locked exam is matched by ``course_identity``, and is locked
    under its code in ``courses`` even when that code was renumbered since
    the save (its saved rooms name each sharing partner by its code of
    today). Only an identity that is gone is refused. ``source`` is the
    normalised saved run the request came from: it defines what each locked
    cell holds, with its rooms. ``pinned`` must be validated pins.

    ``board`` is the submitted board of a loaded action (Check, Save, Optimise,
    Fix): a cell already locked in the source must hold exactly its saved exams
    there, and a cell newly locked must match the saved run. Without a board (a
    Build or a multistart) only the source's saved locks - or a part of them -
    are honoured: a Build cannot see what the page shows. ``scope`` is the
    Build's programs and sections, which must be the source's.

    Every refusal is an ``ExamLocksError`` naming the field, the courses and
    the cell. Nothing is ever dropped silently.
    """
    # None is the Python default only; a request's ``null`` never reaches here.
    raw = require_lock_list([] if raw is None else raw)
    items = _parse_items(raw, days, periods)
    if not items:
        return NO_LOCKS
    day_locks = {day for _, day, period in items if period is None}
    cell_locks = {(day, period) for _, day, period in items if period is not None}
    position_of_day = {day: index for index, day, period in items if period is None}
    position_of_cell = {(day, period): index for index, day, period in items if period is not None}

    def position(day: str, period: str) -> int:
        if (day, period) in position_of_cell:
            return position_of_cell[(day, period)]
        return position_of_day[day]

    if not isinstance(source, Mapping) or source.get("status") != "ok":
        raise ExamLocksError(
            "Locks keep a saved timetable as it is. Load the saved timetable, then lock it.",
            code=SOURCE_REQUIRED,
            field="previous_run_id",
        )
    if assign_rooms is not True:
        raise ExamLocksError(
            "Locked days keep their rooms; room assignment must stay on.",
            code=INVALID,
            field="assign_rooms",
        )
    if scope is not None:
        saved_scope = source.get("enrollment_scope") or {}
        if {str(value) for value in scope[0] or []} != {
            str(value) for value in saved_scope.get("programs") or []
        } or {str(value) for value in scope[1] or []} != {
            str(value) for value in saved_scope.get("sections") or []
        }:
            raise ExamLocksError(
                "The locked days were saved for other programs or sections. "
                "Choose the saved programs and sections, or unlock the days.",
                code=SCOPE_CHANGED,
                field="exam_locks",
            )

    header = {(day, period) for day in days for period in periods}
    cells = frozenset(
        (day, period) for day, period in header if _covered(day, period, day_locks, cell_locks)
    )

    # What each lock holds: the source's exams, by the source's own labels. A
    # day lock covers every period that day had when it was saved - one the
    # header no longer has cannot silently let its exams go.
    code_by_identity = {_identity_of(code, meta): code for code, meta in courses.items()}
    source_entries = [
        entry
        for entry in source.get("schedule") or []
        if isinstance(entry, dict) and entry.get("day") != OVERFLOW
    ]
    # A locked exam is its course identity. Its display code is renumbered
    # whenever the scope gains or loses another study-plan name for the same
    # registrar code ("X" <-> "X (1)"/"X (2)"); the exam, its cell and its
    # rooms stay what they are, under the code it has now - as a link does.
    code_by_saved_code = {
        str(entry.get("course_code", "")): code_by_identity.get(
            _identity_of(str(entry.get("course_code", "")), entry)
        )
        for entry in source_entries
    }
    held: list[tuple[str, str, dict, tuple[str, str], int]] = []
    for entry in sorted(
        source_entries,
        key=lambda entry: (int(entry.get("slot_index", 0) or 0), str(entry.get("course_code"))),
    ):
        day, period = str(entry.get("day", "")), str(entry.get("period", ""))
        if day not in day_locks and (day, period) not in cell_locks:
            continue
        saved_code = str(entry.get("course_code", ""))
        index = position_of_cell.get((day, period), position_of_day.get(day, 0))
        where = f"exam_locks[{index}]"
        if (day, period) not in header:
            raise ExamLocksError(
                f"{saved_code} sits in locked {_where(day, period)}, which is not in this "
                f"timetable any more. Unlock {day}, or add the period back.",
                code=OUTSIDE,
                field=where,
                courses=[saved_code],
                cell=_cell(day, period),
            )
        code = code_by_saved_code.get(saved_code)
        if code is None:
            raise ExamLocksError(
                f"{saved_code} is in locked {_where(day, period)} but not selected for this "
                "timetable. Select it again, or unlock the cell.",
                code=COURSE_NOT_SELECTED,
                field=where,
                courses=[saved_code],
                cell=_cell(day, period),
            )
        held.append((code, saved_code, entry, (day, period), index))

    rooms_text: dict[str, str] = {}
    sections_text: dict[str, str] = {}
    operations_text: dict[str, str] = {}
    placements: dict[str, tuple[str, str]] = {}
    identities: dict[str, str] = {}
    if held:
        if source.get("assign_rooms") is not True:
            code, _, _, (day, period), index = held[0]
            raise ExamLocksError(
                "This timetable was saved without rooms. "
                "Check and save it with rooms, then lock it.",
                code=SOURCE_INCOMPLETE,
                field=f"exam_locks[{index}]",
                courses=[code],
                cell=_cell(day, period),
            )
        for code, saved_code, entry, (day, period), index in held:
            rooms, rows, sections = _require_complete_source_course(
                saved_code,
                entry,
                source,
                field_name=f"exam_locks[{index}]",
                cell=_cell(day, period),
                shown_as=code,
            )
            rooms_text[code] = _dumps(_renumbered_partners(rooms, code_by_saved_code))
            sections_text[code] = _dumps(rows)
            operations_text[code] = _dumps(sections)
            placements[code] = (day, period)
            identities[code] = _identity_of(saved_code, entry)
        if _roster_refusal(source) is not None:
            code, _, _, (day, period), index = held[0]
            raise ExamLocksError(
                "This saved timetable cannot be read by the Student lists. "
                "Check and save it with rooms, then lock it.",
                code=SOURCE_INCOMPLETE,
                field=f"exam_locks[{index}]",
                courses=[code],
                cell=_cell(day, period),
            )
        terms = {
            (str(row.get("academic_year", "")), str(row.get("term", "")))
            for code, *_ in held
            for row in json.loads(sections_text[code])
        }
        if current_term is None or terms != {(str(current_term[0]), str(current_term[1]))}:
            code, _, _, (day, period), index = held[0]
            raise ExamLocksError(
                "The locked exams were saved for another academic term. "
                "Unlock them to rebuild the timetable for this term.",
                code=TERM_CHANGED,
                field=f"exam_locks[{index}]",
                courses=sorted(placements),
                cell=_cell(day, period),
            )

    # Rule 4: a link with a locked member is locked whole, in one cell.
    for unit, members in sorted(
        links.members.items(), key=lambda item: links.positions.get(item[0], 0)
    ):
        locked = [code for code in members if code in placements]
        if not locked:
            continue
        cells_held = {placements.get(code) for code in members}
        if len(locked) != len(members) or len(cells_held) != 1:
            day, period = placements[locked[0]]
            outside = [code for code in members if placements.get(code) != (day, period)]
            raise ExamLocksError(
                f"{locked[0]} is locked in {_where(day, period)} and linked to "
                f"{', '.join(outside)}, which is not there. "
                "Unlock the cell or unlink the courses.",
                code=LINK_OUTSIDE,
                field=f"linked_exams[{links.positions.get(unit, 0)}]",
                courses=members,
                cell=_cell(day, period),
            )
    # A room linked courses share in a locked cell stays shared: unlinking them
    # would leave that saved room double booked. Read from the saved rows,
    # where each partner still has the code it was saved under.
    saved_rooms = {code: entry.get("rooms") or [] for code, _, entry, _, _ in held}
    for code, (day, period) in sorted(placements.items()):
        for room in saved_rooms[code]:
            for partner in room.get("room_shared_with") or []:
                current = code_by_saved_code.get(str(partner))
                if current is None or links.unit(current) != links.unit(code):
                    named = current or str(partner)
                    raise ExamLocksError(
                        f"{code} and {named} share a room in locked {_where(day, period)}. "
                        "Unlock the cell before unlinking them.",
                        code=LINK_ROOM_SHARED,
                        field="linked_exams",
                        courses=[code, named],
                        cell=_cell(day, period),
                    )

    # Rule 6, pins: never into or out of a locked cell.
    for pin in pinned or []:
        code, where_pinned = pin["course_code"], (pin["day"], pin["period"])
        if code in placements and where_pinned != placements[code]:
            day, period = placements[code]
            raise ExamLocksError(
                f"{code} is in locked {_where(day, period)}. Unlock it to pin the exam elsewhere.",
                code=PINNED_ELSEWHERE,
                field="pinned",
                courses=[code],
                cell=_cell(day, period),
            )
        if code not in placements and where_pinned in cells:
            day, period = where_pinned
            raise ExamLocksError(
                f"{code} cannot be pinned to locked {_where(day, period)}. Unlock it first.",
                code=PIN_IN_LOCKED_CELL,
                field="pinned",
                courses=[code],
                cell=_cell(day, period),
            )

    # Rule 6, the board. A cell the source already locked must hold its exams
    # and nothing else; a cell locked only now must be the saved run's. A Build
    # has no board, so it honours only what the source already locked.
    saved_days, saved_cells = _saved_lock_sets(source.get("exam_locks"))
    header_order = {
        (day, period): number
        for number, (day, period) in enumerate((day, period) for day in days for period in periods)
    }
    ordered_cells = sorted(cells, key=lambda cell: (position(*cell), header_order[cell]))
    if board is None:
        for day, period in ordered_cells:
            if not _covered(day, period, saved_days, saved_cells):
                raise ExamLocksError(
                    f"{_where(day, period)} is not locked in the saved timetable. "
                    "Save the timetable with the lock, then build again.",
                    code=CELL_UNSAVED,
                    field=f"exam_locks[{position(day, period)}]",
                    cell=_cell(day, period),
                )
    else:
        on_board: dict[str, tuple[str, str] | None] = {}
        by_cell: dict[tuple[str, str], list[str]] = defaultdict(list)
        for placed in board:
            code = str(placed.get("course_code", ""))
            day, period = str(placed.get("day", "")), str(placed.get("period", ""))
            on_board[code] = None if day == OVERFLOW else (day, period)
            if day != OVERFLOW:
                by_cell[(day, period)].append(code)
        for day, period in ordered_cells:
            field_name = f"exam_locks[{position(day, period)}]"
            already = _covered(day, period, saved_days, saved_cells)
            here = sorted(code for code, cell in placements.items() if cell == (day, period))
            for code in here:
                if code not in on_board:
                    raise ExamLocksError(
                        f"{code} is in locked {_where(day, period)} but not selected for this "
                        "timetable. Select it again, or unlock the cell.",
                        code=COURSE_NOT_SELECTED,
                        field=field_name,
                        courses=[code],
                        cell=_cell(day, period),
                    )
                if on_board[code] != (day, period):
                    if not already:
                        raise _unsaved(day, period, field_name)
                    raise ExamLocksError(
                        f"{code} is in locked {_where(day, period)}. Unlock it to move the exam.",
                        code=MOVED_OUT,
                        field=field_name,
                        courses=[code],
                        cell=_cell(day, period),
                    )
            for code in sorted(by_cell.get((day, period), [])):
                if code not in placements:
                    if not already:
                        raise _unsaved(day, period, field_name)
                    raise ExamLocksError(
                        f"{code} cannot be placed in locked {_where(day, period)}. "
                        "Unlock it first.",
                        code=MOVED_IN,
                        field=field_name,
                        courses=[code],
                        cell=_cell(day, period),
                    )

    canonical = tuple(
        _cell(day, period)
        for _, day, period in sorted(
            items,
            key=lambda item: (
                days.index(item[1]),
                -1 if item[2] is None else periods.index(item[2]),
            ),
        )
    )
    return ExamLocks(
        canonical=canonical,
        cells=cells,
        placements=placements,
        identities=identities,
        rooms_text=rooms_text,
        sections_text=sections_text,
        operations_text=operations_text,
        positions={cell: position(*cell) for cell in cells},
        day_order=tuple(days),
        period_order=tuple(periods),
    )


def _unsaved(day: str, period: str, field_name: str) -> ExamLocksError:
    return ExamLocksError(
        f"{_where(day, period)} has unsaved changes. Save the timetable, then lock it.",
        code=CELL_UNSAVED,
        field=field_name,
        cell=_cell(day, period),
    )


# ── reports ──────────────────────────────────────────────────────────────────


def _all_locked(locks: ExamLocks, codes: Iterable[str]) -> bool:
    codes = [str(code) for code in codes]
    return bool(codes) and all(code in locks.placements for code in codes)


def mark_locked_rows(qa: dict, locks: ExamLocks) -> None:
    """Rule 5: say which reported problems sit inside a locked cell.

    Such a problem is reported, never fixed by moving: a row whose courses are
    all locked gets ``"locked": true``. Nothing is added to a run without locks.
    """
    if not locks:
        return
    for key in ("manual_override_details", "schedule_violation_details"):
        for row in qa.get(key) or []:
            if isinstance(row, dict) and _all_locked(locks, row.get("courses") or []):
                row["locked"] = True
    rooms: dict = qa["rooms"] if isinstance(qa.get("rooms"), dict) else {}
    for row in rooms.get("unassigned_room_sections") or []:
        if isinstance(row, dict) and locks.holds(row):
            row["locked"] = True
    for row in rooms.get("room_double_bookings") or []:
        if isinstance(row, dict) and _all_locked(locks, row.get("courses") or []):
            row["locked"] = True
    for row in qa.get("multi_sitting_details") or []:
        if isinstance(row, dict) and str(row.get("course_code", "")) in locks.placements:
            row["locked"] = True
    for row in qa.get("room_feasibility_violations") or []:
        if isinstance(row, dict) and str(row.get("course_code", "")) in locks.placements:
            row["locked"] = True


def exam_locks_qa(
    locks: ExamLocks,
    schedule_entries: list[dict],
    qa: Mapping[str, Any],
    rooms_inventory: list[dict],
    drift: list[dict],
) -> dict[str, Any]:
    """The lock report: what each locked cell holds and what is wrong inside it.

    Counts only, never a student id. A fixed-time Check reproduces it exactly:
    same source, same locks, same live data.
    """
    where_of_slot: dict[int, tuple[str, str]] = {}
    for entry in schedule_entries:
        if entry.get("day") != OVERFLOW:
            where_of_slot[int(entry["slot_index"])] = (entry["day"], entry["period"])

    held: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for code, cell in sorted(locks.placements.items()):
        held[cell].append(
            {"course_code": code, "course_identity": locks.identities.get(code, code)}
        )
    cells = [
        {"day": day, "period": period, "courses": held.get((day, period), [])}
        for day, period in sorted(locks.cells, key=lambda cell: locks.slot_order(*cell))
    ]

    issues: list[dict[str, Any]] = []
    clashes: dict[tuple[int, tuple[str, ...]], int] = defaultdict(int)
    for row in qa.get("same_slot_conflicts") or []:
        slot = int(row.get("slot_index", -1))
        if where_of_slot.get(slot) in locks.cells:
            clashes[(slot, tuple(sorted(row.get("courses") or [])))] += 1
    for (slot, courses), count in sorted(clashes.items()):
        day, period = where_of_slot[slot]
        issues.append(
            {
                "kind": "clash",
                "day": day,
                "period": period,
                "courses": list(courses),
                "student_count": count,
            }
        )
    for row in qa.get("bucket_day_violations") or []:
        if _all_locked(locks, row.get("courses") or []):
            issues.append(
                {
                    "kind": "bucket_day",
                    "day": row.get("day"),
                    "program": row.get("program"),
                    "programme_term": row.get("programme_term"),
                    "courses": list(row.get("courses") or []),
                }
            )
    issues.extend(drift)

    inventory = {str(room.get("room_code", "")): room for room in rooms_inventory}
    seated: dict[tuple[str, str, str], int] = defaultdict(int)
    gender_of: dict[tuple[str, str, str], str] = {}
    courses_in: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for entry in sorted(schedule_entries, key=lambda entry: str(entry.get("course_code"))):
        if not locks.holds(entry):
            continue
        for room in entry.get("rooms") or []:
            room_code = str(room.get("room_code", ""))
            if room_code == UNASSIGNED:
                issues.append(
                    {
                        "kind": "unassigned",
                        "day": entry["day"],
                        "period": entry["period"],
                        "course_code": entry["course_code"],
                        "section": room.get("section", ""),
                        "student_count": int(room.get("student_count", 0) or 0),
                    }
                )
                continue
            key = (entry["day"], entry["period"], room_code)
            seated[key] += int(room.get("student_count", 0) or 0)
            gender_of.setdefault(key, str(room.get("gender", "")))
            courses_in[key].append(entry["course_code"])
    for key in sorted(seated, key=lambda key: (locks.slot_order(key[0], key[1]), key[2])):
        day, period, room_code = key
        room = inventory.get(room_code)
        if room is None:
            issues.append(
                {
                    "kind": "room_unavailable",
                    "day": day,
                    "period": period,
                    "room_code": room_code,
                    "courses": sorted(set(courses_in[key])),
                }
            )
            continue
        capacity = int(room.get("capacity", 0) or 0)
        if seated[key] > capacity:
            issues.append(
                {
                    "kind": "room_over_capacity",
                    "day": day,
                    "period": period,
                    "room_code": room_code,
                    "seated": seated[key],
                    "room_capacity": capacity,
                }
            )
        cohort = str(room.get("section", "") or "")
        if cohort and cohort != gender_of[key]:
            issues.append(
                {
                    "kind": "room_cohort_changed",
                    "day": day,
                    "period": period,
                    "room_code": room_code,
                    "saved_gender": gender_of[key],
                    "gender": cohort,
                }
            )
    rooms_qa: dict = qa["rooms"] if isinstance(qa.get("rooms"), dict) else {}
    for row in rooms_qa.get("room_double_bookings") or []:
        booked = where_of_slot.get(int(row.get("slot_index", -1)))
        if booked is not None and booked in locks.cells:
            issues.append(
                {
                    "kind": "double_booking",
                    "day": booked[0],
                    "period": booked[1],
                    "room_code": row.get("room_code"),
                    "courses": list(row.get("courses") or []),
                }
            )
    return {
        "cells": cells,
        "locked_courses": len(locks.placements),
        "issues": issues,
        "issue_count": len(issues),
    }


# ── sources ──────────────────────────────────────────────────────────────────


def current_exam_term() -> tuple[str, str] | None:
    """The registrar term the exam timetable's live registrations belong to."""
    from core.services.exam_sections import exam_timetable_links

    _links, year, term = exam_timetable_links()
    return (str(year), str(term)) if year and term else None


def _run_id(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def load_lock_source(run_id: Any) -> dict[str, Any] | None:
    """A saved run a lock can be read from: normalised and ``ok``, or None."""
    from core.models import ExamTimetableRun
    from core.services.exam_run_schema import load_normalised_run

    number = _run_id(run_id)
    if number is None:
        return None
    try:
        run = ExamTimetableRun.objects.get(pk=number)
    except ExamTimetableRun.DoesNotExist:
        return None
    payload = dict(load_normalised_run(run))
    return payload if payload.get("status") == "ok" else None


def settle_build_request(payload: dict) -> tuple[list, list, dict[str, Any] | None]:
    """A Build's ``(exam_locks, linked_exams, lock_source)``.

    - ``exam_locks`` sent as ``[]``: no locks, and no source is read - the
      Build master ran.
    - sent non-empty: the source is ``previous_run_id``, which must be readable.
    - not sent: a readable ``previous_run_id``'s saved locks. A lock ends only
      by an explicit unlock, never by a client that forgot to send it. When
      locks are inherited, links not sent are inherited from the same run, so
      locks never arrive without the links they were validated with.

    A Build without locks keeps master's own ``linked_exams`` default.
    """
    linked = payload.get("linked_exams", [])
    if "exam_locks" in payload:
        raw = require_lock_list(payload["exam_locks"])
        if not raw:
            return raw, linked, None
        source = load_lock_source(payload.get("previous_run_id"))
        if source is None:
            raise ExamLocksError(
                "Locks keep a saved timetable as it is. Load the saved timetable, then lock it.",
                code=SOURCE_REQUIRED,
                field="previous_run_id",
            )
        return raw, linked, source
    source = load_lock_source(payload.get("previous_run_id"))
    inherited = source.get("exam_locks") if source is not None else None
    if source is None or not isinstance(inherited, list) or not inherited:
        return [], linked, None
    if "linked_exams" not in payload:
        linked = source.get("linked_exams") or []
    return list(inherited), linked, source


def settle_job_payload(payload: dict) -> dict:
    """A Build job's payload with inherited locks written down when it is submitted.

    The job may wait; a source deleted meanwhile must refuse the Build (its
    locks are explicit now), never drop the locks silently.
    """
    if "exam_locks" in payload:
        return payload
    locks, linked, source = settle_build_request(payload)
    if source is None:
        return payload
    settled = {**payload, "exam_locks": locks}
    if "linked_exams" not in payload:
        settled["linked_exams"] = linked
    return settled
