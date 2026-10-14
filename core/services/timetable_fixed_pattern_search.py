"""Whole-section neighbourhood for instructor repair and compaction.

Ordinary stages move individual sessions. Fixed weekly courses instead expose
their complete alternatives here, with the same instructor and student gates.
"""

from __future__ import annotations

from core.services.timetable_assignment_models import SectionMeeting, TimetableEvaluationResult
from core.services.timetable_course_policy import (
    get_course_meeting_policy,
    meeting_options_for_policy,
)


def improve_fixed_course_patterns(scenario_id: int, *, repair_clashes: bool = False) -> dict:
    from core.models import SectionPlacement, TimetableScenario
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_constraints import (
        count_instructor_clashes,
        count_instructor_daily_overloads,
    )
    from core.services.timetable_optimisation_policy import (
        evaluation_is_better,
        instructor_schedule_regressions,
        schedule_metrics_of,
    )
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_locked_section_ids_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
        persist_section_states_to_scenario,
    )
    from core.services.timetable_pr4_instructor import get_instructor_daily_cap

    # Avoid building the expensive assignment model on ordinary timetables.
    rows = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .values_list(
            "term_section__course_code", "term_section__course_key", "term_section__course_number"
        )
        .distinct()
    )
    if not any(get_course_meeting_policy(*row) for row in rows):
        return {"evaluated": 0, "accepted": 0}
    scenario = TimetableScenario.objects.get(pk=scenario_id)
    states = build_section_states_for_scenario(scenario_id)
    sections = {s.section_id: s for s in states}
    teachers = build_section_instructor_map_for_scenario(scenario_id)
    locked = build_locked_section_ids_for_scenario(scenario_id)
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id)
    tiers = build_course_tier_map_for_scenario(scenario_id)

    def evaluate() -> TimetableEvaluationResult:
        return evaluate_generated_timetable_candidate(
            "fixed_weekly_patterns",
            states,
            profiles,
            rigidity,
            section_instructor_ids=teachers,
            course_tiers=tiers,
        )

    incumbent = evaluate()
    ceilings = schedule_metrics_of(incumbent)
    trials = accepted = 0
    for _round in range(2):
        changed = False
        for section in sorted(states, key=lambda s: s.section_id):
            policy = get_course_meeting_policy(section.course_code)
            if not policy or section.section_id in locked:
                continue
            if not policy.accepts(section.meetings):
                raise ValueError("English instructor search requires a complete permitted pattern")
            old_clashes = count_instructor_clashes(sections, teachers)
            old_overloads = count_instructor_daily_overloads(
                sections, teachers, get_instructor_daily_cap()
            )
            for option in meeting_options_for_policy(
                policy,
                blocked_slots=scenario.blocked_slots or [],
                slots=(scenario.slot_config or []) + (scenario.lab_slot_config or []),
            ):
                candidate = [
                    SectionMeeting(
                        ("SUN", "MON", "TUE", "WED", "THU").index(m["day"]),
                        int(m["start"][:2]) * 60 + int(m["start"][3:]),
                        int(m["end"][:2]) * 60 + int(m["end"][3:]),
                    )
                    for m in option
                ]
                if candidate == section.meetings:
                    continue
                old = section.meetings
                section.meetings = candidate
                keep = False
                try:
                    trials += 1
                    clashes = count_instructor_clashes(sections, teachers)
                    overloads = count_instructor_daily_overloads(
                        sections, teachers, get_instructor_daily_cap()
                    )
                    if clashes > old_clashes or overloads > old_overloads:
                        continue
                    evaluated = evaluate()
                    if instructor_schedule_regressions(ceilings, schedule_metrics_of(evaluated)):
                        continue
                    if instructor_schedule_regressions(
                        schedule_metrics_of(incumbent), schedule_metrics_of(evaluated)
                    ):
                        continue
                    if any(
                        n > o
                        for n, o in zip(
                            evaluated.lexicographic_score[:4],
                            incumbent.lexicographic_score[:4],
                            strict=True,
                        )
                    ):
                        continue
                    keep = (repair_clashes and clashes < old_clashes) or evaluation_is_better(
                        evaluated, incumbent
                    )
                    if keep:
                        incumbent = evaluated
                        permitted = policy.pattern_for(candidate)
                        assert permitted is not None
                        section.pattern_id = permitted.pattern_id
                        accepted += 1
                        changed = True
                        break
                finally:
                    if not keep:
                        section.meetings = old
        if not changed:
            break
    if accepted:
        persist_section_states_to_scenario(scenario_id, sections)
    return {"evaluated": trials, "accepted": accepted}
