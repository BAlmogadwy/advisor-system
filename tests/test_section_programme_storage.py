"""Where a "mainly for programme" mark lives, and that it is never lost.

The mark is ``TermSection.preferred_programme``. A global (registrar) section
can never carry one; a scenario copy and a cancelled planner job's restore keep
it; the board builder hands it to the seater only under the flag; and the CP-SAT
polisher, which rebuilds every ``SectionState`` field by field, carries it.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from core.models import DeliveryBoard, SectionPlacement, TermSection, TimetableScenario
from core.services.timetable_scenario_copy import copy_timetable_scenario

pytestmark = pytest.mark.django_db


def _scenario_with_marked_section(mark: str = "AI2"):
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="Marks", programs=["AI", "AI2"]
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 2", nominal_term=2)
    section = TermSection.objects.create(
        scenario=scenario,
        source_tag="tw_auto",
        course_key="AI236",
        course_code="AI236",
        course_number="236",
        section="S2",
        preferred_programme=mark,
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )
    return scenario, section


def test_the_database_refuses_a_programme_mark_on_a_global_section() -> None:
    TermSection.objects.create(
        course_key="MATH105", course_code="MATH105", course_number="105", section="M1"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        TermSection.objects.create(
            course_key="MATH106",
            course_code="MATH106",
            course_number="106",
            section="M1",
            preferred_programme="AI2",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        TermSection.objects.filter(course_key="MATH105").update(preferred_programme="AI")


def test_a_scenario_copy_carries_the_mark() -> None:
    source, section = _scenario_with_marked_section("AI2")
    target = copy_timetable_scenario(source.pk).scenario
    copied = TermSection.objects.get(scenario=target, course_key="AI236", section="S2")
    assert copied.pk != section.pk
    assert copied.preferred_programme == "AI2"


def test_a_cancelled_planner_job_restores_the_mark() -> None:
    from core.services.planner_job_snapshot import restore_planner_job, snapshot_planner_job

    scenario, section = _scenario_with_marked_section("AI2")
    baseline = snapshot_planner_job(scenario.pk)
    # A job may delete and re-create sections; the cancel puts the start back.
    TermSection.objects.filter(pk=section.pk).update(preferred_programme="")
    TermSection.objects.filter(pk=section.pk).delete()
    restore_planner_job(baseline)
    assert TermSection.objects.get(pk=section.pk).preferred_programme == "AI2"


@pytest.mark.parametrize("enabled", [False, True])
def test_the_board_builder_hands_the_mark_to_the_seater_only_under_the_flag(enabled) -> None:
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario, _section = _scenario_with_marked_section(" ai2 ")
    with override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=enabled):
        states = build_section_states_for_scenario(scenario.pk)
    assert [s.preferred_programme for s in states] == (["AI2"] if enabled else [""])


@override_settings(TIMETABLE_CPSAT_WORKERS=1)
def test_the_cpsat_polisher_keeps_the_mark() -> None:
    from dataclasses import replace

    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat
    from tests.test_cpsat_resources import _clashing_pair_polish_inputs

    inputs = _clashing_pair_polish_inputs()
    inputs["current_sections"] = [
        replace(s, preferred_programme=mark)
        for s, mark in zip(inputs["current_sections"], ["AI", "DS"], strict=True)
    ]
    inputs["current_eval"] = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=inputs["current_sections"],
        student_profiles=inputs["student_profiles"],
        course_rigidity=inputs["course_rigidity"],
    )
    result = polish_scenario_with_cpsat(**inputs)
    assert result is not None, "the clashing pair must be polished apart"
    marks = {s.section_id: s.preferred_programme for s in result["improved_sections"]}
    assert marks == {"AI101_S1": "AI", "DS201_S1": "DS"}
