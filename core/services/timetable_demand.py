"""
core/services/timetable_demand.py
Board-level demand and capacity computation for the Timetable Workspace.

Bridges ``reporting.build_aggregate_counts()`` and section-planning capacity
rules to produce per-course demand vs raw capacity for a delivery board.

Workflow
--------
1. ``compute_board_demand()`` resolves which courses belong to a board's
   nominal term / programme and counts how many students need each course
   (via the batch recommender aggregation pipeline).
2. ``compute_board_capacity()`` pairs that demand with the raw seat capacity
   of sections already placed on the board, producing a deficit figure that
   drives the UI's "capacity gap" indicators.

Both functions are called from the timetable workspace API layer
(``timetable_workspace_views.py``) and the XLSX export pipeline.
"""

from __future__ import annotations

from collections import Counter

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    SectionPlacement,
)


def compute_board_demand(board: DeliveryBoard) -> Counter[str]:
    """Compute course demand (student counts) relevant to a delivery board.

    Delegates to ``reporting.build_aggregate_counts`` to run the batch
    recommender for the board's academic year, term, and programme.  If the
    board has a ``nominal_term`` set, the result is filtered to only those
    courses that appear in ``ProgrammeRequirement`` for that term level.

    Parameters
    ----------
    board : DeliveryBoard
        The board whose demand should be computed.  Must have a related
        ``scenario`` with ``academic_year`` and ``term`` populated.

    Returns
    -------
    Counter[str]
        Mapping of course_code -> number of students who need that course.
        If no ``nominal_term`` is set, returns the *full* unfiltered aggregate.
    """
    # Lazy import to avoid circular dependency with reporting module
    from core.services.reporting import build_aggregate_counts

    year = int(board.scenario.academic_year) if board.scenario.academic_year else 0
    term = int(board.scenario.term) if board.scenario.term else 0

    program = board.program or None
    student_count, full_aggregate = build_aggregate_counts(year, term, program=program)

    # No nominal_term means this board covers all terms — return everything
    if not board.nominal_term:
        return full_aggregate

    # Filter to courses that belong to the board's nominal_term in the
    # programme requirements table (e.g. only Term 3 courses for a Term 3 board)
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
    """Compute demand vs raw capacity for every course on a board.

    Merges two data sources:
    * **Demand** — how many students need each course (from the recommender).
    * **Capacity** — how many seats are available from sections already placed
      on this board (summing ``TermSection.available_capacity``).

    The *deficit* for a course is ``max(0, demand - raw_capacity)`` when
    demand > 0, indicating how many additional seats are needed.

    Parameters
    ----------
    board_id : int
        Primary key of the ``DeliveryBoard``.

    Returns
    -------
    list[dict]
        Each dict contains: ``course_code``, ``course_name``, ``demand``,
        ``raw_capacity``, ``placed_sections``, ``deficit``.
        Sorted by deficit descending, then demand descending, so the
        biggest capacity gaps appear first.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return []

    demand = compute_board_demand(board)

    # ── Step 1: Get placed sections on this board ────────────────
    placements = SectionPlacement.objects.filter(board_id=board_id).select_related("term_section")

    # ── Step 2: Aggregate raw capacity per course from placed sections ──
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

    # ── Step 3: Merge demand and capacity into a unified result ───
    # Include courses that appear in *either* set — a course may have
    # demand but no placed sections, or be placed but have zero demand.
    all_codes = set(demand.keys()) | set(course_capacity.keys())
    result = []
    for code in sorted(all_codes):
        d = demand.get(code, 0)
        cap_info = course_capacity.get(code, {})
        raw = cap_info.get("raw_capacity", 0)
        placed = cap_info.get("placed_sections", 0)
        name = cap_info.get("course_name", "")

        # Fall back to Course table for the display name when a course
        # appears only in demand (no placement to read the name from)
        if not name:
            try:
                name = Course.objects.get(course_code=code).description or code
            except Course.DoesNotExist:
                name = code

        deficit = max(0, d - raw) if d > 0 else 0

        result.append(
            {
                "course_code": code,
                "course_name": name,
                "demand": d,
                "raw_capacity": raw,
                "placed_sections": placed,
                "deficit": deficit,
            }
        )

    # Sort by deficit descending so biggest gaps show first
    result.sort(key=lambda x: (-x["deficit"], -x["demand"], x["course_code"]))
    return result
