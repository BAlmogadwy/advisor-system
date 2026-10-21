"""Seating real students into sections — the check the objective has never faced.

Everything the planner optimises for students is a **proxy**. It minimises

    expected clashes = sum over colliding section pairs of shared / (na * nb)

which assumes students land in sections uniformly at random. That assumption is
what makes the objective cheap enough to solve — scoring a move is an O(1)
lookup instead of re-seating four hundred students — and the blueprint is
explicit that exact seating exists to **confirm a finished board, never to run
inside the loop** (N3, and the module docstring of `solve`).

Until now that confirmation had never been run. So the headline "-86% expected
clashes" described an objective function, not a student's week.

Two things can go wrong with a proxy, and only seating can tell them apart:

* it can **overstate** the damage, because real students are not scattered at
  random — a timetable can be arranged so the sections that collide are ones
  few students need together;
* it can **understate** it, because sections have **capacity**. The proxy is
  free to imagine students spread evenly across four sections; the registrar is
  not, and a full section forces students into whichever one is left.

Capacity is why this is a real assignment problem rather than a counting
exercise, and why it is solved rather than approximated.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter

from scheduler.domain import Day, DeliveryMode, SectionId, Snapshot, TimeWindow
from scheduler.domain.board import Board
from scheduler.meeting_patterns import policy_accepts


@dataclass
class SeatingResult:
    """Where every student ended up, and what it cost them."""

    students: int
    seated_demands: int
    unseated_demands: int
    students_with_a_clash: int
    total_clashes: int
    idle_minutes: int
    proven_optimal: bool
    notes: list[str] = field(default_factory=list)
    #: Waiting time split by whether the student's load sits in one curriculum
    #: term. Reported separately because only the first group's week can be made
    #: tidy — a student picking up leftovers from three terms has a scattered
    #: set by construction, and averaging the two hides both.
    regular_students: int = 0
    regular_idle_minutes: int = 0
    #: Students who got every class they asked for. A student left with no seat
    #: has no clash, so they must not be counted as clash-free.
    students_fully_seated: int = 0
    status: str = "UNKNOWN"
    fully_seated_students_with_a_clash: int | None = None
    #: What "mainly for programme" marks did, when any were passed (else None,
    #: and the summary has no such key): see `seat_students`.
    programme_grouping: dict | None = None

    @property
    def solution_found(self) -> bool:
        return self.proven_optimal or self.status in {"OPTIMAL", "FEASIBLE"}

    @property
    def clash_free_percent(self) -> float | None:
        """Share of FULLY SEATED students with no collision — or None.

        Two ways this used to read 100% for a board nobody could attend:

        * the failure path returns zeros, so ``students - 0`` was every student;
        * a student with no seat has no clash, and the numerator counted them.

        Measured by review: fifty students against a single seat reported
        ``clash_free_percent: 100.0`` beside ``unseated_demands: 49``. Since
        this is the number the blueprint designates as the confirmation of the
        clash proxy, it is now **None** whenever there is nothing honest to say,
        rather than a flattering float. `None` is loud; 100.0 is not.
        """
        if not self.proven_optimal or not self.students_fully_seated:
            return None
        clashed = self.fully_seated_students_with_a_clash
        if clashed is None:
            clashed = self.students_with_a_clash
        return round(
            100.0 * (self.students_fully_seated - clashed) / self.students_fully_seated,
            1,
        )

    def summary(self) -> dict:
        return {
            "students": self.students,
            "students_fully_seated": self.students_fully_seated,
            "clash_free_percent": self.clash_free_percent,
            "students_with_a_clash": self.students_with_a_clash if self.solution_found else None,
            "total_clashes": self.total_clashes if self.solution_found else None,
            "unseated_demands": self.unseated_demands if self.solution_found else None,
            "average_idle_minutes": (
                round(self.idle_minutes / self.students, 1) if self.students else 0.0
            )
            if self.solution_found
            else None,
            "regular_students": self.regular_students,
            "regular_idle_minutes_avg": (
                round(self.regular_idle_minutes / self.regular_students, 1)
                if self.regular_students
                else 0.0
            )
            if self.solution_found
            else None,
            "proven_optimal": self.proven_optimal,
            "status": self.status,
            "solution_found": self.solution_found,
            "seated_demands": self.seated_demands if self.solution_found else None,
            "notes": list(self.notes),
            **(
                {"programme_grouping": dict(self.programme_grouping)}
                if self.programme_grouping is not None
                else {}
            ),
        }


def grouping_cost(programme: str, mark: str, own: bool) -> int:
    """How far one seat is from what the section marks ask for (0 = as asked).

    ``programme`` is the student's, ``mark`` the section's "mainly for
    programme" ("" = any) and ``own`` whether the offering has a section marked
    for the student's programme. Another programme's section costs 1; outside
    one's own programme's section (when the offering has one) costs 1 more.

    The same table as ``core.services.timetable_student_assignment.
    programme_grouping_cost``; restated, not imported, because this subsystem
    stays isolated from the original planner. A test pins the two together.
    """
    return int(bool(mark) and mark != programme) + int(own and mark != programme)


def seat_students(
    snapshot: Snapshot,
    board: Board,
    *,
    time_limit_seconds: float = 60.0,
    preferred_programme: Mapping[SectionId, str] | None = None,
    grouping_seconds: float | None = None,
) -> SeatingResult:
    """Assign each student to one section per course they need, respecting capacity.

    Minimises unseated demands first, then colliding course pairs. Waiting time
    is measured after assignment. Section capacity is a hard constraint, because
    ignoring it would flatter the board: the proxy objective is allowed to assume
    students spread evenly across sections, and a registrar is not.

    A student who cannot be seated at all is reported rather than hidden.

    ``preferred_programme`` maps a section to the programme it is mainly for.
    With none (or an empty map) this is exactly the seating above. With marks, a
    second solve on the same model re-seats the students towards their
    programme's section with every phase-1 unseated flag pinned (so a missed
    seat can never pay for a better grouping) and each student's clashes held
    at or below phase 1. It gets ``grouping_seconds`` of its own (default
    ``min(10, limit / 2)``; 0 skips it) and is kept only if it finds a
    solution; otherwise phase 1 stands. Waiting time may change and is reported
    before and after in ``programme_grouping``.
    """
    from ortools.sat.python import cp_model

    started = perf_counter()
    if time_limit_seconds < 0:
        raise ValueError("time_limit_seconds must be nonnegative")
    if grouping_seconds is not None and grouping_seconds < 0:
        raise ValueError("grouping_seconds must be nonnegative")
    marks = {
        section_id: str(mark or "").strip().upper()
        for section_id, mark in (preferred_programme or {}).items()
        if str(mark or "").strip()
    }
    offerings = snapshot.offerings_by_id
    sections_by_offering = snapshot.sections_by_offering

    placements: dict[str, list] = defaultdict(list)
    for p in board.placements:
        placements[p.section_id].append(p)

    # An absent or incomplete section offers no deliverable seat. Counting it
    # as an empty schedule would reward missing meetings with zero clashes.
    usable: dict[str, bool] = {}
    for section in snapshot.sections:
        offering = offerings[section.offering_id]
        required = [r for r in offering.requirements for _ in range(r.count_per_week)]
        actual = sorted(placements.get(section.id, ()), key=lambda p: p.meeting_index)
        usable[section.id] = (
            bool(required)
            and len(actual) == len(required)
            and all(
                p.meeting_index == index
                and p.offering_id == offering.id
                and p.kind is req.kind
                and p.delivery is req.delivery
                and p.window.duration == req.duration
                for index, (p, req) in enumerate(zip(actual, required, strict=False), start=1)
            )
        )
        if usable[section.id]:
            usable[section.id] = policy_accepts(offering, actual) and not any(
                a.overlaps(b) for index, a in enumerate(actual) for b in actual[index + 1 :]
            )

    # CLASH counts every meeting, online included: an online class at 15:50 and
    # an on-campus one at 16:00 collide in real time, and a student cannot
    # attend both however either is delivered.
    def busy_windows(section_id: str) -> list[tuple[Day, TimeWindow]]:
        return [(p.day, p.window) for p in placements.get(section_id, ())]

    # WAITING counts only what puts a student on campus (owner rule): an evening
    # online class is not time spent hanging about between lectures, so it must
    # not inflate the figure -- and it would inflate it badly, since the online
    # family sits hours after the on-campus day ends.
    def campus_windows(section_id: str) -> list[tuple[Day, TimeWindow]]:
        return [
            (p.day, p.window)
            for p in placements.get(section_id, ())
            if p.delivery is DeliveryMode.IN_PERSON
        ]

    model = cp_model.CpModel()
    y: dict[tuple[int, str, str], cp_model.IntVar] = {}
    unseated: dict[tuple[int, str], cp_model.IntVar] = {}

    demands = [
        (d.student_id, offering_id)
        for d in snapshot.demand
        for offering_id in sorted(d.offering_ids)
        if offering_id in offerings and offerings[offering_id].is_scheduled
    ]
    if not demands:
        return SeatingResult(
            0, 0, 0, 0, 0, 0, True, ["no student demand to seat"], status="OPTIMAL"
        )

    for student_id, offering_id in demands:
        choices = []
        for section in sections_by_offering.get(offering_id, ()):
            if not usable[section.id]:
                continue
            var = model.new_bool_var(f"y_{student_id}_{section.id}")
            y[(student_id, offering_id, section.id)] = var
            choices.append(var)
        miss = model.new_bool_var(f"miss_{student_id}_{offering_id}")
        unseated[(student_id, offering_id)] = miss
        model.add_exactly_one([*choices, miss])

    # Capacity: a section holds only so many people.
    for offering_id, sections in sections_by_offering.items():
        for section in sections:
            takers = [
                var
                for (sid, oid, sec_id), var in y.items()
                if sec_id == section.id and oid == offering_id
            ]
            if takers:
                model.add(sum(takers) <= section.capacity)

    # Clashes: for one student, two chosen sections whose meetings overlap.
    wanted: dict[int, list[str]] = defaultdict(list)
    for student_id, offering_id in demands:
        wanted[student_id].append(offering_id)

    overlap_cache: dict[tuple[str, str], bool] = {}

    def overlaps(a: str, b: str) -> bool:
        key = (a, b) if a < b else (b, a)
        if key not in overlap_cache:
            overlap_cache[key] = any(
                da is db and wa.overlaps(wb)
                for da, wa in busy_windows(a)
                for db, wb in busy_windows(b)
            )
        return overlap_cache[key]

    clash_terms: list[cp_model.IntVar] = []
    per_student_clashes: dict[int, list[cp_model.IntVar]] = defaultdict(list)
    for student_id, offering_ids in wanted.items():
        ordered = sorted(offering_ids)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                for sa in sections_by_offering.get(first, ()):
                    for sb in sections_by_offering.get(second, ()):
                        if not usable[sa.id] or not usable[sb.id]:
                            continue
                        if not overlaps(sa.id, sb.id):
                            continue
                        va = y[(student_id, first, sa.id)]
                        vb = y[(student_id, second, sb.id)]
                        clash = model.new_bool_var("")
                        model.add(clash >= va + vb - 1)
                        model.add(clash <= va)
                        model.add(clash <= vb)
                        clash_terms.append(clash)
                        per_student_clashes[student_id].append(clash)

    objective: list[cp_model.LinearExpr] = list(clash_terms)
    # Bound the ENTIRE clash objective: one additional seat must always beat
    # every possible clash saving, even on a large cohort.
    missed_weight = len(clash_terms) + 1
    objective += [v * missed_weight for v in unseated.values()]
    if objective:
        model.minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(
        0.001, time_limit_seconds - (perf_counter() - started)
    )
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SeatingResult(
            len(wanted),
            0,
            len(demands),
            0,
            0,
            0,
            False,
            [f"no seating found within {time_limit_seconds:.0f}s"],
            status=solver.status_name(status),
            programme_grouping=(
                {"marked_sections": sorted(marks), "status": "NO_SEATING"} if marks else None
            ),
        )

    def chosen_of(answer) -> dict[int, list[str]]:
        chosen: dict[int, list[str]] = defaultdict(list)
        for (student_id, _offering_id, section_id), var in y.items():
            if answer.value(var):
                chosen[student_id].append(section_id)
        return chosen

    # Waiting time is measured the same way it is for instructors: the day's
    # span minus the time actually spent in class.
    regular_ids = {int(d.student_id) for d in snapshot.demand if not d.is_cross_term}

    def waiting(chosen: dict[int, list[str]]) -> tuple[int, int]:
        idle = regular_idle = 0
        for student_id, section_ids in chosen.items():
            by_day: dict[Day, list[TimeWindow]] = defaultdict(list)
            for section_id in section_ids:
                for day, window in campus_windows(section_id):
                    by_day[day].append(window)
            theirs = 0
            for windows in by_day.values():
                if len(windows) < 2:
                    continue
                ordered_windows = sorted(windows, key=lambda w: w.start)
                end = ordered_windows[0].end
                for window in ordered_windows[1:]:
                    theirs += max(0, window.start - end)
                    end = max(end, window.end)
            idle += theirs
            if int(student_id) in regular_ids:
                regular_idle += theirs
        return idle, regular_idle

    # The answer read below: phase 1, or the grouped phase 2 when it found one.
    answer = solver
    grouping: dict | None = None
    if marks:
        answer, grouping = _group_by_programme(
            cp_model,
            model,
            solver,
            snapshot,
            marks,
            y=y,
            unseated=unseated,
            per_student_clashes=per_student_clashes,
            seconds=(
                min(10.0, time_limit_seconds / 2) if grouping_seconds is None else grouping_seconds
            ),
            waiting=lambda solved: waiting(chosen_of(solved))[0],
        )

    chosen = chosen_of(answer)
    missed = sum(1 for v in unseated.values() if answer.value(v))
    short = {student_id for (student_id, _o), v in unseated.items() if answer.value(v)}
    clashed = sum(
        1
        for student_id, flags in per_student_clashes.items()
        if any(answer.value(f) for f in flags)
    )
    total = sum(int(answer.value(c)) for c in clash_terms)
    idle, regular_idle = waiting(chosen)

    return SeatingResult(
        students=len(wanted),
        seated_demands=len(demands) - missed,
        unseated_demands=missed,
        students_with_a_clash=clashed,
        total_clashes=total,
        idle_minutes=idle,
        proven_optimal=status == cp_model.OPTIMAL,
        students_fully_seated=len(wanted) - len(short),
        regular_students=len(regular_ids & set(wanted)),
        regular_idle_minutes=regular_idle,
        status=solver.status_name(status),
        fully_seated_students_with_a_clash=sum(
            1
            for student_id, flags in per_student_clashes.items()
            if student_id not in short and any(answer.value(f) for f in flags)
        ),
        programme_grouping=grouping,
    )


def _group_by_programme(
    cp_model,
    model,
    solver,
    snapshot: Snapshot,
    marks: dict[str, str],
    *,
    y: dict,
    unseated: dict,
    per_student_clashes: dict,
    seconds: float,
    waiting,
):
    """Phase 2 of `seat_students`: the phase-1 seating, grouped by programme.

    Returns ``(answer, report)``: the solver whose values to read (phase 2's
    when it found a solution, else phase 1's) and what happened. Phase 2 cannot
    cost a seat: every unseated flag is pinned to its phase-1 value — without
    that, a missed seat costs nothing here and the cheapest grouping would leave
    mismatched students out — and every student's clashes are held at or below
    phase 1, so unseated, total clashes and students with a clash are each the
    same or better.
    """
    programme = {int(d.student_id): str(d.program or "").strip().upper() for d in snapshot.demand}
    own: dict[str, set[str]] = {}
    for offering_id, sections in snapshot.sections_by_offering.items():
        offering_marks = {marks.get(s.id, "") for s in sections} - {""}
        if offering_marks:
            own[offering_id] = offering_marks
    costs = []
    for (student_id, offering_id, section_id), var in y.items():
        if offering_id not in own:
            continue
        theirs = programme.get(int(student_id), "")
        cost = grouping_cost(
            theirs, marks.get(section_id, ""), bool(theirs) and theirs in own[offering_id]
        )
        if cost:
            costs.append((var, cost))
    before = sum(cost for var, cost in costs if solver.value(var))
    idle_before = waiting(solver)
    report = {
        "marked_sections": sorted(marks),
        "grouping_cost_before": before,
        "grouping_cost_after": before,
        "idle_minutes_before": idle_before,
        "idle_minutes_after": idle_before,
        "status": "NOT_NEEDED" if not before else "NOT_RUN",
    }
    if not before or seconds <= 0:
        return solver, report

    for var in unseated.values():
        model.add(var == int(solver.value(var)))
    for flags in per_student_clashes.values():
        model.add(sum(flags) <= sum(int(solver.value(flag)) for flag in flags))
    model.clear_hints()
    for var in (*y.values(), *unseated.values()):
        model.add_hint(var, int(solver.value(var)))
    for flags in per_student_clashes.values():
        for flag in flags:
            model.add_hint(flag, int(solver.value(flag)))
    model.minimize(sum(var * cost for var, cost in costs))

    grouped = cp_model.CpSolver()
    grouped.parameters.max_time_in_seconds = max(0.001, seconds)
    grouped.parameters.num_search_workers = 8
    status = grouped.solve(model)
    report["status"] = grouped.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solver, report
    report["grouping_cost_after"] = sum(cost for var, cost in costs if grouped.value(var))
    report["idle_minutes_after"] = waiting(grouped)
    return grouped, report
