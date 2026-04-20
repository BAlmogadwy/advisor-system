"""PR3 commit 5 — warm-start + perturbation-metric module.

Holds the four public symbols the PR3 DoR nailed down:

- ``BaselinePlacement`` — a frozen dataclass a caller supplies per section
  to tell the planner "if this slot is still legal, please keep it".
- ``apply_warm_start`` — inline-helper used inside ``auto_place_board``
  (per ChatGPT commit-5 ruling O2). Returns the matching candidate
  option from the pre-generated ``_generate_meeting_options`` list, or
  ``None`` if no option matches the baseline slot. The caller is
  responsible for validating the returned option against the board's
  current placements — this helper only pairs baseline → candidate.
- ``compute_perturbation_metric`` — standalone function (ruling S1) that
  counts ``unchanged`` / ``changes_from_baseline`` / ``newly_placed`` /
  ``removed`` sections between a baseline and the final placements.
- ``is_warm_start_enabled`` — reads
  ``settings.TIMETABLE_PR3_WARM_START_ENABLED``. Default ``False`` until
  commit 8's promotion; callers must pass ``baseline_placements``
  explicitly AND the flag must be ``True`` for retention to kick in.

No DB writes anywhere in this module — baseline is caller-supplied,
in-memory only (DoR §warm-start-scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings


@dataclass(frozen=True)
class BaselinePlacement:
    """A single baseline slot for a section from a prior planner run.

    Carries only the minimum needed to pair baseline → candidate option
    inside ``auto_place_board``: course + section identity and the
    time-block (day + start + end). Room is intentionally NOT part of
    the dataclass (ChatGPT ruling P2) — re-running the rooming oracle
    against the retained time-slot is cheaper and safer than pinning a
    room that may have been re-typed or lost capacity since the baseline
    was produced.
    """

    course_code: str
    section: str
    day: str
    start_time: str
    end_time: str

    @property
    def section_code(self) -> str:
        """``"<course>|<section>"`` — matches the key shape used
        throughout PR1/PR2/PR3 placement dicts and trace maps."""
        return f"{self.course_code}|{self.section}"


def is_warm_start_enabled() -> bool:
    """Return whether PR3 warm-start retention is active.

    Reads ``settings.TIMETABLE_PR3_WARM_START_ENABLED``. Default ``False``
    until commit 8 promotes it: warm-start changes placement decisions
    (unlike trace capture), so the default stays opt-in until the
    broader scenario pack has run through the perturbation audit.
    """
    return bool(getattr(settings, "TIMETABLE_PR3_WARM_START_ENABLED", False))


def _entry_to_tuple(entry: Any) -> tuple[str, str, str]:
    """Pull ``(day, start_time, end_time)`` from either a
    ``BaselinePlacement`` or a plain dict. Missing keys yield empty
    strings so comparisons against candidate options fall through
    cleanly rather than raising."""
    if isinstance(entry, BaselinePlacement):
        return entry.day, entry.start_time, entry.end_time
    if isinstance(entry, dict):
        return (
            str(entry.get("day", "")),
            str(entry.get("start_time", "")),
            str(entry.get("end_time", "")),
        )
    return "", "", ""


def apply_warm_start(
    section_code: str,
    baseline_map: dict[str, Any] | None,
    candidate_options: list,
) -> list | None:
    """Return the candidate option whose first meeting matches the
    baseline for ``section_code``, or ``None`` if no baseline exists or
    no option matches.

    ``candidate_options`` is the list returned by
    ``_generate_meeting_options`` — each option is itself a list of
    meeting dicts with keys ``day`` / ``start`` / ``end`` / ``slot_idx``.
    Matching compares the first meeting only (single-meeting courses are
    the common warm-start case; multi-meeting retention is a separate
    commit).

    This helper does NOT check feasibility against the current board
    state (student overlap, room occupancy, lock collisions). The caller
    is responsible for validating the returned option — typically by
    checking whether it appears in the scoring-loop's ``scored_options``
    list (if it does, it survived the feasibility filters).
    """
    if not baseline_map:
        return None
    entry = baseline_map.get(section_code)
    if entry is None:
        return None
    day, start, end = _entry_to_tuple(entry)
    if not day or not start:
        return None
    for option in candidate_options:
        if not option:
            continue
        first = option[0]
        if first.get("day") == day and first.get("start") == start and first.get("end") == end:
            return option
    return None


def compute_perturbation_metric(
    placements: list[dict],
    baseline: dict[str, Any] | None,
) -> dict[str, int]:
    """Return the four perturbation counters for a scenario re-run.

    Keys match the PR3 DoR schema exactly (no synonyms):

    - ``unchanged_count`` — section was in baseline AND ended up at the
      same (day, start_time, end_time). Room is not part of the
      comparison (see ``BaselinePlacement`` docstring: rooming is
      re-derived, not pinned).
    - ``changes_from_baseline_count`` — section was in baseline AND
      ended up at a different slot.
    - ``newly_placed_count`` — section was NOT in baseline but ended up
      placed. With ``baseline=None`` every placement lands here.
    - ``removed_count`` — section WAS in baseline but did not end up
      placed (dropped because no legal slot, or pruned out of the
      scenario entirely).

    ``placements`` is the ``placement_results`` list emitted by
    ``auto_place_board`` — each entry has ``course_code`` / ``section``
    / ``meetings`` where ``meetings[0]`` is the first meeting's
    ``{day, start, end, room}`` dict.
    """
    if not baseline:
        return {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": len(placements),
            "removed_count": 0,
        }

    placed_codes: set[str] = set()
    unchanged = 0
    changed = 0
    newly = 0

    for placement in placements:
        section_code = f"{placement.get('course_code', '')}|{placement.get('section', '')}"
        placed_codes.add(section_code)
        entry = baseline.get(section_code)
        meetings = placement.get("meetings") or []
        first_meeting = meetings[0] if meetings else {}
        if entry is None:
            newly += 1
            continue
        b_day, b_start, b_end = _entry_to_tuple(entry)
        if (
            first_meeting.get("day") == b_day
            and first_meeting.get("start") == b_start
            and first_meeting.get("end") == b_end
        ):
            unchanged += 1
        else:
            changed += 1

    removed = sum(1 for section_code in baseline if section_code not in placed_codes)

    return {
        "changes_from_baseline_count": changed,
        "unchanged_count": unchanged,
        "newly_placed_count": newly,
        "removed_count": removed,
    }
