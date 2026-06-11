from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.dashboard_command_center import _latest_timetable_snapshot
from core.services.timetable_autoplace import auto_place_board
from core.services.timetable_rooming import assign_rooms_to_board
from core.services.timetable_workspace import (
    build_scenario_builder_actions,
    check_publish_readiness,
    preview_placement_room_candidates,
)

pytestmark = pytest.mark.django_db


def _scenario(name: str = "online rooming") -> TimetableScenario:
    return TimetableScenario.objects.create(academic_year="1448", term="1", name=name)


def _board(scenario: TimetableScenario, label: str = "Term 1") -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario,
        label=label,
        nominal_term=1,
        program="AI",
        display_order=1,
    )


def _budget(scenario: TimetableScenario, code: str, demand: int = 20) -> None:
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=code,
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=40,
        total_demand=demand,
        programme_term=1,
    )


def _section(scenario: TimetableScenario, code: str) -> TermSection:
    return TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=code,
        section="S1",
        available_capacity=40,
        registered_count=20,
        source_tag="test",
    )


def _room(code: str = "AI-R1") -> Room:
    return Room.objects.create(
        room_code=code,
        capacity=60,
        room_type="lecture",
        department="AI",
        section="",
    )


def test_rooming_clears_online_unassigned_without_counting_room_failure() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _budget(scenario, "GS101")
    _room()
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "GS101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    result = assign_rooms_to_board(board.id)

    placement.refresh_from_db()
    assert placement.room == ""
    assert result["assigned"] == 0
    assert result["unassigned"] == 0
    assert result["room_failures"] == []


def test_rooming_clears_locked_online_room_when_respecting_locks() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _budget(scenario, "GS101")
    _room("AI-R1")
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "GS101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="AI-R1",
        is_locked=True,
    )

    result = assign_rooms_to_board(board.id, respect_locked=True)

    placement.refresh_from_db()
    assert placement.is_locked is True
    assert placement.day == "SUN"
    assert placement.start_time == "09:00"
    assert placement.room == ""
    assert result["assigned"] == 0
    assert result["unassigned"] == 0
    assert result["room_failures"] == []


def test_legacy_online_room_does_not_consume_room_for_physical_course() -> None:
    scenario = _scenario()
    online_board = _board(scenario, "Online board")
    physical_board = _board(scenario, "Physical board")
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _budget(scenario, "GS101")
    _budget(scenario, "AI101")
    _room("AI-R1")

    SectionPlacement.objects.create(
        board=online_board,
        term_section=_section(scenario, "GS101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="AI-R1",
    )
    physical = SectionPlacement.objects.create(
        board=physical_board,
        term_section=_section(scenario, "AI101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="",
    )

    result = assign_rooms_to_board(physical_board.id)

    physical.refresh_from_db()
    assert physical.room == "AI-R1"
    assert result["assigned"] == 1
    assert result["unassigned"] == 0


def test_publish_readiness_ignores_online_unassigned_rooms() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _budget(scenario, "GS101")
    SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "GS101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is True
    assert readiness["blockers"] == []


def test_builder_actions_skip_online_unassigned_rooms_with_normalized_codes() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code=" gs101 ", is_online=True)
    _budget(scenario, "GS101")
    _budget(scenario, "AI101")
    SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, " gs101 "),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "AI101"),
        day="SUN",
        start_time="11:00",
        end_time="12:15",
        room="UNASSIGNED",
    )

    result = build_scenario_builder_actions(scenario.id, limit=20)
    unassigned_room_actions = [
        action for action in result["actions"] if action["kind"] == "unassigned_room"
    ]

    assert [action["course_code"] for action in unassigned_room_actions] == ["AI101"]


def test_dashboard_snapshot_counts_only_offline_unassigned_rooms() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code=" gs101 ", is_online=True)
    _budget(scenario, "GS101")
    _budget(scenario, "AI101")
    SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "Gs101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room=" unassigned ",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "AI101"),
        day="SUN",
        start_time="11:00",
        end_time="12:15",
        room="UNASSIGNED",
    )

    snapshot = _latest_timetable_snapshot()

    assert snapshot["scenario_id"] == scenario.id
    assert snapshot["unassigned_rooms"] == 1


def test_room_candidate_preview_has_no_physical_rooms_for_online_course() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _room()
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=_section(scenario, "GS101"),
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    preview = preview_placement_room_candidates(placement.id)

    assert preview["target"]["is_online"] is True
    assert preview["candidates"] == []
    assert preview["summary"]["is_online"] is True


def test_auto_place_persists_online_course_without_room() -> None:
    scenario = _scenario()
    board = _board(scenario)
    ProgrammeRequirement.objects.create(program="AI", course_code="GS101", is_online=True)
    _budget(scenario, "GS101")
    _room()

    result = auto_place_board(board.id, strategy="compact")

    placements = list(SectionPlacement.objects.filter(board=board).select_related("term_section"))
    assert result["placed"] == 1
    assert placements
    assert {p.term_section.course_code for p in placements} == {"GS101"}
    assert {p.room for p in placements} == {""}
