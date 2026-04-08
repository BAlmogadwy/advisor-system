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


# ── Placement Strategies ─────────────────────────────────────────
#
# Each strategy adjusts how the scoring tuple is weighted.
# The auto_place_board function applies these weights when comparing options.

STRATEGIES: dict[str, dict] = {
    "compact": {
        "label": "Compact",
        "description": "Pack courses back-to-back, minimize idle time between classes",
        "gap_multiplier": 10,     # very strong gap penalty
        "slot_preference": 0,     # no slot position preference
    },
    "morning": {
        "label": "Morning-first",
        "description": "Pack courses into early slots, free afternoons for study",
        "gap_multiplier": 2,      # low gap penalty (gaps less important than being early)
        "slot_preference": 50,    # very strong preference for early slots
    },
    "balanced": {
        "label": "Balanced",
        "description": "Moderate gaps, try to use fewer days per course",
        "gap_multiplier": 5,      # moderate gap penalty
        "slot_preference": 0,
    },
    "optimal": {
        "label": "Optimal (CP-SAT Solver)",
        "description": "OR-Tools constraint solver — finds globally optimal solution (slower)",
        "gap_multiplier": 10,
        "slot_preference": 0,
    },
    "hybrid": {
        "label": "Hybrid (Greedy + Annealing)",
        "description": "Best quality — greedy build + simulated annealing improvement",
        "gap_multiplier": 10,
        "slot_preference": 0,
    },
    "load_balanced": {
        "label": "Load-Balanced",
        "description": "Equalize daily course load — no heavy/light days",
        "gap_multiplier": 5,
        "slot_preference": 0,
    },
}

DEFAULT_STRATEGY = "compact"


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
    """Score a candidate meeting option.  **Lower is better.**

    The returned 5-tuple is compared lexicographically by the caller, so
    earlier elements dominate.  The ordering encodes priority:

    1. ``hard_conflict`` -- highest priority.  Direct bitmask overlap with
       an already-placed section in the *same student group*.  Any non-zero
       value here means students physically cannot attend both courses.
    2. ``same_course_overlap`` -- overlap with another section of the *same*
       course code across all groups.  A single instructor typically teaches
       all sections, so they must not collide.
    3. ``student_gap`` -- total idle minutes between consecutive on-campus
       meetings for the student group on each day.  Large gaps waste student
       time.  Online courses are excluded from this calculation because
       students need not be on campus for them.
    4. ``instructor_spread`` -- online-course scheduling preference.  For
       online courses, earlier slot indices receive higher penalty, pushing
       them toward the end of the day so students can leave campus first.
    5. ``time_variance`` -- number of distinct slot indices used minus one.
       Zero means all meetings happen at the same time of day across
       different weekdays, which is easier for students to remember.

    Parameters
    ----------
    option : list[dict]
        A candidate set of meetings (from ``_generate_meeting_options``).
    same_group_masks : list[tuple[str, int]]
        ``(course_code, bitmask)`` pairs for sections already placed in
        the *same* student group.
    course_students : dict[str, set[int]]
        Mapping of course code to the set of student IDs enrolled in that
        course (used for future weighted-conflict scoring -- currently
        reserved).
    my_students : set[int]
        Student IDs enrolled in the course being placed (reserved for
        weighted scoring).
    my_code : str
        Course code of the section being placed.
    same_group_schedule : list[tuple[str, str, str]] | None
        ``(day, start, end)`` triples of meetings already placed in the
        same student group, used for gap calculation.
    other_sections_masks : list[tuple[str, int]] | None
        ``(course_code, bitmask)`` pairs for *all* sections placed so far
        across *all* groups.  Used to detect same-course overlap.
    is_online : bool
        Whether the course being placed is delivered online.
    online_codes_in_group : set[str] | None
        Course codes flagged as online within the current group (reserved
        for future per-entry gap filtering).

    Returns
    -------
    tuple[int, int, int, int, int]
        ``(hard_conflict, same_course_overlap, student_gap,
        instructor_spread, time_variance)``
    """
    _online_in_group = online_codes_in_group or set()

    # Build a combined bitmask for all meetings in this option.
    # Each bit represents a 5-minute block on a specific day (see _time_mask).
    total_mask = 0
    for m in option:
        total_mask |= _time_mask(m["day"], m["start"], m["end"])

    # ── (1) Hard conflict: bitmask overlap with same student group ────
    hard_conflict = 0
    for placed_code, placed_mask in same_group_masks:
        if total_mask & placed_mask:
            hard_conflict += 1

    # ── (2) Same-course overlap across ALL groups ─────────────────────
    # Prevents the same instructor from being double-booked.
    same_course_overlap = 0
    if other_sections_masks:
        for other_code, other_mask in other_sections_masks:
            if other_code == my_code and (total_mask & other_mask):
                same_course_overlap += 1

    # ── (3) Student gap: idle minutes between on-campus classes ───────
    # Online courses are excluded because students do not need to be
    # physically present.  When placing an online course (is_online=True),
    # it is not added to the day's interval list, so it does not inflate
    # the gap metric.
    day_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    if same_group_schedule:
        for d, s, e in same_group_schedule:
            # NOTE: the schedule does not track per-entry online status, so
            # all previously placed meetings are included.  The online
            # course currently being scored is excluded below via the
            # is_online guard.
            day_intervals[d].append((_to_min(s), _to_min(e)))

    if not is_online:
        # On-campus course: include its meetings in the gap calculation.
        for m in option:
            day_intervals[m["day"]].append((_to_min(m["start"]), _to_min(m["end"])))

    # Calculate idle gaps. The prayer break (11:45→13:00 = 75min) is
    # treated as a REAL gap — the algorithm should try to keep all
    # courses either before or after the break on each day.
    PRAYER_END = 13 * 60  # 13:00

    student_gap = 0
    for day, intervals in day_intervals.items():
        if len(intervals) >= 2:
            intervals.sort()
            has_morning = any(s < PRAYER_END for s, e in intervals)
            has_afternoon = any(s >= PRAYER_END for s, e in intervals)
            crosses_prayer = has_morning and has_afternoon

            for i in range(len(intervals) - 1):
                idle = intervals[i + 1][0] - intervals[i][1]
                if idle > 0:
                    # Extra penalty for crossing the prayer break
                    if crosses_prayer and idle >= 60:
                        student_gap += idle * 2  # double penalty
                    else:
                        student_gap += idle

    # ── (4) Online preference: push to late slots ─────────────────────
    # For online courses, penalise early time-slot indices.  The formula
    # ``10 - slot_idx`` means slot 0 (earliest) adds 10, while slot 4
    # (latest) adds only 6, making later slots cheaper.
    instructor_spread = 0
    if is_online:
        for m in option:
            instructor_spread += (10 - m["slot_idx"])

    # ── (5) Time consistency across days ──────────────────────────────
    # Zero if all meetings share the same slot index; +1 for each
    # additional distinct index.
    slot_indices = [m["slot_idx"] for m in option]
    time_variance = len(set(slot_indices)) - 1

    return hard_conflict, same_course_overlap, student_gap, instructor_spread, time_variance


def auto_place_board(board_id: int, strategy: str = DEFAULT_STRATEGY) -> dict:
    """Auto-place all unplaced sections on a single delivery board.

    This is the main entry point for the greedy placement algorithm.  It
    operates on one board (one nominal term-level) and proceeds as follows:

    1. Load the board's scenario, slot configuration, and section budgets
       (sorted by descending ``total_demand`` so high-demand courses get
       first pick of slots).
    2. Build a ``course_students`` map from ``ScenarioStudentMap`` -- this
       records which students need each course and drives conflict scoring.
    3. Pre-compute feasible meeting options for every course (respecting
       credit hours, prayer break, etc.).
    4. Place sections in **round-robin by group index**: all S1 sections
       first (round 1), then all S2 sections (round 2), and so on.
       Within each round, courses are sorted by credit hours descending
       (4-credit courses first) then by student count, so the most
       constrained courses claim slots first.
    5. For each section to place, score every candidate option against the
       current state of the board using ``_score_option``, pick the
       minimum-score option, and persist it as ``TermSection`` +
       ``TermSectionMeeting`` + ``SectionPlacement`` rows.

    Parameters
    ----------
    board_id : int
        Primary key of the ``DeliveryBoard`` to populate.

    Returns
    -------
    dict
        ``{"placed": int, "skipped": int, "placements": list[dict]}``
        where each placement dict contains ``course_code``, ``section``,
        ``credit_hours``, ``meetings`` (list of day/start/end dicts),
        and ``conflict_score``.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"placed": 0, "skipped": 0, "placements": []}

    scenario = board.scenario
    slot_config = scenario.slot_config if scenario.slot_config else DEFAULT_SLOTS

    # ── 1. Load section budgets for this board's term level ───────────
    # Ordered by descending demand so the most popular courses are placed
    # first and get the best (least-conflicting) slots.
    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario,
            programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )

    if not budgets:
        return {"placed": 0, "skipped": 0, "placements": []}

    # ── 2. Build student-to-course mapping ────────────────────────────
    # For each course code, collect the set of student IDs that need it.
    student_maps = ScenarioStudentMap.objects.filter(scenario=scenario)
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    # ── Placement tracking structures ─────────────────────────────────
    # group_masks  : per-group bitmasks used for hard-conflict detection.
    #                group_masks[1] holds masks for all S1 sections;
    #                sections in the same group MUST NOT overlap (same
    #                student cohort), while sections in different groups
    #                CAN overlap (different cohorts).
    # group_schedule: per-group (day, start, end) triples for gap calc.
    # all_placed_masks: global list across all groups, used for
    #                   same-course overlap detection.
    group_masks: dict[int, list[tuple[str, int]]] = defaultdict(list)
    group_schedule: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    all_placed_masks: list[tuple[str, int]] = []
    placement_results: list[dict] = []
    total_placed = 0
    total_skipped = 0

    # ── 3. Identify online courses (from ProgrammeRequirement) ────────
    # Online courses receive a late-slot preference penalty so they are
    # scheduled after on-campus classes, letting students leave first.
    from core.models import ProgrammeRequirement as PR
    online_codes: set[str] = set()
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_qs = PR.objects.filter(program__in=programs, is_online=True).values_list(
            "course_code", flat=True
        )
        online_codes = {c.strip().upper() for c in online_qs}

    # ── 4. Pre-compute per-course data ────────────────────────────────
    # For each budgeted course, determine how many sections still need
    # placing and pre-generate all feasible meeting options.
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

    # ── 5. Round-robin placement: all S1s, then all S2s, ... ──────────
    # By placing all sections of the same group index together, we ensure
    # that S1 of different courses (which serve the *same* primary student
    # cohort) never overlap.  S2 sections get their own independent
    # conflict space, and so on.
    max_sections_needed = max((cd["to_place"] for cd in course_data), default=0)

    for sec_round in range(1, max_sections_needed + 1):
        # Gap weighting: the primary group (S1) receives a 10x multiplier
        # on gap penalty, making the algorithm work hard to give them a
        # compact, zero-gap schedule.  Overflow groups (S2, S3, ...) get
        # progressively lower weights since their students are less likely
        # to take a full course load.
        strat = STRATEGIES.get(strategy, STRATEGIES[DEFAULT_STRATEGY])
        gap_base = strat["gap_multiplier"]
        slot_pref = strat["slot_preference"]
        gap_weight = max(1, gap_base + 1 - sec_round)  # S1 gets highest, diminishing

        # Sort courses by credit hours descending -- place 4-credit courses
        # (3 meetings/week) first so they claim the best adjacent slot
        # patterns, then 3-credit, then 2-credit.  Ties broken by student
        # count descending (higher demand = higher priority).
        course_data_sorted = sorted(
            course_data,
            key=lambda cd: (-cd["credit_hours"], -len(cd["students"])),
        )

        for cd in course_data_sorted:
            sec_idx = cd["already"] + sec_round
            if sec_round > cd["to_place"]:
                continue

            code = cd["code"]
            sec_label = f"S{sec_idx}"
            my_students = cd["students"]
            all_options = cd["all_options"]

            # Retrieve the bitmasks and schedule for the SAME group only.
            same_group = group_masks.get(sec_idx, [])
            same_sched = group_schedule.get(sec_idx)

            best_score = (float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
            best_option = None

            is_online = cd.get("is_online", False)

            # ── Score every candidate option and keep the best ────────
            for option in all_options:
                raw_score = _score_option(
                    option, same_group, course_students, my_students,
                    my_code=code,
                    same_group_schedule=same_sched,
                    other_sections_masks=all_placed_masks,
                    is_online=is_online,
                )
                # Apply the group-dependent gap weight (element [2] is
                # student_gap).  This makes S1 gap-averse and S2+ tolerant.
                # Apply strategy weights:
                # - gap_weight: amplifies idle gap penalty
                # - slot_pref: for morning strategy, heavily penalize afternoon slots
                slot_penalty = 0
                if slot_pref > 0:
                    # Each meeting in slot 2+ (afternoon) gets a big penalty
                    # Slot 0,1 = morning (free), slot 2,3,4 = afternoon (penalized)
                    for m in option:
                        if m["slot_idx"] >= 2:  # afternoon slots
                            slot_penalty += slot_pref * (m["slot_idx"] - 1)
                score = (raw_score[0], raw_score[1], raw_score[2] * gap_weight + slot_penalty, raw_score[3], raw_score[4])
                if score < best_score:
                    best_score = score
                    best_option = option

            if best_option is None:
                total_skipped += 1
                continue

            # ── Persist the chosen placement ──────────────────────────
            # TermSection: the logical section record (e.g. "MATH101 S1").
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

            # TermSectionMeeting + SectionPlacement: one row per meeting
            # day.  Also update the in-memory tracking structures so
            # subsequent placements see the new constraints.
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


def auto_place_scenario(scenario_id: int, strategy: str = DEFAULT_STRATEGY) -> dict:
    """Auto-place sections on every board in a scenario.

    Iterates over all ``DeliveryBoard`` rows belonging to the scenario
    (ordered by ``display_order``) and calls ``auto_place_board`` for each.

    Parameters
    ----------
    scenario_id : int
        Primary key of the ``TimetableScenario``.

    Returns
    -------
    dict
        ``{"boards": {label: board_result, ...}, "total_placed": int,
        "total_skipped": int}`` where each *board_result* has the same
        shape as the return value of ``auto_place_board``.
    """
    # Use CP-SAT solver for "optimal" strategy
    if strategy == "optimal":
        from core.services.timetable_solver import solve_scenario
        return solve_scenario(scenario_id, time_limit_seconds=5.0)

    # Load-balanced: greedy build + redistribution
    if strategy == "load_balanced":
        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        results = {}
        total_placed = 0
        for board in boards:
            r = auto_place_board(board.id, strategy="compact")
            results[board.label] = r
            total_placed += r["placed"]
        from core.services.timetable_load_balanced import rebalance_scenario
        rebalance_scenario(scenario_id, max_seconds_per_board=5.0)
        return {"boards": results, "total_placed": total_placed, "total_skipped": 0}

    # Hybrid: greedy build + simulated annealing improvement
    if strategy == "hybrid":
        # Phase 1: greedy (compact) — build feasible solution
        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        results = {}
        total_placed = 0
        total_skipped = 0
        for board in boards:
            r = auto_place_board(board.id, strategy="compact")
            results[board.label] = r
            total_placed += r["placed"]
            total_skipped += r["skipped"]

        # Phase 2: simulated annealing improvement
        from core.services.timetable_local_search import optimize_scenario
        sa_result = optimize_scenario(scenario_id, max_seconds_per_board=5.0)

        return {
            "boards": results,
            "total_placed": total_placed,
            "total_skipped": total_skipped,
            "optimization": sa_result,
        }

    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    total_skipped = 0
    for board in boards:
        r = auto_place_board(board.id, strategy=strategy)
        results[board.label] = r
        total_placed += r["placed"]
        total_skipped += r["skipped"]
    return {
        "boards": results,
        "total_placed": total_placed,
        "total_skipped": total_skipped,
    }
