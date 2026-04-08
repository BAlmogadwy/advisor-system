"""
core/services/timetable_autoplace.py
Auto-placement algorithm for the Timetable Workspace feature.

This module implements a greedy constraint-satisfaction algorithm that assigns
university course sections to weekly time slots on delivery boards.  Each
board represents a single nominal term-level (e.g. "Level 3") and belongs to
a TimetableScenario.

The algorithm's primary goal is to **minimise student scheduling conflicts**:
courses taken by the same cohort of students should not overlap in time.  It
also respects a set of hard and soft constraints described below.

Key concepts
------------
* **Delivery board** -- a grid of days x slots for one term level.
* **Section group** -- sections with the same ordinal index across courses
  (e.g. all "S1" sections) share one student cohort, so they must NOT
  overlap.  Sections in *different* groups (S1 vs S2) serve different
  cohorts and CAN overlap.
* **Meeting pattern** -- how many weekly meetings a course needs, and the
  duration of each, determined by credit hours.
* **Prayer-break window** -- a hard constraint blocking any meeting from
  starting between 11:35 and 12:59.

Meeting patterns based on credit hours
---------------------------------------
  4 credits --> 3 meetings/week (75 min, 75 min, 100 min) on 3 different days
  3 credits --> 2 meetings/week (75 min, 75 min) on 2 different days
  2 credits --> 1 meeting/week (100 min)
  1 credit  --> 1 meeting/week (75 min)  [fallback]

Hard constraints (violations are never accepted)
-------------------------------------------------
  - No more than 1 meeting per day per section.
  - No two sections in the same student group may overlap in time.
  - No meeting may start during the prayer-break window (11:35-12:59).

Soft constraints (penalised but not forbidden)
----------------------------------------------
  - Prefer the same time-slot index across all meeting days for one course.
  - Minimise idle gaps between on-campus classes for the same student group.
  - Online courses should be placed in late slots so students can leave
    campus first.
  - Different sections of the same course (taught by one instructor) must
    not overlap (same-course overlap penalty).

Placement order
---------------
Sections are placed in *round-robin by group index*: all S1 sections first,
then all S2 sections, etc.  Within a round, courses are processed in
descending demand order (highest ``total_demand`` first).  This ensures the
most constrained group (S1 -- the primary cohort) gets the best slots.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_workspace import _time_mask, _to_minutes

# ── Meeting Patterns ─────────────────────────────────────────────
# The Saudi academic week runs Sunday through Thursday.
WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]

# Maps credit hours to a list of meeting durations (minutes per meeting).
# The length of the list determines how many meetings per week.
MEETING_PATTERNS: dict[int, list[int]] = {
    4: [75, 75, 100],   # 3 meetings: two 75 min + one 100 min
    3: [75, 75],         # 2 meetings: two 75 min
    2: [100],            # 1 meeting: 100 min
    1: [75],             # 1 meeting: 75 min (fallback for rare 1-credit courses)
}


def get_meeting_pattern(credit_hours: int) -> list[int]:
    """Return the list of per-meeting durations for a given credit-hour count.

    Parameters
    ----------
    credit_hours : int
        The number of credit hours for the course (1-4).

    Returns
    -------
    list[int]
        Durations in minutes for each weekly meeting.
        Falls back to the 3-credit pattern if *credit_hours* is not in the map.
    """
    return MEETING_PATTERNS.get(credit_hours, MEETING_PATTERNS[3])


# ── Default Slots ────────────────────────────────────────────────
# Five 75-minute teaching slots spanning the day, with a gap from 11:45 to
# 13:00 for the midday prayer break.  A scenario may override these via its
# ``slot_config`` JSON field.

DEFAULT_SLOTS = [
    {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
    {"label": "10:30-11:45", "start": "10:30", "end": "11:45"},
    # -- prayer break gap: no slot starts between 11:35 and 12:59 --
    {"label": "13:00-14:15", "start": "13:00", "end": "14:15"},
    {"label": "14:30-15:45", "start": "14:30", "end": "15:45"},
    {"label": "16:00-17:15", "start": "16:00", "end": "17:15"},
]

# Hard constraint: no course meeting may START during the prayer break.
# The window is inclusive on both ends.
BLOCKED_START_WINDOW = ("11:35", "12:59")


def _start_is_blocked(start_time: str) -> bool:
    """Check whether *start_time* falls inside the prayer-break window.

    Parameters
    ----------
    start_time : str
        "HH:MM" string for the proposed meeting start.

    Returns
    -------
    bool
        ``True`` if a meeting starting at this time would violate the
        prayer-break hard constraint.
    """
    start_min = _to_minutes(start_time)
    block_from = _to_minutes(BLOCKED_START_WINDOW[0])
    block_to = _to_minutes(BLOCKED_START_WINDOW[1])
    return block_from <= start_min <= block_to


def _get_slots(slot_config: list[dict]) -> list[dict]:
    """Return *slot_config* if non-empty, otherwise fall back to DEFAULT_SLOTS."""
    return slot_config if slot_config else DEFAULT_SLOTS


# ── Auto-Placement ───────────────────────────────────────────────


def _generate_meeting_options(
    pattern: list[int],
    slot_config: list[dict],
) -> list[list[dict]]:
    """Generate every valid way to schedule a course's weekly meetings.

    The function enumerates combinations of (day, slot) assignments that
    satisfy the hard constraints (one meeting per day, prayer-break
    avoidance) and produces a list of *candidate options* for the scorer
    to rank.

    Parameters
    ----------
    pattern : list[int]
        Per-meeting durations in minutes, e.g. ``[75, 75]`` for a 3-credit
        course.  The list length equals the number of meetings per week.
    slot_config : list[dict]
        Slot definitions from the scenario, or ``DEFAULT_SLOTS``.

    Returns
    -------
    list[list[dict]]
        Each element is one complete option -- a list of meeting dicts, one
        per meeting in *pattern*.  Each meeting dict has keys:
        ``day`` (str), ``start`` (str "HH:MM"), ``end`` (str "HH:MM"),
        ``slot_idx`` (int -- the 0-based position in *slots*).

    Notes
    -----
    * 75 min meetings fit in a single slot.
    * 100 min meetings span two consecutive slots (start of slot *i* to
      end of slot *i+1*).
    * The generator prefers giving every meeting the same ``slot_idx``
      (time-consistency), but falls back to the first available slot when
      the preferred index is unavailable for a particular duration.
    """
    slots = _get_slots(slot_config)
    num_meetings = len(pattern)

    # All ways to pick *num_meetings* distinct days from the 5-day week.
    day_combos = list(combinations(WEEKDAYS, num_meetings))

    # For each meeting duration, pre-compute which (slot_idx, start, end)
    # positions are feasible (respecting prayer break and slot merging).
    slot_options_per_duration: list[list[tuple[int, str, str]]] = []
    for duration in pattern:
        positions = []
        if duration <= 75:
            # Standard lecture -- fits within one slot boundary.
            for i, s in enumerate(slots):
                if not _start_is_blocked(s["start"]):
                    positions.append((i, s["start"], s["end"]))
        else:
            # Extended lecture (100 min) -- merge two consecutive slots.
            # Uses the start of slot[i] and the end of slot[i+1].
            for i in range(len(slots) - 1):
                if not _start_is_blocked(slots[i]["start"]):
                    positions.append((i, slots[i]["start"], slots[i + 1]["end"]))
        slot_options_per_duration.append(positions)

    all_options: list[list[dict]] = []

    for days in day_combos:
        # Iterate over every feasible slot position for the *first* meeting;
        # subsequent meetings try to reuse the same slot index (time
        # consistency) and fall back to their first available position.
        for first_pos in slot_options_per_duration[0]:
            option: list[dict] = []
            valid = True

            for m_idx in range(num_meetings):
                day = days[m_idx]
                # Target: same slot index as the first meeting (time consistency).
                target_slot_idx = first_pos[0]

                positions = slot_options_per_duration[m_idx]

                # First try: exact same slot index as the first meeting.
                found = False
                for pos in positions:
                    if pos[0] == target_slot_idx:
                        option.append({
                            "day": day,
                            "start": pos[1],
                            "end": pos[2],
                            "slot_idx": pos[0],
                        })
                        found = True
                        break

                if not found:
                    # Fallback: use the earliest available position for this
                    # duration.  The scorer will penalise the time variance.
                    if positions:
                        pos = positions[0]
                        option.append({
                            "day": day,
                            "start": pos[1],
                            "end": pos[2],
                            "slot_idx": pos[0],
                        })
                    else:
                        valid = False
                        break

            if valid and len(option) == num_meetings:
                all_options.append(option)

    return all_options


def _to_min(t: str) -> int:
    """Convert an "HH:MM" string to total minutes since midnight.

    This is a local helper identical in behaviour to ``_to_minutes`` from
    ``timetable_workspace``, duplicated here to avoid an extra import in
    the hot scoring loop.
    """
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _score_option(
    option: list[dict],
    same_group_masks: list[tuple[str, int]],
    course_students: dict[str, set[int]],
    my_students: set[int],
    my_code: str = "",
    same_group_schedule: list[tuple[str, str, str]] | None = None,
    other_sections_masks: list[tuple[str, int]] | None = None,
    is_online: bool = False,
    online_codes_in_group: set[str] | None = None,
) -> tuple[int, int, int, int, int]:
    """Score a meeting option. Lower is better.

    Returns (hard_conflict, same_course_overlap, student_gap, instructor_spread, time_variance).

    - hard_conflict: direct time overlap with another course in the same student group
    - same_course_overlap: overlap with OTHER sections of the SAME course (instructor can't teach both)
    - student_gap: idle time between on-campus classes (online courses excluded from gap calc)
    - instructor_spread: for online courses, penalize early slots (prefer late/last slot)
    - time_variance: penalty for different slot indices across days
    """
    _online_in_group = online_codes_in_group or set()
    total_mask = 0
    for m in option:
        total_mask |= _time_mask(m["day"], m["start"], m["end"])

    # (1) Hard conflict: time overlap with same student group
    hard_conflict = 0
    for placed_code, placed_mask in same_group_masks:
        if total_mask & placed_mask:
            hard_conflict += 1

    # (2) Same-course overlap: can't teach two sections at the same time
    same_course_overlap = 0
    if other_sections_masks:
        for other_code, other_mask in other_sections_masks:
            if other_code == my_code and (total_mask & other_mask):
                same_course_overlap += 1

    # (3) Student gap: idle time between ON-CAMPUS classes (online excluded)
    # Online courses don't require campus presence, so gaps before/after them don't count
    day_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    if same_group_schedule:
        for d, s, e in same_group_schedule:
            # Only include non-online courses in gap calculation
            # We don't have per-entry code tracking in schedule, so include all
            # (the online course itself will be excluded when it's the one being placed)
            day_intervals[d].append((_to_min(s), _to_min(e)))

    if not is_online:
        # This is an on-campus course — add it and measure gaps against other on-campus
        for m in option:
            day_intervals[m["day"]].append((_to_min(m["start"]), _to_min(m["end"])))

    student_gap = 0
    for day, intervals in day_intervals.items():
        if len(intervals) >= 2:
            intervals.sort()
            for i in range(len(intervals) - 1):
                idle = intervals[i + 1][0] - intervals[i][1]
                if idle > 0:
                    student_gap += idle

    # (4) Online preference: late slots. For online courses, penalize early time slots.
    # Prefer the LAST available slot so students can leave campus first.
    instructor_spread = 0
    if is_online:
        # Invert slot index: lower slot_idx = higher penalty (earlier = bad for online)
        # Max slot_idx is best (latest slot)
        for m in option:
            instructor_spread += (10 - m["slot_idx"])  # lower idx = higher penalty

    # (5) Time consistency (prefer same slot index across days)
    slot_indices = [m["slot_idx"] for m in option]
    time_variance = len(set(slot_indices)) - 1

    return hard_conflict, same_course_overlap, student_gap, instructor_spread, time_variance


def auto_place_board(board_id: int) -> dict:
    """Auto-place sections on a board using greedy conflict minimization.

    Algorithm:
    1. Get courses for this board from the budget, sorted by demand (highest first)
    2. Build student overlap data from ScenarioStudentMap
    3. For each course × each section to place:
       a. Generate all valid meeting options (respecting day/pattern rules)
       b. Score each option by (student_conflicts, time_variance)
       c. Pick the option with minimum score
       d. Create TermSection + TermSectionMeeting + SectionPlacement
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"placed": 0, "skipped": 0, "placements": []}

    scenario = board.scenario
    slot_config = scenario.slot_config if scenario.slot_config else DEFAULT_SLOTS

    # Get courses for this board's term level
    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario,
            programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )

    if not budgets:
        return {"placed": 0, "skipped": 0, "placements": []}

    # Build student-course overlap
    student_maps = ScenarioStudentMap.objects.filter(scenario=scenario)
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    # Track placed masks per section group number
    # group_masks[1] = masks for all S1 sections, group_masks[2] = all S2, etc.
    # Sections in the same group MUST NOT overlap (same student cohort)
    # Sections in different groups CAN overlap (different student cohorts)
    group_masks: dict[int, list[tuple[str, int]]] = defaultdict(list)
    group_schedule: dict[int, list[tuple[str, str, str]]] = defaultdict(list)  # sec_idx -> [(day,start,end)]
    all_placed_masks: list[tuple[str, int]] = []  # all sections, all groups
    placement_results: list[dict] = []
    total_placed = 0
    total_skipped = 0

    # Load online course flags
    from core.models import ProgrammeRequirement as PR
    online_codes: set[str] = set()
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_qs = PR.objects.filter(program__in=programs, is_online=True).values_list(
            "course_code", flat=True
        )
        online_codes = {c.strip().upper() for c in online_qs}

    # Pre-compute per-course data
    course_data: list[dict] = []
    for budget in budgets:
        code = budget.course_code
        credit_hours = budget.credit_hours or 3
        pattern = get_meeting_pattern(credit_hours)
        already = SectionPlacement.objects.filter(
            board=board, term_section__course_code=code
        ).count()
        to_place = max(0, budget.planned_sections - already)
        if to_place == 0:
            continue
        all_options = _generate_meeting_options(pattern, slot_config)
        if not all_options:
            total_skipped += to_place
            continue
        course_data.append({
            "code": code,
            "budget": budget,
            "credit_hours": credit_hours,
            "pattern": pattern,
            "already": already,
            "to_place": to_place,
            "all_options": all_options,
            "students": course_students.get(code, set()),
            "is_online": code.upper() in online_codes,
        })

    # Place section-by-section across ALL courses: all S1s first, then all S2s, etc.
    # This ensures S1 of different courses don't overlap (same student group 1),
    # S2 of different courses don't overlap (same student group 2), etc.
    max_sections_needed = max((cd["to_place"] for cd in course_data), default=0)

    for sec_round in range(1, max_sections_needed + 1):
        # Group 1 (S1) = regular students taking full courses → BEST timetable, zero gaps
        # Group 2+ = overflow → gaps acceptable
        # Weight gap penalty higher for earlier groups
        gap_weight = max(1, 11 - sec_round)  # S1=10x, S2=9x, S3=8x, ...

        for cd in course_data:
            sec_idx = cd["already"] + sec_round
            if sec_round > cd["to_place"]:
                continue

            code = cd["code"]
            sec_label = f"S{sec_idx}"
            my_students = cd["students"]
            all_options = cd["all_options"]

            # Score against SAME GROUP's masks (same student cohort = must not overlap)
            same_group = group_masks.get(sec_idx, [])
            same_sched = group_schedule.get(sec_idx)

            best_score = (float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
            best_option = None

            is_online = cd.get("is_online", False)

            for option in all_options:
                raw_score = _score_option(
                    option, same_group, course_students, my_students,
                    my_code=code,
                    same_group_schedule=same_sched,
                    other_sections_masks=all_placed_masks,
                    is_online=is_online,
                )
                # Weight gap by group priority: S1 gets 10x gap penalty
                score = (raw_score[0], raw_score[1], raw_score[2] * gap_weight, raw_score[3], raw_score[4])
                if score < best_score:
                    best_score = score
                    best_option = option

            if best_option is None:
                total_skipped += 1
                continue

            # Create TermSection
            ts, _ = TermSection.objects.get_or_create(
                course_key=code,
                section=sec_label,
                defaults={
                    "course_code": code,
                    "course_number": code,
                    "course_name": code,
                    "available_capacity": cd["budget"].max_per_section,
                    "source_tag": "tw_auto",
                },
            )

            meeting_results = []
            for m in best_option:
                TermSectionMeeting.objects.get_or_create(
                    term_section=ts,
                    day=m["day"],
                    start_time=m["start"],
                    end_time=m["end"],
                    defaults={"room": "", "instructor": ""},
                )
                SectionPlacement.objects.get_or_create(
                    board=board,
                    term_section=ts,
                    day=m["day"],
                    start_time=m["start"],
                    defaults={"end_time": m["end"]},
                )
                mask = _time_mask(m["day"], m["start"], m["end"])
                group_masks[sec_idx].append((code, mask))
                group_schedule[sec_idx].append((m["day"], m["start"], m["end"]))
                all_placed_masks.append((code, mask))
                meeting_results.append({"day": m["day"], "start": m["start"], "end": m["end"]})

            total_placed += 1
            placement_results.append({
                "course_code": code,
                "section": sec_label,
                "credit_hours": cd["credit_hours"],
                "meetings": meeting_results,
                "conflict_score": best_score[0],
            })

    return {
        "placed": total_placed,
        "skipped": total_skipped,
        "placements": placement_results,
    }


def auto_place_scenario(scenario_id: int) -> dict:
    """Auto-place sections on ALL boards in a scenario."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    total_skipped = 0
    for board in boards:
        r = auto_place_board(board.id)
        results[board.label] = r
        total_placed += r["placed"]
        total_skipped += r["skipped"]
    return {
        "boards": results,
        "total_placed": total_placed,
        "total_skipped": total_skipped,
    }
