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
from core.services.timetable_room_audit import QUALITY_REASON
from core.services.timetable_secondary_recovery import soft_unresolved_count
from core.services.timetable_student_assignment import decode_score

logger = logging.getLogger(__name__)

#: The evaluator's report list behind each verdict that ``QUALITY_REASON`` shows.
_REPORT_IDS = {
    "missing": "original_unassigned_demand_ids",
    "undersized": "original_undersized_demand_ids",
    "unsuitable": "original_unsuitable_demand_ids",
}


def _rooms(scenario_id: int) -> dict[str, Any]:
    oracle = build_recovery_room_evaluator(scenario_id)
    report = oracle.original_room_report
    # Reasons in the payload's order: the room rule's verdicts (missing,
    # undersized, incompatible = unsuitable), then the scenario-wide clash.
    ids_by_reason = {
        reason: set(report[_REPORT_IDS[verdict]]) for verdict, reason in QUALITY_REASON.items()
    }
    ids_by_reason["clash"] = set(report["original_room_conflict_demand_ids"])
    details = []
    for demand in oracle.demands:
        reasons = [reason for reason, ids in ids_by_reason.items() if demand.demand_id in ids]
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
        "shortages": len(set().union(*ids_by_reason.values())),
        "missing": len(ids_by_reason["missing"]),
        "undersized": len(ids_by_reason["undersized"]),
        "incompatible": len(ids_by_reason["incompatible"]),
        "clashes": len(ids_by_reason["clash"]),
        "physical_meetings": len(oracle.demands),
        "details": details,
    }


def _assignment_clash_counts(
    evaluation: TimetableEvaluationResult, sections: list[SectionState]
) -> tuple[int, int]:
    """``(pairs, students)`` of the seated clashes, in one pass.

    ``pairs`` counts (student, pair of that student's seated sections that
    overlap): a student seated in three mutually overlapping sections adds 3,
    and a student in two overlapping sections adds 1. Never course-demand or
    duplicate meeting rows. ``students`` counts the unique students with at
    least one such pair.
    """
    by_id = {section.section_id: section for section in sections}
    pairs = students = 0
    for state in evaluation.assignment_states.values():
        student_pairs = sum(
            any(
                left.day == right.day
                and left.start_min < right.end_min
                and right.start_min < left.end_min
                for left in by_id[a].meetings
                for right in by_id[b].meetings
            )
            for a, b in combinations(sorted(state.section_ids), 2)
        )
        pairs += student_pairs
        students += student_pairs > 0
    return pairs, students


def _seat_tiers(evaluation: TimetableEvaluationResult) -> dict[str, Any]:
    """The seating outcome by tier, decoded from the evaluation's own score.

    Tiered score: ``high_risk`` counts STUDENTS, the rest count REQUESTS
    (a student's wanted course left unseated).

    - ``high_risk``: high-risk students with at least one unseated required
      (T1 or T2) course. High-risk = ``RiskTier.A``, a student with a failed
      course or a retake in progress (``StudentCourse`` failed, or studying
      with a failing grade) -- the ones the engine seats first. Any unseated
      T2 course counts here, tolerated or not.
    - ``required``: unseated T1 (specialised major) requests plus T2 (shared
      foundation) requests beyond the per-course tolerance.
    - ``flexible``: unseated T3 (gen-ed, free electives) requests plus T2
      requests within the tolerance.
    - ``total``: ``required`` + ``flexible``.

    Legacy score (tiering off): only the ``total`` unseated requests exist.
    """
    score = decode_score(evaluation.lexicographic_score)
    if score["layout"] == "tiered":
        return {
            "assessed": True,
            "layout": "tiered",
            "high_risk": int(score["highrisk_unresolved"]),
            "required": int(score["t1_unresolved"]) + int(score["t2_unresolved_over_tol"]),
            "flexible": int(score["soft_unresolved"]),
            "total": int(score["unresolved_courses"]),
        }
    return {"assessed": True, "layout": "legacy", "total": int(score["unresolved_courses"])}


def build_scenario_quality(scenario_id: int) -> dict[str, Any]:
    from core.services.timetable_flags import is_one_teacher_active

    if is_one_teacher_active():
        from core.services.section_teacher_assignment import assignment_scope

        with assignment_scope(scenario_id):  # one teacher map for the whole panel
            return _build_scenario_quality(scenario_id)
    return _build_scenario_quality(scenario_id)


def _build_scenario_quality(scenario_id: int) -> dict[str, Any]:
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
        "seats": {
            "assessed": False,
            "layout": None,
            "high_risk": None,
            "required": None,
            "flexible": None,
            "total": None,
        },
        "assigned_student_clashes": {
            "count": None,
            "students": None,
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
        from core.services.timetable_flags import is_one_teacher_active

        if is_one_teacher_active():
            # Days and idle count only the sections that have a teacher, so
            # the panel says how many do — a TBD section never reads as a
            # better week unseen.
            from core.services.section_teacher_assignment import coverage_of, load_assignment

            coverage = coverage_of(load_assignment(scenario_id))
            result["instructor"]["coverage"] = {
                "assigned": coverage["assigned"],
                "tbd": coverage["tbd"],
                "tbd_d10_floor": coverage["tbd_d10_floor"],
                "no_links": coverage["no_links"],
                "sections_total": coverage["sections_total"],
            }
    except Exception:
        logger.exception("Could not assess saved meetings for scenario %s", scenario_id)
        result["instructor"]["reason"] = "assessment_failed"
        return result

    try:
        profiles = build_student_profiles_for_scenario(scenario_id)
        if not profiles:
            result["soft_seats"]["reason"] = "no_student_demand"
            result["seats"]["reason"] = "no_student_demand"
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
        # Everything is read off the one evaluation before any of it is stored,
        # so a failure leaves all three parts unknown together.
        unresolved = soft_unresolved_count(evaluation)
        seats = _seat_tiers(evaluation)
        clash_pairs, clash_students = _assignment_clash_counts(evaluation, sections)
        result["soft_seats"].update(unresolved=unresolved, assessed=True)
        result["seats"] = seats
        result["assigned_student_clashes"].update(
            count=clash_pairs,
            students=clash_students,
            assessed=True,
        )
    except Exception:
        logger.exception("Could not assess saved student seating for scenario %s", scenario_id)
        result["soft_seats"]["reason"] = "assessment_failed"
        result["seats"]["reason"] = "assessment_failed"
        result["assigned_student_clashes"]["reason"] = "assessment_failed"
    return result
