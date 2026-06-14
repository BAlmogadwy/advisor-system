"""Registrar-facing shadow A/B report for the instructor idle-gap penalty.

Runs one scenario through the optimise pipeline twice — once with
``TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED`` OFF, once ON — and reports the
instructor idle-minute reduction. Crucially it asserts the **student-facing**
score (lexicographic positions 0–5) is byte-identical between the two runs:
instructor compaction must never trade away a student outcome.

Usage::

    python manage.py instructor_gap_report <scenario_id>
    python manage.py instructor_gap_report <scenario_id> --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from core.services.timetable_optimizer_v2 import optimise_current_timetable


class Command(BaseCommand):
    help = "Shadow A/B: instructor idle gaps before/after the penalty, students held constant."

    def add_arguments(self, parser) -> None:
        parser.add_argument("scenario_id", type=int)
        parser.add_argument("--format", choices=("table", "json"), default="table")

    def _run(self, scenario_id: int, enabled: bool) -> dict:
        with override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=enabled):
            return optimise_current_timetable(
                scenario_id,
                run_local_search=True,
                run_chain_search=True,
                run_cpsat_polish=False,
            )

    def handle(self, *args, **options) -> None:
        scenario_id = options["scenario_id"]

        off = self._run(scenario_id, enabled=False)
        on = self._run(scenario_id, enabled=True)

        if off.get("error"):
            raise CommandError(f"OFF run failed: {off['error']}")
        if on.get("error"):
            raise CommandError(f"ON run failed: {on['error']}")

        # Student-facing positions (0–5) must match exactly.
        student_off = list(off.get("final_score") or [])[:6]
        student_on = list(on.get("final_score") or [])[:6]
        students_held = student_off == student_on

        metric = on.get("instructor_gap_metric") or {}
        report = {
            "scenario_id": scenario_id,
            "student_score_off": student_off,
            "student_score_on": student_on,
            "students_held_constant": students_held,
            "instructor_idle_before": metric.get("idle_minutes_before", 0),
            "instructor_idle_after": metric.get("idle_minutes_after", 0),
            "instructor_idle_delta": metric.get("idle_delta", 0),
            "affected_instructors": metric.get("affected_instructors", 0),
        }

        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self.stdout.write(f"Scenario {scenario_id} — instructor gap shadow A/B")
            self.stdout.write(f"  Affected instructors : {report['affected_instructors']}")
            self.stdout.write(f"  Idle minutes before  : {report['instructor_idle_before']}")
            self.stdout.write(f"  Idle minutes after   : {report['instructor_idle_after']}")
            self.stdout.write(f"  Idle reduction (Δ)    : {report['instructor_idle_delta']}")
            self.stdout.write(f"  Student score (OFF)   : {student_off}")
            self.stdout.write(f"  Student score (ON)    : {student_on}")
            verdict = "OK" if students_held else "REGRESSION"
            self.stdout.write(f"  Students held constant: {students_held}  [{verdict}]")

        if not students_held:
            raise CommandError(
                "Student-facing score changed between OFF and ON — the instructor "
                "gap penalty must be strictly subordinate to student outcomes."
            )
