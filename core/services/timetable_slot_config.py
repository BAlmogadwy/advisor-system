"""A scenario's grid columns (its slot configs): the effective lists, saving
them without losing data, and which meetings a change would leave off the grid.

The scenario stores two lists, ``slot_config`` (lecture columns) and
``lab_slot_config`` (lab columns). An empty list means the defaults in
``timetable_autoplace``. Those defaults are the only defaults: the pages draw
and edit what :func:`effective_lecture_slots` and :func:`effective_lab_slots`
return, never a copy of their own (they used to carry three different ones,
so the grid could show columns the scheduler does not use).

A column may carry flags besides ``label``, ``start`` and ``end``:
``online_only`` (the evening windows only online GS/GSE use; no automatic
placer may use them, see ``placeable_slots``) and ``policy_only`` (display
columns for course-scoped bundles). Losing a flag is a data defect: an
``online_only`` window that loses it becomes a place where an engine may book
a room at 18:30. So a save carries each flag over from the column it replaces
(matched by start and end), unless the request sets that flag itself.

A meeting is drawn on the grid when its day is a teaching day and its start is
a column start of its kind: a lab when it runs more than 80 minutes, otherwise
a lecture (``isLabPlacement`` in the workspace's script and ``_is_lab_p`` in the
per-plan export use the same rule). A change of columns that would leave a
drawn meeting off the grid must name those meetings and be confirmed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS

GRID_DAYS = ("SUN", "MON", "TUE", "WED", "THU")
LAB_MINUTES_ABOVE = 80
SLOT_FLAGS = ("online_only", "policy_only")
MAX_SLOTS_PER_LIST = 40
MAX_LABEL_LENGTH = 40
# HH:MM, optionally with seconds (stored times can read "09:00:00").
_TIME = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\s*$")


def effective_lecture_slots(scenario: Any) -> list[dict]:
    """The lecture columns the scenario uses: its own, or the defaults."""
    return [dict(slot) for slot in (scenario.slot_config or DEFAULT_SLOTS)]


def effective_lab_slots(scenario: Any) -> list[dict]:
    """The lab columns the scenario uses: its own, or the defaults."""
    return [dict(slot) for slot in (scenario.lab_slot_config or DEFAULT_LAB_SLOTS)]


def _minutes(value: Any) -> int | None:
    match = _TIME.match(str(value)) if value is not None else None
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _hhmm(value: Any) -> str | None:
    minutes = _minutes(value)
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class SlotError:
    """One invalid column in a save request."""

    list: str  # "slot_config" or "lab_slot_config"
    index: int
    field: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "list": self.list,
            "index": self.index,
            "field": self.field,
            "message": self.message,
        }


def clean_slot_rows(rows: Any, list_name: str) -> tuple[list[dict], list[SlotError]]:
    """Validate one list of columns from a save request.

    Each column needs a start and an end as HH:MM, the start before the end,
    and a different start and end from every other column in its list. The
    label is optional (it defaults to "start-end"). Only the known flags are
    kept, and only as booleans; anything else in a row is dropped.
    """
    errors: list[SlotError] = []
    if not isinstance(rows, list):
        return [], [SlotError(list_name, -1, "list", "must be an array")]
    if len(rows) > MAX_SLOTS_PER_LIST:
        errors.append(
            SlotError(list_name, -1, "list", f"has more than {MAX_SLOTS_PER_LIST} columns")
        )
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows[:MAX_SLOTS_PER_LIST]):
        if not isinstance(row, dict):
            errors.append(SlotError(list_name, index, "row", "must be an object"))
            continue
        start, end = _hhmm(row.get("start")), _hhmm(row.get("end"))
        if start is None:
            errors.append(SlotError(list_name, index, "start", "must be a time as HH:MM"))
        if end is None:
            errors.append(SlotError(list_name, index, "end", "must be a time as HH:MM"))
        if start is None or end is None:
            continue
        if start >= end:
            errors.append(SlotError(list_name, index, "end", "must be after the start"))
            continue
        if (start, end) in seen:
            errors.append(SlotError(list_name, index, "start", "repeats another column"))
            continue
        seen.add((start, end))
        label = row.get("label")
        label = str(label).strip()[:MAX_LABEL_LENGTH] if label is not None else ""
        clean: dict[str, object] = {"label": label or f"{start}-{end}", "start": start, "end": end}
        for flag in SLOT_FLAGS:
            if flag in row:
                clean[flag] = bool(row[flag])
        cleaned.append(clean)
    return cleaned, errors


def carry_slot_flags(new_rows: list[dict], previous_rows: list[dict]) -> list[dict]:
    """Give each new column the flags of the column it replaces.

    Columns are matched by start and end. A flag the request sets itself wins;
    a flag it does not mention comes from the matched column. Flags that are
    false are not stored, so an unflagged column stays ``{label, start, end}``.
    """
    previous = {
        (_hhmm(slot.get("start")), _hhmm(slot.get("end"))): slot
        for slot in previous_rows
        if isinstance(slot, dict)
    }
    carried: list[dict] = []
    for row in new_rows:
        match = previous.get((row["start"], row["end"])) or {}
        out = {"label": row["label"], "start": row["start"], "end": row["end"]}
        for flag in SLOT_FLAGS:
            value = row[flag] if flag in row else bool(match.get(flag))
            if value:
                out[flag] = True
        carried.append(out)
    return carried


def is_lab_meeting(start_time: Any, end_time: Any) -> bool:
    """The grid's lab rule: a meeting longer than 80 minutes."""
    start, end = _minutes(start_time), _minutes(end_time)
    if start is None or end is None:
        return False
    return end - start > LAB_MINUTES_ABOVE


def is_on_grid(
    day: Any, start_time: Any, end_time: Any, lecture_slots: list[dict], lab_slots: list[dict]
) -> bool:
    """Whether the workspace draws this meeting: a teaching day and a column start of its kind."""
    if str(day or "").upper()[:3] not in GRID_DAYS:
        return False
    start = _hhmm(start_time)
    columns = lab_slots if is_lab_meeting(start_time, end_time) else lecture_slots
    return start is not None and any(
        _hhmm(slot.get("start")) == start for slot in columns if isinstance(slot, dict)
    )


def hidden_by_slot_change(
    scenario: Any, new_lecture_slots: list[dict], new_lab_slots: list[dict]
) -> list[dict[str, object]]:
    """The meetings drawn today that the new columns would leave off the grid.

    Every board of the scenario counts. An empty new list means the defaults,
    as it does when saved. Meetings already off the grid are not listed: the
    change does not hide them.
    """
    from core.models import SectionPlacement

    old_lecture, old_lab = effective_lecture_slots(scenario), effective_lab_slots(scenario)
    new_lecture = new_lecture_slots or [dict(slot) for slot in DEFAULT_SLOTS]
    new_lab = new_lab_slots or [dict(slot) for slot in DEFAULT_LAB_SLOTS]
    placements = (
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by(
            "board__display_order",
            "board_id",
            "term_section__course_code",
            "term_section__section",
            "day",
            "start_time",
            "id",
        )
    )
    hidden: list[dict[str, object]] = []
    for placement in placements:
        args = (placement.day, placement.start_time, placement.end_time)
        if is_on_grid(*args, old_lecture, old_lab) and not is_on_grid(*args, new_lecture, new_lab):
            section = placement.term_section
            hidden.append(
                {
                    "placement_id": placement.id,
                    "board_id": placement.board_id,
                    "board_label": placement.board.label,
                    "course_code": getattr(section, "course_code", "") or "",
                    "section": getattr(section, "section", "") or "",
                    "day": str(placement.day or "").upper()[:3],
                    "start_time": _hhmm(placement.start_time) or str(placement.start_time),
                    "end_time": _hhmm(placement.end_time) or str(placement.end_time),
                    "is_locked": bool(placement.is_locked),
                }
            )
    return hidden


def hidden_fingerprint(hidden: list[dict[str, object]]) -> str:
    """A short digest of exactly which meetings a change hides.

    A confirmation names this digest, not a count: two different sets of the
    same size must not confirm each other.
    """
    ids = ",".join(str(pid) for pid in sorted(int(str(item["placement_id"])) for item in hidden))
    return hashlib.sha256(ids.encode("ascii")).hexdigest()[:16]
