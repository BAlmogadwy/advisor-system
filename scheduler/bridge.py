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

**A section is placed on exactly ONE board**, chosen by `choose_board`. Boards are
how the workbook and the screen segment the timetable, and both render one cell
per placement row — so a course written to every board whose students needed it
was drawn once per board in the same square. The section still has one schedule;
that was never the part boards duplicated.
"""

from __future__ import annotations

from collections import Counter, defaultdict

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
    time_of_day_drift,
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


def choose_board(
    offering,
    *,
    plan_term_of_key: dict[str, int],
    board_of_term: dict[int, int],
    headcount: dict[int, int] | None = None,
) -> int | None:
    """The ONE board a course belongs on, and why.

    Boards are how the workbook and the screen segment the timetable, and both
    render a cell per placement row — so a course written to several boards is
    drawn several times in the same cell. Exactly one, therefore, in preference
    order:

    1. **the section budget's plan term** — the scenario's own answer, and where
       a registrar expects to find the course;
    2. **the programme plan itself.** Some budget rows carry no plan term at all
       (CHEM101's is null), and guessing from headcount then put a first-term
       course on the Term 7 board, where the coverage sheet rightly called it
       misplaced. The offering already knows which terms its plan rows name;
       the earliest is where a student meets it first;
    3. **the board with the most students who need it** — the same tie-break the
       existing engine uses for genuinely cross-term courses, with a stable id
       so two runs of the same data agree.

    Returns ``None`` when no board can hold it, which the caller reports rather
    than silently dropping.
    """
    from core.services.course_identity import planner_course_key

    key = planner_course_key(offering.course_code, offering.course_name)
    term = plan_term_of_key.get(key.upper())
    if term is not None and term in board_of_term:
        return board_of_term[term]

    candidates = sorted(t for t in offering.terms if t and t in board_of_term)
    if candidates:
        return board_of_term[candidates[0]]

    if headcount:
        return min(headcount.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return None


def build_into_scenario(
    scenario_id: int,
    *,
    academic_year: str,
    term: int,
    programs: list[str],
    gender: str,
    seconds: float = 120.0,
    # THREE, not one. Measured at equal total compute on the male cohort --
    # 3 x 200s against 1 x 600s, two repetitions each:
    #
    #                     instructor idle   T1 clashes   students clash-free
    #   one long run        1540 / 2570     3.5 / 4.2       98.7 / 98.7 %
    #   three, keep best    1420 / 1900     3.2 / 4.0       99.5 / 99.5 %
    #
    # The clash-free figures do not overlap: two students affected instead of
    # five, on both repetitions. Instructor idle is better and tighter, and the
    # single-run arm shows why -- 1540 and 2570 from nothing but the seed.
    #
    # This objective has no usable lower bound (N8), so optimality is
    # unprovable and a single run is a lottery ticket. Variance is the
    # phenomenon; harvesting it beats hoping for a good draw. More time alone
    # does NOT do this -- measured at 120/300/600s it widened the spread rather
    # than improving the median.
    runs: int = 3,
    clash_tolerance: float = 0.20,
    default_capacity: int = 25,
    #: Owner rule (D18): a course fewer than this many students want is withheld
    #: from the board and taken elsewhere in the college. The policy default
    #: lives here, at the caller, exactly as `default_capacity` does — intake
    #: itself defaults to 1 (rule off) so no test silently inherits a filter.
    min_demand: int = 5,
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
        ScenarioSectionBudget,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.course_identity import planner_course_key

    scenario = TimetableScenario.objects.get(id=scenario_id)

    # ── two things this must refuse to do, rather than do quietly ──────────
    #
    # 1. A snapshot is single-gender (D1): students, rooms and instructor links
    #    are all filtered to one gender at intake. A scenario scaffolded with no
    #    section is an ALL-gender scenario — its boards, budgets and student
    #    links cover everybody — so filling it from a single-gender snapshot
    #    would write a timetable derived from part of its own demand, sized
    #    against a budget for all of it, and report success. On the live data
    #    that is 1606 male students planned into a scenario built for 4004.
    scenario_gender = str(scenario.gender or "").strip().upper()
    asked = str(gender).strip().upper()
    if scenario_gender != asked:
        raise RuntimeError(
            f"this scenario is for {scenario_gender or 'ALL genders'} but the build "
            f"was asked for {asked}. A timetable is single-gender by construction "
            "(D1), so filling it would cover only part of the scenario's own "
            "students while its section budget counts all of them. Re-generate "
            f"with the section set to {asked}."
        )

    # 2. A locked placement is the user's hard constraint — the existing engine's
    #    rebuild goes out of its way to keep them (`reset_scenario(keep_locked=
    #    True)`). This engine re-solves the whole week from scratch and deletes
    #    its own rows to do it, which cascades through locked placements without
    #    a word. Until locks are carried into the solve (`solve(fix=...,
    #    free_sections=...)` is the hook), the honest answer is to stop: a lost
    #    lock is a decision somebody made by hand and cannot get back.
    locked = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__source_tag=SOURCE_TAG, is_locked=True
    ).count()
    if locked:
        raise RuntimeError(
            f"{locked} placement(s) in this scenario are locked. This engine "
            "rebuilds the whole week and would delete them; unlock them first, "
            "or build into a fresh scenario. (The existing engine keeps locks "
            "because it moves placements rather than replacing them.)"
        )

    if progress:
        progress("snapshot")
    snapshot = assign_instructors(
        build_snapshot(
            academic_year=str(academic_year),
            term=int(term),
            gender=str(gender),
            programs=list(programs),
            default_capacity=int(default_capacity),
            min_demand=int(min_demand),
        )
    )

    if progress:
        progress("solve")
    # The same-hour ceilings (D14) are deliberately NOT restated here. They come
    # from `plan()`'s own defaults, so the Generate button and `sch_plan` cannot
    # drift apart on a policy question — a screen and a command line disagreeing
    # about what the timetable rules are would be worse than either choice.
    if runs > 1:
        result = plan_portfolio(
            snapshot,
            seeds=tuple(range(1, runs + 1)),
            time_limit_seconds=seconds,
            clash_tolerance=clash_tolerance,
        )
    else:
        result = plan(snapshot, time_limit_seconds=seconds, clash_tolerance=clash_tolerance)

    if result.unplaced:
        # Not fatal — a short board still beats none, and D7 says a shortage
        # reports rather than blocks. But it must not arrive looking complete:
        # every metric in the summary is computed over the placements that
        # exist, so a board missing classes scores better than a full one.
        result.warnings.append(
            f"{len(result.unplaced)} meeting(s) have no legal slot and were not "
            "placed; the figures below cover only what was placed"
        )
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

    # ONE board per course. This is the rule the existing engine follows, and
    # departing from it was a real mistake.
    #
    # A section was previously placed on every board whose students needed it —
    # reasoning that a section has one schedule and simply appears in several
    # places. But the boards are how the workbook and the screen SEGMENT the
    # timetable: they render one cell per placement row, so a course wanted by
    # students on all five boards was drawn five times in the same cell, and
    # every course showed "(on T1)" because it genuinely sat on Term 1 as well
    # as its own. The output was unreadable.
    #
    # The existing engine picks a single board and explicitly refuses to place a
    # course twice ("already placed on another board -> skip"). Matching it:
    #
    #   1. the board whose nominal term IS the course's plan term — where the
    #      registrar expects to find it;
    #   2. otherwise the board with the most students who need it, which is the
    #      same tie-break the existing engine uses for cross-term courses.
    #
    # The section itself still has ONE schedule; that was never the part boards
    # were duplicating.
    boards = list(DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order"))
    board_of_term: dict[int, int] = {}
    for board_row in boards:
        if board_row.nominal_term is not None:
            board_of_term.setdefault(int(board_row.nominal_term), board_row.id)

    plan_term_of_key: dict[str, int] = {}
    for budget in ScenarioSectionBudget.objects.filter(scenario=scenario):
        if budget.programme_term is not None:
            key = str(budget.course_key or budget.course_code or "").strip().upper()
            if key:
                plan_term_of_key[key] = int(budget.programme_term)

    students_of_board: dict[int, set[int]] = {
        board_row.id: set(
            BoardStudentLink.objects.filter(board=board_row).values_list("student_id", flat=True)
        )
        for board_row in boards
    }
    wanted_by_student: dict[int, set[str]] = defaultdict(set)
    for demand in snapshot.demand:
        wanted_by_student[int(demand.student_id)] |= set(demand.offering_ids)

    headcount: dict[str, Counter] = defaultdict(Counter)
    for board_id, student_ids in students_of_board.items():
        for student_id in student_ids:
            for offering_id in wanted_by_student.get(int(student_id), ()):
                headcount[offering_id][board_id] += 1

    def board_for(offering):
        return choose_board(
            offering,
            plan_term_of_key=plan_term_of_key,
            board_of_term=board_of_term,
            headcount=headcount.get(offering.id),
        )

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
            target_board = board_for(offering)
            if target_board is None:
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
                SectionPlacement.objects.get_or_create(
                    board_id=target_board,
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
    drift = time_of_day_drift(snapshot, board)

    return {
        "engine": "scheduler",
        "scenario_id": scenario.id,
        "sections": len(snapshot.sections),
        "meetings_written": written,
        "meetings_unplaced": len(result.unplaced),
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
        "sections_on_the_same_hour_percent": drift["percent_same_slot"],
        "sections_within_one_slot_percent": drift["percent_within_one_slot"],
        "mean_time_of_day_drift_minutes": drift["mean_drift_minutes"],
        "slot_columns_added": added_lecture + added_lab,
        "meetings_with_an_instructor": sum(
            1 for p in board.placements if p.instructor_id is not None
        ),
        "notes": list(result.notes),
        # Separate from `notes`, which every successful run also fills. A screen
        # that shows both as warnings teaches the reader to ignore warnings.
        "warnings": list(result.warnings),
    }
