"""
core/services/timetable_autoplace.py
Auto-placement algorithm for the Timetable Workspace feature.

This module implements a greedy constraint-satisfaction algorithm that assigns
university course sections to weekly time slots on delivery boards.  Each
board represents a single nominal term-level (e.g. "Level 3") and belongs to
a TimetableScenario.

The algorithm's primary goal is to **minimise student scheduling conflicts**
using a real student-overlap matrix (built from canonical course requests) rather
than fake cohort groupings.  Cross-course conflicts are soft penalties
proportional to shared student count; same-course overlap is hard.

Key concepts
------------
* **Delivery board** -- a grid of days x slots for one term level.
* **Overlap matrix** -- maps course pairs to their shared student count,
  built from canonical scenario course requests. Drives all conflict scoring.
* **Meeting pattern** -- how many weekly meetings a course needs, and the
  duration of each, determined by credit hours.

Meeting patterns based on credit hours
---------------------------------------
  4 credits --> 3 meetings/week (75 min, 75 min, 100 min) on 3 different days
  3 credits --> 2 meetings/week (75 min, 75 min) on 2 different days
  2 credits --> 1 meeting/week (100 min)
  1 credit  --> 1 meeting/week (75 min)  [fallback]

Hard constraints (violations are never accepted)
-------------------------------------------------
  - No more than 1 meeting per day per section.
  - Same-course sections must not overlap (instructor double-booking).
  - Locked placements are honoured (when ``TIMETABLE_ENFORCE_LOCKS`` is on).
  - Blocked slots (scenario ``blocked_slots``) are never used.

  Prayer compliance is guaranteed at the slot-grid level, not per meeting:
  the fixed slot grids never start a lecture in 11:30-12:59 or a lab in
  11:10-12:59 (see ``timetable_validation.assert_slot_grid_prayer_compliant``),
  so no runtime per-meeting prayer rule is required.

Soft constraints (penalised but not forbidden)
----------------------------------------------
  - Cross-course student overlap: penalty proportional to shared student count.
  - Same-course sections should form a back-to-back pair; for 2 sections,
    the second section is strongly pushed beside the first, and for 3+
    sections at least one pair should be consecutive.
  - Prefer the same time-slot index across all meeting days for one course.
  - Minimise idle gaps between on-campus classes for courses sharing students.
  - Online courses should be placed in late slots.
  - Slot density: prefer less-populated time slots for better distribution.

Placement order
---------------
Sections are placed in *round-robin by section index*: all S1 sections first,
then all S2 sections, etc.  Within a round, courses are processed in
descending demand order (highest ``total_demand`` first).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from itertools import combinations

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_decision_trace import (
    INSTRUCTOR_CLASH,
    SAME_COURSE_INSTRUCTOR_CLASH,
    STUDENT_CONFLICT,
    Alternative,
    DecisionTrace,
    is_decision_trace_enabled,
)
from core.services.timetable_demand import (
    compute_course_students_index,
    load_scenario_course_demands,
)
from core.services.timetable_lab_predicate import (
    is_lab_heuristic_unified,
    meeting_requires_lab_room,
)
from core.services.timetable_online import OnlineCourseLookup, normalise_course_code
from core.services.timetable_pr4_instructor import (
    is_instructor_clash_enabled,
    normalise_instructor,
)
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    ROOM_BUFFER_REJECT,
    ROOM_OCCUPIED,
    RoomFailureReason,
    check_capacity_feasibility,
    check_gender_feasibility,
    check_occupancy,
    check_type_feasibility,
    is_room_oracle_enabled,
    room_failure_breakdown,
)
from core.services.timetable_same_course import (
    first_overlapping_same_course_window,
    make_meeting_window,
    same_course_candidate_penalty,
)
from core.services.timetable_stage_telemetry import (
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
    merge_stage_telemetry,
    record_stage_iterations,
    record_stage_ms,
)
from core.services.timetable_validation import (
    LOCK_RESPECT,
    RejectionReason,
    is_lock_enforcement_enabled,
)
from core.services.timetable_warm_start import (
    BaselinePlacement,
    apply_warm_start,
    compute_perturbation_metric,
    is_warm_start_enabled,
)
from core.services.timetable_workspace import _time_mask

logger = logging.getLogger(__name__)

# ── Meeting Patterns ─────────────────────────────────────────────
# The Saudi academic week runs Sunday through Thursday.
WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]

# Maps credit hours to a list of meeting durations (minutes per meeting).
# The length of the list determines how many meetings per week.
MEETING_PATTERNS: dict[int, list[int]] = {
    4: [75, 75, 100],  # 3 meetings: two 75 min + one 100 min
    3: [75, 75],  # 2 meetings: two 75 min
    2: [100],  # 1 meeting: 100 min
    1: [75],  # 1 meeting: 75 min (fallback for rare 1-credit courses)
}

# Optional order variants for structurally identical families.
# For 4-credit mixed lecture/lab courses, permitting the 100-minute block to
# occur in any weekly position gives the planner and local-search layers more
# flexibility for lab utilisation without changing total contact hours.
MEETING_PATTERN_VARIANTS: dict[int, list[list[int]]] = {
    4: [[100, 75, 75], [75, 100, 75], [75, 75, 100]],
    3: [[75, 75]],
    2: [[100]],
    1: [[75]],
}


def get_meeting_pattern_variants(credit_hours: int) -> list[list[int]]:
    """Return all valid duration-order variants for a course.

    The primary use case is 4-credit mixed lecture/lab courses, where the
    100-minute block may appear in any weekly position: ``[100,75,75]``,
    ``[75,100,75]``, or ``[75,75,100]``.
    """
    return [
        list(v)
        for v in MEETING_PATTERN_VARIANTS.get(credit_hours, [get_meeting_pattern(credit_hours)])
    ]


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
# 13:00 for the midday break.  A scenario may override these via its
# ``slot_config`` JSON field.  The grid is prayer-compliant by construction:
# no lecture slot starts in 11:30-12:59 (see timetable_validation).

DEFAULT_SLOTS = [
    {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
    {"label": "10:30-11:45", "start": "10:30", "end": "11:45"},
    # -- midday break gap: no lecture slot starts in 11:30-12:59 --
    {"label": "13:00-14:15", "start": "13:00", "end": "14:15"},
    {"label": "14:30-15:45", "start": "14:30", "end": "15:45"},
    {"label": "16:00-17:15", "start": "16:00", "end": "17:15"},
]

# Lab slots: 100-minute dedicated time grid (separate from lecture slots).
DEFAULT_LAB_SLOTS = [
    {"label": "Lab 1", "start": "09:00", "end": "10:40"},
    {"label": "Lab 2", "start": "10:45", "end": "12:25"},
    {"label": "Lab 3", "start": "13:00", "end": "14:40"},
    {"label": "Lab 4", "start": "14:45", "end": "16:25"},
    {"label": "Lab 5", "start": "16:30", "end": "18:10"},
]


def _get_slots(slot_config: list[dict]) -> list[dict]:
    """Return *slot_config* if non-empty, otherwise fall back to DEFAULT_SLOTS."""
    return slot_config if slot_config else DEFAULT_SLOTS


# ── Auto-Placement ───────────────────────────────────────────────


def _generate_meeting_options(
    pattern: list[int],
    slot_config: list[dict],
    lab_slot_config: list[dict] | None = None,
    blocked_slots: list[dict] | None = None,
) -> list[list[dict]]:
    """Generate every valid way to schedule a course's weekly meetings.

    The function enumerates combinations of (day, slot) assignments that
    satisfy the hard constraints (one meeting per day, blocked-slot
    avoidance) and produces a list of *candidate options* for the scorer
    to rank.

    Parameters
    ----------
    pattern : list[int]
        Per-meeting durations in minutes, e.g. ``[75, 75]`` for a 3-credit
        course or ``[75, 75, 100]`` for a 4-credit course with lab.
    slot_config : list[dict]
        Lecture slot definitions from the scenario, or ``DEFAULT_SLOTS``.
    lab_slot_config : list[dict] | None
        Lab (100-min) slot definitions. If ``None``, uses ``DEFAULT_LAB_SLOTS``.

    Returns
    -------
    list[list[dict]]
        Each element is one complete option -- a list of meeting dicts, one
        per meeting in *pattern*.  Each meeting dict has keys:
        ``day`` (str), ``start`` (str "HH:MM"), ``end`` (str "HH:MM"),
        ``slot_idx`` (int -- the 0-based position in the relevant slot list),
        ``is_lab`` (bool -- True for 100-min lab meetings).

    Notes
    -----
    * 75 min meetings use the lecture slot grid.
    * 100 min meetings use the dedicated lab slot grid (separate time slots).
    * Labs must be on a different day than lectures of the same course.
    """
    slots = _get_slots(slot_config)
    lab_slots = _get_slots(lab_slot_config) if lab_slot_config else DEFAULT_LAB_SLOTS
    num_meetings = len(pattern)

    # All ways to pick *num_meetings* distinct days from the 5-day week.
    # Sort so combos with at least 1 gap day between meetings come first.
    # E.g. (SUN,TUE) before (SUN,MON) — avoids consecutive-day meetings.
    _DAY_IDX = {d: i for i, d in enumerate(WEEKDAYS)}

    def _day_spacing_score(combo: tuple[str, ...]) -> int:
        """Lower = better spacing. 0 = all gaps ≥ 2 days. Penalise consecutive."""
        indices = [_DAY_IDX[d] for d in combo]
        indices.sort()
        penalty = 0
        for j in range(len(indices) - 1):
            gap = indices[j + 1] - indices[j]
            if gap == 1:
                penalty += 10  # consecutive days — heavy penalty
            elif gap == 2:
                penalty += 0  # 1 day gap — ideal
            # gap >= 3 is also fine
        return penalty

    day_combos = sorted(combinations(WEEKDAYS, num_meetings), key=_day_spacing_score)

    # Build set of blocked (day, start) pairs for fast lookup
    _blocked_set: set[tuple[str, str]] = set()
    if blocked_slots:
        for bs in blocked_slots:
            _blocked_set.add((bs.get("day", ""), bs.get("start", "")))

    # For each meeting duration, pre-compute which (slot_idx, start, end)
    # positions are feasible. Prayer compliance is a property of the slot
    # grid (see ``timetable_validation.assert_slot_grid_prayer_compliant``),
    # not a per-candidate runtime filter.
    lecture_positions = [(i, s["start"], s["end"]) for i, s in enumerate(slots)]
    lab_positions = [(i, s["start"], s["end"]) for i, s in enumerate(lab_slots)]

    # Generate all unique permutations of the pattern so the lab can be
    # on any day (first, middle, or last meeting of the week).
    from itertools import permutations as _perms

    if len(set(pattern)) > 1:
        seen_pats: set[tuple[int, ...]] = set()
        pattern_variants = []
        for perm in _perms(pattern):
            if perm not in seen_pats:
                seen_pats.add(perm)
                pattern_variants.append(list(perm))
    else:
        pattern_variants = [pattern]

    all_options: list[list[dict]] = []

    for current_pattern in pattern_variants:
        slot_options_per_duration = [
            lab_positions if d > 75 else lecture_positions for d in current_pattern
        ]

        for days in day_combos:
            # Iterate over every feasible slot position for the *first* meeting;
            # subsequent meetings try to reuse the same slot index (time
            # consistency) and fall back to their first available position.
            for first_pos in slot_options_per_duration[0]:
                option: list[dict] = []
                valid = True

                for m_idx in range(num_meetings):
                    day = days[m_idx]
                    target_slot_idx = first_pos[0]
                    positions = slot_options_per_duration[m_idx]

                    found = False
                    for pos in positions:
                        if pos[0] == target_slot_idx and (day, pos[1]) not in _blocked_set:
                            option.append(
                                {"day": day, "start": pos[1], "end": pos[2], "slot_idx": pos[0]}
                            )
                            found = True
                            break

                    if not found:
                        fallback_found = False
                        for pos in positions:
                            if (day, pos[1]) not in _blocked_set:
                                option.append(
                                    {"day": day, "start": pos[1], "end": pos[2], "slot_idx": pos[0]}
                                )
                                fallback_found = True
                                break
                        if not fallback_found:
                            valid = False
                            break

                if valid and len(option) == num_meetings:
                    all_options.append(option)

    return all_options


def generate_meeting_options(
    pattern: list[int],
    slot_config: list[dict],
    lab_slot_config: list[dict] | None = None,
    blocked_slots: list[dict] | None = None,
) -> list[list[dict]]:
    """Public wrapper for generating complete weekly meeting patterns."""

    return _generate_meeting_options(pattern, slot_config, lab_slot_config, blocked_slots)


# ── Placement Strategies ─────────────────────────────────────────
#
# Each strategy adjusts how the scoring tuple is weighted.
# The auto_place_board function applies these weights when comparing options.

STRATEGIES: dict[str, dict] = {
    "compact": {
        "label": "Compact",
        "description": "Pack courses back-to-back, minimize idle time between classes",
        "gap_multiplier": 10,  # very strong gap penalty
        "slot_preference": 0,  # no slot position preference
    },
    "morning": {
        "label": "Morning-first",
        "description": "Pack courses into early slots, free afternoons for study",
        "gap_multiplier": 2,  # low gap penalty (gaps less important than being early)
        "slot_preference": 50,  # very strong preference for early slots
    },
    "balanced": {
        "label": "Balanced",
        "description": "Moderate gaps, try to use fewer days per course",
        "gap_multiplier": 5,  # moderate gap penalty
        "slot_preference": 0,
    },
    "optimal": {
        "label": "CP-SAT (per-board)",
        "description": "OR-Tools CP-SAT solve per board, time-limited — optimal within "
        "each board's budget, not a global optimum (slower)",
        "gap_multiplier": 10,
        "slot_preference": 0,
    },
    "hybrid": {
        "label": "Hybrid (Greedy + Annealing)",
        "description": "Greedy build + simulated-annealing improvement",
        "gap_multiplier": 10,
        "slot_preference": 0,
    },
    "load_balanced": {
        "label": "Load-Balanced",
        "description": "Equalize daily course load — no heavy/light days",
        "gap_multiplier": 5,
        "slot_preference": 0,
    },
    "adaptive": {
        "label": "Adaptive (Greedy + CP-SAT + SA)",
        "description": "Greedy baseline → per-board CP-SAT improvement → simulated-annealing polish",
        "gap_multiplier": 10,
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


def _placed_same_course_windows(
    placed_schedule: list[tuple[str, str, str, str]],
    my_code: str,
):
    return [
        make_meeting_window(my_code, entry[0], entry[1], entry[2])
        for entry in placed_schedule
        if len(entry) > 3 and entry[3] == my_code
    ]


def _option_same_course_windows(option: list[dict], my_code: str):
    return [
        make_meeting_window(my_code, meeting["day"], meeting["start"], meeting["end"])
        for meeting in option
    ]


def _same_course_overlap_context(
    option: list[dict],
    placed_schedule: list[tuple[str, str, str, str]],
    my_code: str,
) -> dict | None:
    """Return trace context if this option overlaps an existing same-course section."""
    overlap = first_overlapping_same_course_window(
        _option_same_course_windows(option, my_code),
        _placed_same_course_windows(placed_schedule, my_code),
    )
    if overlap is not None:
        _candidate, existing = overlap
        return {
            "clashing_section": my_code,
            "same_course": True,
            "existing_day": existing.day,
            "existing_start": f"{existing.start_min // 60:02d}:{existing.start_min % 60:02d}",
            "existing_end": f"{existing.end_min // 60:02d}:{existing.end_min % 60:02d}",
        }
    return None


def _same_course_back_to_back_penalty(
    option: list[dict],
    placed_schedule: list[tuple[str, str, str, str]],
    my_code: str,
) -> int:
    return same_course_candidate_penalty(
        _option_same_course_windows(option, my_code),
        _placed_same_course_windows(placed_schedule, my_code),
    )


def _score_option(
    option: list[dict],
    placed_masks: list[tuple[str, int]],
    course_students: dict[str, set[int]],
    my_students: set[int],
    my_code: str = "",
    placed_schedule: list[tuple[str, str, str, str]] | None = None,
    other_sections_masks: list[tuple[str, int]] | None = None,
    is_online: bool = False,
    online_codes_in_group: set[str] | None = None,
    overlap_matrix: dict | None = None,
) -> tuple[int, int, int, int, int, int]:
    """Score a candidate meeting option.  **Lower is better.**

    The returned 6-tuple is compared lexicographically by the caller, so
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
    6. ``student_overlap_penalty`` -- sum of shared-student counts across
       overlapping course pairs (semi-hard).  Returned last here; the
       caller (``auto_place_board``) re-weights it and reorders it into
       final-tuple position 3 so it dominates gap/density but not the
       same-course constraints.

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
    tuple[int, int, int, int, int, int]
        ``(hard_conflict, same_course_overlap, student_gap,
        instructor_spread, time_variance)``
    """
    _online_in_group = online_codes_in_group or set()

    # Build a combined bitmask for all meetings in this option.
    # Each bit represents a 5-minute block on a specific day (see _time_mask).
    total_mask = 0
    for m in option:
        total_mask |= _time_mask(m["day"], m["start"], m["end"])

    # ── (1) Cross-course student overlap: weighted soft penalty ────────
    # Accumulated into student_overlap_penalty and added to the gap bucket
    # (position 2) so it competes with gap/density/room on a level field.
    # NOT lexicographically dominant — a slightly higher overlap count
    # can be accepted if the gap/room/density picture is much better.
    from core.services.timetable_overlap import shared_student_count as _ssc_score

    hard_conflict = 0  # reserved for future use
    student_overlap_penalty = 0
    for placed_code, placed_mask in placed_masks:
        if total_mask & placed_mask:
            if placed_code == my_code:
                continue  # same course handled in (2) below
            shared = _ssc_score(overlap_matrix, my_code, placed_code) if overlap_matrix else 0
            if shared > 0:
                student_overlap_penalty += shared

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
    if placed_schedule:
        for entry in placed_schedule:
            d, s, e = entry[0], entry[1], entry[2]
            entry_code = entry[3] if len(entry) > 3 else ""
            # Only include gaps with courses that share students
            if (
                overlap_matrix
                and entry_code
                and _ssc_score(overlap_matrix, my_code, entry_code) == 0
            ):
                continue
            day_intervals[d].append((_to_min(s), _to_min(e)))

    if not is_online:
        # On-campus course: include its meetings in the gap calculation.
        for m in option:
            day_intervals[m["day"]].append((_to_min(m["start"]), _to_min(m["end"])))

    # Calculate idle gaps. The midday break (11:45→13:00 = 75min) is
    # treated as a REAL gap — the algorithm should try to keep all
    # courses either before or after the break on each day.
    MIDDAY_END = 13 * 60  # 13:00

    student_gap = 0
    for _day, intervals in day_intervals.items():
        if len(intervals) >= 2:
            intervals.sort()
            has_morning = any(s < MIDDAY_END for s, e in intervals)
            has_afternoon = any(s >= MIDDAY_END for s, e in intervals)
            crosses_midday = has_morning and has_afternoon

            for i in range(len(intervals) - 1):
                idle = intervals[i + 1][0] - intervals[i][1]
                if idle > 0:
                    # Extra penalty for crossing the midday break
                    if crosses_midday and idle >= 60:
                        student_gap += idle * 2  # double penalty
                    else:
                        student_gap += idle

    # ── (4) Slot position preference ───────────────────────────────────
    # Online courses: penalise early slots (push to late).
    # Non-online courses: penalise slot 5 (16:00-17:15) — students
    # prefer earlier classes.
    instructor_spread = 0
    if is_online:
        for m in option:
            instructor_spread += 10 - m["slot_idx"]
    else:
        for m in option:
            if m["slot_idx"] == 4:  # slot 5 (0-indexed as 4)
                instructor_spread += 5

    # ── (5) Time consistency across days ──────────────────────────────
    # Zero if all meetings share the same slot index; +1 for each
    # additional distinct index.
    slot_indices = [m["slot_idx"] for m in option]
    time_variance = len(set(slot_indices)) - 1

    return (
        hard_conflict,
        same_course_overlap,
        student_gap,
        instructor_spread,
        time_variance,
        student_overlap_penalty,
    )


def _classify_pr3_alternative(
    option: tuple[dict, ...],
    placed_schedule: list[tuple[str, str, str, str]],
    course_students: dict[str, set],
    my_students: set,
) -> tuple[str, dict] | None:
    """Return ``(rejection_code, context)`` for a candidate option or ``None``.

    Commit-3 classifier: inspects each meeting of the option against
    already-placed meetings and returns the first hard reason that
    explains why this option would be a worse placement than the winner.
    Only known-sentinel codes are emitted — acceptance bar #2.

    Covered today:
    - ``STUDENT_CONFLICT`` — at least one shared student between this
      option and a placed section at the same day+time.
    - ``ROOM_OCCUPIED`` — same day+time as a placed section (no shared
      students). Falls out of the assumption that rooming contention
      dominates when multiple sections want the same slot; the PR2 room
      oracle would refine this with ``ROOM_BUFFER_REJECT`` etc. but
      post-placement re-check would duplicate feasibility work.

    Not covered here (emitted elsewhere):
    - ``INSTRUCTOR_CLASH`` — PR4 commit 3 lands real emission in the
      candidate-scoring loop (flag-gated on
      ``TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED``). The loop accumulates
      clashes in ``pr4_instructor_rejected`` and surfaces them via
      ``_build_pr3_alternatives`` without going through this classifier.

    Returns ``None`` if no hard reason applies — the option was
    feasibility-clean and just lost on score. Those options are
    excluded from the trace to keep rejection codes meaningful.
    """
    from core.services.timetable_validation import _overlaps

    for m in option:
        for ps in placed_schedule:
            p_day, p_start, p_end, p_code = ps[:4]
            if p_day != m["day"]:
                continue
            if not _overlaps(m["start"], m["end"], p_start, p_end):
                continue
            # Same day + overlapping interval → hard clash.
            p_students = course_students.get(p_code, set())
            shared = my_students & p_students
            if shared:
                return STUDENT_CONFLICT, {
                    "clashing_section": p_code,
                    "shared_student_count": len(shared),
                }
            return ROOM_OCCUPIED, {"occupying_section": p_code}
    return None


def _normalise_baseline_map(
    baseline_placements: dict,
) -> dict[str, BaselinePlacement]:
    """Convert a caller-supplied baseline map into
    ``dict[str, BaselinePlacement]``.

    Accepts either dataclass instances or plain dicts
    (``{"day", "start_time", "end_time", ...}``) — fixture loaders and
    ad-hoc callers tend to pass dicts; production callers may pass the
    dataclass directly. Both shapes land in the same normalised form so
    the retention path below has one type to worry about.
    """
    out: dict[str, BaselinePlacement] = {}
    for section_code, entry in baseline_placements.items():
        if isinstance(entry, BaselinePlacement):
            out[section_code] = entry
            continue
        if not isinstance(entry, dict):
            continue
        if "|" in section_code:
            course_code, section = section_code.split("|", 1)
        else:
            course_code = section_code
            section = entry.get("section", "S1")
        # Day is uppercased to match ``WEEKDAYS`` ("SUN", "MON", …) that
        # ``_generate_meeting_options`` stamps into every candidate
        # option. Without this, fixture-style ``"Sun"`` / ``"Mon"`` never
        # pair with the option dicts and warm-start silently no-ops.
        out[section_code] = BaselinePlacement(
            course_code=course_code,
            section=section,
            day=str(entry.get("day", "")).upper(),
            start_time=str(entry.get("start_time", "")),
            end_time=str(entry.get("end_time", "")),
        )
    return out


def _build_pr3_alternatives(
    *,
    scored_options: list[tuple[tuple, tuple[dict, ...]]],
    winning_option: tuple[dict, ...],
    placed_schedule: list[tuple[str, str, str, str]],
    course_students: dict[str, set],
    my_students: set,
    my_code: str,
    meeting_results: list[dict],
    instructor_rejected: list[tuple[tuple[dict, ...], str, dict]] | None = None,
) -> list[Alternative]:
    """Build up to 3 ``Alternative`` entries for a placed section.

    Ordering rules (ChatGPT commit-3 ruling F1, extended in PR4 commit 3):
    - Instructor-clash rejected options come first — a "hard-filter,
      never scored" bucket surfaced ahead of scored losers.
    - Scored losers follow, ordered by ascending score (best-rejected
      first) — the option that would have scored next-best after the
      winner appears first.

    Capped at 3 total. The trace stops producing alternatives beyond
    that; this is a fixed cap per the DoR (not configurable).
    """
    alternatives: list[Alternative] = []
    for opt, code, context in (instructor_rejected or [])[:3]:
        if len(alternatives) >= 3:
            return alternatives
        first_m = opt[0]
        alternatives.append(
            Alternative(
                day=first_m["day"],
                start_time=first_m["start"],
                end_time=first_m["end"],
                room="",
                rejection_code=code,
                rejection_context=context,
            )
        )

    losers = sorted(
        (entry for entry in scored_options if entry[1] is not winning_option),
        key=lambda entry: entry[0],
    )
    for _score, opt in losers:
        if len(alternatives) >= 3:
            break
        classified = _classify_pr3_alternative(opt, placed_schedule, course_students, my_students)
        if classified is None:
            continue
        code, context = classified
        first_m = opt[0]
        alternatives.append(
            Alternative(
                day=first_m["day"],
                start_time=first_m["start"],
                end_time=first_m["end"],
                room="",
                rejection_code=code,
                rejection_context=context,
            )
        )

    # Avoid unused-parameter warnings — ``my_code`` and ``meeting_results``
    # are reserved for future classifier extensions (same-course overlap,
    # room-specific rejection refinement). Kept as keyword args so commit
    # 4/5 can extend without changing the signature.
    del my_code, meeting_results
    return alternatives


def auto_place_board(
    board_id: int,
    strategy: str = DEFAULT_STRATEGY,
    baseline_placements: dict | None = None,
) -> dict:
    """Auto-place all unplaced sections on a single delivery board.

    This is the main entry point for the greedy placement algorithm.  It
    operates on one board (one nominal term-level) and proceeds as follows:

    1. Load the board's scenario, slot configuration, and section budgets
       (sorted by descending ``total_demand`` so high-demand courses get
       first pick of slots).
    2. Build a ``course_students`` map from canonical course requests -- this
       records which students need each course and drives conflict scoring.
    3. Pre-compute feasible meeting options for every course (respecting
       credit hours, blocked slots, etc.).
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
    # PR3 commit 5 — normalise the caller-supplied baseline map into a
    # uniform ``dict[str, BaselinePlacement]`` keyed by ``"course|section"``.
    # Fixture loaders and test helpers tend to pass plain dicts; production
    # callers may pass dataclass instances. Both shapes land here.
    normalised_baseline: dict[str, BaselinePlacement] | None = (
        _normalise_baseline_map(baseline_placements) if baseline_placements else None
    )
    warm_start_on = is_warm_start_enabled()

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {
            "placed": 0,
            "skipped": 0,
            "placements": [],
            "capacity_buffer": None,
            "pr1_lock_rejections": [],
            "room_failures": [],
            "room_failure_breakdown": {},
            "buffer_only_rejects": 0,
            "unplaced_count": 0,
            # PR3 commit 3 — schema-stable empty dict even on the board-
            # not-found fast path (DoR sign-off amendment on schema
            # stability across all payload exits).
            "decision_trace": {},
            # PR3 commit 5 — perturbation metric is always present.
            # With no placements and no baseline, all four counters are 0.
            # Board-not-found pre-dates budget discovery, so we cannot scope
            # the caller's baseline to this board's courses — pass ``None``
            # to avoid mis-attributing scenario-wide baseline entries as
            # ``removed`` here. Scenario-level aggregation (commit 6) sees
            # the true set via the boards that DO run through the pipeline.
            "perturbation_metric": compute_perturbation_metric([], None),
            # PR6 commit 3 — telemetry block always present, zeros here.
            "stage_telemetry": empty_stage_telemetry(),
        }

    scenario = board.scenario
    slot_config = scenario.slot_config if scenario.slot_config else DEFAULT_SLOTS
    lab_slot_config = scenario.lab_slot_config if scenario.lab_slot_config else DEFAULT_LAB_SLOTS

    # ── PR1 lock-respect enforcement (flag-gated; default OFF) ──────
    # Enforcement is structural, not advisory: when the flag is on, the
    # preload loop (below, after occupancy structures are initialised)
    # (1) collects locked SectionPlacement rows up front, (2) seeds the
    # same occupancy state the planner consults for conflict scoring and
    # room assignment, and (3) lets the existing `already = count()`
    # logic skip those sections in the round-robin iteration — so no
    # parallel "locked cells" path is checked in isolation.
    #
    # Telemetry honesty: in PR1 ``pr1_lock_rejections`` records
    # lock-respect events from preloaded locked placements (one entry
    # per locked row). It does NOT record per-candidate lock collisions
    # during round-robin placement — the structural skip makes those
    # collisions unreachable, so there is nothing to count there.
    lock_rule_on = is_lock_enforcement_enabled()
    pr1_lock_rejections: list[dict] = []

    # ── 0. Load rooms for this board's programme(s) ─────────────────
    from core.services.timetable_rooming import (
        RoomTracker,
        get_board_gender,
        get_capacity_buffer,
        get_programme_rooms,
    )

    capacity_buffer = get_capacity_buffer()

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    room_list = get_programme_rooms(programmes) if programmes else []
    room_tracker = RoomTracker(room_list) if room_list else None
    online_lookup = OnlineCourseLookup()
    online_codes: set[str] = online_lookup.codes_for_board(board)
    # Board-wide gender derived from linked students (homogeneous in
    # practice). Restricts the room pool to matching-section rooms.
    board_gender = get_board_gender(board_id)

    # Pre-populate tracker with rooms already used by OTHER boards in this scenario
    if room_tracker:
        other_placements = (
            SectionPlacement.objects.filter(board__scenario=scenario)
            .exclude(board=board)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("board", "term_section")
        )
        for other in other_placements:
            if online_lookup.is_online_course_for_board(
                other.board, other.term_section.course_code
            ):
                continue
            room_tracker.usage[(other.day, other.start_time)].add(other.room)

    # ── 1. Load section budgets for this board's term level ───────────
    # Ordered by descending demand so the most popular courses are placed
    # first and get the best (least-conflicting) slots.
    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario=scenario,
            programme_term=board.nominal_term,
        ).order_by("-total_demand")
    )

    # ── 1b. Pick up cross-term courses ──────────────────────────────
    # Problem: Some courses (e.g. FE2) have programme_term=10 in one
    # plan but students needing them are on boards 3/5/7/9. The budget
    # filter in Step 1 (programme_term == board.nominal_term) misses them.
    #
    # Solution: Check if students on THIS board need courses that aren't
    # in the budget, and place them here — but ONLY if this board has
    # the most students who need the course (to avoid duplicate placement
    # on every board that has even one student needing it).
    from core.models import BoardStudentLink

    budget_codes = {b.course_key or b.course_code for b in budgets}
    board_student_ids = set(
        BoardStudentLink.objects.filter(board=board).values_list("student_id", flat=True)
    )
    if board_student_ids:
        # Find courses needed by students on this board but not in budget
        scenario_demands = load_scenario_course_demands(scenario.id)
        cross_term_demand: dict[str, int] = defaultdict(int)
        for demand in scenario_demands:
            if demand.student_id in board_student_ids and demand.course_key not in budget_codes:
                cross_term_demand[demand.course_key] += 1

        if cross_term_demand:
            # For each cross-term course, check if THIS board has the
            # most students who need it (across all boards in the scenario)
            all_boards = DeliveryBoard.objects.filter(scenario=scenario)
            for course_code, this_board_count in cross_term_demand.items():
                # Check if already placed on another board
                already_placed = SectionPlacement.objects.filter(
                    board__scenario=scenario,
                    term_section__course_key=course_code,
                ).exists()
                if already_placed:
                    continue

                # Check if another board has MORE students needing this course
                best_board = True
                for other_board in all_boards:
                    if other_board.id == board.id:
                        continue
                    other_sids = set(
                        BoardStudentLink.objects.filter(board=other_board).values_list(
                            "student_id", flat=True
                        )
                    )
                    other_count = sum(
                        1
                        for demand in scenario_demands
                        if demand.student_id in other_sids and demand.course_key == course_code
                    )
                    if other_count > this_board_count:
                        best_board = False
                        break

                if best_board:
                    cb = ScenarioSectionBudget.objects.filter(
                        scenario=scenario, course_key=course_code
                    ).first()
                    if cb:
                        budgets.append(cb)
                        budget_codes.add(course_code)

    if not budgets:
        return {
            "placed": 0,
            "skipped": 0,
            "placements": [],
            "capacity_buffer": capacity_buffer,
            "pr1_lock_rejections": pr1_lock_rejections,
            "room_failures": [],
            "room_failure_breakdown": {},
            "buffer_only_rejects": 0,
            "unplaced_count": 0,
            # PR3 commit 3 — schema stability: the decision_trace key is
            # always present, even when there is nothing to place.
            "decision_trace": {},
            # PR3 commit 5 — perturbation metric present even on the
            # no-budget fast path. Same reasoning as the board-not-found
            # branch: without budgets we cannot scope baseline to this
            # board's courses, so pass ``None`` rather than falsely
            # attributing scenario-wide entries as ``removed``.
            "perturbation_metric": compute_perturbation_metric([], None),
            # PR6 commit 3 — zero telemetry on the no-budget fast path.
            "stage_telemetry": empty_stage_telemetry(),
        }

    # ── 2. Build student-to-course mapping ────────────────────────────
    # For each planner course key, collect the set of student IDs that need it.
    course_students = compute_course_students_index(scenario.id)

    # ── Build real student-overlap matrix ───────────────────────────────
    from core.services.timetable_overlap import (
        build_overlap_matrix as _build_om,
    )
    from core.services.timetable_overlap import (
        course_overlap_load as _col,
    )

    board_courses = set(budget_codes)
    overlap_matrix = _build_om(scenario.id, board_courses)

    # ── Placement tracking structures ─────────────────────────────────
    # placed_masks: flat list of (course_code, bitmask) for ALL placed sections
    #               Used for hard conflict detection via real student overlap.
    # placed_schedule: flat list of (day, start, end, course_code) for gap calc.
    #                  Gap only computed between courses that share students.
    # all_placed_masks: alias for same-course overlap detection (unchanged).
    placed_masks: list[tuple[str, int]] = []
    placed_schedule: list[tuple[str, str, str, str]] = []
    all_placed_masks: list[tuple[str, int]] = []
    # Slot density: count how many sections use each start_time.
    # Used to break ties by pushing courses to less-populated slots.
    slot_density: dict[str, int] = defaultdict(int)
    placement_results: list[dict] = []
    total_placed = 0
    total_skipped = 0
    # PR2 commit 3 — typed failure records for the two silent-UNASSIGNED sites
    # inside auto_place_board (scoring best_option=None, and tracker None
    # fallback). Surfaced on the return dict as ``room_failures``.
    room_failures: list[dict] = []
    # PR3 commit 3 — decision-trace capture (observational, flag-gated).
    # When ``trace_enabled`` is True we record one ``DecisionTrace`` per
    # placed section: the chosen slot plus up to 3 alternatives ordered
    # by score rank (best-rejected first). When False, ``decision_trace``
    # stays empty but the key is still present in the return payload for
    # schema stability (DoR sign-off amendment). The captured codes today
    # are a subset of the DoR alphabet: STUDENT_CONFLICT is live;
    # INSTRUCTOR_CLASH is a defined sentinel but not emitted until
    # instructor-per-section data plumbing lands (ChatGPT commit-3 ruling
    # I2 — see docs/PR3-DOR.md and the scenario-pack README).
    trace_enabled = is_decision_trace_enabled()
    decision_trace: dict[str, dict] = {}

    # ── PR4 commit 3 — instructor-clash plumbing (flag-gated) ────────
    # When ``TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED`` is on we build two
    # per-run lookup tables from the scenario's ``TermSectionMeeting``
    # rows (opaque-string discipline per A6 — no delimiter parsing):
    #
    #   ``instructor_schedule_full`` — ``{normalised_instructor:
    #       {(section_code_full, day_upper, start_minute), ...}}``.
    #       Richer than the public ``build_instructor_schedule`` helper
    #       (commit 2) because the emission loop must distinguish
    #       "another section already booked this instructor here" from
    #       "this is the current section's own pre-existing booking".
    #
    #   ``section_instructor`` — ``{section_code_full: normalised_id}``
    #       so the candidate loop can look up the current section's
    #       instructor without a second DB round-trip.
    #
    # Both maps stay empty when the flag is off, which keeps the
    # candidate loop's fast-path free of any extra work. Commit 8 flips
    # the flag default; env override remains the kill-switch.
    instructor_clash_on = is_instructor_clash_enabled()
    instructor_schedule_full: dict[str, set[tuple[str, str, int]]] = {}
    section_instructor: dict[str, str | None] = {}
    if instructor_clash_on:
        meeting_rows = (
            TermSectionMeeting.objects.filter(term_section__scenario_id=scenario.id)
            .exclude(instructor="")
            .values_list(
                "term_section__course_key",
                "term_section__section",
                "day",
                "start_time",
                "instructor",
            )
        )
        for cc, sec, day, start_time, instructor in meeting_rows:
            normalised = normalise_instructor(instructor)
            if normalised is None:
                continue
            section_full = f"{cc}|{sec}"
            try:
                hh_str, mm_str = start_time.split(":", 1)
                start_minute = int(hh_str) * 60 + int(mm_str)
            except (ValueError, AttributeError):
                continue
            day_upper = (day or "").upper()
            instructor_schedule_full.setdefault(normalised, set()).add(
                (section_full, day_upper, start_minute)
            )
            section_instructor[section_full] = normalised

    # ── 2b. PR1 lock preload ─────────────────────────────────────────
    # When TIMETABLE_ENFORCE_LOCKS is on, locked SectionPlacement rows
    # for this board are seeded into the same occupancy structures the
    # planner consults below:
    #   - placed_masks + all_placed_masks → same-course / cross-course
    #     student-overlap conflict detection
    #   - placed_schedule → idle-gap scoring and S2+ instructor-adjacency
    #     scoring for the same course
    #   - room_tracker.usage → room assignment avoids locked rooms at
    #     the locked (day, start) without any special-case branch
    #   - slot_density → tie-breaking pushes later placements toward
    #     less-populated slots, acknowledging the locked cell's pressure
    # The round-robin skip is automatic: ``already = count()`` on line
    # ~820 already counts locked rows, so ``to_place = planned_sections
    # - already`` excludes them and the sec_round loop never retries
    # that section index. One LOCK_RESPECT telemetry row is emitted per
    # locked cell so the payload records which locks were respected.
    if lock_rule_on:
        locked_placements = (
            SectionPlacement.objects.filter(board=board, is_locked=True)
            .select_related("term_section")
            .order_by("day", "start_time")
        )
        for lp in locked_placements:
            cc = lp.term_section.course_key or lp.term_section.course_code
            mask = _time_mask(lp.day, lp.start_time, lp.end_time)
            placed_masks.append((cc, mask))
            placed_schedule.append((lp.day, lp.start_time, lp.end_time, cc))
            all_placed_masks.append((cc, mask))
            slot_density[lp.start_time] += 1
            if (
                room_tracker
                and lp.room
                and lp.room != "UNASSIGNED"
                and normalise_course_code(lp.term_section.course_code) not in online_codes
            ):
                room_tracker.usage[(lp.day, lp.start_time)].add(lp.room)
            pr1_lock_rejections.append(
                RejectionReason(
                    code=LOCK_RESPECT,
                    day=lp.day,
                    start_time=lp.start_time,
                    end_time=lp.end_time,
                    course_code=cc,
                    context={"locked_room": lp.room},
                ).to_dict()
            )
            # PR3 commit 5 — when warm-start is on and the caller
            # provided a baseline for a locked section at a DIFFERENT
            # slot, emit a DecisionTrace row showing the lock as chosen
            # and the baseline as the rejected alternative (with
            # rejection_code=LOCK_RESPECT). This is the only way the
            # registrar learns "your previous slot was overridden by a
            # lock" — the round-robin loop skips locked sections so
            # they never reach the per-section trace path below.
            # Priority order locked in: PR1 locks → PR3 warm-start →
            # cold-start (see fixture #5 notes).
            if trace_enabled and warm_start_on and normalised_baseline is not None:
                section_code_full = f"{cc}|{lp.term_section.section}"
                baseline_entry = normalised_baseline.get(section_code_full)
                if baseline_entry is not None and (
                    baseline_entry.day != lp.day or baseline_entry.start_time != lp.start_time
                ):
                    decision_trace_lock_alt = Alternative(
                        day=baseline_entry.day,
                        start_time=baseline_entry.start_time,
                        end_time=baseline_entry.end_time,
                        room="",
                        rejection_code=LOCK_RESPECT,
                        rejection_context={"locked_section": section_code_full},
                    )
                    decision_trace[section_code_full] = DecisionTrace(
                        section_code=section_code_full,
                        course_code=cc,
                        chosen_day=lp.day,
                        chosen_start_time=lp.start_time,
                        chosen_end_time=lp.end_time,
                        chosen_room=lp.room or "",
                        alternatives=(decision_trace_lock_alt,),
                    ).to_dict()

    # ── 4. Pre-compute per-course data ────────────────────────────────
    # For each budgeted course, determine how many sections still need
    # placing and pre-generate all feasible meeting options.
    course_data: list[dict] = []
    for budget in budgets:
        code = budget.course_key or budget.course_code
        display_code = budget.course_code
        course_name = budget.course_name or display_code
        credit_hours = budget.credit_hours or 3
        pattern = get_meeting_pattern(credit_hours)
        already = (
            SectionPlacement.objects.filter(board=board, term_section__course_key=code)
            .values("term_section_id")
            .distinct()
            .count()
        )
        to_place = max(0, budget.planned_sections - already)
        if to_place == 0:
            continue
        blocked = scenario.blocked_slots if scenario.blocked_slots else []
        all_options = _generate_meeting_options(pattern, slot_config, lab_slot_config, blocked)
        if not all_options:
            total_skipped += to_place
            continue
        course_data.append(
            {
                "code": code,
                "display_code": display_code,
                "course_name": course_name,
                "budget": budget,
                "credit_hours": credit_hours,
                "pattern": pattern,
                "already": already,
                "to_place": to_place,
                "all_options": all_options,
                "students": course_students.get(code, set()),
                "is_online": normalise_course_code(display_code) in online_codes,
            }
        )

    # ── 5. Round-robin placement: all S1s, then all S2s, ... ──────────
    # By placing all sections of the same group index together, we ensure
    # that S1 of different courses (which serve the *same* primary student
    # cohort) never overlap.  S2 sections get their own independent
    # conflict space, and so on.
    max_sections_needed = max((cd["to_place"] for cd in course_data), default=0)

    # PR6 commit 3 — greedy stage instrumentation.
    # Per DoR §3 + ChatGPT commit-3 ruling:
    #   greedy.ms         = placement-loop wall time only (monotonic clock)
    #   greedy.iterations = section-placement decisions attempted
    #                       (includes failed/unassigned attempts)
    # Setup work (graph build, sort, room preload) is deliberately NOT
    # timed — that keeps the metric comparable across scenarios with
    # different setup costs. Flag-off short-circuits both reads so the
    # non-telemetry path has zero measurable overhead.
    _telemetry_on = is_stage_telemetry_enabled()
    _greedy_attempts = 0
    # perf_counter gives sub-millisecond resolution on Windows where
    # monotonic() can round to the 15ms OS tick — matters for tiny
    # fixtures where a 1-section placement finishes well under one tick.
    _greedy_t0 = time.perf_counter() if _telemetry_on else 0.0

    for sec_round in range(1, max_sections_needed + 1):
        # Gap weighting: the primary group (S1) receives a 10x multiplier
        # on gap penalty, making the algorithm work hard to give them a
        # compact, zero-gap schedule.  Overflow groups (S2, S3, ...) get
        # progressively lower weights since their students are less likely
        # to take a full course load.
        strat = STRATEGIES.get(strategy, STRATEGIES[DEFAULT_STRATEGY])
        gap_base = strat["gap_multiplier"]
        slot_pref = strat["slot_preference"]
        # gap_weight now set per-course based on real overlap load (not sec_round)

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

            # PR6 commit 3 — count one placement decision attempted.
            # Incremented past the to_place guard so rounds with nothing
            # to do don't inflate the counter. Failed/unassigned attempts
            # still count (ChatGPT ruling a).
            _greedy_attempts += 1

            code = cd["code"]
            display_code = cd.get("display_code", code)
            sec_label = f"S{sec_idx}"
            my_students = cd["students"]
            all_options = cd["all_options"]

            # Per-course gap weight based on real overlap load
            overlap_load = _col(overlap_matrix, code)
            gap_weight = max(1, min(gap_base, overlap_load // 5))

            # Use all placed masks + schedule (overlap matrix filters by real students)
            cur_placed = placed_masks.copy()
            cur_sched = placed_schedule.copy()

            best_score = (float("inf"),) * 6
            best_option = None

            # PR3 commit 3 — per-section trace accumulators. Only populated
            # when the flag is on (cheap early-out keeps the hot scoring
            # loop free of extra work when trace capture is disabled).
            pr3_scored_options: list[tuple[tuple, tuple[dict, ...]]] = []
            # PR4 commit 3 — parallel accumulator for INSTRUCTOR_CLASH
            # rejections. Populated only when the PR4 flag is on (the
            # outer ``instructor_clash_on`` gate elides the filter entirely
            # when dormant). Consumed by ``_build_pr3_alternatives`` so
            # the decision-trace alphabet can now include INSTRUCTOR_CLASH.
            pr4_instructor_rejected: list[tuple[tuple[dict, ...], str, dict]] = []

            is_online = cd.get("is_online", False)

            # ── Score every candidate option and keep the best ────────
            # Use actual students per section (not theoretical max) for room matching
            # ceil division to ensure room can hold the largest possible section
            budget = cd["budget"]
            # Actual students per section scaled by TIMETABLE_CAPACITY_BUFFER
            # (default 1.1, i.e. +10% for late adds).
            raw_cap = (
                -(-budget.total_demand // budget.planned_sections)  # ceil division
                if budget.planned_sections > 0
                else budget.max_per_section
            )
            section_cap = int(raw_cap * capacity_buffer)
            # Only 4-credit courses have actual lab meetings. 2-credit
            # courses have 100-min meetings that are long lectures, not labs.
            is_lab_course = budget.credit_hours == 4
            for option in all_options:
                # PR4 commit 3 — INSTRUCTOR_CLASH filter (flag-gated).
                # Reject any option that would double-book the current
                # section's instructor at a (day, start_time) already
                # held by a different section with the same normalised
                # instructor id. This is a pre-score hard filter: options
                # that fail this check never reach the
                # scoring step. The first clashing meeting per option is
                # recorded; subsequent clashes in the same option are
                # noise (the option is already out of the candidate set).
                if instructor_clash_on:
                    section_full_cur = f"{code}|{sec_label}"
                    my_instr = section_instructor.get(section_full_cur)
                    if my_instr is not None:
                        bookings = instructor_schedule_full.get(my_instr, set())
                        clash_ctx: dict | None = None
                        for m in option:
                            m_day_up = m["day"].upper()
                            m_start_min = _to_min(m["start"])
                            for other_section, bd, bs in bookings:
                                if other_section == section_full_cur:
                                    continue
                                if bd == m_day_up and bs == m_start_min:
                                    clash_ctx = {
                                        "clashing_section": other_section,
                                        "clashing_instructor_id": my_instr,
                                    }
                                    break
                            if clash_ctx is not None:
                                break
                        if clash_ctx is not None:
                            if trace_enabled:
                                pr4_instructor_rejected.append(
                                    (option, INSTRUCTOR_CLASH, clash_ctx)
                                )
                            continue

                # Same-course instructor clash: two sections of the same
                # course cannot overlap because the same instructor
                # typically teaches all sections of a course.
                # This is independent of the INSTRUCTOR_CLASH filter above
                # (which requires the ``instructor_id`` field to be set) —
                # here we rely on the registrar's convention that course
                # code → instructor. Rejected options are logged to the
                # same trace bucket as INSTRUCTOR_CLASH.
                same_course_ctx = _same_course_overlap_context(option, placed_schedule, code)
                if same_course_ctx is not None:
                    if trace_enabled:
                        pr4_instructor_rejected.append(
                            (option, SAME_COURSE_INSTRUCTOR_CLASH, same_course_ctx)
                        )
                    continue

                # Room feasibility: prefer options with rooms, penalize roomless
                room_penalty = 0
                if room_tracker and not is_online:
                    for m in option:
                        duration = _to_min(m["end"]) - _to_min(m["start"])
                        # Rule: only 4-credit courses (is_lab_course) go to
                        # labs. The unified duration predicate only gates the
                        # length; the cr==4 gate must stay on both paths.
                        if is_lab_heuristic_unified():
                            rtype = (
                                "lab"
                                if (is_lab_course and meeting_requires_lab_room(duration))
                                else "lecture"
                            )
                        else:
                            rtype = "lab" if (duration > 80 and is_lab_course) else "lecture"
                        room_cap = 0 if rtype == "lab" else section_cap
                        if not room_tracker.is_feasible(
                            m["day"], m["start"], room_cap, rtype, board_gender
                        ):
                            room_penalty += 100  # heavy penalty but not a hard reject

                raw_score = _score_option(
                    option,
                    cur_placed,
                    course_students,
                    my_students,
                    my_code=code,
                    placed_schedule=cur_sched,
                    other_sections_masks=all_placed_masks,
                    is_online=is_online,
                    overlap_matrix=overlap_matrix,
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
                # Time variance: light preference for same slot across meetings
                # Too high = all courses cluster in slots 1-2
                time_var_penalty = raw_score[4] * 3

                # Slot density: push courses toward less-populated slots.
                # This breaks ties when the first few courses are being placed
                # and all options score identically — without it, everything
                # lands in slot 1.  Weight 3: enough to break ties but won't
                # override student conflict avoidance (shared*2 per overlap).
                density_penalty = 0
                for m in option:
                    density_penalty += slot_density[m["start"]] * 3

                # Same-course section adjacency: create the back-to-back
                # section pair during generation, not as a later optimiser
                # repair. The penalty is added to the high-priority overlap
                # bucket below so it dominates normal gap/density concerns.
                same_course_adjacency_penalty = _same_course_back_to_back_penalty(
                    option,
                    placed_schedule,
                    code,
                )

                # raw_score[5] = student_overlap_penalty (sum of shared counts
                # for overlapping course pairs).  Placed in its own score
                # position between same_course_overlap and gap, so it
                # dominates gap/density but NOT same-course constraints.
                # This is intentionally "semi-hard": overlap is always worse
                # than any gap arrangement, but a course pair with 2 shared
                # students can be overlapped if the alternative requires
                # overlapping a pair with 5 shared students.
                score = (
                    raw_score[0],
                    raw_score[1],
                    raw_score[5] + same_course_adjacency_penalty,
                    raw_score[2] * gap_weight
                    + slot_penalty
                    + time_var_penalty
                    + room_penalty
                    + density_penalty,
                    raw_score[3],
                    raw_score[4],
                )
                if trace_enabled:
                    pr3_scored_options.append((score, option))
                if score < best_score:
                    best_score = score
                    best_option = option

            # PR3 commit 5 — warm-start retention (flag-gated).
            # Priority: PR1 locks (handled by the preload skip above) →
            # PR3 warm-start retention → cold-start scoring. When a
            # baseline slot is still feasible (it survived room/feasibility
            # filtering into ``pr3_scored_options``), swap it in as the
            # chosen option.
            section_code_full = f"{code}|{sec_label}"
            baseline_failure_code: str | None = None
            baseline_failure_context: dict = {}
            baseline_option_matched: tuple[dict, ...] | None = None
            warm_start_retained = False
            if warm_start_on and normalised_baseline is not None:
                baseline_option_matched = apply_warm_start(
                    section_code_full, normalised_baseline, all_options
                )
                if baseline_option_matched is not None:
                    # Was it feasibility-clean (i.e. it was scored)?
                    feasible = any(opt is baseline_option_matched for _s, opt in pr3_scored_options)
                    if feasible:
                        best_option = baseline_option_matched
                        warm_start_retained = True
                    # else: the baseline option was filtered out pre-score
                    # (e.g. instructor clash); no specific failure code is
                    # surfaced for the trace in that case.

            if best_option is None:
                total_skipped += 1
                # PR2 commit 4 — Site 1: scoring loop found no feasible option.
                # No option survived scoring, so there's no specific (day,
                # start_time, end_time) to report — slot fields stay empty
                # strings. When the oracle is off, default to NO_ROOM_CAPACITY
                # (commit 3 parity). When it's on, run the Stage 1 refinement
                # chain against the overall room pool: type → gender → capacity.
                # We deliberately pick the required type from the course's
                # ``is_lab_course`` flag rather than any specific meeting —
                # the scoring loop ran against every option and none
                # survived, so the coarse pool check is what we can prove.
                refined: RoomFailureReason | None = None
                if is_room_oracle_enabled() and room_tracker:
                    section_dict = {
                        "course_code": display_code,
                        "section_code": sec_label,
                        "day": "",
                        "start_time": "",
                        "end_time": "",
                        "demand": raw_cap,
                        "room_type_required": "lab" if is_lab_course else "lecture",
                        "gender_required": board_gender,
                    }
                    refined = (
                        check_type_feasibility(section_dict, room_tracker.rooms)
                        or check_gender_feasibility(section_dict, room_tracker.rooms)
                        or check_capacity_feasibility(
                            section_dict, room_tracker.rooms, capacity_buffer
                        )
                    )
                if refined is None:
                    refined = RoomFailureReason(
                        code=NO_ROOM_CAPACITY,
                        day="",
                        start_time="",
                        end_time="",
                        course_code=display_code,
                        section_code=sec_label,
                    )
                room_failures.append(refined.to_dict())
                continue

            # ── Persist the chosen placement ──────────────────────────
            # TermSection: the logical section record (e.g. "MATH101 S1").
            ts, _ = TermSection.objects.get_or_create(
                scenario=scenario,
                course_key=code,
                section=sec_label,
                defaults={
                    "course_code": display_code,
                    "course_number": display_code,
                    "course_name": cd.get("course_name") or display_code,
                    "available_capacity": cd["budget"].max_per_section,
                    "source_tag": "tw_auto",
                },
            )

            # TermSectionMeeting + SectionPlacement: one row per meeting
            # day.  Also update the in-memory tracking structures so
            # subsequent placements see the new constraints.
            meeting_results = []
            preferred_room = None  # room stability: try same room for all meetings
            for m in best_option:
                # Assign room if tracker available
                assigned_room = ""
                if room_tracker and not is_online:
                    duration = _to_min(m["end"]) - _to_min(m["start"])
                    # Same cr==4 (is_lab_course) gate as the scoring loop above.
                    if is_lab_heuristic_unified():
                        rtype = (
                            "lab"
                            if (is_lab_course and meeting_requires_lab_room(duration))
                            else "lecture"
                        )
                    else:
                        rtype = "lab" if (duration > 80 and is_lab_course) else "lecture"
                    # Try preferred room first (same room as previous meetings)
                    if preferred_room and room_tracker.is_feasible(
                        m["day"], m["start"], section_cap, rtype, board_gender
                    ):
                        used = room_tracker.usage.get((m["day"], m["start"]), set())
                        if preferred_room not in used:
                            from core.models import Room as _RoomModel

                            pr_obj = _RoomModel.objects.filter(room_code=preferred_room).first()
                            if (
                                pr_obj
                                and pr_obj.capacity >= section_cap
                                and pr_obj.room_type == rtype
                                and (
                                    not board_gender
                                    or (pr_obj.section or "").upper() == board_gender
                                )
                            ):
                                room_tracker.usage[(m["day"], m["start"])].add(preferred_room)
                                assigned_room = preferred_room

                    if not assigned_room:
                        # For lab meetings, don't filter by capacity — lab rooms
                        # have fixed physical size. Section demand may exceed lab
                        # capacity because students rotate through lab slots.
                        room_cap = 0 if rtype == "lab" else section_cap
                        best_fit = room_tracker.assign_best_fit(
                            m["day"], m["start"], room_cap, rtype, board_gender
                        )
                        if best_fit:
                            assigned_room = best_fit
                        else:
                            assigned_room = "UNASSIGNED"
                            # PR2 commit 4 — Site 2: tracker.assign_best_fit
                            # returned None for this meeting. Run the
                            # refinement chain: type → gender → capacity →
                            # occupancy. Labs pass demand=0 to the helpers
                            # because the existing control flow skips
                            # capacity filtering for lab rooms (rotation
                            # model); this keeps the oracle symmetric with
                            # the tracker behaviour so we don't spuriously
                            # report NO_ROOM_CAPACITY for a lab pool that
                            # the tracker would have treated as adequate.
                            m_section_dict = {
                                "course_code": display_code,
                                "section_code": sec_label,
                                "day": m["day"],
                                "start_time": m["start"],
                                "end_time": m["end"],
                                "demand": 0 if rtype == "lab" else raw_cap,
                                "room_type_required": rtype,
                                "gender_required": board_gender,
                            }
                            m_refined: RoomFailureReason | None = None
                            if is_room_oracle_enabled():
                                m_refined = (
                                    check_type_feasibility(m_section_dict, room_tracker.rooms)
                                    or check_gender_feasibility(m_section_dict, room_tracker.rooms)
                                    or check_capacity_feasibility(
                                        m_section_dict,
                                        room_tracker.rooms,
                                        capacity_buffer,
                                    )
                                    or check_occupancy(
                                        m_section_dict,
                                        room_tracker.rooms,
                                        room_tracker.usage.get((m["day"], m["start"]), set()),
                                    )
                                )
                            if m_refined is None:
                                m_refined = RoomFailureReason(
                                    code=NO_ROOM_CAPACITY,
                                    day=m["day"],
                                    start_time=m["start"],
                                    end_time=m["end"],
                                    course_code=display_code,
                                    section_code=sec_label,
                                )
                            room_failures.append(m_refined.to_dict())
                    if not preferred_room and assigned_room and assigned_room != "UNASSIGNED":
                        preferred_room = assigned_room

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
                    defaults={"end_time": m["end"], "room": assigned_room},
                )
                mask = _time_mask(m["day"], m["start"], m["end"])
                placed_masks.append((code, mask))
                placed_schedule.append((m["day"], m["start"], m["end"], code))
                all_placed_masks.append((code, mask))
                slot_density[m["start"]] += 1
                meeting_results.append(
                    {
                        "day": m["day"],
                        "start": m["start"],
                        "end": m["end"],
                        "room": assigned_room,
                    }
                )

            total_placed += 1
            placement_results.append(
                {
                    "course_key": code,
                    "course_code": display_code,
                    "course_name": cd.get("course_name") or display_code,
                    "section": sec_label,
                    "credit_hours": cd["credit_hours"],
                    "meetings": meeting_results,
                    "conflict_score": best_score[0],
                }
            )

            # PR3 commit 3 — build the DecisionTrace for this placement.
            # First-successful-placement only (ChatGPT commit-3 ruling E);
            # no re-capture on polish paths. Alternatives = top-3 by score
            # rank, best-rejected first (ruling F1), classified by the
            # first hard reason they fail. Options with no hard reason
            # (feasibility-clean but lost on score) are skipped — rejection
            # codes must be known sentinels (acceptance bar #2) and "lost
            # on score" has no honest name.
            if trace_enabled and best_option is not None:
                chosen_first = best_option[0]
                alternatives = _build_pr3_alternatives(
                    scored_options=pr3_scored_options,
                    winning_option=best_option,
                    placed_schedule=placed_schedule,
                    course_students=course_students,
                    my_students=my_students,
                    my_code=code,
                    meeting_results=meeting_results,
                    instructor_rejected=pr4_instructor_rejected,
                )
                # PR3 commit 5 — warm-start fallback: prepend the
                # baseline's real failure reason so the registrar can
                # see *why* the previous placement no longer works.
                # Only applies when warm-start was requested, baseline
                # matched a real option, but the option was filtered
                # before scoring (e.g. an instructor clash).
                if (
                    warm_start_on
                    and not warm_start_retained
                    and baseline_option_matched is not None
                    and baseline_failure_code is not None
                ):
                    baseline_first_meeting = baseline_option_matched[0]
                    baseline_alt = Alternative(
                        day=baseline_first_meeting["day"],
                        start_time=baseline_first_meeting["start"],
                        end_time=baseline_first_meeting["end"],
                        room="",
                        rejection_code=baseline_failure_code,
                        rejection_context=baseline_failure_context,
                    )
                    alternatives = [baseline_alt] + [
                        alt
                        for alt in alternatives
                        if not (
                            alt.day == baseline_alt.day
                            and alt.start_time == baseline_alt.start_time
                            and alt.end_time == baseline_alt.end_time
                        )
                    ]
                    alternatives = alternatives[:3]
                decision_trace[section_code_full] = DecisionTrace(
                    section_code=section_code_full,
                    course_code=display_code,
                    chosen_day=chosen_first["day"],
                    chosen_start_time=chosen_first["start"],
                    chosen_end_time=chosen_first["end"],
                    chosen_room=meeting_results[0].get("room", "") if meeting_results else "",
                    alternatives=tuple(alternatives),
                ).to_dict()

    # PR6 commit 3 — stop the greedy clock. Flag-off path stays at zeros.
    _stage_telemetry = empty_stage_telemetry()
    if _telemetry_on:
        record_stage_ms(_stage_telemetry, "greedy", int((time.perf_counter() - _greedy_t0) * 1000))
        record_stage_iterations(_stage_telemetry, "greedy", _greedy_attempts)

    logger.info(
        "auto_place_board(board=%s): placed=%d skipped=%d "
        "lock_rule_on=%s (rejections=%d) warm_start_on=%s (baseline_provided=%s)",
        board_id,
        total_placed,
        total_skipped,
        lock_rule_on,
        len(pr1_lock_rejections),
        warm_start_on,
        normalised_baseline is not None,
    )

    _rfb = room_failure_breakdown(room_failures)
    return {
        "placed": total_placed,
        "skipped": total_skipped,
        "placements": placement_results,
        "capacity_buffer": capacity_buffer,
        "pr1_lock_rejections": pr1_lock_rejections,
        "room_failures": room_failures,
        "room_failure_breakdown": _rfb,
        # PR4 commit 7 — authoritative buffer-only counter, derived from
        # the breakdown so it cannot drift from the underlying failure
        # records.
        "buffer_only_rejects": _rfb.get(ROOM_BUFFER_REJECT, 0),
        "unplaced_count": total_skipped,
        # PR3 commit 3 — key is always present for schema stability.
        # Empty dict when the flag is off or no sections were placed.
        "decision_trace": decision_trace,
        # PR3 commit 5 — perturbation counters against the baseline.
        # With ``baseline_placements=None`` every placement is reported
        # as ``newly_placed``; with a baseline, the four counters split
        # placements into unchanged / changed / newly_placed / removed.
        #
        # PR3 commit 6 — scope the baseline to THIS board's course set
        # before counting. The caller (``auto_place_scenario`` /
        # ``optimise_scenario_timetable_v2``) fans a scenario-wide
        # baseline out to every board; without scoping, each board
        # would report every other board's baseline entries as
        # ``removed``, and summing the four counters at the scenario
        # level would over-count removals by (num_boards-1)×.
        # Scoping by course_code is sufficient because section codes
        # (``<course>|<section>``) are globally unique across boards
        # within a scenario — the same invariant that
        # ``_merge_board_decision_traces`` relies on.
        "perturbation_metric": compute_perturbation_metric(
            placement_results,
            (
                {
                    k: v
                    for k, v in normalised_baseline.items()
                    if k.split("|", 1)[0] in board_courses
                }
                if normalised_baseline is not None
                else None
            ),
        ),
        # PR6 commit 3 — schema-stable telemetry block. Keys always
        # present; values populated when the flag is on, zero otherwise.
        "stage_telemetry": _stage_telemetry,
    }


def _adaptive_scenario(scenario_id: int) -> dict:
    """Adaptive portfolio: greedy → CP-SAT → local search per board.

    1. Greedy compact baseline (always produces a feasible solution).
    2. CP-SAT warm-started from greedy (dynamic time budget by board size).
       If CP-SAT finds an equal-or-better solution, persist it.
    3. Simulated annealing polish on whatever is persisted (greedy or CP-SAT).

    Never returns an empty board — greedy baseline is the floor.
    """
    import logging

    from core.services.timetable_local_search import optimize_and_persist_board
    from core.services.timetable_solver import persist_solver_result, solve_board_with_hints

    logger = logging.getLogger(__name__)

    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    board_results = {}
    total_placed = 0
    total_skipped = 0
    phases_log = {}

    for board in boards:
        label = board.label
        phase_info = {"greedy": None, "cpsat": None, "local_search": None}

        # ── Phase 1: Greedy baseline ────────────────────────────
        greedy = auto_place_board(board.id, strategy="compact")
        phase_info["greedy"] = {"placed": greedy["placed"], "skipped": greedy["skipped"]}
        best_placed = greedy["placed"]

        # ── Phase 2: CP-SAT with warm-start hints ───────────────
        n_sections = greedy["placed"] + greedy["skipped"]
        if n_sections > 0:
            # Dynamic time budget: small boards get full solver, large ones less
            if n_sections < 15:
                cpsat_budget = 3.0
            elif n_sections < 30:
                cpsat_budget = 5.0
            else:
                cpsat_budget = 8.0

            try:
                cpsat = solve_board_with_hints(
                    board.id,
                    greedy["placements"],
                    time_limit_seconds=cpsat_budget,
                )
                phase_info["cpsat"] = {
                    "status": cpsat["status"],
                    "placed": cpsat["placed"],
                    "objective": cpsat.get("objective", 0),
                    "improved": cpsat.get("improved", False),
                }
                if cpsat["status"] in ("optimal", "feasible") and cpsat["placed"] >= best_placed:
                    # CP-SAT found an equal-or-better solution (more placements, or
                    # same count but solver-optimized objective) — persist it
                    persist_solver_result(board.id, cpsat)
                    from core.services.timetable_rooming import (
                        assign_rooms_to_board as _assign_rooms,
                    )

                    _assign_rooms(board.id)
                    best_placed = cpsat["placed"]
                    logger.info(
                        "adaptive[%s]: CP-SAT improved (%s→%s placed)",
                        label,
                        greedy["placed"],
                        cpsat["placed"],
                    )
            except Exception:
                logger.exception("adaptive[%s]: CP-SAT failed, keeping greedy baseline", label)
                phase_info["cpsat"] = {"status": "error", "placed": 0}

        # ── Phase 3: Local search polish ────────────────────────
        try:
            sa = optimize_and_persist_board(board.id, max_seconds=5.0)
            phase_info["local_search"] = {
                "status": sa.get("status", "unknown"),
                "cost_before": sa.get("cost_before", 0),
                "cost_after": sa.get("cost_after", 0),
            }
        except Exception:
            logger.exception("adaptive[%s]: local search failed", label)
            phase_info["local_search"] = {"status": "error"}

        # ── Phase 4: Hotspot feasibility check ──────────────────
        from core.services.timetable_pair_feasibility import find_infeasible_hotspots

        try:
            from core.services.timetable_overlap import build_overlap_matrix as _bom_phase4

            board_courses_p4 = set(
                ScenarioSectionBudget.objects.filter(
                    scenario=board.scenario, programme_term=board.nominal_term
                ).values_list("course_key", flat=True)
            )
            om_p4 = _bom_phase4(board.scenario_id, board_courses_p4)
            infeasible = find_infeasible_hotspots(board.id, om_p4)
            phase_info["feasibility"] = {
                "checked": True,
                "infeasible_pairs": len(infeasible),
                "details": [
                    {
                        "pair": f"{r['course_a']} vs {r['course_b']}",
                        "shared": r["shared_students"],
                        "max_flow": r["max_assignable"],
                    }
                    for r in infeasible
                ],
            }
            if infeasible:
                logger.warning(
                    "adaptive[%s]: %d infeasible hotspot pairs detected",
                    label,
                    len(infeasible),
                )
        except Exception:
            logger.exception("adaptive[%s]: feasibility check failed", label)
            phase_info["feasibility"] = {"checked": False}

        # Report actual placements from DB (may have been improved by CP-SAT or SA)
        final_placements = list(
            SectionPlacement.objects.filter(board=board)
            .select_related("term_section")
            .values_list(
                "term_section__course_code",
                "term_section__section",
                "day",
                "start_time",
                "end_time",
            )
        )
        # Carry the greedy baseline's full schema (decision_trace,
        # stage_telemetry, room_failures, perturbation_metric,
        # capacity_buffer, etc.) forward, then overlay the adaptive
        # post-greedy phases. Keeps the observability payload shape
        # byte-identical across strategies.
        overlaid = dict(greedy)
        overlaid["placed"] = best_placed
        overlaid["placements"] = [
            {"course_code": cc, "section": sec, "meetings": [{"day": d, "start": s, "end": e}]}
            for cc, sec, d, s, e in final_placements
        ]
        board_results[label] = overlaid
        total_placed += best_placed
        total_skipped += greedy["skipped"]
        phases_log[label] = phase_info

    return {
        "boards": board_results,
        "total_placed": total_placed,
        "total_skipped": total_skipped,
        "adaptive_phases": phases_log,
    }


def _build_scenario_result(scenario_id: int) -> dict:
    """Build a result dict from the current DB state of a scenario.

    ``decision_trace`` is always emitted as an empty dict on this path
    because this builder reconstructs the result from DB state alone,
    after in-memory traces from the original placement calls have been
    discarded. PR3 schema-stability clause: the key is always present
    (and commit 6 adds ``perturbation_metric`` with the same invariant).
    """
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    for board in boards:
        placed = (
            SectionPlacement.objects.filter(board=board)
            .values("term_section_id")
            .distinct()
            .count()
        )
        results[board.label] = {
            "placed": placed,
            "skipped": 0,
            "decision_trace": {},
            "perturbation_metric": dict(_EMPTY_PERTURBATION_METRIC),
        }
        total_placed += placed
    return {
        "boards": results,
        "total_placed": total_placed,
        "total_skipped": 0,
        "decision_trace": {},
        "perturbation_metric": dict(_EMPTY_PERTURBATION_METRIC),
    }


def _merge_board_decision_traces(boards_result: dict) -> dict:
    """Merge per-board ``decision_trace`` dicts into a scenario-level dict.

    Used by every path through ``auto_place_scenario`` so the top-level
    return payload always carries a merged trace (PR3 schema stability).
    Section codes are scoped ``<course>|<section>`` and are unique across
    boards within a scenario, so flat-merging via ``dict.update`` is
    loss-free — if a duplicate ever appeared it would indicate a
    scenario-level invariant violation elsewhere.
    """
    merged: dict[str, dict] = {}
    for board_result in boards_result.values():
        trace = board_result.get("decision_trace", {})
        if trace:
            merged.update(trace)
    return merged


_EMPTY_PERTURBATION_METRIC: dict[str, int] = {
    "changes_from_baseline_count": 0,
    "unchanged_count": 0,
    "newly_placed_count": 0,
    "removed_count": 0,
}


def _sum_board_perturbation_metrics(boards_result: dict) -> dict:
    """Sum per-board ``perturbation_metric`` dicts at the scenario level.

    PR3 commit 6. Each of the four counters is additive across boards
    *provided* each board already scoped the caller-supplied baseline to
    its own course set before counting — otherwise ``removed_count``
    double-counts. ``auto_place_board`` handles the scoping; this helper
    is a pure sum.

    Boards that did not emit the key (e.g. a future non-greedy path) are
    treated as all-zero rather than raising, so adding new strategies
    does not break the schema-stability invariant.
    """
    totals = dict(_EMPTY_PERTURBATION_METRIC)
    for board_result in boards_result.values():
        metric = board_result.get("perturbation_metric") or {}
        for key in totals:
            totals[key] += int(metric.get(key, 0))
    return totals


def auto_place_scenario(
    scenario_id: int,
    strategy: str = DEFAULT_STRATEGY,
    baseline_placements: dict | None = None,
) -> dict:
    """Auto-place sections on every board in a scenario.

    Iterates over all ``DeliveryBoard`` rows belonging to the scenario
    (ordered by ``display_order``) and calls ``auto_place_board`` for each.

    Parameters
    ----------
    scenario_id : int
        Primary key of the ``TimetableScenario``.
    strategy : str
        Placement strategy to apply.
    baseline_placements : dict | None
        PR3 commit 6 — optional scenario-wide warm-start baseline. Keys
        are ``"<course>|<section>"`` strings; values are either
        ``BaselinePlacement`` dataclasses or plain dicts with ``day``
        / ``start_time`` / ``end_time`` fields. Fanned out to every
        ``auto_place_board`` call unmodified; each board scopes the map
        down to its own courses internally (see the ``auto_place_board``
        return-site comment on why scoping lives there, not here).

    Returns
    -------
    dict
        ``{"boards": {label: board_result, ...}, "total_placed": int,
        "total_skipped": int, "decision_trace": {...},
        "perturbation_metric": {...}}`` where each *board_result* has
        the same shape as the return value of ``auto_place_board``.
        ``decision_trace`` is the scenario-level flat-merge of every
        board's per-section traces (PR3 commit 4). ``perturbation_metric``
        is the four-counter sum across boards (PR3 commit 6). Non-greedy
        strategies that cannot observe baseline (CP-SAT solver, SA, load
        balancer) emit an all-zero metric for schema stability.
    """
    # Adaptive: greedy baseline → CP-SAT improvement → local search polish
    if strategy == "adaptive":
        result = _adaptive_scenario(scenario_id)
        result.setdefault("decision_trace", _merge_board_decision_traces(result.get("boards", {})))
        # PR3 commit 6 — schema stability. Adaptive does not observe the
        # caller's baseline (no warm-start wire-through), so report the
        # all-zero metric rather than a silent omission.
        result.setdefault(
            "perturbation_metric", _sum_board_perturbation_metrics(result.get("boards", {}))
        )
        return result

    # Use CP-SAT solver for "optimal" strategy — per-board fallback to compact
    if strategy == "optimal":
        import logging

        from core.services.timetable_solver import solve_scenario

        logger = logging.getLogger(__name__)
        result = solve_scenario(scenario_id, time_limit_seconds=5.0)
        boards_result = result.get("boards", {})

        # Per-board fallback: only re-run compact on boards that got 0 placements
        empty_boards = DeliveryBoard.objects.filter(
            scenario_id=scenario_id,
        ).exclude(label__in=[lbl for lbl, b in boards_result.items() if b.get("placed", 0) > 0])

        for board in empty_boards:
            logger.warning("optimal: CP-SAT empty on '%s', falling back to compact", board.label)
            r = auto_place_board(board.id, strategy="compact")
            boards_result[board.label] = r
            result["total_placed"] = result.get("total_placed", 0) + r["placed"]

        # PR3 commit 4: surface per-board traces at the scenario level. For
        # CP-SAT-placed boards there is no trace (solver is silent on
        # rejected alternatives); for compact-fallback boards we keep the
        # greedy trace.
        result["decision_trace"] = _merge_board_decision_traces(boards_result)
        # PR3 commit 6 — same schema-stability reasoning as the adaptive
        # branch above. The CP-SAT solver is baseline-agnostic today, so
        # this path emits an all-zero metric.
        result["perturbation_metric"] = _sum_board_perturbation_metrics(boards_result)
        return result

    # Load-balanced: greedy build + redistribution
    if strategy == "load_balanced":
        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        for board in boards:
            auto_place_board(board.id, strategy="compact")
        from core.services.timetable_load_balanced import rebalance_scenario

        rebalance_scenario(scenario_id, max_seconds_per_board=5.0)
        # Re-query actual DB state after rebalancing
        return _build_scenario_result(scenario_id)

    # Hybrid: greedy build + simulated annealing improvement
    if strategy == "hybrid":
        # Phase 1: greedy (compact) — build feasible solution
        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        for board in boards:
            auto_place_board(board.id, strategy="compact")

        # Phase 2: simulated annealing improvement
        from core.services.timetable_local_search import optimize_scenario

        sa_result = optimize_scenario(scenario_id, max_seconds_per_board=5.0)

        # Re-query actual DB state after SA optimization
        result = _build_scenario_result(scenario_id)
        result["optimization"] = sa_result
        return result

    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    total_placed = 0
    total_skipped = 0
    for board in boards:
        r = auto_place_board(
            board.id,
            strategy=strategy,
            baseline_placements=baseline_placements,
        )
        results[board.label] = r
        total_placed += r["placed"]
        total_skipped += r["skipped"]
    # PR6 commit 7 — scenario-level stage_telemetry = sum of board-level.
    # Aggregation rule per DoR §3: stage_ms and stage_iterations sum
    # board-wise; no averaging. auto_place_board only touches greedy and
    # rooming_repair, but the fold is key-agnostic so later stages (when
    # called through this path) aggregate the same way.
    _scenario_tel = empty_stage_telemetry()
    for _board_result in results.values():
        _bt = _board_result.get("stage_telemetry")
        if isinstance(_bt, dict):
            _scenario_tel = merge_stage_telemetry(_scenario_tel, _bt)
    return {
        "boards": results,
        "total_placed": total_placed,
        "total_skipped": total_skipped,
        "decision_trace": _merge_board_decision_traces(results),
        # PR3 commit 6 — scenario-level perturbation metric. Each board's
        # ``auto_place_board`` scopes its baseline to its own course set
        # before counting, so summing the four counters across boards is
        # loss-free (no double-counting of ``removed``).
        "perturbation_metric": _sum_board_perturbation_metrics(results),
        "stage_telemetry": _scenario_tel,
    }
