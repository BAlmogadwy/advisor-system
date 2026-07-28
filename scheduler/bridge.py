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


#: Over this many minutes, both the workbook export and the on-screen grid read a
#: meeting as a lab and look for it in the lab columns. Not a choice made here —
#: it is the existing convention on both surfaces, named so the dependency is
#: visible rather than implied.
LAB_DURATION_MINUTES = 80


def grid_columns_for(
    board, lecture_config: list | None, lab_config: list | None
) -> tuple[list, list, int, int]:
    """Column definitions wide enough to hold every placement on this board.

    Returns ``(lecture_columns, lab_columns, lecture_added, lab_added)``.

    Derived from the finished board rather than from any grid definition, so
    every placement has a column **by construction** — including times a grid
    written for a different engine never anticipated. Existing columns are always
    kept: this only ever adds, so a scenario never loses a column something else
    was relying on.
    """
    short_windows: set[tuple[str, str]] = set()
    long_windows: set[tuple[str, str]] = set()
    for placement in board.placements:
        pair = (_hhmm(placement.window.start), _hhmm(placement.window.end))
        if placement.window.duration > LAB_DURATION_MINUTES:
            long_windows.add(pair)
        else:
            short_windows.add(pair)

    def merge(existing: list | None, needed: set[tuple[str, str]]) -> tuple[list, int]:
        have = {(row.get("start"), row.get("end")) for row in (existing or [])}
        missing = sorted(needed - have)
        if not missing:
            return list(existing or []), 0
        merged = list(existing or []) + [
            {"label": f"{start}-{end}", "start": start, "end": end} for start, end in missing
        ]
        merged.sort(key=lambda row: str(row.get("start")))
        return merged, len(missing)

    lecture_columns, lecture_added = merge(lecture_config, short_windows)
    lab_columns, lab_added = merge(lab_config, long_windows)
    return lecture_columns, lab_columns, lecture_added, lab_added


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
    from core.services.course_identity import planner_course_key

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

    # ── make the scenario declare the grid this board actually uses ────────
    #
    # The workbook export and the on-screen grid both build their columns from
    # `scenario.slot_config` / `lab_slot_config` and match a placement by its
    # exact start time. A meeting whose start has no column is simply not drawn:
    # it vanishes from the export without an error, which is the worst way for
    # data to be wrong.
    #
    # That is what happened. Online classes sit in their own late family — 15:00,
    # 16:45 and 18:30 (D9) — which the original engine has no notion of, so 30 of
    # 644 placements had nowhere to go. Rather than teach the scheduler to avoid
    # times the old grid happens to list, the scenario is told what its own
    # timetable uses. `slot_config` is per-scenario precisely so it can differ.
    #
    # Derived from the finished board rather than from the grid definition, so it
    # stays correct if the grid ever changes: every placement gets a column by
    # construction. Existing columns are kept — this only ever adds.
    #
    # The split is by DURATION, because that is how both the export and the grid
    # decide which table a meeting belongs to (over 80 minutes reads as a lab).
    new_lecture, new_lab, added_lecture, added_lab = grid_columns_for(
        board, scenario.slot_config, scenario.lab_slot_config
    )
    if added_lecture or added_lab:
        scenario.slot_config = new_lecture
        scenario.lab_slot_config = new_lab
        scenario.save(update_fields=["slot_config", "lab_slot_config"])

    # Instructor names, so the assignment survives into the export.
    #
    # This subsystem decides who teaches what, proves each instructor sits on
    # their own minimum number of working days, and reports their idle time — and
    # then wrote an empty string here, so the workbook's Instructors sheet had
    # nothing to render and skipped itself entirely. The most carefully optimised
    # part of the result was invisible to the only people who read the output.
    instructor_names = {i.id: i.name for i in snapshot.instructors}

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

            # `course_key` is the planner's IDENTITY, not the display code. The
            # project's own rule (N1): a course code is never an identifier —
            # `FE1` and `CS111` are each two different courses with different
            # demand, so planning keys on `CODE::NORMALISED_NAME` and collapsing
            # them would merge two courses into one budget.
            #
            # Writing the bare code here broke the per-plan export outright: it
            # joins sections to `ProgrammeRequirement` rows on that identity, so
            # all 71 sections matched nothing and every course read as missing
            # from its plan. Same rule the subsystem states in its own blueprint,
            # violated at the one place it had to cross over.
            term_section = TermSection.objects.create(
                scenario=scenario,
                course_key=planner_course_key(offering.course_code, offering.course_name),
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
                    defaults={
                        "room": placement.room_id or "",
                        # Fanned into EVERY meeting of the section, which is what
                        # the Instructors sheet expects: it reads the first
                        # non-empty name per section and assumes the rest agree.
                        "instructor": instructor_names.get(placement.instructor_id, ""),
                    },
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
        "slot_columns_added": added_lecture + added_lab,
        "meetings_with_an_instructor": sum(
            1 for p in board.placements if p.instructor_id is not None
        ),
        "notes": list(result.notes),
    }
