"""The placer sees every placement already on the board it is filling.

``auto_place_board`` only places what is still missing and keeps the rest, so
a re-run against a populated board — the required-section guarantee's retry —
must respect the sections already there. It used to seed only LOCKED rows, so
the retry booked a reinstated section into a room this board already used at
that time, on top of the course's own other sections and on top of its
students' other classes (lab scenario 667: CS112 PROGRAMMING II in AI124 S2's
and CS114 S1's rooms; a from-scratch build put DS331 into IS124 S2's room).
"""

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
from core.services.timetable_autoplace import auto_place_board

DAYS = ("SUN", "MON", "TUE", "WED", "THU")
NINE = {"start": "09:00", "end": "10:15"}
TEN_THIRTY = {"start": "10:30", "end": "11:45"}


def _board(name: str, slots: tuple[dict, ...] = (NINE,)) -> DeliveryBoard:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name=name,
        slot_config=list(slots),  # default one slot a day: no way round
        lab_slot_config=[],
    )
    return DeliveryBoard.objects.create(scenario=scenario, label="T4", nominal_term=4, program="AI")


def _budget(board: DeliveryBoard, code: str, planned: int) -> None:
    ScenarioSectionBudget.objects.create(
        scenario=board.scenario,
        course_code=code,
        course_key=code,
        course_name=code,
        department="AI",
        credit_hours=3,
        planned_sections=planned,
        max_per_section=30,
        total_demand=20 * planned,
        programme_term=4,
    )


def _existing(
    board: DeliveryBoard, code: str, label: str, room: str, slot: dict = NINE
) -> TermSection:
    """An unlocked section already on the board in ``slot`` every day."""
    ts = TermSection.objects.create(
        scenario=board.scenario,
        course_key=code,
        course_code=code,
        course_number=code,
        course_name=code,
        section=label,
        source_tag="tw_auto",
    )
    for day in DAYS:
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=slot["start"],
            end_time=slot["end"],
            room=room,
            is_locked=False,
        )
    return ts


@pytest.mark.django_db
def test_a_rerun_does_not_book_a_room_the_board_already_uses() -> None:
    board = _board("rerun rooms")
    Room.objects.create(room_code="SMALL", capacity=25, room_type="lecture", department="AI")
    Room.objects.create(room_code="BIG", capacity=100, room_type="lecture", department="AI")
    _existing(board, "AI113", "S1", "SMALL")  # holds the best-fit room at every 09:00
    _budget(board, "AI201", planned=1)

    result = auto_place_board(board.id)

    assert result["placed"] == 1
    rooms = set(
        SectionPlacement.objects.filter(board=board, term_section__course_key="AI201").values_list(
            "room", flat=True
        )
    )
    assert rooms == {"BIG"}


@pytest.mark.django_db
def test_a_rerun_keeps_a_new_section_off_its_students_existing_classes() -> None:
    board = _board("rerun students", slots=(NINE, TEN_THIRTY))
    for code in ("RX", "RZ1", "RZ2", "FREE"):  # a free room in both slots
        Room.objects.create(room_code=code, capacity=100, room_type="lecture", department="AI")
    _budget(board, "AI113", planned=1)
    _existing(board, "AI113", "S1", "RX")  # every 09:00
    for student_id in (1, 2, 3):  # AI113's students also need AI201
        for code in ("AI113", "AI201"):
            ScenarioStudentCourseRequest.objects.create(
                scenario=board.scenario, student_id=student_id, course_key=code, course_code=code
            )
    # Two unrelated courses fill every 10:30, so everything but the shared
    # students — slot density, the early-slot preference, the idle gap after
    # AI113 — favours 09:00. Only the student overlap can push AI201 to 10:30.
    _existing(board, "AI150", "S1", "RZ1", slot=TEN_THIRTY)
    _existing(board, "AI160", "S1", "RZ2", slot=TEN_THIRTY)
    _budget(board, "AI201", planned=1)

    result = auto_place_board(board.id)

    assert result["placed"] == 1
    starts = set(
        SectionPlacement.objects.filter(board=board, term_section__course_key="AI201").values_list(
            "start_time", flat=True
        )
    )
    assert starts == {"10:30"}


@pytest.mark.django_db
def test_a_rerun_keeps_a_new_section_off_its_own_courses_existing_section() -> None:
    board = _board("rerun same course")
    Room.objects.create(room_code="R1", capacity=100, room_type="lecture", department="AI")
    Room.objects.create(room_code="R2", capacity=100, room_type="lecture", department="AI")
    _budget(board, "AI201", planned=2)
    _existing(board, "AI201", "S1", "R1")  # S1 already holds every 09:00

    result = auto_place_board(board.id)

    # S2 cannot be placed without overlapping S1, so it is skipped — and says why.
    assert result["placed"] == 0
    assert not SectionPlacement.objects.filter(board=board, term_section__section="S2").exists()
    [failure] = result["room_failures"]
    assert failure["reason"] == "SAME_COURSE_INSTRUCTOR_CLASH"
    assert failure["section_code"] == "S2"
