"""
core/services/timetable_load_balanced.py
Load-balanced timetable placement using iterative redistribution.

Unlike compact (minimize gaps) or optimal (minimize total idle), this
algorithm minimizes the MAXIMUM daily course load for each student group.

Goal: no day has more than ceil(total_meetings / 5) courses. Students
get a consistent daily schedule instead of heavy/light days.

Algorithm:
  Phase 1: Greedy build (same as compact — gets a feasible solution)
  Phase 2: Iterative rebalancing
    - Find the day with the most meetings for S1
    - Move one meeting from the heaviest day to the lightest day
    - Only accept moves that don't create conflicts
    - Repeat until max_daily_load can't be reduced further
  Phase 3: Gap minimization within the balanced constraint
    - For each day, sort meetings to minimize gaps (swap within same day)
"""

from __future__ import annotations

import random
import time
from collections import defaultdict

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    WEEKDAYS,
    _start_is_blocked,
    _time_mask,
)


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


PRAYER_BOUNDARY = 13 * 60


def _build_schedule_from_db(
    board_id: int, slot_config: list[dict], lab_slot_config: list[dict] | None = None
) -> tuple:
    """Load current placements into an in-memory schedule structure."""
    lab_slots = lab_slot_config or DEFAULT_LAB_SLOTS
    placements = list(
        SectionPlacement.objects.filter(board_id=board_id)
        .select_related("term_section")
        .order_by("term_section__course_code", "term_section__section", "day")
    )

    online_codes: set[str] = set()
    board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    if board.program:
        programs = [p.strip() for p in board.program.split(",") if p.strip()]
        online_codes = set(
            ProgrammeRequirement.objects.filter(program__in=programs, is_online=True).values_list(
                "course_code", flat=True
            )
        )

    def _to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    sections = []
    schedule: dict[int, list[dict]] = {}
    sec_map: dict[tuple[str, str], int] = {}

    for p in placements:
        key = (p.term_section.course_code, p.term_section.section)
        if key not in sec_map:
            idx = len(sections)
            sec_map[key] = idx
            sec_label = p.term_section.section
            sec_num = (
                int(sec_label[1:]) if sec_label.startswith("S") and sec_label[1:].isdigit() else 1
            )
            sections.append(
                {
                    "code": p.term_section.course_code,
                    "sec_num": sec_num,
                    "label": sec_label,
                    "term_section_id": p.term_section_id,
                    "is_online": p.term_section.course_code in online_codes,
                }
            )
            schedule[idx] = []

        idx = sec_map[key]
        # Find slot_idx: check lecture slots, then lab slots
        slot_idx = 0
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        if duration <= 75:
            for si, s in enumerate(slot_config):
                if s["start"] == p.start_time:
                    slot_idx = si
                    break
        else:
            for si, s in enumerate(lab_slots):
                if s["start"] == p.start_time:
                    slot_idx = si
                    break

        schedule[idx].append(
            {
                "day": p.day,
                "slot_idx": slot_idx,
                "start": p.start_time,
                "end": p.end_time,
                "mask": _time_mask(p.day, p.start_time, p.end_time),
                "placement_id": p.id,
            }
        )

    return sections, schedule, online_codes


def _daily_load(
    schedule: dict, sections: list, online_codes: set, overlap_matrix: dict | None = None
) -> dict[str, int]:
    """Count overlap-weighted campus courses per day."""
    from core.services.timetable_overlap import course_overlap_load as _col

    day_count: dict[str, int] = {d: 0 for d in WEEKDAYS}
    for i, meetings in schedule.items():
        sec = sections[i]
        if sec["code"] in online_codes:
            continue
        load_w = max(1, _col(overlap_matrix, sec["code"])) if overlap_matrix else 1
        for m in meetings:
            day_count[m["day"]] += load_w
    return day_count


def _compute_balance_score(
    schedule: dict, sections: list, online_codes: set, overlap_matrix: dict | None = None
) -> float:
    """Lower is better. Penalizes uneven daily loads + gaps."""
    loads = _daily_load(schedule, sections, online_codes, overlap_matrix)
    values = [v for v in loads.values() if v > 0]
    if not values:
        return 0.0

    max_load = max(values)
    min_load = min(values)
    imbalance = (max_load - min_load) * 100  # heavy penalty for imbalance

    # Secondary: overlap-weighted gap cost (same pattern as local search)
    from core.services.timetable_overlap import shared_student_count as _ssc

    course_day_times: dict[str, dict[str, list[tuple]]] = defaultdict(lambda: defaultdict(list))
    for i, meetings in schedule.items():
        sec = sections[i]
        if sec["code"] in online_codes:
            continue
        for m in meetings:
            course_day_times[sec["code"]][m["day"]].append((_to_min(m["start"]), _to_min(m["end"])))

    gap_total = 0.0
    codes = list(course_day_times.keys())
    for a_idx in range(len(codes)):
        for b_idx in range(a_idx, len(codes)):
            ca, cb = codes[a_idx], codes[b_idx]
            if ca == cb:
                continue  # same course: student takes ONE section, no inter-section gap
            elif overlap_matrix:
                shared = _ssc(overlap_matrix, ca, cb)
                if shared == 0:
                    continue
                w = min(shared, 30)
            else:
                w = 10
            for day in set(course_day_times[ca].keys()) & set(course_day_times[cb].keys()):
                times = sorted(course_day_times[ca][day] + course_day_times[cb][day])
                for j in range(len(times) - 1):
                    gap = times[j + 1][0] - times[j][1]
                    if gap > 0:
                        gap_total += gap * w

    return imbalance + gap_total


def _check_constraints(schedule: dict, sections: list, overlap_matrix: dict | None = None) -> bool:
    """Check all hard constraints using real student overlap."""
    from core.services.timetable_overlap import shared_student_count as _ssc_ck

    all_masks: list[tuple[str, int]] = []
    for i, meetings in schedule.items():
        sec = sections[i]
        for m in meetings:
            all_masks.append((sec["code"], m["mask"]))

    for a in range(len(all_masks)):
        for b in range(a + 1, len(all_masks)):
            if all_masks[a][0] == all_masks[b][0]:
                continue
            if all_masks[a][1] & all_masks[b][1]:
                # Only hard-block for high-overlap pairs (>= 20 shared students)
                shared = (
                    _ssc_ck(overlap_matrix, all_masks[a][0], all_masks[b][0])
                    if overlap_matrix
                    else 999
                )
                if shared >= 20:
                    return False

    course_masks: dict[str, list[int]] = defaultdict(list)
    for i, meetings in schedule.items():
        for m in meetings:
            course_masks[sections[i]["code"]].append(m["mask"])
    for _code, masks in course_masks.items():
        for a in range(len(masks)):
            for b in range(a + 1, len(masks)):
                if masks[a] & masks[b]:
                    return False

    for i, meetings in schedule.items():  # noqa: B007
        days = [m["day"] for m in meetings]
        if len(days) != len(set(days)):
            return False

    return True


def rebalance_board(board_id: int, max_seconds: float = 8.0, seed: int = 42) -> dict:
    """Rebalance a board's timetable to equalize daily loads.

    Returns dict with balance metrics before and after.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"status": "error"}

    slot_config = board.scenario.slot_config or DEFAULT_SLOTS

    # Build real overlap matrix
    from core.models import ScenarioSectionBudget as _SSB
    from core.services.timetable_overlap import build_overlap_matrix as _bom

    _board_courses = set(
        _SSB.objects.filter(scenario=board.scenario, programme_term=board.nominal_term).values_list(
            "course_code", flat=True
        )
    )
    overlap_matrix = _bom(board.scenario_id, _board_courses) if _board_courses else {}
    lab_slot_config = board.scenario.lab_slot_config or DEFAULT_LAB_SLOTS
    sections, schedule, online_codes = _build_schedule_from_db(
        board_id, slot_config, lab_slot_config
    )

    if not sections:
        return {"status": "empty"}

    rng = random.Random(seed)
    score_before = _compute_balance_score(schedule, sections, online_codes, overlap_matrix)
    best_score = score_before
    best_schedule = {k: [m.copy() for m in v] for k, v in schedule.items()}

    # Build valid options per meeting
    valid_opts: dict[tuple[int, int], list[dict]] = {}
    for i, meetings in schedule.items():  # noqa: B007
        for m_idx, m in enumerate(meetings):
            duration = _to_min(m["end"]) - _to_min(m["start"])
            opts = []
            for day in WEEKDAYS:
                if duration <= 75:
                    for si, s in enumerate(slot_config):
                        if _start_is_blocked(s["start"]):
                            continue
                        opts.append(
                            {
                                "day": day,
                                "slot_idx": si,
                                "start": s["start"],
                                "end": s["end"],
                                "mask": _time_mask(day, s["start"], s["end"]),
                            }
                        )
                else:
                    # Lab (100-min): use dedicated lab slot grid
                    for si, s in enumerate(lab_slot_config):
                        if _start_is_blocked(s["start"]):
                            continue
                        opts.append(
                            {
                                "day": day,
                                "slot_idx": si,
                                "start": s["start"],
                                "end": s["end"],
                                "mask": _time_mask(day, s["start"], s["end"]),
                            }
                        )
            valid_opts[(i, m_idx)] = opts

    # Iterative improvement
    current_score = score_before
    iterations = 0
    improvements = 0
    start_time = time.time()
    temperature = 300.0

    while time.time() - start_time < max_seconds:
        iterations += 1
        temperature *= 0.9995

        # Pick random section and meeting
        sec_idx = rng.randint(0, len(sections) - 1)
        meetings = schedule[sec_idx]
        if not meetings:
            continue
        m_idx = rng.randint(0, len(meetings) - 1)

        opts = valid_opts.get((sec_idx, m_idx), [])
        if not opts:
            continue

        new_opt = rng.choice(opts)
        old = meetings[m_idx].copy()

        # Apply move
        meetings[m_idx] = {**new_opt, "placement_id": old.get("placement_id")}

        if not _check_constraints(schedule, sections, overlap_matrix):
            meetings[m_idx] = old
            continue

        new_score = _compute_balance_score(schedule, sections, online_codes, overlap_matrix)
        delta = new_score - current_score

        import math

        if delta < 0 or (temperature > 0.01 and rng.random() < math.exp(-delta / temperature)):
            current_score = new_score
            if delta < 0:
                improvements += 1
            if new_score < best_score:
                best_score = new_score
                best_schedule = {k: [m.copy() for m in v] for k, v in schedule.items()}
        else:
            meetings[m_idx] = old

    # Compute final daily loads
    schedule.update(best_schedule)
    loads_after = _daily_load(schedule, sections, online_codes, overlap_matrix)

    return {
        "status": "optimized",
        "score_before": score_before,
        "score_after": best_score,
        "iterations": iterations,
        "improvements": improvements,
        "daily_loads": loads_after,
        "schedule": best_schedule,
        "sections": sections,
    }


def rebalance_and_persist_board(board_id: int, max_seconds: float = 8.0) -> dict:
    """Rebalance and persist to database."""
    result = rebalance_board(board_id, max_seconds=max_seconds)
    if result["status"] != "optimized":
        return result

    schedule = result["schedule"]
    sections = result["sections"]

    try:
        board = DeliveryBoard.objects.get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return result

    # Delete and recreate
    auto = SectionPlacement.objects.filter(board=board, term_section__source_tag="tw_auto")
    ts_ids = set(auto.values_list("term_section_id", flat=True))
    auto.delete()
    for ts_id in ts_ids:
        TermSectionMeeting.objects.filter(term_section_id=ts_id).delete()

    for i, meetings in schedule.items():
        sec = sections[i]
        try:
            ts = TermSection.objects.get(id=sec["term_section_id"])
        except TermSection.DoesNotExist:
            continue
        for m in meetings:
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

    # Assign rooms after re-persisting
    from core.services.timetable_rooming import assign_rooms_to_board

    assign_rooms_to_board(board_id)

    return result


def rebalance_scenario(scenario_id: int, max_seconds_per_board: float = 5.0) -> dict:
    """Rebalance all boards in a scenario."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    results = {}
    for board in boards:
        r = rebalance_and_persist_board(board.id, max_seconds=max_seconds_per_board)
        results[board.label] = r
    return {"boards": results}
