"""Course-specific weekly schedules, independent of Django and either planner.

Academic credits do not determine the contact pattern for these two courses.
Keep complete weekly alternatives here so generators, editors and independent
certifiers agree on the same rule. All other courses use their ordinary rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


def _code(value: object) -> str:
    return str(value or "").replace("\u00a0", " ").strip().upper().replace(" ", "")


def _field(value: object, *names: str) -> object:
    for name in names:
        result = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if result is not None:
            return result
    return None


def _minutes(value: object) -> int:
    if isinstance(value, int):
        return value
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return int(value.hour) * 60 + int(value.minute)
    parts = str(value).split(":")
    if len(parts) != 2:
        raise ValueError("Expected a minute count or HH:MM time")
    hour, minute = map(int, parts)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("Time is outside the teaching day")
    return hour * 60 + minute


def meeting_signature(meeting: object) -> tuple[str, int, int]:
    """Read workspace rows, search states or scheduler day/window records."""
    day = _field(meeting, "day")
    day = getattr(day, "value", day)
    if isinstance(day, int):
        day = WEEKDAYS[day] if 0 <= day < len(WEEKDAYS) else ""
    window = _field(meeting, "window")
    source = window if window is not None else meeting
    start = _minutes(_field(source, "start_min", "start_time", "start"))
    end = _minutes(_field(source, "end_min", "end_time", "end"))
    return _code(day), start, end


@dataclass(frozen=True)
class WeeklyMeeting:
    day: str
    start_min: int
    end_min: int

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "start": f"{self.start_min // 60:02d}:{self.start_min % 60:02d}",
            "end": f"{self.end_min // 60:02d}:{self.end_min % 60:02d}",
            "is_lab": False,
        }


@dataclass(frozen=True)
class WeeklyPattern:
    pattern_id: str
    meetings: tuple[WeeklyMeeting, ...]

    @property
    def label(self) -> str:
        return (
            "Morning · 09:00–12:00"
            if self.meetings[0].start_min == 540
            else "Afternoon · 13:00–16:00"
        )

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "label": self.label,
            "meetings": [m.to_dict() for m in self.meetings],
        }


@dataclass(frozen=True)
class CourseMeetingPolicy:
    policy_id: str
    patterns: tuple[WeeklyPattern, ...]
    meeting_kind: str = "lecture"
    uses_shared_room: bool = False

    @property
    def durations(self) -> tuple[int, ...]:
        return tuple(m.end_min - m.start_min for m in self.patterns[0].meetings)

    def pattern_for(self, meetings: Iterable[object]) -> WeeklyPattern | None:
        try:
            signature = sorted(meeting_signature(m) for m in meetings)
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        return next(
            (
                p
                for p in self.patterns
                if signature == sorted(meeting_signature(m) for m in p.meetings)
            ),
            None,
        )

    def accepts(self, meetings: Iterable[object]) -> bool:
        return self.pattern_for(meetings) is not None


ENGLISH_INTENSIVE_POLICY = CourseMeetingPolicy(
    policy_id="ENG_INTENSIVE_V1",
    patterns=tuple(
        WeeklyPattern(
            pattern_id=f"ENG_INTENSIVE_V1_{name}",
            meetings=tuple(WeeklyMeeting(day, start, end) for day in ("SUN", "MON", "WED", "THU")),
        )
        for name, start, end in (("AM", 540, 720), ("PM", 780, 960))
    ),
)


def get_course_meeting_policy(
    course_code: object, course_key: object = "", course_number: object = ""
) -> CourseMeetingPolicy | None:
    """Select by exact registrar code while keeping offering identities intact."""
    key, code, number = _code(course_key), _code(course_code), _code(course_number)
    if "::" in key:
        code = key.split("::", 1)[0]
    elif "::" in code:
        code = code.split("::", 1)[0]
    elif not any(c.isdigit() for c in code):
        if key in {"ENG101", "ENG102"}:
            code = key
        elif number:
            code = number if number.startswith(code) else code + number
    return ENGLISH_INTENSIVE_POLICY if code in {"ENG101", "ENG102"} else None


def meeting_options_for_policy(
    policy: CourseMeetingPolicy,
    *,
    blocked_slots: Iterable[Mapping] = (),
    slots: Iterable[Mapping] = (),
) -> list[list[dict]]:
    """Exclude a complete pattern when a blocked interval touches any meeting.

    A start-only blocked cell takes its end from the declared lecture/lab grid.
    An unknown start-only cell still blocks a pattern containing that instant.
    """
    slot_ends = {str(s.get("start")): s.get("end") for s in slots}
    blocked = []
    for row in blocked_slots:
        try:
            day = _code(row.get("day"))
            start = _minutes(row.get("start", row.get("start_time")))
            end_value = row.get("end", row.get("end_time")) or slot_ends.get(
                str(row.get("start", row.get("start_time")))
            )
            end = _minutes(end_value) if end_value else start + 1
            blocked.append((day, start, end))
        except (TypeError, ValueError):
            continue
    options = []
    for index, pattern in enumerate(policy.patterns):
        if any(
            m.day == day and m.start_min < end and start < m.end_min
            for m in pattern.meetings
            for day, start, end in blocked
        ):
            continue
        options.append([dict(m.to_dict(), slot_idx=index) for m in pattern.meetings])
    return options
