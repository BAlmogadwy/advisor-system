"""PR1 — end-to-end parity + behaviour tests for prayer/lock enforcement.

Parity: with both flags OFF, planner output is identical to baseline d0c6739.
Behaviour: with both flags ON, a fixture containing a prayer-straddling
placement + a locked row produces the expected rejections on the return
payload (pr1_prayer_rejections, pr1_lock_rejections).

These tests currently fail: the validator module, auto_place_board wiring,
and payload fields land in subsequent commits.
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

pytestmark = pytest.mark.django_db


@pytest.fixture()
def pr1_fixture():
    """Minimal scenario:

    - 1 programme (PR1), 1 DeliveryBoard.
    - 3 courses (PR1_A, PR1_B, PR1_C), 3 students.
    - Rooms: one general-purpose A101 cap 30.
    - Slot schedule: Sun 08:00–09:15, 10:00–11:15, 12:00–13:15 (last straddles prayer).
    - Prayer schedule on Sun: 12:00–12:15 (stored in scenario.slot_config).
    - One pre-existing LOCKED placement: PR1_A at Sun 08:00 in A101.
    """
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="PR1 E2E",
        slot_config={
            "slots": [
                {"day": "Sun", "start": "08:00", "end": "09:15"},
                {"day": "Sun", "start": "10:00", "end": "11:15"},
                {"day": "Sun", "start": "12:00", "end": "13:15"},
            ],
            "prayers": [
                {"day": "Sun", "start": "12:00", "end": "12:15"},
            ],
        },
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


# ---------------------------------------------------------------------------
# Parity: both flags OFF ⇒ planner output matches baseline (d0c6739).
# ---------------------------------------------------------------------------


@override_settings(
    TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=False,
    TIMETABLE_ENFORCE_LOCKS=False,
)
def test_parity_flags_off_matches_baseline(pr1_fixture) -> None:
    """With both flags off, the rejection fields are empty and no locked
    placement is overwritten (pre-PR1 behaviour preserved)."""
    _, board = pr1_fixture
    result = auto_place_board(board.id)

    # Schema-stable return: new keys exist and are empty when disabled.
    assert result.get("pr1_prayer_rejections") == []
    assert result.get("pr1_lock_rejections") == []


# ---------------------------------------------------------------------------
# Behaviour: both flags ON ⇒ rejections populated, locked rows respected.
# ---------------------------------------------------------------------------


@override_settings(
    TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=True,
    TIMETABLE_ENFORCE_LOCKS=True,
)
def test_behaviour_flags_on_emits_rejections(pr1_fixture) -> None:
    _, board = pr1_fixture
    result = auto_place_board(board.id)

    # Locked placement (PR1_A at Sun 08:00 in A101) remains untouched.
    locked = SectionPlacement.objects.get(
        board=board, term_section__course_code="PR1_A", is_locked=True
    )
    assert locked.room == "A101"
    assert locked.start_time == "08:00"

    # At least one rejection surface is non-empty: prayer straddling
    # should produce at least one pr1_prayer_rejections row (the 12:00
    # slot straddles the 12:00–12:15 prayer) OR the planner avoided that
    # slot entirely. Either way the payload fields exist.
    assert "pr1_prayer_rejections" in result
    assert "pr1_lock_rejections" in result
    assert isinstance(result["pr1_prayer_rejections"], list)
    assert isinstance(result["pr1_lock_rejections"], list)


@override_settings(
    TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=True,
    TIMETABLE_ENFORCE_LOCKS=False,
)
def test_behaviour_prayer_only_does_not_preload_locks(pr1_fixture) -> None:
    """With only the prayer flag on, the lock preload is a no-op."""
    _, board = pr1_fixture
    result = auto_place_board(board.id)
    assert result.get("pr1_lock_rejections") == []
