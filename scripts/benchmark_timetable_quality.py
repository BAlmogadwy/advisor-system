"""Compare timetable engines on an isolated copy of a real SQLite snapshot.

Run this script in separate processes for the original and changed source roots.
It never connects Django to the supplied snapshot: SQLite's backup API copies it
to a new scratch database first. Reports contain aggregate outcomes, not names.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path


def _instructor_metrics(sections, teachers):
    intervals = {}
    for section in sections:
        if section.is_online or getattr(section, "delivery_is_online", False):
            continue
        for instructor in teachers.get(section.section_id, ()):
            for meeting in section.meetings:
                intervals.setdefault((str(instructor), meeting.day), []).append(
                    (meeting.start_min, meeting.end_min)
                )
    days = {}
    idle_by_instructor = {str(i): 0 for ids in teachers.values() for i in ids}
    idle = 0
    for (instructor, _day), meetings in intervals.items():
        days[instructor] = days.get(instructor, 0) + 1
        end = None
        for start, finish in sorted(meetings):
            if end is not None:
                gap = max(0, start - end)
                idle += gap
                idle_by_instructor[instructor] += gap
            end = max(end or 0, finish)
    return {
        "working_days": sum(days.values()),
        "idle_minutes": idle,
        "days": days,
        "idle_by_instructor": idle_by_instructor,
    }


def _classic_metrics(scenario_id):
    from core.models import CourseInstructor, SectionPlacement, TimetableScenario
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_exact_rooming import _load_room_inventory, build_meeting_demands
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_student_assignment import decode_score
    from core.services.timetable_workspace import compute_scenario_safety_summary

    sections = build_section_states_for_scenario(scenario_id)
    # Independent measurement uses the full course identity. Older adapters
    # match only TermSection.course_code, which may hold just the letter prefix
    # on a freshly generated section and incorrectly report zero instructors.
    scenario = TimetableScenario.objects.get(pk=scenario_id)
    eligible = {}
    for code, instructor in CourseInstructor.objects.filter(
        program__in=scenario.programs,
        section=scenario.gender,
        instructor__is_active=True,
    ).values_list("course_code", "instructor_id"):
        eligible.setdefault(code.strip().upper(), set()).add(instructor)
    teachers = {
        section.section_id: frozenset(eligible[section.course_code.split("::", 1)[0].upper()])
        for section in sections
        if section.course_code.split("::", 1)[0].upper() in eligible
    }
    profiles = build_student_profiles_for_scenario(scenario_id)
    evaluation = evaluate_generated_timetable_candidate(
        "independent_benchmark",
        sections,
        profiles,
        build_course_rigidity_for_scenario(scenario_id),
        section_instructor_ids=teachers,
        course_tiers=build_course_tier_map_for_scenario(scenario_id),
    )
    signatures = {
        section.section_id: sorted(
            (meeting.day, meeting.start_min, meeting.end_min) for meeting in section.meetings
        )
        for section in sections
    }
    room_inventory, capacities = _load_room_inventory()
    physical_demands = build_meeting_demands(scenario_id, room_inventory)
    missing_rooms = incompatible_rooms = 0
    for demand in physical_demands:
        assigned = set(demand.incumbent_rooms)
        if not assigned or assigned <= {"", "UNASSIGNED"}:
            missing_rooms += 1
        elif len(assigned) != 1 or not all(
            room in demand.compatible_rooms and capacities.get(room, 0) >= demand.required_capacity
            for room in assigned
        ):
            incompatible_rooms += 1
    return {
        "instructors": _instructor_metrics(sections, teachers),
        "students": len(profiles),
        "unresolved_students": len(evaluation.unresolved_student_ids),
        "student_score": decode_score(evaluation.lexicographic_score),
        "sections": len(sections),
        "meetings": sum(len(section.meetings) for section in sections),
        "unassigned_room_rows": SectionPlacement.objects.filter(
            board__scenario_id=scenario_id, room__in=["", "UNASSIGNED"]
        ).count(),
        "physical_rooming": {
            "unassigned": missing_rooms,
            "incompatible": incompatible_rooms,
            "shortages": missing_rooms + incompatible_rooms,
        },
        "safety": compute_scenario_safety_summary(scenario_id),
        "signatures": signatures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scenario", type=int, default=662)
    parser.add_argument(
        "--action", choices=["current", "full", "scheduler", "measure"], required=True
    )
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--programs", help="Real institutional cohort, e.g. AI,AI2,DS,DS2")
    parser.add_argument("--gender", choices=["M", "F"])
    parser.add_argument(
        "--term", choices=[1, 2, 3], type=int, help="Use another real institutional term"
    )
    args = parser.parse_args()
    source = args.source_root.resolve()
    snapshot, scratch = args.snapshot.resolve(), args.scratch.resolve()
    output = args.output.resolve()
    if not snapshot.is_file() or scratch.exists() or scratch == snapshot:
        parser.error("snapshot must exist; scratch must be a new, distinct file")
    scratch.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as connection:
        with sqlite3.connect(scratch) as destination:
            connection.backup(destination)
    os.environ["DATABASE_URL"] = "sqlite:///" + scratch.as_posix()
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ["DJANGO_DEBUG"] = "true"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-timetable-benchmark-only"
    os.chdir(source)
    sys.path.insert(0, str(source))
    import django

    django.setup()
    from django.conf import settings
    from django.db import connections
    from django.test import override_settings

    from core.models import TimetableScenario

    if Path(settings.DATABASES["default"]["NAME"]).resolve() != scratch:
        raise RuntimeError("Django database is not the isolated scratch database")
    logging.disable(logging.INFO)
    scenario = TimetableScenario.objects.get(pk=args.scenario)
    if args.programs and args.action != "scheduler":
        from core.services.timetable_generate import generate_workspace_scenario

        generated = generate_workspace_scenario(
            year=int(scenario.academic_year),
            semester=args.term or int(scenario.term),
            program=args.programs.split(","),
            section=args.gender or scenario.gender,
            scenario_name="Isolated real-cohort benchmark",
            run_autoplace=False,
        )
        args.scenario = int(generated["scenario"]["id"])
        scenario = TimetableScenario.objects.get(pk=args.scenario)
    report = {
        "source_root": str(source),
        "snapshot": str(snapshot),
        "scratch_database": str(scratch),
        "scenario": args.scenario,
        "action": args.action,
        "budget": {"seconds": args.seconds, "runs": args.runs, "iterations": args.iterations},
    }
    started = time.perf_counter()
    if args.action == "scheduler":
        from scheduler.instructors import assign_instructors
        from scheduler.intake import build_snapshot
        from scheduler.rooms import assign_rooms_exact, room_shortfall
        from scheduler.seating import seat_students
        from scheduler.solve import expected_clashes, instructor_metrics, plan, plan_portfolio
        from scheduler.validate import validate

        inputs = assign_instructors(
            build_snapshot(
                academic_year=scenario.academic_year,
                term=args.term or int(scenario.term),
                programs=args.programs.split(",") if args.programs else list(scenario.programs),
                gender=args.gender or scenario.gender,
                default_capacity=25,
                min_demand=5,
            )
        )
        common = {"time_limit_seconds": args.seconds / args.runs, "clash_tolerance": 0.20}
        if args.runs > 1:
            result = plan_portfolio(inputs, seeds=tuple(range(1, args.runs + 1)), **common)
        else:
            result = plan(inputs, seed=1, **common)
        confirmation = getattr(result, "seating_confirmation", None)
        # Portfolio selection has already measured and retained this roomed
        # board. Re-solving rooming here could describe a different assignment.
        board = (
            result.board if confirmation is not None else assign_rooms_exact(inputs, result.board)
        )
        if confirmation is None:
            confirmation = seat_students(inputs, board, time_limit_seconds=20).summary()
        report["after"] = {
            "input_fingerprint": inputs.fingerprint(),
            "students": len({d.student_id for d in inputs.demand}),
            "sections": len(inputs.sections),
            "instructors": instructor_metrics(inputs, board),
            "rooming": room_shortfall(inputs, board),
            "validation": validate(inputs, board).as_dict(),
            "unplaced": list(result.unplaced),
            "expected_clashes": expected_clashes(inputs, board),
            "seating": confirmation,
            "notes": result.notes,
            "warnings": result.warnings,
        }
    else:
        from core.services.timetable_v2_runner import run_v2_optimisation_guarded

        with override_settings(
            TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS=30,
            TIMETABLE_CHAIN_TIME_LIMIT_SECONDS=20,
        ):
            report["before"] = _classic_metrics(args.scenario)
            report["result"] = (
                {"measured_saved_snapshot": True}
                if args.action == "measure"
                else (
                    run_v2_optimisation_guarded(
                        args.scenario,
                        mode=args.action,
                        max_iterations=args.iterations,
                        max_chain_iterations=3,
                        cpsat_limit=args.seconds,
                    )
                )
            )
            report["after"] = _classic_metrics(args.scenario)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    connections.close_all()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output), "elapsed_seconds": report["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
