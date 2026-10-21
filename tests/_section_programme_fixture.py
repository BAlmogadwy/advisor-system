"""A small scenario-38-like board for the "mainly for programme" tests.

One course (AI236) with two sections a day apart, ten seats each (so two
reserve, eight regular), wanted by six AI2 and six DS students; a second course
(IS124) wanted by everyone, so the seating has more than one course to keep.
"""

from __future__ import annotations

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)

AI2_IDS = tuple(range(880001, 880007))
DS_IDS = tuple(range(880101, 880107))


def build_marked_scenario(*, mark: str = "AI2", status: str = "draft"):
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name="Programme marks",
        status=status,
        gender="M",
        programs=["AI2", "DS"],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 2", nominal_term=2, program="AI2"
    )
    for course, sections in (("AI236", 2), ("IS124", 1)):
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            course_name=course,
            department="AI",
            credit_hours=3,
            planned_sections=sections,
            max_per_section=10 if sections == 2 else 20,
            total_demand=12,
            programme_term=2,
        )
    placements = {}
    rows = (
        ("AI236", "S1", "SUN", ""),
        ("AI236", "S2", "MON", mark),
        ("IS124", "S1", "TUE", ""),
    )
    for course, label, day, preferred in rows:
        section = TermSection.objects.create(
            scenario=scenario,
            source_tag="tw_auto",
            course_key=course,
            course_code=course,
            course_number=course[-3:],
            course_name=course,
            section=label,
            preferred_programme=preferred,
        )
        placements[(course, label)] = SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day=day,
            start_time="09:00",
            end_time="10:15",
            room="R1",
        )
    for programme, ids in (("AI2", AI2_IDS), ("DS", DS_IDS)):
        for sid in ids:
            Student.objects.create(
                student_id=sid,
                registration_no=str(sid),
                name=f"Student {sid}",
                program=programme,
                section="M",
                status="active",
            )
            for course in ("AI236", "IS124"):
                ScenarioStudentCourseRequest.objects.create(
                    scenario=scenario,
                    student_id=sid,
                    course_key=course,
                    course_code=course,
                    primary_term=2,
                    status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                    priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                    source="test",
                )
    return scenario, board, placements


def evaluate(scenario_id: int):
    """The scenario's evaluation exactly as the optimiser builds it."""
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    profiles = build_student_profiles_for_scenario(scenario_id)
    result = evaluate_generated_timetable_candidate(
        candidate_id="db",
        generated_sections=build_section_states_for_scenario(scenario_id),
        student_profiles=profiles,
        course_rigidity=build_course_rigidity_for_scenario(scenario_id),
        course_tiers=(
            build_course_tier_map_for_scenario(scenario_id)
            if is_tiered_objective_enabled()
            else None
        ),
    )
    return result, profiles
