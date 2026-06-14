"""WS-3: ``build_section_instructor_map_for_scenario`` keys must line up exactly
with the ``section_id``s the evaluator sees, and unassigned courses must be
absent from the map.
"""

from __future__ import annotations

import pytest

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_optimizer_v2 import (
    build_section_instructor_map_for_scenario,
    build_section_states_for_scenario,
)
from core.services.timetable_pr4_instructor import normalise_instructor
from core.services.timetable_student_assignment import build_sections_by_id


def _term_section(scenario, code: str) -> TermSection:
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=code,
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
    )
    return TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=code,
        section="S1",
        source_tag="test",
    )


def _place(board, ts, start="09:00", end="10:15") -> None:
    SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time=start, end_time=end, room="UNASSIGNED"
    )


@pytest.mark.django_db
def test_map_keys_match_assigned_section_ids_only() -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="AI M", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )

    assigned = _term_section(scenario, "AI101")
    unassigned = _term_section(scenario, "AI102")
    _place(board, assigned)
    _place(board, unassigned)

    instr = Instructor.objects.create(
        full_name="Dr A", normalised_name=normalise_instructor("Dr A")
    )
    CourseInstructor.objects.create(
        program="AI", course_code="AI101", section="M", instructor=instr, role="primary"
    )

    section_map = build_section_instructor_map_for_scenario(scenario.id)
    sections = build_section_states_for_scenario(scenario.id)
    section_ids = set(build_sections_by_id(sections).keys())

    # Every map key is a real section_id the evaluator will look up.
    assert set(section_map.keys()) <= section_ids
    # Exactly the assigned section is present, carrying the right instructor.
    assert section_map == {"AI101_S1": frozenset({instr.pk})}
    # The unassigned course is absent (contributes nothing to the idle-gap term).
    assert "AI102_S1" not in section_map


@pytest.mark.django_db
def test_map_empty_without_scenario_gender() -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="no gender", gender="", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    ts = _term_section(scenario, "AI101")
    _place(board, ts)
    instr = Instructor.objects.create(
        full_name="Dr B", normalised_name=normalise_instructor("Dr B")
    )
    CourseInstructor.objects.create(
        program="AI", course_code="AI101", section="M", instructor=instr, role="primary"
    )
    # Gender-scoped assignment model → no gender means no resolvable instructors.
    assert build_section_instructor_map_for_scenario(scenario.id) == {}
