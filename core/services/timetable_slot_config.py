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
placer may use them, see ``placeable_slots``, except for an online section when
``TIMETABLE_ONLINE_EVENING_WINDOWS`` is on, see ``timetable_online_windows``)
and ``policy_only`` (display
columns for course-scoped bundles). Losing a flag is a data defect: an
``online_only`` window that loses it becomes a place where an engine may book
a room at 18:30. So a save carries each flag over from the column it replaces
(matched by start and end), unless the request sets that flag itself.

A meeting is drawn on the grid when its day is a teaching day and its start is
a column start of its kind: a lab when it runs more than 80 minutes, otherwise
a lecture (``isLabPlacement`` in the workspace's script and ``_is_lab_p`` in the
exports use the same rule). A meeting under a course meeting policy (the
three-hour English bundles) is drawn in its own table built from the meetings
themselves, so no column change ever hides it. A meeting drawn in an
online-only window (:func:`online_window`) whose course is not online for its
board is a publish-readiness warning, never a refusal
(``check_publish_readiness``, ``online_window_room_class``).

A change of columns must be confirmed, by name, when it would leave drawn
meetings off the grid, or when a flagged column (an online-only window)
disappears: a moved online window is a plain column the engines may use.
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
# HH:MM, optionally with seconds (stored times can read "09:00:00"). The
# digits are ASCII, as in the pages' copy of this rule (``SERVER_TIME`` in
# static/js/timetable-slot-config.js, where ``\d`` is ASCII). ``\d`` here
# would also read any Unicode digit: an Arabic-Indic nine then ":30" was 09:30
# on the grid for the server and parked by the page. ``\s`` stays Unicode, as
# the pages' ``\s`` is, so a time with a no-break space around it reads as
# before (``re.ASCII`` would narrow ``\s`` too).
_TIME = re.compile(r"^\s*([01]?[0-9]|2[0-3]):([0-5][0-9])(?::[0-5][0-9])?\s*$")


def effective_lecture_slots(scenario: Any) -> list[dict]:
    """The lecture columns the scenario uses: its own, or the defaults."""
    return [
        dict(slot) for slot in (scenario.slot_config or DEFAULT_SLOTS) if isinstance(slot, dict)
    ]


def effective_lab_slots(scenario: Any) -> list[dict]:
    """The lab columns the scenario uses: its own, or the defaults."""
    return [
        dict(slot)
        for slot in (scenario.lab_slot_config or DEFAULT_LAB_SLOTS)
        if isinstance(slot, dict)
    ]


def offered_move_columns(slots: list[dict], meeting_start: Any) -> list[dict]:
    """The columns of one kind a manual move of a meeting is offered.

    ``slots`` are the kind's effective columns. A policy column is never
    offered (a course-scoped bundle moves by its own patterns). An online-only
    (evening) window is offered only when the meeting already starts in one,
    so an online class can move between evening windows while a class that
    needs a room is never put where nothing is open. This is the rule of
    ``offeredColumns`` in ``static/js/timetable-slot-config.js``, which the
    pages apply to the columns they draw; the two must agree.
    """
    start = str(meeting_start or "")[:5]
    in_online = any(slot.get("online_only") and slot.get("start") == start for slot in slots)
    return [
        slot
        for slot in slots
        if not slot.get("policy_only") and (not slot.get("online_only") or in_online)
    ]


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
    kept, and only when they are real booleans; anything else in a row is
    dropped.
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
            # Only a real boolean says something; "false", null and the like
            # are not taken as a choice (they would clear a carried flag).
            if isinstance(row.get(flag), bool):
                clean[flag] = row[flag]
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
    day: Any,
    start_time: Any,
    end_time: Any,
    lecture_slots: list[dict],
    lab_slots: list[dict],
    *,
    policy_meeting: bool = False,
) -> bool:
    """Whether the workspace draws this meeting: a teaching day and a column start of its kind.

    A ``policy_meeting`` (a course meeting policy, e.g. English) has its own
    table built from its meetings: it is drawn whatever the columns.
    """
    if str(day or "").upper()[:3] not in GRID_DAYS:
        return False
    if policy_meeting:
        return True
    return drawn_column(day, start_time, end_time, lecture_slots, lab_slots) is not None


def drawn_column(
    day: Any,
    start_time: Any,
    end_time: Any,
    lecture_slots: list[dict],
    lab_slots: list[dict],
) -> dict | None:
    """The column the grid draws a meeting in, or None when it is parked.

    A teaching day, and the first column of the meeting's kind (a lab runs
    more than 80 minutes) whose start is the meeting's start: the renderer
    draws a meeting once, in the first such column (``firstColumn`` in the
    workspace's ``_renderSingleGrid``). A meeting under a course meeting policy
    is not asked here: it has its own table (``is_on_grid``).
    """
    if str(day or "").upper()[:3] not in GRID_DAYS:
        return None
    start = _hhmm(start_time)
    if start is None:
        return None
    columns = lab_slots if is_lab_meeting(start_time, end_time) else lecture_slots
    return next(
        (slot for slot in columns if isinstance(slot, dict) and _hhmm(slot.get("start")) == start),
        None,
    )


def online_window(
    day: Any,
    start_time: Any,
    end_time: Any,
    lecture_slots: list[dict],
    lab_slots: list[dict],
) -> dict[str, str] | None:
    """The online-only window the grid draws a meeting in, or None.

    The owner's rule (2026-07-28, ``DEFAULT_LAB_SLOTS``): only online courses
    meet in the online-only windows; nothing in the estate is open in the
    evening. Which column a meeting is in is the grid's rule
    (:func:`drawn_column`), so a lab in Lab 5 (16:30-18:10) is not "in" Online 1
    or Online 2 although their times overlap, and a meeting off the grid is in
    none (the page lists it under its table).

    Returns the window's ``label`` (its ``start-end`` when it has none) and the
    meeting's ``day``, ``start`` and ``end`` as the grid reads them
    (``SUN``, ``HH:MM``).
    """
    column = drawn_column(day, start_time, end_time, lecture_slots, lab_slots)
    if column is None or not column.get("online_only"):
        return None
    start, end = _hhmm(start_time) or "", _hhmm(end_time) or str(end_time or "")
    label = str(column.get("label") or "").strip()
    return {
        "label": label or f"{_hhmm(column.get('start'))}-{_hhmm(column.get('end'))}",
        "day": str(day).upper()[:3],
        "start": start,
        "end": end,
    }


def hidden_by_slot_change(
    scenario: Any, new_lecture_slots: list[dict], new_lab_slots: list[dict]
) -> list[dict[str, object]]:
    """The meetings drawn today that the new columns would leave off the grid.

    Every board of the scenario counts. An empty new list means the defaults,
    as it does when saved. Meetings already off the grid are not listed: the
    change does not hide them; nor are policy meetings, which are drawn in
    their own table.
    """
    from core.models import SectionPlacement
    from core.services.timetable_course_policy import get_course_meeting_policy

    old_lecture, old_lab = effective_lecture_slots(scenario), effective_lab_slots(scenario)
    new_lecture = new_lecture_slots or [
        dict(slot) for slot in DEFAULT_SLOTS if isinstance(slot, dict)
    ]
    new_lab = new_lab_slots or [dict(slot) for slot in DEFAULT_LAB_SLOTS if isinstance(slot, dict)]
    rows = (
        SectionPlacement.objects.filter(board__scenario=scenario)
        .order_by(
            "board__display_order",
            "board_id",
            "term_section__course_code",
            "term_section__section",
            "day",
            "start_time",
            "id",
        )
        .values(
            "id",
            "board_id",
            "board__label",
            "day",
            "start_time",
            "end_time",
            "is_locked",
            "term_section__course_code",
            "term_section__course_key",
            "term_section__course_number",
            "term_section__section",
        )
    )
    policy_cache: dict[tuple, bool] = {}
    hidden: list[dict[str, object]] = []
    for row in rows:
        identity = (
            row["term_section__course_code"],
            row["term_section__course_key"],
            row["term_section__course_number"],
        )
        if identity not in policy_cache:
            policy_cache[identity] = get_course_meeting_policy(*identity) is not None
        args = (row["day"], row["start_time"], row["end_time"])
        kind = {"policy_meeting": policy_cache[identity]}
        if is_on_grid(*args, old_lecture, old_lab, **kind) and not is_on_grid(
            *args, new_lecture, new_lab, **kind
        ):
            hidden.append(
                {
                    "placement_id": row["id"],
                    "board_id": row["board_id"],
                    "board_label": row["board__label"] or "",
                    "course_code": row["term_section__course_code"] or "",
                    "section": row["term_section__section"] or "",
                    "day": str(row["day"] or "").upper()[:3],
                    "start_time": _hhmm(row["start_time"]) or str(row["start_time"]),
                    "end_time": _hhmm(row["end_time"]) or str(row["end_time"]),
                    "is_locked": bool(row["is_locked"]),
                }
            )
    return hidden


def dropped_flagged_columns(
    previous_rows: list[dict], new_rows: list[dict], list_name: str
) -> list[dict[str, object]]:
    """Flags (online-only, policy) that a change takes off a column.

    Compared after the carry-over: a column keeps a flag when a new column
    with its start and end still has it. One that moves loses it (an online
    window edited to other times is a plain column the automatic placers may
    use), and so does one the request sets false. An empty new list means the
    defaults, as it does when saved.
    """
    if not new_rows:
        defaults = DEFAULT_SLOTS if list_name == "slot_config" else DEFAULT_LAB_SLOTS
        new_rows = [dict(slot) for slot in defaults if isinstance(slot, dict)]
    kept: dict[tuple[str | None, str | None], set[str]] = {}
    for row in new_rows:
        if isinstance(row, dict):
            key = (_hhmm(row.get("start")), _hhmm(row.get("end")))
            kept.setdefault(key, set()).update(flag for flag in SLOT_FLAGS if row.get(flag))
    dropped: list[dict[str, object]] = []
    for slot in previous_rows:
        if not isinstance(slot, dict):
            continue
        start, end = _hhmm(slot.get("start")), _hhmm(slot.get("end"))
        lost = [
            flag
            for flag in SLOT_FLAGS
            if slot.get(flag) and flag not in kept.get((start, end), set())
        ]
        if lost:
            dropped.append(
                {
                    "list": list_name,
                    "label": str(slot.get("label") or f"{start}-{end}"),
                    "start": start,
                    "end": end,
                    "flags": lost,
                }
            )
    return dropped


def confirmation_fingerprint(
    hidden: list[dict[str, object]], dropped: list[dict[str, object]]
) -> str:
    """A short digest of exactly what a change asks the user to accept.

    A confirmation names this digest, not a count: two different sets of the
    same size must not confirm each other.
    """
    ids = ",".join(str(pid) for pid in sorted(int(str(item["placement_id"])) for item in hidden))
    columns = ",".join(sorted(f"{d['list']}:{d['start']}-{d['end']}" for d in dropped))
    return hashlib.sha256(f"{ids}|{columns}".encode("ascii")).hexdigest()[:16]
