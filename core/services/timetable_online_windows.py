"""Online courses in the online evening windows (``TIMETABLE_ONLINE_EVENING_WINDOWS``).

Owner request 2026-10-26: online courses meet only in Online 1 (15:50-17:30),
Online 2 (17:40-19:20) and Online 3 (19:30-21:10). Those windows are the
scenario's ``online_only`` columns (``DEFAULT_LAB_SLOTS`` declares them), which
``placeable_slots`` keeps out of every automatic placer so that no class that
needs a room is ever put in the evening.

With the flag ON, the automatic placers and movers (the greedy placer, both
CP-SAT builders, simulated annealing, the load balancer, the instructor cap /
clash repairs and the one-teacher guard that mirrors the placer) give a section
that is online for its board the windows below as its ONLY candidates, on any
weekday; a section that is not online keeps exactly today's candidates. With the
flag OFF :func:`active_online_windows` returns nothing and no caller changes.

A meeting placed in a window takes the window's own start and end. The grid
draws a meeting in the first column of its kind whose start is the meeting's
start, a lab being a meeting over 80 minutes (``drawn_column``); the windows are
100-minute lab columns, so a 75-minute meeting at 15:50 would be parked off the
grid, out of the readiness check's sight. A window is used only when the grid
draws a window-long meeting at its start in that very window: a window shadowed
by an earlier column with the same start, or one in the wrong list for its
length, is not a place a meeting could be seen, so it is not offered.

This module must not import ``timetable_autoplace`` or ``timetable_slot_config``
at module level: ``timetable_slot_config`` imports ``timetable_autoplace``, which
imports this module.
"""

from __future__ import annotations

import logging
from itertools import combinations, product
from typing import Any

from core.services.timetable_flags import is_online_evening_windows_enabled

logger = logging.getLogger(__name__)

# The teaching days, in the order every placer enumerates them.
WINDOW_DAYS = ("SUN", "MON", "TUE", "WED", "THU")


def active_online_windows(
    slot_config: Any,
    lab_slot_config: Any,
    *,
    warn_for: str = "",
) -> list[dict]:
    """The online windows an automatic placer offers an online section, or ``[]``.

    ``[]`` when the flag is OFF. Otherwise the scenario's effective columns (its
    own lists, or the defaults when a list is empty) are read in declared order,
    lecture columns first, and every column that is ``online_only``, not
    ``policy_only``, and drawn by the grid as itself is kept, once per start and
    end. Each window is ``{"label", "start", "end", "slot_idx"}`` with
    ``slot_idx`` counting from 0 among the windows.

    When the flag is ON but no column qualifies, the engines keep today's
    candidates for online sections (they never invent a column the grid cannot
    draw); ``warn_for`` names the caller in the warning logged then.
    """
    if not is_online_evening_windows_enabled():
        return []
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
    from core.services.timetable_slot_config import _hhmm, drawn_column

    lecture = [slot for slot in (slot_config or DEFAULT_SLOTS) if isinstance(slot, dict)]
    lab = [slot for slot in (lab_slot_config or DEFAULT_LAB_SLOTS) if isinstance(slot, dict)]
    windows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for slot in (*lecture, *lab):
        if not slot.get("online_only") or slot.get("policy_only"):
            continue
        start, end = _hhmm(slot.get("start")), _hhmm(slot.get("end"))
        if start is None or end is None or start >= end or (start, end) in seen:
            continue
        column = drawn_column(WINDOW_DAYS[0], start, end, lecture, lab)
        if (
            column is None
            or not column.get("online_only")
            or column.get("policy_only")
            or (_hhmm(column.get("start")), _hhmm(column.get("end"))) != (start, end)
        ):
            continue
        seen.add((start, end))
        label = str(slot.get("label") or "").strip() or f"{start}-{end}"
        windows.append({"label": label, "start": start, "end": end, "slot_idx": len(windows)})
    if not windows and warn_for:
        logger.warning(
            "TIMETABLE_ONLINE_EVENING_WINDOWS is on but %s has no drawable online-only "
            "window among its columns; its online courses keep their daytime candidates.",
            warn_for,
        )
    return windows


def online_window_cells(windows: list[dict], blocked: set[tuple[str, str]]) -> list[dict]:
    """Every weekday x window cell that is not blocked, day by day.

    ``blocked`` is the ``{(day, start)}`` set of ``blocked_slot_keys``. Each cell
    is ``{"day", "slot_idx", "start", "end"}``.
    """
    return [
        {"day": day, "slot_idx": window["slot_idx"], "start": window["start"], "end": window["end"]}
        for day in WINDOW_DAYS
        for window in windows
        if (day, window["start"]) not in blocked
    ]


def online_window_relocate_domain(
    windows: list[dict], blocked: set[tuple[str, str]], start: Any, end: Any
) -> list[dict]:
    """The cells a relocating mover (annealing, the load balancer) may move one
    meeting of an online section to: the window cells of the meeting's own length.

    Those movers persist only when every section keeps its meeting lengths
    (``_persist_guard_rejections``), so a window of another length is never a
    move they could keep. A meeting of another length (one placed in the day
    before the flag, or by hand) gets no cell: it stays where it is until a
    Build re-places its section, and no mover ever puts it back in the day.
    Each cell carries the ``mask`` the movers compare.
    """
    from core.services.timetable_slot_config import _minutes
    from core.services.timetable_workspace import _time_mask

    begin, finish = _minutes(start), _minutes(end)
    if begin is None or finish is None:
        return []
    return [
        dict(cell, mask=_time_mask(cell["day"], cell["start"], cell["end"]))
        for cell in online_window_cells(windows, blocked)
        if (_minutes(cell["end"]) or 0) - (_minutes(cell["start"]) or 0) == finish - begin
    ]


def _adjacent_day_pairs(days: tuple[str, ...]) -> int:
    """The greedy generator's day-combination order: fewer adjacent pairs first."""
    indices = sorted(WINDOW_DAYS.index(day) for day in days)
    return sum(1 for left, right in zip(indices, indices[1:], strict=False) if right - left == 1)


def online_window_meeting_options(
    meeting_count: int, windows: list[dict], blocked: set[tuple[str, str]]
) -> list[list[dict]]:
    """Every weekly option for an online section with ``meeting_count`` meetings.

    One meeting a day on distinct weekdays (combinations ordered like
    ``_generate_meeting_options``: non-adjacent days first), each meeting in any
    window that is not blocked that day, in declared window order. Each meeting
    is ``{"day", "start", "end", "slot_idx"}``, the shape of the daytime
    generator's meetings. 1, 2 and 3 meetings over three windows give 15, 90 and
    270 options.
    """
    if meeting_count <= 0 or not windows:
        return []
    options: list[list[dict]] = []
    for days in sorted(combinations(WINDOW_DAYS, meeting_count), key=_adjacent_day_pairs):
        for chosen in product(windows, repeat=meeting_count):
            if any(
                (day, window["start"]) in blocked for day, window in zip(days, chosen, strict=True)
            ):
                continue
            options.append(
                [
                    {
                        "day": day,
                        "start": window["start"],
                        "end": window["end"],
                        "slot_idx": window["slot_idx"],
                    }
                    for day, window in zip(days, chosen, strict=True)
                ]
            )
    return options
