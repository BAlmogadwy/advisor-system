from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_workspace import check_publish_readiness

pytestmark = pytest.mark.django_db


def _scenario() -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Publish hard-gate regression",
    )


def _board(
    scenario: TimetableScenario,
    label: str,
    display_order: int,
) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario,
        label=label,
        nominal_term=display_order,
        program="AI",
        display_order=display_order,
    )


def _placement(
    scenario: TimetableScenario,
    board: DeliveryBoard,
    course_code: str,
    room_code: str,
    *,
    section: str = "M1",
) -> SectionPlacement:
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_key=course_code,
        course_code=course_code,
        course_number=course_code,
        course_name=course_code,
        section=section,
        available_capacity=40,
        registered_count=20,
        source_tag="test",
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=course_code,
        course_code=course_code,
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=40,
        total_demand=40,
        programme_term=board.nominal_term,
    )
    return SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room=room_code,
    )


def _room(
    room_code: str,
    *,
    room_type: str = "lecture",
    capacity: int = 60,
    section: str = "M",
    department: str = "AI",
) -> Room:
    return Room.objects.create(
        room_code=room_code,
        room_type=room_type,
        capacity=capacity,
        section=section,
        department=department,
    )


def _cross_board_scenario(shared_students: int) -> TimetableScenario:
    scenario = _scenario()
    board_a = _board(scenario, "Term 1", 1)
    board_b = _board(scenario, "Term 3", 3)
    _room("R1")
    _room("R2")
    _placement(scenario, board_a, "AI101", "R1")
    _placement(scenario, board_b, "AI201", "R2")
    requests = []
    for student_id in range(1, shared_students + 1):
        for course_code in ("AI101", "AI201"):
            requests.append(
                ScenarioStudentCourseRequest(
                    scenario=scenario,
                    student_id=student_id,
                    course_key=course_code,
                    course_code=course_code,
                    primary_term=1,
                    source="test",
                )
            )
    ScenarioStudentCourseRequest.objects.bulk_create(requests)
    return scenario


def test_cross_board_h15_at_hard_threshold_blocks_publish() -> None:
    scenario = _cross_board_scenario(shared_students=20)

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is False
    assert any(
        "1 cross-board conflicts with 20+ shared students" in blocker
        for blocker in readiness["blockers"]
    )
    assert not any("cross-board conflicts" in warning for warning in readiness["warnings"])


def test_cross_board_overlap_below_h15_threshold_does_not_block_publish() -> None:
    scenario = _cross_board_scenario(shared_students=19)

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is True
    assert readiness["blockers"] == []


@pytest.mark.parametrize(
    ("room_overrides", "failed_constraint"),
    [
        ({"room_type": "lab"}, "H11"),
        ({"capacity": 43}, "H12"),
        ({"section": "F"}, "H13"),
        ({"department": "DS"}, "H14"),
    ],
)
def test_each_h11_to_h14_room_violation_blocks_publish(
    room_overrides: dict[str, object],
    failed_constraint: str,
) -> None:
    scenario = _scenario()
    board = _board(scenario, "Term 1", 1)
    room_values = {
        "room_type": "lecture",
        "capacity": 60,
        "section": "M",
        "department": "AI",
        **room_overrides,
    }
    _room("R1", **room_values)
    _placement(scenario, board, "AI101", "R1")

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is False
    assert any(
        "physical placement(s) violate H11-H14" in blocker and f"{failed_constraint}=1" in blocker
        for blocker in readiness["blockers"]
    )


def test_fully_compatible_physical_room_passes_h11_to_h14_gate() -> None:
    scenario = _scenario()
    board = _board(scenario, "Term 1", 1)
    _room("R1", capacity=44)
    _placement(scenario, board, "AI101", "R1")

    readiness = check_publish_readiness(scenario.id)

    assert readiness == {"ready": True, "blockers": [], "warnings": []}
