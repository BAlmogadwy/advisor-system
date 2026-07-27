"""Putting a `scheduler` board into a workspace scenario the rest of the app can see.

The subsystem was built deliberately isolated: its own tables, no imports from
the old engine, nothing of its own written into `core`. That isolation is what
made it safe to build a second timetable engine beside a working one. It is also
why, until now, nothing it produced could be looked at outside a terminal.

This module is the seam, and it is narrow on purpose.

**It writes only into a scenario it was given.** The caller creates the scenario
scaffold with the project's own `generate_workspace_scenario(run_autoplace=False)`
— same students, same boards, same section budgets as the existing engine would
produce — and this fills in the placements instead of `auto_place_scenario`. So
the two engines differ in exactly one respect, which is the interesting one: where
the classes go. Everything upstream is shared, and every comparison between them
is therefore a comparison of scheduling rather than of bookkeeping.

**It never touches a scenario it did not create.** Rows are tagged
``source_tag="tw_scheduler"`` so they are always distinguishable from the old
engine's ``tw_auto``.

**A section is placed on every board whose students need it, at one time.** That
is the shared-section model this subsystem exists to get right: a section has ONE
schedule and appears on several boards, rather than being duplicated per board
and drifting into two different times at once.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from scheduler.instructors import assign_instructors
from scheduler.intake import build_snapshot
from scheduler.rooms import assign_rooms_exact, room_shortfall
from scheduler.solve import (
    expected_clashes,
    instructor_metrics,
    plan,
    plan_portfolio,
    sibling_adjacency,
)
from scheduler.validate import validate

SOURCE_TAG = "tw_scheduler"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build_into_scenario(
    scenario_id: int,
    *,
    academic_year: str,
    term: int,
    programs: list[str],
    gender: str,
    seconds: float = 120.0,
    runs: int = 1,
    clash_tolerance: float = 0.20,
    default_capacity: int = 25,
    progress=None,
) -> dict:
    """Plan this scenario with the new engine and persist the result.

    Returns a summary the planner-job layer can store and the UI can show. Every
    figure is reported beside the floor it should be read against, because "18
    working days" says nothing without "and 18 was the proven minimum".
    """
    from core.models import (
        BoardStudentLink,
        DeliveryBoard,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )

    scenario = TimetableScenario.objects.get(id=scenario_id)

    if progress:
        progress("snapshot")
    snapshot = assign_instructors(
        build_snapshot(
            academic_year=str(academic_year),
            term=int(term),
            gender=str(gender),
            programs=list(programs),
            default_capacity=int(default_capacity),
        )
    )

    if progress:
        progress("solve")
    if runs > 1:
        result = plan_portfolio(
            snapshot,
            seeds=tuple(range(1, runs + 1)),
            time_limit_seconds=seconds,
            clash_tolerance=clash_tolerance,
        )
    else:
        result = plan(snapshot, time_limit_seconds=seconds, clash_tolerance=clash_tolerance)

    if not result.board.placements:
        # An empty board scores perfectly on every metric — no meetings means no
        # clashes and nothing unroomed — so it must never be written or reported
        # as a result. Readiness explains why: usually a course planning more
        # sections than the week has non-overlapping slots to hold them.
        raise RuntimeError(
            f"the scheduler produced no timetable ({result.status}). " + " ".join(result.notes)
        )

    if progress:
        progress("rooming")
    board = assign_rooms_exact(snapshot, result.board)

    # Which boards need which course: a board needs it when one of its own
    # students does. Using the students rather than the term budgets keeps
    # cross-term courses working without duplicating the old engine's heuristic
    # for choosing a single "best" board.
    students_of_board: dict[int, set[int]] = {}
    for board_row in DeliveryBoard.objects.filter(scenario=scenario):
        students_of_board[board_row.id] = set(
            BoardStudentLink.objects.filter(board=board_row).values_list("student_id", flat=True)
        )
    wanted_by_student: dict[int, set[str]] = defaultdict(set)
    for demand in snapshot.demand:
        wanted_by_student[int(demand.student_id)] |= set(demand.offering_ids)

    boards_for_offering: dict[str, set[int]] = defaultdict(set)
    for board_id, student_ids in students_of_board.items():
        for student_id in student_ids:
            for offering_id in wanted_by_student.get(int(student_id), ()):
                boards_for_offering[offering_id].add(board_id)

    offerings = snapshot.offerings_by_id
    sections = {s.id: s for s in snapshot.sections}
    by_section: dict[str, list] = defaultdict(list)
    for placement in board.placements:
        by_section[placement.section_id].append(placement)

    if progress:
        progress("persist")
    written = orphaned = 0
    with transaction.atomic():
        # Replace only what this engine put there before; the old engine's rows
        # and any hand-made edits are left untouched.
        TermSection.objects.filter(scenario=scenario, source_tag=SOURCE_TAG).delete()

        for section_id, placements in sorted(by_section.items()):
            section = sections.get(section_id)
            offering = offerings.get(section.offering_id) if section else None
            if section is None or offering is None:
                continue
            target_boards = boards_for_offering.get(offering.id) or set()
            if not target_boards:
                orphaned += 1
                continue

            term_section = TermSection.objects.create(
                scenario=scenario,
                course_key=offering.course_code,
                course_code=offering.course_code,
                course_number=offering.course_code,
                course_name=offering.course_name,
                section=section.label,
                available_capacity=section.capacity,
                source_tag=SOURCE_TAG,
            )
            for placement in sorted(placements, key=lambda p: (p.day.index, p.window.start)):
                start, end = _hhmm(placement.window.start), _hhmm(placement.window.end)
                TermSectionMeeting.objects.get_or_create(
                    term_section=term_section,
                    day=placement.day.value,
                    start_time=start,
                    end_time=end,
                    defaults={"room": placement.room_id or "", "instructor": ""},
                )
                for board_id in sorted(target_boards):
                    SectionPlacement.objects.get_or_create(
                        board_id=board_id,
                        term_section=term_section,
                        day=placement.day.value,
                        start_time=start,
                        defaults={
                            "end_time": end,
                            "room": placement.room_id or "UNASSIGNED",
                        },
                    )
                written += 1

    report = validate(snapshot, board)
    rooms = room_shortfall(snapshot, board)
    instructors = instructor_metrics(snapshot, board)
    pairing = sibling_adjacency(snapshot, board)

    return {
        "engine": "scheduler",
        "scenario_id": scenario.id,
        "sections": len(snapshot.sections),
        "meetings_written": written,
        "meetings_without_a_board": orphaned,
        "solver_status": result.status,
        "wall_time_seconds": round(result.wall_time_seconds, 1),
        "violations": report.violation_count,
        "certification": report.certification.value,
        "expected_clashes": round(expected_clashes(snapshot, board), 1),
        "instructor_days": instructors["working_days"],
        "instructor_days_floor": instructors["floor_days"],
        "instructor_idle_minutes": instructors["idle_minutes"],
        "unroomed": rooms["unroomed"],
        "unroomed_floor": rooms["impossible"] + rooms["saturated"],
        "sibling_pairs_back_to_back": pairing["pairs_back_to_back"],
        "sibling_pairs_achievable": pairing["pairs_achievable"],
        "notes": list(result.notes),
    }
