from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_rooming import assign_rooms_to_board, simulate_buffer_impact

pytestmark = pytest.mark.django_db


def _scenario() -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Rooming course-key identity",
    )


def _board(scenario: TimetableScenario) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
        display_order=1,
    )


def _budget(
    scenario: TimetableScenario,
    *,
    course_key: str,
    course_code: str = "CS111",
    demand: int,
    credits: int = 3,
) -> None:
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=course_key,
        course_code=course_code,
        department="AI",
        credit_hours=credits,
        planned_sections=1,
        max_per_section=demand,
        total_demand=demand,
        programme_term=1,
    )


def _section(
    scenario: TimetableScenario,
    *,
    course_key: str,
    course_code: str = "CS111",
    section: str = "S1",
) -> TermSection:
    return TermSection.objects.create(
        scenario=scenario,
        course_key=course_key,
        course_code=course_code,
        course_number=course_code,
        course_name=course_key,
        section=section,
        available_capacity=60,
        registered_count=0,
        source_tag="test",
    )


def _placement(
    board: DeliveryBoard,
    section: TermSection,
    *,
    start: str,
) -> SectionPlacement:
    return SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time=start,
        end_time="10:15" if start == "09:00" else "11:45",
        room="",
    )


def _room(code: str, capacity: int) -> None:
    Room.objects.create(
        room_code=code,
        capacity=capacity,
        room_type="lecture",
        department="AI",
        section="",
    )


def test_rooming_uses_course_key_when_visible_course_codes_collide() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _budget(scenario, course_key="CS111::LARGE", demand=50)
    _budget(scenario, course_key="CS111::SMALL", demand=20)
    _room("AI-R30", 30)
    _room("AI-R60", 60)
    large = _placement(
        board,
        _section(scenario, course_key="CS111::LARGE", section="S1"),
        start="09:00",
    )
    small = _placement(
        board,
        _section(scenario, course_key="CS111::SMALL", section="S2"),
        start="10:30",
    )

    result = assign_rooms_to_board(board.id)

    large.refresh_from_db()
    small.refresh_from_db()
    assert result["assigned"] == 2
    assert result["unassigned"] == 0
    assert large.room == "AI-R60"
    assert small.room == "AI-R30"


def test_rooming_keeps_unique_visible_code_fallback_for_legacy_sections() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _budget(scenario, course_key="CS111::SPECIAL", demand=20)
    _room("AI-R30", 30)
    placement = _placement(
        board,
        _section(scenario, course_key="CS111", section="S1"),
        start="09:00",
    )

    result = assign_rooms_to_board(board.id)

    placement.refresh_from_db()
    assert result["assigned"] == 1
    assert result["unassigned"] == 0
    assert placement.room == "AI-R30"


def test_rooming_does_not_guess_duplicate_visible_code_without_matching_key() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _budget(scenario, course_key="CS111::LARGE", demand=50)
    _budget(scenario, course_key="CS111::SMALL", demand=20)
    _room("AI-R39", 39)
    placement = _placement(
        board,
        _section(scenario, course_key="CS111", section="S1"),
        start="09:00",
    )

    result = assign_rooms_to_board(board.id)

    placement.refresh_from_db()
    assert result["assigned"] == 0
    assert result["unassigned"] == 1
    assert placement.room == "UNASSIGNED"


def test_buffer_simulation_uses_course_key_when_visible_codes_collide() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _budget(scenario, course_key="CS111::LARGE", demand=50)
    _budget(scenario, course_key="CS111::SMALL", demand=20)
    _room("AI-R30", 30)
    _placement(
        board,
        _section(scenario, course_key="CS111::LARGE", section="S1"),
        start="09:00",
    )
    _placement(
        board,
        _section(scenario, course_key="CS111::SMALL", section="S2"),
        start="10:30",
    )

    impact = simulate_buffer_impact(board.id, [1.1])

    assert impact["results"] == [
        {
            "buffer": 1.1,
            "assigned": 1,
            "unassigned": 1,
            "rejected_by_buffer_vs_1_0": 0,
        }
    ]
