"""Shadow A/B report for the tiered lexicographic objective.

For each scenario it runs the full optimise pipeline twice — once with
``TIMETABLE_TIERED_OBJECTIVE_ENABLED`` OFF (legacy objective), once ON (tiered)
— then re-scores each resulting board through the SAME tiered lens so the two
are apples-to-apples. Both runs execute inside a rolled-back transaction, so
this is read-only: no placement is persisted.

Headline metrics per scenario (all via the tiered decode):
  - t1_unresolved      — specialised (core) seats left unresolved (want ON <= OFF, ->0)
  - t2_over_tolerance  — Tier-2 seats beyond the per-course tolerance
  - soft_unresolved    — T3 + Tier-2-within-tolerance (acceptable to leave)
  - real_gap_minutes   — student idle, UNBUNDLED from same-course spread
  - same_course_spread — the pseudo-penalty, now reported separately
  - highrisk_unresolved / clashes / reserve_used
  - students_moved     — students whose assigned section set differs OFF vs ON

Usage::

    python manage.py tiered_objective_report                 # scenarios 632-639
    python manage.py tiered_objective_report 635 637         # specific scenarios
    python manage.py tiered_objective_report --format json
    python manage.py tiered_objective_report --fast          # short CP-SAT budget
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import override_settings

DEFAULT_SCENARIOS = list(range(632, 640))


class Command(BaseCommand):
    help = "Shadow A/B report: legacy vs tiered objective on real scenarios (no persist)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("scenarios", nargs="*", type=int, help="Scenario IDs (default 632-639)")
        parser.add_argument("--format", choices=["text", "json"], default="text")
        parser.add_argument("--fast", action="store_true", help="Short CP-SAT budget (10s)")
        parser.add_argument(
            "--soft-budget",
            type=int,
            default=None,
            help="Override TIMETABLE_TIERED_SOFT_GAP_BUDGET (gap-min per gen-ed seat) for this run",
        )

    def handle(self, *args, **opts) -> None:
        scenarios = opts["scenarios"] or DEFAULT_SCENARIOS
        cpsat = 10.0 if opts["fast"] else 60.0
        budget = opts.get("soft_budget")

        def _run_all():
            return [self._report_scenario(scn, cpsat) for scn in scenarios]

        if budget is not None:
            with override_settings(TIMETABLE_TIERED_SOFT_GAP_BUDGET=budget):
                rows = _run_all()
        else:
            rows = _run_all()

        if opts["format"] == "json":
            self.stdout.write(json.dumps(rows, indent=2))
            return
        self._print_text(rows)

    # ── one scenario ─────────────────────────────────────────────────────

    def _report_scenario(self, scenario_id: int, cpsat_time_limit: float) -> dict:
        from core.models import TimetableScenario

        if not TimetableScenario.objects.filter(id=scenario_id).exists():
            return {"scenario_id": scenario_id, "error": "scenario not found"}
        try:
            off_score, off_states = self._run_ab(scenario_id, enabled=False, cpsat=cpsat_time_limit)
            on_score, on_states = self._run_ab(scenario_id, enabled=True, cpsat=cpsat_time_limit)
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure in the row
            return {"scenario_id": scenario_id, "error": f"{type(exc).__name__}: {exc}"}

        if off_score is None or on_score is None:
            return {"scenario_id": scenario_id, "error": "no placements to score"}

        moved = sum(1 for sid in off_states if off_states.get(sid) != on_states.get(sid))
        moved += sum(1 for sid in on_states if sid not in off_states)
        return {
            "scenario_id": scenario_id,
            "off": self._decompose(off_score),
            "on": self._decompose(on_score),
            "students_moved": moved,
        }

    def _run_ab(self, scenario_id: int, *, enabled: bool, cpsat: float):
        """Run the pipeline under the flag inside a rolled-back transaction.

        Returns (tiered_score, {student_id: frozenset(section_ids)}) evaluated on
        the resulting board through the tiered lens (regardless of `enabled`), so
        OFF and ON boards are compared on the same axes. Nothing persists.
        """
        score = None
        states_snapshot: dict[str, frozenset[str]] = {}
        with transaction.atomic():
            with override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=enabled):
                from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

                optimise_scenario_timetable_v2(
                    scenario_id,
                    cpsat_time_limit=cpsat,
                )
            score, states_snapshot = self._score_board_tiered(scenario_id)
            transaction.set_rollback(True)
        return score, states_snapshot

    @staticmethod
    def _score_board_tiered(scenario_id: int):
        from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
        from core.services.timetable_optimizer_v2 import (
            build_course_rigidity_for_scenario,
            build_course_tier_map_for_scenario,
            build_section_states_for_scenario,
            build_student_profiles_for_scenario,
        )

        states = build_section_states_for_scenario(scenario_id)
        profiles = build_student_profiles_for_scenario(scenario_id)
        if not states or not profiles:
            return None, {}
        rigidity = build_course_rigidity_for_scenario(scenario_id)
        tiers = build_course_tier_map_for_scenario(scenario_id)
        with override_settings(TIMETABLE_TIERED_OBJECTIVE_ENABLED=True):
            result = evaluate_generated_timetable_candidate(
                "tiered_probe", states, profiles, rigidity, course_tiers=tiers
            )
        snapshot = {sid: frozenset(st.section_ids) for sid, st in result.assignment_states.items()}
        return tuple(result.lexicographic_score), snapshot

    @staticmethod
    def _decompose(score: tuple[int, ...]) -> dict:
        from core.services.timetable_student_assignment import decode_score

        d = decode_score(score)
        return {
            "highrisk_unresolved": d["highrisk_unresolved"],
            "clashes": d["actual_assigned_clashes"],
            "t1_unresolved": d["t1_unresolved"],
            "t2_over_tolerance": d["t2_unresolved_over_tol"],
            "student_cost": d.get("student_cost", d["gap_minutes"]),
            "real_gap_minutes": d["gap_minutes"],
            "soft_unresolved": d["soft_unresolved"],
            "reserve_used": d["reserve_used"],
            "same_course_spread": d["same_course_spread"],
        }

    # ── text rendering ───────────────────────────────────────────────────

    def _print_text(self, rows: list[dict]) -> None:
        for row in rows:
            scn = row["scenario_id"]
            if "error" in row:
                self.stdout.write(f"scn {scn}: ERROR - {row['error']}")
                continue
            off, on = row["off"], row["on"]
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n== scenario {scn} =="))
            self._line(
                "T1 unresolved (core)", off["t1_unresolved"], on["t1_unresolved"], lower=True
            )
            self._line(
                "T2 over-tolerance", off["t2_over_tolerance"], on["t2_over_tolerance"], lower=True
            )
            self._line(
                "High-risk unresolved",
                off["highrisk_unresolved"],
                on["highrisk_unresolved"],
                lower=True,
            )
            self._line(
                "Soft unresolved (T3/tol)",
                off["soft_unresolved"],
                on["soft_unresolved"],
                lower=True,
            )
            self._line(
                "Real gap minutes", off["real_gap_minutes"], on["real_gap_minutes"], lower=True
            )
            self._line("Student cost (blend)", off["student_cost"], on["student_cost"], lower=True)
            self._line(
                "Same-course spread",
                off["same_course_spread"],
                on["same_course_spread"],
                lower=True,
            )
            self._line("Clashes", off["clashes"], on["clashes"], lower=True)
            self._line("Reserve used", off["reserve_used"], on["reserve_used"], lower=True)
            self.stdout.write(f"  students moved OFF->ON : {row['students_moved']}")

    def _line(self, label: str, off, on, *, lower: bool) -> None:
        delta = on - off
        better = (delta < 0) if lower else (delta > 0)
        worse = (delta > 0) if lower else (delta < 0)
        tag = ""
        if better:
            tag = self.style.SUCCESS("  [better]")
        elif worse:
            tag = self.style.WARNING("  [worse]")
        self.stdout.write(f"  {label:<26} {off:>8} -> {on:>8}  ({delta:+d}){tag}")
