"""A locked section that is not S1 never shares its label with a new section.

Scenario 52 (STAT103, three planned sections, only S2 locked): the placers
numbered the new sections from how many were already placed, so they were
named S2 and S3. The new "S2" became the locked section's own row, every pass
of a full rebuild added another meeting pattern to it (18 meetings after eight
passes), S1 was never placed, and the rebuild was rolled back as "Required
sections dropped".
"""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_autoplace import auto_place_scenario, free_section_labels
from core.services.timetable_optimizer_v2 import _reset_unlocked_placements
from core.services.timetable_solver import solve_board

COURSE = "STAT103::PRINCIPLES"


def _scenario_with_locked_s2() -> tuple[TimetableScenario, DeliveryBoard, TermSection]:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name="lock S2")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 2", nominal_term=2)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=COURSE,
        course_code="STAT103",
        course_name="PRINCIPLES",
        credit_hours=3,
        planned_sections=3,
        max_per_section=50,
        total_demand=111,
        programme_term=2,
    )
    locked = TermSection.objects.create(
        scenario=scenario,
        course_code="STAT103",
        course_number="STAT103",
        course_key=COURSE,
        course_name="PRINCIPLES",
        section="S2",
        source_tag="test",
    )
    for day in ("MON", "WED"):
        SectionPlacement.objects.create(
            board=board,
            term_section=locked,
            day=day,
            start_time="13:00",
            end_time="14:15",
            room="R1",
            is_locked=True,
        )
    return scenario, board, locked


def _labels(board: DeliveryBoard) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in SectionPlacement.objects.filter(
        board=board, term_section__course_key=COURSE
    ).values_list("term_section__section", flat=True):
        counts[label] = counts.get(label, 0) + 1
    return counts


@pytest.mark.django_db
def test_free_section_labels_skip_the_locked_label() -> None:
    _, board, _ = _scenario_with_locked_s2()
    assert free_section_labels(board, COURSE, 2) == ["S1", "S3"]


@pytest.mark.django_db
def test_repeated_full_rebuild_passes_keep_the_locked_section_whole() -> None:
    scenario, board, locked = _scenario_with_locked_s2()
    # Each full-rebuild pass: reset the unlocked rows, place again.
    for _ in range(3):
        _reset_unlocked_placements(scenario.id)
        auto_place_scenario(scenario.id, strategy="compact")

    counts = _labels(board)
    assert counts.get("S2") == 2
    assert SectionPlacement.objects.filter(term_section=locked).count() == 2
    assert set(counts) == {"S1", "S2", "S3"}


@pytest.mark.django_db
def test_board_solver_names_new_sections_around_the_lock() -> None:
    _, board, _ = _scenario_with_locked_s2()
    result = solve_board(board.id, time_limit_seconds=5)

    assert sorted(p["section"] for p in result["placements"]) == ["S1", "S3"]
    assert sorted(p["sec_num"] for p in result["placements"]) == [1, 3]
