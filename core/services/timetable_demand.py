"""
core/services/timetable_demand.py
Board-level demand and capacity computation for the Timetable Workspace.

Uses the scenario's canonical per-student course request rows as the demand source,
then pairs that scenario demand with the raw seat capacity of sections already
placed on a delivery board.

Workflow
--------
1. ``compute_board_demand()`` resolves which scenario-recommended courses are
   relevant to a board's nominal term or are already placed on that board.
2. ``compute_board_capacity()`` pairs that demand with the raw seat capacity
   of sections already placed on the board, producing a deficit figure that
   drives the UI's "capacity gap" indicators.

Both functions are called from the timetable workspace API layer
(``timetable_workspace_views.py``) and the XLSX export pipeline.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TimetableScenario,
)
from core.services.student_helpers import normalize_code

ACTIVE_DEMAND_STATUSES = frozenset(
    {
        ScenarioStudentCourseRequest.STATUS_REQUESTED,
        ScenarioStudentCourseRequest.STATUS_BLOCKED,
        ScenarioStudentCourseRequest.STATUS_SERVED,
    }
)


@dataclass(frozen=True)
class StudentCourseDemand:
    """Canonical in-memory shape for a student's course request/demand."""

    scenario_id: int
    student_id: int
    course_key: str
    course_code: str
    course_name: str = ""
    primary_term: int | None = None
    is_cross_term: bool = False
    status: str = ScenarioStudentCourseRequest.STATUS_REQUESTED
    priority: str = ScenarioStudentCourseRequest.PRIORITY_NORMAL
    reason_blocked: str = ""
    reason_detail: str = ""
    source: str = "scenario_student_course_request"
    source_payload: dict[str, object] = field(default_factory=dict)


def _course_code(value: object) -> str:
    """Return the display course code from a recommendation key/code."""

    return str(value or "").split("::", 1)[0].strip()


def _course_key(value: object) -> str:
    """Return the stable scheduling key from a recommendation key/code."""

    return str(value or "").strip()


def _normalise_course_filter(course_keys: Iterable[str] | None) -> set[str]:
    return {normalize_code(course_key) for course_key in (course_keys or []) if course_key}


def load_scenario_course_demands(
    scenario_id: int,
    *,
    course_keys: Iterable[str] | None = None,
    statuses: Iterable[str] | None = None,
) -> list[StudentCourseDemand]:
    """Load canonical per-student course demand for a scenario."""

    course_filter = _normalise_course_filter(course_keys)
    active_statuses = set(statuses or ACTIVE_DEMAND_STATUSES)
    rows = (
        ScenarioStudentCourseRequest.objects.filter(
            scenario_id=scenario_id,
            status__in=active_statuses,
        )
        .order_by("student_id", "course_key")
        .values(
            "student_id",
            "course_key",
            "course_code",
            "course_name",
            "primary_term",
            "is_cross_term",
            "status",
            "priority",
            "reason_blocked",
            "reason_detail",
            "source",
            "source_payload",
        )
    )
    out = []
    for row in rows:
        course_key = _course_key(row["course_key"])
        if course_filter and normalize_code(course_key) not in course_filter:
            continue
        out.append(
            StudentCourseDemand(
                scenario_id=scenario_id,
                student_id=int(row["student_id"]),
                course_key=course_key,
                course_code=str(row.get("course_code") or _course_code(course_key)),
                course_name=str(row.get("course_name") or ""),
                primary_term=row.get("primary_term"),
                is_cross_term=bool(row.get("is_cross_term")),
                status=str(row.get("status") or ScenarioStudentCourseRequest.STATUS_REQUESTED),
                priority=str(row.get("priority") or ScenarioStudentCourseRequest.PRIORITY_NORMAL),
                reason_blocked=str(row.get("reason_blocked") or ""),
                reason_detail=str(row.get("reason_detail") or ""),
                source=str(row.get("source") or "scenario_student_course_request"),
                source_payload=dict(row.get("source_payload") or {}),
            )
        )
    return out


def compute_course_students_map(
    scenario_id: int,
    course_keys: Iterable[str],
) -> dict[str, set[int]]:
    """Return ``{normalised_course_key: student_ids}`` from canonical demand."""

    wanted = _normalise_course_filter(course_keys)
    if not wanted:
        return {}
    course_students: dict[str, set[int]] = {}
    for row in load_scenario_course_demands(scenario_id, course_keys=wanted):
        key = normalize_code(row.course_key)
        if key in wanted:
            course_students.setdefault(key, set()).add(row.student_id)
    return course_students


def load_student_course_demand_map(
    scenario_id: int,
    *,
    student_ids: Iterable[int] | None = None,
    course_keys: Iterable[str] | None = None,
) -> dict[int, list[StudentCourseDemand]]:
    """Return canonical demand rows grouped by student id."""

    wanted_students = {int(student_id) for student_id in (student_ids or [])}
    grouped: dict[int, list[StudentCourseDemand]] = {}
    for row in load_scenario_course_demands(scenario_id, course_keys=course_keys):
        if wanted_students and row.student_id not in wanted_students:
            continue
        grouped.setdefault(row.student_id, []).append(row)
    return grouped


def compute_course_students_index(
    scenario_id: int,
    *,
    course_keys: Iterable[str] | None = None,
    student_ids: Iterable[int] | None = None,
) -> dict[str, set[int]]:
    """Return a raw+normalised course-key demand index.

    This intentionally indexes the stable planner ``course_key`` only.  It
    does not alias to display ``course_code`` because duplicate visible codes
    can represent different planner identities.
    """

    wanted_students = {int(student_id) for student_id in (student_ids or [])}
    index: dict[str, set[int]] = {}
    for row in load_scenario_course_demands(scenario_id, course_keys=course_keys):
        if wanted_students and row.student_id not in wanted_students:
            continue
        raw_key = _course_key(row.course_key)
        norm_key = normalize_code(raw_key)
        for key in {raw_key, norm_key}:
            if key:
                index.setdefault(key, set()).add(row.student_id)
    return index


def sync_scenario_student_course_requests(
    *,
    scenario: TimetableScenario,
    classified_students: list[dict],
    student_course_keys: dict[int, list[str]],
    source: str = "batch_recommender",
) -> int:
    """Persist canonical per-course request rows for a generated scenario."""

    objects: list[ScenarioStudentCourseRequest] = []
    for student in classified_students:
        student_id = int(student["student_id"])
        course_keys = list(
            student_course_keys.get(student_id) or student.get("recommended_courses") or []
        )
        course_codes = list(student.get("recommended_courses") or [])
        seen_for_student: set[str] = set()
        for index, raw_course_key in enumerate(course_keys):
            course_key = _course_key(raw_course_key)
            if not course_key or course_key in seen_for_student:
                continue
            seen_for_student.add(course_key)
            display_code = course_codes[index] if index < len(course_codes) else course_key
            objects.append(
                ScenarioStudentCourseRequest(
                    scenario=scenario,
                    student_id=student_id,
                    course_key=course_key,
                    course_code=_course_code(display_code or course_key),
                    course_name="",
                    primary_term=student.get("primary_term"),
                    is_cross_term=bool(student.get("is_cross_term")),
                    status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                    priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                    source=source,
                    source_payload={
                        "recommended_course_display": display_code,
                    },
                )
            )
    ScenarioStudentCourseRequest.objects.bulk_create(
        objects, ignore_conflicts=True, batch_size=1000
    )
    return len(objects)


def compute_scenario_course_demand(scenario_id: int) -> Counter[str]:
    """Count unique scenario students needing each recommended course."""

    demand: Counter[str] = Counter()
    by_student: dict[int, set[str]] = {}
    for row in load_scenario_course_demands(scenario_id):
        by_student.setdefault(row.student_id, set()).add(row.course_key)
    for course_keys in by_student.values():
        demand.update(course_keys)
    return demand


def compute_board_demand(board: DeliveryBoard) -> Counter[str]:
    """Compute course demand (student counts) relevant to a delivery board.

    Reads canonical scenario request rows. If the board has a ``nominal_term``
    set, the result is filtered to courses planned for that term plus courses
    already placed on the board.

    Parameters
    ----------
    board : DeliveryBoard
        The board whose demand should be computed.

    Returns
    -------
    Counter[str]
        Mapping of course_key -> unique scenario students who need that
        course. If no ``nominal_term`` is set, returns the full scenario
        demand.
    """
    scenario_demand = compute_scenario_course_demand(board.scenario_id)

    # No nominal_term means this board covers all terms — return everything
    if not board.nominal_term:
        return scenario_demand

    # Prefer scenario budgets because generated scenarios can represent
    # combined programmes such as "DS,AI,AI2,DS2" that do not map cleanly to
    # one ProgrammeRequirement.program value.
    relevant_keys = {
        row[0] or row[1]
        for row in ScenarioSectionBudget.objects.filter(
            scenario_id=board.scenario_id,
            programme_term=board.nominal_term,
        ).values_list("course_key", "course_code")
        if row[0] or row[1]
    }

    # Include off-term/shared courses already placed on this board so their
    # capacity is compared against real scenario demand instead of zero.
    relevant_keys.update(
        row[0] or row[1]
        for row in SectionPlacement.objects.filter(board=board).values_list(
            "term_section__course_key",
            "term_section__course_code",
        )
        if row[0] or row[1]
    )

    relevant_codes: set[str] = set()
    if not relevant_keys:
        relevant_codes = set(
            ProgrammeRequirement.objects.filter(
                programme_term=board.nominal_term,
                **({"program": board.program} if board.program else {}),
            ).values_list("course_code", flat=True)
        )

    if not relevant_keys and not relevant_codes:
        return scenario_demand

    return Counter(
        {
            key: count
            for key, count in scenario_demand.items()
            if key in relevant_keys or _course_code(key) in relevant_codes
        }
    )


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
    budget_meta = {
        row[0] or row[1]: {
            "course_key": row[0] or row[1],
            "course_code": row[1],
            "course_name": row[2],
        }
        for row in ScenarioSectionBudget.objects.filter(scenario_id=board.scenario_id).values_list(
            "course_key",
            "course_code",
            "course_name",
        )
        if row[0] or row[1]
    }

    # ── Step 1: Get placed sections on this board ────────────────
    placements = SectionPlacement.objects.filter(board_id=board_id).select_related("term_section")

    # ── Step 2: Aggregate raw capacity per course from placed sections ──
    course_capacity: dict[str, dict] = {}
    counted_sections: set[int] = set()
    for p in placements:
        if p.term_section_id in counted_sections:
            continue
        counted_sections.add(p.term_section_id)
        key = p.term_section.course_key or p.term_section.course_code
        if key not in course_capacity:
            course_capacity[key] = {
                "course_key": key,
                "course_code": p.term_section.course_code,
                "course_name": p.term_section.course_name,
                "raw_capacity": 0,
                "placed_sections": 0,
            }
        cap = p.term_section.available_capacity or 0
        course_capacity[key]["raw_capacity"] += cap
        course_capacity[key]["placed_sections"] += 1

    # ── Step 3: Merge demand and capacity into a unified result ───
    # Include courses that appear in *either* set — a course may have
    # demand but no placed sections, or be placed but have zero demand.
    all_keys = set(demand.keys()) | set(course_capacity.keys())
    result = []
    for key in sorted(all_keys):
        d = demand.get(key, 0)
        cap_info = course_capacity.get(key, {})
        meta = budget_meta.get(key, {})
        raw = cap_info.get("raw_capacity", 0)
        placed = cap_info.get("placed_sections", 0)
        code = cap_info.get("course_code") or meta.get("course_code") or _course_code(key)
        name = cap_info.get("course_name") or meta.get("course_name") or ""

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
                "course_key": key,
                "course_code": code,
                "course_name": name,
                "demand": d,
                "raw_capacity": raw,
                "placed_sections": placed,
                "deficit": deficit,
            }
        )

    # Sort by deficit descending so biggest gaps show first
    result.sort(key=lambda x: (-x["deficit"], -x["demand"], x["course_code"], x["course_name"]))
    return result
