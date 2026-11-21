"""Per-instructor teaching-time rules: what time an instructor may not teach.

This module is the single source of truth for the rule semantics; the staff
editor's ``static/js/instructor-time-rules.js`` mirrors it exactly, and
``tests/fixtures/instructor_availability_cases.json`` holds both to the same
cases. It is pure Python except :func:`load_time_rules`, which imports the model
lazily, and :func:`default_period_grid`, which imports the default grid lazily.
It is deliberately not named ``timetable_*`` so the ``scheduler`` app may import
it.

Semantics
---------
* Days are SUN..THU only, indexes 0..4 (``DAY_CODES``). A rule stores them as a
  bit mask: bit ``i`` is day ``i`` (SUN=1, MON=2, TUE=4, WED=8, THU=16).
* Times are minutes from midnight and every window is half-open
  ``[start, end)``; a whole day is ``[0, 1440)``.
* Two kinds: ``unavailable`` ("cannot teach") and ``available_only`` ("can
  teach only"), each on its chosen days during its window.
* For an instructor and a day ``d``:

  - ``U_d`` is the union of the instructor's ``unavailable`` windows whose days
    include ``d``.
  - If the instructor has at least one ``available_only`` rule, on ANY day,
    ``A_d`` is the union of the ``available_only`` windows whose days include
    ``d`` (empty when none does), and
    ``forbidden_d = ([0, 1440) minus A_d) union U_d``.
    A day no ``available_only`` rule names is therefore wholly forbidden.
  - Otherwise ``forbidden_d = U_d``.

* A meeting ``(d, s, e)`` violates the rules iff it overlaps ``forbidden_d``:
  some ``(a, b)`` in it has ``s < b`` and ``a < e``. Touching is not
  overlapping, so an ``available_only`` window must CONTAIN the whole meeting.
* A meeting with ``s >= e``, or on a day outside 0..4, never violates.

Known consequence, by design: on the default grid no teaching period lies
wholly inside 11:00-13:00, so "can teach only 11:00-13:00" leaves no open
period at all. :func:`open_periods` lets the editor show that before saving.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import permutations
from typing import Any

DAY_CODES = ("SUN", "MON", "TUE", "WED", "THU")
DAY_NAMES = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY")
MINUTES_PER_DAY = 1440
ALL_DAYS_MASK = (1 << len(DAY_CODES)) - 1  # 31

KIND_UNAVAILABLE = "unavailable"
KIND_AVAILABLE_ONLY = "available_only"
KINDS = (KIND_UNAVAILABLE, KIND_AVAILABLE_ONLY)

MAX_RULES_PER_INSTRUCTOR = 20
MAX_NOTE_LENGTH = 200

# "H:MM" / "HH:MM", optionally ":SS" (seconds are dropped), ASCII digits only,
# like ``timetable_slot_config._TIME``; plus "24:00", the end of a whole day.
_TIME = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])(?::[0-5][0-9])?$")
_END_OF_DAY = re.compile(r"^24:00(?::00)?$")
# The staff API's stricter wire form: "H:MM" / "HH:MM", ASCII digits only (an
# explicit class: Python's \d would also match Arabic-Indic digits), no seconds.
_WIRE_TIME = re.compile(r"([0-9]{1,2}):([0-9]{2})")

Interval = tuple[int, int]


# ── Values ───────────────────────────────────────────────────────


def day_index(value: object) -> int | None:
    """The index 0..4 of a teaching day, or ``None``.

    Accepts a day code in any case with surrounding spaces ("SUN", "sun "),
    the full English name ("Sunday"), or an ``int`` 0..4 (never a ``bool``).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < len(DAY_CODES) else None
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text in DAY_CODES:
        return DAY_CODES.index(text)
    if text in DAY_NAMES:
        return DAY_NAMES.index(text)
    return None


def to_minutes(value: object) -> int | None:
    """Minutes from midnight of a typed time, or ``None``.

    "HH:MM" or "HH:MM:SS" with ASCII digits (a one-digit hour is accepted,
    seconds are dropped, surrounding spaces are ignored), or "24:00" for the
    end of the day. An ``int`` 0..1440 passes through; a ``bool`` does not.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MINUTES_PER_DAY else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _END_OF_DAY.match(text):
        return MINUTES_PER_DAY
    match = _TIME.match(text)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def wire_day_index(value: object) -> int | None:
    """The index 0..4 of a day as the staff API accepts it, or ``None``.

    Strict: only a ``str`` day code (SUN..THU, any case, surrounding spaces
    ignored). Ints, bools and full day names are refused; :func:`day_index`
    stays lenient for engine callers.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return DAY_CODES.index(text) if text in DAY_CODES else None


def wire_minutes(value: object, *, end: bool) -> int | None:
    """Minutes from midnight of a time as the staff API accepts it, or ``None``.

    Strict: only a ``str`` "H:MM" / "HH:MM" of ASCII digits (surrounding spaces
    ignored), hours 0..23 and minutes 0..59; "24:00" is accepted only as an
    ``end``. No seconds, no numbers, no bools.
    """
    if not isinstance(value, str):
        return None
    match = _WIRE_TIME.fullmatch(value.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if end and (hours, minutes) == (24, 0):
        return MINUTES_PER_DAY
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def to_hhmm(minutes: int) -> str:
    """Format a minute count as HH:MM: 0 -> "00:00", 1440 -> "24:00"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def mask_from_days(days: Iterable[object]) -> int:
    """The day mask of an iterable of day codes or indexes.

    Raises ``ValueError`` on anything :func:`day_index` does not read.
    """
    mask = 0
    for day in days:
        index = day_index(day)
        if index is None:
            raise ValueError(f"unknown day: {day!r}")
        mask |= 1 << index
    return mask


def days_from_mask(mask: int) -> tuple[str, ...]:
    """The day codes a mask names, in week order."""
    return tuple(code for index, code in enumerate(DAY_CODES) if mask & (1 << index))


# ── Compiled availability ────────────────────────────────────────


@dataclass(frozen=True)
class TimeRule:
    """One stored rule, as the engines see it (no note, no timestamps)."""

    instructor_id: int
    kind: str
    day_mask: int
    start_minute: int
    end_minute: int


# Per instructor, per day index: the merged forbidden intervals as two parallel
# sorted tuples (starts, ends), so a probe is one bisect.
_DayIndex = tuple[tuple[int, ...], tuple[int, ...]]
_EMPTY_DAY: _DayIndex = ((), ())


class InstructorAvailability:
    """The forbidden teaching time of a set of instructors, ready to query.

    Immutable. Only instructors with at least one forbidden interval are held,
    so an instructor without rules costs one dict lookup per probe.
    """

    __slots__ = ("_days",)

    def __init__(self, days: Mapping[int, tuple[_DayIndex, ...]]) -> None:
        self._days: dict[int, tuple[_DayIndex, ...]] = dict(days)

    def __bool__(self) -> bool:
        return bool(self._days)

    def __repr__(self) -> str:
        return f"InstructorAvailability(instructors={sorted(self._days)})"

    @property
    def instructor_ids(self) -> frozenset[int]:
        """The instructors with at least one forbidden interval."""
        return frozenset(self._days)

    def forbidden(self, instructor_id: int, day: int) -> tuple[Interval, ...]:
        """The merged, sorted forbidden intervals of one instructor on one day."""
        starts, ends = self._day(instructor_id, day)
        return tuple(zip(starts, ends, strict=True))

    def blocks(self, instructor_id: int, day: int, start_minute: int, end_minute: int) -> bool:
        """Whether a meeting ``[start_minute, end_minute)`` on ``day`` breaks a rule."""
        if start_minute >= end_minute:
            return False
        starts, ends = self._day(instructor_id, day)
        # The last interval starting before the meeting ends is the only one
        # that can overlap it: intervals are disjoint and sorted, so every
        # earlier one also ends earlier.
        index = bisect_left(starts, end_minute) - 1
        return index >= 0 and ends[index] > start_minute

    def blocked_instructors(
        self, instructor_ids: Iterable[int], day: int, start_minute: int, end_minute: int
    ) -> tuple[int, ...]:
        """The instructors among ``instructor_ids`` that a meeting would violate, sorted."""
        return tuple(
            sorted(
                {
                    instructor_id
                    for instructor_id in instructor_ids
                    if self.blocks(instructor_id, day, start_minute, end_minute)
                }
            )
        )

    def fingerprint(self) -> str:
        """A stable sha256 hex of the forbidden time; "" when nothing is forbidden."""
        if not self._days:
            return ""
        canonical = [
            [instructor_id, [list(zip(*day, strict=True)) for day in self._days[instructor_id]]]
            for instructor_id in sorted(self._days)
        ]
        payload = json.dumps(canonical, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    def _day(self, instructor_id: int, day: int) -> _DayIndex:
        days = self._days.get(instructor_id)
        if days is None or not 0 <= day < len(DAY_CODES):
            return _EMPTY_DAY
        return days[day]


EMPTY_AVAILABILITY = InstructorAvailability({})


def _merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of half-open intervals; overlapping AND touching ones merge."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _complement(merged: Sequence[Interval]) -> list[Interval]:
    """``[0, 1440)`` minus already-merged intervals."""
    gaps: list[Interval] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < MINUTES_PER_DAY:
        gaps.append((cursor, MINUTES_PER_DAY))
    return gaps


def _check(rule: TimeRule) -> None:
    if rule.kind not in KINDS:
        raise ValueError(f"unknown rule kind: {rule.kind!r}")
    if not 1 <= rule.day_mask <= ALL_DAYS_MASK:
        raise ValueError(f"day mask out of range: {rule.day_mask!r}")
    if not 0 <= rule.start_minute < rule.end_minute <= MINUTES_PER_DAY:
        raise ValueError(f"invalid window: {rule.start_minute!r}-{rule.end_minute!r}")


def _forbidden_week(rules: Sequence[TimeRule]) -> tuple[list[Interval], ...]:
    """The merged forbidden intervals of ONE instructor's rules, per day."""
    has_available_only = any(rule.kind == KIND_AVAILABLE_ONLY for rule in rules)
    week: list[list[Interval]] = []
    for day in range(len(DAY_CODES)):
        bit = 1 << day
        windows = {
            kind: [
                (r.start_minute, r.end_minute) for r in rules if r.kind == kind and r.day_mask & bit
            ]
            for kind in KINDS
        }
        forbidden = list(windows[KIND_UNAVAILABLE])
        if has_available_only:
            forbidden += _complement(_merge(windows[KIND_AVAILABLE_ONLY]))
        week.append(_merge(forbidden))
    return tuple(week)


def compile_rules(rules: Iterable[TimeRule]) -> InstructorAvailability:
    """Compile stored rules into an :class:`InstructorAvailability`.

    Raises ``ValueError`` for a rule the database constraints would refuse.
    """
    by_instructor: dict[int, list[TimeRule]] = defaultdict(list)
    for rule in rules:
        _check(rule)
        by_instructor[rule.instructor_id].append(rule)
    days: dict[int, tuple[_DayIndex, ...]] = {}
    for instructor_id, own in by_instructor.items():
        week = _forbidden_week(own)
        if any(week):
            days[instructor_id] = tuple(
                (tuple(a for a, _ in day), tuple(b for _, b in day)) for day in week
            )
    return InstructorAvailability(days) if days else EMPTY_AVAILABILITY


# ── Database ─────────────────────────────────────────────────────


def load_time_rules(instructor_ids: Iterable[int] | None = None) -> list[TimeRule]:
    """The stored rules of ``instructor_ids`` (every rule when ``None``), in one query."""
    from core.models import InstructorTimeRule

    rows = InstructorTimeRule.objects.all()
    if instructor_ids is not None:
        ids = sorted(set(instructor_ids))
        if not ids:
            return []
        rows = rows.filter(instructor_id__in=ids)
    return [
        TimeRule(*row)
        for row in rows.order_by("id").values_list(
            "instructor_id", "kind", "day_mask", "start_minute", "end_minute"
        )
    ]


def load_instructor_availability(
    instructor_ids: Iterable[int] | None = None, *, scenario: Any = None
) -> InstructorAvailability:
    """The compiled availability of ``instructor_ids`` (all when ``None``), in one query.

    Rules are global in this version; ``scenario`` is accepted so term or
    scenario scoping can be added here later without touching any caller.
    """
    del scenario  # v1: every rule applies to every scenario
    return compile_rules(load_time_rules(instructor_ids))


# ── Editor helpers ───────────────────────────────────────────────


def rules_revision(rules: Iterable[Mapping[str, Any]]) -> str:
    """A short, order-independent token over a stored rule set (notes included).

    Each mapping carries ``kind``, ``day_mask``, ``start_minute``,
    ``end_minute`` and ``note``. Two writers holding the same token saw the
    same rules. The empty set has a fixed, NON-empty token too, so the first
    save of an instructor without rules carries a real ``expected_revision``
    (the API refuses an empty one).
    """
    canonical = sorted(
        [
            str(rule["kind"]),
            int(rule["day_mask"]),
            int(rule["start_minute"]),
            int(rule["end_minute"]),
            str(rule.get("note") or ""),
        ]
        for rule in rules
    )
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def open_periods(
    availability: InstructorAvailability, instructor_id: int, slots: Sequence[Mapping[str, Any]]
) -> list[list[bool]]:
    """``[day][slot]``: True when that teaching period is NOT blocked for the instructor.

    Each slot carries ``start`` and ``end`` as "HH:MM" (or minutes). Raises
    ``ValueError`` for a slot whose times do not read.
    """
    windows: list[Interval] = []
    for slot in slots:
        start, end = to_minutes(slot.get("start")), to_minutes(slot.get("end"))
        if start is None or end is None:
            raise ValueError(f"slot times do not read: {slot!r}")
        windows.append((start, end))
    return [
        [not availability.blocks(instructor_id, day, start, end) for start, end in windows]
        for day in range(len(DAY_CODES))
    ]


# The weekly meeting patterns a course can need, in minutes per meeting. A
# 75-minute meeting takes a lecture period, a longer one a 100-minute (lab)
# period, and the meetings of one course fall on distinct days -- exactly what
# ``timetable_autoplace._generate_meeting_options`` generates options for.
LECTURE_MEETING_MINUTES = 75
SHAPES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("1x75", (75,)),
    ("1x100", (100,)),
    ("2x75", (75, 75)),
    ("2x100", (100, 100)),
    ("2x75+100", (75, 75, 100)),
)

STATE_ANY = "any"  # every default period is open
STATE_LIMITED = "limited"  # some period is blocked, every shape still fits
STATE_NARROW = "narrow"  # something is open, but some shape no longer fits
STATE_NONE = "none"  # no default period is open


def shape_feasible(
    meetings: Sequence[int], lecture_days: Sequence[bool], lab_days: Sequence[bool]
) -> bool:
    """Whether a weekly pattern fits: each meeting on its own day, and that day
    has an open period of the meeting's grid (lecture for 75 minutes, lab for
    longer). A brute force over the five days; at most 60 orderings."""
    for days in permutations(range(len(DAY_CODES)), len(meetings)):
        if all(
            (lecture_days if minutes <= LECTURE_MEETING_MINUTES else lab_days)[day]
            for minutes, day in zip(meetings, days, strict=True)
        ):
            return True
    return False


def feasibility_summary(lecture: Sequence[Sequence[bool]], lab: Sequence[Sequence[bool]]) -> dict:
    """The editor's and roster's summary of two ``[day][period]`` open tables.

    ``{"lecture": {"open", "total"}, "lab": {...}, "days": {"lecture": [5 bools],
    "lab": [...]}, "shapes": [{"key", "feasible"}], "no_teaching_days": [codes],
    "state": "any" | "limited" | "narrow" | "none"}``.
    """
    counts = {
        name: {
            "open": sum(cell for row in table for cell in row),
            "total": sum(len(row) for row in table),
        }
        for name, table in (("lecture", lecture), ("lab", lab))
    }
    lecture_days = [any(row) for row in lecture]
    lab_days = [any(row) for row in lab]
    shapes = [
        {"key": key, "feasible": shape_feasible(meetings, lecture_days, lab_days)}
        for key, meetings in SHAPES
    ]
    if all(c["open"] == c["total"] for c in counts.values()):
        state = STATE_ANY
    elif not any(c["open"] for c in counts.values()):
        state = STATE_NONE
    elif not all(shape["feasible"] for shape in shapes):
        state = STATE_NARROW
    else:
        state = STATE_LIMITED
    return {
        **counts,
        "days": {"lecture": lecture_days, "lab": lab_days},
        "shapes": shapes,
        "no_teaching_days": [
            code
            for code, has_lecture, has_lab in zip(DAY_CODES, lecture_days, lab_days, strict=True)
            if not has_lecture and not has_lab
        ],
        "state": state,
    }


def open_summary(
    availability: InstructorAvailability, instructor_id: int, grid: Mapping[str, Any]
) -> dict[str, Any]:
    """:func:`feasibility_summary` of one instructor over a period grid."""
    return feasibility_summary(
        open_periods(availability, instructor_id, grid["lecture"]),
        open_periods(availability, instructor_id, grid["lab"]),
    )


def default_period_grid() -> dict[str, Any]:
    """The default teaching periods the editor previews rules against.

    ``{"days": [...], "lecture": [{label, start, end}], "lab": [...]}`` from the
    placeable default lecture and lab columns (online-only and policy-only
    columns are not teaching periods any automatic placer uses).
    """
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        placeable_slots,
    )

    def periods(slots: list[dict]) -> list[dict[str, str]]:
        return [
            {"label": str(slot["label"]), "start": str(slot["start"]), "end": str(slot["end"])}
            for slot in placeable_slots(slots)
        ]

    return {
        "days": list(DAY_CODES),
        "lecture": periods(DEFAULT_SLOTS),
        "lab": periods(DEFAULT_LAB_SLOTS),
    }


__all__ = [
    "ALL_DAYS_MASK",
    "DAY_CODES",
    "EMPTY_AVAILABILITY",
    "KINDS",
    "KIND_AVAILABLE_ONLY",
    "KIND_UNAVAILABLE",
    "LECTURE_MEETING_MINUTES",
    "MAX_NOTE_LENGTH",
    "MAX_RULES_PER_INSTRUCTOR",
    "MINUTES_PER_DAY",
    "SHAPES",
    "STATE_ANY",
    "STATE_LIMITED",
    "STATE_NARROW",
    "STATE_NONE",
    "InstructorAvailability",
    "TimeRule",
    "compile_rules",
    "day_index",
    "days_from_mask",
    "default_period_grid",
    "feasibility_summary",
    "load_instructor_availability",
    "load_time_rules",
    "mask_from_days",
    "open_periods",
    "open_summary",
    "rules_revision",
    "shape_feasible",
    "to_hhmm",
    "to_minutes",
    "wire_day_index",
    "wire_minutes",
]
