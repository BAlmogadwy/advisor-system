"""Build a timetable and report student, instructor and room outcomes. Read-only.

S4 of the new subsystem: the planner, with the instructor objective the owner
asked for. Working days are settled first and then frozen as a hard budget, so
the gap term can be pushed hard without ever costing anyone a commute (D11).

    python manage.py sch_plan --year 1448 --term 1 --programs AI,AI2,DS,DS2 --gender M
    python manage.py sch_plan --year 1448 --term 1 --programs AI,DS --gender F --alpha 0.9
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from scheduler.instructors import assign_instructors
from scheduler.intake import IntakeError, build_snapshot, elective_placeholder_report
from scheduler.placement import place_naively
from scheduler.rooms import assign_rooms_exact, room_shortfall
from scheduler.solve import (
    _SCALE,
    expected_clashes,
    instructor_metrics,
    plan,
    plan_portfolio,
    sibling_adjacency,
)
from scheduler.validate import validate


class Command(BaseCommand):
    help = "Plan a timetable: minimise instructor working days, then student clashes."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--year", required=True)
        parser.add_argument("--term", type=int, required=True)
        parser.add_argument("--programs", required=True)
        parser.add_argument("--gender", required=True, choices=["M", "F", "m", "f"])
        parser.add_argument("--default-capacity", type=int, default=25)
        parser.add_argument(
            "--seconds", type=float, default=90.0, help="total budget, split across the two passes"
        )
        parser.add_argument(
            "--alpha", type=float, default=0.9, help="1.0 = students only, 0.0 = instructors only"
        )
        parser.add_argument(
            "--clash-tolerance",
            type=float,
            default=0.05,
            help="how much worse student clashes may get, against the board that "
            "settled the working days, in exchange for shorter gaps. 0.05 is "
            "free on the live data (it improves both); 0.20 roughly halves "
            "the gaps again for about 7%% more clashes.",
        )
        parser.add_argument(
            "--span-weight",
            type=float,
            default=None,
            help="cost of one minute of instructor idle time, in expected-clash "
            "units, used by the second pass. The working-day count is "
            "already frozen by then, so this only trades gaps against "
            "student clashes — it cannot cost anyone an extra day.",
        )
        parser.add_argument(
            "--show-instructors",
            action="store_true",
            help="print each instructor's actual weekly timetable, with every gap "
            "named. The aggregate idle figure says how much waiting there is; "
            "only the timetable says where it falls.",
        )
        parser.add_argument(
            "--runs",
            type=int,
            default=1,
            help="how many independent attempts to make, keeping the best. This "
            "solver's objective has no usable lower bound, so optimality "
            "can never be proven and identical inputs give a wide spread — "
            "re-running is the cheapest quality available. Each run costs "
            "the full --seconds budget.",
        )
        parser.add_argument(
            "--back-to-back",
            type=float,
            default=None,
            help="how hard to pair the sections of one course back to back, for "
            "the service courses this department does not staff. Sections "
            "pair TWO at a time, so three sections give one pair and a "
            "leftover. Default 3, measured as close to free; 10 pairs more "
            "but starts costing student clashes.",
        )
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options) -> None:
        try:
            snapshot = build_snapshot(
                academic_year=options["year"],
                term=options["term"],
                gender=options["gender"],
                programs=str(options["programs"]).split(","),
                default_capacity=options["default_capacity"],
            )
        except IntakeError as exc:
            raise CommandError(str(exc)) from exc

        snapshot = assign_instructors(snapshot)
        baseline = expected_clashes(snapshot, place_naively(snapshot))
        plan_kwargs = {}
        if options["span_weight"] is not None:
            plan_kwargs["gap_weight"] = int(options["span_weight"] * _SCALE)
        if options["back_to_back"] is not None:
            plan_kwargs["sibling_adjacency_weight"] = int(options["back_to_back"] * _SCALE)
        runs = max(1, int(options["runs"]))
        planner = plan if runs == 1 else plan_portfolio
        if runs > 1:
            plan_kwargs["seeds"] = tuple(range(1, runs + 1))
        result = planner(
            snapshot,
            time_limit_seconds=options["seconds"],
            alpha=options["alpha"],
            clash_tolerance=options["clash_tolerance"],
            **plan_kwargs,
        )
        # Exact, not first-fit: greedy strands a class whose only legal room was
        # already spent on one that had alternatives (see tests).
        board = assign_rooms_exact(snapshot, result.board)
        report = validate(snapshot, board)
        clashes = expected_clashes(snapshot, board)
        instructors = instructor_metrics(snapshot, board)
        placeholders = elective_placeholder_report(snapshot, options["year"], options["term"])
        rooms = room_shortfall(snapshot, board)
        pairing = sibling_adjacency(snapshot, board)

        if options["format"] == "json":
            self.stdout.write(
                json.dumps(
                    {
                        "solve": result.summary(),
                        "certification": report.certification.value,
                        "violations": report.violation_count,
                        "expected_clashes": round(clashes, 1),
                        "naive_baseline": round(baseline, 1),
                        "instructors": instructors,
                        "elective_placeholders": placeholders,
                        "room_shortfall": rooms,
                        "sibling_pairing": pairing,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        w = self.stdout.write
        b = board.summary()
        cov = instructors["coverage"]
        w(
            f"Plan {snapshot.academic_year} T{snapshot.term} {snapshot.gender} — "
            f"{', '.join(snapshot.programs)}   (alpha={options['alpha']})"
        )
        w(f"  certification : {report.certification.value}  ({report.violation_count} violations)")
        w("")
        w("  STUDENTS")
        w(
            f"    expected clashes : {clashes:.1f}   (naive baseline {baseline:.0f} "
            f"-> -{100 * (baseline - clashes) / baseline:.0f}%)"
        )
        w("")
        w(
            "  INSTRUCTORS   "
            + (
                "no instructor data for this cohort — nothing to optimise"
                if not instructors["instructors"]
                else f"scope: {cov['sections_assigned']}/{cov['sections_total']} sections "
                f"({cov['percent']}%) — but {cov['sections_assigned']}"
                f"/{cov['sections_staffable']} of the sections this department "
                f"staffs at all ({cov['percent_of_staffable']}%); the rest are "
                f"service courses run elsewhere"
            )
        )
        if instructors["instructors"]:
            w(
                f"    working days     : {instructors['working_days']} "
                f"(proven floor {instructors['floor_days']}, excess {instructors['excess_days']})"
                + ("   AT THE PROVEN FLOOR" if instructors["at_proven_floor"] else "")
            )
            w(f"    idle minutes     : {instructors['idle_minutes']}")
            for row in instructors["per_instructor"]:
                w(
                    f"      instructor {row['instructor_id']:<4} {row['sessions']:>2} sessions  "
                    f"{row['working_days']} days (floor {row['floor_days']})  "
                    f"{row['idle_minutes']:>4} min idle"
                )
        if options["show_instructors"] and instructors["instructors"]:
            self._instructor_timetables(w, snapshot, board, instructors)

        w("")
        w("  ROOMS")
        w(
            f"    roomed           : {b['roomed']}/{b['physical_meetings']}  "
            f"(unroomed {b['unroomed']} — legal during building, blocks publication)"
        )
        if rooms["unroomed"]:
            w(
                f"    unfixable       : {rooms['impossible']} no room is big enough, "
                f"{rooms['saturated']} more meetings than room-periods all week"
            )
            w(
                f"    recoverable     : {rooms['recoverable']} — only these are worth "
                f"another solve; the rest need a decision about rooms"
            )
            for f in rooms["findings"]:
                w(
                    f"      {f['reason']:<10} {f['course']:<9} {f['meetings']} meeting(s)  "
                    f"{f['detail']}"
                )
        if pairing["pairs_achievable"]:
            w("")
            w(
                f"  SECTIONS OF ONE COURSE, BACK TO BACK   "
                f"{pairing['pairs_back_to_back']}/{pairing['pairs_achievable']} pairs "
                f"({pairing['percent']}%)"
            )
            w(
                "    sections pair two at a time, so a course with three makes one "
                "pair and one is left over"
            )
        if placeholders:
            w("")
            unresolved = [p for p in placeholders if p["status"] != "RESOLVED"]
            w(
                f"  ELECTIVE PLACEHOLDERS   {len(placeholders)} scheduled, "
                f"{len(unresolved)} cannot be published under a real course name"
            )
            for p in placeholders:
                detail = (
                    f"-> {p['options'][0]}"
                    if p["status"] == "RESOLVED"
                    else f"-> one of {', '.join(p['options'])}"
                    if p["options"]
                    else "no term mapping — nobody has said what fills this slot"
                )
                w(
                    f"    {p['status']:<10} {p['placeholder']:<6} "
                    f"{p['sections']} section(s)  {detail}"
                )
        w("")
        w("")
        w(f"  solver: {result.status}  {result.wall_time_seconds:.0f}s")
        for note in result.notes:
            if "portfolio" in note or "two-pass" in note or "discarded" in note:
                w(f"    {note}")

    @staticmethod
    def _hhmm(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _instructor_timetables(self, w, snapshot, board, instructors) -> None:
        """One instructor, one week, every gap named.

        The aggregate idle figure says how much waiting there is; only the
        timetable says *where* it falls, which is what somebody actually lives
        with. Gaps are printed as their own lines so a bad day is visible at a
        glance rather than inferred from two timestamps.
        """
        offerings = snapshot.offerings_by_id
        sections = {s.id: s for s in snapshot.sections}
        names = {i.id: i.name for i in snapshot.instructors}
        rows = {r["instructor_id"]: r for r in instructors["per_instructor"]}

        by_instructor: dict[int, list] = {}
        for p in board.placements:
            if p.instructor_id is not None:
                by_instructor.setdefault(p.instructor_id, []).append(p)

        w("")
        w("  INSTRUCTOR TIMETABLES")
        for instructor_id, placements in sorted(by_instructor.items()):
            row = rows.get(instructor_id, {})
            at_floor = row.get("excess_days", 0) == 0
            w("")
            w(
                f"    {names.get(instructor_id, f'instructor {instructor_id}')}"
                f"  (id {instructor_id})  —  {row.get('sessions', 0)} sessions, "
                f"{row.get('working_days', 0)} days"
                f" (floor {row.get('floor_days', 0)}{', at floor' if at_floor else ''}), "
                f"{row.get('idle_minutes', 0)} min idle"
            )
            by_day: dict = {}
            for p in placements:
                by_day.setdefault(p.day, []).append(p)

            for day in snapshot.grid.days():
                items = sorted(by_day.get(day, []), key=lambda x: x.window.start)
                if not items:
                    continue
                previous_end = None
                for index, p in enumerate(items):
                    if previous_end is not None and p.window.start > previous_end:
                        w(
                            f"      {'':<4} {'':<13}  {'':<9} "
                            f"... {p.window.start - previous_end} min gap"
                        )
                    offering = offerings.get(p.offering_id)
                    section = sections.get(p.section_id)
                    w(
                        f"      {day.value if index == 0 else '':<4} "
                        f"{self._hhmm(p.window.start)}-{self._hhmm(p.window.end)}  "
                        f"{offering.course_code if offering else '?':<9} "
                        f"{section.label if section else '':<3} "
                        f"{p.kind.name[:3]:<4} "
                        f"{p.room_id or ('online' if not p.needs_room else 'NO ROOM')}"
                    )
                    previous_end = p.window.end
