from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
from core.services.timetable_load_balanced import _build_schedule_from_db, _daily_load
from core.services.timetable_local_search import _compute_cost
from core.services.timetable_solver import solve_board

pytestmark = pytest.mark.django_db


def _scenario() -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Online detection",
        slot_config=DEFAULT_SLOTS,
        lab_slot_config=DEFAULT_LAB_SLOTS,
    )


def _board(scenario: TimetableScenario) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
        display_order=1,
    )


def _online_requirement() -> None:
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code=" gs101 ",
        course_name="General online",
        programme_term=1,
        credit_hours=2,
        is_online=True,
    )


def test_solver_uses_shared_normalized_online_lookup() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _online_requirement()
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="GS101",
        course_code="GS101",
        course_name="General online",
        department="AI",
        credit_hours=2,
        planned_sections=1,
        max_per_section=40,
        total_demand=1,
        programme_term=1,
    )

    result = solve_board(board.id, time_limit_seconds=1.0)

    assert result["placed"] == 1
    assert result["placements"][0]["course_code"] == "GS101"
    assert result["placements"][0]["is_online"] is True


def test_local_search_cost_normalizes_online_codes() -> None:
    sections = [{"code": " gs101 "}, {"code": "AI101"}]
    schedule = {
        0: [{"day": "SUN", "start": "09:00", "end": "10:15"}],
        1: [{"day": "SUN", "start": "11:00", "end": "12:15"}],
    }
    overlap_matrix = {("AI101", " gs101 "): 5, (" gs101 ", "AI101"): 5}

    cost = _compute_cost(schedule, sections, {"GS101"}, overlap_matrix)

    assert cost == 0


def test_load_balanced_db_load_uses_shared_normalized_online_lookup() -> None:
    scenario = _scenario()
    board = _board(scenario)
    _online_requirement()
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_key="GS101",
        course_code="GS101",
        course_number="GS101",
        course_name="General online",
        section="S1",
        available_capacity=40,
        registered_count=1,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="",
    )

    sections, schedule, online_codes = _build_schedule_from_db(
        board.id,
        DEFAULT_SLOTS,
        DEFAULT_LAB_SLOTS,
    )

    assert sections[0]["is_online"] is True
    assert _daily_load(schedule, sections, online_codes) == {
        "SUN": 0,
        "MON": 0,
        "TUE": 0,
        "WED": 0,
        "THU": 0,
    }
