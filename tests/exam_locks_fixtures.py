"""Saved runs for the exam-locks tests, built by hand and readable by the Student lists.

A lock freezes what a SAVED run holds: its placements, its rooms and the
section rows those rooms were sized for. ``saved_run`` makes one without a
database - every field the Student lists' saved-run check reads is present and
consistent - so the resolver's refusals can be tested one at a time.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

DAYS = ["W1-Sun", "W1-Mon", "W1-Tue"]
PERIODS = ["08:00-10:00", "11:00-13:00"]
TERM = ("1448", "1")


def fingerprint(members) -> str:
    return hashlib.sha256(",".join(str(sid) for sid in sorted(members)).encode()).hexdigest()


def section_rows(code: str, sizes: dict[str, int], *, number: int = 1, term=TERM) -> list[dict]:
    """One mapped section group per gender, ``sizes[gender]`` students each."""
    rows = []
    for gender, size in sorted(sizes.items()):
        base = number * 1000 + (0 if gender == "M" else 500)
        rows.append(
            {
                "section": f"{gender}{number}",
                "section_key": f"term-section:{code}-{gender}",
                "term_section_id": number,
                "mapping_status": "mapped",
                "mapping_source": "scraper_timetable",
                "academic_year": term[0],
                "term": term[1],
                "gender": gender,
                "preferred_room": "",
                "student_count": size,
                "membership_fingerprint": fingerprint(range(base, base + size)),
            }
        )
    return rows


def room_rows(code: str, rows: list[dict], *, room_prefix: str = "R") -> list[dict]:
    """Each section group seated whole in a room of its own, as the allocator writes it."""
    rooms = []
    for row in rows:
        part = {
            "section": row["section"],
            "section_key": row["section_key"],
            "term_section_id": row["term_section_id"],
            "mapping_status": row["mapping_status"],
            "gender": row["gender"],
            "student_count": row["student_count"],
            "room_group_index": 1,
            "room_group_count": 1,
        }
        rooms.append(
            {
                "section": row["section"],
                "room_code": f"{room_prefix}{row['gender']}-{code}",
                "student_count": row["student_count"],
                "room_capacity": 40,
                "gender": row["gender"],
                "merged_from": [row["section"]],
                "section_parts": [part],
                "mapping_status": "mapped",
                "room_group": "",
                "building": "B1",
                "floor": "1",
            }
        )
    return rooms


def operations_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            **{
                key: row[key]
                for key in (
                    "section",
                    "section_key",
                    "term_section_id",
                    "mapping_status",
                    "mapping_source",
                    "academic_year",
                    "term",
                    "gender",
                    "student_count",
                )
            },
            "program_counts": {"CS": row["student_count"]},
            "instructors": ["Dr Saved"],
            "instructor_source": "recorded_section_meetings",
            "instructor_status": "recorded",
        }
        for row in rows
    ]


def saved_run(
    placements: dict[str, tuple[str, str] | None],
    *,
    days: list[str] = DAYS,
    periods: list[str] = PERIODS,
    sizes: dict[str, dict[str, int]] | None = None,
    exam_locks: list[dict] | None = None,
    linked_exams: list[dict] | None = None,
    scope: dict | None = None,
    term=TERM,
) -> dict:
    """A normalised ``ok`` run: ``placements`` maps a course to (day, period), or None for OVERFLOW."""
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate((day, period) for day in days for period in periods)
    ]
    slot_of = {(slot["day"], slot["period"]): slot["index"] for slot in slots}
    schedule = []
    enrollment = {}
    operations = {}
    overflow = len(slots)
    for number, (code, where) in enumerate(sorted(placements.items()), start=1):
        rows = section_rows(
            code, (sizes or {}).get(code, {"M": 12, "F": 9}), number=number, term=term
        )
        enrollment[code] = rows
        if where is None:
            day, period, slot = "OVERFLOW", f"Extra-{overflow}", overflow
            overflow += 1
            rooms = []
        else:
            day, period = where
            slot = slot_of[(day, period)]
            rooms = room_rows(code, rows)
        schedule.append(
            {
                "course_code": code,
                "source_course_code": code,
                "course_name": f"{code} name",
                "course_identity": f"{code}::{code} name",
                "slot_index": slot,
                "day": day,
                "period": period,
                "is_online": False,
                "rooms": rooms,
            }
        )
        operations[code] = {
            "course_identity": f"{code}::{code} name",
            "source_course_code": code,
            "course_name": f"{code} name",
            "program_counts": [],
            "sections": operations_rows(rows),
        }
    schedule.sort(key=lambda entry: (entry["slot_index"], entry["course_code"]))
    run = {
        "schema_version": 6,
        "status": "ok",
        "enrollment_source": "scraper_timetable",
        "enrollment_scope": scope or {"programs": ["CS"], "sections": ["F", "M"]},
        "pinned": [],
        "linked_exams": linked_exams or [],
        "slots": slots,
        "schedule": schedule,
        "qa": {"max_per_day": 2, "thin_threshold": 0},
        "section_enrollment": enrollment,
        "operations_snapshot": {
            "version": 1,
            "enrollment_source": "scraper_timetable",
            "courses": operations,
        },
        "assign_rooms": True,
        "seed": None,
        "primary_status": "clean",
        "status_flags": [],
        "status_derivation_version": 3,
        "input_fingerprint": "0" * 64,
    }
    if exam_locks is not None:
        run["exam_locks"] = exam_locks
    return run


def courses_of(run: dict) -> dict[str, dict]:
    """The request's courses as a loaded action sees them: each saved entry by its code."""
    return {entry["course_code"]: deepcopy(entry) for entry in run["schedule"]}


def board_of(run: dict) -> list[dict]:
    return [
        {
            key: entry[key]
            for key in ("course_code", "course_identity", "day", "period", "slot_index")
        }
        for entry in run["schedule"]
    ]


def move(board: list[dict], code: str, day: str, period: str) -> list[dict]:
    moved = deepcopy(board)
    for entry in moved:
        if entry["course_code"] == code:
            entry.update(day=day, period=period)
    return moved
