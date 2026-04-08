"""
core/services/timetable_demand.py
Board-level demand and capacity computation for the Timetable Workspace.

Bridges reporting.build_aggregate_counts() and section_planning capacity rules
to produce per-course demand vs raw capacity for a delivery board.
"""

from __future__ import annotations

from collections import Counter

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    SectionPlacement,
    TermSection,
)


def compute_board_demand(board: DeliveryBoard) -> Counter[str]:
    """Compute course demand relevant to a delivery board.

    Uses ProgrammeRequirement to find courses for the board's nominal_term/program,
    then counts students who need those courses from StudentCourse status='studying'.
    Falls back to aggregate recommendation counts if available.
    """
    from core.services.reporting import build_aggregate_counts

    year = int(board.scenario.academic_year) if board.scenario.academic_year else 0
    term = int(board.scenario.term) if board.scenario.term else 0

    program = board.program or None
    student_count, full_aggregate = build_aggregate_counts(year, term, program=program)

    if not board.nominal_term:
        return full_aggregate

    # Filter to courses that belong to the board's nominal_term
    relevant_codes = set(
        ProgrammeRequirement.objects.filter(
            programme_term=board.nominal_term,
            **({"program": board.program} if board.program else {}),
        ).values_list("course_code", flat=True)
    )

    if not relevant_codes:
        return full_aggregate

    filtered = Counter({k: v for k, v in full_aggregate.items() if k in relevant_codes})
    return filtered


def compute_board_capacity(board_id: int) -> list[dict]:
    """Compute demand vs raw capacity for each course on a board.

    Returns list of dicts:
        [{course_code, course_name, demand, raw_capacity, placed_sections, deficit}]
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return []

    demand = compute_board_demand(board)

    # Get placed sections on this board
    placements = (
        SectionPlacement.objects.filter(board_id=board_id)
        .select_related("term_section")
    )

    # Aggregate raw capacity per course from placed sections
    course_capacity: dict[str, dict] = {}
    for p in placements:
        code = p.term_section.course_code
        if code not in course_capacity:
            course_capacity[code] = {
                "course_code": code,
                "course_name": p.term_section.course_name,
                "raw_capacity": 0,
                "placed_sections": 0,
            }
        cap = p.term_section.available_capacity or 0
        course_capacity[code]["raw_capacity"] += cap
        course_capacity[code]["placed_sections"] += 1

    # Merge demand and capacity
    all_codes = set(demand.keys()) | set(course_capacity.keys())
    result = []
    for code in sorted(all_codes):
        d = demand.get(code, 0)
        cap_info = course_capacity.get(code, {})
        raw = cap_info.get("raw_capacity", 0)
        placed = cap_info.get("placed_sections", 0)
        name = cap_info.get("course_name", "")

        # Try to get course name from Course table if not from placement
        if not name:
            try:
                name = Course.objects.get(course_code=code).description or code
            except Course.DoesNotExist:
                name = code

        deficit = max(0, d - raw) if d > 0 else 0

        result.append({
            "course_code": code,
            "course_name": name,
            "demand": d,
            "raw_capacity": raw,
            "placed_sections": placed,
            "deficit": deficit,
        })

    # Sort by deficit descending so biggest gaps show first
    result.sort(key=lambda x: (-x["deficit"], -x["demand"], x["course_code"]))
    return result
