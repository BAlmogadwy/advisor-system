"""Who sits which exam, where: rosters rebuilt from the live lists and verified.

A saved exam timetable stores no student IDs (owner decision, 2026-09-25). It
stores, per exam, the teaching-section groups it was built from - each with
its size and a ``membership_fingerprint``, the sha256 of the group's sorted
student IDs - plus the room parts those groups were packed into. This module
rebuilds the students behind every group from the live registrar lists and
checks each group against its saved fingerprint.

ADR - rebuilding membership from live records
---------------------------------------------
``exam_operations_snapshot`` says readers must never reconstruct historic
membership from current student records. A student list cannot honour that
literally, because the IDs were never saved. This module departs from the
doctrine on one condition: every group is verified against the fingerprint the
build saved, with the very helper the build used
(``resolve_exam_section_enrollment``), and every difference is surfaced on the
rows it affects. A group whose fingerprint matches is, provably, the saved
group. Nothing here guesses silently:

* **Matches** - identical students, so identical, reproducible output.
* **Changed** - same group, different students (a same-size swap included).
* **New since save** / **Gone since save** - the group exists on one side only.
* **Program mix** - the live per-programme counts against the saved
  ``operations_snapshot`` ones, which catches a programme change inside one
  course identity that a fingerprint cannot see.

Rules that keep the rebuild honest
----------------------------------
* The rebuild always uses the run's FULL saved ``enrollment_scope``. Course
  identity numbering ("PHYS103 (1)"/"(2)") depends on the population, so a
  filtered rebuild could renumber and mis-join exams.
* The live term must equal the saved one, read from ``section_enrollment``
  only. ``schedule[].term`` is not the academic term (it is shadowed by a
  programme term in the build), and ``TermSection`` IDs are reused across
  terms, so a mismatch refuses instead of joining another term's lists.
* A run that lacks what this needs is refused once and plainly: rebuild and
  save it. Every saved run so far is test data; there is no legacy path.
* Student fields come from exactly one query of ``student_id, name, program,
  section``. Nothing else about a student is ever read here.

``seat_students`` and ``student_exam_flags`` are pure, so the screen and the
file can never disagree about who sits where or who has a clash.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from functools import cached_property

from django.utils import timezone

from core.models import ExamTimetableRun, Student
from core.services.exam_operations_snapshot import EXAM_OPERATIONS_SNAPSHOT_VERSION
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_sections import (
    EXAM_ENROLLMENT_SOURCE,
    exam_timetable_links,
    resolve_exam_section_enrollment,
)
from core.services.exam_timetable import (
    build_enrolled_sets_with_meta,
    select_exam_course_enrollments,
)

logger = logging.getLogger(__name__)

OVERFLOW_DAY = "OVERFLOW"
UNASSIGNED_ROOM = "UNASSIGNED"
GENDERS = ("M", "F", "U")

MATCHES = "matches"
CHANGED = "changed"
NEW = "new"
GONE = "gone"

# Room basis of one student sitting, in the order a room list is read.
BASIS_WHOLE = "whole"
BASIS_SPLIT = "split"
BASIS_NO_SEAT = "no_seat"
BASIS_UNASSIGNED = "unassigned"
BASIS_NOT_SCHEDULED = "not_scheduled"
UNSEATED_BASES = frozenset({BASIS_NO_SEAT, BASIS_UNASSIGNED, BASIS_NOT_SCHEDULED})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_START = re.compile(r"\s*(\d{1,2}):(\d{2})")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


# ── Refusals ───────────────────────────────────────────────────


class ExportRefused(Exception):
    """A gate refused the export. Nothing is audited and no file is made."""

    status = 409
    code = "export_refused"
    message = "This timetable can't be exported."

    def __init__(self, message: str | None = None, *, reason: str = "") -> None:
        self.reason = reason
        super().__init__(message or self.message)


class RebuildRequired(ExportRefused):
    """The saved run lacks what a verified roster needs (owner decision 1)."""

    code = "rebuild_required"
    message = "Rebuild and save this timetable to export student data."


class ListsUnavailable(ExportRefused):
    code = "lists_unavailable"
    message = "No student timetables have been imported for this term."


class ListsTermMismatch(ExportRefused):
    code = "lists_term_mismatch"
    message = (
        "Student lists can't be exported for this timetable: the imported student "
        "timetables are for {live} and this exam timetable is for {saved}."
    )

    def __init__(self, *, live: tuple[str, str], saved: tuple[str, str]) -> None:
        self.live = live
        self.saved = saved
        super().__init__(
            self.message.format(
                live=f"{live[0]} term {live[1]}", saved=f"{saved[0]} term {saved[1]}"
            ),
            reason="term",
        )


def _refuse(reason: str) -> RebuildRequired:
    logger.info("Student export refused, rebuild required: %s", reason)
    return RebuildRequired(reason=reason)


# ── Pure rules ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RoomPart:
    """One saved room part of one section group, exactly as the run sized it."""

    room_code: str
    room_group_index: int
    room_group_count: int
    student_count: int
    slot_index: int
    gender: str
    building: str = ""
    floor: str = ""
    capacity: int | None = None

    @property
    def assigned(self) -> bool:
        return self.room_code != UNASSIGNED_ROOM


def seat_order(parts: Sequence[RoomPart]) -> list[RoomPart]:
    """Real rooms in ``room_group_index`` order, then the unassigned parts."""
    return sorted(
        parts, key=lambda part: (not part.assigned, part.room_group_index, part.room_code)
    )


def seat_students(members: Iterable[int], parts: Sequence[RoomPart]) -> dict[int, RoomPart | None]:
    """Seat a section group by the approved rule (owner decision 2).

    Members are taken in ascending student ID and fill the saved parts in
    ``room_group_index`` order - real rooms first, then any part the timetable
    could not room - each up to its SAVED ``student_count``. Nobody is absorbed
    into spare seats, so every room holds exactly what the screen shows. Anyone
    left over has no seat (``None``): rooms were sized for the saved count.
    """
    ordered = sorted({int(member) for member in members})
    seats: dict[int, RoomPart | None] = {}
    position = 0
    for part in seat_order(parts):
        taken = ordered[position : position + max(0, part.student_count)]
        seats.update(dict.fromkeys(taken, part))
        position += len(taken)
    seats.update(dict.fromkeys(ordered[position:]))
    return seats


def start_time(period: str) -> time | None:
    """The start of a period label such as ``08:00-10:00``; None when unreadable."""
    match = _START.match(str(period or ""))
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def start_label(period: str) -> str:
    start = start_time(period)
    return start.strftime("%H:%M") if start else str(period or "")


@dataclass(frozen=True)
class SittingFlags:
    """Whole-run flags of one student's sitting of one exam."""

    clash_with: tuple[str, ...]
    same_day_with: tuple[tuple[str, str], ...]  # (start "13:00", exam), in slot order
    exams_that_day: int

    @property
    def clash(self) -> bool:
        return bool(self.clash_with)

    @property
    def same_day(self) -> bool:
        return bool(self.same_day_with)


@dataclass(frozen=True)
class FlagIndex:
    """Flags per sitting, and every student's scheduled exams per day."""

    sittings: dict[tuple[int, str], SittingFlags]
    # sid -> day -> ((slot_index, exam, start label), ...) in slot order
    days: dict[int, dict[str, tuple[tuple[int, str, str], ...]]]

    def flagged(self, student_id: int) -> bool:
        return any(len(exams) >= 2 for exams in self.days.get(student_id, {}).values())

    def day_has_clash(self, student_id: int, day: str) -> bool:
        slots = Counter(slot for slot, _exam, _start in self.days[student_id][day])
        return any(count >= 2 for count in slots.values())

    def clash_periods(self, student_id: int) -> int:
        """Periods in which this student has two or more exams."""
        slots = Counter(
            slot for exams in self.days.get(student_id, {}).values() for slot, _e, _s in exams
        )
        return sum(1 for count in slots.values() if count >= 2)


def student_exam_flags(
    members_by_exam: Mapping[str, Iterable[int]], schedule: Sequence[Mapping]
) -> FlagIndex:
    """Clash and same-day flags over the WHOLE run, from live membership.

    *Clash*: another of the student's exams in the same slot. *Same day*:
    another exam that day in a different slot. The two are independent: 08:00
    A, 08:00 B and 13:00 C give Clash on A and B and Same day on all three.
    OVERFLOW exams have no time, so they never flag - exactly as ``_build_qa``
    excludes them. Never read ``qa.overload_details``: it only records days
    above ``max_per_day`` and misses every student with exactly two exams.
    """
    placed: dict[str, tuple[int, str, str]] = {}
    for entry in schedule:
        day = str(entry.get("day") or "")
        if day and day != OVERFLOW_DAY:
            placed[str(entry["course_code"])] = (
                int(entry["slot_index"]),
                day,
                start_label(str(entry.get("period") or "")),
            )
    by_student: dict[int, dict[str, list[tuple[int, str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for exam, members in members_by_exam.items():
        if exam not in placed:
            continue
        slot, day, start = placed[exam]
        for sid in members:
            by_student[int(sid)][day].append((slot, exam, start))
    days: dict[int, dict[str, tuple[tuple[int, str, str], ...]]] = {}
    sittings: dict[tuple[int, str], SittingFlags] = {}
    for sid, per_day in by_student.items():
        days[sid] = {}
        for day, exams in per_day.items():
            ordered = tuple(sorted(exams, key=lambda item: (item[0], natural_key(item[1]))))
            days[sid][day] = ordered
            for slot, exam, _start in ordered:
                sittings[(sid, exam)] = SittingFlags(
                    clash_with=tuple(e for s, e, _ in ordered if s == slot and e != exam),
                    same_day_with=tuple((st, e) for s, e, st in ordered if s != slot),
                    exams_that_day=len(ordered),
                )
    return FlagIndex(sittings=sittings, days=days)


def natural_key(label: str) -> tuple:
    """M2 before M10 and "PHYS103 (2)" after "PHYS103 (1)": the house order."""
    return tuple(
        (1, int(part)) if part.isdecimal() else (0, part.casefold())
        for part in re.split(r"(\d+)", str(label or ""))
    )


def lists_code(lines: Iterable[str]) -> str:
    """``L-`` + 10 hex of the whole run's group fingerprints: one code per list state."""
    digest = hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()
    return "L-" + digest[:10].upper()


def _lists_line(identity: str, section_key: str, gender: str, fingerprint: str) -> str:
    return "\x1f".join((identity, section_key, gender, fingerprint))


# ── The saved run ──────────────────────────────────────────────


def _text(value: object, *, blank: bool = True) -> bool:
    return isinstance(value, str) and (blank or bool(value.strip())) and not _CONTROL.search(value)


def _count(value: object, *, positive: bool = False) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and (value > 0 if positive else value >= 0)
    )


@dataclass(frozen=True)
class ExamFacts:
    """One saved exam, as the timetable placed it."""

    code: str
    source_code: str
    name: str
    identity: str
    day: str
    period: str
    slot_index: int
    is_online: bool
    day_no: int | None
    start: time | None
    in_lists: bool = True

    @property
    def scheduled(self) -> bool:
        return self.day != OVERFLOW_DAY


@dataclass(frozen=True)
class SavedGroup:
    exam: str
    section_key: str
    gender: str
    section: str
    mapping_status: str
    student_count: int
    fingerprint: str
    program_counts: dict[str, int]


@dataclass
class SavedRun:
    run_id: int
    label: str
    saved_at: datetime
    primary_status: str
    input_fingerprint: str
    programs: list[str]
    sections: list[str]
    academic_year: str
    term: str
    assign_rooms: bool
    schedule: list[dict]
    exams: dict[str, ExamFacts]
    days: list[str]
    groups: dict[str, dict[tuple[str, str], SavedGroup]]
    parts: dict[tuple[str, str, str], tuple[RoomPart, ...]]
    rooms: dict[tuple[int, str], dict]
    qa: dict
    students_count: int


def _saved_run(run: ExamTimetableRun) -> SavedRun:
    """Validate the saved payload; anything short of a verifiable run refuses."""
    data = load_normalised_run(run)
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise _refuse("status")
    if data.get("enrollment_source") != EXAM_ENROLLMENT_SOURCE:
        raise _refuse("enrollment_source")
    scope = data.get("enrollment_scope")
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(key), list) or not all(_text(v) for v in scope[key])
        for key in ("programs", "sections")
    ):
        raise _refuse("enrollment_scope")
    schedule = data.get("schedule")
    if not isinstance(schedule, list) or not schedule:
        raise _refuse("schedule")
    enrollment = data.get("section_enrollment")
    snapshot = data.get("operations_snapshot")
    if not isinstance(enrollment, dict):
        raise _refuse("section_enrollment")
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("version")) is not int
        or snapshot["version"] != EXAM_OPERATIONS_SNAPSHOT_VERSION
        or snapshot.get("enrollment_source") != EXAM_ENROLLMENT_SOURCE
        or not isinstance(snapshot.get("courses"), dict)
    ):
        raise _refuse("operations_snapshot")
    slots = data.get("slots")
    if not isinstance(slots, list) or any(
        not isinstance(slot, dict)
        or not _count(slot.get("index"))
        or not _text(slot.get("day"), blank=False)
        or not _text(slot.get("period"))
        for slot in slots
    ):
        raise _refuse("slots")

    codes: list[str] = []
    for entry in schedule:
        if (
            not isinstance(entry, dict)
            or not _text(entry.get("course_code"), blank=False)
            or not _count(entry.get("slot_index"))
            or not _text(entry.get("day"), blank=False)
            or not _text(entry.get("period"))
            or not _text(entry.get("course_identity"), blank=False)
            or not _text(entry.get("source_course_code"), blank=False)
            or not _text(entry.get("course_name"))
        ):
            raise _refuse("schedule_entry")
        codes.append(entry["course_code"])
    if len(set(codes)) != len(codes) or set(enrollment) != set(codes):
        raise _refuse("schedule_codes")
    if not set(codes) <= set(snapshot["courses"]):
        raise _refuse("snapshot_codes")

    # Exam days in slot order; OVERFLOW is never a day.
    placed = sorted(
        [(slot["index"], slot["day"]) for slot in slots]
        + [(entry["slot_index"], entry["day"]) for entry in schedule],
    )
    days = list(dict.fromkeys(day for _index, day in placed if day != OVERFLOW_DAY))
    day_no = {day: position for position, day in enumerate(days, 1)}

    exams: dict[str, ExamFacts] = {}
    groups: dict[str, dict[tuple[str, str], SavedGroup]] = {}
    terms: set[tuple[str, str]] = set()
    for entry in schedule:
        code = entry["course_code"]
        scheduled = entry["day"] != OVERFLOW_DAY
        exams[code] = ExamFacts(
            code=code,
            source_code=entry["source_course_code"],
            name=entry["course_name"],
            identity=entry["course_identity"],
            day=entry["day"],
            period=entry["period"] if scheduled else "",
            slot_index=entry["slot_index"],
            is_online=entry.get("is_online") is True,
            day_no=day_no.get(entry["day"]) if scheduled else None,
            start=start_time(entry["period"]) if scheduled else None,
        )
        course_snapshot = snapshot["courses"][code]
        if not isinstance(course_snapshot, dict) or not isinstance(
            course_snapshot.get("sections"), list
        ):
            raise _refuse("snapshot_course")
        mix: dict[tuple[str, str], dict[str, int]] = {}
        for section in course_snapshot["sections"]:
            counts = section.get("program_counts") if isinstance(section, dict) else None
            if not isinstance(counts, dict) or not all(
                isinstance(program, str) and _count(value) for program, value in counts.items()
            ):
                raise _refuse("snapshot_program_counts")
            mix[(section.get("section_key"), section.get("gender"))] = dict(counts)
        rows = enrollment[code]
        if not isinstance(rows, list):
            raise _refuse("section_enrollment_rows")
        groups[code] = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not _text(row.get("section_key"), blank=False)
                or row.get("gender") not in GENDERS
                or row.get("mapping_status") not in {"mapped", "missing", "ambiguous"}
                or not _text(row.get("section"))
                or not _count(row.get("student_count"), positive=True)
                or not isinstance(row.get("membership_fingerprint"), str)
                or not _HEX64.fullmatch(row["membership_fingerprint"])
                or not _text(row.get("academic_year"), blank=False)
                or not _text(row.get("term"), blank=False)
            ):
                raise _refuse("section_group")
            key = (row["section_key"], row["gender"])
            if key in groups[code] or key not in mix:
                raise _refuse("section_group_key")
            terms.add((row["academic_year"], row["term"]))
            groups[code][key] = SavedGroup(
                exam=code,
                section_key=row["section_key"],
                gender=row["gender"],
                section=row["section"],
                mapping_status=row["mapping_status"],
                student_count=row["student_count"],
                fingerprint=row["membership_fingerprint"],
                program_counts=mix[key],
            )
    if len(terms) != 1:
        raise _refuse("term")
    academic_year, term = next(iter(terms))

    assign_rooms = data.get("assign_rooms", True) is True
    parts: dict[tuple[str, str, str], list[RoomPart]] = defaultdict(list)
    rooms: dict[tuple[int, str], dict] = {}
    for entry in schedule:
        code = entry["course_code"]
        entry_rooms = entry.get("rooms") or []
        if not isinstance(entry_rooms, list):
            raise _refuse("rooms")
        for room in entry_rooms:
            if (
                not isinstance(room, dict)
                or not _text(room.get("room_code"), blank=False)
                or room.get("gender") not in GENDERS
                or not isinstance(room.get("section_parts"), list)
                or not room["section_parts"]
            ):
                raise _refuse("room")
            capacity = room.get("room_capacity")
            capacity = capacity if _count(capacity) else None
            building = room.get("building")
            floor = room.get("floor")
            building = building.strip() if _text(building) else ""
            floor = str(floor).strip() if _text(floor) or _count(floor) else ""
            if room["room_code"] != UNASSIGNED_ROOM:
                slot_room = rooms.setdefault(
                    (entry["slot_index"], room["room_code"]),
                    {
                        "room_code": room["room_code"],
                        "slot_index": entry["slot_index"],
                        "gender": room["gender"],
                        "capacity": capacity,
                        "building": building,
                        "floor": floor,
                        "seated_at_save": 0,
                        "exams": [],
                    },
                )
                slot_room["seated_at_save"] += sum(
                    part.get("student_count", 0)
                    for part in room["section_parts"]
                    if isinstance(part, dict) and _count(part.get("student_count"))
                )
                slot_room["exams"].append(code)
            for part in room["section_parts"]:
                if (
                    not isinstance(part, dict)
                    or not _text(part.get("section_key"), blank=False)
                    or part.get("gender", room["gender"]) != room["gender"]
                    or not _count(part.get("student_count"), positive=True)
                    or not _count(part.get("room_group_index"), positive=True)
                    or not _count(part.get("room_group_count"), positive=True)
                ):
                    raise _refuse("room_part")
                key = (part["section_key"], room["gender"])
                if key not in groups[code]:
                    raise _refuse("room_part_group")
                parts[(code, *key)].append(
                    RoomPart(
                        room_code=room["room_code"],
                        room_group_index=part["room_group_index"],
                        room_group_count=part["room_group_count"],
                        student_count=part["student_count"],
                        slot_index=entry["slot_index"],
                        gender=room["gender"],
                        building=building,
                        floor=floor,
                        capacity=capacity,
                    )
                )
    for key, group_parts in parts.items():
        if sum(part.student_count for part in group_parts) != groups[key[0]][key[1:]].student_count:
            raise _refuse("room_parts_total")
    qa = data.get("qa") if isinstance(data.get("qa"), dict) else {}
    return SavedRun(
        run_id=run.pk,
        label=run.label,
        saved_at=run.created_at,
        primary_status=str(data.get("primary_status") or ""),
        input_fingerprint=str(data.get("input_fingerprint") or ""),
        programs=list(scope["programs"]),
        sections=list(scope["sections"]),
        academic_year=academic_year,
        term=term,
        assign_rooms=assign_rooms,
        schedule=schedule,
        exams=exams,
        days=days,
        groups=groups,
        parts={key: tuple(seat_order(value)) for key, value in parts.items()},
        rooms=rooms,
        qa=qa,
        students_count=data.get("students_count") if _count(data.get("students_count")) else 0,
    )


# ── The roster model ───────────────────────────────────────────


@dataclass(frozen=True)
class StudentFacts:
    student_id: int
    name: str
    program: str


@dataclass(frozen=True)
class SectionGroup:
    """One exam x teaching section x group: the grain of the saved fingerprint."""

    exam: str
    section_key: str
    gender: str
    section: str
    mapping_status: str
    saved_count: int  # 0 when new since save
    members: tuple[int, ...]  # live, ascending
    membership: str
    saved_program_counts: dict[str, int]
    live_program_counts: dict[str, int]
    parts: tuple[RoomPart, ...]
    seats: dict[int, RoomPart | None] = field(repr=False)

    @property
    def program_mix(self) -> str:
        return MATCHES if self.saved_program_counts == self.live_program_counts else CHANGED

    @property
    def assigned_parts(self) -> tuple[RoomPart, ...]:
        return tuple(part for part in self.parts if part.assigned)


@dataclass
class RosterModel:
    saved: SavedRun
    groups: list[SectionGroup]
    students: dict[int, StudentFacts]
    flags: FlagIndex
    missing_exams: tuple[str, ...]
    lists_code_saved: str
    lists_code_now: str
    checked_at: datetime

    @property
    def exams(self) -> dict[str, ExamFacts]:
        return self.saved.exams

    @cached_property
    def group_index(self) -> dict[tuple[str, str, str], SectionGroup]:
        return {(g.exam, g.section_key, g.gender): g for g in self.groups}

    @property
    def changed_groups(self) -> list[SectionGroup]:
        return [group for group in self.groups if group.membership != MATCHES]

    @property
    def lists_match(self) -> bool:
        return self.lists_code_saved == self.lists_code_now and not self.missing_exams

    def basis(self, group: SectionGroup, student_id: int) -> str:
        return sitting_basis(self.saved, group, student_id)


def sitting_basis(saved: SavedRun, group: SectionGroup, student_id: int) -> str:
    exam = saved.exams[group.exam]
    if not exam.scheduled:
        return BASIS_NOT_SCHEDULED
    if not saved.assign_rooms:
        return BASIS_UNASSIGNED
    part = group.seats.get(student_id)
    if part is None:
        return BASIS_NO_SEAT
    if not part.assigned:
        return BASIS_UNASSIGNED
    return BASIS_SPLIT if len(group.parts) > 1 else BASIS_WHOLE


def group_sort_key(exam: ExamFacts, gender: str, section: str, mapping_status: str, key: str):
    return (
        exam.day_no is None,
        exam.day_no or 0,
        exam.start or time.max,
        exam.slot_index,
        natural_key(exam.code),
        GENDERS.index(gender),
        mapping_status != "mapped",
        natural_key(section),
        key,
    )


def build_roster_model(run: ExamTimetableRun, *, now: datetime | None = None) -> RosterModel:
    """Gates, then the live rebuild of every saved group, verified."""
    saved = _saved_run(run)
    _links, year, term = exam_timetable_links()
    if not year or not term:
        raise ListsUnavailable(reason="no_live_term")
    live_term = (str(year), str(term))
    if live_term != (saved.academic_year, saved.term):
        raise ListsTermMismatch(live=live_term, saved=(saved.academic_year, saved.term))

    enrolled, metadata = build_enrolled_sets_with_meta(
        programs=saved.programs or None, sections=saved.sections or None
    )
    missing: list[str] = []
    try:
        selected, selected_meta = select_exam_course_enrollments(
            saved.schedule, enrolled, metadata, missing_out=missing
        )
    except ValueError as exc:
        raise _refuse(f"identity: {exc}") from exc

    population = sorted({sid for members in selected.values() for sid in members})
    students: dict[int, StudentFacts] = {}
    section_by_student: dict[int, str] = {}
    program_by_student: dict[int, str] = {}
    # The only student read in this feature: four allowed fields, one query.
    for sid, name, program, section in Student.objects.filter(pk__in=population).values_list(
        "student_id", "name", "program", "section"
    ):
        students[sid] = StudentFacts(
            student_id=sid,
            name=" ".join(str(name or "").split()),
            program=str(program or "").strip().upper(),
        )
        section_by_student[sid] = str(section or "").strip()
        program_by_student[sid] = program
    for sid in population:
        students.setdefault(sid, StudentFacts(student_id=sid, name="", program=""))

    members_out: dict[str, dict[tuple[str, str], frozenset[int]]] = {}
    live_mix: dict[str, list[dict]] = {}
    live = resolve_exam_section_enrollment(
        selected,
        course_meta=selected_meta,
        section_by_student=section_by_student,
        program_by_student=program_by_student,
        operations_sections=live_mix,
        members_out=members_out,
    )

    groups: list[SectionGroup] = []
    saved_lines: list[str] = []
    live_lines: list[str] = []
    for code in missing:
        saved.exams[code] = replace(saved.exams[code], in_lists=False)
    for code, exam in saved.exams.items():
        saved_groups = saved.groups[code]
        live_groups = {(row["section_key"], row["gender"]): row for row in live.get(code, [])}
        live_counts = {
            (row["section_key"], row["gender"]): dict(row["program_counts"])
            for row in live_mix.get(code, [])
        }
        for key, group in saved_groups.items():
            saved_lines.append(_lists_line(exam.identity, *key, group.fingerprint))
        for key, row in live_groups.items():
            live_lines.append(_lists_line(exam.identity, *key, row["membership_fingerprint"]))
        for key in set(saved_groups) | set(live_groups):
            before = saved_groups.get(key)
            after = live_groups.get(key)
            members = tuple(sorted(members_out.get(code, {}).get(key, frozenset())))
            if before is None:
                verdict = NEW
            elif after is None:
                verdict = GONE
            elif after["membership_fingerprint"] == before.fingerprint:
                verdict = MATCHES
            else:
                verdict = CHANGED
            group_parts = saved.parts.get((code, *key), ())
            groups.append(
                SectionGroup(
                    exam=code,
                    section_key=key[0],
                    gender=key[1],
                    section=(after or {}).get("section", before.section if before else ""),
                    mapping_status=(after or {}).get(
                        "mapping_status", before.mapping_status if before else "missing"
                    ),
                    saved_count=before.student_count if before else 0,
                    members=members,
                    membership=verdict,
                    saved_program_counts=dict(before.program_counts) if before else {},
                    live_program_counts=live_counts.get(key, {}),
                    parts=group_parts,
                    seats=seat_students(members, group_parts),
                )
            )
    groups.sort(
        key=lambda group: group_sort_key(
            saved.exams[group.exam],
            group.gender,
            group.section,
            group.mapping_status,
            group.section_key,
        )
    )
    flags = student_exam_flags(selected, saved.schedule)
    return RosterModel(
        saved=saved,
        groups=groups,
        students=students,
        flags=flags,
        missing_exams=tuple(missing),
        lists_code_saved=lists_code(saved_lines),
        lists_code_now=lists_code(live_lines),
        checked_at=now or timezone.now(),
    )
