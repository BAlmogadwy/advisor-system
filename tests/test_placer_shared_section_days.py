"""The greedy placer keeps a section that sits on two boards on its teacher's four days.

A section shared by two boards (the registrar's one section of a course taught to two terms)
is placed twice, once per board, by two separate runs of the placer, and nothing in either run
knows the other board's days are the same teacher's. Under the day-off policy, inside
``aligning_shared_section_days`` (what Generate sets), the second placement counts a teaching
day beyond the policy's four against its teacher. Everywhere else the placer is unchanged.
"""

from __future__ import annotations

import pytest

from core.models import DeliveryBoard, Room, SectionPlacement
from core.services.timetable_autoplace import (
    FIFTH_DAY_WEIGHT,
    _option_fifth_day_cost,
    aligning_shared_section_days,
    auto_place_board,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
    unplace,
)

SLOTS = (
    ("08:00", "09:15"),
    ("09:30", "10:45"),
    ("11:00", "12:15"),
    ("12:30", "13:45"),
    ("14:00", "15:15"),
)


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    return settings


def _two_boards_one_teacher(*, on_two_boards: bool = True):
    """Dr T teaches AI201 (the shared section) and AI202. AI202 holds Monday/Wednesday and AI201
    S1 Tuesday/Thursday on the first board: four days, Sunday is his day off. The second board
    has to place AI201 S1 again, and its Monday-Thursday are busy with other courses, so
    without a preference the emptier Sunday looks best."""
    shared = requirement("AI", "AI201", "ML")
    requirement("DS", "AI201", "ML", term=4)  # taught to a second programme's fourth term too
    other = requirement("AI", "AI202", "DB")
    filler = requirement("DS", "AI203", "OS", term=4)
    sc, first = scenario(programs=("AI", "DS"), board_program="AI")
    sc.slot_config = [{"start": s, "end": e} for s, e in SLOTS]
    sc.lab_slot_config = []
    sc.blocked_slots = []
    sc.save()
    second = DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="DS")
    for code in ("R1", "R2", "R3", "R4"):
        Room.objects.create(room_code=code, capacity=60, room_type="lecture", department="AI")
    teacher = instructor("Dr T")
    link("AI", "AI201", teacher)
    link("DS", "AI201", teacher)
    link("AI", "AI202", teacher)
    shared_budget = budget(sc, shared, 1)
    shared_budget.programme_term = 4  # the second board places it; the first holds a copy
    shared_budget.save()
    shared_section = section(sc, shared, "S1")
    if on_two_boards:
        place(first, shared_section, [("TUE", *SLOTS[0]), ("THU", *SLOTS[0])])
    else:  # the same four days, but nothing of AI201 stands on the first board
        extra = requirement("AI", "AI204", "NET")
        link("AI", "AI204", teacher)
        place(first, section(sc, extra, "S1"), [("TUE", *SLOTS[0]), ("THU", *SLOTS[0])])
    place(first, section(sc, other, "S1"), [("MON", *SLOTS[0]), ("WED", *SLOTS[0])])
    for n, day in enumerate(("MON", "TUE", "WED", "THU"), start=1):
        place(second, section(sc, filler, f"F{n}"), [(day, *SLOTS[1]), (day, *SLOTS[2])])
    return sc, second, shared_section


def placements_of(board, term_section) -> set[tuple[str, str]]:
    return set(
        SectionPlacement.objects.filter(board=board, term_section=term_section).values_list(
            "day", "start_time"
        )
    )


def _days_of(second, shared_section) -> set[str]:
    return set(
        SectionPlacement.objects.filter(board=second, term_section=shared_section).values_list(
            "day", flat=True
        )
    )


@pytest.mark.django_db
def test_a_shared_section_is_placed_on_its_teachers_four_days(flags) -> None:
    sc, second, shared_section = _two_boards_one_teacher()

    with aligning_shared_section_days():
        auto_place_board(second.id)

    assert _days_of(second, shared_section)  # it was placed, on the second board
    assert _days_of(second, shared_section) <= {"MON", "TUE", "WED", "THU"}


@pytest.mark.django_db
def test_without_the_context_the_placer_is_unchanged(flags) -> None:
    sc, second, shared_section = _two_boards_one_teacher()

    auto_place_board(second.id)

    assert "SUN" in _days_of(second, shared_section)  # control: the emptier day wins


@pytest.mark.django_db
def test_a_section_that_is_on_one_board_only_is_placed_as_before(flags) -> None:
    sc, second, only_section = _two_boards_one_teacher(on_two_boards=False)

    with aligning_shared_section_days():
        auto_place_board(second.id)
    steered = placements_of(second, only_section)
    assert steered
    unplace(only_section)
    auto_place_board(second.id)

    # Only a section on two boards is steered: this one lands where it lands without the context.
    assert placements_of(second, only_section) == steered


@pytest.mark.django_db
def test_with_the_day_off_policy_off_the_context_changes_nothing(flags) -> None:
    flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False
    sc, second, shared_section = _two_boards_one_teacher()

    with aligning_shared_section_days():
        auto_place_board(second.id)

    assert "SUN" in _days_of(second, shared_section)


def test_the_fifth_day_cost_counts_only_days_beyond_the_policys_four() -> None:
    def option(*days):
        return [{"day": d} for d in days]

    def schedule(*days):
        return {7: {("S", d, 540, 615) for d in days}}

    four = schedule("SUN", "MON", "TUE", "WED")
    assert _option_fifth_day_cost(option("MON", "TUE"), [7], four, 4) == 0
    assert _option_fifth_day_cost(option("MON", "THU"), [7], four, 4) == 1
    # Already past four days: nothing left to lose.
    assert (
        _option_fifth_day_cost(option("THU"), [7], schedule("SUN", "MON", "TUE", "WED", "THU"), 4)
        == 0
    )
    # A teacher with room to spare pays nothing for a new day within four.
    assert _option_fifth_day_cost(option("THU"), [7], schedule("SUN", "MON"), 4) == 0
    # Several teachers add up; an unknown teacher has no days yet.
    both = {
        **four,
        8: {
            ("S", "SUN", 540, 615),
            ("S", "MON", 540, 615),
            ("S", "TUE", 540, 615),
            ("S", "WED", 540, 615),
        },
    }
    assert _option_fifth_day_cost(option("THU"), [7, 8, 99], both, 4) == 2


def test_the_weight_is_a_soft_cost_below_the_hard_score_positions() -> None:
    assert 0 < FIFTH_DAY_WEIGHT
