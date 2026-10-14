"""Read-only, scenario-wide quality of the currently saved meeting schedule.

Student seats are reconstructed by the canonical evaluator. Room auditing only
checks persisted assignments against its canonical domains; it never solves or
repairs the timetable. Failed or absent assessments are explicitly unknown.
"""

from __future__ import annotations

import logging
from itertools import combinations
from typing import Any

from core.services.timetable_assignment_models import SectionState, TimetableEvaluationResult
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_flags import is_tiered_objective_enabled
from core.services.timetable_optimisation_policy import instructor_schedule_metrics
from core.services.timetable_optimizer_v2 import (
    build_course_rigidity_for_scenario,
    build_course_tier_map_for_scenario,
    build_section_instructor_map_for_scenario,
    build_section_states_for_scenario,
    build_student_profiles_for_scenario,
)
from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
from core.services.timetable_secondary_recovery import soft_unresolved_count

logger = logging.getLogger(__name__)


def _rooms(scenario_id: int) -> dict[str, Any]:
    oracle = build_recovery_room_evaluator(scenario_id)
    report = oracle.original_room_report
    missing = set(report["original_unassigned_demand_ids"])
    undersized = set(report["original_undersized_demand_ids"])
    incompatible = set(report["original_unsuitable_demand_ids"])
    clashes = set(report["original_room_conflict_demand_ids"])
    details = []
    for demand in oracle.demands:
        reasons = [
            reason
            for reason, ids in (
                ("missing", missing),
                ("undersized", undersized),
                ("incompatible", incompatible),
                ("clash", clashes),
            )
            if demand.demand_id in ids
        ]
        if reasons:
            details.append(
                {
                    "demand_id": demand.demand_id,
                    "term_section_id": demand.term_section_id,
                    "section_id": oracle.section_ids[demand.term_section_id],
                    "course_code": demand.course_code,
                    "placement_ids": list(demand.placement_ids),
                    "day": demand.day,
                    "start_min": demand.start_min,
                    "end_min": demand.end_min,
                    "rooms": sorted(set(demand.incumbent_rooms)),
                    "required_capacity": demand.required_capacity,
                    "required_type": demand.required_type,
                    "reasons": reasons,
                }
            )
    return {
        "assessed": True,
        "shortages": len(missing | undersized | incompatible | clashes),
        "missing": len(missing),
        "undersized": len(undersized),
        "incompatible": len(incompatible),
        "clashes": len(clashes),
        "physical_meetings": len(oracle.demands),
        "details": details,
    }


def _assignment_clashes(evaluation: TimetableEvaluationResult, sections: list[SectionState]) -> int:
    """Count student/section pairs, not course-demand or duplicate meeting rows."""
    by_id = {section.section_id: section for section in sections}
    return sum(
        any(
            left.day == right.day
            and left.start_min < right.end_min
            and right.start_min < left.end_min
            for left in by_id[a].meetings
            for right in by_id[b].meetings
        )
        for state in evaluation.assignment_states.values()
        for a, b in combinations(sorted(state.section_ids), 2)
    )


def build_scenario_quality(scenario_id: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scenario_id": scenario_id,
        "instructor": {
            "working_days": None,
            "idle_minutes": None,
            "instructors": None,
            "assessed": False,
        },
        "soft_seats": {
            "unresolved": None,
            "assessed": False,
            "source": "canonical_assignment_evaluation",
        },
        "rooms": {
            "shortages": None,
            "missing": None,
            "undersized": None,
            "incompatible": None,
            "clashes": None,
            "physical_meetings": None,
            "assessed": False,
            "details": [],
        },
        "assigned_student_clashes": {
            "count": None,
            "assessed": False,
            "source": "canonical_assignment_evaluation",
        },
    }
    try:
        result["rooms"] = _rooms(scenario_id)
    except Exception:
        logger.exception("Could not audit saved rooms for scenario %s", scenario_id)
        result["rooms"]["reason"] = "assessment_failed"

    try:
        sections = build_section_states_for_scenario(scenario_id)
        instructor_ids = build_section_instructor_map_for_scenario(scenario_id)
        metrics = instructor_schedule_metrics(sections, instructor_ids)
        result["instructor"] = {
            "working_days": metrics["working_days"],
            "idle_minutes": metrics["idle_minutes"],
            "instructors": metrics["affected_instructors"],
            "assessed": True,
        }
    except Exception:
        logger.exception("Could not assess saved meetings for scenario %s", scenario_id)
        result["instructor"]["reason"] = "assessment_failed"
        return result

    try:
        profiles = build_student_profiles_for_scenario(scenario_id)
        if not profiles:
            result["soft_seats"]["reason"] = "no_student_demand"
            result["assigned_student_clashes"]["reason"] = "no_student_demand"
            return result
        tiers = (
            build_course_tier_map_for_scenario(scenario_id)
            if is_tiered_objective_enabled()
            else None
        )
        evaluation = evaluate_generated_timetable_candidate(
            candidate_id=f"quality:{scenario_id}",
            generated_sections=sections,
            student_profiles=profiles,
            course_rigidity=build_course_rigidity_for_scenario(scenario_id),
            section_instructor_ids=instructor_ids,
            course_tiers=tiers,
        )
        result["soft_seats"].update(unresolved=soft_unresolved_count(evaluation), assessed=True)
        result["assigned_student_clashes"].update(
            count=_assignment_clashes(evaluation, sections),
            assessed=True,
        )
    except Exception:
        logger.exception("Could not assess saved student seating for scenario %s", scenario_id)
        result["soft_seats"]["reason"] = "assessment_failed"
        result["assigned_student_clashes"]["reason"] = "assessment_failed"
    return result
