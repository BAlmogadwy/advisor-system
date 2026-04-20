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

from core.services.timetable_rooming import get_capacity_buffer, simulate_buffer_impact

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
