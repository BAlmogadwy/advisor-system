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
from dataclasses import dataclass, field

from scheduler.domain import Snapshot
from scheduler.domain.board import Board

#: Leaving a student without a seat is far worse than giving them a clash: they
#: cannot take the course at all. Priced so no amount of clash-avoidance is ever
#: worth an empty seat.
_UNSEATED_PENALTY = 1000


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

    @property
    def clash_free_percent(self) -> float:
        return (
            round(100.0 * (self.students - self.students_with_a_clash) / self.students, 1)
            if self.students
            else 0.0
        )

    def summary(self) -> dict:
        return {
            "students": self.students,
            "clash_free_percent": self.clash_free_percent,
            "students_with_a_clash": self.students_with_a_clash,
            "total_clashes": self.total_clashes,
            "unseated_demands": self.unseated_demands,
            "average_idle_minutes": round(self.idle_minutes / self.students, 1)
            if self.students
            else 0.0,
            "proven_optimal": self.proven_optimal,
            "notes": list(self.notes),
        }


def seat_students(
    snapshot: Snapshot,
    board: Board,
    *,
    time_limit_seconds: float = 60.0,
) -> SeatingResult:
    """Assign each student to one section per course they need, respecting capacity.

    Minimises the number of students left with a timetable clash, then their
    waiting time between classes. Section capacity is a hard constraint, because
    ignoring it would flatter the board: the proxy objective is allowed to assume
    students spread evenly across sections, and a registrar is not.

    A student who cannot be seated at all is reported rather than hidden.
    """
    from ortools.sat.python import cp_model

    offerings = snapshot.offerings_by_id
    sections_by_offering = snapshot.sections_by_offering

    placements: dict[str, list] = defaultdict(list)
    for p in board.placements:
        placements[p.section_id].append(p)

    # Only in-person meetings can clash; online sits in its own late family and
    # is explicitly excluded from student conflict (D9).
    def busy_windows(section_id: str):
        return [
            (p.day, p.window)
            for p in placements.get(section_id, ())
            if p.delivery.name == "IN_PERSON"
        ]

    model = cp_model.CpModel()
    y: dict[tuple[int, str, str], object] = {}
    unseated: dict[tuple[int, str], object] = {}

    demands = [
        (d.student_id, offering_id)
        for d in snapshot.demand
        for offering_id in sorted(d.offering_ids)
        if sections_by_offering.get(offering_id) and offerings[offering_id].is_scheduled
    ]
    if not demands:
        return SeatingResult(0, 0, 0, 0, 0, 0, True, ["no student demand to seat"])

    for student_id, offering_id in demands:
        choices = []
        for section in sections_by_offering[offering_id]:
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

    clash_terms: list[object] = []
    per_student_clashes: dict[int, list] = defaultdict(list)
    for student_id, offering_ids in wanted.items():
        ordered = sorted(offering_ids)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                for sa in sections_by_offering[first]:
                    for sb in sections_by_offering[second]:
                        if not overlaps(sa.id, sb.id):
                            continue
                        va = y[(student_id, first, sa.id)]
                        vb = y[(student_id, second, sb.id)]
                        clash = model.new_bool_var("")
                        model.add(clash >= va + vb - 1)
                        clash_terms.append(clash)
                        per_student_clashes[student_id].append(clash)

    objective = [c for c in clash_terms]
    objective += [v * _UNSEATED_PENALTY for v in unseated.values()]
    if objective:
        model.minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
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
        )

    chosen: dict[int, list[str]] = defaultdict(list)
    for (student_id, _offering_id, section_id), var in y.items():
        if solver.value(var):
            chosen[student_id].append(section_id)

    missed = sum(1 for v in unseated.values() if solver.value(v))
    clashed = sum(
        1
        for student_id, flags in per_student_clashes.items()
        if any(solver.value(f) for f in flags)
    )
    total = sum(int(solver.value(c)) for c in clash_terms)

    # Waiting time is measured the same way it is for instructors: the day's
    # span minus the time actually spent in class.
    idle = 0
    for _student_id, section_ids in chosen.items():
        by_day: dict = defaultdict(list)
        for section_id in section_ids:
            for day, window in busy_windows(section_id):
                by_day[day].append(window)
        for windows in by_day.values():
            if len(windows) < 2:
                continue
            span = max(w.end for w in windows) - min(w.start for w in windows)
            idle += max(0, span - sum(w.duration for w in windows))

    return SeatingResult(
        students=len(wanted),
        seated_demands=len(demands) - missed,
        unseated_demands=missed,
        students_with_a_clash=clashed,
        total_clashes=total,
        idle_minutes=idle,
        proven_optimal=status == cp_model.OPTIMAL,
    )
