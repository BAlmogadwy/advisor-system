"""Add courses to a saved exam timetable, moving as few of its exams as possible.

The committee builds a timetable from part of its courses, perfects it - with
drags, pins, links and locked days - and then wants the remaining courses in
THAT timetable. A Build would place everything again; this places only the new
exams:

1. Build's own greedy (``schedule``) seats every new exam that fits around the
   saved board, with every existing exam fixed where it is. Nothing moves, the
   choice is scored exactly as a Build scores it - with seats first, so a big
   course does not land where its cohort's rooms are already full - and it is
   plain Python, the same on every platform. On a board with room this is the
   whole answer.
2. What the greedy could not seat goes to ``exam_min_change.place_added_exams``,
   which may move unprotected existing exams - the fewest it can - to make room.

Pinned exams, locked cells, exams an earlier Fix protected, and the exams of
any rule break the saved board already has never move: Add never "fixes"
something it was not asked to fix. Existing exams never go to OVERFLOW; a new
exam stays there only when no legal board seats it, with the reason.

This module reads the database and never writes it. The view saves the result
as a new run; the source run is never changed.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from functools import cache
from typing import Any

from core.services.course_identity import planner_course_key
from core.services.exam_min_change import _bucket_mates, _day_of
from core.services.exam_timetable import (
    _source_code_for_display,
    build_enrolled_sets_with_meta,
    schedule,
)

#: The refusal codes; each ``AddCoursesError`` carries one.
INVALID = "add_courses_invalid"
NONE = "add_courses_none"
SOURCE_REQUIRED = "add_courses_source_required"
UNSAVED_CHANGES = "add_courses_unsaved_changes"
TERM_CHANGED = "add_courses_term_changed"
ALREADY_IN_TIMETABLE = "add_courses_already_in_timetable"
OUTSIDE_SCOPE = "add_courses_outside_scope"
UNAVAILABLE = "add_courses_unavailable"

REFUSAL_CODES = (
    INVALID,
    NONE,
    SOURCE_REQUIRED,
    UNSAVED_CHANGES,
    TERM_CHANGED,
    ALREADY_IN_TIMETABLE,
    OUTSIDE_SCOPE,
    UNAVAILABLE,
)

MODE = "add_courses"
REBUILD_MODE = "added_courses_from_loaded"
#: The most courses one request may add, and the longest identity it may name.
MAX_ADDED = 500
MAX_IDENTITY_LENGTH = 300
OVERFLOW = "OVERFLOW"
UNASSIGNED = "UNASSIGNED"
#: How many courses a "not placed" reason names for each kind of blocker.
_NAMED = 5


class AddCoursesError(ValueError):
    """A request to add courses that cannot be honoured, the field that says so, and which courses."""

    def __init__(
        self, message: str, *, code: str, field: str, courses: Iterable[str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.courses = list(courses) if courses is not None else None


# ── the request ──────────────────────────────────────────────────────────────


def parse_added_courses(raw: Any) -> list[str]:
    """The identities of the courses to add, in request order.

    Only ``course_identity`` counts: a code is renumbered whenever the scope
    changes, an identity is not. Anything else a client sends is ignored.
    """
    if not isinstance(raw, list) or len(raw) > MAX_ADDED:
        raise AddCoursesError(
            f"The courses to add must be a list of at most {MAX_ADDED} courses.",
            code=INVALID,
            field="added_courses",
        )
    identities: list[str] = []
    for index, item in enumerate(raw):
        identity = item.get("course_identity") if isinstance(item, dict) else None
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or len(identity.strip()) > MAX_IDENTITY_LENGTH
        ):
            raise AddCoursesError(
                "Each course to add must be named by its course identity.",
                code=INVALID,
                field=f"added_courses[{index}]",
            )
        identity = identity.strip()
        if identity in identities:
            raise AddCoursesError(
                "A course was asked to be added twice.",
                code=INVALID,
                field=f"added_courses[{index}]",
            )
        identities.append(identity)
    if not identities:
        raise AddCoursesError(
            "Choose at least one course to add.", code=NONE, field="added_courses"
        )
    return identities


def _identity(entry: Mapping[str, Any]) -> str:
    explicit = str(entry.get("course_identity") or "").strip()
    if explicit:
        return explicit
    code = str(entry.get("course_code", "")).strip()
    return planner_course_key(
        _source_code_for_display(code, entry.get("source_course_code")), entry.get("course_name")
    )


def _where(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """An entry's place for comparing boards: OVERFLOW is one place, whatever its ``Extra-n``."""
    if entry.get("day") == OVERFLOW:
        return (OVERFLOW,)
    return (str(entry.get("day", "")), str(entry.get("period", "")))


def _link_sets(links: Any, identity_of_code: Mapping[str, str]) -> set[frozenset[str]]:
    groups: set[frozenset[str]] = set()
    for link in links if isinstance(links, list) else []:
        members = link.get("members") if isinstance(link, dict) else None
        names = frozenset(
            str(member.get("course_identity") or "").strip()
            or identity_of_code.get(str(member.get("course_code") or ""), "")
            for member in members or []
            if isinstance(member, dict)
        )
        groups.add(names)
    return groups


def _lock_set(locks: Any) -> set[tuple[str, str | None]]:
    out: set[tuple[str, str | None]] = set()
    for item in locks if isinstance(locks, list) else []:
        if isinstance(item, dict):
            period = item.get("period")
            out.add((str(item.get("day", "")), str(period) if period is not None else None))
    return out


def require_clean_board(
    *,
    source: Mapping[str, Any],
    entries: list[dict],
    pinned: Any,
    linked_exams: Any,
    exam_locks: Any,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    thin_conflict_threshold: int,
    assign_rooms: bool,
) -> None:
    """Adding works on the SAVED timetable: refuse a board with unsaved changes.

    Compared by identity: placements (every OVERFLOW entry as one place), the
    set of courses, pins, links, locks, the header, the daily limit, the tiny
    course threshold and room assignment. The name is not compared: it names
    the new run.
    """

    def refuse(field: str) -> AddCoursesError:
        return AddCoursesError(
            "Save your changes, or open the saved timetable again from Saved timetables "
            "to discard them. Adding courses works on the saved timetable.",
            code=UNSAVED_CHANGES,
            field=field,
        )

    slots = [slot for slot in source.get("slots") or [] if isinstance(slot, dict)]
    qa = source.get("qa") if isinstance(source.get("qa"), dict) else {}
    if (
        list(dict.fromkeys(str(slot.get("day", "")) for slot in slots)) != list(days)
        or list(dict.fromkeys(str(slot.get("period", "")) for slot in slots)) != list(periods)
        or qa.get("max_per_day", 2) != max_per_day
        or qa.get("thin_threshold", 0) != thin_conflict_threshold
        or source.get("assign_rooms", True) is not assign_rooms
    ):
        raise refuse("header")
    saved = {
        _identity(entry): _where(entry)
        for entry in source.get("schedule") or []
        if isinstance(entry, dict)
    }
    board = {_identity(entry): _where(entry) for entry in entries}
    if board != saved:
        raise refuse("base_schedule")
    identity_of_code = {str(entry.get("course_code", "")): _identity(entry) for entry in entries}
    saved_pins = {
        (str(pin.get("course_code", "")), str(pin.get("day", "")), str(pin.get("period", "")))
        for pin in source.get("pinned") or []
        if isinstance(pin, dict)
    }
    sent_pins = {
        (
            str(pin.get("course_code", "")).strip(),
            str(pin.get("day", "")),
            str(pin.get("period", "")),
        )
        for pin in pinned or []
        if isinstance(pin, dict)
    }
    if sent_pins != saved_pins or len(pinned or []) != len(sent_pins):
        raise refuse("pinned")
    if _link_sets(linked_exams, identity_of_code) != _link_sets(
        source.get("linked_exams") or [], identity_of_code
    ):
        raise refuse("linked_exams")
    if _lock_set(exam_locks) != _lock_set(source.get("exam_locks") or []):
        raise refuse("exam_locks")


def source_term(source: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The registrar terms a saved run's section rows were taken from.

    Read where locks read it: ``schedule[].term`` held a study-plan term on
    every run saved before 2026-09-26, and would make every older run look as
    if it belonged to another term.
    """
    terms: set[tuple[str, str]] = set()
    for rows in (source.get("section_enrollment") or {}).values():
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and (row.get("academic_year") or row.get("term")):
                terms.add((str(row.get("academic_year", "")), str(row.get("term", ""))))
    return terms


def term_changed(source: Mapping[str, Any], current: tuple[str, str] | None) -> bool:
    """The run was built for another academic term than today's registrations."""
    terms = source_term(source)
    if not terms or current is None:
        return False
    return terms != {(str(current[0]), str(current[1]))}


def require_same_term(source: Mapping[str, Any], current: tuple[str, str] | None) -> None:
    if term_changed(source, current):
        raise AddCoursesError(
            "This timetable was built for another academic term. "
            "Build a new timetable for this term.",
            code=TERM_CHANGED,
            field="previous_run_id",
        )


# ── the courses of a scope ───────────────────────────────────────────────────


def assign_add_codes(
    existing_codes: Iterable[str], live: Mapping[str, Mapping[str, Any]], in_run: set[str]
) -> dict[str, str]:
    """The code each live course not in the run takes when it is added, by identity.

    Its live display code, unless an existing exam already uses that code;
    then the smallest free ``"<source> (n)"`` that is neither an existing
    code nor any live code. Existing codes never change - pins, links, locks
    and shared-room rows name them.

    Computed over every live course of the scope, never over one request's
    choice, so the list and the action give a course the same code whichever
    other courses are chosen with it.
    """
    existing = set(existing_codes)
    reserved = existing | {str(meta["course_code"]) for meta in live.values()}
    codes: dict[str, str] = {}
    waiting: list[str] = []
    for identity in sorted(set(live) - in_run):
        code = str(live[identity]["course_code"])
        if code in existing:
            waiting.append(identity)
        else:
            codes[identity] = code
    for identity in waiting:
        source = str(live[identity].get("source_course_code") or "") or _source_code_for_display(
            str(live[identity]["course_code"])
        )
        number = 1
        while f"{source} ({number})" in reserved:
            number += 1
        codes[identity] = f"{source} ({number})"
        reserved.add(codes[identity])
    return codes


def live_courses(
    programs: list[str] | None, sections: list[str] | None
) -> dict[str, dict[str, Any]]:
    """Each live course of the scope, by identity, with its display code."""
    _enrolled, course_meta = build_enrolled_sets_with_meta(programs=programs, sections=sections)
    return {
        str(meta["course_identity"]): {"course_code": code, **meta}
        for code, meta in course_meta.items()
    }


def resolve_added_courses(
    identities: list[str],
    *,
    source: Mapping[str, Any],
    programs: list[str] | None,
    sections: list[str] | None,
) -> list[dict]:
    """The new exams' board entries, or the refusal naming the first course that cannot be added.

    Each is refused in request order: one already in the timetable, one of
    another scope (it has students, but none in this timetable's programmes
    and sections), one with no registrations at all.
    """
    in_run = {
        _identity(entry): str(entry.get("course_code", ""))
        for entry in source.get("schedule") or []
        if isinstance(entry, dict)
    }
    live = live_courses(programs, sections)
    unscoped: dict[str, dict[str, Any]] | None = None
    for index, identity in enumerate(identities):
        field = f"added_courses[{index}]"
        if identity in in_run:
            raise AddCoursesError(
                f"{in_run[identity]} is already in this timetable.",
                code=ALREADY_IN_TIMETABLE,
                field=field,
                courses=[in_run[identity]],
            )
        if identity in live:
            continue
        if unscoped is None:
            unscoped = live_courses(None, None)
        if identity in unscoped:
            raise AddCoursesError(
                f"{unscoped[identity]['course_code']} has no students in this timetable's "
                "programmes and sections.",
                code=OUTSIDE_SCOPE,
                field=field,
                courses=[str(unscoped[identity]["course_code"])],
            )
        raise AddCoursesError(
            "A course to add has no registrations in the imported student timetables any more.",
            code=UNAVAILABLE,
            field=field,
            courses=[],
        )
    codes = assign_add_codes(in_run.values(), live, set(in_run))
    return [
        {
            "course_code": codes[identity],
            "source_course_code": str(live[identity].get("source_course_code") or ""),
            "course_name": str(live[identity].get("course_name") or ""),
            "course_identity": identity,
        }
        for identity in sorted(identities, key=lambda identity: codes[identity])
    ]


def scope_courses(
    source: Mapping[str, Any],
    *,
    rows: list[dict],
    current_term: tuple[str, str] | None,
) -> dict[str, Any]:
    """What the Add courses list shows for a saved run: every live course of its scope.

    Courses already in the run are marked ``in_timetable``, by identity, with
    where they sit; every other course carries the code it would get when
    added (``add_code``) and any code of the run it shares a registrar code
    with. ``missing`` names run courses with no live registrations: any Add
    would be refused until they are removed.
    """
    scope = source.get("enrollment_scope") or {}
    programs, sections = list(scope.get("programs") or []), list(scope.get("sections") or [])
    entries = {
        _identity(entry): entry for entry in source.get("schedule") or [] if isinstance(entry, dict)
    }
    live = {str(row["course_identity"]): row for row in rows}
    codes = assign_add_codes(
        (str(entry.get("course_code", "")) for entry in entries.values()), live, set(entries)
    )
    sources_in_run: dict[str, list[str]] = defaultdict(list)
    for entry in entries.values():
        code = str(entry.get("course_code", ""))
        sources_in_run[_source_code_for_display(code, entry.get("source_course_code"))].append(code)
    courses = []
    for row in rows:
        identity = str(row["course_identity"])
        out = dict(row)
        entry = entries.get(identity)
        if entry is not None:
            out["in_timetable"] = True
            out["timetable_course_code"] = str(entry.get("course_code", ""))
            out["placement"] = (
                {"overflow": True}
                if entry.get("day") == OVERFLOW
                else {"day": str(entry.get("day", "")), "period": str(entry.get("period", ""))}
            )
        else:
            out["in_timetable"] = False
            out["add_code"] = codes[identity]
            source_code = _source_code_for_display(
                str(row["course_code"]), row.get("source_course_code")
            )
            shared = sorted(sources_in_run.get(source_code, []))
            if shared:
                out["same_code_as"] = shared
        courses.append(out)
    missing = [
        {"course_code": str(entry.get("course_code", "")), "course_identity": identity}
        for identity, entry in sorted(
            entries.items(), key=lambda item: str(item[1].get("course_code", ""))
        )
        if identity not in live
    ]
    return {
        "scope": {"programs": programs, "sections": sections},
        "term_changed": term_changed(source, current_term),
        "courses": courses,
        "missing": missing,
    }


# ── phase 1: Build's greedy around the saved board ───────────────────────────


class SeatLedger:
    """Seats each (slot, cohort) holds, so the greedy prefers a slot with room.

    Aggregate seats only - necessary, not sufficient: the rooming decides. A
    slot the saved board already filled past its cohort's capacity keeps that
    load as its limit, and is charged for anything added to it.
    """

    def __init__(
        self,
        demand: Mapping[str, Mapping[str, int]],
        capacity: Mapping[str, int],
        fixed: Mapping[str, int],
    ) -> None:
        self.demand = demand
        self.capacity = {gender: int(seats) for gender, seats in capacity.items()}
        self.used: dict[tuple[int, str], int] = defaultdict(int)
        for course, slot in fixed.items():
            for gender in self.capacity:
                self.used[slot, gender] += int((demand.get(course) or {}).get(gender, 0) or 0)
        self.limit = {
            key: max(self.capacity[key[1]], used) for key, used in list(self.used.items())
        }

    def _limit(self, slot: int, gender: str) -> int:
        return self.limit.get((slot, gender), self.capacity[gender])

    def cost(self, course: str, slot: int) -> int:
        total = 0
        for gender in sorted(self.capacity):
            seats = int((self.demand.get(course) or {}).get(gender, 0) or 0)
            if not seats:
                continue
            used, limit = self.used[slot, gender], self._limit(slot, gender)
            total += max(0, used + seats - limit) - max(0, used - limit)
        return total

    def take(self, course: str, slot: int) -> None:
        for gender in self.capacity:
            self.used[slot, gender] += int((self.demand.get(course) or {}).get(gender, 0) or 0)


def seat_demand(section_enrollment: Mapping[str, list[dict]]) -> dict[str, dict[str, int]]:
    """Students per cohort for each course, from the section rows the rooming seats."""
    demand: dict[str, dict[str, int]] = {}
    for code, rows in section_enrollment.items():
        per: dict[str, int] = defaultdict(int)
        for row in rows or []:
            per[str(row.get("gender", "U") or "U").upper()] += int(row.get("student_count", 0) or 0)
        demand[code] = dict(per)
    return demand


def seat_capacity(rooms: list[dict]) -> dict[str, int]:
    """Exam seats per cohort, over the normalised room inventory."""
    capacity: dict[str, int] = defaultdict(int)
    for room in rooms:
        capacity[str(room["section"])] += int(room["capacity"])
    return dict(capacity)


def place_with_greedy(
    *,
    existing: Mapping[str, int],
    new_codes: list[str],
    adj: dict[str, dict[str, int]],
    slots: list[dict],
    enrolled_sets: dict[str, set[int]],
    max_per_day: int,
    plan_term_buckets: dict[tuple[str, int], set[str]],
    course_buckets: dict[str, list[tuple[str, int]]],
    credit_map: dict[str, int],
    locked: list[dict],
    closed_slots: frozenset[int],
    seats: SeatLedger | None = None,
    on_placed=None,
) -> dict[str, int]:
    """Build's greedy, placing only ``new_codes``: every existing exam is fixed.

    Existing exams are fixed one by one, never as links: a link's members all
    sit at one cell, so fixing each there is fixing the link, and a link that
    sits wholly in OVERFLOW is not on the board at all. Returns each new exam
    the greedy seated, with its slot; one it could not seat is absent.
    """
    locked_codes = {pin["course_code"] for pin in locked}
    by_index = {int(slot["index"]): slot for slot in slots}
    pinned = [
        {"course_code": code, "day": by_index[slot]["day"], "period": by_index[slot]["period"]}
        for code, slot in sorted(existing.items())
        if code not in locked_codes
    ]
    placed = schedule(
        sorted(existing) + list(new_codes),
        adj,
        slots,
        enrolled_sets=enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=plan_term_buckets,
        course_buckets=course_buckets,
        pinned=pinned,
        credit_map=credit_map,
        seed=None,
        on_placed=on_placed,
        locked=locked or None,
        closed_slots=closed_slots,
        seats=seats,
    )
    new = set(new_codes)
    return {
        entry["course_code"]: int(entry["slot_index"])
        for entry in placed
        if entry["course_code"] in new and entry["day"] != OVERFLOW
    }


def day_cost_function(
    board: Mapping[str, int],
    students: Mapping[str, set[int]],
    max_per_day: int,
    periods_per_day: int,
):
    """``cost(exam, day)``: its students who already sit the daily limit that day without it."""
    count: dict[int, Counter] = defaultdict(Counter)
    for course, slot in board.items():
        day = _day_of(slot, periods_per_day)
        for student in students.get(course, ()):
            count[student][day] += 1

    @cache
    def cost(course: str, day: int) -> int:
        own = board.get(course)
        own_day = _day_of(own, periods_per_day) if own is not None else None
        return sum(
            1
            for student in students.get(course, ())
            if count[student][day] - (1 if own_day == day else 0) >= max_per_day
        )

    return cost


# ── the report ───────────────────────────────────────────────────────────────


def explain_unplaced(
    unit: str,
    *,
    board: Mapping[str, int],
    adj: dict[str, dict[str, int]],
    plan_term_buckets: dict[tuple[str, int], set[str]] | None,
    protected: set[str],
    closed_slots: frozenset[int],
    slot_count: int,
    periods_per_day: int,
    search_stopped: bool,
    members_of,
) -> dict[str, Any]:
    """Why a new exam is in OVERFLOW, judged against the final board."""
    mates = _bucket_mates(plan_term_buckets)
    clash: set[str] = set()
    fixed: set[str] = set()
    open_slots = [slot for slot in range(slot_count) if slot not in closed_slots]
    closed_free = False
    open_free = False
    every_bucket = bool(open_slots)
    every_fixed = bool(open_slots)
    for slot in range(slot_count):
        day = _day_of(slot, periods_per_day)
        here = {mate for mate in adj.get(unit, {}) if board.get(mate) == slot}
        same_day = {
            mate
            for mate in mates.get(unit, ())
            if mate in board and _day_of(board[mate], periods_per_day) == day
        }
        if slot in closed_slots:
            closed_free = closed_free or not (here or same_day)
            continue
        if not here and not same_day:
            open_free = True
        clash |= here
        every_bucket = every_bucket and bool(same_day)
        blockers = here | same_day
        fixed |= blockers & protected
        every_fixed = every_fixed and bool(blockers & protected)
    if open_free:
        reason = "search_limit"
    elif closed_free or not open_slots:
        reason = "only_locked_periods_free"
    elif every_bucket:
        reason = "bucket_days_full"
    elif every_fixed:
        reason = "blocked_by_fixed_exams"
    elif search_stopped:
        reason = "search_limit"
    else:
        reason = "blocked_by_clashes"
    buckets = [
        {
            "program": program,
            "programme_term": term,
            "courses": sorted(
                code
                for mate in members
                if mate != unit and mate in board
                for code in members_of(mate)
            )[:_NAMED],
        }
        for (program, term), members in sorted((plan_term_buckets or {}).items())
        if unit in members and any(mate != unit and mate in board for mate in members)
    ][:_NAMED]
    return {
        "reason": reason,
        "blocked_by": {
            "locked_slots": len(closed_slots),
            "clash": sorted(code for mate in clash for code in members_of(mate))[:_NAMED],
            "fixed": sorted(code for mate in fixed for code in members_of(mate))[:_NAMED],
            "bucket": buckets,
        },
    }


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(row.get("section_key", "")),
        str(row.get("gender", "")),
        str(row.get("membership_fingerprint", "")),
        int(row.get("student_count", 0) or 0),
    )


def sections_changed(source: Mapping[str, Any], result: Mapping[str, Any]) -> int:
    """Section groups of existing exams whose live rows differ from the saved run's."""
    saved = source.get("section_enrollment") or {}
    live = result.get("section_enrollment") or {}
    existing = {
        str(entry.get("course_code", "")) for entry in source.get("schedule") or [] if entry
    }
    changed = 0
    for code in sorted(existing):
        before = {_row_key(row)[:2]: _row_key(row) for row in saved.get(code) or []}
        after = {_row_key(row)[:2]: _row_key(row) for row in live.get(code) or []}
        changed += sum(1 for key in set(before) | set(after) if before.get(key) != after.get(key))
    return changed


def _room_packs(run: Mapping[str, Any]) -> dict[tuple[str, str, str], str]:
    """Each (day, period, cohort)'s room rows, course by course, as saved text."""
    packs: dict[tuple[str, str, str], dict[str, list]] = defaultdict(dict)
    for entry in run.get("schedule") or []:
        if not isinstance(entry, dict) or entry.get("day") == OVERFLOW:
            continue
        for room in entry.get("rooms") or []:
            if not isinstance(room, dict):
                continue
            key = (
                str(entry.get("day", "")),
                str(entry.get("period", "")),
                str(room.get("gender", "")),
            )
            packs[key].setdefault(str(entry.get("course_code", "")), []).append(room)
    return {
        key: json.dumps(value, ensure_ascii=False, sort_keys=True) for key, value in packs.items()
    }


def rooms_changed(
    source: Mapping[str, Any], result: Mapping[str, Any], slots: list[dict]
) -> list[dict[str, str]]:
    """Every board and cohort whose rooms differ from the saved run's, in timetable order."""
    before, after = _room_packs(source), _room_packs(result)
    order = {(slot["day"], slot["period"]): int(slot["index"]) for slot in slots}
    changed = [key for key in set(before) | set(after) if before.get(key) != after.get(key)]
    return [
        {"day": day, "period": period, "gender": gender}
        for day, period, gender in sorted(
            changed, key=lambda key: (order.get((key[0], key[1]), len(order)), key[2])
        )
    ]


def unassigned_added(result: Mapping[str, Any], added_codes: set[str]) -> int:
    """Section groups of the new exams the rooming could not seat."""
    return sum(
        1
        for entry in result.get("schedule") or []
        if entry.get("course_code") in added_codes
        for room in entry.get("rooms") or []
        if isinstance(room, dict) and room.get("room_code") == UNASSIGNED
    )
