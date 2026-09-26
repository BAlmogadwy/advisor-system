"""Student lists on screen: one row serializer, navigator facts, lookups and a short cache.

The screen must say exactly what the Excel file says (phase 1), so nothing here
recomputes a roster. Rows are the export's own sittings (``all_sittings``),
chosen by the export's own scope test (``_selected``), from the phase-1 roster
model (``build_roster_model``): the same seat rule, flags and verdicts. A room,
course or section on screen and the same scope's Student exams sheet list the
same students, sections, rooms, bases and flags; a parity test pins this.

Rules this module enforces
--------------------------
* **One serializer.** ``roster_row`` is the only function that turns a student
  sitting into screen data and ``ROSTER_ROW_KEYS`` is its exact key set. The
  student fields are the allowed ones only (owner decision 3): Student ID,
  Name, Program, Section, Exam room and the Clash / Same day flags, with the
  other exam's code. Department (from the programme) and Group (the sitting's
  cohort) are timetable facts. No level; nothing else about a student is read.
* **Timetable facts travel beside the rows**, keyed by code (``exam_facts``,
  ``room_facts``), so a row never repeats them and they never need a student.
* **Navigator facts carry no student**: counts and timetable facts only, so
  they are the one GET, and are not audited (like the export preflight).
* **Language-neutral.** Rows and facts carry codes (``no_seat``, ``M``,
  ``ai-ds``); the page words them in its own language.
* **A short viewing cache.** Building the model reads every live list. For
  VIEWING only, one model per saved run is kept per process for
  ``ROSTER_VIEW_TTL_SECONDS``; every answer says when the lists were checked
  and whether it came from the cache, and Refresh rebuilds. Downloads never
  read this cache: the phase-1 export always rebuilds.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils.crypto import salted_hmac

from core.models import ExamTimetableRun
from core.services.exam_department_export import day_weekday
from core.services.exam_rosters import (
    BASIS_NO_SEAT,
    BASIS_NOT_SCHEDULED,
    BASIS_SPLIT,
    BASIS_UNASSIGNED,
    BASIS_WHOLE,
    CHANGED,
    MATCHES,
    NEW,
    ExamFacts,
    RosterModel,
    SectionGroup,
    build_roster_model,
    natural_key,
    start_time,
)

# The export's own sittings, scope parsing and scope test: the screen reuses
# them so a room or course on screen is, by construction, that scope's file.
from core.services.exam_student_export import (
    ExportOptions,
    ExportOptionsError,
    Sitting,
    _check_summary,
    _department,
    _selected,
    all_sittings,
    available_programs,
    parse_export_options,
    scope_value,
)
from core.services.student_sections import arabic_term_section_course_names

ROSTER_VIEW_TTL_SECONDS = 60
# A whole-run model is a few MB; two runs cover two people comparing two saves
# without holding every run anyone opened in a 512 MB instance.
ROSTER_VIEW_CACHE_RUNS = 2
# One build at a time per process; a request waits this long for another
# thread's build (which it then reuses) before answering 503.
ROSTER_BUILD_WAIT_SECONDS = 20

VIEW_SCOPE_KINDS = ("room", "course", "section")
SEARCH_LIMIT = 8
SEARCH_MIN_DIGITS = 4
SEARCH_MIN_LETTERS = 3
SEARCH_MIN_WORD_LETTERS = 2
SEARCH_MAX_LENGTH = 64
STUDENT_ID_MAX_DIGITS = 12

#: Audit rows never hold a student ID, a name or search text (phase-2 rule):
#: a lookup names the student asked for, and those shown, by this keyed
#: reference instead (``audit_subject_ref``).
AUDIT_SUBJECT_SALT = "core.exam_roster_view.audit_subject"
AUDIT_SUBJECT_PREFIX = "sr-"

SEATED = frozenset({BASIS_WHOLE, BASIS_SPLIT})
CHANGE_MARKERS = {CHANGED: "changed", NEW: "new"}
OTHER_DEPARTMENT = "other"

#: The exact keys of every student row on screen - drawer, room, course,
#: section and lookup alike. Tests pin this set; add nothing without an owner
#: decision (owner decision 3 lists the allowed student fields).
ROSTER_ROW_KEYS = (
    "student_id",
    "name",
    "program",
    "department",
    "group",
    "exam",
    "section_key",
    "section",
    "section_status",
    "room",
    "room_basis",
    "part",
    "clash",
    "clash_with",
    "same_day",
    "same_day_with",
    "exams_that_day",
    "change",
)

#: The exact keys of one Find suggestion (a student-search match).
SEARCH_MATCH_KEYS = ("student_id", "name", "program", "department", "exams")


class RosterRequestError(ExportOptionsError):
    """A roster, lookup or search request the server refuses (400)."""

    code = "invalid_request"


class RosterBusy(Exception):
    """Another request's build held the lists too long; the caller answers 503."""


# ── Words-free helpers ─────────────────────────────────────────


def department_key(program: str) -> str:
    """The department a programme belongs to, as the export groups them."""
    return _department(program)[0] or OTHER_DEPARTMENT


def section_display(group: SectionGroup) -> tuple[str | None, str]:
    """(label, status) exactly as the export words it.

    The export prints the label only for a mapped group that has one, and
    "Not recorded" otherwise, so a mapped group with an empty label reads as
    missing here too.
    """
    if group.mapping_status == "mapped" and group.section:
        return group.section, "mapped"
    if group.mapping_status == "ambiguous":
        return None, "ambiguous"
    return None, "missing"


def _clock_label(exam: ExamFacts) -> str | None:
    return exam.start.strftime("%H:%M") if exam.start else None


def exam_facts(exam: ExamFacts, arabic_names: dict[str, str]) -> dict[str, Any]:
    """One exam as the timetable placed it. Timetable facts only."""
    return {
        "code": exam.code,
        "course_code": exam.source_code,
        "name": exam.name,
        "name_ar": arabic_names.get(exam.source_code) or None,
        "day": exam.day if exam.scheduled else None,
        "day_no": exam.day_no,
        "period": exam.period or None,
        "start": _clock_label(exam),
        "slot_index": exam.slot_index if exam.scheduled else None,
        "scheduled": exam.scheduled,
        "online": exam.is_online,
        "in_lists": exam.in_lists,
    }


def room_facts(model: RosterModel, key: tuple[int, str], seated_now: int) -> dict[str, Any]:
    """One room in one period, with its saved and live seat counts."""
    room = model.saved.rooms[key]
    exams = sorted(set(room["exams"]), key=natural_key)
    return {
        "slot_index": key[0],
        "room_code": key[1],
        "gender": room["gender"],
        "building": room["building"] or None,
        "floor": room["floor"] or None,
        "capacity": room["capacity"],
        "seated_at_save": room["seated_at_save"],
        "seated_now": seated_now,
        # As the export's Rooms sheet decides it.
        "online": any(model.exams[code].is_online for code in exams),
        "exams": exams,
    }


def departments_legend(programs: Iterable[str]) -> dict[str, dict[str, str]]:
    """The department names for the keys the given programmes produce."""
    legend: dict[str, dict[str, str]] = {}
    for program in programs:
        key, english, arabic = _department(program)
        legend.setdefault(key or OTHER_DEPARTMENT, {"en": english, "ar": arabic})
    return dict(sorted(legend.items()))


# ── The one row serializer ─────────────────────────────────────


def roster_row(sitting: Sitting, model: RosterModel) -> dict[str, Any]:
    """The ONLY screen serializer of a student sitting (keys: ``ROSTER_ROW_KEYS``).

    ``part`` is the seat-order position of the student's room part within the
    section group (as the export's "(2 of 2)"), never the saved
    ``room_group_index``, which the build numbers across both cohorts.
    """
    group, flags = sitting.group, sitting.flags
    student = model.students[sitting.student_id]
    label, status = section_display(group)
    seated = sitting.basis in SEATED and sitting.part is not None
    clash = bool(flags and flags.clash)
    same_day = bool(flags and flags.same_day)
    return {
        "student_id": sitting.student_id,
        "name": student.name,
        "program": student.program,
        "department": department_key(student.program),
        "group": group.gender,
        "exam": group.exam,
        "section_key": group.section_key,
        "section": label,
        "section_status": status,
        "room": sitting.part.room_code if seated else None,  # type: ignore[union-attr]
        "room_basis": sitting.basis,
        "part": sitting.seat_rank + 1 if sitting.part is not None else None,
        "clash": clash,
        "clash_with": list(flags.clash_with) if flags and clash else [],
        "same_day": same_day,
        "same_day_with": [code for _start, code in flags.same_day_with]
        if flags and same_day
        else [],
        "exams_that_day": flags.exams_that_day if flags else None,
        "change": CHANGE_MARKERS.get(group.membership),
    }


# ── Name search ────────────────────────────────────────────────

# Arabic folding for search: tashkeel and the superscript alef go, tatweel
# goes, hamza-carrying alefs become alef, alef maksura becomes yeh and teh
# marbuta becomes heh. Arabic-Indic digits read as ASCII. Built from code
# points so the source holds no Arabic or invisible characters.
_TASHKEEL = re.compile("[" + chr(0x064B) + "-" + chr(0x065F) + chr(0x0670) + "]")
_FOLD = str.maketrans(
    {
        0x0623: 0x0627,
        0x0625: 0x0627,
        0x0622: 0x0627,
        0x0671: 0x0627,
        0x0649: 0x064A,
        0x0629: 0x0647,
        0x0640: None,
        **{0x0660 + digit: 0x30 + digit for digit in range(10)},
        **{0x06F0 + digit: 0x30 + digit for digit in range(10)},
    }
)
_DIGITS = re.compile(r"[0-9]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def normalise_search_text(text: str) -> str:
    """Fold a name or a query the same way: case, spacing, Arabic variants."""
    folded = _TASHKEEL.sub("", unicodedata.normalize("NFKC", str(text or ""))).translate(_FOLD)
    return " ".join(folded.casefold().split())


# ── The cached view of one run ─────────────────────────────────


@dataclass
class RosterView:
    """A built roster model plus the indexes the screen reads it through.

    Read-only once built: several request threads share it.
    """

    model: RosterModel
    sittings: list[Sitting]
    by_exam: dict[str, list[Sitting]]
    by_room: dict[tuple[int, str], list[Sitting]]
    by_student: dict[int, list[Sitting]]
    search_names: dict[int, str]
    arabic_names: dict[str, str]
    navigator: dict[str, Any]
    built: float  # the cache clock when the build started

    @property
    def checked_at(self) -> datetime:
        return self.model.checked_at


def build_roster_view(run: ExamTimetableRun, *, built: float) -> RosterView:
    """Gates and the live rebuild (phase 1), then the screen's indexes."""
    model = build_roster_model(run)
    sittings = all_sittings(model)
    by_exam: dict[str, list[Sitting]] = defaultdict(list)
    by_room: dict[tuple[int, str], list[Sitting]] = defaultdict(list)
    by_student: dict[int, list[Sitting]] = defaultdict(list)
    for sitting in sittings:
        by_exam[sitting.exam.code].append(sitting)
        by_student[sitting.student_id].append(sitting)
        if sitting.basis in SEATED and sitting.part is not None:
            by_room[(sitting.exam.slot_index, sitting.part.room_code)].append(sitting)
    arabic_names = arabic_term_section_course_names(
        sorted({exam.source_code for exam in model.exams.values()})
    )
    view = RosterView(
        model=model,
        sittings=sittings,
        by_exam=dict(by_exam),
        by_room=dict(by_room),
        by_student=dict(by_student),
        search_names={
            sid: normalise_search_text(facts.name) for sid, facts in model.students.items()
        },
        arabic_names=arabic_names,
        navigator={},
        built=built,
    )
    view.navigator = navigator_facts(view)
    return view


class RosterViewCache:
    """Per-process, per-run roster views for viewing, ``ttl`` seconds each.

    * One build at a time per process (``build_lock``); a request that waited
      for another request's build of the same run reuses it.
    * ``refresh`` rebuilds unless a build STARTED after the refresh was asked
      (two people pressing Refresh together cost one build).
    * Keyed by run id AND saved time, so a reused id never serves another run.
    * At most ``max_runs`` runs are held; the least recently used goes first,
      and an expired run is dropped at the next request (~19 MB for a whole
      1448 T1 run, measured).
    """

    def __init__(
        self,
        *,
        ttl: float = ROSTER_VIEW_TTL_SECONDS,
        max_runs: int = ROSTER_VIEW_CACHE_RUNS,
        wait: float = ROSTER_BUILD_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        build: Callable[..., RosterView] = build_roster_view,
    ) -> None:
        self.ttl = ttl
        self.max_runs = max_runs
        self.wait = wait
        self.clock = clock
        self.build = build
        self._entries: OrderedDict[tuple[int, str], RosterView] = OrderedDict()
        self._lock = threading.Lock()
        self.build_lock = threading.Lock()

    @staticmethod
    def key(run: ExamTimetableRun) -> tuple[int, str]:
        return (int(run.pk), run.created_at.isoformat() if run.created_at else "")

    def _expired(self, view: RosterView, now: float) -> bool:
        return now - view.built >= self.ttl

    def _usable(self, key: tuple[int, str], asked: float, refresh: bool) -> RosterView | None:
        with self._lock:
            view = self._entries.get(key)
            if view is None:
                return None
            if refresh and view.built < asked:
                return None
            # Checked again here: a request that waited for the build lock
            # may find the run expired since it was swept.
            if not refresh and self._expired(view, self.clock()):
                return None
            self._entries.move_to_end(key)
            return view

    def _sweep(self, keep: tuple[int, str] | None = None) -> None:
        """Drop expired runs so their memory goes as soon as anyone asks again."""
        now = self.clock()
        with self._lock:
            for stale in [k for k, v in self._entries.items() if self._expired(v, now)]:
                if stale != keep:
                    del self._entries[stale]
            while len(self._entries) > self.max_runs:
                self._entries.popitem(last=False)

    def get(self, run: ExamTimetableRun, *, refresh: bool = False) -> tuple[RosterView, bool]:
        """(view, cached): ``cached`` when the lists were read before this request."""
        asked = self.clock()
        key = self.key(run)
        self._sweep()
        view = None if refresh else self._usable(key, asked, refresh=False)
        if view is not None:
            return view, True
        if not self.build_lock.acquire(timeout=self.wait):
            raise RosterBusy
        try:
            # Another request may have built it while this one waited.
            view = self._usable(key, asked, refresh)
            if view is not None:
                return view, view.built < asked
            view = self.build(run, built=self.clock())
            with self._lock:
                self._entries[key] = view
                self._entries.move_to_end(key)
            self._sweep(keep=key)
            return view, False
        finally:
            self.build_lock.release()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


ROSTER_VIEW_CACHE = RosterViewCache()


def freshness(view: RosterView, cached: bool) -> dict[str, Any]:
    """The provenance every answer carries: which run, which lists, how old."""
    saved = view.model.saved
    return {
        "run": {
            "id": saved.run_id,
            "label": saved.label,
            "saved_at": saved.saved_at.isoformat(),
            "academic_year": saved.academic_year,
            "term": saved.term,
        },
        "checked_at": view.checked_at.isoformat(),
        "cached": cached,
        "cache_ttl_seconds": ROSTER_VIEW_TTL_SECONDS,
    }


# ── Navigator facts (GET): counts and timetable facts only ─────


def _slot_key(exam: ExamFacts) -> tuple:
    """Timetable order of an exam's period: day, start time, slot."""
    return (
        exam.day_no is None,
        exam.day_no or 0,
        exam.start is None,
        exam.start.isoformat() if exam.start else "",
        exam.slot_index,
    )


def _time_key(exam: ExamFacts) -> tuple:
    return (*_slot_key(exam), natural_key(exam.code))


def _group_key(group: SectionGroup) -> tuple[str, str, str]:
    return (group.exam, group.section_key, group.gender)


def _floor_rank(floor: str) -> tuple[int, int | str]:
    text = str(floor or "").strip()
    return (0, int(text)) if re.fullmatch(r"-?\d{1,4}", text) else (1, text)


def navigator_facts(view: RosterView) -> dict[str, Any]:
    """Days, periods, rooms and exams with seat and flag counts. No student.

    Every figure is a count over the same sittings the rows come from, so a
    navigator item and the list it opens always agree: a room's figures are
    its own list's (a room's list never holds a student without a seat), and
    a period's students without a seat - more students in a section than the
    saved rooms hold, or a section new since the save - are items of their
    own, each counting the No seat rows of the section list it opens.
    """
    model = view.model
    saved = model.saved

    part_now: Counter = Counter()  # (group key, position) -> sittings holding that part
    group_no_seat: Counter = Counter()
    group_unassigned: Counter = Counter()
    for sitting in view.sittings:
        key = _group_key(sitting.group)
        if sitting.part is not None:
            part_now[(key, sitting.seat_rank + 1)] += 1
        if sitting.basis == BASIS_NO_SEAT:
            group_no_seat[key] += 1
        elif sitting.basis == BASIS_UNASSIGNED:
            group_unassigned[key] += 1

    # Rooms: every saved room of every period, sorted as the committee reads
    # them (building, floor, natural code) within the period.
    parts_in_room: dict[tuple[int, str], list[tuple[SectionGroup, int]]] = defaultdict(list)
    for group in model.groups:
        for position, part in enumerate(group.parts, 1):
            if part.assigned:
                parts_in_room[(part.slot_index, part.room_code)].append((group, position))
    rooms = []
    for room_key in saved.rooms:
        seated = view.by_room.get(room_key, [])
        facts = room_facts(model, room_key, len(seated))
        sections: list[dict[str, Any]] = []
        review: set[str] = set()
        for group, position in parts_in_room.get(room_key, []):
            label, status = section_display(group)
            part = group.parts[position - 1]
            sections.append(
                {
                    "exam": group.exam,
                    "section": label,
                    "section_status": status,
                    # The sitting's cohort, as a row's ``group`` and a tab's
                    # ``gender``: an unrecorded section is named by it.
                    "gender": group.gender,
                    "membership": group.membership,
                    "part": position,
                    "parts": len(group.parts),
                    "saved": part.student_count,
                    "now": part_now[(_group_key(group), position)],
                }
            )
            if group.membership != MATCHES:
                review.add("changed")
            if status != "mapped":
                review.add("not_recorded")
        exam = model.exams[facts["exams"][0]] if facts["exams"] else None
        rooms.append(
            (
                (
                    _slot_key(exam) if exam else (True,),
                    # Rooms with no recorded building come after the buildings.
                    not facts["building"],
                    natural_key(facts["building"] or ""),
                    _floor_rank(facts["floor"] or ""),
                    natural_key(facts["room_code"]),
                ),
                {
                    **facts,
                    "sections": sorted(
                        sections,
                        key=lambda s: (natural_key(s["exam"]), natural_key(s["section"] or "")),
                    ),
                    "flags": {
                        "clash": sum(1 for s in seated if s.flags and s.flags.clash),
                        "same_day": sum(1 for s in seated if s.flags and s.flags.same_day),
                    },
                    "review": sorted(review),
                },
            )
        )
    rooms.sort(key=lambda item: item[0])

    # Section parts the timetable could not room ("Not assigned"), and every
    # student of a run built without rooms.
    not_assigned = []
    for group in model.groups:
        exam = model.exams[group.exam]
        if not exam.scheduled:
            continue
        unassigned_saved = sum(p.student_count for p in group.parts if not p.assigned)
        if not saved.assign_rooms:
            unassigned_saved = group.saved_count
        now = group_unassigned[_group_key(group)]
        if unassigned_saved or now:
            label, status = section_display(group)
            not_assigned.append(
                {
                    "slot_index": exam.slot_index,
                    "exam": group.exam,
                    "section_key": group.section_key,
                    "gender": group.gender,
                    "section": label,
                    "section_status": status,
                    "saved": unassigned_saved,
                    "now": now,
                }
            )

    # Students with no seat: in no room's list, so each section that has them
    # is an item of its period, NEW sections (no saved part at all) included.
    # It opens the section's list, whose No seat rows it counts.
    no_seat = []
    for group in model.groups:
        count = group_no_seat[_group_key(group)]
        if not count:
            continue
        label, status = section_display(group)
        no_seat.append(
            {
                "slot_index": model.exams[group.exam].slot_index,
                "exam": group.exam,
                "section_key": group.section_key,
                "gender": group.gender,
                "section": label,
                "section_status": status,
                "membership": group.membership,
                "saved": group.saved_count,
                "now": len(group.members),
                "no_seat": count,
            }
        )

    # Exams: one item per exam with counts. Its sections, parts and ID ranges
    # come with its roster (POST), so the navigator stays small.
    groups_by_exam: dict[str, list[SectionGroup]] = defaultdict(list)
    for group in model.groups:
        groups_by_exam[group.exam].append(group)
    exams = []
    by_slot: dict[int, list[str]] = defaultdict(list)
    for code, exam in sorted(model.exams.items(), key=lambda item: _time_key(item[1])):
        rows = view.by_exam.get(code, [])
        groups = groups_by_exam.get(code, [])
        flags = {
            "clash": sum(1 for s in rows if s.flags and s.flags.clash),
            "same_day": sum(1 for s in rows if s.flags and s.flags.same_day),
            "no_seat": sum(1 for s in rows if s.basis == BASIS_NO_SEAT),
            "not_recorded": sum(1 for s in rows if section_display(s.group)[1] != "mapped"),
            "not_assigned": sum(1 for s in rows if s.basis == BASIS_UNASSIGNED),
        }
        review = set()
        if not exam.in_lists or any(
            g.membership != MATCHES or g.program_mix != MATCHES for g in groups
        ):
            review.add("changed")
        for reason in ("no_seat", "not_recorded", "not_assigned"):
            if flags[reason]:
                review.add(reason)
        if not exam.scheduled:
            review.add("not_scheduled")
        else:
            by_slot[exam.slot_index].append(code)
        exams.append(
            {
                **exam_facts(exam, view.arabic_names),
                "students": len(rows),
                "saved": sum(g.saved_count for g in groups),
                "rooms": list(
                    dict.fromkeys(
                        part.room_code for g in groups for part in g.parts if part.assigned
                    )
                ),
                "programs": dict(
                    sorted(Counter(model.students[s.student_id].program for s in rows).items())
                ),
                "sections": len(groups),
                "flags": flags,
                "review": sorted(review),
            }
        )

    day_rows: dict[str, list[Sitting]] = defaultdict(list)
    slot_rows: Counter = Counter()
    slot_students: dict[int, set[int]] = defaultdict(set)
    for sitting in view.sittings:
        if sitting.exam.scheduled:
            day_rows[sitting.exam.day].append(sitting)
            slot_rows[sitting.exam.slot_index] += 1
            slot_students[sitting.exam.slot_index].add(sitting.student_id)
    slot_rooms = Counter(slot for slot, _code in saved.rooms)
    day_no = {day: number for number, day in enumerate(saved.days, 1)}
    slots = [
        {
            "slot_index": index,
            "day": day,
            "day_no": day_no.get(day),
            "period": period,
            "start": start.strftime("%H:%M") if (start := start_time(period)) else None,
            "exams": sorted(by_slot.get(index, []), key=natural_key),
            "sittings": slot_rows[index],
            # Distinct, as a day's: a student with a clash sits twice, counts once.
            "students": len(slot_students.get(index, ())),
            "rooms": slot_rooms[index],
        }
        for index, day, period in saved.slots
    ]
    programs = available_programs(model)
    students_by_program = Counter(facts.program for facts in model.students.values())
    return {
        "check": _check_summary(model),
        "days": [
            {
                "day": day,
                "day_no": number,
                "weekday": day_weekday(day),
                "sittings": len(day_rows.get(day, [])),
                "students": len({s.student_id for s in day_rows.get(day, [])}),
            }
            for number, day in enumerate(saved.days, 1)
        ],
        "slots": slots,
        "not_scheduled": [e["code"] for e in exams if not e["scheduled"]],
        "rooms": [room for _key, room in rooms],
        "not_assigned": not_assigned,
        "no_seat": no_seat,
        "exams": exams,
        "programs": [
            {
                "program": program,
                "department": department_key(program),
                "students": students_by_program.get(program, 0),
            }
            for program in programs
        ],
        "departments": departments_legend(programs),
        "totals": {
            "sittings": len(view.sittings),
            "students": len(view.by_student),
            "exams": len(model.exams),
            "rooms": len(saved.rooms),
        },
    }


# ── Scope rosters (POST) ───────────────────────────────────────


def parse_view_scope(payload: object, model: RosterModel) -> ExportOptions:
    """A room, course or section of this run, validated by the export's own rules."""
    if not isinstance(payload, dict) or set(payload) - {"scope", "refresh"}:
        raise RosterRequestError("Send a scope and nothing else.", field="body")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in VIEW_SCOPE_KINDS:
        raise RosterRequestError("Choose a room, a course or a section.", field="scope.kind")
    options = parse_export_options(
        {"scope": scope, "language": "en", "one_file_per_group": False}, model
    )
    if options is None:  # only when no scope is sent, which is refused above
        raise RosterRequestError("Choose a room, a course or a section.", field="scope")
    return options


def select_scope(view: RosterView, options: ExportOptions) -> list[Sitting]:
    """The scope's sittings in seat order, chosen by the export's own test."""
    if options.scope_kind == "room":
        candidates = view.by_room.get((int(options.slot_index or 0), options.room_code), [])
    else:
        candidates = view.by_exam.get(options.exam, [])
    return [sitting for sitting in candidates if _selected(sitting, options, view.model)]


def _scope_groups(view: RosterView, options: ExportOptions) -> list[SectionGroup]:
    model = view.model
    if options.scope_kind == "section":
        return [
            g
            for g in model.groups
            if (g.exam, g.section_key, g.gender)
            == (options.exam, options.section_key, options.gender)
        ]
    if options.scope_kind == "course":
        return [g for g in model.groups if g.exam == options.exam]
    return [
        g
        for g in model.groups
        if any(
            part.assigned
            and part.slot_index == options.slot_index
            and part.room_code == options.room_code
            for part in g.parts
        )
    ]


def _counts(sittings: Sequence[Sitting], model: RosterModel) -> dict[str, Any]:
    return {
        "rows": len(sittings),
        "students": len({s.student_id for s in sittings}),
        "clash": sum(1 for s in sittings if s.flags and s.flags.clash),
        "same_day": sum(1 for s in sittings if s.flags and s.flags.same_day),
        "no_seat": sum(1 for s in sittings if s.basis == BASIS_NO_SEAT),
        "not_recorded": sum(1 for s in sittings if section_display(s.group)[1] != "mapped"),
        "not_assigned": sum(1 for s in sittings if s.basis == BASIS_UNASSIGNED),
        "not_scheduled": sum(1 for s in sittings if s.basis == BASIS_NOT_SCHEDULED),
        "changed": sum(1 for s in sittings if s.group.membership in CHANGE_MARKERS),
        "programs": dict(
            sorted(Counter(model.students[s.student_id].program for s in sittings).items())
        ),
    }


def _referenced_facts(
    view: RosterView, sittings: Sequence[Sitting], exams: Iterable[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Facts of every exam and room the rows mention, flags' other exams included."""
    model = view.model
    codes = set(exams)
    rooms: set[tuple[int, str]] = set()
    for sitting in sittings:
        codes.add(sitting.exam.code)
        if sitting.flags:
            codes.update(sitting.flags.clash_with)
            codes.update(code for _start, code in sitting.flags.same_day_with)
        if sitting.basis in SEATED and sitting.part is not None:
            rooms.add((sitting.exam.slot_index, sitting.part.room_code))
    exam_map = {
        code: exam_facts(model.exams[code], view.arabic_names)
        for code in sorted(codes, key=natural_key)
    }
    room_list = [
        room_facts(model, key, len(view.by_room.get(key, [])))
        for key in sorted(rooms, key=lambda k: (k[0], natural_key(k[1])))
    ]
    return exam_map, room_list


def scope_audit_details(
    view: RosterView, options: ExportOptions, sittings: Sequence[Sitting], cached: bool
) -> dict[str, Any]:
    """What a scope load records: the scope and counts, never a student."""
    model = view.model
    return {
        "run_id": model.saved.run_id,
        "scope": {"kind": options.scope_kind, "value": scope_value(options, model)},
        "rows": len(sittings),
        "students": len({s.student_id for s in sittings}),
        "check": "matches" if model.unchanged else "changed",
        "lists_code_saved": model.lists_code_saved,
        "lists_code_now": model.lists_code_now,
        "lists_checked_at": view.checked_at.isoformat(),
        "cached": cached,
    }


def scope_payload(
    view: RosterView, options: ExportOptions, sittings: Sequence[Sitting]
) -> dict[str, Any]:
    """The roster of one scope: tabs, chip counts, facts and the rows."""
    model = view.model
    by_group: dict[tuple[str, str, str], list[Sitting]] = defaultdict(list)
    for sitting in sittings:
        by_group[(sitting.group.exam, sitting.group.section_key, sitting.group.gender)].append(
            sitting
        )
    sections = []
    for group in _scope_groups(view, options):
        rows = by_group.get((group.exam, group.section_key, group.gender), [])
        label, status = section_display(group)
        parts = []
        for position, part in enumerate(group.parts, 1):
            ids = [s.student_id for s in rows if s.part is not None and s.seat_rank + 1 == position]
            parts.append(
                {
                    "position": position,
                    "room": part.room_code if part.assigned else None,
                    "saved": part.student_count,
                    "rows": len(ids),
                    # From this scope's rows only: a range never names a
                    # student the scope (and so the audit row) does not cover.
                    "first_id": min(ids) if ids else None,
                    "last_id": max(ids) if ids else None,
                }
            )
        sections.append(
            {
                "exam": group.exam,
                "section_key": group.section_key,
                "gender": group.gender,
                "section": label,
                "section_status": status,
                "membership": group.membership,
                "program_mix": group.program_mix,
                "saved": group.saved_count,
                "now": len(group.members),
                "rows": len(rows),
                "no_seat": sum(1 for s in rows if s.basis == BASIS_NO_SEAT),
                "parts": parts,
            }
        )
    scope: dict[str, Any] = {"kind": options.scope_kind, "value": scope_value(options, model)}
    room: dict[str, Any] | None = None
    if options.scope_kind == "room":
        room_key = (int(options.slot_index or 0), options.room_code)
        scope.update(slot_index=room_key[0], room_code=room_key[1])
        room = room_facts(model, room_key, len(view.by_room.get(room_key, [])))
        scope_exams = list(room["exams"])
    else:
        scope.update(exam=options.exam)
        if options.scope_kind == "section":
            scope.update(section_key=options.section_key, gender=options.gender)
        scope_exams = [options.exam]
    exam_map, room_list = _referenced_facts(view, sittings, scope_exams)
    return {
        "scope": scope,
        "room": room,
        "sections": sections,
        "counts": _counts(sittings, model),
        "exams": exam_map,
        "rooms": room_list,
        "departments": departments_legend(model.students[s.student_id].program for s in sittings),
        "rows": [roster_row(sitting, model) for sitting in sittings],
    }


# ── Student lookup and search (POST) ───────────────────────────


@dataclass(frozen=True)
class LookupRequest:
    mode: str  # "student" | "search"
    student_id: int = 0
    query: str = ""


def parse_lookup(payload: object) -> LookupRequest:
    """Exactly one of an exact ``student_id`` or a search ``query``."""
    if not isinstance(payload, dict) or set(payload) - {"student_id", "query", "refresh"}:
        raise RosterRequestError("Send a student ID or a search.", field="body")
    has_id, has_query = "student_id" in payload, "query" in payload
    if has_id == has_query:
        raise RosterRequestError("Send a student ID or a search, not both.", field="body")
    if has_id:
        raw = payload["student_id"]
        if not isinstance(raw, int | str):  # True reads "True": refused as not digits
            raise RosterRequestError("Enter a student ID.", field="student_id")
        text = str(raw).translate(_FOLD).strip()
        if not re.fullmatch(rf"[0-9]{{1,{STUDENT_ID_MAX_DIGITS}}}", text):
            raise RosterRequestError("A student ID is digits only.", field="student_id")
        return LookupRequest(mode="student", student_id=int(text))
    raw = payload["query"]
    if not isinstance(raw, str) or len(raw) > SEARCH_MAX_LENGTH or _CONTROL.search(raw):
        raise RosterRequestError(
            f"Search with plain text of {SEARCH_MAX_LENGTH} characters or fewer.", field="query"
        )
    query = normalise_search_text(raw)
    compact = query.replace(" ", "")
    if _DIGITS.fullmatch(compact):
        if len(compact) < SEARCH_MIN_DIGITS:
            raise RosterRequestError(
                f"Type at least {SEARCH_MIN_DIGITS} digits of a student ID.", field="query"
            )
        return LookupRequest(mode="search", query=compact)
    # A name is letters: digits beside letters are a course or room code (the
    # page matches those itself), never a student to look up and audit.
    if any(char.isdigit() for char in compact):
        raise RosterRequestError(
            "Search a student ID with digits only, or a name with letters only.", field="query"
        )
    # Letters are counted, not characters: "s t e" or "---" is no name.
    if sum(char.isalpha() for char in compact) < SEARCH_MIN_LETTERS or any(
        sum(char.isalpha() for char in word) < SEARCH_MIN_WORD_LETTERS for word in query.split()
    ):
        raise RosterRequestError(
            f"Type at least {SEARCH_MIN_LETTERS} letters of a name, "
            f"{SEARCH_MIN_WORD_LETTERS} or more in each word.",
            field="query",
        )
    return LookupRequest(mode="search", query=query)


def lookup_sittings(view: RosterView, student_id: int) -> list[Sitting]:
    """The student's every exam in this run, in timetable order."""
    return list(view.by_student.get(student_id, []))


def search_students(view: RosterView, query: str) -> tuple[list[int], int]:
    """(first ``SEARCH_LIMIT`` matching IDs, total matches) among this run's students.

    Digits match a student ID by prefix; anything else matches when every word
    of the query appears in the folded name, names starting with the first
    word ranked first.
    """
    if _DIGITS.fullmatch(query):
        hits = sorted(sid for sid in view.by_student if str(sid).startswith(query))
    else:
        words = query.split()
        found = [
            sid
            for sid, name in view.search_names.items()
            if sid in view.by_student and all(word in name for word in words)
        ]
        hits = sorted(
            found,
            key=lambda sid: (
                not view.search_names[sid].startswith(words[0]),
                view.search_names[sid],
                sid,
            ),
        )
    return hits[:SEARCH_LIMIT], len(hits)


def search_match(view: RosterView, student_id: int) -> dict[str, Any]:
    """One Find suggestion (keys: ``SEARCH_MATCH_KEYS``)."""
    facts = view.model.students[student_id]
    return {
        "student_id": student_id,
        "name": facts.name,
        "program": facts.program,
        "department": department_key(facts.program),
        "exams": len(view.by_student.get(student_id, [])),
    }


def audit_subject_ref(student_id: int) -> str:
    """The keyed reference an audit row names a student by, never the ID itself.

    HMAC-SHA-256 under the site's secret (the key of the audit hash chain):
    the row names no one to whoever reads or exports the log, yet an auditor
    can still answer "who looked up student X" by computing X's reference
    (``manage.py exam_roster_audit_ref X``) and searching for it.
    """
    digest = salted_hmac(AUDIT_SUBJECT_SALT, str(int(student_id)), algorithm="sha256")
    return f"{AUDIT_SUBJECT_PREFIX}{digest.hexdigest()[:24]}"


def lookup_audit_details(
    view: RosterView, request: LookupRequest, matched: Sequence[int], total: int, cached: bool
) -> dict[str, Any]:
    """A settled lookup: the kind of ask, its length, who was asked for and shown.

    Never a student ID, a name or the search text (phase-2 rule; phase 1's
    audit rows hold "options and counts, never a student"): the student asked
    for and each student shown are keyed references (``audit_subject_ref``).
    """
    model = view.model
    if request.mode == "student":
        asked, length = "student_id", len(str(request.student_id))
        subject: str | None = audit_subject_ref(request.student_id)
    else:
        compact = request.query.replace(" ", "")
        asked = "id_prefix" if _DIGITS.fullmatch(compact) else "name"
        length = len(compact)
        subject = None
    return {
        "run_id": model.saved.run_id,
        "mode": request.mode,
        "asked": asked,
        "asked_length": length,
        "subject_ref": subject,
        "shown_refs": [audit_subject_ref(sid) for sid in matched],
        "matches": total,
        "lists_code_now": model.lists_code_now,
        "lists_checked_at": view.checked_at.isoformat(),
        "cached": cached,
    }


def lookup_payload(view: RosterView, sittings: Sequence[Sitting]) -> dict[str, Any]:
    model = view.model
    exam_map, room_list = _referenced_facts(view, sittings, ())
    return {
        "mode": "student",
        "found": bool(sittings),
        "counts": _counts(sittings, model),
        "exams": exam_map,
        "rooms": room_list,
        "departments": departments_legend(model.students[s.student_id].program for s in sittings),
        "rows": [roster_row(sitting, model) for sitting in sittings],
    }


def search_payload(view: RosterView, matched: Sequence[int], total: int) -> dict[str, Any]:
    programs = [view.model.students[sid].program for sid in matched]
    return {
        "mode": "search",
        "matches": [search_match(view, sid) for sid in matched],
        "total": total,
        "more": total > len(matched),
        "departments": departments_legend(programs),
    }


#: The whole run's check as every student-data answer carries it: enough to
#: say "Lists match" or how many sections changed, from the answer's own build.
CHECK_KEYS = (
    "status",
    "sections_changed",
    "sections_new",
    "sections_gone",
    "program_mix_changed",
    "lists_code_saved",
    "lists_code_now",
)


def compact_check(view: RosterView) -> dict[str, Any]:
    """The navigator's check of this build, cut to ``CHECK_KEYS`` (no student)."""
    check = view.navigator["check"]
    return {key: check[key] for key in CHECK_KEYS}
