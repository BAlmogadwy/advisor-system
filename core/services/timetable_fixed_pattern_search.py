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


def improve_fixed_course_patterns(
    scenario_id: int, *, repair_clashes: bool = False, repair_availability: bool = False
) -> dict:
    """Whole-bundle neighbourhood. ``repair_availability`` (the teaching-time
    repair, ``TIMETABLE_INSTRUCTOR_AVAILABILITY``): switch only a bundle that
    breaks its teacher's rules to the legal bundle with the fewest broken
    meetings, never raising a clash or overload and never taking the teacher's
    last day off; the day, idle and student vetoes of the optional search do
    not apply (the repair is mandatory and the run's floors absorb its cost).
    A bundle the instructor-week gate allows is always preferred. Only when no
    legal bundle passes the gate may the repair take one that costs a teacher
    an extra working day or idle time (owner, 2026-11-22): the cheapest first,
    and the cost is reported."""
    from core.models import SectionPlacement, TimetableScenario
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_constraints import (
        count_instructor_clashes,
        count_instructor_daily_overloads,
        move_adds_availability_violation,
    )
    from core.services.timetable_instructor_availability import availability_for
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
    # Teaching-time rules (fix plan D-D, strict): a whole-bundle switch never
    # takes a meeting into a time its teacher may not teach; a forbidden meeting
    # the bundle already had may stay. (Only meeting-policy courses are searched
    # here, and those are never online: OnlineCourseLookup leaves them out.)
    availability = availability_for(scenario_id)

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
    if repair_availability:
        if not availability:
            return {"evaluated": 0, "accepted": 0}
        return _switch_bundles_into_availability(
            scenario_id,
            scenario,
            (states, sections, teachers, locked),
            availability,
            evaluate,
            incumbent,
        )
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
                    if availability and move_adds_availability_violation(
                        sections, teachers, availability, {section.section_id: old}
                    ):
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


def _switch_bundles_into_availability(
    scenario_id, scenario, board, availability, evaluate, incumbent
) -> dict:
    """The ``repair_availability`` mode: for each unlocked bundle that breaks its
    teacher's rules, take the legal-for-the-teacher bundle with the fewest broken
    meetings (the first among equals), provided no clash or overload is added and
    the instructor-week gate (a teacher keeps a day off) is not tripped. When
    the gate refuses every legal bundle, the cheapest refused bundle that does
    not take a teacher's last day off is taken (fewest working days added, then
    the smallest idle rise, then fewest broken meetings): owner, 2026-11-22.
    The returned report carries ``instructor_before`` / ``instructor_after``
    (the whole pass) and ``spent_switches`` (the bundles that cost something)
    only when a bundle switched."""
    from core.services.timetable_constraints import (
        count_instructor_clashes,
        count_instructor_daily_overloads,
        section_availability_violations,
    )
    from core.services.timetable_optimisation_policy import (
        instructor_schedule_metrics,
        instructor_spend,
        schedule_metrics_of,
        spend_is_empty,
        spend_order_key,
        takes_last_day_off,
        week_move_breaks_gate,
    )
    from core.services.timetable_optimizer_v2 import persist_section_states_to_scenario
    from core.services.timetable_pr4_instructor import get_instructor_daily_cap

    states, sections, teachers, locked = board
    trials = accepted = 0
    week_start = instructor_schedule_metrics(states, teachers)
    paid_switches: list[dict] = []
    for section in sorted(states, key=lambda s: s.section_id):
        policy = get_course_meeting_policy(section.course_code)
        if not policy or section.section_id in locked:
            continue
        ids = teachers.get(section.section_id, ())
        before = section_availability_violations(section, ids, availability)
        if not before:
            continue
        cap = get_instructor_daily_cap()
        old_clashes = count_instructor_clashes(sections, teachers)
        old_overloads = count_instructor_daily_overloads(sections, teachers, cap)
        week_now = instructor_schedule_metrics(states, teachers)
        old = section.meetings
        best = None  # tier 0: the cheapest legal bundle the week gate allows
        paid = None  # tier 1: a refused bundle that costs no last day off
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
            if candidate == old:
                continue
            section.meetings = candidate
            try:
                trials += 1
                if (
                    count_instructor_clashes(sections, teachers) > old_clashes
                    or count_instructor_daily_overloads(sections, teachers, cap) > old_overloads
                ):
                    continue
                after = section_availability_violations(section, ids, availability)
                if after >= before or (best is not None and after >= best[0]):
                    continue
                evaluated = evaluate()
                if week_move_breaks_gate(
                    schedule_metrics_of(incumbent),
                    incumbent.lexicographic_score,
                    schedule_metrics_of(evaluated),
                    evaluated.lexicographic_score,
                ):
                    if best is None:
                        week_trial = instructor_schedule_metrics(states, teachers)
                        if not takes_last_day_off(week_now, week_trial):
                            cost = instructor_spend(week_now, week_trial)
                            rank = (*spend_order_key(cost), after)
                            if paid is None or rank < paid[0]:
                                paid = (rank, candidate, cost)
                    continue
                best = (after, candidate)
            finally:
                section.meetings = old
        cost = None
        if best is None and paid is not None:
            best, cost = (paid[0][2], paid[1]), paid[2]
        if best is not None:
            section.meetings = best[1]
            permitted = policy.pattern_for(best[1])
            assert permitted is not None
            section.pattern_id = permitted.pattern_id
            incumbent = evaluate()
            accepted += 1
            if cost is not None and not spend_is_empty(cost):
                paid_switches.append({"section_id": section.section_id, "cost": cost})
    report: dict = {"evaluated": trials, "accepted": accepted}
    if accepted:
        persist_section_states_to_scenario(scenario_id, sections)
        report["instructor_before"] = week_start
        report["instructor_after"] = instructor_schedule_metrics(states, teachers)
        if paid_switches:
            report["spent_switches"] = paid_switches
    return report
