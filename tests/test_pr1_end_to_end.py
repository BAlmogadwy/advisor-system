"""End-to-end parity + behaviour tests for placement-lock enforcement.

Parity: with ``TIMETABLE_ENFORCE_LOCKS`` OFF, the planner's lock-rejection
field is empty and the baseline return surface is preserved. Behaviour: with
the flag ON, a pre-existing locked row is respected and a ``LOCK_RESPECT``
telemetry row is emitted.

(The companion prayer-overlap rule was removed — prayer compliance is now a
property of the fixed slot grid, guarded by
``timetable_validation.assert_slot_grid_prayer_compliant`` — so these tests
cover locks only.)
"""

from __future__ import annotations

import pytest
from django.test.utils import override_settings

from core.models import (
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_autoplace import auto_place_board
from core.services.timetable_validation import LOCK_RESPECT

pytestmark = pytest.mark.django_db


@pytest.fixture()
def pr1_fixture():
    """Minimal scenario with one pre-existing LOCKED placement.

    - 1 programme (PR1), 1 DeliveryBoard.
    - 3 courses (PR1_A, PR1_B, PR1_C), 3 students.
    - Room: one general-purpose A101 cap 30.
    - Slot schedule: Sun 08:00–09:15, 10:00–11:15, 11:15–12:30.
    - One pre-existing LOCKED placement: PR1_A at Sun 08:00 in A101.
    """
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="PR1 E2E",
        slot_config=[
            {"day": "Sun", "start": "08:00", "end": "09:15"},
            {"day": "Sun", "start": "10:00", "end": "11:15"},
            {"day": "Sun", "start": "11:15", "end": "12:30"},
        ],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="PR1_E2E",
        program="PR1",
        display_order=1,
    )
    Room.objects.create(
        room_code="A101",
        capacity=30,
        room_type="lecture",
        department="PR1",
        section="M",
    )
    for code in ["PR1_A", "PR1_B", "PR1_C"]:
        Course.objects.get_or_create(
            course_code=code,
            defaults={"credit_hours": 3, "department": "PR1"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="PR1",
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3},
        )
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=code,
            department="PR1",
            credit_hours=3,
            planned_sections=1,
            max_per_section=30,
            total_demand=3,
        )
    for i in range(3):
        Student.objects.get_or_create(
            student_id=9900001 + i,
            defaults={"program": "PR1", "section": "M", "name": f"PR1 Student {i}"},
        )

    # Pre-existing locked placement: PR1_A at Sun 08:00, room A101.
    ts_a = TermSection.objects.create(
        scenario=scenario,
        course_code="PR1_A",
        course_number="101",
        course_key="PR1_A",
        section="S1",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ts_a,
        day="Sun",
        start_time="08:00",
        end_time="09:15",
        room="A101",
        is_locked=True,
    )
    return scenario, board


@override_settings(TIMETABLE_ENFORCE_LOCKS=False)
def test_parity_flag_off_matches_baseline(pr1_fixture) -> None:
    """With the lock flag off, the lock-rejection field is empty AND the
    baseline planner surface (placed/skipped/capacity_buffer) is preserved."""
    _, board = pr1_fixture
    result = auto_place_board(board.id)

    assert result.get("pr1_lock_rejections") == []

    assert "placed" in result
    assert "skipped" in result
    assert "placements" in result
    assert isinstance(result["placed"], int)
    assert isinstance(result["skipped"], int)
    assert isinstance(result["placements"], list)
    assert result.get("capacity_buffer") == pytest.approx(1.1)


@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_behaviour_flag_on_respects_lock(pr1_fixture) -> None:
    """With the lock flag on, the locked row is untouched and a LOCK_RESPECT
    telemetry row is emitted."""
    _, board = pr1_fixture
    result = auto_place_board(board.id)

    # Locked placement (PR1_A at Sun 08:00 in A101) remains untouched.
    locked = SectionPlacement.objects.get(
        board=board, term_section__course_code="PR1_A", is_locked=True
    )
    assert locked.room == "A101"
    assert locked.start_time == "08:00"
    assert locked.is_locked is True

    # LOCK_RESPECT must appear in the telemetry for the preloaded locked cell.
    lock_rejections = result.get("pr1_lock_rejections", [])
    assert any(r.get("reason") == LOCK_RESPECT for r in lock_rejections), (
        f"expected LOCK_RESPECT in pr1_lock_rejections, got {lock_rejections}"
    )
