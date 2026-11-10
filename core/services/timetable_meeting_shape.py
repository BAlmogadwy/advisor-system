"""The weekly shape a course is taught in, when the department chooses it.

Credit hours give every course its default weekly shape: a 3-credit course meets
twice a week for 75 minutes (``timetable_autoplace.MEETING_PATTERNS``). A
department may instead teach a 3-credit course as TWO 100-MINUTE MEETINGS a week
(owner, 2026-11-10: STAT103 and MATH243 in the hand-made 1448 sheets). This
module names those shapes and decides, from a stored choice and the course's
credit hours, which one applies.

The contract of the one shape there is today, ``"2x100"``:

* two meetings a week, on two different days;
* each in a 100-minute column - the lab-grid TIMES (Lab 1-5), which is where the
  timetable already keeps its 100-minute windows;
* in a LECTURE room. A meeting's room type is decided by credit hours (a lab
  room only for a 4-credit course's 100-minute meeting:
  ``timetable_rooming._room_type_for_credit_duration``), never by this shape;
* an ordinary duration shape for every rule: same-course separation, day
  spread, one teacher per section, rooms. It is NOT a ``CourseMeetingPolicy``
  (``timetable_course_policy``): a policy means a fixed, roomless English bundle
  that siblings may overlap, selected by registrar code.

Where a choice is stored: ``ScenarioSectionBudget.meeting_pattern``, the
scenario's own copy, which every engine of that scenario reads. ``""`` is the
credit default. A course that is not configured behaves exactly as before.

The module is pure - no Django, no database - so the classic engines, the
workspace, the manual importer and the new scheduler can all read one rule.
Read paths never raise: an unknown or unusable stored value means the credit
default. Only the write paths (:func:`validate_meeting_pattern`) refuse.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

STANDARD = ""
TWO_BY_100 = "2x100"

#: A meeting longer than this sits in the 100-minute column family. It is the
#: same split ``timetable_autoplace._generate_meeting_options`` uses to choose a
#: grid, so a shape's durations are grid selectors: a 100-minute meeting takes
#: whatever length the scenario's 100-minute column has.
LONG_MEETING_MINUTES = 75


@dataclass(frozen=True)
class MeetingShape:
    """A weekly shape a department may choose for a course."""

    shape_id: str
    durations: tuple[int, ...]
    credit_hours: int
    label: str
    label_ar: str


SHAPES: dict[str, MeetingShape] = {
    TWO_BY_100: MeetingShape(
        shape_id=TWO_BY_100,
        durations=(100, 100),
        credit_hours=3,
        label="2 × 100 min",
        label_ar="٢ × ١٠٠ دقيقة",
    ),
}

#: Every value a stored ``meeting_pattern`` may hold, in display order.
PATTERN_CHOICES: tuple[str, ...] = (STANDARD, *SHAPES)


def _engine_credits(credit_hours: object) -> int:
    """Credit hours as the engines read them: missing or unreadable means 3."""
    try:
        return int(credit_hours or 3)
    except (TypeError, ValueError):
        return 3


def _exact_credits(credit_hours: object) -> int | None:
    """Credit hours exactly as stored, for write-time checks (None if unreadable)."""
    if isinstance(credit_hours, bool):
        return None
    try:
        return int(str(credit_hours).strip())
    except (TypeError, ValueError):
        return None


def shape_for(meeting_pattern: object, credit_hours: object) -> MeetingShape | None:
    """The configured shape that applies, or None for the credit default.

    Never raises. A value this module does not know, or a shape stored for a
    course whose credit hours it does not fit, reads as the credit default.
    Credit hours are read as the engines read them (missing means 3), so every
    engine of a scenario resolves the same budget row to the same shape.
    """
    shape = SHAPES.get(str(meeting_pattern or "").strip().lower())
    if shape is None or shape.credit_hours != _engine_credits(credit_hours):
        return None
    return shape


def configured_durations(meeting_pattern: object, credit_hours: object) -> list[int] | None:
    """The configured shape's durations as a fresh list, or None for the default."""
    shape = shape_for(meeting_pattern, credit_hours)
    return list(shape.durations) if shape else None


def pattern_choices(credit_hours: object) -> list[str]:
    """The values an advisor may choose for a course with these credit hours."""
    credits = _exact_credits(credit_hours)
    return [STANDARD, *(s.shape_id for s in SHAPES.values() if s.credit_hours == credits)]


def normalise_meeting_pattern(value: object) -> str:
    """A submitted choice as stored; refuses anything that is not a known shape."""
    text = str(value if value is not None else "").strip().lower().replace(" ", "")
    if text in ("", "standard"):
        return STANDARD
    if text in SHAPES:
        return text
    raise ValueError(f"Unknown weekly meeting pattern: {value!r}.")


def validate_meeting_pattern(
    meeting_pattern: object, credit_hours: object, *, course_code: str = ""
) -> str:
    """Check a choice before it is written; returns the value to store.

    Going back to the credit default is always allowed. A shape is allowed only
    for a course with exactly the credit hours it was defined for.
    """
    value = normalise_meeting_pattern(meeting_pattern)
    if value == STANDARD:
        return value
    shape = SHAPES[value]
    credits = _exact_credits(credit_hours)
    if credits != shape.credit_hours:
        name = course_code or "This course"
        shown = credits if credits is not None else "unknown"
        raise ValueError(
            f"{name} has {shown} credit hours; {shape.label} a week is only for "
            f"{shape.credit_hours}-credit courses."
        )
    return value


def shape_label(meeting_pattern: object, credit_hours: object, *, arabic: bool = False) -> str:
    """The configured shape's label, or "" for the credit default."""
    shape = shape_for(meeting_pattern, credit_hours)
    if shape is None:
        return ""
    return shape.label_ar if arabic else shape.label


def meeting_family(minutes: int) -> str:
    """Which column family a meeting of this length sits in: "long" or "short"."""
    return "long" if int(minutes) > LONG_MEETING_MINUTES else "short"


def meetings_match_shape(minutes: Iterable[int], shape: MeetingShape) -> bool:
    """Do these meetings have the shape's column families, one for one?

    Compared as families, not exact lengths: a scenario's 100-minute column may
    be a few minutes longer or shorter, and a meeting placed in it keeps the
    column's own length.
    """
    placed = sorted(meeting_family(m) for m in minutes)
    return placed == sorted(meeting_family(d) for d in shape.durations)
