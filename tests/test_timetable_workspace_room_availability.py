from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_workspace import (
    preview_placement_room_candidates,
    preview_placement_slot_candidates,
    preview_placement_student_evidence,
    validate_placement,
)

pytestmark = pytest.mark.django_db


def _term_section(scenario: TimetableScenario, code: str, section: str) -> TermSection:
    return TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=code,
        section=section,
        available_capacity=30,
        registered_count=25,
        source_tag="test",
    )


def _workspace() -> tuple[TimetableScenario, DeliveryBoard, DeliveryBoard]:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Room availability test",
    )
    board_a = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
        display_order=1,
    )
    board_b = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 3",
        nominal_term=3,
        program="AI",
        display_order=2,
    )
    return scenario, board_a, board_b


def test_validate_placement_flags_room_occupied_on_another_board() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")

    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    validation = validate_placement(
        board_id=target_board.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
        term_section_id=moving_ts.id,
    )

    assert validation["warning_count"] == 1
    assert validation["room_clashes"][0]["scope"] == "cross_board"
    assert validation["room_clashes"][0]["board_label"] == "Term 3"


def test_slot_preview_marks_cross_board_room_as_unavailable() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_placement_slot_candidates(moving.id)
    candidate = next(
        row for row in preview["candidates"] if row["day"] == "MON" and row["start"] == "09:00"
    )

    assert candidate["warning_count"] >= 1
    assert candidate["tone"] == "risky"
    assert any(item["kind"] == "cross_board_room" for item in candidate["evidence"])


def test_room_candidates_rank_available_room_above_occupied_room() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="M", department="AI"
    )
    Room.objects.create(
        room_code="R200", capacity=35, room_type="lecture", section="M", department="AI"
    )
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_placement_room_candidates(
        moving.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    by_room = {row["room_code"]: row for row in preview["candidates"]}

    assert by_room["R100"]["available"] is False
    assert by_room["R100"]["occupied_by"][0]["board_label"] == "Term 3"
    assert by_room["R200"]["available"] is True
    assert preview["candidates"][0]["room_code"] == "R200"


def test_student_evidence_lists_exact_students_for_selected_placement() -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=other_ts,
        day="MON",
        start_time="09:30",
        end_time="10:45",
        room="R200",
    )
    Student.objects.create(student_id=10, name="A", program="AI", section="M")
    Student.objects.create(student_id=11, name="B", program="AI", section="M")
    Student.objects.create(student_id=12, name="C", program="AI", section="M")
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=10,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=11,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=12,
        primary_term=1,
        recommended_courses=["AI101"],
    )

    evidence = preview_placement_student_evidence(moving.id)

    assert evidence["affected_student_count"] == 2
    assert evidence["conflicts"][0]["affected_count"] == 2
    assert [row["student_id"] for row in evidence["students"]] == [10, 11]
