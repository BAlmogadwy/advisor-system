"""PR0 — TIMETABLE_CAPACITY_BUFFER externalisation.

Verifies the buffer helper reads from settings, falls back safely on
invalid values, and that offline comparison / management command
surfaces work without a populated board.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_rooming import (
    assign_rooms_to_board,
    get_capacity_buffer,
    simulate_buffer_impact,
)

pytestmark = pytest.mark.django_db


def test_default_buffer_is_one_point_one() -> None:
    assert get_capacity_buffer() == pytest.approx(1.1)


@override_settings(TIMETABLE_CAPACITY_BUFFER=1.05)
def test_override_is_respected() -> None:
    assert get_capacity_buffer() == pytest.approx(1.05)


@override_settings(TIMETABLE_CAPACITY_BUFFER=1.0)
def test_buffer_of_exactly_one_is_respected() -> None:
    assert get_capacity_buffer() == pytest.approx(1.0)


@override_settings(TIMETABLE_CAPACITY_BUFFER="not-a-number")
def test_invalid_string_falls_back_to_default() -> None:
    assert get_capacity_buffer() == pytest.approx(1.1)


@override_settings(TIMETABLE_CAPACITY_BUFFER=0)
def test_zero_falls_back_to_default() -> None:
    assert get_capacity_buffer() == pytest.approx(1.1)


@override_settings(TIMETABLE_CAPACITY_BUFFER=-2.0)
def test_negative_falls_back_to_default() -> None:
    assert get_capacity_buffer() == pytest.approx(1.1)


def test_simulate_buffer_impact_on_missing_board() -> None:
    result = simulate_buffer_impact(board_id=999_999_999, buffers=[1.0, 1.1])
    assert result == {"board_id": 999_999_999, "programmes": [], "results": []}


def test_buffer_compare_command_requires_target() -> None:
    with pytest.raises(CommandError):
        call_command("timetable_buffer_compare")


def test_buffer_compare_command_rejects_empty_scenario() -> None:
    out = StringIO()
    with pytest.raises(CommandError):
        call_command(
            "timetable_buffer_compare",
            "--scenario-id",
            "999999999",
            stdout=out,
        )


def test_buffer_compare_command_handles_missing_board_id() -> None:
    out = StringIO()
    call_command(
        "timetable_buffer_compare",
        "--board-id",
        "999999999",
        stdout=out,
    )
    # Missing board prints the "no data" branch, exits cleanly.
    assert "no data" in out.getvalue()


@override_settings(TIMETABLE_CAPACITY_BUFFER=1.1)
def test_buffer_compare_uses_current_setting_when_buffers_unspecified() -> None:
    out = StringIO()
    call_command(
        "timetable_buffer_compare",
        "--board-id",
        "999999999",
        stdout=out,
    )
    # Default buffer sequence is {1.0, current} which dedupes+sorts.
    assert "current setting 1.1" in out.getvalue()
    assert "[1.0, 1.1]" in out.getvalue()


# ---------------------------------------------------------------------------
# End-to-end parity: at the default buffer (1.1), planner output is unchanged
# versus the hardcoded 1.1 it replaces.
# ---------------------------------------------------------------------------


@pytest.fixture()
def buffer_parity_fixture():
    """Minimal scenario: one course, one placement, one room.

    Course raw per-section demand = 20 students. With the default 1.1
    buffer the planner sizes rooms at ``int(20 * 1.1) = 22``. A room of
    capacity 22 will therefore fit at default; a room of capacity 21 will
    not fit at default but would fit at buffer = 1.0.
    """
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Buffer Parity",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="PARITY",
        program="PAR",
        display_order=1,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="PAR101",
        department="PAR",
        credit_hours=3,
        planned_sections=1,
        max_per_section=40,
        total_demand=20,
    )
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="PAR101",
        course_number="101",
        course_key="PAR101",
        section="S1",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="Sun",
        start_time="08:00",
        end_time="09:15",
        room="",
    )
    return scenario, board


def _make_lecture_room(code: str, capacity: int) -> Room:
    return Room.objects.create(
        room_code=code,
        capacity=capacity,
        room_type="lecture",
        department="PAR",
        section="M",
    )


def test_default_buffer_preserves_planner_parity_at_1_1(buffer_parity_fixture) -> None:
    """At default buffer, a 22-cap room fits a raw-20 course (22 == int(20*1.1))."""
    _, board = buffer_parity_fixture
    _make_lecture_room("PAR-BUFFERED", 22)

    result = assign_rooms_to_board(board.id)

    assert result["assigned"] == 1
    assert result["unassigned"] == 0
    assert result["capacity_buffer"] == pytest.approx(1.1)
    assert result["buffer_only_rejects"] == 0


def test_default_buffer_rejects_room_that_fits_raw_but_not_buffered(
    buffer_parity_fixture,
) -> None:
    """A 21-cap room is feasible at buffer=1.0 (21 >= 20) but not at 1.1 (21 < 22).

    This proves the buffer is actually being applied to room sizing and
    that the lecture-only diagnostic counter increments in that case.
    """
    _, board = buffer_parity_fixture
    _make_lecture_room("PAR-TIGHT", 21)

    result = assign_rooms_to_board(board.id)

    assert result["assigned"] == 0
    assert result["unassigned"] == 1
    assert result["buffer_only_rejects"] == 1


@override_settings(TIMETABLE_CAPACITY_BUFFER=1.0)
def test_buffer_of_1_0_accepts_room_rejected_at_default(buffer_parity_fixture) -> None:
    """With the buffer disabled, the 21-cap room fits the raw 20-demand course."""
    _, board = buffer_parity_fixture
    _make_lecture_room("PAR-TIGHT", 21)

    result = assign_rooms_to_board(board.id)

    assert result["assigned"] == 1
    assert result["unassigned"] == 0
    assert result["capacity_buffer"] == pytest.approx(1.0)
    assert result["buffer_only_rejects"] == 0
