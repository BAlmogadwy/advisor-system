"""
core/services/timetable_autoplace.py
Auto-placement algorithm for timetable workspace.

Places sections onto boards using student overlap data to minimize conflicts.

Meeting patterns based on credit hours:
  4 credits → 3 meetings/week (50min, 50min, 100min) on 3 different days
  3 credits → 2 meetings/week (50min, 50min) on 2 different days
  2 credits → 1 meeting/week (100min)

Rules:
  - No more than 1 meeting per day per section
  - Prefer same time slot across days for the same course
  - Minimize student conflicts (courses with shared students don't overlap)
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

WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU"]

MEETING_PATTERNS: dict[int, list[int]] = {
    4: [75, 75, 100],   # 3 meetings: two 75min + one 100min
    3: [75, 75],         # 2 meetings: two 75min
    2: [100],            # 1 meeting: 100min
    1: [75],             # fallback
}


def get_meeting_pattern(credit_hours: int) -> list[int]:
    """Return list of meeting durations in minutes for given credit hours."""
    return MEETING_PATTERNS.get(credit_hours, MEETING_PATTERNS[3])


# ── Default Slots ────────────────────────────────────────────────

DEFAULT_SLOTS = [
    {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
    {"label": "10:30-11:45", "start": "10:30", "end": "11:45"},
    # No slot starts between 11:35-12:59 (prayer break)
    {"label": "13:00-14:15", "start": "13:00", "end": "14:15"},
    {"label": "14:30-15:45", "start": "14:30", "end": "15:45"},
    {"label": "16:00-17:15", "start": "16:00", "end": "17:15"},
]

# Hard constraint: no course may START between these times (prayer break)
BLOCKED_START_WINDOW = ("11:35", "12:59")


def _start_is_blocked(start_time: str) -> bool:
    """Return True if a course starting at this time violates the prayer break."""
    start_min = _to_minutes(start_time)
    block_from = _to_minutes(BLOCKED_START_WINDOW[0])
    block_to = _to_minutes(BLOCKED_START_WINDOW[1])
    return block_from <= start_min <= block_to


def _get_slots(slot_config: list[dict]) -> list[dict]:
    return slot_config if slot_config else DEFAULT_SLOTS


# ── Auto-Placement ───────────────────────────────────────────────


def _generate_meeting_options(
    pattern: list[int],
    slot_config: list[dict],
) -> list[list[dict]]:
    """Generate all valid meeting combinations for a course's pattern.

    Rules:
      - One meeting per day only
      - 50min meetings use a single slot
      - 100min meetings use two consecutive slots
      - Prefer same time slot index across days (scored later, not filtered here)

    Returns list of options, each option is a list of {day, start, end, slot_idx} dicts.
    """
    slots = _get_slots(slot_config)
    num_meetings = len(pattern)

    # Generate day combinations (pick N different days from WEEKDAYS)
    day_combos = list(combinations(WEEKDAYS, num_meetings))

    # For each duration, determine which slot positions work
    slot_options_per_duration: list[list[tuple[int, str, str]]] = []
    for duration in pattern:
        positions = []
        if duration <= 75:
            # 75min lecture → fits in a single slot
            for i, s in enumerate(slots):
                if not _start_is_blocked(s["start"]):
                    positions.append((i, s["start"], s["end"]))
        else:
            # 100min lecture → two consecutive slots merged
            for i in range(len(slots) - 1):
                if not _start_is_blocked(slots[i]["start"]):
                    positions.append((i, slots[i]["start"], slots[i + 1]["end"]))
        slot_options_per_duration.append(positions)

    all_options: list[list[dict]] = []

    for days in day_combos:
        # For same-time preference: try all slot positions for the first meeting,
        # then prefer the same slot index for subsequent meetings
        for first_pos in slot_options_per_duration[0]:
            option: list[dict] = []
            valid = True

            for m_idx in range(num_meetings):
                day = days[m_idx]
                target_slot_idx = first_pos[0]  # prefer same slot index

                # Find the best position for this meeting
                positions = slot_options_per_duration[m_idx]

                # First try: exact same slot index
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
                    # Fallback: use the first available position
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


def _score_option(
    option: list[dict],
    placed_masks: list[tuple[str, int]],
    course_students: dict[str, set[int]],
    my_students: set[int],
) -> tuple[int, int, int]:
    """Score a meeting option. Lower is better.

    Returns (hard_conflict, student_conflict, time_variance).
    - hard_conflict: direct time overlap with ANY already-placed section on this board
      (courses on the same board must never overlap since they serve the same students)
    - student_conflict: weighted by number of shared students across boards
    - time_variance: penalty for different slot indices across days
    """
    total_mask = 0
    for m in option:
        total_mask |= _time_mask(m["day"], m["start"], m["end"])

    # Hard conflict: ANY time overlap with anything on this board is bad
    hard_conflict = 0
    student_conflict = 0
    for placed_code, placed_mask in placed_masks:
        if total_mask & placed_mask:
            hard_conflict += 1  # direct overlap — worst case
            shared = my_students & course_students.get(placed_code, set())
            student_conflict += len(shared)

    # Time consistency score (prefer same slot index across all meetings)
    slot_indices = [m["slot_idx"] for m in option]
    time_variance = len(set(slot_indices)) - 1

    return hard_conflict, student_conflict, time_variance


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
    all_placed_masks: list[tuple[str, int]] = []  # global for fallback
    placement_results: list[dict] = []
    total_placed = 0
    total_skipped = 0

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
        })

    # Place section-by-section across ALL courses: all S1s first, then all S2s, etc.
    # This ensures S1 of different courses don't overlap (same student group 1),
    # S2 of different courses don't overlap (same student group 2), etc.
    max_sections_needed = max((cd["to_place"] for cd in course_data), default=0)

    for sec_round in range(1, max_sections_needed + 1):
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

            best_score = (float("inf"), float("inf"), float("inf"))
            best_option = None

            for option in all_options:
                score = _score_option(option, same_group, course_students, my_students)
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
